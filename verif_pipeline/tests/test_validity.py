"""Validity propagation regression tests (hand-computed expectations).

Covers the faults reproduced by scripts/repro_validity_bugs.py and the
full missing-data chain: file decoding (_FillValue / missing_value /
non-finite / packed variables), accumulation differencing (both ends
required, reset recovery), spatial mapping (zero-weight neighbours),
and the statistical inputs (valid-member counts, expected-member
denominator fixed by eligible manifest rows, event probabilities,
regional scores). See tests/validity_fixture.py for the datasets and
every hand-derived number.
"""
import csv
import math
import os
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import validity_fixture as vf  # noqa: E402
from pipeline.accum import (FLAG_MISSING_INPUT, FLAG_MISSING_PREV, FLAG_OK,
                            FLAG_PERIOD_RESET, FLAG_START, compute_intervals)
from pipeline.io_forecast import read_forecast_nc
from pipeline.runner import run_analysis
from pipeline.synthetic import write_forecast_nc
from reference.reference_impl import run_check

TOL = 1e-9


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(s):
    return float(s) if s not in ("", None) else float("nan")


def _one(rows, **kw):
    m = [r for r in rows if all(r[k] == str(v) for k, v in kw.items())]
    assert len(m) == 1, f"expected 1 row for {kw}, got {len(m)}"
    return m[0]


def _close(got, want):
    assert abs(_f(got) - want) <= TOL, f"{got} != {want}"


# ---------------------------------------------------------------- V1: chain
def _v1(tmp):
    cfg = vf.build_v1(os.path.join(tmp, "v1"))
    return cfg, run_analysis(cfg)


def test_fill_value_never_scored_and_reset_recovery():
    with tempfile.TemporaryDirectory() as tmp:
        _, outdir = _v1(tmp)
        sm = _rows(os.path.join(outdir, "station_member.csv"))

        def interval(station, member, lead):
            r = _one(sm, station_id=station, member=member, lead_hours=lead)
            return r["interval_mm"], r["valid"]

        # the sentinel itself never reaches the station series
        assert interval("A", 2, 12) == ("", "0")     # missing input
        assert interval("A", 3, 18) == ("", "0")     # missing at reset lead
        assert interval("A", 3, 24) == ("", "0")     # difference needs both ends
        # counter reset at lead 18: member 2 recovers on the new reading
        assert interval("A", 2, 18) == ("6", "1")
        # station C never touches a bad cell
        assert interval("C", 2, 12) == ("3", "1")
        assert interval("C", 3, 18) == ("5", "1")
        assert interval("C", 3, 24) == ("7", "1")
        # clean members keep their exact values
        assert interval("A", 1, 12) == ("3", "1")
        assert interval("A", 1, 24) == ("5", "1")


def test_missing_reasons_are_recorded_per_grid_point():
    with tempfile.TemporaryDirectory() as tmp:
        _, outdir = _v1(tmp)
        events = _rows(os.path.join(outdir, "accum_events.csv"))
        kinds = {(r["member"], r["lead_hours"], r["lat"], r["lon"], r["kind"])
                 for r in events}
        assert ("2", "12", "0", "0", "missing_input") in kinds
        assert ("3", "18", "0", "0", "missing_input") in kinds
        assert ("3", "24", "0", "0", "missing_previous") in kinds
        # reset recovery is recorded as a normal period reset, not missing
        assert ("2", "18", "0", "0", "period_reset") in kinds
        gi = _rows(os.path.join(outdir, "grid_intervals.csv"))
        r = _one(gi, member="2", lead_hours="12", lat="0", lon="0")
        assert r["flag"] == "missing_input" and r["interval_mm"] == ""
        r = _one(gi, member="3", lead_hours="24", lat="0", lon="0")
        assert r["flag"] == "missing_previous" and r["interval_mm"] == ""
        r = _one(gi, member="2", lead_hours="18", lat="0", lon="0")
        assert r["flag"] == "period_reset" and r["interval_mm"] == "6"


