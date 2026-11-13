"""Forecast product manifest handling and NetCDF reading.

A forecast product consists of cycles x members x valid times, one NetCDF
file per (cycle, member). If duplicate files exist for the same
(cycle, member), the manifest ``priority`` decides: the lowest priority
value wins; ties are broken by the lexicographically smallest path.
Every file not used is reported with a reason.
"""
from __future__ import annotations

import csv
import os

import netCDF4
import numpy as np

from .timeutil import parse_time

MANIFEST_FIELDS = ("path", "cycle", "member", "priority", "run_id")


def read_manifest(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "path": r["path"],
                    "cycle": r["cycle"],
                    "member": int(r["member"]),
                    "priority": int(r["priority"]),
                    "run_id": r["run_id"],
                }
            )
    return rows


def resolve_manifest_paths(rows: list, manifest_path: str) -> None:
    """Resolve manifest-relative file paths in place (``_abspath``)."""
    base = os.path.dirname(manifest_path)
    for r in rows:
        p = r["path"]
        r["_abspath"] = p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))


def select_files(rows: list, init_cutoff: str):
    """Pick one file per (cycle, member).

    Returns ``(selected, unused)`` where ``selected`` maps
    ``(cycle, member) -> manifest row`` and ``unused`` is a list of
    manifest rows extended with a ``reason`` field.
    """
    cutoff = parse_time(init_cutoff)
    selected = {}
    unused = []
    candidates = {}
    for r in rows:
        if parse_time(r["cycle"]) > cutoff:
            unused.append({**r, "reason": "cycle_after_init_cutoff"})
            continue
        candidates.setdefault((r["cycle"], r["member"]), []).append(r)
    for key in sorted(candidates):
        cands = sorted(candidates[key], key=lambda r: (r["priority"], r["path"]))
        selected[key] = cands[0]
        for r in cands[1:]:
            unused.append({**r, "reason": "duplicate_lower_priority"})
    return selected, unused


def read_forecast_nc(path: str) -> dict:
    """Read one forecast file.

    ``apcp`` is ACCUMULATED precipitation (mm) since the start of the
    current accumulation period; see global attribute
    ``reset_interval_hours``. It is never a per-hour rate.
    """
    ds = netCDF4.Dataset(path)
    try:
        ds.set_auto_mask(False)
        data = {
            "cycle": str(ds.getncattr("cycle")),
            "member": int(ds.getncattr("member")),
            "reset_interval_hours": int(ds.getncattr("reset_interval_hours")),
            "leads": [int(x) for x in ds.variables["lead_hours"][:]],
            "lats": [float(x) for x in ds.variables["lat"][:]],
            "lons": [float(x) for x in ds.variables["lon"][:]],
            "apcp": np.array(ds.variables["apcp"][:], dtype=float),
        }
    finally:
        ds.close()
    return data
