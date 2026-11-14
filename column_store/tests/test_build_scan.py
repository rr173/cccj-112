"""End-to-end behaviour of build + scan on the synthetic vendor data."""

import json
import os

import support
from support import StoreCase, VENDOR_SCHEMA_DOC, vendor_rows

from column_store.builder import build
from column_store.catalog import load_store
from column_store.errors import UsageError
from column_store.scanner import scan, scan_to_file
from column_store.schema import make_schema


class ScanCorrectnessTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.rows = vendor_rows(120)
        self.src, self.store, self.schema = self.make_store(
            self.rows, row_group_max=7)

    def test_projection_keeps_source_order_and_column_order(self):
        got, report = scan(self.store, ["description", "event_id"], [])
        self.assertEqual(len(got), len(self.rows))
        for i, row in enumerate(got):
            self.assertEqual(row, (self.rows[i]["description"],
                                   self.rows[i]["event_id"]))
        self.assertEqual(report.rows_emitted, len(self.rows))

    def test_where_column_not_projected(self):
        conds = [["amount_cents", "gt", 10]]
        got, report = scan(self.store, ["event_id"], conds)
        want = [r["event_id"] for r in self.rows
                if r["amount_cents"] is not None
                and r["amount_cents"] > 10]
        self.assertEqual([r[0] for r in got], want)

    def test_conditions_are_anded(self):
        conds = [["region", "eq", "Norte"], ["amount_cents", "ge", 0],
                 ["is_refund", "is_not_null", None]]
        got, _ = scan(self.store, ["event_id", "region"], conds)
        want = []
        for r in self.rows:
            if (r["region"] == "Norte" and r["amount_cents"] is not None
                    and r["amount_cents"] >= 0):
                want.append((r["event_id"], r["region"]))
        self.assertEqual(got, want)

    def test_comparison_operators(self):
        for op, fn in [("eq", lambda a, b: a == b),
                       ("lt", lambda a, b: a < b),
                       ("le", lambda a, b: a <= b),
                       ("gt", lambda a, b: a > b),
                       ("ge", lambda a, b: a >= b)]:
            with self.subTest(op=op):
                got, _ = scan(self.store, ["event_id"],
                              [["amount_cents", op, 9]])
                want = [r["event_id"] for r in self.rows
                        if r["amount_cents"] is not None
                        and fn(r["amount_cents"], 9)]
                self.assertEqual([g[0] for g in got], want)

    def test_null_semantics_for_comparisons(self):
        # amount_cents is null at i % 7 == 0; none of those rows may match a
        # value comparison, and is_null must find exactly them.
        nulls, _ = scan(self.store, ["event_id"],
                        [["amount_cents", "is_null", None]])
        self.assertEqual([r[0] for r in nulls],
                         [r["event_id"] for r in self.rows
                          if r["amount_cents"] is None])
        not_nulls, _ = scan(self.store, ["event_id"],
                            [["amount_cents", "is_not_null", None]])
        self.assertEqual([r[0] for r in not_nulls],
                         [r["event_id"] for r in self.rows
                          if r["amount_cents"] is not None])
        empty, _ = scan(self.store, ["event_id"],
                        [["amount_cents", "lt", 2 ** 62]])
        self.assertEqual(len(empty), len(not_nulls))

    def test_string_comparison_uses_code_points(self):
        # Uppercase letters sort before lowercase in code point order, which
        # dictionary insertion order would not guarantee.
        names = ["banana", "Apple", "apple", "Äpfel", "zebra", "Zebra",
                 "éclair", "0zero", " space"]
        schema = [{"name": "k", "type": "integer", "nullable": False},
                  {"name": "s", "type": "string", "nullable": True}]
        rows = [{"k": i, "s": s} for i, s in enumerate(names)]
        src, store, _ = self.make_store(rows, schema_doc=schema, name="cp",
                                        row_group_max=2)
        for op, fn in [("lt", lambda a, b: a < b),
                       ("le", lambda a, b: a <= b),
                       ("gt", lambda a, b: a > b),
                       ("ge", lambda a, b: a >= b),
                       ("eq", lambda a, b: a == b)]:
            with self.subTest(op=op):
                for literal in ("Apple", "a", "zebra", "é", "0", " s"):
                    got, _ = scan(store, ["k"], [["s", op, literal]])
                    want = [i for i, s in enumerate(names) if fn(s, literal)]
                    self.assertEqual([g[0] for g in got], want,
                                     (op, literal))

    def test_integer_comparison_is_numeric_not_lexical(self):
        schema = [{"name": "n", "type": "integer", "nullable": False},
                  {"name": "tag", "type": "string", "nullable": False}]
        values = [-100, -9, -1, 0, 2, 9, 10, 11, 99, 100, 1000]
        rows = [{"n": v, "tag": str(v)} for v in values]
        src, store, _ = self.make_store(rows, schema_doc=schema, name="num",
                                        row_group_max=3)
        got, _ = scan(store, ["n"], [["n", "gt", 9]])
        self.assertEqual([g[0] for g in got], [10, 11, 99, 100, 1000])
        got, _ = scan(store, ["n"], [["n", "lt", -1]])
        self.assertEqual([g[0] for g in got], [-100, -9])

    def test_metadata_statistics(self):
        meta = load_store(self.store)
        self.assertEqual(meta.row_count, len(self.rows))
        self.assertEqual(sum(g.rows for g in meta.groups), len(self.rows))
        self.assertTrue(all(g.rows <= 7 for g in meta.groups))
        amount_total = meta.column_totals[2]
        present = [r["amount_cents"] for r in self.rows
                   if r["amount_cents"] is not None]
        self.assertEqual(amount_total.min, min(present))
        self.assertEqual(amount_total.max, max(present))
        self.assertEqual(amount_total.null_count,
                         len(self.rows) - len(present))
        # Every group carries its own stats; integer min/max are numeric.
        for g in meta.groups:
            page = g.columns[2]
            if page.null_count < g.rows:
                self.assertLessEqual(page.min, page.max)

    def test_all_null_group_is_explicit(self):
        schema = [{"name": "k", "type": "integer", "nullable": False},
                  {"name": "s", "type": "string", "nullable": True}]
        rows = [{"k": 0, "s": "x"}, {"k": 1, "s": None}, {"k": 2, "s": None},
                {"k": 3, "s": None}]
        src, store, _ = self.make_store(rows, schema_doc=schema, name="an",
                                        row_group_max=2)
        meta = load_store(store)
        self.assertTrue(meta.groups[1].columns[1].all_null)
        self.assertEqual(meta.groups[1].columns[1].null_count, 2)
        got, report = scan(store, ["k"], [["s", "is_null", None]])
        self.assertEqual([g[0] for g in got], [1, 2, 3])
        self.assertEqual(report.row_groups_skipped_by_stats, 0)
        got, report = scan(store, ["k"], [["s", "eq", "zzz"]])
        self.assertEqual(got, [])
        # Group 0 holds "x": "zzz" is outside [x, x].  Group 1 is all null.
        # Both are skipped from statistics alone.
        self.assertEqual(report.row_groups_skipped_by_stats, 2)
        self.assertEqual(report.bytes_read, 0)

    def test_empty_input(self):
        src = self.path("empty.jsonl")
        open(src, "wb").close()
        store = self.path("empty")
        build(src, store, make_schema(VENDOR_SCHEMA_DOC))
        meta = load_store(store)
        self.assertEqual(meta.row_count, 0)
        self.assertEqual(meta.groups, [])
        got, report = scan(store, ["event_id"], [["region", "eq", "x"]])
        self.assertEqual(got, [])
        self.assertEqual(report.row_groups_total, 0)
        out = self.path("empty-out.jsonl")
        n, _ = scan_to_file(store, out, ["event_id"], [])
        self.assertEqual(n, 0)
        self.assertEqual(os.path.getsize(out), 0)

    def test_empty_result_is_success(self):
        out = self.path("none.jsonl")
        n, report = scan_to_file(self.store, out, ["event_id"],
                                 [["region", "eq", "nowhere"]])
        self.assertEqual(n, 0)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(os.path.getsize(out), 0)
        self.assertEqual(report.rows_emitted, 0)

    def test_rebuild_after_reordering_input(self):
        reordered = list(reversed(self.rows))
        src2 = support.write_jsonl(self.path("rev.jsonl"), reordered)
        store2 = self.path("rev")
        build(src2, store2, self.schema, row_group_max=7)
        got, _ = scan(store2, ["event_id", "region"], [])
        want = [(r["event_id"], r["region"]) for r in reordered]
        self.assertEqual(got, want)
        # And every row still filters the same way, in the new order.
        got, _ = scan(store2, ["event_id"], [["amount_cents", "gt", 9]])
        want = [r["event_id"] for r in reordered
                if r["amount_cents"] is not None
                and r["amount_cents"] > 9]
        self.assertEqual([g[0] for g in got], want)

    def test_dictionary_ids_differ_across_groups_but_filtering_is_right(self):
        # Force the same string to appear in different groups with different
        # dictionaries: it must be encoded by value, not id.
        schema = [{"name": "k", "type": "integer", "nullable": False},
                  {"name": "s", "type": "string", "nullable": False},
                  {"name": "pad", "type": "string", "nullable": False}]
        rows = []
        for g in range(4):
            for j in range(3):
                rows.append({"k": g * 3 + j, "s": "shared",
                             "pad": "g%d-%d-%s" % (g, j, "z" * (g * 5))})
        src, store, _ = self.make_store(rows, schema_doc=schema, name="dict",
                                        row_group_max=3)
        meta = load_store(store)
        encodings = [g.columns[1].encoding for g in meta.groups]
        self.assertTrue(all(e.startswith("str-dict") for e in encodings),
                        encodings)
        got, _ = scan(store, ["k"], [["s", "eq", "shared"]])
        self.assertEqual([g[0] for g in got], list(range(12)))
        got, _ = scan(store, ["pad"], [["s", "eq", "shared"],
                                       ["k", "ge", 6]])
        self.assertEqual([g[0] for g in got], [r["pad"] for r in rows[6:]])

    def test_projection_of_predicate_column_only(self):
        got, report = scan(self.store, ["region"],
                           [["region", "eq", "Sul"]])
        self.assertEqual(report.columns_read, ["region"])
        self.assertEqual(len(got), sum(1 for r in self.rows
                                       if r["region"] == "Sul"))


class ValidationBeforeOutputTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.rows = vendor_rows(20)
        self.src, self.store, self.schema = self.make_store(
            self.rows, row_group_max=5)

    def test_unknown_projection_column(self):
        with self.assertRaises(UsageError):
            scan_to_file(self.store, self.path("x.jsonl"), ["nope"], [])
        self.assertFalse(os.path.exists(self.path("x.jsonl")))

    def test_unknown_condition_column(self):
        with self.assertRaises(UsageError):
            scan_to_file(self.store, self.path("x.jsonl"), ["event_id"],
                         [["nope", "eq", 1]])
        self.assertFalse(os.path.exists(self.path("x.jsonl")))

    def test_validation_leaves_no_output_directory(self):
        out_dir = self.path("newdir")
        self.assertFalse(os.path.exists(out_dir))
        with self.assertRaises(UsageError):
            scan_to_file(self.store, os.path.join(out_dir, "x.jsonl"),
                         ["nope"], [])
        self.assertFalse(os.path.exists(out_dir))

    def test_incompatible_literals(self):
        bad = [
            ["amount_cents", "eq", "10"],
            ["amount_cents", "eq", True],
            ["amount_cents", "eq", 1.5],
            ["amount_cents", "eq", None],
            ["region", "eq", 10],
            ["region", "eq", True],
            ["is_refund", "eq", True],
            ["is_refund", "gt", False],
            ["event_id", "between", 1],
        ]
        for cond in bad:
            with self.subTest(cond=cond):
                out = self.path("bad-%s.jsonl" % cond[1])
                with self.assertRaises(UsageError):
                    scan_to_file(self.store, out, ["event_id"], [cond])
                self.assertFalse(os.path.exists(out))
        # is_null/is_not_null are fine on any column, booleans included.
        got, _ = scan(self.store, ["event_id"],
                      [["is_refund", "is_not_null", None]])
        self.assertEqual(len(got), len(self.rows))
        # A null-test with a value is a usage error.
        with self.assertRaises(UsageError):
            scan(self.store, ["event_id"], [["event_id", "is_null", 1]])

    def test_existing_output_is_not_overwritten(self):
        out = self.path("exists.jsonl")
        with open(out, "w") as fh:
            fh.write("keep me\n")
        with self.assertRaises(Exception):
            scan_to_file(self.store, out, ["event_id"], [])
        with open(out) as fh:
            self.assertEqual(fh.read(), "keep me\n")

    def test_source_and_store_unchanged_by_scan(self):
        def digest_tree(root):
            out = {}
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for name in sorted(filenames):
                    p = os.path.join(dirpath, name)
                    with open(p, "rb") as fh:
                        out[os.path.relpath(p, root)] = fh.read()
            return out
        with open(self.src, "rb") as fh:
            src_before = fh.read()
        store_before = digest_tree(self.store)
        scan_to_file(self.store, self.path("out.jsonl"), ["region"],
                     [["amount_cents", "gt", 0]])
        with open(self.src, "rb") as fh:
            self.assertEqual(fh.read(), src_before)
        self.assertEqual(digest_tree(self.store), store_before)

    def test_output_jsonl_has_projection_order(self):
        out = self.path("order.jsonl")
        scan_to_file(self.store, out, ["description", "event_id",
                                       "region"], [])
        with open(out) as fh:
            first = json.loads(fh.readline())
        self.assertEqual(first, [self.rows[0]["description"],
                                 self.rows[0]["event_id"],
                                 self.rows[0]["region"]])
