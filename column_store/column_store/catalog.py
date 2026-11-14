"""Store container: root header, catalog (the index), and validation.

On-disk layout (format version 1, see ``docs/FORMAT.md``)::

    <store>/HEADER           80-byte root header
    <store>/data/g000000.bin data pages for group 0, concatenated
    <store>/data/g000001.bin ...

The catalog is canonical UTF-8 JSON.  It is the index: schema, group order,
per-page offsets/lengths/checksums, encodings, and per-group statistics.  The
root header records the catalog's length, CRC-32 and SHA-256, so any damage to
the index is detected before a single data page is opened.  The header itself
ends with its own CRC-32.

Column names never appear in a path.  Group files are named from the group
ordinal and the catalog is required to use exactly that name; a catalog that
names anything else is rejected.
"""

import hashlib
import json
import os
import re
import struct
import zlib

from .codec import ENCODINGS_FOR_TYPE, PAGE_HEADER_SIZE
from .errors import StoreFormatError, StoreIntegrityError
from .schema import (BOOLEAN, INTEGER, INT64_MAX, INT64_MIN, STRING,
                     make_schema)

HEADER_MAGIC = b"CSTORE01"
HEADER_SIZE = 80
HEADER_VERSION = 1
FORMAT_NAME = "column-store"
FORMAT_VERSION = 1
_HEADER = struct.Struct(">8sIIQQI32sQI")
_HEADER_CRC_END = 76

CATALOG_MAX_BYTES = 256 * 1024 * 1024
GROUP_FILE_RE = re.compile(r"^g([0-9]{6,})\.bin$")
DATA_DIR = "data"

# Keys that define the whole document; anything else is a future format and
# is rejected rather than ignored.
_CATALOG_KEYS = {
    "format", "format_version", "row_group_max", "row_count", "group_count",
    "schema", "groups", "column_totals",
}
_GROUP_KEYS = {"ordinal", "rows", "file", "file_bytes", "columns"}
_COLUMN_KEYS = {"encoding", "offset", "length", "crc32", "null_count",
                "all_null", "min", "max"}
_TOTAL_KEYS = {"name", "type", "null_count", "all_null", "min", "max"}


class PageMeta(object):
    __slots__ = ("encoding", "offset", "length", "crc32", "null_count",
                 "all_null", "min", "max")

    def __init__(self, encoding, offset, length, crc32, null_count,
                 all_null, min_, max_):
        self.encoding = encoding
        self.offset = offset
        self.length = length
        self.crc32 = crc32
        self.null_count = null_count
        self.all_null = all_null
        self.min = min_
        self.max = max_

    def to_json(self):
        return {
            "encoding": self.encoding,
            "offset": self.offset,
            "length": self.length,
            "crc32": self.crc32,
            "null_count": self.null_count,
            "all_null": self.all_null,
            "min": self.min,
            "max": self.max,
        }


class GroupMeta(object):
    __slots__ = ("ordinal", "rows", "file", "file_bytes", "columns")

    def __init__(self, ordinal, rows, file, file_bytes, columns):
        self.ordinal = ordinal
        self.rows = rows
        self.file = file
        self.file_bytes = file_bytes
        self.columns = columns

    def to_json(self):
        return {
            "ordinal": self.ordinal,
            "rows": self.rows,
            "file": self.file,
            "file_bytes": self.file_bytes,
            "columns": [c.to_json() for c in self.columns],
        }


class ColumnTotal(object):
    __slots__ = ("name", "type", "null_count", "all_null", "min", "max")

    def __init__(self, name, type_, null_count, all_null, min_, max_):
        self.name = name
        self.type = type_
        self.null_count = null_count
        self.all_null = all_null
        self.min = min_
        self.max = max_

    def to_json(self):
        return {
            "name": self.name, "type": self.type,
            "null_count": self.null_count, "all_null": self.all_null,
            "min": self.min, "max": self.max,
        }


