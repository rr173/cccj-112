#!/usr/bin/env python3
"""Minimal reproductions of the validity-propagation faults.

Case 1 (sentinel scored as data): ``m3.nc`` stores ``_FillValue = 9999``
on its ``apcp`` grid point. Station A sits exactly on that point.
Expected: member 3 is invalid (valid=0, no 9999 anywhere); with the two
remaining legal readings 2 and 6 and event threshold 6 mm, station A's
event probability is 1/2 = 0.5. Faulty behaviour: valid=1, interval
9999, probability 2/3.

Case 2 (missing file shrinks the expected set): the manifest declares
members 1-4 but the member-4 file never exists. Expected:
``n_expected_members = 4`` and ``member_coverage = 3/4 = 0.75``. Faulty
behaviour: ``n_expected_members = 3`` and ``member_coverage = 1`` -- and
the reference check still passes because it derives the same wrong
denominator from the same readable files.

Exit code 0 = all expectations met (fixed); 1 = fault reproduced.
"""
import csv
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from pipeline.runner import run_analysis
from pipeline.synthetic import (write_forecast_nc, write_manifest, write_obs,
                                write_stations)
from reference.reference_impl import run_check

CYCLE = "2026-06-01T00:00:00Z"
VALID = "2026-06-01T06:00:00Z"
FILL = 9999.0


def _write_common(root, member_values, manifest_members, fill_value=None):
    """1x1 grid, one lead (6 h), station A on the single grid point."""
    fdir = os.path.join(root, "forecast")
    odir = os.path.join(root, "obs")
    os.makedirs(fdir, exist_ok=True)
    os.makedirs(odir, exist_ok=True)
    manifest = []
    for m in manifest_members:
        fname = f"m{m}.nc"
        if m in member_values:
            apcp = np.array([[[member_values[m]]]], dtype=float)
            write_forecast_nc(os.path.join(fdir, fname), CYCLE, m, [6],
                              [0.0], [0.0], apcp, 12, fill_value=fill_value)
        manifest.append({"path": fname, "cycle": CYCLE, "member": m,
                         "priority": 1, "run_id": "run_a"})
    write_manifest(os.path.join(fdir, "manifest.csv"), manifest)
    write_stations(os.path.join(odir, "stations.csv"),
                   [{"station_id": "A", "lat": 0.0, "lon": 0.0,
                     "region": "R", "weight": 1.0}])
    write_obs(os.path.join(odir, "station_obs.csv"),
              [{"station_id": "A", "valid_time": VALID, "precip_mm": 10.0,
                "version": 1, "issued_time": "2026-06-02T00:00:00Z"}])
    cfg = {
        "name": "repro",
        "manifest_path": os.path.join(fdir, "manifest.csv"),
        "obs_path": os.path.join(odir, "station_obs.csv"),
        "stations_path": os.path.join(odir, "stations.csv"),
        "output_root": os.path.join(root, "outputs"),
        "init_cutoff": CYCLE,
        "obs_cutoff": "2026-06-03T00:00:00Z",
        "thresholds": [6.0],
        "reliability_bin_width": 0.1,
        "neg_diff_tolerance": 1e-6,
    }
    cfg_path = os.path.join(root, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg_path


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


FAILURES = []


def expect(label, want, got):
    ok = got == want
    if not ok:
        FAILURES.append(label)
    print(f"  [{'OK ' if ok else 'BUG'}] {label}: expected {want!r}, got {got!r}")


def case1(tmp):
    print("case 1: _FillValue=9999 in m3.nc must not be scored")
    root = os.path.join(tmp, "case1")
    # m1 = 2 mm, m2 = 6 mm, m3 = 9999 (the _FillValue itself on disk)
    cfg = _write_common(root, {1: 2.0, 2: 6.0, 3: FILL}, [1, 2, 3],
                        fill_value=FILL)
    outdir = run_analysis(cfg)
    sm = _rows(os.path.join(outdir, "station_member.csv"))
    m3 = [r for r in sm if r["member"] == "3"][0]
    expect("member 3 valid flag", "0", m3["valid"])
    expect("member 3 interval (9999 must not appear)", "", m3["interval_mm"])
    sp = _rows(os.path.join(outdir, "station_probs.csv"))[0]
    expect("n_valid_members", "2", sp["n_valid_members"])
    expect("n_expected_members", "3", sp["n_expected_members"])
    expect("member_coverage", f"{2 / 3:.10g}", sp["member_coverage"])
    expect("forecast_prob (legal readings 2 and 6, threshold 6)",
           "0.5", sp["forecast_prob"])
    st = _rows(os.path.join(outdir, "scores_threshold.csv"))[0]
    expect("brier_score ((0.5-1)^2)", f"{0.25:.10g}", st["brier_score"])
    return cfg, outdir


def case2(tmp):
    print("case 2: manifest members 1-4, member-4 file missing")
    root = os.path.join(tmp, "case2")
    cfg = _write_common(root, {1: 2.0, 2: 6.0, 3: 1.0}, [1, 2, 3, 4])
    outdir = run_analysis(cfg)
    sp = _rows(os.path.join(outdir, "station_probs.csv"))[0]
    expect("n_valid_members", "3", sp["n_valid_members"])
    expect("n_expected_members (fixed by eligible manifest rows)",
           "4", sp["n_expected_members"])
    expect("member_coverage", "0.75", sp["member_coverage"])
    expect("forecast_prob (only member 2 >= 6)", f"{1 / 3:.10g}",
           sp["forecast_prob"])
    st = _rows(os.path.join(outdir, "scores_threshold.csv"))[0]
    expect("mean_member_coverage", "0.75", st["mean_member_coverage"])
    n_mism = run_check(cfg, outdir, verbose=False)
    print(f"  [info] reference check mismatches: {n_mism} "
          "(passes either way when both sides share the wrong denominator)")
    return cfg, outdir


def main():
    with tempfile.TemporaryDirectory() as tmp:
        case1(tmp)
        case2(tmp)
    print()
    if FAILURES:
        print(f"VERDICT: FAULT REPRODUCED -- {len(FAILURES)} expectation(s) "
              f"violated: {FAILURES}")
        return 1
    print("VERDICT: all expectations met -- validity propagates correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
