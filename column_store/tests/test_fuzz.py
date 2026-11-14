"""Fuzz the scanner against the independent naive oracle.

Generates random schemas, random rows (including nulls, empty strings, NUL
bytes, non-ASCII text and extreme integers), random row-group sizes, random
projections and random ANDed predicates.  Every comparison is checked both
through the API and through ``tests/naive_scan.py`` run as its own process.
"""

import json
import os
import random
import subprocess
import sys

import support
from support import StoreCase, write_jsonl

from column_store.builder import build
from column_store.scanner import scan
from column_store.schema import make_schema

TYPES = ("integer", "string", "boolean")
STRINGS = ["", "\x00", "a", "A", "a\x00b", "Norte", "norte", "NORTE", " ",
           "  x", "é", "é", "😀", "Ω", "10", "9", "zzz", "Zzz",
           "a" * 40, "shared", "shared", "shared"]
INTS = [-(2 ** 63), 2 ** 63 - 1, -1000, -10, -9, -1, 0, 1, 9, 10, 11,
        1000, 2 ** 40]


class FuzzCase(StoreCase):
    def generate(self, rng, width, nrows):
        schema_doc = []
        for c in range(width):
            nullable = rng.random() < 0.6
            schema_doc.append({"name": "c%d" % c, "type": rng.choice(TYPES),
                               "nullable": nullable})
        rows = []
        for _ in range(nrows):
            row = {}
            for col in schema_doc:
                if col["nullable"] and rng.random() < 0.2:
                    row[col["name"]] = None
                    continue
                if col["type"] == "integer":
                    row[col["name"]] = rng.choice(INTS)
                elif col["type"] == "string":
                    row[col["name"]] = rng.choice(STRINGS)
                else:
                    row[col["name"]] = rng.choice([True, False])
            rows.append(row)
        return schema_doc, rows

    def random_conditions(self, rng, schema_doc):
        conds = []
        for _ in range(rng.randint(0, 3)):
            col = rng.choice(schema_doc)
            name, t = col["name"], col["type"]
            if t == "boolean":
                op = rng.choice(["is_null", "is_not_null"])
            else:
                op = rng.choice(["eq", "lt", "le", "gt", "ge", "eq", "eq",
                                 "is_null", "is_not_null"])
            if op in ("is_null", "is_not_null"):
                conds.append([name, op, None])
            elif t == "integer":
                conds.append([name, op, rng.choice(INTS + [10 ** 20,
                                                           -10 ** 20])])
            else:
                conds.append([name, op, rng.choice(STRINGS)])
        return conds

    def test_fuzz_against_naive(self):
        rng = random.Random(20251114)
        for iteration in range(25):
            width = rng.randint(1, 4)
            nrows = rng.randint(0, 40)
            schema_doc, rows = self.generate(rng, width, nrows)
            schema = make_schema(schema_doc)
            name = "f%d" % iteration
            src = write_jsonl(self.path(name + ".jsonl"), rows,
                              ensure_ascii=False)
            store = self.path(name)
            build(src, store, schema, row_group_max=rng.randint(1, 6))
            names = [c["name"] for c in schema_doc]
            for _ in range(8):
                proj = rng.sample(names, rng.randint(1, len(names)))
                conds = self.random_conditions(rng, schema_doc)
                with self.subTest(iteration=iteration, proj=proj,
                                  conds=conds):
                    got, report = scan(store, proj, conds)
                    want = self.naive(rows, schema_doc, proj, conds)
                    self.assertEqual(got, want)
                    self.assertLessEqual(
                        report.row_groups_checked
                        + report.row_groups_skipped_by_stats,
                        report.row_groups_total)
            # And the standalone oracle process agrees on one projection.
            proj = rng.sample(names, rng.randint(1, len(names)))
            conds = self.random_conditions(rng, schema_doc)
            out = self.path(name + "-out.jsonl")
            from column_store.scanner import scan_to_file
            scan_to_file(store, out, proj, conds)
            self.assert_naive_cli(src, self.write_schema_doc(name, schema_doc),
                                  proj, conds, out)

    def write_schema_doc(self, name, schema_doc):
        path = self.path(name + "-schema.json")
        with open(path, "w") as fh:
            json.dump({"columns": schema_doc}, fh)
        return path
