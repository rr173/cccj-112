"""Schema and source-record validation: build must reject bad input lines."""

import json
import os

from support import StoreCase, write_jsonl

from column_store.builder import build
from column_store.errors import InputError, OutputRefused, SchemaError
from column_store.schema import load_schema, make_schema

SCHEMA = [
    {"name": "i", "type": "integer", "nullable": False},
    {"name": "s", "type": "string", "nullable": True},
    {"name": "b", "type": "boolean", "nullable": False},
]


class SchemaTests(StoreCase):
    def test_order_and_names_are_preserved(self):
        schema = make_schema([
            {"name": "first name", "type": "string", "nullable": True},
            {"name": "x-y (z)", "type": "integer", "nullable": False},
            {"name": "naïve☃", "type": "boolean", "nullable": False},
        ])
        self.assertEqual([c.name for c in schema],
                         ["first name", "x-y (z)", "naïve☃"])
        self.assertEqual([c.type for c in schema],
                         ["string", "integer", "boolean"])

    def test_duplicate_names_rejected(self):
        with self.assertRaises(SchemaError):
            make_schema([
                {"name": "a", "type": "integer", "nullable": False},
                {"name": "a", "type": "string", "nullable": False},
            ])

    def test_bad_declarations(self):
        cases = [
            [{"name": "a", "type": "float", "nullable": False}],
            [{"name": "", "type": "integer", "nullable": False}],
            [{"name": "a", "type": "integer"}],
            [{"name": "a", "type": "integer", "nullable": "yes"}],
            [{"name": "a", "type": "integer", "nullable": False,
              "extra": 1}],
            [{"name": "a\x00b", "type": "integer", "nullable": False}],
            [],
        ]
        for doc in cases:
            with self.assertRaises(SchemaError, msg=repr(doc)):
                make_schema(doc)

    def test_schema_file_must_be_utf8_json(self):
        path = self.path("schema.json")
        with open(path, "wb") as fh:
            fh.write(b'{"columns": [{"name": "\xff", "type": "integer", '
                     b'"nullable": false}]}')
        with self.assertRaises(SchemaError):
            load_schema(path)


