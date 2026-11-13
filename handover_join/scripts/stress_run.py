"""Run the join job as a child process and measure its peak RSS on Linux.

The job is deliberately executed in a *separate* process so that the measured
peak resident set size is the job's own footprint, not this harness's.  On
Linux ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` reports the child
peak in KiB.  The output file is then validated strictly in streaming order
without being loaded into memory:

* total line count equals the independently computed expected pair count;
* every line parses and carries left_row/right_row/left/right;
* ordering is strictly (left_row asc, right_row asc with null last);
* unmatched left rows have right == null and right_row == null.

Exit status is non-zero when the measured peak RSS exceeds the configured
limit, so the 96 MiB budget is enforced rather than merely observed.
"""

import argparse
import json
import os
import resource
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stream_validate(output_path, expected_pairs):
    import json as _json

    seen = 0
    prev_left = 0
    prev_right = 0
    unmatched = 0
    null_tail = 10**18
    with open(output_path, "rb") as fh:
        for line in fh:
            rec = _json.loads(line)
            lr = rec["left_row"]
            rr = rec["right_row"]
            assert isinstance(lr, int) and lr >= 1, lr
            assert set(rec) == {"left_row", "right_row", "left", "right"}, rec.keys()
            assert isinstance(rec["left"], dict)
            if rr is None:
                assert rec["right"] is None
                unmatched += 1
                rr_key = null_tail
            else:
                assert isinstance(rr, int) and rr >= 1
                assert isinstance(rec["right"], dict)
                rr_key = rr
            if lr != prev_left:
                assert lr > prev_left, "left_row must be non-decreasing"
                prev_right = 0
            assert rr_key > prev_right, "right_row must be increasing within group"
            prev_left = lr
            prev_right = rr_key
            seen += 1
    assert seen == expected_pairs, (seen, expected_pairs)
    return {"output_lines": seen, "unmatched_left_rows": unmatched}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--chunk-size", type=int, default=100000)
    parser.add_argument("--chunk-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--fan-in", type=int, default=4)
    parser.add_argument("--pair-chunk-size", type=int, default=100000)
    parser.add_argument("--pair-chunk-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--rss-limit-mib", type=int, default=96)
    parser.add_argument("--report", default=None)
    args = parser.parse_args(argv)

    with open(os.path.join(args.data_dir, "stress_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)

    os.makedirs(args.work_dir, exist_ok=True)
    cmd = [
        sys.executable,
        "-B",
        "-E",
        "-m",
        "handover_join.cli",
        "--left",
        meta["left_path"],
        "--right",
        meta["right_path"],
        "--config",
        meta["config_path"],
        "--output",
        args.output,
        "--work-dir",
        args.work_dir,
        "--chunk-size",
        str(args.chunk_size),
        "--chunk-bytes",
        str(args.chunk_bytes),
        "--pair-chunk-size",
        str(args.pair_chunk_size),
        "--pair-chunk-bytes",
        str(args.pair_chunk_bytes),
        "--fan-in",
        str(args.fan_in),
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")

    start = time.monotonic()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    wall_seconds = time.monotonic() - start
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"join subprocess failed with code {proc.returncode}")
    job_stats = json.loads(proc.stdout)

    # RUSAGE_CHILDREN reports the maximum RSS of reaped children (KiB, Linux).
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak_rss_kib = int(ru.ru_maxrss)
    peak_rss_mib = peak_rss_kib / 1024.0

    validation = stream_validate(args.output, meta["expected_pairs"])
    output_bytes = os.path.getsize(args.output)

    report = {
        "input_left_rows": meta["left_rows"],
        "input_right_rows": meta["right_rows"],
        "input_total_bytes": meta["total_input_bytes"],
        "input_total_mib": round(meta["total_input_bytes"] / 1048576, 2),
        "hot_left_rows": meta["hot_left_rows"],
        "hot_right_rows": meta["hot_right_rows"],
        "row_bytes": meta["row_bytes"],
        "expected_output_pairs": meta["expected_pairs"],
        "output_bytes": output_bytes,
        "output_mib": round(output_bytes / 1048576, 2),
        "fan_in": args.fan_in,
        "chunk_size": args.chunk_size,
        "chunk_bytes": args.chunk_bytes,
        "pair_chunk_size": args.pair_chunk_size,
        "pair_chunk_bytes": args.pair_chunk_bytes,
        "merge_waves": {
            "left": job_stats["left_waves"],
            "right": job_stats["right_waves"],
            "pair": job_stats["pair_waves"],
        },
        "spilled_runs": {
            "left": job_stats["left_runs"],
            "right": job_stats["right_runs"],
        },
        "peak_rss_mib_measured": round(peak_rss_mib, 2),
        "rss_limit_mib": args.rss_limit_mib,
        "rss_within_limit": peak_rss_mib <= args.rss_limit_mib,
        "wall_seconds": round(wall_seconds, 2),
        "validation": validation,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if not report["rss_within_limit"]:
        raise SystemExit(
            f"peak RSS {peak_rss_mib:.1f} MiB exceeds limit {args.rss_limit_mib} MiB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
