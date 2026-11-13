"""Tests for the offline gauge-network planner.

The production optimiser is an exact dynamic program over coverage
signatures.  To prove that the four-layer objective and the stable
tie-break are right, the tests include an *independent* exhaustive
enumeration written directly from the requirement text (a small, separate
implementation), and compare the two on many small instances and on the
checked-in examples.

These tests are plain functions with asserts (pytest-compatible) and can
also be run without pytest via

    python3 scripts/planning/selftest_planning.py
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import sys
import tempfile
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "planning"))

from pipeline.planning import (  # noqa: E402
    PlanningError, brute_force, solve, haversine_km, load_plan_inputs, run_plan,
)

DATA = os.path.join(ROOT, "data", "planning")
EXAMPLES = os.path.join(DATA, "examples")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path, text):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(text)


def _temp_tables(tmp, stations, samples, candidates):
    os.makedirs(tmp, exist_ok=True)
    sp = os.path.join(tmp, "stations.csv")
    samp = os.path.join(tmp, "samples.csv")
    cp = os.path.join(tmp, "candidates.csv")
    _write(sp, stations)
    _write(samp, samples)
    _write(cp, candidates)
    return samp, cp, sp


def _run(stations, samples, candidates, budget, radius, required=None,
         forbidden=None):
    with tempfile.TemporaryDirectory() as tmp:
        paths = _temp_tables(tmp, stations, samples, candidates)
        return solve(load_plan_inputs(*paths, budget, radius, required, forbidden))


def _plan(samples_path, candidates_path, stations_path, budget, radius,
          required=None, forbidden=None):
    inde = load_plan_inputs(samples_path, candidates_path, stations_path,
                            budget, radius, required, forbidden)
    return solve(inde)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# 1. Distance: Haversine, inclusive radius
# ---------------------------------------------------------------------------


def test_haversine_declared_radius_and_boundary():
    # one degree of longitude on the equator at R = 6371 km
    assert abs(haversine_km(0.0, 0.0, 0.0, 1.0) - 111.19492664455873) < 1e-9
    # antipodal
    assert abs(haversine_km(0.0, 0.0, 0.0, 180.0) - math.pi * 6371.0) < 1e-6
    # zero
    assert haversine_km(12.0, 34.0, 12.0, 34.0) == 0.0

    # A sample exactly at distance == radius is served (inclusive); a
    # sample one metre farther is not.
    stations = "station_id,lat,lon\nS,0,0\n"
    samples = "sample_id,lat,lon,weight\nIn,0,1.0,5\n"
    candidates = "candidate_id,lat,lon,cost\nC,0,0.5,1\n"
    radius = haversine_km(0.0, 0.0, 0.0, 1.0)
    in_plan = _run(stations, samples, candidates, 1, radius)
    assert in_plan.before_counts[0] == 1  # exactly at radius -> served
    out_plan = _run(stations, "sample_id,lat,lon,weight\nOut,0,1.0001,1\n",
                    "candidate_id,lat,lon,cost\nFar,2,2,1\n", 1, radius)
    assert out_plan.before_counts == [0]
    assert out_plan.after_counts == [0]  # beyond radius is not served


# ---------------------------------------------------------------------------
# 2. Exact brute-force parity
# ---------------------------------------------------------------------------


def test_brute_force_matches_dp_on_examples():
    """The production DP and the exhaustive enumeration agree on every
    checked-in example across a range of budgets and radii."""
    cases = [
        ("greedy_trap", None),
        ("redundancy", None),
        ("old_station", None),
        ("tie", None),
    ]
    for name, _ in cases:
        d = os.path.join(EXAMPLES, name)
        for budget in ("0", "1", "4", "5", "6", "8", "9", "10", "11", "12",
                       "16", "23", "24"):
            for radius in (1.0, 3.0, 10.0, 30.0, 200.0):
                ins = load_plan_inputs(os.path.join(d, "samples.csv"),
                                       os.path.join(d, "candidates.csv"),
                                       os.path.join(d, "stations.csv"),
                                       budget, radius)
                got = solve(ins)
                best = brute_force(ins)
                assert best is not None
                assert tuple(got.chosen) == tuple(best[1]), (
                    name, budget, radius, got.chosen, best[1])
                assert got.layer1_after == best[0][0]
                assert got.layer2_after == best[0][1]
                assert got.total_cost == best[0][2]


def test_brute_force_matches_dp_on_random_instances():
    """Randomised small instances (deterministic seed): 2^n enumeration vs DP."""
    rng = random.Random(20261113)
    for trial in range(40):
        n_samples = rng.randint(1, 7)
        n_cands = rng.randint(1, 9)
        n_stations = rng.randint(0, 3)
        samples = ["sample_id,lat,lon,weight"]
        candidates = ["candidate_id,lat,lon,cost"]
        stations = ["station_id,lat,lon"]
        for i in range(n_samples):
            samples.append(f"S{i},{rng.uniform(-1, 1):.4f},"
                           f"{rng.uniform(-1, 1):.4f},{rng.choice([1, 2, 3, 5, 7])}")
        for i in range(n_cands):
            candidates.append(f"C{i},{rng.uniform(-1, 1):.4f},"
                              f"{rng.uniform(-1, 1):.4f},{rng.randint(0, 9)}")
        for i in range(n_stations):
            stations.append(f"E{i},{rng.uniform(-1, 1):.4f},"
                            f"{rng.uniform(-1, 1):.4f}")
        budget = rng.randint(0, max(1, 3 * n_cands))
        radius = rng.choice([10.0, 40.0, 90.0, 150.0])
        required = []
        forbidden = []
        keys = [f"C{i}" for i in range(n_cands)]
        if keys and rng.random() < 0.4:
            required = [rng.choice(keys)]
        if keys and rng.random() < 0.4:
            forbidden = [rng.choice(keys)]
        if set(required) & set(forbidden):
            forbidden = []
        try:
            ins = load_plan_inputs(*_temp_tables(
                os.path.join(tempfile.mkdtemp(), "t"),
                "\n".join(stations) + "\n", "\n".join(samples) + "\n",
                "\n".join(candidates) + "\n"), budget, radius, required,
                forbidden)
        except PlanningError as exc:
            if exc.budget_conflict:
                continue
            raise
        got = solve(ins)
        best = brute_force(ins)
        assert best is not None
        assert tuple(got.chosen) == tuple(best[1]), trial
        assert got.layer1_after == best[0][0], trial
        assert got.layer2_after == best[0][1], trial
        assert got.total_cost == best[0][2], trial


# ---------------------------------------------------------------------------
# 3. Greedy would do worse
# ---------------------------------------------------------------------------


def test_greedy_nearest_picks_low_weight_plan():
    """At budget 12 both Guide (cost 11) and Hero (cost 12) are affordable
    individually.  A "nearest to the first uncovered sample" greedy picks
    Guide (the sample L sorts first and Guide sits on it) for weight 6 and
    cannot afford Hero; the exact optimiser picks Hero for weight 10.

    The DP must never mimic the greedy choice here."""
    d = os.path.join(EXAMPLES, "greedy_trap")
    r = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
              os.path.join(d, "stations.csv"), 12, 30)
    assert r.chosen == ["Hero"]
    assert r.layer1_after == Decimal("10")
    assert r.total_cost == Decimal("12")
    # Guide is affordable and sits exactly on sample L; it is left out.
    assert "Guide" not in r.chosen
    # One position can improve several neighbouring reaches at once: at
    # budget 23 both sites are bought and the cluster is fully redundant.
    r2 = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
               os.path.join(d, "stations.csv"), 23, 30)
    assert r2.chosen == ["Guide", "Hero"]
    assert r2.layer1_after == Decimal("16")


# ---------------------------------------------------------------------------
# 4. High-weight redundancy needs the *second* station
# ---------------------------------------------------------------------------


def test_high_weight_reach_needs_second_station_for_redundancy():
    d = os.path.join(EXAMPLES, "redundancy")
    # H already has one source (E); N1..N3 have one source (E).  Both A and
    # B are affordable at 8; A only adds redundancy to H (weight 100), B
    # only to N1..N3 (weight 6).  Layer 2 must pick A.
    r = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
              os.path.join(d, "stations.csv"), 8, 30)
    assert r.chosen == ["A"]
    assert r.layer2_before == Decimal("0")
    assert r.layer2_after == Decimal("100")
    # With enough money both redundancies are bought.
    r2 = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
               os.path.join(d, "stations.csv"), 16, 30)
    assert r2.chosen == ["A", "B"]
    assert r2.layer2_after == Decimal("106")
    # Layer 1 is unchanged in both cases: redundancy is a separate layer.
    assert r.layer1_after == r2.layer1_after == Decimal("106")


# ---------------------------------------------------------------------------
# 5. Budget boundary: exactly used vs one unit over
# ---------------------------------------------------------------------------


def test_budget_exactly_used_and_one_over():
    d = os.path.join(EXAMPLES, "old_station")
    samples = os.path.join(d, "samples.csv")
    cands = os.path.join(d, "candidates.csv")
    stations = os.path.join(d, "stations.csv")
    exactly = _plan(samples, cands, stations, 9, 30)
    assert exactly.chosen == ["F1"] and exactly.total_cost == Decimal("9")
    over = _plan(samples, cands, stations, 8.999, 30)
    assert over.chosen == [] and over.total_cost == Decimal("0")
    # Decimal, not binary float: 0.1 + 0.2 == 0.3 exactly is a real test.
    stations_t = "station_id,lat,lon\n"
    samples_t = "sample_id,lat,lon,weight\nA,0,0,5\n"
    cands_t = ("candidate_id,lat,lon,cost\nC1,0,0.01,0.1\nC2,0,0.02,0.2\n")
    fit = _run(stations_t, samples_t, cands_t, "0.3", 10)
    assert fit.chosen == ["C1", "C2"]
    assert fit.total_cost == Decimal("0.3")
    # One unit below the exact sum: both sites no longer fit; the cheaper
    # site still covers the same sample, so layer 3 picks C1 (0.1), not C2.
    miss = _run(stations_t, samples_t, cands_t, "0.2999999999", 10)
    assert miss.chosen == ["C1"] and miss.total_cost == Decimal("0.1")


# ---------------------------------------------------------------------------
# 6. Existing stations are free and not duplicated
# ---------------------------------------------------------------------------


def test_existing_stations_do_not_consume_budget_and_are_not_rebought():
    d = os.path.join(EXAMPLES, "old_station")
    r = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
              os.path.join(d, "stations.csv"), 1, 30)
    # N is cheap but adds nothing (A is already doubled by E1/E2), so the
    # optimiser buys nothing: layer 1 and 2 are already maximal and cost 0.
    assert r.chosen == [] and r.total_cost == Decimal("0")
    assert r.before_counts == [2, 0]
    assert r.after_counts == [2, 0]
    # Existing stations contribute but are never charged: with budget 9 the
    # only purchase is F1 (note N stays unchosen).
    r2 = _plan(os.path.join(d, "samples.csv"), os.path.join(d, "candidates.csv"),
               os.path.join(d, "stations.csv"), 9, 30)
    assert r2.chosen == ["F1"]
    assert r2.required_cost == Decimal("0") and r2.optional_cost == Decimal("9")


def test_one_candidate_is_one_source_not_two():
    stations = "station_id,lat,lon\n"
    samples = "sample_id,lat,lon,weight\nA,0,0,5\n"
    candidates = "candidate_id,lat,lon,cost\nC,0,0.01,5\n"
    r = _run(stations, samples, candidates, 5, 10)
    assert r.chosen == ["C"]
    assert r.after_counts == [1]           # single, not redundant
    assert r.layer2_after == Decimal("0")
    # A duplicated candidate row is rejected instead of counting twice.
    with tempfile.TemporaryDirectory() as tmp:
        paths = _temp_tables(tmp, stations, samples,
                             "candidate_id,lat,lon,cost\nC,0,0.01,5\nC,0,0.02,5\n")
        try:
            load_plan_inputs(*paths, 5, 10)
        except PlanningError as exc:
            assert any(e["kind"] == "duplicate_key" for e in exc.errors)
        else:
            raise AssertionError("duplicate candidate key was accepted")


# ---------------------------------------------------------------------------
# 7. Required / forbidden
# ---------------------------------------------------------------------------


def test_required_is_always_bought_and_counted():
    stations = "station_id,lat,lon\n"
    samples = "sample_id,lat,lon,weight\nA,0,0,5\n"
    candidates = ("candidate_id,lat,lon,cost\n"
                  "Rq,0,0.01,5\nOpt,0,0.02,5\n")
    # Without a requirement layer 3 would buy nothing at 10?  A is already
    # covered only by these candidates, so both add coverage; with the
    # budget 5 the required Rq is bought and Opt is not.
    r = _run(stations, samples, candidates, 5, 10, required=["Rq"])
    assert r.chosen == ["Rq"]
    assert r.total_cost == Decimal("5")
    assert r.chosen_required == ["Rq"]
    # With enough budget the optimiser buys the optional site too; the
    # required cost stays separate.
    r2 = _run(stations, samples, candidates, 10, 10, required=["Rq"])
    assert r2.chosen == ["Opt", "Rq"]
    assert r2.required_cost == Decimal("5")
    assert r2.optional_cost == Decimal("5")


def test_required_and_forbidden_same_candidate_rejected():
    d = os.path.join(EXAMPLES, "conflict")
    with open(os.path.join(d, "samples.csv")) as f:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        paths = _temp_tables(
            tmp, "station_id,lat,lon\n",
            "sample_id,lat,lon,weight\nA,0,0,5\n",
            "candidate_id,lat,lon,cost,required,forbidden\n"
            "X,0,0.01,5,1,0\n")
        try:
            load_plan_inputs(*paths, 10, 10, ["X"], ["X"])
        except PlanningError as exc:
            kinds = {e["kind"] for e in exc.errors}
            assert "required_forbidden_conflict" in kinds
        else:
            raise AssertionError("required+forbidden was accepted")


def test_required_over_budget_reports_conflict_and_writes_nothing():
    d = os.path.join(EXAMPLES, "budget_conflict")
    with tempfile.TemporaryDirectory() as out:
        try:
            run_plan(os.path.join(d, "samples.csv"),
                     os.path.join(d, "candidates.csv"),
                     os.path.join(d, "stations.csv"), 10, 30,
                     required=["Rq"], output_dir=out)
        except PlanningError as exc:
            assert exc.budget_conflict
            assert any(e["kind"] == "budget_conflict" for e in exc.errors)
        else:
            raise AssertionError("budget conflict was silently ignored")
        assert os.listdir(out) == []  # no "successful-looking" empty plan


def test_unknown_required_and_forbidden_keys_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        paths = _temp_tables(
            tmp, "station_id,lat,lon\n",
            "sample_id,lat,lon,weight\nA,0,0,5\n",
            "candidate_id,lat,lon,cost\nX,0,0.01,5\n")
        for option, kind in (((["Nope"], []), "unknown_required_candidate"),
                             (([], ["Nope"]), "unknown_forbidden_candidate")):
            try:
                load_plan_inputs(*paths, 10, 10, *option)
            except PlanningError as exc:
                assert any(e["kind"] == kind for e in exc.errors)
            else:
                raise AssertionError(f"{kind} was accepted")


# ---------------------------------------------------------------------------
# 8. Stable tie-break and input-order invariance
# ---------------------------------------------------------------------------


def test_perfect_tie_is_stable_and_order_invariant():
    stations = "station_id,lat,lon\n"
    samples = "sample_id,lat,lon,weight\nA,0,0,5\nB,0,0.1,5\n"
    candidates = ("candidate_id,lat,lon,cost\n"
                  "T1,0,0.02,4\nT2,0,0.08,4\n")
    r = _run(stations, samples, candidates, 4, 6)
    assert r.chosen == ["T1"]  # full tie on coverage and cost -> key order

    def reorder(text):
        lines = text.strip().split("\n")
        return "\n".join([lines[0]] + list(reversed(lines[1:]))) + "\n"

    # Reverse every table's data rows: same choice and same objectives.
    r2 = _run(reorder(stations), reorder(samples), reorder(candidates), 4, 6)
    assert r2.chosen == r.chosen
    assert r2.layer1_after == r.layer1_after
    assert r2.layer2_after == r.layer2_after
    assert r2.total_cost == r.total_cost

    # Two disjoint tied sites where either would be enough: the
    # first key wins, again regardless of row order.
    samples2 = "sample_id,lat,lon,weight\nA,0,0,5\nB,0,1,5\n"
    cands2 = ("candidate_id,lat,lon,cost\n"
              "Ka,0,0,4\nKb,0,1,4\n")
    r3 = _run(stations, samples2, cands2, 4, 6)
    assert r3.chosen == ["Ka"]
    r4 = _run(reorder(stations), reorder(samples2), reorder(cands2), 4, 6)
    assert r4.chosen == ["Ka"]


def test_full_run_output_is_row_order_invariant():
    """Reordering the input rows must not change the produced files
    (apart from the recorded output directory)."""
    d = os.path.join(EXAMPLES, "redundancy")

    def reversed_csv(path):
        with open(path, newline="", encoding="utf-8") as f:
            lines = f.read().strip().split("\n")
        return "\n".join([lines[0]] + list(reversed(lines[1:])))

    with tempfile.TemporaryDirectory() as tmp1, \
            tempfile.TemporaryDirectory() as tmp2:
        r1, p1 = run_plan(os.path.join(d, "samples.csv"),
                          os.path.join(d, "candidates.csv"),
                          os.path.join(d, "stations.csv"), 8, 30,
                          output_dir=tmp1)
        sd = os.path.join(tmp2, "samples.csv")
        cd = os.path.join(tmp2, "candidates.csv")
        st = os.path.join(tmp2, "stations.csv")
        _write(sd, reversed_csv(os.path.join(d, "samples.csv")) + "\n")
        _write(cd, reversed_csv(os.path.join(d, "candidates.csv")) + "\n")
        _write(st, reversed_csv(os.path.join(d, "stations.csv")) + "\n")
        r2, p2 = run_plan(sd, cd, st, 8, 30, output_dir=tmp2 + "/out")
        assert r1.chosen == r2.chosen
        for name in ("purchase_recommendation.csv", "sample_coverage.csv",
                     "unselected_improvements.csv"):
            with open(os.path.join(tmp1, name)) as f1, \
                    open(os.path.join(tmp2, "out", name)) as f2:
                assert f1.read() == f2.read(), name
        # plan_validation.json records the input file paths; compare the
        # substantive content only.
        v1 = json.load(open(os.path.join(tmp1, "plan_validation.json")))
        v2 = json.load(open(os.path.join(tmp2, "out", "plan_validation.json")))
        v1.pop("source_files"), v2.pop("source_files")
        assert v1 == v2
        s1 = json.load(open(p1["summary"]))
        s2 = json.load(open(p2["summary"]))
        s1.pop("output_dir")
        s2.pop("output_dir")
        assert s1 == s2


# ---------------------------------------------------------------------------
# 9. Input validation
# ---------------------------------------------------------------------------


def _expect_errors(stations, samples, candidates, budget, radius,
                   required=None, forbidden=None):
    with tempfile.TemporaryDirectory() as tmp:
        paths = _temp_tables(tmp, stations, samples, candidates)
        try:
            load_plan_inputs(*paths, budget, radius, required, forbidden)
        except PlanningError as exc:
            return exc.errors
        raise AssertionError("invalid input was accepted")


def test_invalid_records_are_reported_before_solving():
    good_samples = "sample_id,lat,lon,weight\nA,0,0,5\n"
    good_cands = "candidate_id,lat,lon,cost\nC,0,0.01,5\n"
    good_stations = "station_id,lat,lon\n"

    cases = [
        ("lat out of range",
         _expect_errors(good_stations,
                        "sample_id,lat,lon,weight\nA,91,0,5\n",
                        good_cands, 10, 10), "out_of_range"),
        ("lon not finite",
         _expect_errors(good_stations,
                        "sample_id,lat,lon,weight\nA,0,inf,5\n",
                        good_cands, 10, 10), "non_finite"),
        ("lat not numeric",
         _expect_errors(good_stations,
                        "sample_id,lat,lon,weight\nA,x,0,5\n",
                        good_cands, 10, 10), "non_numeric"),
        ("zero sample weight",
         _expect_errors(good_stations,
                        "sample_id,lat,lon,weight\nA,0,0,0\n",
                        good_cands, 10, 10), "out_of_range"),
        ("negative candidate cost",
         _expect_errors(good_stations, good_samples,
                        "candidate_id,lat,lon,cost\nC,0,0.01,-5\n",
                        10, 10), "out_of_range"),
        ("negative budget",
         _expect_errors(good_stations, good_samples, good_cands, -1, 10),
         "out_of_range"),
        ("non-finite budget",
         _expect_errors(good_stations, good_samples, good_cands, "nan", 10),
         "non_finite"),
        ("zero radius",
         _expect_errors(good_stations, good_samples, good_cands, 10, 0),
         "out_of_range"),
        ("duplicate sample key",
         _expect_errors(good_stations,
                        "sample_id,lat,lon,weight\nA,0,0,5\nA,0,0.1,5\n",
                        good_cands, 10, 10), "duplicate_key"),
        ("duplicate candidate coordinates",
         _expect_errors(good_stations, good_samples,
                        "candidate_id,lat,lon,cost\nC1,0,0.01,5\nC2,0,0.01,5\n",
                        20, 10), "duplicate_coordinates"),
    ]
    for label, errors, kind in cases:
        assert any(e["kind"] == kind for e in errors), (label, errors)
    # The message carries the exact record (line and key).
    errors = _expect_errors(good_stations,
                            "sample_id,lat,lon,weight\n"
                            "A,0,0,5\n"
                            "Bad,91,0,5\n",
                            good_cands, 10, 10)
    bad = [e for e in errors if e["kind"] == "out_of_range"]
    assert bad and bad[0]["line"] == 3 and bad[0]["key"] == "Bad"
    # An empty candidate table or empty sample table is refused, not "optimised".
    errors = _expect_errors(good_stations, good_samples,
                            "candidate_id,lat,lon,cost\n", 10, 10)
    assert any(e["kind"] == "empty_input" for e in errors)
    errors = _expect_errors(good_stations,
                            "sample_id,lat,lon,weight\n",
                            good_cands, 10, 10)
    assert any(e["kind"] == "empty_input" for e in errors)


def test_validation_failure_writes_no_files():
    with tempfile.TemporaryDirectory() as out:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _temp_tables(
                tmp, "station_id,lat,lon\n",
                "sample_id,lat,lon,weight\nA,0,0,5\n",
                "candidate_id,lat,lon,cost\nC,0,0.01,5\n")
            try:
                run_plan(*paths, -1, 10, output_dir=out)
            except (PlanningError, TypeError):
                pass
        assert os.listdir(out) == []


# ---------------------------------------------------------------------------
# 10. Checked-in demo outputs
# ---------------------------------------------------------------------------


def test_large_instance_matches_component_exhaustive():
    """The large, cluster-structured demo must match the independent
    component-exhaustive verifier (run as a subprocess so it cannot share
    code with the production solver)."""
    import subprocess
    import verify_plan  # from scripts/planning (on sys.path above)
    d = os.path.join(DATA, "examples", "large")
    out = os.path.join(ROOT, "outputs", "planning_large")
    if not os.path.exists(os.path.join(out, "plan_summary.json")):
        return  # generated by scripts/planning/run_planning_demo.py
    samples, candidates, stations = verify_plan.load(
        os.path.join(d, "samples.csv"),
        os.path.join(d, "candidates.csv"),
        os.path.join(d, "stations.csv"))
    best = verify_plan.component_exhaustive(
        samples, stations, candidates, Decimal("240"), 25.0,
        ["B10200"], ["B40203"])
    summary = json.load(open(os.path.join(out, "plan_summary.json")))
    chosen = set(summary["purchase"]["chosen_candidates"])
    assert chosen == set(best[3]), (sorted(chosen), sorted(best[3]))
    assert Decimal(summary["purchase"]["cost_total"]) == best[2]
    assert Decimal(summary["coverage_summary"][
        "weight_with_at_least_one_source"]["after"]) == best[0]
    assert Decimal(summary["coverage_summary"][
        "weight_with_at_least_two_sources"]["after"]) == best[1]


def test_demo_files_exist_and_are_consistent():
    out = os.path.join(ROOT, "outputs", "planning_demo")
    for name in ("plan_summary.json", "purchase_recommendation.csv",
                 "sample_coverage.csv", "unselected_improvements.csv",
                 "plan_validation.json"):
        assert os.path.exists(os.path.join(out, name)), name
    summary = json.load(open(os.path.join(out, "plan_summary.json")))
    assert summary["verdict"] == "ok"
    chosen = summary["purchase"]["chosen_candidates"]
    assert summary["purchase"]["cost_total"] == "18"
    assert summary["coverage_summary"]["weight_with_at_least_one_source"][
        "after"] == "53"
    assert summary["coverage_summary"]["weight_with_at_least_two_sources"][
        "after"] == "4"
    rows = _read_csv(os.path.join(out, "sample_coverage.csv"))
    assert {r["sample_id"] for r in rows} == {
        "A", "D", "B", "G", "H", "H2", "L1", "L2", "L3", "T1", "T2", "UF"}
    for row in rows:
        after = json.loads(row["sources_after_json"])
        before = json.loads(row["sources_before_json"])
        assert int(row["sources_before"]) == sum(
            1 for s in before if s["in_radius"])
        assert int(row["sources_after"]) == sum(
            1 for s in after if s["in_radius"])
    purchase = _read_csv(os.path.join(out, "purchase_recommendation.csv"))
    decisions = {r["candidate_id"]: r["decision"] for r in purchase}
    assert decisions["R1"] == "chosen_required"
    assert decisions["F"] == "forbidden"
    assert decisions["G3"] == "chosen_optional"