class StoreMeta(object):
    __slots__ = ("schema", "groups", "row_count", "row_group_max",
                 "column_totals", "catalog_bytes", "header_bytes",
                 "catalog_crc32", "catalog_sha256")

    def __init__(self, schema, groups, row_count, row_group_max,
                 column_totals, catalog_bytes, header_bytes, catalog_crc32,
                 catalog_sha256):
        self.schema = schema
        self.groups = groups
        self.row_count = row_count
        self.row_group_max = row_group_max
        self.column_totals = column_totals
        self.catalog_bytes = catalog_bytes
        self.header_bytes = header_bytes
        self.catalog_crc32 = catalog_crc32
        self.catalog_sha256 = catalog_sha256


def _is_int(value):
    return type(value) is int


def _check_unicode_string(value, where):
    if not isinstance(value, str):
        raise StoreFormatError("%s: expected a string" % where)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise StoreFormatError("%s: string is not valid Unicode" % where)
    return value


def _check_int64(value, where):
    if not _is_int(value):
        raise StoreFormatError("%s: expected an integer" % where)
    if value < INT64_MIN or value > INT64_MAX:
        raise StoreFormatError("%s: integer outside signed 64-bit range"
                               % where)
    return value


def _check_bool(value, where):
    if not isinstance(value, bool):
        raise StoreFormatError("%s: expected true or false" % where)
    return value


def _check_nonneg_int(value, where):
    if not _is_int(value) or value < 0:
        raise StoreFormatError("%s: expected a non-negative integer" % where)
    return value


def encode_catalog(schema, groups, row_count, row_group_max, column_totals):
    doc = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "row_group_max": row_group_max,
        "row_count": row_count,
        "group_count": len(groups),
        "schema": [c.to_json() for c in schema],
        "groups": [g.to_json() for g in groups],
        "column_totals": [t.to_json() for t in column_totals],
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def make_header(catalog_bytes):
    crc = zlib.crc32(catalog_bytes) & 0xFFFFFFFF
    digest = hashlib.sha256(catalog_bytes).digest()
    head = _HEADER.pack(HEADER_MAGIC, HEADER_VERSION, 0, HEADER_SIZE,
                        len(catalog_bytes), crc, digest, 0, 0)
    header_crc = zlib.crc32(head[:_HEADER_CRC_END]) & 0xFFFFFFFF
    return head[:_HEADER_CRC_END] + struct.pack(">I", header_crc)


def read_root_header(store_dir, counters=None):
    path = os.path.join(store_dir, "HEADER")
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise StoreFormatError("cannot open store header %s: %s"
                               % (path, exc))
    try:
        size = os.fstat(fd).st_size
        raw = os.pread(fd, HEADER_SIZE, 0)
        if counters is not None:
            counters["header_bytes"] += len(raw)
    except OSError as exc:
        raise StoreFormatError("cannot read store header %s: %s"
                               % (path, exc))
    finally:
        os.close(fd)
    if size < HEADER_SIZE or len(raw) != HEADER_SIZE:
        raise StoreFormatError("store header is truncated (%d of %d bytes)"
                               % (size, HEADER_SIZE))
    (magic, version, flags, catalog_offset, catalog_length, catalog_crc,
     catalog_sha, reserved, header_crc) = _HEADER.unpack(raw)
    if magic != HEADER_MAGIC:
        raise StoreFormatError("bad store magic (not a column_store)")
    actual = zlib.crc32(raw[:_HEADER_CRC_END]) & 0xFFFFFFFF
    if actual != header_crc:
        raise StoreIntegrityError("store header checksum mismatch")
    if flags != 0:
        raise StoreFormatError("unsupported header flags 0x%x" % flags)
    if reserved != 0:
        raise StoreFormatError("reserved header field is not zero")
    if version != HEADER_VERSION:
        raise StoreFormatError("unsupported store format version %d "
                               "(this reader supports %d)"
                               % (version, HEADER_VERSION))
    if catalog_offset != HEADER_SIZE:
        raise StoreFormatError("catalog offset %d is not %d in format 1"
                               % (catalog_offset, HEADER_SIZE))
    if catalog_length <= 0 or catalog_length > CATALOG_MAX_BYTES:
        raise StoreFormatError("catalog length %d is out of range"
                               % catalog_length)
    expected_size = catalog_offset + catalog_length
    if size < expected_size:
        raise StoreFormatError("store is truncated: catalog needs %d bytes, "
                               "file has %d" % (expected_size, size))
    if size != expected_size:
        raise StoreFormatError("store has %d trailing bytes after the catalog"
                               % (size - expected_size))
    return {
        "version": version,
        "catalog_offset": catalog_offset,
        "catalog_length": catalog_length,
        "catalog_crc32": catalog_crc,
        "catalog_sha256": catalog_sha,
        "header_bytes": raw,
    }


