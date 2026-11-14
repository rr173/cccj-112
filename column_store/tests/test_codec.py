"""Encoding tests: nulls, integer/boolean layouts, and string codecs."""

import struct

import support  # noqa: F401  (sets sys.path)

from column_store import codec
from column_store.catalog import _HEADER  # reuse only for field names in a
# deliberately independent validation below?  No: tests build pages through
# the public codec and re-check them with a hand-built reader.


def build_page(type_, values):
    raw, encoding, nul = codec.encode_page(type_, values)
    return raw, encoding, nul


def repair_checksums(raw):
    """Recompute page CRC fields so a semantic check, not the CRC, decides."""
    import zlib
    raw = bytearray(raw)
    null_bytes = struct.unpack_from(">I", raw, 12)[0]
    body_len = struct.unpack_from(">Q", raw, 16)[0]
    body = bytes(raw[32:32 + null_bytes + body_len])
    struct.pack_into(">I", raw, 24, zlib.crc32(body) & 0xFFFFFFFF)
    struct.pack_into(">I", raw, 28,
                     zlib.crc32(bytes(raw[:28])) & 0xFFFFFFFF)
    return bytes(raw)


def hand_decode_header(raw):
    magic, kind, version, flags, reserved, rows, null_bytes, body_len, \
        body_crc, header_crc = struct.unpack(">4sBBBBIIQII", raw[:32])
    return dict(magic=magic, kind=kind, version=version, flags=flags,
                reserved=reserved, rows=rows, null_bytes=null_bytes,
                body_len=body_len, body_crc=body_crc,
                header_crc=header_crc)


class NullBitmapTests(__import__("unittest").TestCase):
    def test_bitmap_positions(self):
        values = [None, 1, None, 2, 3, None, 4, None, None]
        raw, encoding, nul = build_page("integer", values)
        header = hand_decode_header(raw)
        self.assertEqual(header["rows"], 9)
        self.assertEqual(header["null_bytes"], 2)
        bitmap = raw[32:34]
        want = bytearray(2)
        for i in (0, 2, 5, 7, 8):
            want[i >> 3] |= 1 << (i & 7)
        self.assertEqual(bitmap, bytes(want))
        self.assertEqual(nul, 5)
        self.assertEqual(codec.decode_page(raw, 0, "integer", 9).values,
                         values)

    def test_null_in_one_column_does_not_shift_another(self):
        # Row 1 is null in "b" but present in "a": "a" must keep value 2 at
        # row index 1, not slide to index 0.
        a = [1, 2, 3]
        b = [10, None, 30]
        ra, ea, _ = build_page("integer", a)
        rb, eb, _ = build_page("integer", b)
        da = codec.decode_page(ra, 0, "integer", 3)
        db = codec.decode_page(rb, 0, "integer", 3)
        self.assertEqual(da.values, [1, 2, 3])
        self.assertEqual(db.values, [10, None, 30])

    def test_all_null_group_has_zero_length_body(self):
        values = [None] * 5
        raw, encoding, nul = build_page("string", values)
        header = hand_decode_header(raw)
        self.assertEqual(header["body_len"], 0)
        self.assertEqual(header["null_bytes"], 1)
        self.assertEqual(header["flags"] & codec.FLAG_ALL_NULL,
                         codec.FLAG_ALL_NULL)
        page = codec.decode_page(raw, 0, "string", 5, encoding)
        self.assertEqual(page.values, values)

    def test_all_null_boolean_body_is_empty(self):
        raw, encoding, nul = build_page("boolean", [None, None, None])
        header = hand_decode_header(raw)
        self.assertEqual(header["body_len"], 0)
        self.assertEqual(header["null_bytes"], 1)

    def test_decode_rejects_bitmap_row_mismatch(self):
        raw, _, _ = build_page("integer", [1, 2, 3])
        # Claim 4 rows: null bitmap and row count no longer fit.
        with self.assertRaises(Exception):
            codec.decode_page(raw, 0, "integer", 4)


class IntegerTests(__import__("unittest").TestCase):
    def test_fixed_width_big_endian(self):
        values = [0, -1, 1, -(2 ** 63), 2 ** 63 - 1, None, 300]
        raw, encoding, _ = build_page("integer", values)
        self.assertEqual(encoding, "int64-plain")
        body = raw[32 + 1 + 8:]  # skip header + 1 bitmap byte
        body = raw[32 + 1:]
        self.assertEqual(struct.unpack_from(">q", body, 0)[0], 0)
        self.assertEqual(struct.unpack_from(">q", body, 8)[0], -1)
        self.assertEqual(struct.unpack_from(">q", body, 40)[0], 300)

    def test_round_trip_extremes(self):
        values = [-(2 ** 63), 2 ** 63 - 1, 0, -1] * 4
        raw, _, _ = build_page("integer", values)
        self.assertEqual(codec.decode_page(raw, 0, "integer",
                                           len(values)).values, values)

    def test_body_length_is_checked(self):
        values = [1, 2, 3]
        raw, _, _ = build_page("integer", values)
        short = raw[:32 + 1 + 8]  # only one value
        with self.assertRaises(Exception):
            codec.decode_page(short, 0, "integer", 3)


class BooleanTests(__import__("unittest").TestCase):
    def test_packed_lsb_first(self):
        values = [True, False, True, True, False, False, False, True,
                  False, True, None]
        raw, encoding, nul = build_page("boolean", values)
        self.assertEqual(encoding, "bool-bitmap")
        bitmap_len = 2
        body = raw[32 + bitmap_len:]
        nonnull = [v for v in values if v is not None]
        want = bytearray(2)
        for i, v in enumerate(nonnull):
            if v:
                want[i >> 3] |= 1 << (i & 7)
        self.assertEqual(body, bytes(want))
        self.assertEqual(codec.decode_page(raw, 0, "boolean",
                                           len(values)).values, values)


