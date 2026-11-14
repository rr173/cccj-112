"""Strict scanning of one UTF-8 JSONL source record into a schema tuple.

The parser is hand written rather than delegating to ``json.loads`` because
the contract needs three things the standard decoder does not expose:

* **positional schema matching.**  Every field of the object must match a
  declared column, every declared column must be present, and no field may
  occur twice.  This is checked while scanning, with the starting offset of
  the offending key for diagnostics.
* **verbatim strings.**  Escapes are decoded exactly as JSON defines them
  (``\\uXXXX`` / surrogate pairs); nothing else is altered.  The result is
  the original sequence of Unicode code points, empty strings and NUL bytes
  included.
* **type precision.**  Booleans are never accepted as integers, integers are
  range checked against signed 64-bit *numerically* (not by string length),
  and a JSON number is rejected wherever a string or boolean is declared.

The scanner consumes a whole physical line (bytes, without the trailing
newline) and returns ``(values, size)`` where ``values`` is a tuple in schema
order and ``size`` is the number of source bytes the line contributes.  The
largest string token and the largest line are bounded by the caller before
this module is reached (see ``limits.py``); this module additionally refuses
any token whose decoded form would exceed the configured string bound.
"""

from .errors import InputError
from .schema import BOOLEAN, INTEGER, INT64_MAX, INT64_MIN, STRING

_WS = b" \t\r\n"
_DIGITS = b"0123456789"
_HEX = b"0123456789abcdefABCDEF"

# Small cache for repeated keys: many rows share the same field names.
_KEY_CACHE = {}
_KEY_CACHE_LIMIT = 4096

_SIMPLE_ESCAPES = {
    ord('"'): '"', ord("\\"): "\\", ord("/"): "/",
    ord("b"): "\b", ord("f"): "\f", ord("n"): "\n",
    ord("r"): "\r", ord("t"): "\t",
}


