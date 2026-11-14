"""Shared fixtures and helpers for the column_store test suite."""

import json
import os
import random
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from column_store.builder import build  # noqa: E402
from column_store.catalog import load_store  # noqa: E402
from column_store.schema import make_schema  # noqa: E402
from column_store.scanner import scan, scan_to_file  # noqa: E402

import naive_scan  # noqa: E402


VENDOR_SCHEMA_DOC = [
    {"name": "event_id", "type": "integer", "nullable": False},
    {"name": "region", "type": "string", "nullable": True},
    {"name": "amount_cents", "type": "integer", "nullable": True},
    {"name": "is_refund", "type": "boolean", "nullable": False},
    {"name": "description", "type": "string", "nullable": True},
]


def vendor_schema():
    return make_schema(VENDOR_SCHEMA_DOC)


VENDOR_COLUMNS = ["event_id", "region", "amount_cents", "is_refund",
                  "description"]


def write_jsonl(path, rows, ensure_ascii=False):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=ensure_ascii))
            fh.write("\n")
    return path


def write_schema(path, doc=None):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"columns": doc if doc is not None else VENDOR_SCHEMA_DOC},
                  fh)
    return path


def vendor_rows(count, seed=7, with_nulls=True):
    """Synthetic supplier events.

    A few region names repeat heavily; descriptions are long and mostly
    unique; amounts straddle negative values, 0, 9, 10 and large values.
    Nulls sit at different positions per column.
    """
    rng = random.Random(seed)
    regions = ["Norte", "Sul", "Leste", "Oeste", "Norte", "Norte", "Sul"]
    rows = []
    for i in range(count):
        region = regions[i % len(regions)]
        # The first 32 rows are deeply negative, so small row groups there
        # contain only negative amounts and a ">= 0" predicate can skip them
        # from statistics alone.  Later rows mix 0, 9, 10, large positive
        # values and occasional negatives.
        if i < 32:
            amount = -(10 ** 6 + i)
        else:
            amount = [0, 9, 10, 1_000_000 + i, 2 ** 40 + i, i, 7,
                      -1][i % 8]
        desc = "vendor event %06d %s" % (i, "x" * (i % 23))
        row = {
            "event_id": 1000 + i,
            "region": region,
            "amount_cents": amount,
            "is_refund": (i % 4 == 0),
            "description": desc,
        }
        if with_nulls:
            if i % 11 == 0:
                row["region"] = None
            if i % 7 == 0:
                row["amount_cents"] = None
            if i % 13 == 0:
                row["description"] = None
        rows.append(row)
    return rows


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="colstore-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_store(self, rows, schema_doc=None, name="store",
                   row_group_max=4):
        schema = make_schema(schema_doc or VENDOR_SCHEMA_DOC)
        src = write_jsonl(self.path(name + ".jsonl"), rows)
        store = self.path(name)
        build(src, store, schema, row_group_max=row_group_max)
        return src, store, schema

    def read_result(self, path):
        out = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                out.append(tuple(json.loads(line)))
        return out

    def naive(self, rows, schema_doc, projection, conditions):
        """Run the independent oracle in-process on an in-memory row list."""
        by_name = {c["name"]: c for c in schema_doc}
        proj = projection
        conds = []
        for c in conditions:
            name, op, value = c
            conds.append((name, op, value))
        out = []
        for row in rows:
            ok = True
            for name, op, value in conds:
                v = row[name]
                if op == "is_null":
                    if v is not None:
                        ok = False
                        break
                elif op == "is_not_null":
                    if v is None:
                        ok = False
                        break
                else:
                    if v is None:
                        ok = False
                        break
                    if op == "eq" and not v == value:
                        ok = False
                        break
                    if op == "lt" and not v < value:
                        ok = False
                        break
                    if op == "le" and not v <= value:
                        ok = False
                        break
                    if op == "gt" and not v > value:
                        ok = False
                        break
                    if op == "ge" and not v >= value:
                        ok = False
                        break
            if ok:
                out.append(tuple(row[n] for n in proj))
        return out

    def assert_matches_naive(self, rows, schema_doc, store, projection,
                             conditions):
        got, report = scan(store, projection, conditions)
        want = self.naive(rows, schema_doc, projection, conditions)
        self.assertEqual(got, want)
        self.assertEqual(report.rows_emitted, len(want))
        return got, report

    def assert_naive_cli(self, src, schema_path, projection, conditions,
                         store_out):
        """Also run tests/naive_scan.py as a separate process and compare."""
        import subprocess
        cmd = [sys.executable, "-I", os.path.join(_HERE, "naive_scan.py"),
               "--schema", schema_path, "--input", src,
               "--columns", json.dumps(projection)]
        for c in conditions:
            cmd += ["--where", json.dumps(c)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        want = [tuple(x) for x in map(json.loads, r.stdout.splitlines())]
        got = self.read_result(store_out)
        self.assertEqual(got, want)
        return got
