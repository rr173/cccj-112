"""Content-Encoding handling for recovered response bodies.

* no ``Content-Encoding``: the body is stored byte-for-byte, no text
  decoding is assumed;
* ``gzip``: a single RFC 1952 member is inflated with a running size cap,
  and the gzip trailer (CRC-32 and ISIZE) is checked explicitly so a
  truncated or tampered member cannot masquerade as a successful body;
* anything else: rejected with an explicit unsupported reason.
"""

import struct
import zlib

from .errors import HttpParseError, LimitExceeded

_GZIP_MAGIC = b"\x1f\x8b"
_GZIP_COMPRESSION_DEFLATE = 8
_FTEXT = 1
_FHCRC = 2
_FEXTRA = 4
_FNAME = 8
_FCOMMENT = 16
_CHUNK = 64 * 1024


def decode_body(body, content_encoding, max_decoded_bytes):
    """Return the decoded body bytes for a stored (framing-free) body."""
    if content_encoding is None:
        if len(body) > max_decoded_bytes:
            raise LimitExceeded(
                "body_limit",
                f"body of {len(body)} bytes exceeds the configured "
                f"{max_decoded_bytes}-byte decoded-body limit",
            )
        return body, None

    coding = content_encoding.strip().lower()
    if coding != "gzip":
        raise HttpParseError(
            "unsupported_content_encoding",
            f"Content-Encoding {content_encoding!r} is not supported "
            "(only gzip or identity/no encoding)",
        )
    return _decode_gzip(body, max_decoded_bytes), "gzip"


def _decode_gzip(body, max_decoded_bytes):
    if len(body) < 18 or body[:2] != _GZIP_MAGIC:
        raise HttpParseError("gzip_corrupt", "missing gzip magic header")
    if body[2] != _GZIP_COMPRESSION_DEFLATE:
        raise HttpParseError(
            "gzip_corrupt",
            f"gzip CM byte is {body[2]}, expected 8 (deflate)",
        )
    flags = body[3]
    pos = 10
    try:
        if flags & _FEXTRA:
            xlen = struct.unpack_from("<H", body, pos)[0]
            pos += 2 + xlen
        if flags & _FNAME:
            end = body.index(b"\x00", pos)
            pos = end + 1
        if flags & _FCOMMENT:
            end = body.index(b"\x00", pos)
            pos = end + 1
        if flags & _FHCRC:
            pos += 2
    except (ValueError, struct.error, IndexError):
        raise HttpParseError("gzip_corrupt", "gzip header fields are cut off")

    if pos + 8 > len(body):
        raise HttpParseError(
            "gzip_corrupt", "no room for deflate data and the 8-byte trailer"
        )
    trailer_pos = len(body) - 8
    expected_crc, expected_size = struct.unpack_from("<II", body, trailer_pos)

    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    output = bytearray()
    crc = 0
    total = 0
    view = memoryview(body)
    while pos < trailer_pos:
        chunk = view[pos:min(pos + _CHUNK, trailer_pos)]
        pos += len(chunk)
        try:
            part = decompressor.decompress(bytes(chunk))
        except zlib.error as exc:
            raise HttpParseError("gzip_corrupt", f"deflate error: {exc}")
        if part:
            crc = zlib.crc32(part, crc)
            total += len(part)
            if total > max_decoded_bytes:
                raise LimitExceeded(
                    "body_limit",
                    f"decompressed body exceeds the configured "
                    f"{max_decoded_bytes}-byte limit",
                )
            output.extend(part)
    try:
        tail = decompressor.flush()
    except zlib.error as exc:
        raise HttpParseError("gzip_corrupt", f"deflate error: {exc}")
    if tail:
        crc = zlib.crc32(tail, crc)
        total += len(tail)
        if total > max_decoded_bytes:
            raise LimitExceeded(
                "body_limit",
                f"decompressed body exceeds the configured "
                f"{max_decoded_bytes}-byte limit",
            )
        output.extend(tail)

    if decompressor.unused_data:
        raise HttpParseError(
            "gzip_corrupt",
            "trailing bytes inside the gzip member; concatenated members "
            "are not accepted",
        )
    if crc != expected_crc:
        raise HttpParseError(
            "gzip_corrupt",
            f"CRC-32 mismatch: computed {crc:08x}, trailer says "
            f"{expected_crc:08x}",
        )
    if (total & 0xFFFFFFFF) != expected_size:
        raise HttpParseError(
            "gzip_corrupt",
            f"ISIZE mismatch: inflated {total} bytes, trailer says "
            f"{expected_size}",
        )
    return bytes(output)
