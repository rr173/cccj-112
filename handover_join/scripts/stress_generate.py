"""Generate a scalable single-hot-key stress dataset for the join job.

The dataset deliberately concentrates almost all payload mass on ONE join
key shared by every "hot" row, which is the worst case for a group-buffering
implementation.  Rows are written one at a time as JSONL; the generator never
materializes the m*n pair set (it cannot even know the final pair order), so
its own memory footprint stays flat regardless of the requested scale.

Layout per side::

    left  : a few cold rows (distinct / null keys), then hot rows {id:1,...}
    right : hot rows {reg_id:1,...}, then right-only and null-key rows

All sizes are CLI parameters, so the same script drives both a quick smoke
run and the full >256 MiB payload demonstration.
"""

import argparse
import json
import os


def _write_row(fh, obj):
    fh.write(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
    fh.write(b"\n")


def generate(out_dir, left_hot, right_hot, row_bytes, cold_rows):
    os.makedirs(out_dir, exist_ok=True)
    left_path = os.path.join(out_dir, "left.jsonl")
    right_path = os.path.join(out_dir, "right.jsonl")
    config_path = os.path.join(out_dir, "config.json")

    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "left_keys": ["id"],
                "right_keys": ["reg_id"],
                "key_types": ["integer"],
            },
            fh,
        )

    # One fixed-width padding character keeps per-row size predictable; the
    # payload is a plain string, so no quadratic JSON escaping cost applies.
    pad = "x" * int(row_bytes)

    left_bytes = 0
    with open(left_path, "wb") as fh:
        # Interleaved cold rows: distinct keys, one null key, one missing key.
        for i in range(cold_rows):
            _write_row(fh, {"id": 1000 + i, "cold": True, "seq": i})
            left_bytes = fh.tell()
        _write_row(fh, {"id": None, "cold": True})
        _write_row(fh, {"cold": True, "missing": True})
        for i in range(left_hot):  # hot rows: same key, independent records
            _write_row(fh, {"id": 1, "seq": i, "pad": pad})
        left_bytes = fh.tell()

    right_bytes = 0
    with open(right_path, "wb") as fh:
        for i in range(right_hot):
            _write_row(fh, {"reg_id": 1, "rseq": i, "rpad": "y" * 64})
        # Right-only rows must never appear in a left-join output.
        for i in range(cold_rows):
            _write_row(fh, {"reg_id": 2000 + i, "right_only": True})
        _write_row(fh, {"reg_id": None, "null_key": True})
        right_bytes = fh.tell()

    expected_pairs = left_hot * right_hot + cold_rows + 2  # + null/missing left
    stats = {
        "left_path": left_path,
        "right_path": right_path,
        "config_path": config_path,
        "left_rows": left_hot + cold_rows + 2,
        "right_rows": right_hot + cold_rows + 1,
        "left_bytes": left_bytes,
        "right_bytes": right_bytes,
        "total_input_bytes": left_bytes + right_bytes,
        "hot_left_rows": left_hot,
        "hot_right_rows": right_hot,
        "row_bytes": row_bytes,
        "expected_pairs": expected_pairs,
    }
    with open(os.path.join(out_dir, "stress_meta.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--left-hot", type=int, default=300)
    parser.add_argument("--right-hot", type=int, default=8)
    parser.add_argument(
        "--row-bytes",
        type=int,
        default=900_000,
        help="approximate payload bytes per hot left row (default 900000)",
    )
    parser.add_argument("--cold-rows", type=int, default=5)
    args = parser.parse_args(argv)

    stats = generate(
        args.out_dir,
        args.left_hot,
        args.right_hot,
        args.row_bytes,
        args.cold_rows,
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
