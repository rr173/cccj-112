"""Export accepted gauge batches as versioned station observations.

The exported CSV has exactly the columns consumed by
:mod:`pipeline.io_obs` (``station_id,valid_time,precip_mm,version,
issued_time``).  Pulses are accumulated into UTC 3-hour buckets; a pulse
at exactly a bucket boundary belongs to the bucket that *starts* there
(half-open ``[start, end)``).  ``valid_time`` is the bucket end, so the
00-03 UTC observation carries ``03:00Z``.

Completeness is a property of the *accepted batch coverage*, never of
"did packets arrive": a bucket is written only when the union of accepted
batch intervals covers every millisecond of that 3-hour bucket.  Zero
pulses inside full coverage is a real ``0``; any hole suppresses the row
and is reported in the gaps file.  Adjacent accepted segments may jointly
prove completeness.

Arithmetic is done with :class:`decimal.Decimal` on the ``tip_mm``
decimal strings, so repeated 0.2 mm tips give exactly 0.6 mm instead of a
binary floating-point tail.

Every export rewrites the output files; nothing is appended.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pipeline.timeutil import parse_time

BUCKET_MS = 3 * 3600 * 1000
OBS_FIELDS = ("station_id", "valid_time", "precip_mm", "version", "issued_time")
GAP_FIELDS = ("station_id", "valid_time", "missing_start", "missing_end",
              "reason", "pending_batch_ids")
VERSION = 1


def _iso(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def _epoch_ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


def validate_bounds(start: str, end: str):
    """Return ``(start_ms, end_ms)``; both must be UTC 3-hour boundaries."""
    start_dt, end_dt = parse_time(start), parse_time(end)
    for label, dt in (("start", start_dt), ("end", end_dt)):
        if dt.minute or dt.second or dt.microsecond or dt.hour % 3:
            raise ValueError(
                f"{label} boundary {dt.isoformat()} is not a UTC 3-hour boundary")
    start_ms, end_ms = _epoch_ms(start_dt), _epoch_ms(end_dt)
    if start_ms >= end_ms:
        raise ValueError("start must be before end")
    return start_ms, end_ms


def _merge(intervals):
    out = []
    for lo, hi in sorted(intervals):
        if lo >= hi:
            continue
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(lo, hi) for lo, hi in out]


def _clip(intervals, lo, hi):
    return [(max(a, lo), min(b, hi)) for a, b in intervals if a < hi and b > lo]


def _complement(intervals, lo, hi):
    """Sub-intervals of ``[lo, hi)`` not covered by ``intervals``."""
    missing = []
    cursor = lo
    for a, b in _merge(_clip(intervals, lo, hi)):
        if a > cursor:
            missing.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < hi:
        missing.append((cursor, hi))
    return missing


def _read_stations(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        return sorted(r["station_id"] for r in csv.DictReader(f))


def _write_csv_atomic(path: str, fieldnames, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def export_observations(db_path: str, stations_path: str, start: str, end: str,
                        issued_time: str, output_path: str,
                        gaps_path: str | None = None):
    """Write observations and a gap report; returns a summary dict."""
    start_ms, end_ms = validate_bounds(start, end)
    issued_dt = parse_time(issued_time)
    if issued_dt > datetime.now(timezone.utc) + timedelta(days=1):
        # Not an error, but a likely unit mistake; keep going (deterministic).
        pass
    issued_canon = issued_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if gaps_path is None:
        root, _ = os.path.splitext(output_path)
        gaps_path = root + ".gaps.csv"

    for path in (output_path, gaps_path):
        if os.path.exists(path):
            os.remove(path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        stations = _read_stations(stations_path)
        buckets = [(t, t + BUCKET_MS)
                   for t in range(start_ms, end_ms, BUCKET_MS)]
        if not buckets:
            raise ValueError("export window contains no 3-hour bucket")
        obs_rows = []
        gap_rows = []
        per_station = {}
        for sid in stations:
            accepted = [dict(r) for r in conn.execute(
                "SELECT batch_id,start_ms,end_ms,tip_mm FROM accepted_batches "
                "WHERE station_id=? ORDER BY start_ms,batch_id", (sid,))]
            pending = [dict(r) for r in conn.execute(
                "SELECT batch_id,start_ms,end_ms FROM batches "
                "WHERE station_id=? AND status<>'accepted' "
                "ORDER BY start_ms,batch_id", (sid,))]
            accepted_intervals = [(r["start_ms"], r["end_ms"]) for r in accepted]
            # A defensive check: accepted intervals must never overlap.
            merged = _merge(accepted_intervals)
            if sum(b - a for a, b in merged) != sum(
                    b - a for a, b in accepted_intervals):
                raise RuntimeError(f"overlapping accepted batches for {sid}")

            tips_by_batch = {}
            for r in accepted:
                tips_by_batch[r["batch_id"]] = conn.execute(
                    "SELECT time_ms,tips FROM tips WHERE station_id=? AND "
                    "batch_id=? ORDER BY seq", (sid, r["batch_id"])).fetchall()
            buckets_out = []
            for b0, b1 in buckets:
                missing = _complement(accepted_intervals, b0, b1)
                valid_time = _iso(b1)
                if missing:
                    for m0, m1 in missing:
                        overlap_pending = [
                            p["batch_id"] for p in pending
                            if p["start_ms"] < m1 and p["end_ms"] > m0]
                        if overlap_pending:
                            reason = "unaccepted_batch_only"
                        elif any(r["start_ms"] < b1 and r["end_ms"] > b0
                                 for r in accepted):
                            reason = "partial_gap"
                        else:
                            reason = "no_accepted_coverage"
                        gap_rows.append({
                            "station_id": sid,
                            "valid_time": valid_time,
                            "missing_start": _iso(m0),
                            "missing_end": _iso(m1),
                            "reason": reason,
                            "pending_batch_ids": ";".join(overlap_pending),
                        })
                    buckets_out.append({"bucket": [_iso(b0), _iso(b1)],
                                        "status": "missing",
                                        "missing_ranges": [
                                            [_iso(a), _iso(b)] for a, b in missing]})
                    continue
                total = Decimal(0)
                for r in accepted:
                    tip_mm = Decimal(r["tip_mm"])
                    for t in tips_by_batch[r["batch_id"]]:
                        if b0 <= t["time_ms"] < b1:
                            total += tip_mm * int(t["tips"])
                precip = format(total, "f")
                if "." in precip:
                    precip = precip.rstrip("0").rstrip(".")
                if precip in ("", "-0"):
                    precip = "0"
                obs_rows.append({
                    "station_id": sid,
                    "valid_time": valid_time,
                    "precip_mm": precip,
                    "version": VERSION,
                    "issued_time": issued_canon,
                })
                buckets_out.append({"bucket": [_iso(b0), _iso(b1)],
                                    "status": "written",
                                    "precip_mm": precip})
            per_station[sid] = {
                "accepted_batches": len(accepted),
                "pending_batches": len(pending),
                "buckets": buckets_out,
            }
    finally:
        conn.close()

    obs_rows.sort(key=lambda r: (r["station_id"], r["valid_time"]))
    gap_rows.sort(key=lambda r: (r["station_id"], r["missing_start"]))
    _write_csv_atomic(output_path, OBS_FIELDS, obs_rows)
    _write_csv_atomic(gaps_path, GAP_FIELDS, gap_rows)
    return {
        "start": _iso(start_ms),
        "end": _iso(end_ms),
        "issued_time": issued_canon,
        "output_path": output_path,
        "gaps_path": gaps_path,
        "obs_rows": obs_rows,
        "gap_rows": gap_rows,
        "per_station": per_station,
    }