def test_expected_members_fixed_by_manifest_and_scores():
    with tempfile.TemporaryDirectory() as tmp:
        _, outdir = _v1(tmp)
        sp = _rows(os.path.join(outdir, "station_probs.csv"))

        def prob(station, lead):
            return _one(sp, station_id=station, lead_hours=lead,
                        threshold_mm="6")

        # member 4 declared in the manifest but its file is missing:
        # the expected set stays {1,2,3,4}; only availability drops
        expect = {("A", 6): (3, 1 / 3), ("A", 12): (2, 0.0),
                  ("A", 18): (2, 0.5), ("A", 24): (2, 0.0),
                  ("C", 6): (3, 1 / 3), ("C", 12): (3, 0.0),
                  ("C", 18): (3, 1 / 3), ("C", 24): (3, 1 / 3)}
        for (station, lead), (n_valid, p) in expect.items():
            r = prob(station, lead)
            assert int(r["n_valid_members"]) == n_valid, (station, lead)
            assert int(r["n_expected_members"]) == 4, (station, lead)
            _close(r["member_coverage"], n_valid / 4.0)
            _close(r["forecast_prob"], p)

        st = _rows(os.path.join(outdir, "scores_threshold.csv"))

        def bs(lead):
            return _one(st, region="R1", lead_hours=lead, threshold_mm="6")

        _close(bs(6)["brier_score"], 5.0 / 18.0)    # ((1/3-1)^2+(1/3)^2)/2
        _close(bs(12)["brier_score"], 0.5)          # (0 + 1)/2
        _close(bs(18)["brier_score"], 25.0 / 72.0)  # (1/4 + 4/9)/2
        _close(bs(24)["brier_score"], 1.0 / 18.0)   # (0 + 1/9)/2
        _close(bs(12)["mean_member_coverage"], (2 + 3) / 8.0)

        sc = _rows(os.path.join(outdir, "scores_crps.csv"))

        def crps(lead):
            return _one(sc, region="R1", lead_hours=lead)

        _close(crps(6)["crps"], 20.0 / 9.0)    # (5/3 + 25/9)/2
        _close(crps(12)["crps"], 301.0 / 72.0)  # (9/4 + 55/9)/2
        _close(crps(18)["crps"], 91.0 / 72.0)  # (3/4 + 16/9)/2
        _close(crps(24)["crps"], 119.0 / 36.0)  # (5/2 + 37/9)/2


# ---------------------------------------------------------------- V2: scope
def test_zero_weight_neighbour_and_fully_missing_member():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = vf.build_v2(os.path.join(tmp, "v2"))
        outdir = run_analysis(cfg)
        sm = _rows(os.path.join(outdir, "station_member.csv"))

        def interval(station, member, lead):
            r = _one(sm, station_id=station, member=member, lead_hours=lead)
            return r["interval_mm"], r["valid"]

        # P sits exactly on (0,0): the bad (1,0) cell has zero weight there
        assert interval("P", 1, 6) == ("3", "1")
        assert interval("P", 1, 12) == ("6", "1")
        # Q at the cell centre touches (1,0) with weight 0.25 -> invalid,
        # and without a reset the gap propagates to the next interval
        assert interval("Q", 1, 6) == ("", "0")
        assert interval("Q", 1, 12) == ("", "0")
        # member 2 is entirely missing at lead 6 and cannot recover
        assert interval("P", 2, 6) == ("", "0")
        assert interval("P", 2, 12) == ("", "0")
        assert interval("Q", 2, 12) == ("", "0")
        assert interval("Q", 3, 6) == ("3.75", "1")

        sp = _rows(os.path.join(outdir, "station_probs.csv"))

        def prob(station, lead):
            return _one(sp, station_id=station, lead_hours=lead,
                        threshold_mm="5")

        r = prob("P", 6)
        assert int(r["n_valid_members"]) == 2
        assert int(r["n_expected_members"]) == 3
        _close(r["forecast_prob"], 0.5)
        r = prob("Q", 6)
        assert int(r["n_valid_members"]) == 1
        _close(r["member_coverage"], 1.0 / 3.0)
        _close(r["forecast_prob"], 0.0)
        r = prob("P", 12)
        _close(r["forecast_prob"], 1.0)

        st = _rows(os.path.join(outdir, "scores_threshold.csv"))
        _close(_one(st, lead_hours="6")["brier_score"], 0.125)
        _close(_one(st, lead_hours="12")["brier_score"], 1.0)
        sc = _rows(os.path.join(outdir, "scores_crps.csv"))
        _close(_one(sc, lead_hours="6")["crps"], 2.625)   # (1.5 + 3.75)/2
        _close(_one(sc, lead_hours="12")["crps"], 5.875)  # (6 + 5.75)/2


# ------------------------------------------------- V3: everything missing
def test_all_missing_case_yields_missing_scores_not_zero():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = vf.build_v3(os.path.join(tmp, "v3"))
        outdir = run_analysis(cfg)
        sm = _rows(os.path.join(outdir, "station_member.csv"))
        assert sm[0]["valid"] == "0" and sm[0]["interval_mm"] == ""
        sp = _rows(os.path.join(outdir, "station_probs.csv"))[0]
        assert sp["forecast_prob"] == ""
        assert int(sp["n_valid_members"]) == 0
        assert int(sp["n_expected_members"]) == 1
        _close(sp["member_coverage"], 0.0)
        st = _rows(os.path.join(outdir, "scores_threshold.csv"))[0]
        assert st["brier_score"] == "" and int(st["n_cases"]) == 0
        sc = _rows(os.path.join(outdir, "scores_crps.csv"))[0]
        assert sc["crps"] == "" and int(sc["n_cases"]) == 0
        rel = _rows(os.path.join(outdir, "reliability.csv"))
        assert len(rel) == 10
        assert all(int(r["n_cases"]) == 0 and r["mean_prob"] == ""
                   and r["obs_freq"] == "" for r in rel)


