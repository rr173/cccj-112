"""Column page codec: null bitmap, encodings, page framing and CRCs.

Everything the store writes is described here and in ``docs/FORMAT.md``; the
decoder performs the same checks a second implementation would be expected to
perform.  No third-party storage library is involved.

A page is a fixed 32-byte header followed by ``null_bitmap + body``:

    offset size field
    0      4    magic b"CPG1"
    4      1    page kind (1 integer, 2 boolean, 3 string)
    5      1    page format version (1)
    6      1    flags (bit0 all_null, bit1 has_null)
    7      1    reserved, must be 0
    8      4    row_count (big endian)
    12     4    null_bytes = ceil(row_count / 8)
    16     8    body_len (bytes after the null bitmap)
    24     4    body_crc32 (CRC-32 of null_bitmap + body)
    28     4    header_crc32 (CRC-32 of header bytes 0..27)

The null bitmap uses one bit per row, LSB first; a set bit means *null*.
Values that are not null appear in the body in row order.  The null bitmap is
always present (all zero for a non-nullable column) so a reader can verify it
against the group row count independently.
"""

import struct
import zlib

from .errors import StoreIntegrityError
from .schema import BOOLEAN, INTEGER, STRING

PAGE_MAGIC = b"CPG1"
PAGE_VERSION = 1
PAGE_HEADER_SIZE = 32
_HEADER = struct.Struct(">4sBBBBIIQII")
_HEADER_CRC_LEN = 28

KIND_INTEGER = 1
KIND_BOOLEAN = 2
KIND_STRING = 3
KINDS = {INTEGER: KIND_INTEGER, BOOLEAN: KIND_BOOLEAN, STRING: KIND_STRING}

FLAG_ALL_NULL = 0x01
FLAG_HAS_NULL = 0x02

INT_ENCODING = "int64-plain"
BOOL_ENCODING = "bool-bitmap"
STR_DICT = "str-dict"
STR_DICT_RLE = "str-dict-rle"
STR_PREFIX = "str-prefix"
STRING_ENCODINGS = (STR_DICT, STR_DICT_RLE, STR_PREFIX)
ENCODINGS_FOR_TYPE = {
    INTEGER: (INT_ENCODING,),
    BOOLEAN: (BOOL_ENCODING,),
    STRING: STRING_ENCODINGS,
}

#: Tie-break rule when two string encodings produce the same byte length:
#: the earlier name in this tuple wins.  Used identically by the builder and
#: documented for independent readers.
STRING_ENCODING_PRECEDENCE = (STR_DICT, STR_DICT_RLE, STR_PREFIX)


