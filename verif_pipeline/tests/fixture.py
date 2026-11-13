"""Shared fixture: a tiny, fully hand-computable verification dataset.

Layout (all numbers chosen so every score can be verified by hand):

* grid 2x2 (lat/lon 0.0, 1.0); one cycle 2026-01-01T00Z; leads 6/12/18 h;
  accumulation reset period 12 h (lead 18 uses the reset rule).
* 3 members. Member 3 has an anomalous negative accumulation difference
  at lead 12 at grid point (0,0) -> invalid there (valid-member
  denominator drops to 2 at stations A and B, stays 3 at station C).
* station A sits exactly on grid point (0,0); B in the cell centre
  (weights 0.25 each); C on (1,1) in region R2 but has NO observations
  (empty group -> missing scores).
* station weights A=3, B=1 (normalised 0.75/0.25) in region R1.

Interval precipitation at stations (mm):

====  ==  ========  ============  ==========
stn   m   lead 6    lead 12       lead 18 (reset)
====  ==  ========  ============  ==========
A     1   4         16-4 = 12     2
A     2   8         18-8 = 10     10
A     3   12        9-12 <0: NaN  2
B     1   4         13-4 = 9      5
B     2   8         12-8 = 4      4
B     3   12        NaN           5
C     1   4         8             6
C     2   8         2             2
C     3   12        5             6
====  ==  ========  ============  ==========

Observations (mm): A: 7 / 11 / 3; B: 12 / 2 / 8 (leads 6/12/18); C: none.
"""
import json
import os

import numpy as np

from pipeline.synthetic import (write_forecast_nc, write_manifest, write_obs,
                                write_stations)

CYCLE = "2026-01-01T00:00:00Z"
LEADS = [6, 12, 18]
RESET_HOURS = 12
LATS = [0.0, 1.0]
LONS = [0.0, 1.0]

# apcp[member][time][lat][lon], accumulated mm
MEMBER_ACCUM = {
    1: [[[4.0, 4.0], [4.0, 4.0]],
        [[16.0, 12.0], [12.0, 12.0]],
        [[2.0, 6.0], [6.0, 6.0]]],
    2: [[[8.0, 8.0], [8.0, 8.0]],
        [[18.0, 10.0], [10.0, 10.0]],
        [[10.0, 2.0], [2.0, 2.0]]],
    3: [[[12.0, 12.0], [12.0, 12.0]],
        [[9.0, 17.0], [17.0, 17.0]],
        [[2.0, 6.0], [6.0, 6.0]]],
}

VALID_TIMES = {6: "2026-01-01T06:00:00Z",
               12: "2026-01-01T12:00:00Z",
               18: "2026-01-01T18:00:00Z"}

OBS = {("A", VALID_TIMES[6]): 7.0,
       ("A", VALID_TIMES[12]): 11.0,
       ("A", VALID_TIMES[18]): 3.0,
       ("B", VALID_TIMES[6]): 12.0,
       ("B", VALID_TIMES[12]): 2.0,
       ("B", VALID_TIMES[18]): 8.0}

STATIONS = [
    {"station_id": "A", "lat": 0.0, "lon": 0.0, "region": "R1", "weight": 3.0},
    {"station_id": "B", "lat": 0.5, "lon": 0.5, "region": "R1", "weight": 1.0},
    {"station_id": "C", "lat": 1.0, "lon": 1.0, "region": "R2", "weight": 1.0},
]

OBS_ISSUED = "2026-01-02T00:00:00Z"
OBS_CUTOFF = "2026-01-03T00:00:00Z"


def build(root, extra_obs=(), name="handcalc"):
    """Write the fixture dataset under ``root``; returns the config path."""
    fdir = os.path.join(root, "forecast")
    odir = os.path.join(root, "obs")
    os.makedirs(fdir, exist_ok=True)
    os.makedirs(odir, exist_ok=True)

    manifest = []
    for m, accum in sorted(MEMBER_ACCUM.items()):
        fname = f"fcst_m{m}.nc"
        write_forecast_nc(os.path.join(fdir, fname), CYCLE, m, LEADS, LATS, LONS,
                          np.array(accum, dtype=float), RESET_HOURS)
        manifest.append({"path": fname, "cycle": CYCLE, "member": m,
                         "priority": 1, "run_id": "run_a"})
    write_manifest(os.path.join(fdir, "manifest.csv"), manifest)
    write_stations(os.path.join(odir, "stations.csv"), STATIONS)

    obs_rows = [{"station_id": s, "valid_time": vt, "precip_mm": v,
                 "version": 1, "issued_time": OBS_ISSUED}
                for (s, vt), v in sorted(OBS.items())]
    obs_rows += list(extra_obs)
    write_obs(os.path.join(odir, "station_obs.csv"), obs_rows)

    cfg = {
        "name": name,
        "manifest_path": os.path.join(fdir, "manifest.csv"),
        "obs_path": os.path.join(odir, "station_obs.csv"),
        "stations_path": os.path.join(odir, "stations.csv"),
        "output_root": os.path.join(root, "outputs"),
        "init_cutoff": CYCLE,  # cycle exactly at the cutoff is included
        "obs_cutoff": OBS_CUTOFF,
        "thresholds": [5.0, 10.0],
        "reliability_bin_width": 0.1,
        "neg_diff_tolerance": 1e-6,
    }
    cfg_path = os.path.join(root, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg_path