def read_catalog(store_dir, counters=None):
    head = read_root_header(store_dir, counters)
    path = os.path.join(store_dir, "HEADER")
    length = head["catalog_length"]
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            raw = os.pread(fd, length, head["catalog_offset"])
        finally:
            os.close(fd)
        if counters is not None:
            counters["catalog_bytes"] += len(raw)
    except OSError as exc:
        raise StoreFormatError("cannot read store catalog: %s" % exc)
    if len(raw) != length:
        raise StoreFormatError("store catalog is truncated (%d of %d bytes)"
                               % (len(raw), length))
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    if crc != head["catalog_crc32"]:
        raise StoreIntegrityError("catalog (index) checksum mismatch")
    if hashlib.sha256(raw).digest() != head["catalog_sha256"]:
        raise StoreIntegrityError("catalog (index) digest mismatch")
    return head, raw


def load_store(store_dir, counters=None):
    """Read and fully validate the header and index.  No data page is read."""
    if not os.path.isdir(store_dir):
        raise StoreFormatError("store directory does not exist: %s"
                               % store_dir)
    head, raw = read_catalog(store_dir, counters)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StoreFormatError("catalog is not valid UTF-8 JSON: %s" % exc)
    meta = parse_catalog(doc)
    meta.header_bytes = len(head["header_bytes"])
    meta.catalog_bytes = len(raw)
    meta.catalog_crc32 = head["catalog_crc32"]
    meta.catalog_sha256 = head["catalog_sha256"].hex()
    # Data files are deliberately not inspected here: only pages a query
    # actually requests are opened, so damage in an unused page or a skipped
    # group cannot affect an unrelated query.
    return meta


def _expect_keys(obj, keys, where):
    if not isinstance(obj, dict):
        raise StoreFormatError("%s: expected an object" % where)
    missing = keys - set(obj)
    extra = set(obj) - keys
    if missing:
        raise StoreFormatError("%s: missing key(s): %s"
                               % (where, ", ".join(sorted(missing))))
    if extra:
        raise StoreFormatError("%s: unknown key(s): %s"
                               % (where, ", ".join(sorted(extra))))
    return obj


