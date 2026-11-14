"""Strict HTTP/1.1 response stream parser.

The input is one fully reconstructed server-to-client TCP byte stream,
which may carry several HTTP/1.1 responses back to back.  The parser never
searches for a resynchronisation point after an error: once framing is in
doubt the whole connection is rejected.

Supported message bodies are exactly:

* ``Content-Length`` (decimal octet count);
* ``Transfer-Encoding: chunked`` (hex chunk sizes, optional chunk
  extensions, trailers terminated by an empty line);
* messages defined as bodyless: 1xx provisional responses, 204 and 304.

Conflicting Content-Length values, chunked combined with Content-Length,
unknown transfer codings and any other body without an explicit boundary
are rejected -- connection close is never used to guess a body.
"""

from .errors import HttpParseError

CRLF = b"\r\n"
_MAX_CHUNK_LINE = 64
_DIGITS = set(b"0123456789")
_HEX = set(b"0123456789abcdefABCDEF")


class ParsedResponse:
    def __init__(
        self,
        status_code,
        reason_phrase,
        raw_headers,
        header_fields,
        stream_start,
        header_end,
        body_start,
        body_end,
        http_index,
        framing,
    ):
        self.status_code = status_code
        self.reason_phrase = reason_phrase
        # Raw header block as received, each line WITHOUT its terminating
        # CRLF; duplicates are preserved.
        self.raw_headers = raw_headers
        # (name_lower, original_name, value) tuples, duplicates preserved.
        self.header_fields = header_fields
        self.stream_start = stream_start  # offset of "HTTP/1.1 ..."
        self.header_end = header_end  # offset just past the blank line
        self.body_start = body_start  # None for bodyless messages
        self.body_end = body_end  # offset just past the body (== header_end
        #                           for bodyless messages)
        self.http_index = http_index  # 1-based position in the stream
        # 'length' | 'chunked' | None (bodyless)
        self.framing = framing
        self.object_index = None  # assigned later, only body-producing ones


def parse_responses(stream, max_header_bytes):
    """Split a reconstructed stream into :class:`ParsedResponse` objects."""
    responses = []
    pos = 0
    total = len(stream)
    while pos < total:
        response, pos = _parse_one(stream, pos, max_header_bytes,
                                   len(responses) + 1)
        responses.append(response)
    if not responses:
        raise HttpParseError(
            "empty_stream", "the TCP stream contains no HTTP response"
        )
    return responses


def _parse_one(stream, pos, max_header_bytes, http_index):
    start = pos
    line, pos = _read_line(stream, pos, max_header_bytes, "status line",
                           base=start)
    parts = line.split(b" ", 2)
    if len(parts) < 2 or parts[0] != b"HTTP/1.1":
        raise HttpParseError(
            "bad_status_line",
            f"expected HTTP/1.1 status line at offset {start}, got "
            f"{line[:40]!r}",
        )
    code_bytes = parts[1]
    if len(code_bytes) != 3 or any(c not in _DIGITS for c in code_bytes):
        raise HttpParseError(
            "bad_status_code", f"status code not a 3-digit number: {line!r}"
        )
    status_code = int(code_bytes)
    reason_phrase = parts[2].decode("latin-1") if len(parts) == 3 else ""

    raw_headers = []
    header_fields = []
    while True:
        line_start = pos
        line, pos = _read_line(stream, pos, max_header_bytes, "header",
                               base=start)
        if line == b"":
            break  # blank line ends the header section
        colon = line.find(b":")
        if colon <= 0:
            raise HttpParseError(
                "bad_header",
                f"header line without field name at offset {line_start}: "
                f"{line[:40]!r}",
            )
        name = line[:colon]
        value = line[colon + 1:]
        if value.startswith(b" "):
            value = value[1:]
        if value.endswith(b" "):
            value = value[:-1]
        if not _is_token(name):
            raise HttpParseError(
                "bad_header_name",
                f"illegal characters in header name {name!r} at offset "
                f"{line_start}",
            )
        raw_headers.append(line)
        header_fields.append(
            (name.decode("latin-1").lower(), name.decode("latin-1"),
             value.decode("latin-1"))
        )
    header_end = pos

    content_lengths = [v for n, _orig, v in header_fields
                       if n == "content-length"]
    transfer_encodings = [v for n, _orig, v in header_fields
                          if n == "transfer-encoding"]

    bodyless = 100 <= status_code <= 199 or status_code in (204, 304)
    if bodyless:
        # These messages carry no body regardless of framing headers; the
        # headers themselves must still be internally coherent.
        _check_framing_coherence(content_lengths, transfer_encodings)
        return (
            ParsedResponse(
                status_code, reason_phrase, raw_headers, header_fields,
                start, header_end, None, header_end, http_index, None,
            ),
            header_end,
        )

    framing = _check_framing_coherence(content_lengths, transfer_encodings)
    body_start = header_end
    if framing == "length":
        length = int(content_lengths[0].strip())
        body_end = body_start + length
        if body_end > len(stream):
            raise HttpParseError(
                "short_body",
                f"Content-Length {length} needs {length} bytes at offset "
                f"{body_start} but only {len(stream) - body_start} remain",
            )
    elif framing == "chunked":
        body_end = _consume_chunks(stream, body_start)
    else:
        raise HttpParseError(
            "unsupported_framing",
            f"response {status_code} has neither Content-Length nor "
            "Transfer-Encoding: chunked; connection close cannot define a "
            "body boundary",
        )
    return (
        ParsedResponse(
            status_code, reason_phrase, raw_headers, header_fields,
            start, header_end, body_start, body_end, http_index, framing,
        ),
        body_end,
    )


