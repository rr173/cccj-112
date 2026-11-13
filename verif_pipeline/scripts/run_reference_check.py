#!/usr/bin/env python3
"""Cross-check a production output directory with the independent
NumPy reference implementation.

Usage: run_reference_check.py [config.json] [output_dir]
Defaults: configs/demo_analysis.json and the newest output snapshot.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reference.reference_impl import run_check


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "configs", "demo_analysis.json")
    if len(sys.argv) > 2:
        outdir = sys.argv[2]
    else:
        import json
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        out_root = cfg["output_root"]
        if not os.path.isabs(out_root):
            out_root = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(config_path)), out_root))
        candidates = sorted(
            os.path.join(out_root, d) for d in os.listdir(out_root)
            if d.startswith(cfg["name"] + "_")
        )
        if not candidates:
            sys.exit("no output snapshot found; run scripts/run_pipeline.py first")
        outdir = candidates[-1]
    mismatches = run_check(config_path, outdir)
    sys.exit(1 if mismatches else 0)


if __name__ == "__main__":
    main()
