"""scan: predicate pushdown, projection and the audited read path.

Contract highlights (all enforced here):

* conditions are combined with AND; literals are validated against the
  column type before any page is opened;
* a group is skipped from its catalog statistics alone when no row in it can
  match (nulls never satisfy a comparison; ``is_null``/``is_not_null`` use
  the null count; integer/string ranges use the recorded min/max, never a
  decimal string or dictionary-index ordering);
* for groups that survive, only the columns named in the predicate and the
  projection are opened, and only those pages are read and decoded;
* every read is an exact ``pread`` of the page bytes recorded in the index
  followed by a CRC check, so the reported byte count is measured I/O;
* output rows keep source order and projected column order.  Nothing is
  written until every needed page has been read and validated.
"""

import json
import os
import tempfile
import zlib

from .catalog import group_path, load_store
from .codec import decode_page
from .errors import StoreIntegrityError, UsageError
from .schema import BOOLEAN, INTEGER

OPS_VALUE = ("eq", "lt", "le", "gt", "ge")
OPS_NULL = ("is_null", "is_not_null")
OPS = OPS_VALUE + OPS_NULL


class PageReadStats(object):
    __slots__ = ("column", "group", "offset", "length")

    def __init__(self, column, group, offset, length):
        self.column = column
        self.group = group
        self.offset = offset
        self.length = length

    def to_json(self):
        return {"group": self.group, "column": self.column,
                "offset": self.offset, "length": self.length}


class ScanReport(object):
    __slots__ = ("row_groups_total", "row_groups_checked",
                 "row_groups_skipped_by_stats",
                 "row_groups_skipped_by_reason", "pages_read", "bytes_read",
                 "rows_matched", "rows_emitted", "columns_read",
                 "projection", "conditions", "catalog_bytes_read",
                 "header_bytes_read", "store_path", "column_names")

    def __init__(self):
        self.row_groups_total = 0
        self.row_groups_checked = 0
        self.row_groups_skipped_by_stats = 0
        self.row_groups_skipped_by_reason = []
        self.pages_read = []
        self.bytes_read = 0
        self.rows_matched = 0
        self.rows_emitted = 0
        self.columns_read = []
        self.projection = []
        self.conditions = []
        self.catalog_bytes_read = 0
        self.header_bytes_read = 0
        self.store_path = None
        self.column_names = {}

    def to_json(self):
        by_column = {}
        for p in self.pages_read:
            name = self.column_names.get(p.column, p.column)
            by_column[name] = by_column.get(name, 0) + p.length
        return {
            "store": self.store_path,
            "row_groups_total": self.row_groups_total,
            "row_groups_checked": self.row_groups_checked,
            "row_groups_skipped_by_stats":
                self.row_groups_skipped_by_stats,
            "row_groups_skipped_by_reason": self.row_groups_skipped_by_reason,
            "pages_read": len(self.pages_read),
            "bytes_read_by_column": by_column,
            "page_reads": [p.to_json() for p in self.pages_read],
            "bytes_read_from_data_pages": self.bytes_read,
            "rows_matched": self.rows_matched,
            "rows_emitted": self.rows_emitted,
            "columns_read": self.columns_read,
            "projection": self.projection,
            "conditions": self.conditions,
            "index_bytes_read": {"header": self.header_bytes_read,
                                 "catalog": self.catalog_bytes_read},
        }


class Predicate(object):
    __slots__ = ("column", "position", "type", "op", "literal")

    def __init__(self, column, position, type_, op, literal):
        self.column = column
        self.position = position
        self.type = type_
        self.op = op
        self.literal = literal

    def to_json(self):
        return {"column": self.column, "op": self.op,
                "value": self.literal}


def _literal_for(col, op, raw, index):
    where = "condition %d (%s)" % (index, col.name)
    if op in OPS_NULL:
        if raw is not None:
            raise UsageError("%s: %s takes no comparison value" % (where, op))
        return None
    if op not in OPS_VALUE:
        raise UsageError("%s: unsupported operator %r (supported: %s)"
                         % (where, op, ", ".join(OPS)))
    if col.type == BOOLEAN:
        raise UsageError("%s: value comparison is only defined for integer "
                         "and string columns" % where)
    if raw is None:
        raise UsageError("%s: a null comparison value is not allowed; use "
                         "is_null or is_not_null" % where)
    if col.type == INTEGER:
        if type(raw) is not int:
            raise UsageError("%s: comparison value must be an integer, got %s"
                             % (where, _type_name(raw)))
        # Any JSON integer is a valid literal.  Values outside the stored
        # int64 range are not an error: the comparison simply can never hold
        # (or always holds), and the statistics test decides that per group.
        return raw
    if not isinstance(raw, str):
        raise UsageError("%s: comparison value must be a string, got %s"
                         % (where, _type_name(raw)))
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("%s: comparison value is not valid Unicode" % where)
    return raw


