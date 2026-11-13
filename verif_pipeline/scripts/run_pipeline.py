#!/usr/bin/env python3
"""Run the production verification pipeline.

Usage: run_pipeline.py [config.json]
Default config: configs/demo_analysis.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.runner import run_analysis


def main():
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "configs", "demo_analysis.json",
    )
    config_path = sys.argv[1] if len(sys.argv) > 1 else default
    outdir = run_analysis(config_path)
    print(f"analysis snapshot written to: {outdir}")
    for name in sorted(os.listdir(outdir)):
        print(f"  {name}")


if __name__ == "__main__":
    main()