def _check_framing_coherence(content_lengths, transfer_encodings):
    """Validate framing headers and return ``'length' | 'chunked' | None``."""
    if transfer_encodings:
        if len(transfer_encodings) > 1:
            raise HttpParseError(
                "conflicting_transfer_encoding",
                f"multiple Transfer-Encoding headers: {transfer_encodings!r}",
            )
        coding = transfer_encodings[0].strip().lower()
        if coding != "chunked":
            raise HttpParseError(
                "unsupported_transfer_encoding",
                f"transfer coding {transfer_encodings[0]!r} is not "
                "supported (only chunked)",
            )
        if content_lengths:
            raise HttpParseError(
                "conflicting_framing",
                "message declares both Transfer-Encoding: chunked and "
                "Content-Length",
            )
        return "chunked"

    if not content_lengths:
        return None
    normalized = set()
    for value in content_lengths:
        text = value.strip()
        if not text or any(c not in _DIGITS for c in text.encode("ascii")):
            raise HttpParseError(
                "bad_content_length", f"non-integer Content-Length: {value!r}"
            )
        normalized.add(int(text))
    if len(normalized) > 1:
        raise HttpParseError(
            "conflicting_content_length",
            f"different Content-Length values in one message: "
            f"{content_lengths!r}",
        )
    return "length"


def extract_chunked_payload(stream, body_start, body_end):
    """Return the de-chunked payload of an already validated chunked body.

    The body occupies ``stream[body_start:body_end]`` and was validated by
    :func:`_consume_chunks`; this function strips chunk framing, extensions
    and the trailer section without re-validating syntax.
    """
    pieces = []
    pos = body_start
    while pos < body_end:
        crlf = stream.find(CRLF, pos)
        line = stream[pos:crlf]
        pos = crlf + 2
        size = int(line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            break  # trailer section runs to body_end
        pieces.append(stream[pos:pos + size])
        pos += size + 2  # data plus its CRLF
    return b"".join(pieces)


def _consume_chunks(stream, pos):
    """Validate a chunked body; return the offset just past the trailer."""
    while True:
        line_start = pos
        line, pos = _read_line(
            stream, pos, _MAX_CHUNK_LINE, "chunk-size line", fixed_limit=True,
            base=line_start,
        )
        size_text = line.split(b";", 1)[0].strip()
        if not size_text or any(c not in _HEX for c in size_text):
            raise HttpParseError(
                "bad_chunk_size",
                f"invalid hex chunk size at offset {line_start}: {line!r}",
            )
        size = int(size_text, 16)
        if size == 0:
            # The zero-size chunk has no data and no data CRLF; the next
            # bytes are the trailer section terminated by an empty line.
            break
        if pos + size + 2 > len(stream):
            raise HttpParseError(
                "short_body",
                f"chunk of {size} bytes at offset {pos} is not fully "
                "present in the stream",
            )
        if stream[pos + size: pos + size + 2] != CRLF:
            raise HttpParseError(
                "bad_chunk_terminator",
                f"chunk data at offset {pos} is not followed by CRLF",
            )
        pos = pos + size + 2
    # Trailer section: arbitrary header lines until the empty line.
    while True:
        line_start = pos
        line, pos = _read_line(stream, pos, _MAX_CHUNK_LINE,
                               "chunk trailer", fixed_limit=True,
                               base=line_start)
        if line == b"":
            return pos
        if line.find(b":") <= 0:
            raise HttpParseError(
                "bad_trailer",
                f"malformed trailer line at offset {line_start}: "
                f"{line[:40]!r}",
            )


def _read_line(stream, pos, limit, what, fixed_limit=False, base=0):
    """Read one CRLF-terminated line, returning ``(line_without_crlf, next)``.

    The header budget is measured from the start of the current message for
    header lines (``fixed_limit=False``) and as an absolute line length for
    chunk control lines.
    """
    crlf = stream.find(CRLF, pos)
    if crlf == -1:
        raise HttpParseError(
            "missing_crlf", f"{what} starting at offset {pos} has no CRLF"
        )
    line = stream[pos:crlf]
    if b"\n" in line:
        # A bare line feed cannot delimit HTTP lines: the CRLF that ended
        # the search actually belongs to a later line.
        raise HttpParseError(
            "missing_crlf",
            f"{what} starting at offset {pos} is not terminated by CRLF "
            "(bare LF found)",
        )
    next_pos = crlf + 2
    if fixed_limit:
        if len(line) > limit:
            raise HttpParseError(
                "header_too_large",
                f"{what} of {len(line)} bytes exceeds limit {limit}",
            )
    else:
        if next_pos - base > limit:
            raise HttpParseError(
                "header_too_large",
                f"header section exceeds the {limit}-byte limit while "
                f"reading {what} at offset {pos}",
            )
    return line, next_pos


def _is_token(name):
    # Field names are tokens per RFC 7230; reject separators/controls.
    separators = set(b'()<>@,;:\\"/[]?={} \t')
    return len(name) > 0 and all(
        c > 0x20 and c < 0x7F and c not in separators for c in name
    )