class _Scanner(object):
    __slots__ = ("buf", "pos", "end", "line_no", "max_string_bytes",
                 "str_cache")

    def __init__(self, buf, line_no, max_string_bytes):
        self.buf = buf
        self.pos = 0
        self.end = len(buf)
        self.line_no = line_no
        self.max_string_bytes = max_string_bytes

    # -- diagnostics -----------------------------------------------------
    def fail(self, reason, detail="", pos=None):
        if pos is None:
            pos = self.pos
        raise InputError(self.line_no, reason,
                         "%s (byte offset %d)" % (detail, pos) if detail
                         else "byte offset %d" % pos)

    # -- lexical helpers -------------------------------------------------
    def skip_ws(self):
        buf, n = self.buf, self.end
        i = self.pos
        while i < n and buf[i] in _WS:
            i += 1
        self.pos = i

    def peek(self):
        if self.pos >= self.end:
            return -1
        return self.buf[self.pos]

    def expect(self, ch, reason):
        if self.pos >= self.end or self.buf[self.pos] != ch:
            self.fail(reason)
        self.pos += 1

    # -- tokens ----------------------------------------------------------
    def parse_key(self):
        """Parse a string token used as an object key, with caching."""
        buf = self.buf
        if self.pos >= self.end or buf[self.pos] != 0x22:
            self.fail("expected a string object key")
        start = self.pos
        i = start + 1
        j = i
        while True:
            if j >= self.end:
                self.fail("unterminated object key", pos=start)
            b = buf[j]
            if b == 0x22:
                break
            if b == 0x5C:
                key = self.parse_string()
                if len(_KEY_CACHE) < _KEY_CACHE_LIMIT:
                    _KEY_CACHE.setdefault(buf[i:self.pos - 1], key)
                return key
            if b < 0x20:
                self.fail("control character in object key", pos=j)
            j += 1
        raw = buf[i:j]
        self.pos = j + 1
        key = _KEY_CACHE.get(raw)
        if key is None:
            try:
                key = raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                self.fail("invalid UTF-8 in object key", str(exc), pos=i)
            if len(_KEY_CACHE) < _KEY_CACHE_LIMIT:
                _KEY_CACHE[raw] = key
        return key

    def parse_string(self):
        """Parse a JSON string starting at a double quote; return str."""
        buf = self.buf
        start = self.pos
        if start >= self.end or buf[start] != 0x22:
            self.fail("expected a string")
        i = start + 1
        j = i
        n = self.end
        while j < n:
            b = buf[j]
            if b == 0x22:
                raw = buf[i:j]
                try:
                    s = raw.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    self.fail("invalid UTF-8 in string", str(exc), pos=j)
                self.pos = j + 1
                self._check_string_size(s, start)
                return s
            if b == 0x5C:
                return self._parse_escaped_string(start, i, j)
            if b < 0x20:
                self.fail("unescaped control character in string", pos=j)
            j += 1
        self.fail("unterminated string", pos=start)

    def _parse_escaped_string(self, start, seg_start, slash):
        buf, n = self.buf, self.end
        parts = []
        i = seg_start
        j = slash
        while True:
            if j >= n:
                self.fail("unterminated escape sequence", pos=start)
            b = buf[j]
            if b == 0x22:
                raw = buf[i:j]
                try:
                    parts.append(raw.decode("utf-8", "strict"))
                except UnicodeDecodeError as exc:
                    self.fail("invalid UTF-8 in string", str(exc), pos=j)
                self.pos = j + 1
                s = "".join(parts)
                self._check_string_size(s, start)
                return s
            if b == 0x5C:
                raw = buf[i:j]
                try:
                    parts.append(raw.decode("utf-8", "strict"))
                except UnicodeDecodeError as exc:
                    self.fail("invalid UTF-8 in string", str(exc), pos=j)
                if j + 1 >= n:
                    self.fail("unterminated escape sequence", pos=j)
                e = buf[j + 1]
                simple = _SIMPLE_ESCAPES.get(e)
                if simple is not None:
                    parts.append(simple)
                    j += 2
                elif e == 0x75:  # u
                    cp, j = self._read_hex4(j + 2)
                    if 0xD800 <= cp <= 0xDBFF and buf[j:j + 2] == b"\\u":
                        low, after = self._read_hex4(j + 2, peek=True)
                        if 0xDC00 <= low <= 0xDFFF:
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                            j = after
                    if 0xD800 <= cp <= 0xDFFF:
                        self.fail("unpaired UTF-16 surrogate in string "
                                  "escape", pos=j)
                    parts.append(chr(cp))
                else:
                    self.fail("invalid escape sequence \\%c"
                              % chr(e) if e < 128 else "invalid escape",
                              pos=j)
                i = j
                continue
            if b < 0x20:
                self.fail("unescaped control character in string", pos=j)
            j += 1

    def _read_hex4(self, pos, peek=False):
        buf = self.buf
        if pos + 4 > self.end:
            self.fail("truncated \\u escape", pos=pos)
        cp = 0
        for k in range(4):
            c = buf[pos + k]
            if c not in _HEX:
                self.fail("bad hex digit in \\u escape", pos=pos + k)
            cp = cp * 16 + int(chr(c), 16)
        return cp, (pos + 4)

    def _check_string_size(self, s, pos):
        # Reject before the value is retained anywhere.  The decoded UTF-8
        # byte length is what a dictionary entry or offset page must hold.
        try:
            n = len(s.encode("utf-8"))
        except UnicodeEncodeError:
            self.fail("string contains an unpaired surrogate", pos=pos)
        if n > self.max_string_bytes:
            self.fail("string exceeds the configured size bound",
                      "%d bytes > %d" % (n, self.max_string_bytes), pos=pos)
        return n

    def parse_value(self, col):
        t = col.type
        b = self.peek()
        if t == STRING:
            if b != 0x22:
                self.fail("column %r expects a string, got %s"
                          % (col.name, _describe(b)))
            return self.parse_string()
        if t == INTEGER:
            if b == 0x2D or b in _DIGITS:
                return self.parse_integer(col)
            self.fail("column %r expects an integer, got %s"
                      % (col.name, _describe(b)))
        if t == BOOLEAN:
            if b == 0x74:  # t
                self._literal(b"true")
                return True
            if b == 0x66:  # f
                self._literal(b"false")
                return False
            self.fail("column %r expects a boolean, got %s"
                      % (col.name, _describe(b)))
        raise AssertionError("unreachable column type")

    def _literal(self, word):
        if self.buf[self.pos:self.pos + len(word)] != word:
            self.fail("malformed literal, expected %r"
                      % word.decode("ascii"))
        self.pos += len(word)

    def parse_integer(self, col):
        buf, n = self.buf, self.end
        start = self.pos
        i = start
        negative = False
        if i < n and buf[i] == 0x2D:
            negative = True
            i += 1
        if i >= n or buf[i] not in _DIGITS:
            self.fail("column %r expects an integer, got a malformed number"
                      % col.name, pos=start)
        if buf[i] == 0x30:
            i += 1
            if i < n and buf[i] in _DIGITS:
                self.fail("integer literal has a leading zero", pos=start)
        else:
            while i < n and buf[i] in _DIGITS:
                i += 1
        if i < n and buf[i] in b".eE":
            self.fail("column %r expects a signed 64-bit integer, got a "
                      "non-integer JSON number" % col.name, pos=start)
        literal = buf[start:i]
        # Range check numerically; a 20-digit literal may still fit, and a
        # lexicographic check would get negatives and equal widths wrong.
        if len(literal) > 20:
            self.fail("integer is outside the signed 64-bit range",
                      literal.decode("ascii", "replace")[:40], pos=start)
        value = int(literal)
        if value < INT64_MIN or value > INT64_MAX:
            self.fail("integer is outside the signed 64-bit range",
                      literal.decode("ascii", "replace"), pos=start)
        self.pos = i
        return value


