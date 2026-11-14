"""Read a store with a small, independent decoder written from FORMAT.md.

This is a second implementation that shares no code with ``column_store``.
It exists to keep the format document honest: the builder output is parsed
from raw bytes using only the field tables in ``docs/FORMAT.md`` (plus the
standard library's struct/zlib), and every value is compared against the
source JSONL read with ``json.loads``.
"""

import json
import os
import struct
import zlib

import support
from support import StoreCase, vendor_rows, write_jsonl

from column_store.builder import build
from column_store.schema import make_schema

HEADER = struct.Struct(">8sIIQQI32sQI")
PAGE = struct.Struct(">4sBBBBIIQII")


def read_header_and_catalog(store):
    with open(os.path.join(store, "HEADER"), "rb") as fh:
        raw = fh.read()
    magic, version, flags, cat_off, cat_len, cat_crc, cat_sha, res, hcrc = \
        HEADER.unpack(raw[:80])
    assert magic == b"CSTORE01", magic
    assert version == 1
    assert flags == 0 and res == 0
    assert cat_off == 80
    assert zlib.crc32(raw[:76]) & 0xFFFFFFFF == hcrc
    catalog_bytes = raw[cat_off:cat_off + cat_len]
    assert len(catalog_bytes) == cat_len
    assert zlib.crc32(catalog_bytes) & 0xFFFFFFFF == cat_crc
    return json.loads(catalog_bytes.decode("utf-8")), len(raw)


def decode_page_independent(raw, kind, rows, encoding):
    magic, pkind, version, flags, reserved, prows, nbytes, blen, bcrc, hcrc \
        = PAGE.unpack(raw[:32])
    assert magic == b"CPG1"
    assert pkind == kind
    assert version == 1
    assert reserved == 0
    assert prows == rows
    assert nbytes == (rows + 7) // 8
    assert zlib.crc32(raw[:28]) & 0xFFFFFFFF == hcrc, "header crc"
    bitmap = raw[32:32 + nbytes]
    body = raw[32 + nbytes:]
    assert len(body) == blen
    assert zlib.crc32(bitmap + body) & 0xFFFFFFFF == bcrc, "body crc"
    nulls = [(bitmap[i >> 3] >> (i & 7)) & 1 for i in range(rows)]
    present = [i for i in range(rows) if not nulls[i]]
    values = [None] * rows
    if kind == 1:  # integer
        assert len(body) == len(present) * 8
        for k, i in enumerate(present):
            values[i] = struct.unpack_from(">q", body, k * 8)[0]
    elif kind == 2:  # boolean
        for k, i in enumerate(present):
            values[i] = bool(body[k >> 3] & (1 << (k & 7)))
    elif kind == 3:  # string
        if encoding == "str-prefix":
            pos = 0
            for i in present:
                ln = struct.unpack_from(">I", body, pos)[0]
                pos += 4
                values[i] = body[pos:pos + ln].decode("utf-8")
                pos += ln
            assert pos == len(body)
        else:
            width = body[0]
            count = struct.unpack_from(">I", body, 1)[0]
            pos = 5
            table = []
            for _ in range(count):
                ln = struct.unpack_from(">I", body, pos)[0]
                pos += 4
                table.append(body[pos:pos + ln].decode("utf-8"))
                pos += ln
            fmt = {1: ">B", 2: ">H", 4: ">I"}[width]
            size = struct.calcsize(fmt)
            if encoding == "str-dict":
                for i in present:
                    idx = struct.unpack_from(fmt, body, pos)[0]
                    pos += size
                    values[i] = table[idx]
                assert pos == len(body)
            elif encoding == "str-dict-rle":
                runs = struct.unpack_from(">I", body, pos)[0]
                pos += 4
                got = []
                for _ in range(runs):
                    idx = struct.unpack_from(fmt, body, pos)[0]
                    pos += size
                    n = struct.unpack_from(">I", body, pos)[0]
                    pos += 4
                    got.extend([table[idx]] * n)
                assert len(got) == len(present)
                for i, v in zip(present, got):
                    values[i] = v
                assert pos == len(body)
            else:
                raise AssertionError(encoding)
    else:
        raise AssertionError(kind)
    return values


def read_all(store):
    cat, total = read_header_and_catalog(store)
    schema = cat["schema"]
    kind_of = {"integer": 1, "boolean": 2, "string": 3}
    rows = []
    for group in cat["groups"]:
        path = os.path.join(store, "data", group["file"])
        assert os.path.getsize(path) == group["file_bytes"]
        with open(path, "rb") as fh:
            data = fh.read()
        per_col = []
        for ci, col in enumerate(group["columns"]):
            chunk = data[col["offset"]:col["offset"] + col["length"]]
            assert zlib.crc32(chunk) & 0xFFFFFFFF == col["crc32"]
            per_col.append(decode_page_independent(
                chunk, kind_of[schema[ci]["type"]], group["rows"],
                col["encoding"]))
        assert sum(c["length"] for c in group["columns"]) == group["file_bytes"]
        for r in range(group["rows"]):
            rows.append({schema[ci]["name"]: per_col[ci][r]
                         for ci in range(len(schema))})
    return cat, rows


class IndependentDecoderTests(StoreCase):
    def test_values_match_source(self):
        rows = vendor_rows(80, seed=5)
        for i, r in enumerate(rows):
            if r["description"] is not None:
                r["description"] += " ünïcode-%d" % i
        src = write_jsonl(self.path("src.jsonl"), rows, ensure_ascii=False)
        store = self.path("store")
        build(src, store, make_schema(support.VENDOR_SCHEMA_DOC),
              row_group_max=6)
        cat, decoded = read_all(store)
        self.assertEqual(len(decoded), len(rows))
        for want, got in zip(rows, decoded):
            self.assertEqual(got, want)
        self.assertEqual(cat["row_count"], len(rows))

    def test_encodings_cover_all_three_string_codecs(self):
        schema = [
            {"name": "k", "type": "integer", "nullable": False},
            {"name": "low", "type": "string", "nullable": True},
            {"name": "runs", "type": "string", "nullable": True},
            {"name": "high", "type": "string", "nullable": True},
        ]
        rows = []
        for i in range(60):
            rows.append({
                "k": i,
                "low": ["alpha", "beta", "alpha", "gamma"][i % 4],
                "runs": "run" if i < 50 else "other",
                "high": "v%04d-%s" % (i, "z" * (i % 7)),
            })
        src = write_jsonl(self.path("enc.jsonl"), rows)
        store = self.path("enc")
        build(src, store, make_schema(schema), row_group_max=60)
        cat, decoded = read_all(store)
        encodings = [c["encoding"] for c in cat["groups"][0]["columns"]]
        self.assertEqual(encodings[0], "int64-plain")
        self.assertEqual(encodings[2], "str-dict-rle")
        self.assertEqual(encodings[3], "str-prefix")
        self.assertEqual(decoded, rows)
