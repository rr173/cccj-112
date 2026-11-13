"""Wire framing for gauge telemetry.

One frame (all integers big-endian)::

    A5 5A | uint16 body_length | UTF-8 JSON body | uint32 CRC32

``body_length`` is the byte length of the JSON body and must be in
``[1, 4096]``.  The CRC is computed over the two length bytes concatenated
with the body (i.e. everything between the magic and the CRC itself).

TCP is a byte stream: a frame may arrive split across many reads, or many
frames may arrive in one read.  :class:`FrameDecoder` buffers bytes and
emits only complete, CRC-checked frames.  A truncated tail is never
emitted as a message.  On a bad CRC (or bad UTF-8/JSON) the decoder drops
the magic and re-scans for the next ``A55A``; on an out-of-range length it
does the same.  The unparsed buffer is bounded.
"""
from __future__ import annotations

import json
import zlib

MAGIC = b"\xA5\x5A"
MIN_BODY = 1
MAX_BODY = 4096
MAX_FRAME = len(MAGIC) + 2 + MAX_BODY + 4  # 4104
DEFAULT_MAX_BUFFER = 1 << 20


def encode_frame(message: dict) -> bytes:
    """Encode one message as a framed byte string."""
    body = json.dumps(message, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if not MIN_BODY <= len(body) <= MAX_BODY:
        raise ValueError(f"body length {len(body)} outside [{MIN_BODY}, {MAX_BODY}]")
    length = len(body).to_bytes(2, "big")
    crc = zlib.crc32(length + body) & 0xFFFFFFFF
    return MAGIC + length + body + crc.to_bytes(4, "big")


class FrameDecoder:
    """Incremental frame decoder with a hard limit on unparsed bytes.

    ``feed(data)`` returns a list of events.  Each event is either::

        {"type": "frame", "message": {...}, "body_length": n,
         "frame_hex": "..."}

    or a rejection (the frame/malformed bytes are skipped and decoding
    continues)::

        {"type": "reject", "reason": ..., "detail": {...}}

    Rejections are diagnostic only: a bad frame has no trustworthy
    identity, so the server does not reply to it.
    """

    def __init__(self, max_buffer: int = DEFAULT_MAX_BUFFER):
        if max_buffer < MAX_FRAME:
            raise ValueError(f"max_buffer must be >= {MAX_FRAME}")
        self.max_buffer = max_buffer
        self.buf = bytearray()
        self.stats = {
            "frames": 0,
            "bad_crc": 0,
            "invalid_length": 0,
            "bad_json": 0,
            "garbage_bytes": 0,
            "buffer_overflow_bytes": 0,
            "bytes_in": 0,
        }

    @property
    def pending_bytes(self) -> int:
        return len(self.buf)

    def feed(self, data: bytes):
        self.stats["bytes_in"] += len(data)
        self.buf.extend(data)
        events = []
        self._decode(events)
        self._enforce_buffer_bound(events)
        return events

    # -- internals -------------------------------------------------------
    def _decode(self, events):
        while True:
            if len(self.buf) < len(MAGIC):
                return
            idx = self.buf.find(MAGIC)
            if idx < 0:
                # No magic at all.  Keep a trailing A5: it may be the first
                # half of a magic split across reads.
                keep = 1 if self.buf.endswith(MAGIC[:1]) else 0
                dropped = len(self.buf) - keep
                if dropped:
                    self.stats["garbage_bytes"] += dropped
                    events.append({"type": "reject", "reason": "garbage",
                                   "detail": {"dropped_bytes": dropped}})
                    del self.buf[:dropped]
                return
            if idx > 0:
                self.stats["garbage_bytes"] += idx
                events.append({"type": "reject", "reason": "garbage",
                               "detail": {"dropped_bytes": idx}})
                del self.buf[:idx]
                # Loop: magic is now at the front.
            if len(self.buf) < 4:
                return
            length = int.from_bytes(self.buf[2:4], "big")
            if not MIN_BODY <= length <= MAX_BODY:
                self.stats["invalid_length"] += 1
                events.append({"type": "reject", "reason": "invalid_length",
                               "detail": {"body_length": length}})
                # Cannot trust any boundary implied by this header.  Drop
                # the magic and resume scanning after it.
                del self.buf[:len(MAGIC)]
                continue
            total = len(MAGIC) + 2 + length + 4
            if len(self.buf) < total:
                # Incomplete frame: wait for more bytes.  Never emit it.
                return
            length_bytes = bytes(self.buf[2:4])
            body = bytes(self.buf[4:4 + length])
            crc_got = int.from_bytes(self.buf[4 + length:total], "big")
            crc_calc = zlib.crc32(length_bytes + body) & 0xFFFFFFFF
            if crc_got != crc_calc:
                self.stats["bad_crc"] += 1
                events.append({"type": "reject", "reason": "bad_crc",
                               "detail": {"body_length": length}})
                # Re-scan for the next magic rather than trusting the
                # (possibly corrupted) length to find the boundary.
                del self.buf[:len(MAGIC)]
                continue
            try:
                text = body.decode("utf-8")
                message = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.stats["bad_json"] += 1
                events.append({"type": "reject", "reason": "bad_json",
                               "detail": {"error": str(exc)}})
                del self.buf[:len(MAGIC)]
                continue
            if not isinstance(message, dict):
                self.stats["bad_json"] += 1
                events.append({"type": "reject", "reason": "bad_json",
                               "detail": {"error": "body is not a JSON object"}})
                del self.buf[:len(MAGIC)]
                continue
            self.stats["frames"] += 1
            events.append({
                "type": "frame",
                "message": message,
                "body_length": length,
                "frame_hex": bytes(self.buf[:total]).hex(),
            })
            del self.buf[:total]

    def _enforce_buffer_bound(self, events):
        """Bound memory if a peer sends an endless magic-less stream.

        Any complete frame has already been consumed above, so what can
        remain is garbage or one partial frame.  A partial frame is at most
        ``MAX_FRAME - 1`` bytes, so keeping that tail loses no decodable
        frame while capping memory.
        """
        if len(self.buf) <= self.max_buffer:
            return
        keep = MAX_FRAME - 1
        dropped = len(self.buf) - keep
        if dropped > 0:
            self.stats["buffer_overflow_bytes"] += dropped
            events.append({"type": "reject", "reason": "buffer_overflow",
                           "detail": {"dropped_bytes": dropped,
                                      "max_buffer": self.max_buffer}})
            del self.buf[:dropped]