def _describe(b):
    if b < 0:
        return "end of line"
    if b == 0x22:
        return "a string"
    if b == 0x7B:
        return "an object"
    if b == 0x5B:
        return "an array"
    if b == 0x74 or b == 0x66:
        return "a boolean"
    if b == 0x6E:
        return "null"
    if b == 0x2D or b in _DIGITS:
        return "a number"
    if b > 0x20 and b < 0x7F:
        return "character %r" % chr(b)
    return "byte 0x%02x" % b


def scan_record(line, schema, line_no, max_string_bytes):
    """Scan one record line into a schema-ordered tuple.

    ``line`` is bytes without the trailing newline.  Raises
    :class:`InputError` with the line's physical number on any violation.
    """
    # The line is checked for well-formed UTF-8 as a whole before any
    # structural scanning.  A reader may split valid multi-byte sequences
    # anywhere (including inside string values), so the decoder's own
    # per-string strict decoding below is the final authority on values.
    try:
        line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(line_no, "invalid UTF-8",
                         "%s at byte offset %d" % (exc.reason, exc.start))
    colidx = record_index(schema)
    sc = _Scanner(line, line_no, max_string_bytes)
    sc.skip_ws()
    if sc.peek() != 0x7B:
        sc.fail("record must be a JSON object",
                "found %s" % _describe(sc.peek()))
    sc.pos += 1
    width = len(schema)
    values = [None] * width
    present = [False] * width
    seen = 0
    sc.skip_ws()
    if sc.peek() == 0x7D:
        sc.pos += 1
    else:
        while True:
            sc.skip_ws()
            key = sc.parse_key()
            idx = colidx.get(key)
            if idx is None:
                sc.fail("field %r is not declared in the schema" % key)
            if present[idx]:
                sc.fail("duplicate field %r" % key)
            sc.skip_ws()
            sc.expect(0x3A, "expected ':' after object key")
            sc.skip_ws()
            if sc.peek() == 0x6E:  # n
                sc._literal(b"null")
                if not schema[idx].nullable:
                    sc.fail("column %r is not nullable" % key)
                values[idx] = None
            else:
                values[idx] = sc.parse_value(schema[idx])
            present[idx] = True
            seen += 1
            sc.skip_ws()
            b = sc.peek()
            if b == 0x2C:
                sc.pos += 1
                continue
            if b == 0x7D:
                sc.pos += 1
                break
            sc.fail("expected ',' or '}' after a field value")
    sc.skip_ws()
    if sc.pos != sc.end:
        sc.fail("trailing data after the record object")
    if seen != width:
        missing = [schema[i].name for i in range(width) if not present[i]]
        sc.fail("record is missing declared field(s): %s"
                % ", ".join(repr(m) for m in missing))
    return tuple(values)


# The schema object is immutable and hashable; keep a small index cache keyed
# by object identity so the hot loop does not rebuild a dict every line.
_COLUMN_INDEX = {}


def prime_schema(schema):
    """Install the name -> position map used by :func:`scan_record`."""
    key = id(schema)
    cached = _COLUMN_INDEX.get(key)
    if cached is None or cached[0] is not schema:
        _COLUMN_INDEX[key] = (schema, {c.name: i for i, c in enumerate(schema)})
    return _COLUMN_INDEX[key][1]


def record_index(schema):
    """Return the name -> position map for *schema* (public helper)."""
    key = id(schema)
    entry = _COLUMN_INDEX.get(key)
    if entry is None or entry[0] is not schema:
        return prime_schema(schema)
    return entry[1]