def parse_catalog(doc):
    _expect_keys(doc, _CATALOG_KEYS, "catalog")
    if doc["format"] != FORMAT_NAME:
        raise StoreFormatError("catalog format is not %r" % FORMAT_NAME)
    if not _is_int(doc["format_version"]):
        raise StoreFormatError("catalog format_version must be an integer")
    if doc["format_version"] != FORMAT_VERSION:
        raise StoreFormatError("unsupported catalog format version %r "
                               "(this reader supports %d)"
                               % (doc["format_version"], FORMAT_VERSION))
    row_group_max = _check_nonneg_int(doc["row_group_max"],
                                      "catalog row_group_max")
    if row_group_max < 1:
        raise StoreFormatError("catalog row_group_max must be at least 1")
    row_count = _check_nonneg_int(doc["row_count"], "catalog row_count")
    group_count = _check_nonneg_int(doc["group_count"],
                                    "catalog group_count")
    schema_doc = doc["schema"]
    if not isinstance(schema_doc, list):
        raise StoreFormatError("catalog schema must be an array")
    try:
        schema = make_schema(schema_doc)
    except Exception as exc:
        raise StoreFormatError("catalog schema is invalid: %s" % exc)
    groups_doc = doc["groups"]
    if not isinstance(groups_doc, list):
        raise StoreFormatError("catalog groups must be an array")
    if len(groups_doc) != group_count:
        raise StoreFormatError("catalog group_count %d does not match "
                               "%d group entries"
                               % (group_count, len(groups_doc)))
    groups = []
    total_rows = 0
    for gi, gdoc in enumerate(groups_doc):
        where = "group %d" % gi
        _expect_keys(gdoc, _GROUP_KEYS, where)
        ordinal = _check_nonneg_int(gdoc["ordinal"], where + " ordinal")
        if ordinal != gi:
            raise StoreFormatError("%s: ordinal %d is out of sequence"
                                   % (where, ordinal))
        rows = _check_nonneg_int(gdoc["rows"], where + " rows")
        if rows < 1:
            raise StoreFormatError("%s: row count must be at least 1" % where)
        if rows > row_group_max:
            raise StoreFormatError("%s: %d rows exceeds row_group_max %d"
                                   % (where, rows, row_group_max))
        fname = gdoc["file"]
        if not isinstance(fname, str):
            raise StoreFormatError("%s: file name must be a string" % where)
        m = GROUP_FILE_RE.match(fname)
        if (m is None or int(m.group(1)) != gi
                or ("%06d" % gi) != m.group(1)):
            raise StoreFormatError("%s: invalid data file name %r"
                                   % (where, fname))
        file_bytes = _check_nonneg_int(gdoc["file_bytes"],
                                       where + " file_bytes")
        cols_doc = gdoc["columns"]
        if not isinstance(cols_doc, list):
            raise StoreFormatError("%s: columns must be an array" % where)
        if len(cols_doc) != len(schema):
            raise StoreFormatError("%s: %d column entries for a %d column "
                                   "schema" % (where, len(cols_doc),
                                               len(schema)))
        pages = []
        expected_offset = 0
        for ci, cdoc in enumerate(cols_doc):
            cwhere = "%s column %d" % (where, ci)
            _expect_keys(cdoc, _COLUMN_KEYS, cwhere)
            col = schema[ci]
            encoding = cdoc["encoding"]
            if encoding not in ENCODINGS_FOR_TYPE[col.type]:
                raise StoreFormatError("%s: encoding %r is not valid for "
                                       "type %s"
                                       % (cwhere, encoding, col.type))
            offset = _check_nonneg_int(cdoc["offset"], cwhere + " offset")
            length = _check_nonneg_int(cdoc["length"], cwhere + " length")
            if length < PAGE_HEADER_SIZE:
                raise StoreFormatError("%s: page length %d is below the "
                                       "page header size" % (cwhere, length))
            if offset != expected_offset:
                raise StoreFormatError("%s: page offset %d is not the "
                                       "contiguous expected offset %d"
                                       % (cwhere, offset, expected_offset))
            expected_offset = offset + length
            crc = _check_nonneg_int(cdoc["crc32"], cwhere + " crc32")
            if crc > 0xFFFFFFFF:
                raise StoreFormatError("%s: crc32 is out of range" % cwhere)
            null_count = _check_nonneg_int(cdoc["null_count"],
                                           cwhere + " null_count")
            if null_count > rows:
                raise StoreFormatError("%s: null_count %d exceeds group "
                                       "rows %d" % (cwhere, null_count, rows))
            all_null = _check_bool(cdoc["all_null"], cwhere + " all_null")
            if all_null != (null_count == rows):
                raise StoreFormatError("%s: all_null=%r disagrees with "
                                       "null_count=%d and rows=%d"
                                       % (cwhere, all_null, null_count, rows))
            if not col.nullable and null_count:
                raise StoreFormatError("%s: non-nullable column has %d nulls"
                                       % (cwhere, null_count))
            min_v, max_v = _check_stats(col, cdoc["min"], cdoc["max"],
                                        null_count, rows, cwhere)
            pages.append(PageMeta(encoding, offset, length, crc,
                                  null_count, all_null, min_v, max_v))
        if expected_offset != file_bytes:
            raise StoreFormatError("%s: page bytes sum to %d but file_bytes "
                                   "is %d" % (where, expected_offset,
                                              file_bytes))
        groups.append(GroupMeta(gi, rows, fname, file_bytes, pages))
        total_rows += rows
    if total_rows != row_count:
        raise StoreFormatError("catalog row_count %d does not match the sum "
                               "of group rows %d" % (row_count, total_rows))
    totals_doc = doc["column_totals"]
    if not isinstance(totals_doc, list) or len(totals_doc) != len(schema):
        raise StoreFormatError("catalog column_totals must be a %d element "
                               "array" % len(schema))
    totals = []
    for ci, tdoc in enumerate(totals_doc):
        where = "column_totals[%d]" % ci
        _expect_keys(tdoc, _TOTAL_KEYS, where)
        col = schema[ci]
        if tdoc["name"] != col.name or tdoc["type"] != col.type:
            raise StoreFormatError("%s: name/type does not match schema"
                                   % where)
        null_count = _check_nonneg_int(tdoc["null_count"],
                                       where + " null_count")
        if null_count > row_count:
            raise StoreFormatError("%s: null_count exceeds row_count" % where)
        all_null = _check_bool(tdoc["all_null"], where + " all_null")
        expected_all = (row_count > 0 and null_count == row_count)
        if all_null != expected_all:
            raise StoreFormatError("%s: all_null flag is inconsistent"
                                   % where)
        min_v, max_v = _check_stats(col, tdoc["min"], tdoc["max"],
                                    null_count, row_count, where)
        totals.append(ColumnTotal(col.name, col.type, null_count, all_null,
                                  min_v, max_v))
    _verify_totals(schema, groups, totals, row_count)
    return StoreMeta(schema, groups, row_count, row_group_max, totals,
                     None, None, None, None)


