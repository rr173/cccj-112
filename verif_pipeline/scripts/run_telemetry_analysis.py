#!/usr/bin/env python3
"""Full local telemetry -> analysis run (one command).

1. start the telemetry server (fresh runtime database)
2. feed it a simulated gateway covering 2026-02-01 00-06 UTC for the
   four stations in data/obs/stations.csv
3. export accepted batches to data/obs/station_obs_telemetry.csv
   (version 1, issued 2026-02-02T00:00:00Z)
4. run the existing verification pipeline on that CSV
5. cross-check the snapshot with the independent reference implementation

The export replaces (never appends to) the observation file, so the
run is repeatable.  Prints every relevant file path.
"""
import csv
import os
import shutil
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from telemetry.export import export_observations
from telemetry.gateway import GatewayClient, build_batch, seal_message
from telemetry.server import serve


def utc(hh, mm=0, sec=0):
    return int(datetime(2026, 2, 1, hh, mm, sec,
                        tzinfo=timezone.utc).timestamp() * 1000)


def main():
    runtime = os.path.join(ROOT, "runtime", "analysis")
    if os.path.isdir(runtime):
        shutil.rmtree(runtime)
    os.makedirs(runtime)
    db = os.path.join(runtime, "telemetry.sqlite3")
    stations = os.path.join(ROOT, "data", "obs", "stations.csv")

    srv, thread, port = serve(db, stations, port=0,
                              journal_path=os.path.join(runtime, "journal.jsonl"))
    try:
        client = GatewayClient("127.0.0.1", port)
        batches = [
            # S1: one 6 h batch with pulses spread over both buckets
            ("S1", "s1-0006", utc(0), utc(6), [
                (0, utc(0, 20), 3), (1, utc(1, 10), 2), (2, utc(2, 40), 1),
                (3, utc(3, 5), 5), (4, utc(4, 30), 4)]),
            # S2: two adjacent batches (coverage proved jointly)
            ("S2", "s2-0003", utc(0), utc(3), [(0, utc(1, 15), 2)]),
            ("S2", "s2-0306", utc(3), utc(6), [(0, utc(4, 15), 3)]),
            # S3: fully covered, no rain at all
            ("S3", "s3-0006", utc(0), utc(6), []),
            # S4: only the first bucket covered; 03-06 stays a gap
            ("S4", "s4-0003", utc(0), utc(3), [(0, utc(0, 45), 1)]),
        ]
        for station, bid, t0, t1, tips in batches:
            begin, tip_msgs = build_batch(station, bid, t0, t1, tips,
                                          tip_mm="0.2")
            client.send_message(begin)
            for m in tip_msgs:
                client.send_message(m)
            reply = client.send_message(seal_message(station, bid))
            assert reply["ack"], reply
        client.close()
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()
        srv.state.store.close()
        srv.state.close()

    obs_path = os.path.join(ROOT, "data", "obs", "station_obs_telemetry.csv")
    result = export_observations(db, stations, "2026-02-01T00:00:00Z",
                                 "2026-02-01T06:00:00Z",
                                 "2026-02-02T00:00:00Z", obs_path)
    print(f"export -> {obs_path} ({len(result['obs_rows'])} rows)")
    for row in result["obs_rows"]:
        print(f"  {row['station_id']} {row['valid_time']} {row['precip_mm']} mm")
    for gap in result["gap_rows"]:
        print(f"  GAP {gap['station_id']} {gap['missing_start']} - "
              f"{gap['missing_end']} ({gap['reason']})")

    import json
    with open(os.path.join(ROOT, "configs", "demo_analysis.json"),
              encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["name"] = "telemetry"
    cfg["obs_path"] = obs_path
    cfg["manifest_path"] = os.path.join(ROOT, cfg["manifest_path"].lstrip("./"))
    cfg["stations_path"] = os.path.join(ROOT, cfg["stations_path"].lstrip("./"))
    cfg["output_root"] = os.path.join(ROOT, "outputs")
    cfg_path = os.path.join(runtime, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    from pipeline.runner import run_analysis
    outdir = run_analysis(cfg_path)
    print(f"analysis -> {outdir}")

    from reference.reference_impl import run_check
    mismatches = run_check(cfg_path, outdir)
    if mismatches:
        print("REFERENCE CHECK FAILED")
        for m in mismatches:
            print(f"  {m}")
        return 1
    print("reference check: 0 mismatches")

    with open(os.path.join(outdir, "station_probs.csv"), newline="",
              encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    used = sorted({(r["station_id"], r["valid_time"], r["obs_interval_mm"],
                    r["obs_version"]) for r in rows if r["obs_interval_mm"]})
    print(f"analysis consumed {len(used)} (station, valid_time) obs cells:")
    for entry in used:
        print(f"  {entry[0]} {entry[1]} obs={entry[2]} mm v{entry[3]}")
    with open(os.path.join(outdir, "snapshot.json"), encoding="utf-8") as f:
        snap = json.load(f)
    print(f"snapshot {snap['snapshot_id']}: "
          f"{snap['frozen']['obs_rows_used']} obs rows used, "
          f"{snap['frozen']['obs_rows_excluded']} excluded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
