"""Actual file-I/O audit with an LD_PRELOAD read monitor.

The scanner reports which pages it read.  These tests independently verify
that report by intercepting ``read``/``pread``/``pread64`` in a child Python
process with a small shim (``scripts/readwatch.c``) and comparing the offsets
and lengths of the reads that hit ``data/g*.bin`` with the reported page
reads.  The descriptions column is long and mostly unique, so not reading it
is visible both as zero pages in the report and as no I/O at those ranges.
"""

import json
import os
import subprocess
import sys
import unittest

import support
from support import StoreCase, vendor_rows, write_jsonl, write_schema

from column_store.builder import build
from column_store.schema import make_schema

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import io_monitor  # noqa: E402


class ActualIOTests(StoreCase):
    @classmethod
    def setUpClass(cls):
        cls.shim_dir = os.path.join(cls.tmpdir_root(), "shim")
        os.makedirs(cls.shim_dir, exist_ok=True)
        try:
            cls.shim = io_monitor.build_shim(cls.shim_dir)
        except RuntimeError as exc:
            raise unittest.SkipTest(str(exc))

    @classmethod
    def tmpdir_root(cls):
        import tempfile
        if not hasattr(cls, "_root"):
            cls._root = tempfile.mkdtemp(prefix="colstore-io-")
        return cls._root

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmpdir_root(), ignore_errors=True)

    def run_child(self, store, projection, conditions, out_name):
        """Run a scan in a fresh process under the read monitor."""
        log = self.path(out_name + ".log")
        if os.path.exists(log):
            os.unlink(log)
        out = self.path(out_name + ".jsonl")
        report_path = self.path(out_name + ".report.json")
        script = self.path(out_name + ".py")
        with open(script, "w") as fh:
            fh.write(
                "import json, sys\n"
                "sys.path.insert(0, %r)\n"
                "from column_store.scanner import scan_to_file\n"
                "scan_to_file(%r, %r, %r, %r, report_path=%r)\n"
                % (_ROOT, store, out, projection, conditions, report_path))
        env = dict(os.environ)
        env["LD_PRELOAD"] = self.shim
        env["READWATCH_LOG"] = log
        # READWATCH_PATH is intentionally unset: the shim then logs every
        # file, and the test filters to the store's data directory itself.
        env.pop("READWATCH_PATH", None)
        r = subprocess.run([sys.executable, "-I", script], env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError("child scan failed: %s" % r.stderr)
        entries = io_monitor.parse_log(log)
        data_dir = os.path.realpath(os.path.join(store, "data"))
        io = [e for e in entries
              if os.path.realpath(e["path"]).startswith(data_dir + os.sep)
              and e["returned"] > 0]
        with open(report_path) as fh:
            report = json.load(fh)
        with open(out) as fh:
            rows = [json.loads(line) for line in fh]
        return rows, report, io, data_dir

    def check_io_matches_report(self, io, report, data_dir):
        files = {}
        for e in io:
            files.setdefault(os.path.realpath(e["path"]), []).append(
                (e["offset"], e["returned"]))
        reported = {}
        for p in report["page_reads"]:
            meta = self._meta
            fname = os.path.realpath(
                os.path.join(data_dir, meta.groups[p["group"]].file))
            reported.setdefault(fname, []).append((p["offset"], p["length"]))
        self.assertEqual(files, reported)
        self.assertEqual(sum(e["returned"] for e in io),
                         report["bytes_read_from_data_pages"])

    def make_wide_store(self, nrows=200, group=5):
        rows = vendor_rows(nrows, seed=11)
        # Make the description column dominant in size.
        for i, r in enumerate(rows):
            if r["description"] is not None:
                r["description"] += " " + ("payload-%06d " % i) * 8
        schema_doc = support.VENDOR_SCHEMA_DOC
        src = write_jsonl(self.path("wide.jsonl"), rows, ensure_ascii=False)
        store = self.path("wide")
        build(src, store, make_schema(schema_doc), row_group_max=group)
        self._meta = __import__("column_store.catalog",
                                fromlist=["load_store"]).load_store(store)
        self.rows = rows
        return store

    def test_narrow_query_reads_only_needed_pages(self):
        store = self.make_wide_store()
        rows, report, io, data_dir = self.run_child(
            store, ["event_id"], [["amount_cents", "gt", 9]], "narrow")
        self.assertNotIn("description", report["columns_read"])
        self.assertNotIn("description", report["bytes_read_by_column"])
        self.check_io_matches_report(io, report, data_dir)
        # No reported page belongs to the description column.
        meta = self._meta
        desc_pages = set()
        for g in meta.groups:
            p = g.columns[4]
            desc_pages.add((g.ordinal, p.offset, p.length))
        got = {(p["group"], p["offset"], p["length"])
               for p in report["page_reads"]}
        self.assertEqual(desc_pages & got, set())
        # And no actual I/O overlaps a description page.
        for e in io:
            rel = os.path.relpath(os.path.realpath(e["path"]), data_dir)
            ordinal = int(rel[1:7])
            start, length = e["offset"], e["returned"]
            for g in meta.groups:
                if g.ordinal != ordinal:
                    continue
                dp = g.columns[4]
                overlap = start < dp.offset + dp.length and \
                    dp.offset < start + length
                self.assertFalse(overlap,
                                 "I/O touched description page of group %d"
                                 % ordinal)

    def test_statistics_skipped_groups_have_zero_io(self):
        store = self.make_wide_store()
        # Every 8th row has a deeply negative amount; groups of 5 rows can be
        # entirely negative, and those must be skipped from statistics.
        rows, report, io, data_dir = self.run_child(
            store, ["event_id"], [["amount_cents", "ge", 0]], "skipped")
        self.assertGreater(report["row_groups_skipped_by_stats"], 0)
        skipped = set()
        for entry in report["row_groups_skipped_by_reason"]:
            skipped.add(entry["group"])
        touched = set()
        for e in io:
            rel = os.path.relpath(os.path.realpath(e["path"]), data_dir)
            touched.add(int(rel[1:7]))
        self.assertEqual(skipped & touched, set())
        self.check_io_matches_report(io, report, data_dir)

    def test_no_predicate_reads_only_projection_and_not_skipped(self):
        store = self.make_wide_store()
        rows, report, io, data_dir = self.run_child(
            store, ["region"], [], "proj")
        self.assertEqual(report["columns_read"], ["region"])
        self.assertEqual(report["row_groups_skipped_by_stats"], 0)
        self.check_io_matches_report(io, report, data_dir)
        self.assertEqual(len(rows), len(self.rows))
        self.assertEqual([r[0] for r in rows],
                         [r["region"] for r in self.rows])

    def test_damaged_unprojected_page_narrow_query_succeeds(self):
        store = self.make_wide_store()
        meta = self._meta
        # Damage the description page in group 0.
        p = meta.groups[0].columns[4]
        fpath = os.path.join(store, "data", meta.groups[0].file)
        with open(fpath, "r+b") as fh:
            fh.seek(p.offset + p.length // 2)
            b = fh.read(1)
            fh.seek(p.offset + p.length // 2)
            fh.write(bytes([b[0] ^ 0xFF]))
        rows, report, io, data_dir = self.run_child(
            store, ["event_id"], [["amount_cents", "gt", 9]], "narrow-dmg")
        self.assertTrue(rows)
        self.check_io_matches_report(io, report, data_dir)
        # Projecting it fails and leaves no result.
        log = self.path("fail.log")
        out = self.path("fail.jsonl")
        script = self.path("fail.py")
        with open(script, "w") as fh:
            fh.write("import sys; sys.path.insert(0, %r)\n"
                     "from column_store.scanner import scan_to_file\n"
                     "scan_to_file(%r, %r, ['description'], [])\n"
                     % (_ROOT, store, out))
        env = dict(os.environ)
        env["LD_PRELOAD"] = self.shim
        env["READWATCH_LOG"] = log
        env.pop("READWATCH_PATH", None)
        r = subprocess.run([sys.executable, "-I", script], env=env,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(out))