def _check_stats(col, min_v, max_v, null_count, rows, where):
    if col.type == BOOLEAN:
        if min_v is not None or max_v is not None:
            raise StoreFormatError("%s: boolean columns have no min/max"
                                   % where)
        return None, None
    all_null = (null_count == rows)
    if all_null:
        if min_v is not None or max_v is not None:
            raise StoreFormatError("%s: all-null column must have null "
                                   "min/max" % where)
        return None, None
    if col.type == INTEGER:
        min_v = _check_int64(min_v, where + " min")
        max_v = _check_int64(max_v, where + " max")
        if min_v > max_v:
            raise StoreFormatError("%s: min > max" % where)
        return min_v, max_v
    min_v = _check_unicode_string(min_v, where + " min")
    max_v = _check_unicode_string(max_v, where + " max")
    if min_v > max_v:
        raise StoreFormatError("%s: min > max" % where)
    return min_v, max_v


def _verify_totals(schema, groups, totals, row_count):
    for ci, col in enumerate(schema):
        nul = 0
        min_v = None
        max_v = None
        for g in groups:
            p = g.columns[ci]
            nul += p.null_count
            if p.min is not None:
                if min_v is None or p.min < min_v:
                    min_v = p.min
                if max_v is None or p.max > max_v:
                    max_v = p.max
        t = totals[ci]
        if nul != t.null_count:
            raise StoreFormatError("column_totals[%d]: null_count %d does "
                                   "not match group sum %d"
                                   % (ci, t.null_count, nul))
        if t.min != min_v or t.max != max_v:
            raise StoreFormatError("column_totals[%d]: min/max does not "
                                   "match group statistics" % ci)
        if row_count == 0 and t.null_count != 0:
            raise StoreFormatError("column_totals[%d]: empty store has nulls"
                                   % ci)


def group_path(store_dir, group):
    return os.path.join(store_dir, DATA_DIR, group.file)


def write_catalog(store_dir, catalog_bytes):
    header = make_header(catalog_bytes)
    with open(os.path.join(store_dir, "HEADER"), "wb") as fh:
        fh.write(header)
        fh.write(catalog_bytes)
        fh.flush()
        os.fsync(fh.fileno())
