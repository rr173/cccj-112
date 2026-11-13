"""UTC time helpers. All timestamps in this project are ISO-8601 UTC."""
from __future__ import annotations

from datetime import datetime, timezone


def parse_time(s: str) -> datetime:
    """Parse an ISO-8601 string; naive input is assumed to be UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_time(dt: datetime) -> str:
    """Canonical UTC representation used in all output files."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
