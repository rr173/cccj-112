"""Regression fixtures for validity propagation (all hand-computed).

V1 -- partial missing + adjacent-segment propagation + reset recovery
---------------------------------------------------------------------
2x2 grid (lat/lon 0.0, 1.0), one cycle 2026-04-01T00Z, leads 6/12/18/24 h,
accumulation reset 12 h, ``_FillValue = 9999`` on every ``apcp`` variable.
Manifest declares members 1-4; the member-4 file never exists, so
``n_expected_members = 4`` everywhere while availability drops per case.

Accumulated apcp (mm) at the two station grid points ((0,1) and (1,0)
are 0 everywhere and touched by no station):

====  ========  ========  ========  ========
m     lead 6    lead 12   lead 18   lead 24
====  ========  ========  ========  ========
1 A   2         5         3         8
1 C   1         4         2         7
2 A   4         FILL      6         9
2 C   4         7         6         9
3 A   8         10        FILL      12
3 C   8         10        5         12
====  ========  ========  ========  ========

Interval amounts (mm) at station A (grid point (0,0)):

* m1: 2, 5-2=3, 3 (reset), 8-3=5
* m2: 4, NaN (missing input), 6 (reset -> recovers on the new reading),
  9-6=3
* m3: 8, 10-8=2, NaN (missing input at the reset lead), NaN (difference
  needs both ends: accum(18) missing -> missing_previous)

Station C (grid point (1,1)) never touches a bad cell:
m1: 1,3,2,5; m2: 4,3,6,3; m3: 8,2,5,7.

Valid members per case: A = 3,2,2,2; C = 3,3,3,3 (member 4 has no file).
Threshold 6 mm; obs A: 7/0/6/1, C: 0/9/7/0 (events 1/0/1/0, 0/1/1/0).

V2 -- zero-weight neighbour + fully-missing member + no-reset propagation
-------------------------------------------------------------------------
2x2 grid, leads 6/12 h, reset 0 (no resets). Member 1 is FILL only at
grid point (1,0) lead 6; member 2 is FILL everywhere at lead 6 (hence
also unrecoverable at lead 12: no reset, the difference needs both
ends); member 3 is clean. Station P sits exactly on (0,0) (zero weight
on (1,0): must stay valid); station Q at the cell centre (0.25 on each
point: invalid wherever any point is missing).

V3 -- everything missing -> empty groups are missing, never zero
----------------------------------------------------------------
1x1 grid, one member, one lead, apcp = FILL everywhere. The single case
has zero valid members: probability and all scores are missing.
"""
import json
import os

import numpy as np

from pipeline.synthetic import (write_forecast_nc, write_manifest, write_obs,
                                write_stations)

FILL = 9999.0
LATS = [0.0, 1.0]
LONS = [0.0, 1.0]


def _grid(d):
    """Full (nt, 2, 2) accum array; ``d`` maps (lead_idx, ilat, ilon)."""
    nt = max(k[0] for k in d) + 1
    a = np.zeros((nt, 2, 2), dtype=float)
    for (t, i, j), v in d.items():
        a[t, i, j] = v
    return a


