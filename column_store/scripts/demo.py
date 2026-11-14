#!/usr/bin/env python3
"""Synthetic supplier-event demo for column_store.

Builds a store from vendor JSONL with a deliberately small row group, runs
a narrow scan that asks for the region but filters on amount, compares every
result against naive row-at-a-time filtering, and reports which row groups
were skipped.  Writes its artifacts to a fresh directory (never overwriting
anything).

    python3 -B scripts/demo.py --work-dir /tmp/column_store_demo
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from column_store.builder import build  # noqa: E402
from column_store.catalog import load_store  # noqa: E402
from column_store.errors import ColumnStoreError  # noqa: E402
from column_store.scanner import scan_to_file  # noqa: E402
from column_store.schema import load_schema  # noqa: E402

SCHEMA = [
    {"name": "event_id", "type": "integer", "nullable": False},
    {"name": "region", "type": "string", "nullable": True},
    {"name": "amount_cents", "type": "integer", "nullable": True},
    {"name": "is_refund", "type": "boolean", "nullable": False},
    {"name": "description", "type": "string", "nullable": True},
]


def make_rows(n):
    regions = ["Norte", "Sul", "Leste", "Oeste", "Norte", "Norte", "Sul",
               "Sudeste"]
    rows = []
    small = max(1, n // 3)
    for i in range(n):
        if i < small:
            # The first third only holds negative values, 0, 9 and 10.  With
            # a small row group these groups can be skipped for "> 10".
            amount = [-(10 ** 6 + i), -1, 0, 9, 10][i % 5]
        else:
            amount = [10, 250_000, -1, 2 ** 41 + i, 7, 10 ** 9 + i,
                      -(i + 1), 0][i % 8]
        row = {
            "event_id": 5000 + i,
            "region": regions[i % len(regions)],
            "amount_cents": amount,
            "is_refund": i % 5 == 0,
            "description": "supplier settlement note %06d: %s"
                           % (i, ("settlement detail %d " % i) * 6),
        }
        if i % 9 == 0:
            row["region"] = None
        if i % 6 == 0:
            row["amount_cents"] = None
        if i % 11 == 0:
            row["description"] = None
        rows.append(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--work-dir", required=True,
                   help="fresh directory to write into (must not exist)")
    p.add_argument("--rows", type=int, default=96)
    p.add_argument("--row-group-rows", type=int, default=6)
    a = p.parse_args(argv)
    work = os.path.abspath(a.work_dir)
    if os.path.lexists(work):
        print("demo: refusing to reuse existing path %s" % work,
              file=sys.stderr)
        return 2
    os.makedirs(work)
    src = os.path.join(work, "vendor_events.jsonl")
    schema_path = os.path.join(work, "schema.json")
    store = os.path.join(work, "vendor.store")
    rows = make_rows(a.rows)
    with open(src, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(schema_path, "w", encoding="utf-8") as fh:
        json.dump({"columns": SCHEMA}, fh, indent=2)
    print("input: %d rows, %d bytes -> %s"
          % (len(rows), os.path.getsize(src), src))
    build(src, store, load_schema(schema_path),
          row_group_max=a.row_group_rows)
    meta = load_store(store)
    print("store: %d groups of <=%d rows, %s"
          % (len(meta.groups), meta.row_group_max, store))
    sizes = [sum(p.length for p in g.columns) for g in meta.groups]
    print("group file sizes: min %d, max %d, total %d"
          % (min(sizes) if sizes else 0, max(sizes) if sizes else 0,
             sum(sizes)))

    out = os.path.join(work, "region_above_10.jsonl")
    report_path = os.path.join(work, "region_above_10.report.json")
    conditions = [["amount_cents", "gt", 10]]
    scan_to_file(store, out, ["region"], conditions,
                 report_path=report_path)
    with open(report_path) as fh:
        report = json.load(fh)
    print("\nnarrow scan: project region WHERE amount_cents > 10")
    print("  rows matched:              %d" % report["rows_matched"])
    print("  groups total/checked/skip: %d / %d / %d"
          % (report["row_groups_total"], report["row_groups_checked"],
             report["row_groups_skipped_by_stats"]))
    print("  columns read:              %s" % report["columns_read"])
    print("  pages read:                %d" % report["pages_read"])
    print("  bytes read from pages:     %d"
          % report["bytes_read_from_data_pages"])
    for entry in report["row_groups_skipped_by_reason"]:
        print("  skipped group %2d: %s" % (entry["group"], entry["reason"]))
    print("  amount column not projected; region is what is written.")
    print("  bytes it would cost to read every column page: %d"
          % sum(p.length for g in meta.groups for p in g.columns))
    print("  bytes it costs for description alone: %d"
          % sum(g.columns[4].length for g in meta.groups))

    # Independent naive comparison, in a separate process.
    naive = os.path.join(ROOT, "tests", "naive_scan.py")
    cmd = [sys.executable, "-I", naive, "--schema", schema_path,
           "--input", src, "--columns", json.dumps(["region"]),
           "--where", json.dumps(conditions[0])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("naive comparison failed: %s" % r.stderr, file=sys.stderr)
        return 3

    def records(path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    got = records(out)
    want = [json.loads(line) for line in r.stdout.splitlines()]
    same = got == want
    print("\nnaive row-at-a-time oracle agrees: %s (%d rows)"
          % ("yes" if same else "NO", len(want)))
    if not same:
        return 1
    digest = hashlib.sha256(open(src, "rb").read()).hexdigest()
    print("source sha256: %s" % digest)
    print("result sha256: %s"
          % hashlib.sha256(open(out, "rb").read()).hexdigest())
    print("\nartifacts written under %s" % work)
    return 0



if __name__ == "__main__":
    try:
        sys.exit(main())
    except ColumnStoreError as exc:
        print("demo error: %s" % exc, file=sys.stderr)
        sys.exit(2)
