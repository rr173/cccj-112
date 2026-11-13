#!/usr/bin/env python3
"""One-command reproduction of the offline gauge-planning demo.

Runs the planning command for every checked-in example (fresh inputs,
overwriting the same output directories), then cross-checks each result
with the independent exhaustive verifier.  It does not touch forecast
scoring, telemetry reception or observation data.

    python3 scripts/planning/run_planning_demo.py

Prints, for the main demo, the exact commands and file paths needed to
inspect the per-sample reasoning.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def _run(args):
    print("$ " + " ".join(args))
    completed = subprocess.run(args, cwd=ROOT, text=True,
                               capture_output=True)
    sys.stdout.write(completed.stdout)
    if completed.returncode not in (0, 3):
        sys.stderr.write(completed.stderr)
    return completed.returncode


def main():
    py = sys.executable
    plan = os.path.join(ROOT, "scripts", "planning", "run_gauge_plan.py")
    verify = os.path.join(ROOT, "scripts", "planning", "verify_plan.py")

    demos = [
        # (config, verify args, expected exit)
        (os.path.join(ROOT, "configs", "planning", "demo_plan.json"), [], 0),
        (os.path.join(ROOT, "configs", "planning", "json_plan.json"), [], 0),
        (os.path.join(ROOT, "configs", "planning", "large_plan.json"),
         ["--component-exhaustive"], 0),
    ]
    for config, extra, expected in demos:
        print("=" * 72)
        rc = _run([py, plan, "--config", config, "--verbose"])
        if rc != expected:
            print(f"FAILED: planner exited {rc}")
            return 1
        cfg = json.load(open(config, encoding="utf-8"))
        outdir = cfg["output_dir"]
        if not os.path.isabs(outdir):
            outdir = os.path.normpath(
                os.path.join(os.path.dirname(config), outdir))
        rc = _run([py, verify, "--config", config,
                   "--summary", os.path.join(outdir, "plan_summary.json")] + extra)
        if rc != 0:
            print(f"FAILED: verifier exited {rc}")
            return 1

    # Small feature examples, each demonstrating one declared behaviour.
    examples = [
        ("greedy_trap", "12", "30", [], "greedy would pick 'Guide' (weight 6); "
                                     "the optimiser picks 'Hero' (weight 10)"),
        ("redundancy", "8", "30", [], "the high-value reach gets its second "
                                      "station before ordinary reaches"),
        ("old_station", "9", "30", [], "existing stations already cover A; "
                                       "money goes to the uncovered reach"),
        ("tie", "4", "6", [], "equal plans resolve to the smallest key"),
    ]
    print("=" * 72)
    for name, budget, radius, extra, note in examples:
        d = os.path.join(ROOT, "data", "planning", "examples", name)
        print(f"-- {name}: {note}")
        out = os.path.join(ROOT, "outputs", f"planning_example_{name}")
        rc = _run([py, plan,
                   "--samples", os.path.join(d, "samples.csv"),
                   "--candidates", os.path.join(d, "candidates.csv"),
                   "--stations", os.path.join(d, "stations.csv"),
                   "--budget", budget, "--radius-km", radius,
                   "--output-dir", out, "--verbose"])
        if rc != 0:
            return 1
        rc = _run([py, verify,
                   "--samples", os.path.join(d, "samples.csv"),
                   "--candidates", os.path.join(d, "candidates.csv"),
                   "--stations", os.path.join(d, "stations.csv"),
                   "--budget", budget, "--radius-km", radius,
                   "--summary", os.path.join(out, "plan_summary.json"),
                   "--coverage", os.path.join(out, "sample_coverage.csv")])
        if rc != 0:
            return 1

    # Conflict paths: these must fail loudly and write nothing.
    print("=" * 72)
    conflict = os.path.join(ROOT, "data", "planning", "examples",
                            "budget_conflict")
    out = os.path.join(ROOT, "outputs", "planning_should_not_exist")
    rc = _run([py, plan,
               "--samples", os.path.join(conflict, "samples.csv"),
               "--candidates", os.path.join(conflict, "candidates.csv"),
               "--stations", os.path.join(conflict, "stations.csv"),
               "--budget", "10", "--radius-km", "30",
               "--required", "Rq", "--output-dir", out])
    print(f"  required-over-budget exit code: {rc} "
          f"(3 = budget conflict), output written: {os.path.exists(out)}")
    if rc != 3 or os.path.exists(out):
        print("FAILED: budget conflict was not reported cleanly")
        return 1
    conflict = os.path.join(ROOT, "data", "planning", "examples", "conflict")
    out = os.path.join(ROOT, "outputs", "planning_should_not_exist_2")
    rc = _run([py, plan,
               "--samples", os.path.join(conflict, "samples.csv"),
               "--candidates", os.path.join(conflict, "candidates.csv"),
               "--stations", os.path.join(conflict, "stations.csv"),
               "--budget", "10", "--radius-km", "30"])
    print(f"  required-and-forbidden exit code: {rc} "
          f"(2 = invalid input), output written: {os.path.exists(out)}")
    if rc != 2 or os.path.exists(out):
        print("FAILED: required/forbidden conflict was not reported cleanly")
        return 1

    demo_out = os.path.join(ROOT, "outputs", "planning_demo")
    print("=" * 72)
    print("reproduce everything from fresh inputs:")
    print(f"  {py} scripts/planning/run_planning_demo.py")
    print("inspect the demo's per-sample reasoning:")
    print(f"  {py} scripts/planning/run_gauge_plan.py "
          f"--config configs/planning/demo_plan.json --verbose")
    print(f"  cat {os.path.join(demo_out, 'purchase_recommendation.csv')}")
    print(f"  cat {os.path.join(demo_out, 'sample_coverage.csv')}")
    print(f"  cat {os.path.join(demo_out, 'unselected_improvements.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