def null_bitmap(values):
    """Build the null bitmap for *values* (a list with None for null)."""
    n = len(values)
    bitmap = bytearray((n + 7) // 8)
    count = 0
    for i, v in enumerate(values):
        if v is None:
            bitmap[i >> 3] |= 1 << (i & 7)
            count += 1
    return bytes(bitmap), count


def null_count(bitmap, rows):
    count = 0
    for i in range(rows):
        if bitmap[i >> 3] & (1 << (i & 7)):
            count += 1
    return count


def _encode_integer(values):
    buf = bytearray()
    pack = struct.Struct(">q").pack
    for v in values:
        if v is not None:
            buf += pack(v)
    return bytes(buf), INT_ENCODING


def _encode_boolean(values):
    nonnull = [v for v in values if v is not None]
    buf = bytearray((len(nonnull) + 7) // 8)
    for i, v in enumerate(nonnull):
        if v:
            buf[i >> 3] |= 1 << (i & 7)
    return bytes(buf), BOOL_ENCODING


def _dict_body(values, rle):
    index = {}
    entries = []
    ids = []
    append = ids.append
    for v in values:
        if v is None:
            continue
        got = index.get(v)
        if got is None:
            got = len(entries)
            index[v] = got
            entries.append(v)
        append(got)
    width = 1 if len(entries) <= 0x100 else (
        2 if len(entries) <= 0x10000 else 4)
    width_code = width
    fmt = {1: ">B", 2: ">H", 4: ">I"}[width]
    pack_idx = struct.Struct(fmt).pack
    out = bytearray()
    out += struct.pack(">BI", width_code, len(entries))
    for e in entries:
        raw = e.encode("utf-8")
        out += struct.pack(">I", len(raw))
        out += raw
    if not rle:
        for i in ids:
            out += pack_idx(i)
        return bytes(out)
    runs = []
    total = 0
    for i in ids:
        if runs and runs[-1][0] == i:
            runs[-1][1] += 1
        else:
            runs.append([i, 1])
        total += 1
    out += struct.pack(">I", len(runs))
    for i, length in runs:
        out += pack_idx(i)
        out += struct.pack(">I", length)
    assert total == len(ids)
    return bytes(out)


def _prefix_body(values):
    out = bytearray()
    for v in values:
        if v is None:
            continue
        raw = v.encode("utf-8")
        out += struct.pack(">I", len(raw))
        out += raw
    return bytes(out)


def choose_string_encoding(candidates):
    """Pick the smallest body; ties use STRING_ENCODING_PRECEDENCE.

    ``candidates`` is a sequence of ``(name, body_bytes)``.  This is a pure
    function so the tie rule can be tested directly.
    """
    names = set(STRING_ENCODING_PRECEDENCE)
    for name, _body in candidates:
        if name not in names:
            raise AssertionError("unknown string encoding candidate %r"
                                 % (name,))
    best = None
    for name, body in candidates:
        if best is None:
            best = (name, body)
            continue
        if len(body) < len(best[1]):
            best = (name, body)
        elif len(body) == len(best[1]) and (
                STRING_ENCODING_PRECEDENCE.index(name)
                < STRING_ENCODING_PRECEDENCE.index(best[0])):
            best = (name, body)
    if best is None:
        raise AssertionError("no string encoding candidates")
    return best[1], best[0]


def _encode_string(values):
    return choose_string_encoding([
        (STR_DICT, _dict_body(values, False)),
        (STR_DICT_RLE, _dict_body(values, True)),
        (STR_PREFIX, _prefix_body(values)),
    ])


def encode_body(type_, values):
    if type_ == INTEGER:
        return _encode_integer(values)
    if type_ == BOOLEAN:
        return _encode_boolean(values)
    if type_ == STRING:
        return _encode_string(values)
    raise AssertionError(type_)


def encode_page(type_, values, row_count=None):
    """Encode one column page.  Returns (bytes, encoding_name, null_count)."""
    if row_count is None:
        row_count = len(values)
    if row_count != len(values):
        raise AssertionError("row_count mismatch")
    bitmap, nul = null_bitmap(values)
    body, encoding = encode_body(type_, values)
    flags = 0
    if nul:
        flags |= FLAG_HAS_NULL
    if nul == row_count:
        flags |= FLAG_ALL_NULL
    header = _HEADER.pack(
        PAGE_MAGIC, KINDS[type_], PAGE_VERSION, flags, 0,
        row_count, len(bitmap), len(body),
        zlib.crc32(bitmap + body) & 0xFFFFFFFF, 0)
    header_crc = zlib.crc32(header[:_HEADER_CRC_LEN]) & 0xFFFFFFFF
    header = header[:_HEADER_CRC_LEN] + struct.pack(">I", header_crc)
    return header + bitmap + body, encoding, nul


class DecodedPage(object):
    __slots__ = ("values", "row_count", "null_count", "encoding", "flags")

    def __init__(self, values, row_count, nul, encoding, flags):
        self.values = values
        self.row_count = row_count
        self.null_count = nul
        self.encoding = encoding
        self.flags = flags


def _bad(offset, why):
    raise StoreIntegrityError("page at offset %d: %s" % (offset, why))


def decode_page(raw, offset, type_, row_count, encoding=None):
    """Validate and decode one page.

    ``raw`` must be exactly the page bytes (header + bitmap + body) recorded
    in the manifest.  ``row_count`` is the owning group's row count and must
    match the value in the page header.  For string columns ``encoding`` is
    mandatory and must come from the manifest.
    """
    if len(raw) < PAGE_HEADER_SIZE:
        _bad(offset, "page is truncated (%d bytes)" % len(raw))
    (magic, kind, version, flags, reserved, page_rows, null_bytes,
     body_len, body_crc, header_crc) = _HEADER.unpack(raw[:PAGE_HEADER_SIZE])
    if magic != PAGE_MAGIC:
        _bad(offset, "bad page magic")
    if version != PAGE_VERSION:
        _bad(offset, "unsupported page format version %d" % version)
    if reserved != 0:
        _bad(offset, "reserved header byte is not zero")
    actual_header_crc = zlib.crc32(raw[:_HEADER_CRC_LEN]) & 0xFFFFFFFF
    if actual_header_crc != header_crc:
        _bad(offset, "page header checksum mismatch")
    if kind != KINDS.get(type_):
        _bad(offset, "page kind does not match the declared column type")
    if page_rows != row_count:
        _bad(offset, "page row count %d does not match group rows %d"
             % (page_rows, row_count))
    expected_null = (row_count + 7) // 8
    if null_bytes != expected_null:
        _bad(offset, "null bitmap length %d does not match %d rows "
                    "(expected %d)" % (null_bytes, row_count, expected_null))
    if PAGE_HEADER_SIZE + null_bytes + body_len != len(raw):
        _bad(offset, "page length %d does not match header (%d+%d+%d)"
             % (len(raw), PAGE_HEADER_SIZE, null_bytes, body_len))
    body = raw[PAGE_HEADER_SIZE + null_bytes:]
    bitmap = raw[PAGE_HEADER_SIZE:PAGE_HEADER_SIZE + null_bytes]
    actual_body_crc = zlib.crc32(bitmap + body) & 0xFFFFFFFF
    if actual_body_crc != body_crc:
        _bad(offset, "null bitmap / body checksum mismatch")
    # Padding bits past the last row must be zero.
    if row_count & 7:
        pad_mask = ~((1 << (row_count & 7)) - 1) & 0xFF
        if bitmap[-1] & pad_mask:
            _bad(offset, "null bitmap has bits set past the last row")
    nul = null_count(bitmap, row_count)
    all_null = (nul == row_count)
    if bool(flags & FLAG_ALL_NULL) != all_null:
        _bad(offset, "all-null flag disagrees with the null bitmap")
    if bool(flags & FLAG_HAS_NULL) != bool(nul):
        _bad(offset, "has-null flag disagrees with the null bitmap")
    if type_ == STRING:
        if encoding not in STRING_ENCODINGS:
            _bad(offset, "string page has no valid encoding name")
        values = decode_string_body(body, bitmap, row_count, nul,
                                    encoding, offset)
    else:
        values = _decode_body(type_, body, bitmap, row_count, nul, offset)
        encoding = ENCODINGS_FOR_TYPE[type_][0]
    return DecodedPage(values, row_count, nul, encoding, flags)


def _iter_nonnull(bitmap, rows):
    for i in range(rows):
        if not bitmap[i >> 3] & (1 << (i & 7)):
            yield i


def _decode_body(type_, body, bitmap, rows, nul, offset):
    nonnull_rows = rows - nul
    if type_ == INTEGER:
        # The manifest supplies the encoding; integer has exactly one.
        if len(body) != nonnull_rows * 8:
            _bad(offset, "integer body length %d does not match %d values"
                 % (len(body), nonnull_rows))
        unpack = struct.Struct(">q").unpack_from
        values = [None] * rows
        pos = 0
        for i in range(rows):
            if bitmap[i >> 3] & (1 << (i & 7)):
                continue
            values[i] = unpack(body, pos)[0]
            pos += 8
        return values
    if type_ == BOOLEAN:
        expected = (nonnull_rows + 7) // 8
        if len(body) != expected:
            _bad(offset, "boolean body length %d does not match %d values"
                 % (len(body), expected))
        values = [None] * rows
        k = 0
        for i in range(rows):
            if bitmap[i >> 3] & (1 << (i & 7)):
                continue
            values[i] = bool(body[k >> 3] & (1 << (k & 7)))
            k += 1
        return values
    raise AssertionError(type_)


def decode_string_body(body, bitmap, rows, nul, encoding, offset):
    """Decode a string body given the encoding named in the manifest."""
    nonnull_rows = rows - nul
    values = [None] * rows
    if encoding == STR_PREFIX:
        pos = 0
        n = len(body)
        for i in range(rows):
            if bitmap[i >> 3] & (1 << (i & 7)):
                continue
            if pos + 4 > n:
                _bad(offset, "truncated string length prefix")
            ln = struct.unpack_from(">I", body, pos)[0]
            pos += 4
            if pos + ln > n:
                _bad(offset, "string value runs past the page body")
            try:
                values[i] = body[pos:pos + ln].decode("utf-8", "strict")
            except UnicodeDecodeError:
                _bad(offset, "string value is not valid UTF-8")
            pos += ln
        if pos != n:
            _bad(offset, "trailing bytes after string values")
        return values
    # Dictionary encodings share a header and the dictionary entries.
    pos = 0
    n = len(body)
    if pos + 5 > n:
        _bad(offset, "truncated dictionary header")
    width = body[pos]
    dict_count = struct.unpack_from(">I", body, pos + 1)[0]
    pos += 5
    if width not in (1, 2, 4):
        _bad(offset, "invalid dictionary index width %d" % width)
    if dict_count > nonnull_rows:
        # A dictionary cannot have more entries than non-null values; this
        # catches an index that claims an absurd table before allocating.
        _bad(offset, "dictionary has %d entries for %d values"
             % (dict_count, nonnull_rows))
    entries = []
    for _ in range(dict_count):
        if pos + 4 > n:
            _bad(offset, "truncated dictionary entry length")
        ln = struct.unpack_from(">I", body, pos)[0]
        pos += 4
        if pos + ln > n:
            _bad(offset, "dictionary entry runs past the page body")
        try:
            entries.append(body[pos:pos + ln].decode("utf-8", "strict"))
        except UnicodeDecodeError:
            _bad(offset, "dictionary entry is not valid UTF-8")
        pos += ln
    idx_fmt = {1: ">B", 2: ">H", 4: ">I"}[width]
    idx_size = struct.Struct(idx_fmt).size
    unpack_idx = struct.Struct(idx_fmt).unpack_from
    if encoding == STR_DICT:
        needed = nonnull_rows * idx_size
        if n - pos != needed:
            _bad(offset, "dictionary index bytes %d do not match %d values"
                 % (n - pos, nonnull_rows))
        for i in range(rows):
            if bitmap[i >> 3] & (1 << (i & 7)):
                continue
            idx = unpack_idx(body, pos)[0]
            pos += idx_size
            if idx >= dict_count:
                _bad(offset, "dictionary index %d out of range (%d entries)"
                     % (idx, dict_count))
            values[i] = entries[idx]
        return values
    if encoding == STR_DICT_RLE:
        if pos + 4 > n:
            _bad(offset, "truncated run count")
        run_count = struct.unpack_from(">I", body, pos)[0]
        pos += 4
        if n - pos != run_count * (idx_size + 4):
            _bad(offset, "run table length does not match the page body")
        produced = 0
        row_iter = _iter_nonnull(bitmap, rows)
        for _ in range(run_count):
            idx = unpack_idx(body, pos)[0]
            pos += idx_size
            length = struct.unpack_from(">I", body, pos)[0]
            pos += 4
            if idx >= dict_count:
                _bad(offset, "dictionary index %d out of range (%d entries)"
                     % (idx, dict_count))
            if length == 0:
                _bad(offset, "zero-length dictionary run")
            if produced + length > nonnull_rows:
                _bad(offset, "dictionary runs exceed the value count")
            try:
                for _ in range(length):
                    values[next(row_iter)] = entries[idx]
            except StopIteration:
                _bad(offset, "dictionary runs exceed the value count")
            produced += length
        if produced != nonnull_rows:
            _bad(offset, "dictionary runs cover %d of %d values"
                 % (produced, nonnull_rows))
        return values
    _bad(offset, "unknown string encoding %r" % (encoding,))

