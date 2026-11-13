"""Message validation and canonicalisation for the gauge protocol.

Three message types share the identity ``(station_id, batch_id)``::

    begin  start_ms, end_ms, count, tip_mm, sha256
    tip    seq, time_ms, tips
    seal   (identity only)

``start_ms`` / ``end_ms`` / ``time_ms`` are UTC Unix milliseconds and the
batch interval is half-open ``[start_ms, end_ms)``.  Both endpoints must
fall on a whole second.  ``tip_mm`` is a positive decimal string (e.g.
``"0.2"``); it is kept as a string so that precipitation arithmetic can be
done in exact decimal, never binary floating point.

The batch digest is the SHA-256 of the ASCII concatenation, in ascending
``seq`` order, of ``"{seq},{time_ms},{tips}\\n"`` for every tip.  A batch
with ``count == 0`` has the SHA-256 of the empty string.

Validation here is *structural* only.  Cross-message checks (does the
batch exist, is ``seq < count``, is ``time_ms`` inside the declared
interval) live in :class:`telemetry.store.TelemetryStore` so that they can
be answered with a NACK instead of a dropped connection.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

MAX_TIPS = 100000
MAX_TIPS_PER_SEND = 65535
MAX_ID_LEN = 128
TIP_MM_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MSG_TYPES = ("begin", "tip", "seal")


class ProtocolError(ValueError):
    """A message that cannot be trusted or interpreted."""

    def __init__(self, reason: str, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}

    def as_dict(self):
        return {"reason": self.reason, **self.detail}


def _as_int(value, field, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("invalid_message",
                            {field: "must be an integer", "value": repr(value)})
    if isinstance(value, float):
        if not value.is_integer() or abs(value) > 2 ** 53:
            raise ProtocolError("invalid_message",
                                {field: "must be an integer", "value": repr(value)})
        value = int(value)
    if minimum is not None and value < minimum:
        raise ProtocolError("invalid_message",
                            {field: f"must be >= {minimum}", "value": value})
    if maximum is not None and value > maximum:
        raise ProtocolError("invalid_message",
                            {field: f"must be <= {maximum}", "value": value})
    return value


def _as_id(value, field):
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid_message",
                            {field: "must be a non-empty string"})
    if len(value) > MAX_ID_LEN:
        raise ProtocolError("invalid_message",
                            {field: f"longer than {MAX_ID_LEN} characters"})
    if any(ord(c) < 0x20 for c in value):
        raise ProtocolError("invalid_message",
                            {field: "contains control characters"})
    return value


def canonical_tip_mm(value):
    """Validate a positive decimal string and return its canonical form."""
    if not isinstance(value, str) or not TIP_MM_RE.match(value):
        raise ProtocolError("invalid_message",
                            {"tip_mm": "must be a positive decimal string"})
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise ProtocolError("invalid_message", {"tip_mm": "not a decimal"})
    if not d.is_finite() or d <= 0:
        raise ProtocolError("invalid_message",
                            {"tip_mm": "must be > 0", "value": value})
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def canonical_sha256(value):
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise ProtocolError("invalid_message",
                            {"sha256": "must be 64 hex characters"})
    return value.lower()


def normalize_message(message) -> dict:
    """Validate and canonicalise one decoded JSON object.

    Returns ``{"type", "station_id", "batch_id", plus type-specific
    canonical fields}``.  Raises :class:`ProtocolError` for anything that
    must not touch the database.
    """
    if not isinstance(message, dict):
        raise ProtocolError("invalid_message", {"error": "not a JSON object"})
    mtype = message.get("type")
    if mtype not in MSG_TYPES:
        raise ProtocolError("invalid_message",
                            {"type": "must be one of begin/tip/seal"})
    station_id = _as_id(message.get("station_id"), "station_id")
    batch_id = _as_id(message.get("batch_id"), "batch_id")
    norm = {"type": mtype, "station_id": station_id, "batch_id": batch_id}
    if mtype == "begin":
        start_ms = _as_int(message.get("start_ms"), "start_ms")
        end_ms = _as_int(message.get("end_ms"), "end_ms")
        count = _as_int(message.get("count"), "count",
                        minimum=0, maximum=MAX_TIPS)
        if start_ms % 1000 or end_ms % 1000:
            raise ProtocolError("invalid_message",
                                {"interval": "endpoints must be whole seconds"})
        if start_ms >= end_ms:
            raise ProtocolError("invalid_message",
                                {"interval": "start_ms must be < end_ms"})
        norm.update({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "count": count,
            "tip_mm": canonical_tip_mm(message.get("tip_mm")),
            "sha256": canonical_sha256(message.get("sha256")),
        })
    elif mtype == "tip":
        norm.update({
            "seq": _as_int(message.get("seq"), "seq", minimum=0),
            "time_ms": _as_int(message.get("time_ms"), "time_ms"),
            "tips": _as_int(message.get("tips"), "tips",
                            minimum=1, maximum=MAX_TIPS_PER_SEND),
        })
    return norm


def begin_content(norm: dict) -> dict:
    """Content that defines a begin message for idempotency comparisons."""
    return {k: norm[k] for k in ("start_ms", "end_ms", "count", "tip_mm", "sha256")}


def tip_content(norm: dict) -> dict:
    return {k: norm[k] for k in ("seq", "time_ms", "tips")}


def tip_digest(tips) -> str:
    """SHA-256 of ``seq,time_ms,tips\\n`` lines sorted by seq."""
    h = hashlib.sha256()
    for seq, time_ms, tips_count in sorted(tips):
        h.update(f"{seq},{time_ms},{tips_count}\n".encode("ascii"))
    return h.hexdigest()


def empty_digest() -> str:
    return hashlib.sha256(b"").hexdigest()


def missing_ranges(present_seqs, count: int):
    """Inclusive ``[lo, hi]`` ranges of sequence numbers absent from a batch."""
    present = set(present_seqs)
    ranges = []
    seq = 0
    while seq < count:
        if seq in present:
            seq += 1
            continue
        lo = seq
        while seq + 1 < count and (seq + 1) not in present:
            seq += 1
        ranges.append((lo, seq))
        seq += 1
    return ranges


def format_ranges(ranges):
    return [str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges]