class RecordValidationTests(StoreCase):
    def build_line(self, line, name="bad"):
        src = self.path(name + ".jsonl")
        with open(src, "wb") as fh:
            fh.write(line + b"\n")
        return src, self.path(name)

    def assert_rejected(self, line, needle, schema_doc=None):
        schema = make_schema(schema_doc or SCHEMA)
        src, store = self.build_line(line)
        with self.assertRaises(InputError) as ctx:
            build(src, store, schema)
        self.assertIn(needle, str(ctx.exception))
        self.assertEqual(ctx.exception.line_no, 1)
        self.assertFalse(os.path.lexists(store),
                         "a failed build must not leave a store")
        return ctx.exception

    def test_valid_record_and_line_number_reported(self):
        schema = make_schema(SCHEMA)
        src = self.path("ok.jsonl")
        write_jsonl(src, [{"i": 1, "s": None, "b": True}])
        build(src, self.path("ok"), schema)
        bad = self.path("mix.jsonl")
        with open(bad, "wb") as fh:
            fh.write(b'{"i": 1, "s": "", "b": false}\n')
            fh.write(b'{"i": 2, "s": 3, "b": false}\n')
        with self.assertRaises(InputError) as ctx:
            build(bad, self.path("mix"), schema)
        self.assertEqual(ctx.exception.line_no, 2)
        self.assertIn("expects a string", str(ctx.exception))

    def test_missing_and_extra_fields(self):
        self.assert_rejected(b'{"i": 1, "s": "x"}', "missing declared field")
        self.assert_rejected(b'{"i": 1, "s": "x", "b": false, "z": 0}',
                             "not declared")

    def test_duplicate_object_key(self):
        self.assert_rejected(b'{"i": 1, "i": 2, "s": "x", "b": false}',
                             "duplicate field 'i'")

    def test_boolean_is_not_an_integer(self):
        exc = self.assert_rejected(b'{"i": true, "s": "x", "b": false}',
                                   "expects an integer")
        self.assertIn("boolean", str(exc))

    def test_integer_is_not_a_boolean(self):
        self.assert_rejected(b'{"i": 1, "s": "x", "b": 0}', "expects a boolean")

    def test_integer_bounds(self):
        schema = make_schema(SCHEMA)
        src, store = self.build_line(
            b'{"i": 9223372036854775808, "s": "x", "b": false}')
        with self.assertRaises(InputError) as ctx:
            build(src, store, schema)
        self.assertEqual(ctx.exception.line_no, 1)
        self.assertIn("signed 64-bit range", str(ctx.exception))
        src2, store2 = self.build_line(
            b'{"i": -9223372036854775809, "s": "x", "b": false}',
            name="low")
        with self.assertRaises(InputError):
            build(src2, store2, schema)

    def test_int64_extremes_accepted(self):
        schema = make_schema(SCHEMA)
        src = self.path("extremes.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 9223372036854775807, "s": null, "b": true}\n')
            fh.write(b'{"i": -9223372036854775808, "s": null, "b": true}\n')
        build(src, self.path("extremes"), schema)

    def test_non_integer_numbers_rejected(self):
        for literal in (b"1.0", b"1e3", b"1E3", b"-0.5", b"01"):
            with self.subTest(literal=literal):
                schema = make_schema(SCHEMA)
                line = (b'{"i": ' + literal + b', "s": "x", "b": false}')
                src, store = self.build_line(line, name="num")
                with self.assertRaises(InputError):
                    build(src, store, schema)

    def test_null_in_non_nullable_column(self):
        self.assert_rejected(b'{"i": null, "s": "x", "b": false}',
                             "not nullable")

    def test_invalid_utf8(self):
        exc = self.assert_rejected(b'{"i": 1, "s": "\xff\xfe", "b": false}',
                                   "invalid UTF-8")
        self.assertIn("byte offset", str(exc))

    def test_lone_surrogate_escape(self):
        self.assert_rejected(
            b'{"i": 1, "s": "\\ud800", "b": false}', "surrogate")

    def test_not_an_object(self):
        self.assert_rejected(b'[1, 2, 3]', "record must be a JSON object")

    def test_trailing_garbage(self):
        self.assert_rejected(b'{"i": 1, "s": "x", "b": false} x',
                             "trailing data")

    def test_empty_line(self):
        self.assert_rejected(b"", "empty source line")

    def test_string_bound(self):
        schema = make_schema(SCHEMA)
        src = self.path("long.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 1, "s": "' + b"a" * 200 + b'", "b": false}\n')
        with self.assertRaises(InputError) as ctx:
            build(src, self.path("long"), schema, max_string_bytes=100)
        self.assertIn("size bound", str(ctx.exception))

    def test_string_bound_counts_decoded_utf8_bytes(self):
        # "\ud83d\ude00" is 12 source bytes but 4 UTF-8 bytes.  The bound
        # applies to the decoded string, so 4 is accepted and 3 is not.
        schema = make_schema(SCHEMA)
        src = self.path("emoji.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 1, "s": "\ud83d\ude00", "b": false}\n')
        build(src, self.path("emoji4"), schema, max_string_bytes=4,
              max_line_bytes=4 + 100)
        with self.assertRaises(InputError) as ctx:
            build(src, self.path("emoji3"), schema, max_string_bytes=3,
                  max_line_bytes=4 + 100)
        self.assertIn("4 bytes > 3", str(ctx.exception))

    def test_line_bound(self):
        schema = make_schema(SCHEMA)
        src = self.path("lines.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 1, "s": "' + b"a" * 500 + b'", "b": false}\n')
        with self.assertRaises(InputError) as ctx:
            build(src, self.path("lines"), schema, max_line_bytes=200,
                  max_string_bytes=200)
        self.assertIn("line exceeds", str(ctx.exception))

    def test_strings_are_verbatim(self):
        schema = make_schema(SCHEMA)
        src = self.path("str.jsonl")
        values = ["", "a\x00b", "  padded  ", "ÄÖÜ", "é", "😀",
                  "  ", "tab\there"]
        with open(src, "w", encoding="utf-8", newline="\n") as fh:
            for i, v in enumerate(values):
                fh.write(json.dumps({"i": i, "s": v, "b": False},
                                    ensure_ascii=False) + "\n")
        store = self.path("str")
        build(src, store, schema)
        from column_store.scanner import scan
        got, _ = scan(store, ["s"], [])
        self.assertEqual([r[0] for r in got], values)

    def test_escaped_unicode_is_decoded_not_kept_escaped(self):
        schema = make_schema(SCHEMA)
        src = self.path("esc.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 1, "s": "\\u00e9\\ud83d\\ude00", "b": false}\n')
        store = self.path("esc")
        build(src, store, schema)
        from column_store.scanner import scan
        self.assertEqual(scan(store, ["s"], [])[0], [("é😀",)])


class OutputSafetyTests(StoreCase):
    def test_existing_store_is_never_overwritten(self):
        schema = make_schema(SCHEMA)
        src = self.path("s.jsonl")
        write_jsonl(src, [{"i": 1, "s": "x", "b": True}])
        store = self.path("keep")
        build(src, store, schema)
        before = {}
        for name in os.listdir(store):
            p = os.path.join(store, name)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    before[name] = fh.read()
        with self.assertRaises(OutputRefused):
            build(src, store, schema)
        for name in os.listdir(store):
            p = os.path.join(store, name)
            if os.path.isfile(p):
                with open(p, "rb") as fh:
                    self.assertEqual(fh.read(), before[name])

    def test_failed_build_leaves_no_temporary_directory(self):
        schema = make_schema(SCHEMA)
        src = self.path("bad.jsonl")
        with open(src, "wb") as fh:
            fh.write(b'{"i": 1, "s": "x", "b": false}\n')
            fh.write(b'{"i": "no", "s": "x", "b": false}\n')
        parent = self.path("out")
        os.makedirs(parent)
        with self.assertRaises(InputError):
            build(src, os.path.join(parent, "store"), schema)
        self.assertEqual(os.listdir(parent), [])