class StringEncodingChoiceTests(__import__("unittest").TestCase):
    def test_low_cardinality_uses_dictionary(self):
        values = ["Norte", "Sul", "Norte", "Leste", "Norte", "Sul"] * 10
        raw, encoding, _ = build_page("string", values)
        self.assertIn(encoding, ("str-dict", "str-dict-rle"))
        page = codec.decode_page(raw, 0, "string", len(values), encoding)
        self.assertEqual(page.values, values)

    def test_high_cardinality_uses_prefix(self):
        values = ["unique-value-%05d-%s" % (i, "q" * 12) for i in range(200)]
        raw, encoding, _ = build_page("string", values)
        self.assertEqual(encoding, "str-prefix")

    def test_runs_use_rle(self):
        values = ["same"] * 100
        raw, encoding, _ = build_page("string", values)
        self.assertEqual(encoding, "str-dict-rle")
        # The RLE body must really be compressed: dictionary entry plus one
        # run, not 100 indices.
        self.assertLess(len(raw), 32 + 16 + 30)

    def test_tie_prefers_dictionary(self):
        # Construct candidates directly to test the documented precedence.
        chosen, name = codec.choose_string_encoding([
            ("str-prefix", b"aaaa"),
            ("str-dict", b"bbbb"),
            ("str-dict-rle", b"cccc"),
        ])
        self.assertEqual(name, "str-dict")
        chosen, name = codec.choose_string_encoding([
            ("str-prefix", b"aaaa"),
            ("str-dict-rle", b"cccc"),
        ])
        self.assertEqual(name, "str-dict-rle")
        chosen, name = codec.choose_string_encoding([
            ("str-prefix", b"aa"),
            ("str-dict", b"bbbb"),
        ])
        self.assertEqual(name, "str-prefix")

    def test_dictionary_index_widths_round_trip(self):
        for n in (1, 256, 300, 65536, 70000):
            with self.subTest(n=n):
                vals = ["id-%06d" % i for i in range(n)]
                body = codec._dict_body(vals, False)
                width = body[0]
                want = 1 if n <= 256 else (2 if n <= 65536 else 4)
                self.assertEqual(width, want)
                # Build the page through the public decoder with the manifest
                # naming "str-dict", regardless of what the chooser would
                # pick; every width must decode.
                bitmap, nul = codec.null_bitmap(vals)
                header = codec._HEADER.pack(
                    codec.PAGE_MAGIC, codec.KIND_STRING,
                    codec.PAGE_VERSION, 0, 0, n, len(bitmap), len(body),
                    __import__("zlib").crc32(bitmap + body) & 0xFFFFFFFF, 0)
                header = header[:28] + struct.pack(
                    ">I", __import__("zlib").crc32(header[:28]) & 0xFFFFFFFF)
                page = header + bitmap + body
                self.assertEqual(
                    codec.decode_page(page, 0, "string", n, "str-dict").values,
                    vals)

    def test_empty_and_zero_bytes_preserved(self):
        values = ["", "\x00", "a\x00b", "\x00\x00", "é", "😀"]
        raw, encoding, _ = build_page("string", values)
        page = codec.decode_page(raw, 0, "string", len(values), encoding)
        self.assertEqual(page.values, values)

    def test_dictionary_index_out_of_range_rejected(self):
        values = ["alpha-" + "x" * 20, "beta-" + "y" * 20] * 10
        raw, encoding, _ = build_page("string", values)
        self.assertEqual(encoding, "str-dict")
        # Find and corrupt the index bytes: layout is
        # header(32) + bitmap(1) + u8 width + u32 count + entries + indices.
        pos = 32 + 3 + 5  # header + 3 bitmap bytes (20 rows) + dict header
        for _ in range(2):
            ln = struct.unpack_from(">I", raw, pos)[0]
            pos += 4 + ln
        mutated = bytearray(raw)
        mutated[pos] = 99
        with self.assertRaises(Exception) as ctx:
            codec.decode_page(repair_checksums(mutated), 0, "string",
                              len(values), encoding)
        self.assertIn("out of range", str(ctx.exception))

    def test_rle_run_length_mismatch_rejected(self):
        values = ["same"] * 100
        raw, encoding, _ = build_page("string", values)
        self.assertEqual(encoding, "str-dict-rle")
        mutated = bytearray(raw)
        mutated[-1] = 9  # shrink the only run from 100 to 9 rows
        with self.assertRaises(Exception) as ctx:
            codec.decode_page(repair_checksums(mutated), 0, "string", 100,
                              encoding)
        self.assertIn("cover", str(ctx.exception))

    def test_prefix_trailing_bytes_rejected(self):
        values = ["a", "b"]
        raw, encoding, _ = build_page("string", values)
        self.assertEqual(encoding, "str-prefix")
        with self.assertRaises(Exception):
            codec.decode_page(raw + b"junk", 0, "string", 2, encoding)


class PageChecksumTests(__import__("unittest").TestCase):
    def test_header_and_body_checksums_detect_flips(self):
        values = [1, 2, 3, 4, 5]
        raw, encoding, _ = build_page("integer", values)
        for pos in (0, 10, 30, 33, 60):
            with self.subTest(pos=pos):
                mutated = bytearray(raw)
                mutated[pos] ^= 0x01
                with self.assertRaises(Exception):
                    codec.decode_page(bytes(mutated), 0, "integer",
                                      len(values))
