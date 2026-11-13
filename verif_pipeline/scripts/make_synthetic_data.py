#!/usr/bin/env python3
"""Generate the local synthetic demo dataset (no online services).

Creates:
  data/forecast/*.nc        accumulated-precipitation forecast files
  data/forecast/manifest.csv  product manifest (incl. a duplicate + a
                            missing-member entry)
  data/obs/stations.csv     station metadata with region weights
  data/obs/station_obs.csv  versioned station observations (incl. pre-
                            and post-cutoff corrections)

Dataset features exercised by the pipeline:
* 2 cycles x 3 members x 4 leads (6/12/18/24 h), 12 h accumulation reset
* cycle 2026-02-01T12Z member 2: file missing (member coverage < 1)
* cycle 2026-02-01T12Z member 3: anomalous negative accumulation
  difference at lead 12 h (excluded from the valid-member denominator)
* cycle 2026-02-01T00Z member 1: duplicate files, manifest priority
  selects run_b over run_a
* observations: one pre-cutoff correction (used), two post-cutoff
  corrections (ignored by the frozen analysis)
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from pipeline.synthetic import (write_forecast_nc, write_manifest, write_obs,
                                write_stations)
from pipeline.timeutil import format_time, parse_time

LATS = [39.0, 40.0, 41.0, 42.0]
LONS = [115.0, 116.0, 117.0, 118.0]
LEADS = [6, 12, 18, 24]
RESET_HOURS = 12
CYCLES = ["2026-02-01T00:00:00Z", "2026-02-01T12:00:00Z"]
MEMBERS = [1, 2, 3]

STATIONS = [
    {"station_id": "S1", "lat": 40.2, "lon": 116.3, "region": "North", "weight": 2.0},
    {"station_id": "S2", "lat": 41.1, "lon": 117.2, "region": "North", "weight": 1.0},
    {"station_id": "S3", "lat": 39.8, "lon": 115.6, "region": "South", "weight": 1.5},
    {"station_id": "S4", "lat": 40.9, "lon": 117.8, "region": "South", "weight": 1.0},
]


def cycle_tag(cycle):
    return cycle[:13].replace("-", "").replace("T", "")


def gen_accum(rng):
    """Interval precipitation accumulated with a 12 h reset."""
    iv = np.round(rng.gamma(1.5, 3.0, size=(len(LEADS), len(LATS), len(LONS))), 2)
    accum = np.zeros_like(iv)
    for i in range(len(LEADS)):
        if i == 0 or LEADS[i - 1] % RESET_HOURS == 0:
            accum[i] = iv[i]
        else:
            accum[i] = np.round(accum[i - 1] + iv[i], 2)
    return accum


def main():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    fdir = os.path.join(root, "forecast")
    odir = os.path.join(root, "obs")
    os.makedirs(fdir, exist_ok=True)
    os.makedirs(odir, exist_ok=True)
    rng = np.random.RandomState(20260201)

    manifest = []
    for ci, cycle in enumerate(CYCLES):
        for m in MEMBERS:
            tag = f"cyc{cycle_tag(cycle)}_mem{m}"
            if ci == 1 and m == 2:
                # member missing for this cycle: manifest row, no file
                manifest.append({"path": f"fcst_{tag}_runa.nc", "cycle": cycle,
                                 "member": m, "priority": 1, "run_id": "run_a"})
                continue
            accum = gen_accum(rng)
            if ci == 1 and m == 3:
                # anomalous negative difference at lead 12 (not a reset
                # boundary): accum(12) < accum(6) at grid point (0, 0)
                accum[1, 0, 0] = round(float(accum[0, 0, 0]) * 0.5, 2)
            if ci == 0 and m == 1:
                # duplicate files for the same (cycle, member); the
                # manifest priority selects run_b, run_a stays unused
                perturbed = np.round(accum * 1.3, 2)
                write_forecast_nc(os.path.join(fdir, f"fcst_{tag}_runa.nc"),
                                  cycle, m, LEADS, LATS, LONS, perturbed, RESET_HOURS)
                manifest.append({"path": f"fcst_{tag}_runa.nc", "cycle": cycle,
                                 "member": m, "priority": 2, "run_id": "run_a"})
                write_forecast_nc(os.path.join(fdir, f"fcst_{tag}_runb.nc"),
                                  cycle, m, LEADS, LATS, LONS, accum, RESET_HOURS)
                manifest.append({"path": f"fcst_{tag}_runb.nc", "cycle": cycle,
                                 "member": m, "priority": 1, "run_id": "run_b"})
                continue
            write_forecast_nc(os.path.join(fdir, f"fcst_{tag}_runa.nc"),
                              cycle, m, LEADS, LATS, LONS, accum, RESET_HOURS)
            manifest.append({"path": f"fcst_{tag}_runa.nc", "cycle": cycle,
                             "member": m, "priority": 1, "run_id": "run_a"})
    write_manifest(os.path.join(fdir, "manifest.csv"), manifest)

    write_stations(os.path.join(odir, "stations.csv"), STATIONS)

    valid_times = []
    for cycle in CYCLES:
        for lead in LEADS:
            valid_times.append(format_time(parse_time(cycle) + timedelta(hours=lead)))
    # different (cycle, lead) pairs can share the same valid time; the
    # observation database holds exactly one v1 row per valid time
    valid_times = sorted(set(valid_times))

    obs = []
    for st in STATIONS:
        for vt in valid_times:
            obs.append({"station_id": st["station_id"], "valid_time": vt,
                        "precip_mm": round(float(rng.gamma(1.5, 3.0)), 2),
                        "version": 1, "issued_time": "2026-02-02T00:00:00Z"})

    def bump(station, valid_time, add, version, issued):
        base = next(r["precip_mm"] for r in obs
                    if r["station_id"] == station and r["valid_time"] == valid_time
                    and r["version"] == 1)
        obs.append({"station_id": station, "valid_time": valid_time,
                    "precip_mm": round(base + add, 2),
                    "version": version, "issued_time": issued})

    # pre-cutoff correction (obs_cutoff = 2026-02-03T00:00:00Z): used
    bump("S1", "2026-02-01T12:00:00Z", 4.0, 2, "2026-02-02T06:00:00Z")
    # post-cutoff corrections: ignored by the frozen analysis
    bump("S1", "2026-02-01T12:00:00Z", 14.0, 3, "2026-02-05T00:00:00Z")
    bump("S3", "2026-02-02T06:00:00Z", 7.0, 2, "2026-02-04T00:00:00Z")
    write_obs(os.path.join(odir, "station_obs.csv"), obs)

    print(f"synthetic dataset written under {root}")
    print(f"  forecast files: {len(os.listdir(fdir)) - 1} (+ manifest)")
    print(f"  obs rows: {len(obs)} (incl. 3 corrections)")


if __name__ == "__main__":
    main()