def _has_value_slot(cond):
    """True when a null-test unexpectedly carries a non-null value.

    ``[col, "is_null", None]`` is accepted (the value slot is simply empty);
    any other value alongside ``is_null``/``is_not_null`` is a usage error.
    """
    if isinstance(cond, (list, tuple)):
        return len(cond) >= 3 and cond[2] is not None
    return "value" in cond and cond["value"] is not None


def _type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, str):
        return "string"
    if isinstance(v, float):
        return "number"
    return type(v).__name__


def prepare(schema, projection, conditions):
    """Validate projection and conditions against *schema*.

    Returns (column positions of the projection, list of Predicate, list of
    positions needed for filtering).  No store is opened here.
    """
    index = {c.name: i for i, c in enumerate(schema)}
    if not isinstance(projection, (list, tuple)) or not projection:
        raise UsageError("projection must name at least one column")
    seen = set()
    proj_pos = []
    for name in projection:
        if not isinstance(name, str):
            raise UsageError("projection column names must be strings")
        if name not in index:
            raise UsageError("projection names an unknown column: %r" % name)
        if name in seen:
            raise UsageError("projection lists %r more than once" % name)
        seen.add(name)
        proj_pos.append(index[name])
    if not isinstance(conditions, (list, tuple)):
        raise UsageError("conditions must be a list")
    conditions = list(conditions)
    predicates = []
    for k, cond in enumerate(conditions):
        if isinstance(cond, (list, tuple)):
            if len(cond) != 3:
                raise UsageError("condition %d must be [column, op, value]"
                                 % k)
            cname, op, raw = cond
        elif isinstance(cond, dict):
            unknown = set(cond) - {"column", "op", "value"}
            if unknown or "column" not in cond or "op" not in cond:
                raise UsageError("condition %d must have column, op%s"
                                 % (k, ", value" if "value" in cond else ""))
            cname, op, raw = cond["column"], cond["op"], cond.get("value")
        else:
            raise UsageError("condition %d must be [column, op, value] or an "
                             "object" % k)
        if not isinstance(cname, str) or cname not in index:
            raise UsageError("condition %d names an unknown column: %r"
                             % (k, cname))
        if not isinstance(op, str):
            raise UsageError("condition %d: op must be a string" % k)
        col = schema[index[cname]]
        if op in OPS_NULL:
            if _has_value_slot(cond):
                raise UsageError("condition %d: %s takes no value"
                                 % (k, op))
            literal = None
        else:
            if op not in OPS_VALUE:
                raise UsageError("condition %d: unsupported operator %r "
                                 "(supported: %s)"
                                 % (k, op, ", ".join(OPS)))
            if not isinstance(cond, (list, tuple)):
                if "value" not in cond:
                    raise UsageError("condition %d: missing value" % k)
            literal = _literal_for(col, op, raw, k)
        predicates.append(Predicate(cname, index[cname], col.type, op,
                                    literal))
    need = set(proj_pos)
    need.update(p.position for p in predicates)
    return proj_pos, predicates, sorted(need)


def group_may_match(rows, pages, predicates):
    """Statistics-only test.  Returns (keep, reason_if_skipped).

    Uses only the catalog's per-group null count and min/max.  A group is
    skipped only when no row in it can possibly satisfy the predicate, so
    the test is conservative: a false "may match" costs a page read, never a
    wrong result.  Comparisons use the column's native ordering (integers
    numerically, strings by code point).
    """
    for pred in predicates:
        page = pages[pred.position]
        nul = page.null_count
        if pred.op == "is_null":
            if nul == 0:
                return False, "is_null(%s): no nulls in group" % pred.column
            continue
        if pred.op == "is_not_null":
            if nul == rows:
                return False, ("is_not_null(%s): group is all null"
                               % pred.column)
            continue
        # Value comparisons never hold for null.  A group that is entirely
        # null in this column cannot contribute a matching row.
        if nul == rows:
            return False, ("group is all null in column %r" % pred.column)
        lo, hi = page.min, page.max
        if lo is None or hi is None:
            raise StoreIntegrityError(
                "group statistics for column %r are missing although the "
                "group has non-null rows" % pred.column)
        if pred.op == "eq":
            if pred.literal < lo or pred.literal > hi:
                return False, ("eq(%s): %r outside group range [%r, %r]"
                               % (pred.column, pred.literal, lo, hi))
        elif pred.op == "lt":
            if not lo < pred.literal:
                return False, ("lt(%s): group min %r is not below %r"
                               % (pred.column, lo, pred.literal))
        elif pred.op == "le":
            if not lo <= pred.literal:
                return False, ("le(%s): group min %r is above %r"
                               % (pred.column, lo, pred.literal))
        elif pred.op == "gt":
            if not hi > pred.literal:
                return False, ("gt(%s): group max %r is not above %r"
                               % (pred.column, hi, pred.literal))
        elif pred.op == "ge":
            if not hi >= pred.literal:
                return False, ("ge(%s): group max %r is below %r"
                               % (pred.column, hi, pred.literal))
        else:
            raise AssertionError(pred.op)
    return True, None


