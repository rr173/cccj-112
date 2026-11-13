#!/usr/bin/env python3
"""Independent exhaustive checker for the offline gauge-planning optimiser.

This script shares no optimisation code with ``pipeline.planning``.  It
reads the same three tables, recomputes the Haversine distances with its
own formula, then enumerates *every* subset of candidates (2^n), filters
by budget and required/forbidden constraints, and compares the resulting
four-layer objective with the recommendation written by the production
command.

Usage
-----
    python3 scripts/planning/verify_plan.py --config configs/planning/demo_plan.json
    python3 scripts/planning/verify_plan.py --samples S.csv --candidates C.csv \
        --stations ST.csv --budget 18 --radius-km 50 --required R1 --forbidden F \
        --summary outputs/planning_demo/plan_summary.json

With ``--summary`` omitted, only the exhaustive optimum is computed and
printed (useful for small instances).  With ``--summary`` given, the
recommendation in that file must match the exhaustive optimum exactly;
otherwise the script prints the mismatch and exits non-zero.

Exit codes: 0 match (or no summary requested); 1 mismatch; 2 bad input.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import sys
from decimal import Decimal, InvalidOperation

EARTH_RADIUS_KM = 6371.0


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def read_table(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_table_any(path, expected_cols):
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for value in raw.values():
                if isinstance(value, list):
                    raw = value
                    break
        rows = [{k: r.get(k) for k in expected_cols} for r in raw]
        return rows
    return read_table(path)


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def load(samples_path, candidates_path, stations_path):
    samples = [
        {"key": r["sample_id"].strip(), "lat": float(r["lat"]),
         "lon": float(r["lon"]), "weight": Decimal(str(r["weight"]).strip())}
        for r in read_table_any(samples_path, ("sample_id", "lat", "lon", "weight"))
    ]
    candidates = [
        {"key": r["candidate_id"].strip(), "lat": float(r["lat"]),
         "lon": float(r["lon"]), "cost": Decimal(str(r["cost"]).strip()),
         "required": truthy(r.get("required")),
         "forbidden": truthy(r.get("forbidden"))}
        for r in read_table_any(candidates_path,
                                ("candidate_id", "lat", "lon", "cost",
                                 "required", "forbidden"))
    ]
    stations = []
    if stations_path:
        stations = [
            {"key": r["station_id"].strip(), "lat": float(r["lat"]),
             "lon": float(r["lon"])}
            for r in read_table_any(stations_path, ("station_id", "lat", "lon"))
        ]
    return sorted(samples, key=lambda x: x["key"]), \
        sorted(candidates, key=lambda x: x["key"]), \
            sorted(stations, key=lambda x: x["key"])


def rank_plan(values, chosen):
    """Four-layer rank as a Python tuple; larger is better."""
    return (values[0], values[1], -values[2], tuple(chosen))


def better(a, b):
    """True if plan a strictly beats plan b.

    Coverage weight first, redundant weight second, cost third (lower
    wins), then the stable key rule: walking keys in ascending order, a
    plan that includes the first differing key beats one that does not.
    """
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    if a[2] != b[2]:
        return a[2] < b[2]
    for key in sorted(set(a[3]) | set(b[3])):
        if (key in a[3]) != (key in b[3]):
            return key in a[3]
    return False


def evaluate(samples, stations, candidates, chosen, radius):
    counts = [0] * len(samples)
    for station in stations:
        for i, sample in enumerate(samples):
            if haversine(station["lat"], station["lon"],
                         sample["lat"], sample["lon"]) <= radius:
                counts[i] += 1
    for cand in candidates:
        if cand["key"] not in chosen:
            continue
        for i, sample in enumerate(samples):
            if haversine(cand["lat"], cand["lon"],
                         sample["lat"], sample["lon"]) <= radius:
                counts[i] += 1
    weight_once = sum((s["weight"] for s, c in zip(samples, counts) if c >= 1),
                      start=Decimal(0))
    weight_twice = sum((s["weight"] for s, c in zip(samples, counts) if c >= 2),
                       start=Decimal(0))
    cost = sum((c["cost"] for c in candidates if c["key"] in chosen),
               start=Decimal(0))
    return weight_once, weight_twice, cost, counts


def exhaustive(samples, stations, candidates, budget, radius, required, forbidden):
    n = len(candidates)
    required_set = set(required) | {c["key"] for c in candidates if c["required"]}
    forbidden_set = set(forbidden) | {c["key"] for c in candidates if c["forbidden"]}
    best = None
    best_counts = None
    n_feasible = 0
    for size in range(n + 1):
        for combo in itertools.combinations(range(n), size):
            chosen = {candidates[i]["key"] for i in combo}
            if not required_set <= chosen:
                continue
            if chosen & forbidden_set:
                continue
            cost = sum((candidates[k]["cost"] for k in combo), start=Decimal(0))
            if cost > budget:
                continue
            n_feasible += 1
            value = evaluate(samples, stations, candidates, chosen, radius)
            candidate_rank = (value[0], value[1], value[2], chosen)
            if best is None or better(candidate_rank, best):
                best = candidate_rank
                best_counts = value[3]
    return best, best_counts, n_feasible


def components_of(samples, stations, candidates, radius, required,
                  forbidden):
    """Split candidates into components by shared served samples.

    Independent implementation (no import from pipeline.planning): two
    candidates are in the same component when some sample is within the
    radius of both.  Components therefore serve disjoint sample sets.
    """
    served = {}
    for cand in candidates:
        served[cand["key"]] = {
            i for i, s in enumerate(samples)
            if haversine(cand["lat"], cand["lon"], s["lat"], s["lon"]) <= radius
        }
    keys = [c["key"] for c in candidates]
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a in keys:
        for b in keys:
            if a < b and served[a] & served[b]:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    groups = {}
    for c in candidates:
        groups.setdefault(find(c["key"]), []).append(c)
    out = [sorted(g, key=lambda c: c["key"]) for g in groups.values()]
    out.sort(key=lambda g: g[0]["key"])
    return out, served


def component_exhaustive(samples, stations, candidates, budget, radius,
                         required, forbidden):
    """Exhaustive across each component (2^m), exact combine by cost.

    Components serve disjoint samples, so a component's contribution can
    be evaluated standalone and summed.  Within a component every subset
    is enumerated; per purchase cost only the best (covered weight,
    redundant weight, key bits) is kept.  A knapsack over the component
    cost lists then yields the global four-layer optimum.
    """
    required_set = set(required) | {c["key"] for c in candidates if c["required"]}
    forbidden_set = set(forbidden) | {c["key"] for c in candidates if c["forbidden"]}
    base_counts = [0] * len(samples)
    for st in stations:
        for i, s in enumerate(samples):
            if haversine(st["lat"], st["lon"], s["lat"], s["lon"]) <= radius:
                base_counts[i] += 1
    comps, _ = components_of(samples, stations, candidates, radius,
                             required, forbidden)
    # Tie-break as a bit vector over all candidates in ascending key order;
    # a larger integer means "at the first differing key this plan includes
    # it".  Integer comparison is preserved when identical bits from later
    # components are OR-ed in, because the first differing key stays put.
    all_keys = sorted(c["key"] for c in candidates)
    bit = {key: 1 << (len(all_keys) - 1 - i)
           for i, key in enumerate(all_keys)}
    plans_by_comp = []
    for comp in comps:
        in_comp = sorted({i for c in comp for i, s in enumerate(samples)
                          if haversine(c["lat"], c["lon"], s["lat"],
                                       s["lon"]) <= radius})
        served = {
            c["key"]: [i for i in in_comp
                       if haversine(c["lat"], c["lon"], samples[i]["lat"],
                                    samples[i]["lon"]) <= radius]
            for c in comp
        }
        keys = [c["key"] for c in comp]
        forced = {k for k in keys if k in required_set}
        banned = {k for k in keys if k in forbidden_set}
        best_by_cost = {}
        n = len(comp)
        for size in range(n + 1):
            for combo in itertools.combinations(range(n), size):
                chosen = {keys[i] for i in combo}
                if not forced <= chosen or chosen & banned:
                    continue
                cost = sum((comp[i]["cost"] for i in combo), start=Decimal(0))
                if cost > budget:
                    continue
                counts = dict.fromkeys(in_comp, 0)
                for i in combo:
                    for si in served[keys[i]]:
                        counts[si] += 1
                covered = sum((samples[i]["weight"] for i in in_comp
                               if base_counts[i] + counts[i] >= 1),
                              start=Decimal(0))
                redundant = sum((samples[i]["weight"] for i in in_comp
                                 if base_counts[i] + counts[i] >= 2),
                                start=Decimal(0))
                mask = 0
                for k in chosen:
                    mask |= bit[k]
                entry = (covered, redundant, mask)
                cur = best_by_cost.get(cost)
                if cur is None or _entry_better(entry, cur):
                    best_by_cost[cost] = entry
        plans_by_comp.append(best_by_cost)
    states = {Decimal(0): (Decimal(0), Decimal(0), 0)}
    for plans in plans_by_comp:
        nxt = {}
        for total, (cov, red, mask) in states.items():
            for cost, (c_cov, c_red, c_mask) in plans.items():
                nc = total + cost
                if nc > budget:
                    continue
                cand = (cov + c_cov, red + c_red, mask | c_mask)
                cur = nxt.get(nc)
                if cur is None or _entry_better(cand, cur):
                    nxt[nc] = cand
        states = nxt
    best = None
    for cost, (cov, red, mask) in states.items():
        rank = (cov, red, cost, mask)
        if best is None or _final_rank_better(rank, best):
            best = rank
    chosen = [k for k in all_keys if best[3] & bit[k]]
    return (best[0], best[1], best[2], tuple(chosen))


def _final_rank_better(a, b):
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    if a[2] != b[2]:
        return a[2] < b[2]
    return a[3] > b[3]


def _entry_better(a, b):
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    return a[2] > b[2]


def fmt(value):
    if value is None:
        return ""
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def compare(summary_path, best, best_counts):
    with open(summary_path, encoding="utf-8") as f:
        summary = json.load(f)
    errors = []
    chosen = set(summary.get("purchase", {}).get("chosen_candidates", []))
    want_chosen = set(best[3])
    if chosen != want_chosen:
        errors.append(f"chosen candidates {sorted(chosen)} != exhaustive "
                      f"{sorted(want_chosen)}")
    got_cost = Decimal(clean(summary["purchase"]["cost_total"]))
    if got_cost != best[2]:
        errors.append(f"cost {fmt(got_cost)} != exhaustive {fmt(best[2])}")
    got_once = Decimal(clean(summary["coverage_summary"][
        "weight_with_at_least_one_source"]["after"]))
    if got_once != best[0]:
        errors.append(f"weight >=1 {fmt(got_once)} != exhaustive {fmt(best[0])}")
    got_twice = Decimal(clean(summary["coverage_summary"][
        "weight_with_at_least_two_sources"]["after"]))
    if got_twice != best[1]:
        errors.append(f"weight >=2 {fmt(got_twice)} != exhaustive {fmt(best[1])}")
    return errors, want_chosen, best_counts


def clean(text):
    return str(text).strip().rstrip("%") or "0"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config")
    ap.add_argument("--samples")
    ap.add_argument("--candidates")
    ap.add_argument("--stations")
    ap.add_argument("--budget")
    ap.add_argument("--radius-km", dest="radius_km")
    ap.add_argument("--required", action="append", default=[])
    ap.add_argument("--forbidden", action="append", default=[])
    ap.add_argument("--component-exhaustive", action="store_true",
                    help="enumerate each coverage component exhaustively "
                         "(for large, cluster-structured instances)")
    ap.add_argument("--summary", help="plan_summary.json to check")
    ap.add_argument("--coverage", help="sample_coverage.csv to check")
    args = ap.parse_args(argv)

    cfg = {}
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        base = os.path.dirname(os.path.abspath(args.config))
        for key in ("samples", "candidates", "existing_stations"):
            if cfg.get(key):
                cfg[key] = os.path.normpath(os.path.join(base, cfg[key]))
    samples_path = args.samples or cfg.get("samples")
    candidates_path = args.candidates or cfg.get("candidates")
    stations_path = args.stations or cfg.get("existing_stations")
    budget_text = args.budget if args.budget is not None else cfg.get("budget")
    radius_text = args.radius_km if args.radius_km is not None else cfg.get("radius_km")
    required = args.required or list(cfg.get("required_candidates", []))
    forbidden = args.forbidden or list(cfg.get("forbidden_candidates", []))
    if not (samples_path and candidates_path and budget_text is not None
            and radius_text is not None):
        print("error: need --samples, --candidates, --budget and --radius-km "
              "(or a config supplying them)", file=sys.stderr)
        return 2

    samples, candidates, stations = load(samples_path, candidates_path,
                                         stations_path)
    budget = Decimal(str(budget_text).strip())
    radius = float(radius_text)
    try:
        if args.component_exhaustive:
            best = component_exhaustive(
                samples, stations, candidates, budget, radius,
                [k.strip() for k in required], [k.strip() for k in forbidden])
            best_counts = None
            n_feasible = "component-wise"
        else:
            best, best_counts, n_feasible = exhaustive(
                samples, stations, candidates, budget, radius,
                [k.strip() for k in required], [k.strip() for k in forbidden])
    except (InvalidOperation, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if best is None:
        # No feasible subset: report it explicitly instead of a fake optimum.
        print("NO FEASIBLE SUBSET: required candidates cannot be bought within "
              "the budget", file=sys.stderr)
        return 1
    print(f"exhaustive optimum: chosen={sorted(best[3])} cost={fmt(best[2])} "
          f"weight>=1={fmt(best[0])} weight>=2={fmt(best[1])} "
          f"feasible_subsets={n_feasible}")

    if not args.summary:
        return 0
    errors, want, counts = compare(args.summary, best, best_counts)
    if args.coverage:
        errors += check_coverage(args.coverage, samples, stations, candidates,
                                 want, radius, counts)
    if errors:
        for err in errors:
            print(f"MISMATCH: {err}", file=sys.stderr)
        return 1
    print("OK: recommendation matches the exhaustive optimum")
    return 0


def check_coverage(path, samples, stations, candidates, chosen, radius, counts):
    """Cross-check each sample row's source counts against a direct recount."""
    errors = []
    with open(path, newline="", encoding="utf-8") as f:
        rows = {r["sample_id"]: r for r in csv.DictReader(f)}
    cand_by_key = {c["key"]: c for c in candidates}
    for i, sample in enumerate(samples):
        row = rows.get(sample["key"])
        if row is None:
            errors.append(f"coverage row missing for sample {sample['key']}")
            continue
        before = sum(1 for st in stations
                     if haversine(st["lat"], st["lon"], sample["lat"],
                                  sample["lon"]) <= radius)
        after = before + sum(1 for key in chosen
                             if haversine(cand_by_key[key]["lat"],
                                          cand_by_key[key]["lon"],
                                          sample["lat"], sample["lon"]) <= radius)
        if int(row["sources_before"]) != before:
            errors.append(f"{sample['key']}: sources_before "
                          f"{row['sources_before']} != {before}")
        if int(row["sources_after"]) != after:
            errors.append(f"{sample['key']}: sources_after "
                          f"{row['sources_after']} != {after}")
        if after != counts[i]:
            errors.append(f"{sample['key']}: exhaustive count {counts[i]} != {after}")
        # Audit every source's distance/in_radius claim in the after JSON.
        for src in json.loads(row["sources_after_json"]):
            if src["kind"] == "new" and src["source_id"] not in chosen:
                errors.append(f"{sample['key']}: new source {src['source_id']} "
                              "is not in the chosen set")
            if src["kind"] == "existing":
                match = next((st for st in stations
                              if st["key"] == src["source_id"]), None)
            else:
                match = next((c for c in candidates
                              if c["key"] == src["source_id"]), None)
            if match is None:
                errors.append(f"{sample['key']}: unknown source {src['source_id']}")
                continue
            d = haversine(match["lat"], match["lon"], sample["lat"], sample["lon"])
            if abs(d - float(src["distance_km"])) > 1e-6:
                errors.append(f"{sample['key']}: distance to {src['source_id']} "
                              f"{src['distance_km']} != {d}")
            if bool(src["in_radius"]) != (d <= radius):
                errors.append(f"{sample['key']}: in_radius for "
                              f"{src['source_id']} disagrees with distance {d}")
    return errors


if __name__ == "__main__":
    sys.exit(main())
