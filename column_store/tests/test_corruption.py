"""Corruption handling: index and page damage, and what may / may not fail.

Rules under test:
  * any damage to the header or catalog (the index) is fatal for every scan;
  * a damaged page is fatal when a scan reads it, and irrelevant when it does
    not (not projected, not needed by a predicate, or the group was skipped
    by statistics);
  * a failing scan leaves no output file and no temporary files;
  * an unsupported format version is rejected before output.
"""

import json
import os
import shutil
import struct
import zlib

import support
from support import StoreCase, VENDOR_SCHEMA_DOC, vendor_rows

from column_store.builder import build
from column_store.catalog import (HEADER_SIZE, load_store, read_root_header)
from column_store.errors import (StoreFormatError, StoreIntegrityError)
from column_store.scanner import scan, scan_to_file
from column_store.schema import make_schema


def _flip(path, offset):
    with open(path, "r+b") as fh:
        fh.seek(offset)
        b = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([b[0] ^ 0xFF]))


class CorruptionCase(StoreCase):
    def setUp(self):
        super().setUp()
        # Each group has 4 rows so a query can target single groups.
        self.rows = vendor_rows(24)
        self.schema_doc = VENDOR_SCHEMA_DOC
        self.src, self.store, self.schema = self.make_store(
            self.rows, row_group_max=4)

    def copy_store(self, name="copy"):
        dst = self.path(name)
        shutil.copytree(self.store, dst)
        return dst

    def page_meta(self, store, group, column):
        meta = load_store(store)
        return meta.groups[group].columns[column], meta.groups[group]

    def group_file(self, store, group):
        meta = load_store(store)
        return os.path.join(store, "data", meta.groups[group].file)

    def flip_inside_page(self, store, group, column, within=None):
        page, gmeta = self.page_meta(store, group, column)
        path = self.group_file(store, group)
        off = page.offset + (page.length // 2 if within is None else within)
        _flip(path, off)
        return off

    def truncate_page(self, store, group, column):
        """Cut the file in the middle of the given page."""
        page, gmeta = self.page_meta(store, group, column)
        path = self.group_file(store, group)
        os.truncate(path, page.offset + max(1, page.length // 2))


class IndexCorruptionTests(CorruptionCase):
    def test_header_crc_flip_is_fatal(self):
        store = self.copy_store()
        _flip(os.path.join(store, "HEADER"), 60)
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["event_id"], [])

    def test_catalog_crc_flip_is_fatal(self):
        store = self.copy_store()
        head = read_root_header(store)
        _flip(os.path.join(store, "HEADER"),
              HEADER_SIZE + head["catalog_length"] // 2)
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["event_id"], [])

    def test_truncated_index_is_fatal(self):
        store = self.copy_store()
        path = os.path.join(store, "HEADER")
        size = os.path.getsize(path)
        os.truncate(path, size - 10)
        with self.assertRaises(StoreFormatError):
            scan(store, ["event_id"], [])

    def test_unsupported_format_version(self):
        store = self.copy_store()
        path = os.path.join(store, "HEADER")
        with open(path, "r+b") as fh:
            head = bytearray(fh.read(HEADER_SIZE))
            struct.pack_into(">I", head, 8, 2)
            struct.pack_into(">I", head, 76,
                             zlib.crc32(bytes(head[:76])) & 0xFFFFFFFF)
            fh.seek(0)
            fh.write(head)
        out = self.path("v2.jsonl")
        with self.assertRaises(StoreFormatError) as ctx:
            scan_to_file(store, out, ["event_id"], [])
        self.assertIn("version", str(ctx.exception))
        self.assertFalse(os.path.exists(out))

    def test_catalog_semantic_damage_rejected(self):
        # Rebuild a valid header around a catalog that claims a page is
        # somewhere it is not: the index is self-consistently checksummed but
        # semantically wrong, so the reader must reject it.
        store = self.copy_store()
        path = os.path.join(store, "HEADER")
        with open(path, "rb") as fh:
            head = fh.read(HEADER_SIZE)
            catalog = fh.read()
        doc = json.loads(catalog.decode("utf-8"))
        doc["groups"][0]["columns"][0]["offset"] = 9999
        broken = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
        from column_store.catalog import make_header
        with open(path, "wb") as fh:
            fh.write(make_header(broken))
            fh.write(broken)
        with self.assertRaises(StoreFormatError):
            scan(store, ["event_id"], [])

    def test_boolean_type_in_schema_cannot_be_smuggled(self):
        store = self.copy_store()
        path = os.path.join(store, "HEADER")
        with open(path, "rb") as fh:
            head = fh.read(HEADER_SIZE)
            catalog = fh.read()
        doc = json.loads(catalog.decode("utf-8"))
        # Change the integer column to a boolean in the schema only; the page
        # kind will then disagree and the index is rejected at parse time
        # because encodings no longer match the schema.
        doc["schema"][0]["type"] = "boolean"
        broken = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
        from column_store.catalog import make_header
        with open(path, "wb") as fh:
            fh.write(make_header(broken))
            fh.write(broken)
        with self.assertRaises(StoreFormatError):
            scan(store, ["event_id"], [])


class PageCorruptionTests(CorruptionCase):
    def test_damaged_projection_page_fails_without_output(self):
        store = self.copy_store()
        self.flip_inside_page(store, 0, 0)  # event_id page of group 0
        out = self.path("out.jsonl")
        with self.assertRaises(StoreIntegrityError):
            scan_to_file(store, out, ["event_id"], [])
        self.assertFalse(os.path.exists(out))
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".scan")]
        self.assertEqual(leftovers, [])

    def test_damaged_where_page_fails(self):
        store = self.copy_store()
        # is_null cannot be decided from the group range, so the amount page
        # of every group is read; group 1's is damaged.
        self.flip_inside_page(store, 1, 2)
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["event_id"], [["amount_cents", "is_null", None]])

    def test_unrelated_page_damage_does_not_affect_narrow_query(self):
        store = self.copy_store()
        # description (column 4) is long, projected by nobody here.
        self.flip_inside_page(store, 0, 4)
        got, report = scan(store, ["event_id"], [["amount_cents", "is_null",
                                                  None]])
        self.assertTrue(got)
        self.assertNotIn("description", report.columns_read)

    def test_damaged_description_page_breaks_only_queries_that_use_it(self):
        store = self.copy_store()
        self.flip_inside_page(store, 0, 4)
        scan(store, ["event_id"], [])  # fine
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["description"], [])

    def test_skipped_group_damage_is_irrelevant(self):
        # Find a group whose non-null amounts are all negative, then damage
        # a page in it.  "amount_cents >= 0" skips that group from statistics
        # alone, so the query must succeed without touching the group.
        store = self.copy_store()
        meta = load_store(store)
        victim = None
        for g in meta.groups:
            page = g.columns[2]
            if page.min is not None and page.min < 0:
                if page.max is not None and page.max < 0:
                    victim = g.ordinal
                    break
        self.assertIsNotNone(victim)
        self.flip_inside_page(store, victim, 0)
        got, report = scan(store, ["event_id"], [["amount_cents", "ge", 0]])
        self.assertGreaterEqual(report.row_groups_skipped_by_stats, 1)
        self.assertNotIn(victim, [p.group for p in report.pages_read])

    def test_truncated_page_is_fatal(self):
        store = self.copy_store()
        self.truncate_page(store, 0, 4)  # cuts the last page of group 0
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["description"], [])

    def test_page_header_crc_alone_is_checked(self):
        store = self.copy_store()
        page, gmeta = self.page_meta(store, 0, 0)
        _flip(self.group_file(store, 0), page.offset + 5)
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["event_id"], [])

    def test_null_bitmap_row_count_mismatch(self):
        # Rebuild the index so it claims a different group row count while
        # the page still says 4.  The index itself stays self consistent
        # (totals adjusted), so the failure must come from the page check.
        store = self.copy_store()
        meta = load_store(store)
        path = os.path.join(store, "HEADER")
        with open(path, "rb") as fh:
            fh.read(HEADER_SIZE)
            catalog = fh.read()
        doc = json.loads(catalog.decode("utf-8"))
        doc["groups"][0]["rows"] = 5
        doc["row_count"] = sum(g["rows"] for g in doc["groups"])
        broken = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
        from column_store.catalog import make_header
        with open(path, "wb") as fh:
            fh.write(make_header(broken))
            fh.write(broken)
        with self.assertRaises((StoreIntegrityError, StoreFormatError)):
            scan(store, ["event_id"], [])

    def test_dict_index_out_of_range_fails_scan(self):
        store = self.copy_store()
        meta = load_store(store)
        group = meta.groups[0]
        page = group.columns[1]  # region, dictionary encoded
        path = self.group_file(store, 0)
        with open(path, "r+b") as fh:
            fh.seek(page.offset + page.length - 1)
            fh.write(b"\xfe")
        with self.assertRaises(StoreIntegrityError):
            scan(store, ["region"], [])