def _compare(op, value, literal):
    if op == "eq":
        return value == literal
    if op == "lt":
        return value < literal
    if op == "le":
        return value <= literal
    if op == "gt":
        return value > literal
    if op == "ge":
        return value >= literal
    raise AssertionError(op)


def _row_matches(values, predicates):
    for pred in predicates:
        v = values[pred.position]
        op = pred.op
        if op == "is_null":
            if v is not None:
                return False
        elif op == "is_not_null":
            if v is None:
                return False
        else:
            # SQL-style three valued logic: a null never satisfies a value
            # comparison.
            if v is None:
                return False
            if not _compare(op, v, pred.literal):
                return False
    return True


def _read_page(store_dir, group, page_meta, ci, type_, rows, report):
    path = group_path(store_dir, group)
    length = page_meta.length
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise StoreIntegrityError("cannot open group file %s: %s"
                                  % (group.file, exc))
    try:
        try:
            size = os.fstat(fd).st_size
        except OSError as exc:
            raise StoreIntegrityError("cannot stat group file %s: %s"
                                      % (group.file, exc))
        # Only the page range actually requested decides success here; a
        # neighbouring page may be damaged without affecting this query.
        if page_meta.offset + length > size:
            raise StoreIntegrityError(
                "page for column %d in group %d is truncated: needs bytes "
                "%d..%d, file has %d"
                % (ci, group.ordinal, page_meta.offset,
                   page_meta.offset + length, size))
        raw = os.pread(fd, length, page_meta.offset)
    finally:
        os.close(fd)
    if len(raw) != length:
        raise StoreIntegrityError(
            "short read for column %d in group %d: got %d of %d bytes"
            % (ci, group.ordinal, len(raw), length))
    if zlib.crc32(raw) & 0xFFFFFFFF != page_meta.crc32:
        raise StoreIntegrityError(
            "page checksum mismatch for group %d column %d (index says "
            "crc32 %08x)" % (group.ordinal, ci, page_meta.crc32))
    page = decode_page(raw, page_meta.offset, type_, rows,
                       page_meta.encoding)
    if page.null_count != page_meta.null_count:
        raise StoreIntegrityError(
            "page null count %d for group %d column %d disagrees with index "
            "%d" % (page.null_count, group.ordinal, ci, page_meta.null_count))
    if type_ != BOOLEAN and page_meta.min is not None:
        # Cross-check the recorded statistics against the decoded page.  The
        # index already verified min/max are mutually consistent; this catches
        # an index whose numbers do not describe the actual values, which
        # could otherwise make a statistics-skipped group lose rows.
        nonnull = [v for v in page.values if v is not None]
        if nonnull:
            lo, hi = min(nonnull), max(nonnull)
            if lo != page_meta.min or hi != page_meta.max:
                raise StoreIntegrityError(
                    "page statistics mismatch for group %d column %d: index "
                    "says [%r, %r], page has [%r, %r]"
                    % (group.ordinal, ci, page_meta.min, page_meta.max,
                       lo, hi))
    report.pages_read.append(
        PageReadStats(ci, group.ordinal, page_meta.offset, length))
    report.bytes_read += length
    return page.values


def scan(store_path, projection, conditions=(), report=None):
    """Filter *store_path* and return (rows, report).

    ``rows`` is a list of tuples in the projection order, one per matching
    source row, kept in source order.  Raises before returning anything on
    any format or integrity problem.  For a large result use
    :func:`scan_to_file`, which writes each group as it is produced instead
    of accumulating every row.
    """
    counters = {"header_bytes": 0, "catalog_bytes": 0}
    meta = load_store(store_path, counters)
    rows_out = []
    _rows, report = _scan_meta(meta, store_path, projection, conditions,
                               report, sink=rows_out.extend)
    report.header_bytes_read = counters["header_bytes"]
    report.catalog_bytes_read = counters["catalog_bytes"]
    return rows_out, report


