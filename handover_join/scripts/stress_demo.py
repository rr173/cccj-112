"""One-command stress demonstration.

Generates the single-hot-key dataset (default payload > 256 MiB), runs the
external join in a child process with a 96 MiB peak-RSS budget, validates the
output ordering and pair count, and writes a JSON report.  All sizes are
parameters so the same script can do a quick small smoke run instead of
recreating the big files on every regression.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

from stress_generate import generate  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", default=None)
    parser.add_argument("--left-hot", type=int, default=300)
    parser.add_argument("--right-hot", type=int, default=8)
    parser.add_argument("--row-bytes", type=int, default=900_000)
    parser.add_argument("--cold-rows", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=100000)
    parser.add_argument("--chunk-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--pair-chunk-size", type=int, default=100000)
    parser.add_argument("--pair-chunk-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--fan-in", type=int, default=4)
    parser.add_argument("--rss-limit-mib", type=int, default=96)
    args = parser.parse_args(argv)

    meta = generate(
        args.data_dir,
        args.left_hot,
        args.right_hot,
        args.row_bytes,
        args.cold_rows,
    )
    print(
        "generated input: "
        f"{meta['total_input_bytes'] / 1048576:.1f} MiB, "
        f"expected pairs={meta['expected_pairs']}"
    )

    runner = [
        sys.executable,
        "-B",
        "-E",
        os.path.join(_HERE, "stress_run.py"),
        "--data-dir",
        args.data_dir,
        "--work-dir",
        args.work_dir,
        "--output",
        args.output,
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
        "--rss-limit-mib",
        str(args.rss_limit_mib),
    ]
    if args.report:
        runner += ["--report", args.report]
    os.execv(sys.executable, runner)


if __name__ == "__main__":
    raise SystemExit(main())
