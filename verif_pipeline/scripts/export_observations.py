#!/usr/bin/env python3
"""Export accepted gauge batches to the pipeline's observation CSV.

Usage:
    python3 scripts/export_observations.py --db runtime/telemetry.sqlite3 \\
        --stations data/obs/stations.csv \\
        --start 2026-02-01T00:00:00Z --end 2026-02-02T00:00:00Z \\
        --issued-time 2026-02-03T00:00:00Z \\
        --output data/obs/station_obs_telemetry.csv

``--start`` / ``--end`` must be UTC 3-hour boundaries.  A bucket is
written only when accepted batch coverage is complete over the whole
3 hours; gaps are reported in ``<output>.gaps.csv`` (or ``--gaps``).
The output file is rewritten in full on every run.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.export import export_observations


def main(argv=None):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.path.join(root, "runtime", "telemetry.sqlite3"))
    ap.add_argument("--stations", default=os.path.join(root, "data", "obs", "stations.csv"))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--issued-time", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--gaps", default=None)
    ap.add_argument("--summary-json", default=None,
                    help="also write the full export summary (incl. per-bucket "
                         "status and gaps) here")
    args = ap.parse_args(argv)

    result = export_observations(
        args.db, args.stations, args.start, args.end, args.issued_time,
        args.output, gaps_path=args.gaps)
    print(f"exported {len(result['obs_rows'])} observation rows -> {args.output}")
    print(f"reported {len(result['gap_rows'])} gap rows -> {result['gaps_path']}")
    if args.summary_json:
        with open(args.summary_json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, sort_keys=True)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