def _scan_meta(meta, store_path, projection, conditions, report, sink=None):
    proj_pos, predicates, need = prepare(meta.schema, projection, conditions)
    if report is None:
        report = ScanReport()
    report.store_path = store_path
    report.projection = list(projection)
    report.column_names = {i: c.name for i, c in enumerate(meta.schema)}
    report.conditions = [{"column": p.column, "op": p.op, "value": p.literal}
                         for p in predicates]
    report.row_groups_total = len(meta.groups)
    rows_out = []
    emitted = 0
    read_names = set()
    for group in meta.groups:
        keep, reason = group_may_match(group.rows, group.columns,
                                       predicates)
        if not keep:
            report.row_groups_skipped_by_stats += 1
            report.row_groups_skipped_by_reason.append(
                {"group": group.ordinal, "reason": reason})
            continue
        report.row_groups_checked += 1
        columns = {}
        for ci in need:
            columns[ci] = _read_page(store_path, group, group.columns[ci],
                                     ci, meta.schema[ci].type, group.rows,
                                     report)
        read_names.update(meta.schema[ci].name for ci in need)
        if predicates:
            survivors = [r for r in range(group.rows)
                         if _row_matches(_RowView(columns, r), predicates)]
        else:
            survivors = range(group.rows)
        report.rows_matched += len(survivors)
        batch = [tuple(columns[ci][r] for ci in proj_pos)
                 for r in survivors]
        emitted += len(batch)
        if sink is not None:
            sink(batch)
        else:
            rows_out.extend(batch)
    report.rows_emitted = emitted
    report.columns_read = sorted(read_names)
    return rows_out, report


class _RowView(object):
    """Lazily pulls one predicate column for one row (no per-row dict)."""

    __slots__ = ("columns", "row")

    def __init__(self, columns, row):
        self.columns = columns
        self.row = row

    def __getitem__(self, pos):
        return self.columns[pos][self.row]


def scan_to_file(store_path, out_path, projection, conditions=(),
                 report_path=None):
    """Scan and write results to a *new* file, atomically.

    Rows are serialized to a hidden temporary file one group at a time and
    each batch is dropped immediately, so memory stays proportional to a row
    group, not to the number of matches.  Returns ``(rows_written, report)``.

    The temporary file is renamed onto ``out_path`` only after every needed
    page has been read and validated and the report (if any) has been
    written; any failure removes the temporary files, so a failed scan never
    leaves a partial result or clobbers an existing path.  ``out_path`` and
    ``report_path`` must not exist.
    """
    from .errors import OutputRefused
    if os.path.lexists(out_path):
        raise OutputRefused("refusing to overwrite an existing path: %s"
                            % out_path)
    if report_path is not None and os.path.lexists(report_path):
        raise OutputRefused("refusing to overwrite an existing path: %s"
                            % report_path)
    counters = {"header_bytes": 0, "catalog_bytes": 0}
    # The index, projection and predicates are validated before any output
    # directory or temporary file exists.
    meta = load_store(store_path, counters)
    prepare(meta.schema, projection, conditions)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise UsageError("cannot create output directory %s: %s"
                         % (out_dir, exc))
    fd, tmp = tempfile.mkstemp(prefix=".scan-", dir=out_dir)
    report_tmp = None
    try:
        with os.fdopen(fd, "wb") as fh:
            def write_batch(batch):
                # Each group's rows are serialized and dropped; memory does
                # not grow with the number of matches.
                for row in batch:
                    fh.write(json.dumps(row, ensure_ascii=False,
                                        separators=(",", ":")).encode(
                                            "utf-8"))
                    fh.write(b"\n")
            _rows, report = _scan_meta(meta, store_path, projection,
                                       conditions, None, sink=write_batch)
            report.header_bytes_read = counters["header_bytes"]
            report.catalog_bytes_read = counters["catalog_bytes"]
            fh.flush()
            os.fsync(fh.fileno())
        if report_path is not None:
            rfd, report_tmp = tempfile.mkstemp(prefix=".report-",
                                               dir=out_dir)
            with os.fdopen(rfd, "wb") as fh:
                fh.write(json.dumps(report.to_json(), ensure_ascii=False,
                                    indent=2, sort_keys=True).encode(
                                        "utf-8"))
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
        if os.path.lexists(out_path):
            raise OutputRefused("refusing to overwrite an existing path: %s"
                                % out_path)
        os.replace(tmp, out_path)
        tmp = None
        if report_tmp is not None:
            os.replace(report_tmp, report_path)
            report_tmp = None
    finally:
        for leftover in (tmp, report_tmp):
            if leftover is not None:
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
    return report.rows_emitted, report
