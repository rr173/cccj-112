"""Hand-computed verification of the full production path.

Every expected number below is derived by hand from the tiny fixture in
tests/fixture.py (see its docstring for the layout). Covers:

* accumulation -> interval differencing (incl. the 12 h period reset)
* negative-difference anomaly -> member excluded from the denominator
* valid-member denominator and member coverage ratio
* threshold event convention (>=, boundary value counts)
* probability calibration binning (incl. p=0.5 boundary, p=0, p=1)
* tied ensemble members in the CRPS
* regional aggregation with fixed station weights
* empty groups -> missing (NaN), never zero
* independent NumPy reference cross-check of the production output
"""
import csv
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from pipeline.runner import run_analysis  # noqa: E402
from reference.reference_impl import run_check  # noqa: E402

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


def check_fixture_outputs(outdir):
    # ---- accumulation -> interval, period reset, anomaly ----------------
    sm = _rows(os.path.join(outdir, "station_member.csv"))

    def interval(station, member, lead):
        r = _one(sm, station_id=station, member=member, lead_hours=lead)
        return _f(r["interval_mm"]), int(r["valid"])

    # plain difference: member 1, station A, lead 12: 16 - 4 = 12
    assert interval("A", 1, 12) == (12.0, 1)
    # period reset at lead 18: value itself (2), NOT 2 - 16 = -14
    assert interval("A", 1, 18) == (2.0, 1)
    # anomalous negative difference: member 3, lead 12, station A -> NaN
    v, ok = interval("A", 3, 12)
    assert math.isnan(v) and ok == 0
    # ... and it propagates through interpolation to station B ...
    v, ok = interval("B", 3, 12)
    assert math.isnan(v) and ok == 0
    # ... but station C only touches a healthy grid point: 17 - 12 = 5
    assert interval("C", 3, 12) == (5.0, 1)
    # interpolated value at station B, member 1, lead 12: 13 - 4 = 9
    assert interval("B", 1, 12) == (9.0, 1)

    events = _rows(os.path.join(outdir, "accum_events.csv"))
    kinds = {(r["member"], r["lead_hours"], r["kind"]) for r in events}
    assert ("3", "12", "negative_diff_anomaly") in kinds
    assert ("1", "18", "period_reset") in kinds  # reset detected, recorded

    # ---- probabilities, valid-member denominator, coverage -------------
    sp = _rows(os.path.join(outdir, "station_probs.csv"))

    def prob(station, lead, thr):
        return _one(sp, station_id=station, lead_hours=lead, threshold_mm=thr)

    r = prob("A", 12, "10")  # members {12, 10} (m3 invalid), obs 11
    _close(r["forecast_prob"], 1.0)          # 10 >= 10 counts (>= convention)
    assert int(r["n_valid_members"]) == 2    # denominator = valid members
    assert int(r["n_expected_members"]) == 3
    _close(r["member_coverage"], 2.0 / 3.0)
    assert int(r["obs_event"]) == 1
    r = prob("B", 12, "5")                   # members {9, 4} -> p = 0.5
    _close(r["forecast_prob"], 0.5)
    r = prob("A", 6, "10")                   # members {4, 8, 12} -> p = 1/3
    _close(r["forecast_prob"], 1.0 / 3.0)
    assert int(r["obs_event"]) == 0          # obs 7 < 10

    # ---- Brier score, regional weights 0.75 / 0.25 ----------------------
    st = _rows(os.path.join(outdir, "scores_threshold.csv"))

    def bs(region, lead, thr):
        return _one(st, region=region, lead_hours=lead, threshold_mm=thr)

    _close(bs("R1", 6, "10")["brier_score"], 7.0 / 36.0)   # 0.75/9 + 0.25*4/9
    _close(bs("R1", 12, "10")["brier_score"], 0.0)
    _close(bs("R1", 18, "10")["brier_score"], 1.0 / 12.0)  # 0.75/9
    _close(bs("R1", 6, "5")["brier_score"], 1.0 / 9.0)
    _close(bs("R1", 12, "5")["brier_score"], 0.0625)       # 0.25 * 0.5^2
    _close(bs("R1", 18, "5")["brier_score"], 1.0 / 9.0)
    _close(bs("R1", 12, "10")["mean_member_coverage"], 2.0 / 3.0)
    # empty group (region R2 has no observations): missing, not zero
    r2 = bs("R2", 6, "10")
    assert r2["brier_score"] == "" and int(r2["n_cases"]) == 0

    # ---- reliability bins (left-closed, top bin closed) -----------------
    rel = _rows(os.path.join(outdir, "reliability.csv"))

    def binrow(region, lead, thr, idx):
        return _one(rel, region=region, lead_hours=lead,
                    threshold_mm=thr, bin_index=idx)

    r = binrow("R1", 12, "5", 5)   # p = 0.5 -> bin [0.5, 0.6)
    assert int(r["n_cases"]) == 1
    _close(r["mean_prob"], 0.5)
    _close(r["obs_freq"], 0.0)
    r = binrow("R1", 12, "5", 9)   # p = 1.0 -> closed top bin [0.9, 1.0]
    assert int(r["n_cases"]) == 1
    _close(r["obs_freq"], 1.0)
    r = binrow("R1", 12, "10", 0)  # p = 0.0 -> first bin
    assert int(r["n_cases"]) == 1
    _close(r["obs_freq"], 0.0)
    r = binrow("R1", 6, "10", 3)   # two cases with p = 1/3
    assert int(r["n_cases"]) == 2
    _close(r["weighted_n"], 4.0)   # raw station weights 3 + 1
    _close(r["mean_prob"], 1.0 / 3.0)
    _close(r["obs_freq"], 0.25)    # weighted: only B (w 1 of 4) observed

    # ---- CRPS with tied members, regional weights -----------------------
    sc = _rows(os.path.join(outdir, "scores_crps.csv"))

    def crps(region, lead):
        return _one(sc, region=region, lead_hours=lead)

    # A lead 18 has tied members {2, 10, 2}; B lead 18 ties {5, 4, 5}
    _close(crps("R1", 6)["crps"], 53.0 / 36.0)   # 0.75*11/9 + 0.25*20/9
    _close(crps("R1", 12)["crps"], 1.1875)       # 0.75*0.5 + 0.25*3.25
    _close(crps("R1", 18)["crps"], 61.0 / 36.0)  # 0.75*11/9 + 0.25*28/9
    assert crps("R2", 6)["crps"] == ""           # empty group -> missing


def test_handcalculated_scores():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fixture.build(tmp)
        outdir = run_analysis(cfg)
        check_fixture_outputs(outdir)


def test_reference_crosscheck():
    """The independent NumPy reference must reproduce the production path."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = fixture.build(tmp)
        outdir = run_analysis(cfg)
        assert run_check(cfg, outdir, verbose=False) == 0


if __name__ == "__main__":
    test_handcalculated_scores()
    print("test_handcalculated_scores OK")
    test_reference_crosscheck()
    print("test_reference_crosscheck OK")
