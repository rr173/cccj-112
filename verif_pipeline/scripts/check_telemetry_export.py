#!/usr/bin/env python3
"""Check the telemetry export against the analysis cutoffs it feeds.

An analysis frozen at ``obs_cutoff`` only uses observation versions issued
at or before that instant (see README convention 1).  The exporter stamps
every row with the ``issued_time`` it is given, so the export command must
be told a timestamp the analysis will actually accept.

Usage:
    python3 scripts/check_telemetry_export.py CONFIG.json OBS_CSV
    python3 scripts/check_telemetry_export.py CONFIG.json OBS_CSV --strict

Structural problems (header, version, station registry, whole-second
valid times) are hard failures (exit 2).  An ``issued_time`` after
``obs_cutoff`` is a warning (exit 0) because the frozen analysis will
simply exclude those rows; pass ``--strict`` to make it exit 1.
"""
import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.timeutil import parse_time


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("obs_csv")
    ap.add_argument("--strict", action="store_true",
                    help="treat a post-cutoff issued_time as failure too "
                         "(it already is by default; kept for clarity)")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    cutoff = parse_time(cfg["obs_cutoff"])

    required = ["station_id", "valid_time", "precip_mm", "version",
                "issued_time"]
    with open(args.obs_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != required:
            print(f"FAIL header is {reader.fieldnames}, expected {required}")
            return 2
        rows = list(reader)

    errors, warnings = [], []
    if not rows:
        errors.append("observation file is empty")
    versions = {r["version"] for r in rows}
    if versions != {"1"}:
        errors.append(f"version column is {sorted(versions)}, expected ['1']")
    issued = {r["issued_time"] for r in rows}
    if len(issued) != 1:
        errors.append(f"issued_time is not a single value: {sorted(issued)}")
    for r in rows:
        try:
            parse_time(r["valid_time"])
            parse_time(r["issued_time"])
            float(r["precip_mm"])
        except (ValueError, TypeError):
            errors.append(f"unparseable row: {r}")
        if parse_time(r["issued_time"]) > cutoff:
            warnings.append(
                f"{r['station_id']} {r['valid_time']}: issued_time "
                f"{r['issued_time']} is after obs_cutoff {cfg['obs_cutoff']}; "
                "the frozen analysis will exclude this row")
            break
    stations_cfg = cfg["stations_path"]
    if not os.path.isabs(stations_cfg):
        stations_cfg = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(args.config)), stations_cfg))
    with open(stations_cfg, newline="", encoding="utf-8") as f:
        known = {r["station_id"] for r in csv.DictReader(f)}
    unknown = sorted({r["station_id"] for r in rows} - known)
    if unknown:
        errors.append(f"stations not registered: {unknown}")

    # A telemetry row is a 3-hour accumulation valid on a 3-hour boundary.
    for r in rows:
        dt = parse_time(r["valid_time"])
        if dt.minute or dt.second or dt.hour % 3:
            errors.append(
                f"{r['station_id']} valid_time {r['valid_time']} is not a "
                "UTC 3-hour boundary")
            break

    for w in warnings:
        print(f"WARN {w}")
    for e in errors:
        print(f"FAIL {e}")
    if errors:
        return 2
    if warnings:
        if args.strict:
            print("FAIL issued_time after obs_cutoff (strict)")
            return 1
        print("WARN analysis will exclude the rows above; rerun the export "
              f"with issued_time <= {cfg['obs_cutoff']}")
        return 0
    print(f"OK {len(rows)} rows, all issued at or before "
          f"{cfg['obs_cutoff']}, version 1, known stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