def _write(root, cycle, leads, reset, members_accum, manifest_members,
           stations, obs, thresholds, name, obs_cutoff, fill_value=FILL):
    fdir = os.path.join(root, "forecast")
    odir = os.path.join(root, "obs")
    os.makedirs(fdir, exist_ok=True)
    os.makedirs(odir, exist_ok=True)
    manifest = []
    for m in manifest_members:
        fname = f"fcst_m{m}.nc"
        if m in members_accum:
            write_forecast_nc(os.path.join(fdir, fname), cycle, m, leads,
                              LATS[: members_accum[m].shape[1]],
                              LONS[: members_accum[m].shape[2]],
                              members_accum[m], reset, fill_value=fill_value)
        manifest.append({"path": fname, "cycle": cycle, "member": m,
                         "priority": 1, "run_id": "run_a"})
    write_manifest(os.path.join(fdir, "manifest.csv"), manifest)
    write_stations(os.path.join(odir, "stations.csv"), stations)
    write_obs(os.path.join(odir, "station_obs.csv"), obs)
    cfg = {
        "name": name,
        "manifest_path": os.path.join(fdir, "manifest.csv"),
        "obs_path": os.path.join(odir, "station_obs.csv"),
        "stations_path": os.path.join(odir, "stations.csv"),
        "output_root": os.path.join(root, "outputs"),
        "init_cutoff": cycle,
        "obs_cutoff": obs_cutoff,
        "thresholds": thresholds,
        "reliability_bin_width": 0.1,
        "neg_diff_tolerance": 1e-6,
    }
    cfg_path = os.path.join(root, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg_path


CYCLE_V1 = "2026-04-01T00:00:00Z"
VT_V1 = {6: "2026-04-01T06:00:00Z", 12: "2026-04-01T12:00:00Z",
         18: "2026-04-01T18:00:00Z", 24: "2026-04-02T00:00:00Z"}

# member -> accum array; np.nan cells are stored as the _FillValue sentinel
V1_ACCUM = {
    1: _grid({(0, 0, 0): 2.0, (1, 0, 0): 5.0, (2, 0, 0): 3.0, (3, 0, 0): 8.0,
              (0, 1, 1): 1.0, (1, 1, 1): 4.0, (2, 1, 1): 2.0, (3, 1, 1): 7.0}),
    2: _grid({(0, 0, 0): 4.0, (1, 0, 0): np.nan, (2, 0, 0): 6.0, (3, 0, 0): 9.0,
              (0, 1, 1): 4.0, (1, 1, 1): 7.0, (2, 1, 1): 6.0, (3, 1, 1): 9.0}),
    3: _grid({(0, 0, 0): 8.0, (1, 0, 0): 10.0, (2, 0, 0): np.nan, (3, 0, 0): 12.0,
              (0, 1, 1): 8.0, (1, 1, 1): 10.0, (2, 1, 1): 5.0, (3, 1, 1): 12.0}),
}

V1_STATIONS = [
    {"station_id": "A", "lat": 0.0, "lon": 0.0, "region": "R1", "weight": 1.0},
    {"station_id": "C", "lat": 1.0, "lon": 1.0, "region": "R1", "weight": 1.0},
]

V1_OBS_VALUES = {("A", 6): 7.0, ("A", 12): 0.0, ("A", 18): 6.0, ("A", 24): 1.0,
                 ("C", 6): 0.0, ("C", 12): 9.0, ("C", 18): 7.0, ("C", 24): 0.0}


def build_v1(root, name="validity_v1"):
    obs = [{"station_id": s, "valid_time": VT_V1[lead], "precip_mm": v,
            "version": 1, "issued_time": "2026-04-02T00:00:00Z"}
           for (s, lead), v in sorted(V1_OBS_VALUES.items())]
    return _write(root, CYCLE_V1, [6, 12, 18, 24], 12, V1_ACCUM, [1, 2, 3, 4],
                  V1_STATIONS, obs, [6.0], name,
                  obs_cutoff="2026-04-03T00:00:00Z")


CYCLE_V2 = "2026-05-01T00:00:00Z"
VT_V2 = {6: "2026-05-01T06:00:00Z", 12: "2026-05-01T12:00:00Z"}

V2_ACCUM = {
    # fill only at (1,0) lead 6; lead 12 difference then needs both ends
    1: _grid({(0, 0, 0): 3.0, (0, 0, 1): 1.0, (0, 1, 0): np.nan, (0, 1, 1): 2.0,
              (1, 0, 0): 9.0, (1, 0, 1): 2.0, (1, 1, 0): 5.0, (1, 1, 1): 4.0}),
    # fully missing at lead 6; without a reset, lead 12 cannot recover
    2: _grid({(0, 0, 0): np.nan, (0, 0, 1): np.nan,
              (0, 1, 0): np.nan, (0, 1, 1): np.nan,
              (1, 0, 0): 7.0, (1, 0, 1): 7.0, (1, 1, 0): 7.0, (1, 1, 1): 7.0}),
    3: _grid({(0, 0, 0): 5.0, (0, 0, 1): 0.0, (0, 1, 0): 6.0, (0, 1, 1): 4.0,
              (1, 0, 0): 11.0, (1, 0, 1): 3.0, (1, 1, 0): 8.0, (1, 1, 1): 10.0}),
}

V2_STATIONS = [
    {"station_id": "P", "lat": 0.0, "lon": 0.0, "region": "R1", "weight": 1.0},
    {"station_id": "Q", "lat": 0.5, "lon": 0.5, "region": "R1", "weight": 1.0},
]

V2_OBS_VALUES = {("P", 6): 6.0, ("P", 12): 0.0, ("Q", 6): 0.0, ("Q", 12): 10.0}


def build_v2(root, name="validity_v2"):
    obs = [{"station_id": s, "valid_time": VT_V2[lead], "precip_mm": v,
            "version": 1, "issued_time": "2026-05-02T00:00:00Z"}
           for (s, lead), v in sorted(V2_OBS_VALUES.items())]
    return _write(root, CYCLE_V2, [6, 12], 0, V2_ACCUM, [1, 2, 3],
                  V2_STATIONS, obs, [5.0], name,
                  obs_cutoff="2026-05-03T00:00:00Z")


def build_v3(root, name="validity_v3"):
    """One member, one lead, apcp entirely _FillValue."""
    cycle = "2026-06-01T00:00:00Z"
    accum = {1: np.full((1, 1, 1), np.nan)}
    stations = [{"station_id": "S", "lat": 0.0, "lon": 0.0,
                 "region": "R1", "weight": 1.0}]
    obs = [{"station_id": "S", "valid_time": "2026-06-01T06:00:00Z",
            "precip_mm": 5.0, "version": 1,
            "issued_time": "2026-06-02T00:00:00Z"}]
    return _write(root, cycle, [6], 12, accum, [1], stations, obs, [6.0],
                  name, obs_cutoff="2026-06-03T00:00:00Z")