# ------------------------------------------------------- decoding unit tests
def test_read_forecast_nc_sentinels_and_packing():
    with tempfile.TemporaryDirectory() as tmp:
        leads, lats, lons = [6], [0.0], [0.0, 1.0]

        def read_back(apcp, **kw):
            p = os.path.join(tmp, "t.nc")
            write_forecast_nc(p, "2026-06-01T00:00:00Z", 1, leads, lats, lons,
                              np.array(apcp, dtype=float), 12, **kw)
            return read_forecast_nc(p)["apcp"]

        # _FillValue sentinel
        out = read_back([[[1.5, np.nan]]], fill_value=vf.FILL)
        assert out[0, 0, 0] == 1.5 and math.isnan(out[0, 0, 1])
        # missing_value attribute
        out = read_back([[[2.5, np.nan]]], missing_value=-999.0)
        assert out[0, 0, 0] == 2.5 and math.isnan(out[0, 0, 1])
        # non-finite literals (inf and nan stored directly)
        out = read_back([[[np.inf, np.nan]]])
        assert math.isnan(out[0, 0, 0]) and math.isnan(out[0, 0, 1])
        # packed variable: scale_factor/add_offset, _FillValue in packed units
        out = read_back([[[3.5, np.nan]]], fill_value=-32767,
                        packing={"scale_factor": 0.1, "add_offset": 1.0,
                                 "dtype": "i2"})
        assert abs(out[0, 0, 0] - 3.5) <= TOL and math.isnan(out[0, 0, 1])
        # packed variable with missing_value instead of _FillValue
        out = read_back([[[0.4, np.nan]]], missing_value=-32767,
                        packing={"scale_factor": 0.1, "add_offset": 0.0,
                                 "dtype": "i2"})
        assert abs(out[0, 0, 0] - 0.4) <= TOL and math.isnan(out[0, 0, 1])
        # clean data is returned unchanged
        out = read_back([[[1.25, 2.75]]])
        assert list(out[0, 0]) == [1.25, 2.75]


def test_compute_intervals_missing_propagation():
    def run(accum, reset):
        a = np.array(accum, dtype=float).reshape(-1, 1, 1)
        return compute_intervals([6, 12, 18, 24], a, reset)

    # reset period 12 h: the new segment after the counter reset recovers
    iv, flags = run([2.0, np.nan, 5.0, 9.0], 12)
    assert iv[0, 0, 0] == 2.0 and math.isnan(iv[1, 0, 0])
    assert iv[2, 0, 0] == 5.0 and iv[3, 0, 0] == 4.0
    assert list(flags[:, 0, 0]) == [FLAG_START, FLAG_MISSING_INPUT,
                                    FLAG_PERIOD_RESET, FLAG_OK]
    # no reset: a difference needs both ends, so the gap takes two intervals
    iv, flags = run([2.0, np.nan, 5.0, 9.0], 0)
    assert math.isnan(iv[1, 0, 0]) and math.isnan(iv[2, 0, 0])
    assert iv[3, 0, 0] == 4.0
    assert list(flags[:, 0, 0]) == [FLAG_START, FLAG_MISSING_INPUT,
                                    FLAG_MISSING_PREV, FLAG_OK]
    # non-finite readings are missing too
    iv, flags = run([1.0, np.inf, 3.0, 4.0], 0)
    assert list(flags[:, 0, 0]) == [FLAG_START, FLAG_MISSING_INPUT,
                                    FLAG_MISSING_PREV, FLAG_OK]


# ------------------------------------------- independent reference crosscheck
def test_reference_crosscheck_validity_fixtures():
    with tempfile.TemporaryDirectory() as tmp:
        for name, build in (("v1", vf.build_v1), ("v2", vf.build_v2),
                            ("v3", vf.build_v3)):
            cfg = build(os.path.join(tmp, name))
            outdir = run_analysis(cfg)
            assert run_check(cfg, outdir, verbose=False) == 0, name


def _mutate_station_probs(outdir, mutate):
    path = os.path.join(outdir, "station_probs.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0])
    for r in rows:
        mutate(r)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_reference_detects_shrunk_expected_denominator():
    """The old fault (expected set = members with readable files) must be
    caught by the reference: it derives the expected set from the manifest
    independently, so the two implementations can no longer share the same
    wrong denominator."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, outdir = _v1(tmp)
        assert run_check(cfg, outdir, verbose=False) == 0
        shadow = os.path.join(tmp, "shadow")
        shutil.copytree(outdir, shadow)

        def shrink(row):
            row["n_expected_members"] = "3"  # members with files, old fault
            row["member_coverage"] = f"{int(row['n_valid_members']) / 3:.10g}"

        _mutate_station_probs(shadow, shrink)
        assert run_check(cfg, shadow, verbose=False) > 0


def test_reference_detects_sentinel_scored_as_data():
    """Scoring the 9999 sentinel (probability 1 instead of 0 with one extra
    'valid' member) must be caught by the independent reference."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, outdir = _v1(tmp)
        assert run_check(cfg, outdir, verbose=False) == 0
        shadow = os.path.join(tmp, "shadow")
        shutil.copytree(outdir, shadow)

        def score_sentinel(row):
            if row["station_id"] == "A" and row["lead_hours"] == "12":
                row["n_valid_members"] = "3"
                row["forecast_prob"] = "1"

        _mutate_station_probs(shadow, score_sentinel)
        assert run_check(cfg, shadow, verbose=False) > 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([os.path.abspath(__file__), "-v"]))
