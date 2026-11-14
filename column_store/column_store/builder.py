"""build: import a UTF-8 JSONL file into a new column store.

Only the current row group is held in memory.  For every group the builder
collects one Python value per declared column, then encodes each column into
its own page and appends the page bytes to that group's data file.  Nothing
from a completed group survives into the next one except 64-bit statistics.

The destination is built in a sibling temporary directory and renamed into
place only after every row has been validated and every page written.  An
existing path is refused up front and a failed build leaves no destination.
"""

import errno
import os
import shutil
import tempfile

from .catalog import (DATA_DIR, GroupMeta, ColumnTotal, PageMeta,
                      encode_catalog, write_catalog)
from .codec import encode_page
from .errors import InputError, OutputRefused, StoreFormatError
from .jsonl import prime_schema, scan_record

DEFAULT_ROW_GROUP_MAX = 4096
DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STRING_BYTES = 4 * 1024 * 1024
MAX_ROW_GROUP_MAX = 1 << 24


def _group_filename(ordinal):
    return "g%06d.bin" % ordinal


def _open_source(path):
    if path == "-":
        import sys
        return sys.stdin.buffer, False
    try:
        return open(path, "rb"), True
    except OSError as exc:
        raise InputError(0, "cannot open source file", str(exc))


def _iter_lines(fh, max_line_bytes):
    """Yield (line_no, bytes) for each physical line, enforcing a size bound.

    ``max_line_bytes`` applies to the raw line excluding the newline.  A line
    that exceeds it is rejected by reading one byte more; memory stays
    bounded at the limit.
    """
    line_no = 0
    while True:
        chunk = fh.readline(max_line_bytes + 2)
        if not chunk:
            return
        line_no += 1
        if chunk.endswith(b"\n"):
            line = chunk[:-1]
        else:
            line = chunk
            if len(line) > max_line_bytes:
                raise InputError(line_no, "line exceeds the configured size "
                                 "bound", "%d bytes > %d"
                                 % (len(line), max_line_bytes))
            # EOF without newline: the scanner still has the whole line.
        if len(line) > max_line_bytes:
            raise InputError(line_no, "line exceeds the configured size "
                             "bound", "%d bytes > %d"
                             % (len(line), max_line_bytes))
        yield line_no, line


class BuildResult(object):
    __slots__ = ("row_count", "group_count", "column_totals", "path")

    def __init__(self, row_count, group_count, column_totals, path):
        self.row_count = row_count
        self.group_count = group_count
        self.column_totals = column_totals
        self.path = path

    def to_json(self):
        return {
            "path": self.path,
            "row_count": self.row_count,
            "group_count": self.group_count,
            "column_totals": [t.to_json() for t in self.column_totals],
        }


def _accumulate_stats(stats, values, schema):
    """Update per-column (null_count, min, max) for one group in place."""
    for ci, col in enumerate(schema):
        st = stats[ci]
        if col.type == "boolean":
            for v in values[ci]:
                if v is None:
                    st[0] += 1
            continue
        lo = st[1]
        hi = st[2]
        for v in values[ci]:
            if v is None:
                st[0] += 1
            else:
                if lo is None or v < lo:
                    lo = v
                if hi is None or v > hi:
                    hi = v
        st[1] = lo
        st[2] = hi


