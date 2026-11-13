"""Command-line entry point for the external left-join job.

Example::

    python -m handover_join.cli \\
        --left facts.jsonl --right registry.jsonl --config keys.json \\
        --output pairs.jsonl --work-dir /tmp/join --chunk-size 50 --fan-in 3
"""

import argparse
import json
import sys

from .errors import JoinError
from .job import run_join


def build_parser():
    parser = argparse.ArgumentParser(
        prog="handover_join",
        description="External-memory left join of two line-delimited JSON files.",
    )
    parser.add_argument("--left", required=True, help="left input JSONL")
    parser.add_argument("--right", required=True, help="right input JSONL")
    parser.add_argument("--config", required=True, help="join key configuration JSON")
    parser.add_argument("--output", required=True, help="output JSONL path")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="directory for temporary run files (default: system temp)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="records per in-memory sort chunk (default 1000)",
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=None,
        help="max buffered frame bytes per sort chunk; spill earlier once "
        "exceeded (default: bounded by chunk-size only)",
    )
    parser.add_argument(
        "--pair-chunk-size",
        type=int,
        default=None,
        help="records per chunk when sorting output pairs (default: chunk-size)",
    )
    parser.add_argument(
        "--pair-chunk-bytes",
        type=int,
        default=None,
        help="byte budget for the pair-sort chunks (default: chunk-bytes)",
    )
    parser.add_argument(
        "--fan-in",
        type=int,
        default=4,
        help="maximum runs merged in one pass, >= 2 (default 4)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        stats = run_join(
            left_input=args.left,
            right_input=args.right,
            config=args.config,
            output=args.output,
            work_dir=args.work_dir,
            chunk_size=args.chunk_size,
            fan_in=args.fan_in,
            pair_chunk_size=args.pair_chunk_size,
            chunk_bytes=args.chunk_bytes,
            pair_chunk_bytes=args.pair_chunk_bytes,
        )
    except JoinError as exc:
        print(f"join failed: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"join failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
