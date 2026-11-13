#!/usr/bin/env python3
"""Offline rain-gauge network planning from CSV/JSON inputs.

Reads three tables (existing stations, candidate construction sites,
priority river-reach samples) plus a budget and a service radius, and
writes a purchase recommendation with a per-sample coverage explanation.

Every run is computed from fresh inputs and atomically overwrites its own
output files under ``--output-dir``.  It never touches forecast scores,
telemetry reception or observation data.

Examples
--------
Using the checked-in config (CSV inputs)::

    python3 scripts/planning/run_gauge_plan.py --config configs/planning/demo_plan.json

Explicit paths and options::

    python3 scripts/planning/run_gauge_plan.py \
        --samples data/planning/samples.csv \
        --candidates data/planning/candidates.csv \
        --stations data/planning/stations.csv \
        --budget 18 --radius-km 50 \
        --required R1 --forbidden F \
        --output-dir outputs/planning_demo

Exit codes
----------
0  recommendation written
2  input validation failed (the exact invalid records are printed; nothing
   is written)
3  reported budget conflict with required candidates (nothing is written)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from pipeline.planning import PLANNING_SPEC, PlanningError, run_plan  # noqa: E402


def _resolve(base, path):
    if path is None:
        return None
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    out = dict(raw)
    for key in ("samples", "candidates", "existing_stations", "output_dir"):
        if key in out and out[key] is not None:
            out[key] = _resolve(base, out[key])
    return out


def build_parser():
    ap = argparse.ArgumentParser(
        description="Select rain-gauge construction sites under a budget so that "
                    "priority river-reach samples have as many independent "
                    "sources as possible.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=PLANNING_SPEC["fields"]["candidates"]["meaning"],
    )
    ap.add_argument("--config", help="JSON file with the paths and options; "
                                     "explicit arguments override it")
    ap.add_argument("--samples", help="samples table (CSV or JSON)")
    ap.add_argument("--candidates", help="candidates table (CSV or JSON)")
    ap.add_argument("--stations", help="existing stations table (CSV or JSON); "
                                       "omit when there are none")
    ap.add_argument("--budget", help="budget upper bound (exact decimal)")
    ap.add_argument("--radius-km", dest="radius_km",
                    help="service radius in km (Haversine, inclusive)")
    ap.add_argument("--required", action="append", default=[],
                    help="candidate key that must be bought (repeatable)")
    ap.add_argument("--forbidden", action="append", default=[],
                    help="candidate key that must not be bought (repeatable)")
    ap.add_argument("--output-dir",
                    help="directory for the five planning files (created if needed)")
    ap.add_argument("--fields", action="store_true",
                    help="print the machine-readable field/objective spec and exit")
    ap.add_argument("--verbose", action="store_true",
                    help="print the chosen keys and objective values")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.fields:
        print(json.dumps(PLANNING_SPEC, indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    cfg = load_config(args.config) if args.config else {}
    samples = args.samples or cfg.get("samples")
    candidates = args.candidates or cfg.get("candidates")
    stations = args.stations or cfg.get("existing_stations")
    budget = args.budget if args.budget is not None else cfg.get("budget")
    radius_km = args.radius_km if args.radius_km is not None else cfg.get("radius_km")
    required = list(args.required) if args.required else list(cfg.get("required_candidates", []))
    forbidden = list(args.forbidden) if args.forbidden else list(
        cfg.get("forbidden_candidates", []))
    output_dir = args.output_dir or cfg.get("output_dir")

    missing = [name for name, value in (
        ("--samples", samples), ("--candidates", candidates),
        ("--budget", budget), ("--radius-km", radius_km),
        ("--output-dir", output_dir)) if value in (None, "")]
    if missing:
        print("error: missing required option(s): " + ", ".join(missing),
              file=sys.stderr)
        return 2

    try:
        result, paths = run_plan(
            samples, candidates, stations, budget, radius_km,
            required=required, forbidden=forbidden, output_dir=output_dir)
    except PlanningError as exc:
        print("input validation failed; no recommendation written:", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err['kind']}: {err['message']}", file=sys.stderr)
        return 3 if exc.budget_conflict else 2

    print(f"purchase recommendation written to: {output_dir}")
    for name in ("plan_summary.json", "purchase_recommendation.csv",
                 "sample_coverage.csv", "unselected_improvements.csv",
                 "plan_validation.json"):
        print(f"  {name}")
    if args.verbose:
        print(f"chosen: {result.chosen} (cost {result.total_cost})")
        print(f"weight >=1 source: {result.layer1_after} "
              f"(before {result.layer1_before})")
        print(f"weight >=2 sources: {result.layer2_after} "
              f"(before {result.layer2_before})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
