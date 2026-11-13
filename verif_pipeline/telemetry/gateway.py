"""Simulated field gateway for the gauge telemetry protocol.

The gateway keeps bucket-tip pulses locally until it can reach the
server, then uploads a batch as ``begin`` + one ``tip`` per sequence
number + ``seal``.  It never deletes local data until the server has
acknowledged the ``seal``.

``GatewayClient`` is used by the demo and tests.  ``send_batch`` can also
split frames arbitrarily (``chunk``) and resend individual messages; the
server must be idempotent under all of it.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.frame import encode_frame  # noqa: E402
from telemetry.protocol import tip_digest  # noqa: E402


class GatewayClient:
    """One TCP connection speaking the framed protocol."""

    def __init__(self, host="127.0.0.1", port=8765, timeout=10.0,
                 max_buffer=1 << 20):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.max_buffer = max_buffer
        self.pending = bytearray()
        self.sent = []   # [(raw_bytes, message_dict)]
        self.received = []  # [(reply_bytes, reply_dict)]

    # -- low level -------------------------------------------------------
    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def send_message(self, message: dict) -> dict:
        raw = encode_frame(message)
        self.sent.append((raw, message))
        self.send_raw(raw)
        return self.read_reply()

    def read_reply(self, timeout=10.0) -> dict:
        """Read exactly one line-delimited JSON reply."""
        deadline = time.time() + timeout
        while True:
            idx = self.pending.find(b"\n")
            if idx >= 0:
                line = bytes(self.pending[:idx])
                del self.pending[:idx + 1]
                reply = json.loads(line)
                self.received.append((line, reply))
                return reply
            if time.time() > deadline:
                raise TimeoutError("no reply line from telemetry server")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed the connection")
            # Replies are line-delimited JSON, not framed: just buffer.
            if len(self.pending) > self.max_buffer:
                raise BufferError("reply buffer on the gateway side overflowed")
            self.pending.extend(chunk)

    def wait_reply(self, timeout=2.0):
        """Return a reply if one arrives, else None (for negative tests)."""
        try:
            return self.read_reply(timeout=timeout)
        except TimeoutError:
            return None

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Batch building helpers (deterministic, used by demo + tests)
# ---------------------------------------------------------------------------
def build_batch(station_id, batch_id, start_ms, end_ms, tips, tip_mm="0.2",
                count=None):
    """Return ``(begin_message, [tip_message, ...])``.

    ``tips`` is an iterable of ``(seq, time_ms, tips_count)`` triples.  The
    declared count defaults to the number of tips; pass ``count`` larger to
    simulate a lost sequence number.
    """
    tips = sorted(tips)
    declared = len(tips) if count is None else count
    begin = {
        "type": "begin",
        "station_id": station_id,
        "batch_id": batch_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "count": declared,
        "tip_mm": tip_mm,
        "sha256": tip_digest(tips),
    }
    msgs = [dict(type="tip", station_id=station_id, batch_id=batch_id,
                 seq=seq, time_ms=time_ms, tips=tips_count)
            for seq, time_ms, tips_count in tips]
    return begin, msgs


def seal_message(station_id, batch_id):
    return {"type": "seal", "station_id": station_id, "batch_id": batch_id}


def send_batch(client: GatewayClient, begin: dict, tips: list,
               chunk: int | None = None, seal: bool = True):
    """Send begin + tips + seal; returns the list of replies (send order)."""
    replies = []
    raw = bytearray()

    def flush():
        nonlocal raw
        if not raw:
            return
        if chunk is None:
            client.send_raw(bytes(raw))
        else:
            for i in range(0, len(raw), chunk):
                client.send_raw(bytes(raw[i:i + chunk]))
        raw = bytearray()

    for message in [begin] + list(tips):
        encoded = encode_frame(message)
        client.sent.append((encoded, message))
        raw.extend(encoded)
        flush()
        replies.append(client.read_reply())
    if seal:
        replies.append(client.send_message(seal_message(
            begin["station_id"], begin["batch_id"])))
    return replies
