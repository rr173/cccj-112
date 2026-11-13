"""Station metadata and versioned station observations.

Observation files may contain several versions of the same
(station, valid_time) value, each with an ``issued_time``. An analysis
frozen at ``obs_cutoff`` uses, per (station, valid_time), the highest
version issued at or before the cutoff. Corrections issued after the
cutoff are ignored; a correction therefore only affects the valid
times it actually pertains to. Exact (version, issued_time) ties are
broken by file order: the later row supersedes.
"""
from __future__ import annotations

import csv

from .timeutil import parse_time

STATION_FIELDS = ("station_id", "lat", "lon", "region", "weight")
OBS_FIELDS = ("station_id", "valid_time", "precip_mm", "version", "issued_time")


def read_stations(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "station_id": r["station_id"],
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "region": r["region"],
                    "weight": float(r["weight"]),
                }
            )
    return rows


def read_obs(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "station_id": r["station_id"],
                    "valid_time": r["valid_time"],
                    "precip_mm": float(r["precip_mm"]),
                    "version": int(r["version"]),
                    "issued_time": r["issued_time"],
                }
            )
    return rows


def select_obs_versions(rows: list, obs_cutoff: str):
    """Freeze observations at ``obs_cutoff``.

    Returns ``(selected, excluded)``; ``selected`` maps
    ``(station_id, valid_time) -> obs row`` and ``excluded`` lists every
    unused row with a ``reason``.
    """
    cutoff = parse_time(obs_cutoff)
    eligible = {}
    excluded = []
    for r in rows:
        if parse_time(r["issued_time"]) > cutoff:
            excluded.append({**r, "reason": "issued_after_obs_cutoff"})
            continue
        eligible.setdefault((r["station_id"], r["valid_time"]), []).append(r)
    selected = {}
    for key in sorted(eligible):
        cands = sorted(eligible[key], key=lambda r: (r["version"], r["issued_time"]))
        selected[key] = cands[-1]
        for r in cands[:-1]:
            excluded.append({**r, "reason": "superseded_by_newer_version"})
    return selected, excluded
