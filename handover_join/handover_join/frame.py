"""Length-prefixed binary frames used by every external-sort run file.

Frame layout (all integers big endian)::

    magic     1 byte   MAGIC_RECORD (0x01) for side runs,
                       MAGIC_PAIR   (0x02) for the pair spool
    key_len   4 bytes  length of the encoded composite key
    body_len  4 bytes  length of the frame body
    key       key_len bytes
    body      body_len bytes

Side-run body (MAGIC_RECORD)::

    row          8 bytes   1-based input row number
    payload_len  4 bytes   length of the original JSON line
    payload      payload_len bytes (verbatim UTF-8 JSON object)

Pair body (MAGIC_PAIR)::

    left_row        8 bytes
    right_row       8 bytes   (0 means unmatched, i.e. null right side)
    left_len        4 bytes
    left_payload    left_len bytes
    right_len       4 bytes   (0 means unmatched)
    right_payload   right_len bytes

Frames are self-delimiting, so a reader can stream them or skip over them
without parsing JSON.  Lengths are unsigned 32-bit: a single frame must fit
in 4 GiB; in practice it must fit in the configured memory budget (see
README, "Memory accounting"), which is always far smaller.
"""

import struct

MAGIC_RECORD = 0x01
MAGIC_PAIR = 0x02

_HEADER = struct.Struct(">BII")
_U64 = struct.Struct(">Q")
_U32 = struct.Struct(">I")


class FrameWriter:
    """Append-only writer of framed records into a binary file."""

    def __init__(self, fileobj):
        self._f = fileobj

    def write(self, magic, key, body):
        self._f.write(_HEADER.pack(magic, len(key), len(body)))
        if key:
            self._f.write(key)
        self._f.write(body)

    def flush(self):
        self._f.flush()


class FrameReader:
    """Streaming frame reader supporting seek/rewind for group re-reads."""

    def __init__(self, fileobj):
        self._f = fileobj

    def read_frame(self):
        """Return ``(magic, key, body, position)`` or ``None`` at EOF.

        ``position`` is the byte offset of the frame just read, suitable for
        later ``seek`` to re-read a whole key group.
        """
        position = self._f.tell()
        header = self._f.read(_HEADER.size)
        if not header:
            return None
        if len(header) < _HEADER.size:
            raise ValueError("truncated frame header")
        magic, key_len, body_len = _HEADER.unpack(header)
        key = self._f.read(key_len) if key_len else b""
        if len(key) < key_len:
            raise ValueError("truncated frame key")
        body = self._f.read(body_len)
        if len(body) < body_len:
            raise ValueError("truncated frame body")
        return magic, key, body, position

    def skip_frame(self):
        """Skip one frame; return its start position, or None at EOF."""
        position = self._f.tell()
        header = self._f.read(_HEADER.size)
        if not header:
            return None
        if len(header) < _HEADER.size:
            raise ValueError("truncated frame header")
        _, key_len, body_len = _HEADER.unpack(header)
        self._f.seek(key_len + body_len, 1)
        return position

    def tell(self):
        return self._f.tell()

    def seek(self, position):
        self._f.seek(position)


# ---------------------------------------------------------------------------
# Side-run body helpers
# ---------------------------------------------------------------------------

def encode_record_body(row, payload):
    return _U64.pack(row) + _U32.pack(len(payload)) + payload


def decode_record_body(body):
    row = _U64.unpack_from(body, 0)[0]
    (payload_len,) = _U32.unpack_from(body, 8)
    start = 12
    payload = body[start:start + payload_len]
    if len(payload) != payload_len:
        raise ValueError("truncated record body")
    return row, payload


# ---------------------------------------------------------------------------
# Pair body helpers
# ---------------------------------------------------------------------------

def encode_pair_body(left_row, right_row, left_payload, right_payload):
    right = right_payload or b""
    return (
        _U64.pack(left_row)
        + _U64.pack(right_row or 0)
        + _U32.pack(len(left_payload))
        + left_payload
        + _U32.pack(len(right))
        + right
    )


def decode_pair_body(body):
    left_row = _U64.unpack_from(body, 0)[0]
    right_row = _U64.unpack_from(body, 8)[0]
    offset = 16
    (left_len,) = _U32.unpack_from(body, offset)
    offset += 4
    left_payload = body[offset:offset + left_len]
    if len(left_payload) != left_len:
        raise ValueError("truncated pair body (left payload)")
    offset += left_len
    (right_len,) = _U32.unpack_from(body, offset)
    offset += 4
    right_payload = body[offset:offset + right_len]
    if len(right_payload) != right_len:
        raise ValueError("truncated pair body (right payload)")
    return left_row, right_row or None, left_payload, right_payload or None