def build(source_path, store_path, schema, row_group_max=DEFAULT_ROW_GROUP_MAX,
          max_line_bytes=DEFAULT_MAX_LINE_BYTES,
          max_string_bytes=DEFAULT_MAX_STRING_BYTES,
          verbose=False):
    if type(row_group_max) is not int or row_group_max < 1:
        raise StoreFormatError("row group size must be a positive integer")
    if row_group_max > MAX_ROW_GROUP_MAX:
        raise StoreFormatError("row group size %d exceeds the limit %d"
                               % (row_group_max, MAX_ROW_GROUP_MAX))
    if type(max_line_bytes) is not int or max_line_bytes < 1:
        raise StoreFormatError("line size bound must be a positive integer")
    if type(max_string_bytes) is not int or max_string_bytes < 1:
        raise StoreFormatError("string size bound must be a positive integer")
    if max_string_bytes > max_line_bytes:
        # A decoded string can be larger than its JSON escape sequence (an
        # escaped surrogate pair is 12 source bytes for 4 output bytes) but
        # never dramatically; keeping the bound below the line bound keeps
        # every intermediate allocation capped.
        raise StoreFormatError("string size bound must not exceed the line "
                               "size bound")
    schema = tuple(schema)
    prime_schema(schema)
    store_path = os.path.abspath(store_path)
    if os.path.lexists(store_path):
        raise OutputRefused("refusing to overwrite an existing path: %s"
                            % store_path)
    parent = os.path.dirname(store_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise OutputRefused("cannot create parent directory %s: %s"
                            % (parent, exc))
    work = tempfile.mkdtemp(prefix=".build-" + os.path.basename(store_path)
                            + "-", dir=parent)
    committed = False
    try:
        os.mkdir(os.path.join(work, DATA_DIR))
        src, close_src = _open_source(source_path)
        try:
            result = _build_into(work, src, schema, row_group_max,
                                 max_line_bytes, max_string_bytes, verbose)
        finally:
            if close_src:
                src.close()
        # Publish atomically; lexists is rechecked to close the race window
        # as far as a local filesystem allows.
        if os.path.lexists(store_path):
            raise OutputRefused("refusing to overwrite an existing path: %s"
                                % store_path)
        try:
            os.rename(work, store_path)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise OutputRefused("refusing to overwrite an existing "
                                    "path: %s" % store_path)
            raise
        committed = True
        return BuildResult(result["row_count"], result["group_count"],
                           result["column_totals"], store_path)
    finally:
        if not committed:
            shutil.rmtree(work, ignore_errors=True)


def _build_into(work, src, schema, row_group_max, max_line_bytes,
                max_string_bytes, verbose):
    width = len(schema)
    groups = []
    totals = [[0, None, None] for _ in range(width)]   # nulls, min, max
    current = [[] for _ in range(width)]
    pending = 0
    row_count = 0
    ordinal = 0

    def flush():
        nonlocal ordinal, pending
        if pending == 0:
            return
        rows = pending
        fname = _group_filename(ordinal)
        fpath = os.path.join(work, DATA_DIR, fname)
        stats = [[0, None, None] for _ in range(width)]
        _accumulate_stats(stats, current, schema)
        pages = []
        offset = 0
        with open(fpath, "wb") as out:
            for ci, col in enumerate(schema):
                values = current[ci]
                page_bytes, encoding, nul = encode_page(col.type, values)
                out.write(page_bytes)
                pages.append(PageMeta(encoding, offset, len(page_bytes),
                                      _crc_of(page_bytes), nul,
                                      nul == rows, stats[ci][1],
                                      stats[ci][2]))
                offset += len(page_bytes)
            out.flush()
            os.fsync(out.fileno())
        groups.append(GroupMeta(ordinal, rows, fname, offset, pages))
        for ci in range(width):
            totals[ci][0] += stats[ci][0]
            if stats[ci][1] is not None:
                if totals[ci][1] is None or stats[ci][1] < totals[ci][1]:
                    totals[ci][1] = stats[ci][1]
                if totals[ci][2] is None or stats[ci][2] > totals[ci][2]:
                    totals[ci][2] = stats[ci][2]
        ordinal += 1
        pending = 0
        current[:] = [[] for _ in range(width)]

    for line_no, line in _iter_lines(src, max_line_bytes):
        if not line.strip():
            raise InputError(line_no, "empty source line")
        values = scan_record(line, schema, line_no, max_string_bytes)
        for ci in range(width):
            current[ci].append(values[ci])
        pending += 1
        row_count += 1
        if pending >= row_group_max:
            flush()
    flush()
    column_totals = []
    for ci, col in enumerate(schema):
        nul, lo, hi = totals[ci]
        all_null = (row_count > 0 and nul == row_count)
        if col.type == "boolean" or all_null:
            lo = hi = None
        column_totals.append(ColumnTotal(col.name, col.type, nul, all_null,
                                         lo, hi))
    catalog_bytes = encode_catalog(schema, groups, row_count, row_group_max,
                                   column_totals)
    write_catalog(work, catalog_bytes)
    return {"row_count": row_count, "group_count": len(groups),
            "column_totals": column_totals}


def _crc_of(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF
