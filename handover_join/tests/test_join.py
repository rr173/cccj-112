"""Functional tests for the external left-join job.

The expected output of every case is built independently by the naive nested
loop implementation in ``naive_reference.py`` and compared against the
production output *including order*, exact row numbers and full payloads.
Sort chunk size and merge fan-in are deliberately tiny so multi-round merge
waves are exercised by ordinary, fast regression cases.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from handover_join.errors import InputDecodeError, KeyError_  # noqa: E402
from handover_join.job import run_join  # noqa: E402
from handover_join.keycode import validate_config  # noqa: E402

from naive_reference import load_output, naive_join  # noqa: E402

BIG = 10**40 + 17  # far beyond 64-bit range


def write_jsonl(path, rows):
    with open(path, "wb") as fh:
        for row in rows:
            fh.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            fh.write(b"\n")


def write_config(path, left_keys, right_keys, types):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "left_keys": left_keys,
                "right_keys": right_keys,
                "key_types": types,
            },
            fh,
        )


# Defaults chosen to force several spill runs and several merge waves even on
# tiny inputs: chunk_size=2, fan_in=2 means 8+ rows already need 3 waves.
TINY = dict(chunk_size=2, fan_in=2, pair_chunk_size=2)


class JoinCaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hj_test_")
        self.left_path = os.path.join(self.tmp, "left.jsonl")
        self.right_path = os.path.join(self.tmp, "right.jsonl")
        self.config_path = os.path.join(self.tmp, "config.json")
        self.out_path = os.path.join(self.tmp, "out.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, **overrides):
        params = dict(TINY)
        params.update(overrides)
        return run_join(
            self.left_path,
            self.right_path,
            self.config_path,
            self.out_path,
            work_dir=self.tmp,
            **params,
        )

    def assert_matches_naive(self, stats, expected_pairs=None):
        with open(self.config_path, encoding="utf-8") as fh:
            components = validate_config(json.load(fh))
        expected = naive_join(self.left_path, self.right_path, components)
        actual = load_output(self.out_path)
        self.assertEqual(actual, expected)  # full compare incl. order & payloads
        self.assertEqual(len(actual), len(expected))
        if expected_pairs is not None:
            self.assertEqual(stats["output_pairs"], expected_pairs)
        self.assertEqual(stats["output_pairs"], len(expected))


class TestManyToManyAndBasics(JoinCaseBase):
    def config(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])

    def test_many_to_many_nulls_duplicates_and_bigints(self):
        self.config()
        left = [
            {"id": 1, "note": "a"},
            {"id": 2, "note": "b"},          # unmatched left
            {"id": 1, "note": "a-dup"},       # same key, independent row
            {"id": None, "note": "null-key"},
            {"note": "missing-key"},
            {"id": BIG, "note": "huge"},
            {"id": 3, "note": "c"},
        ]
        right = [
            {"reg_id": 1, "reg": "r1"},
            {"reg_id": 1, "reg": "r1-again"},  # duplicate registration kept
            {"reg_id": 1, "reg": "r1-third"},
            {"reg_id": 9, "reg": "right-only"},
            {"reg_id": None, "reg": "null-reg"},
            {"reg_id": BIG, "reg": "huge-reg"},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        # 3 matches for row1, 1 for row3, 1 for row6, plus 4 unmatched lefts
        self.assert_matches_naive(stats)
        self.assertGreaterEqual(stats["left_waves"], 1)
        self.assertGreaterEqual(stats["right_waves"], 1)
        # big integer survives with full precision
        actual = load_output(self.out_path)
        huge_pairs = [r for r in actual if r["left_row"] == 6]
        self.assertEqual(len(huge_pairs), 1)
        self.assertEqual(huge_pairs[0]["left"]["id"], BIG)
        self.assertEqual(huge_pairs[0]["right"]["reg_id"], BIG)
        # unmatched rows carry null right side and null right_row
        unmatched = {r["left_row"]: r for r in actual if r["right_row"] is None}
        for lnum in (2, 4, 5):
            self.assertIn(lnum, unmatched)
            self.assertIsNone(unmatched[lnum]["right"])
        # right-only row never appears
        self.assertFalse(
            any(r["right"] and r["right"].get("reg") == "right-only" for r in actual)
        )

    def test_both_sides_empty_keys_do_not_match(self):
        self.config()
        left = [{"id": None, "x": 1}, {"x": 2}]
        right = [{"reg_id": None, "y": 9}]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        self.assertEqual([r["right_row"] for r in actual], [None, None])

    def test_identical_duplicate_rows_are_independent(self):
        self.config()
        row = {"id": 7, "payload": ["same", {"nested": 1}]}
        left = [dict(row), dict(row), dict(row)]
        right = [dict(reg_id=7, reg="same"), dict(reg_id=7, reg="same")]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        # 3 left x 2 right = 6 pairs even though all inputs are identical
        self.assertEqual(stats["output_pairs"], 6)
        self.assert_matches_naive(stats)


class TestStringSemantics(JoinCaseBase):
    def test_delimiter_strings_and_verbatim_equality(self):
        write_config(self.config_path, ["code"], ["reg_code"], ["string"])
        left = [
            {"code": "A|B|C", "v": 1},          # contains delimiter characters
            {"code": "A|B", "v": 2},
            {"code": "A|B|C ", "v": 3},         # trailing space -> different
            {"code": "a|b|c", "v": 4},          # different case -> different
            {"code": "", "v": 5},               # empty string is a real key
        ]
        right = [
            {"reg_code": "A|B|C", "r": 10},
            {"reg_code": "A|B", "r": 20},
            {"reg_code": "A|B|C|", "r": 30},
            {"reg_code": "", "r": 50},
            {"reg_code": "A|B|C", "r": 11},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        # "A|B|C" must not collide with "A|B|C|" or "A|B"
        abc = [r for r in actual if r["left_row"] == 1]
        self.assertEqual(sorted(r["right_row"] for r in abc), [1, 5])
        self.assertEqual(
            [r["left_row"] for r in actual if r["right_row"] is None],
            [3, 4],
        )


class TestCompositeKeys(JoinCaseBase):
    def test_composite_key_component_wise(self):
        write_config(
            self.config_path,
            ["k1", "k2"],
            ["r1", "r2"],
            ["string", "integer"],
        )
        left = [
            {"k1": "x", "k2": 1},
            {"k1": "x", "k2": 2},
            {"k1": "y", "k2": 1},
            {"k1": "x", "k2": None},
            {"k1": "x", "k2": 1, "extra": [1, 2, {"z": "中"}]},
        ]
        right = [
            {"r1": "x", "r2": 1},
            {"r1": "x", "r2": 1},
            {"r1": "x", "r2": 2},
            {"r1": "y", "r2": 2},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        # rows 1 and 5 both match the two (x,1) registrations -> 4 pairs
        x1_pairs = [
            r for r in actual if r["left_row"] in (1, 5) and r["right_row"]
        ]
        self.assertEqual(len(x1_pairs), 4)
        # null component -> unmatched
        self.assertIn(
            4, [r["left_row"] for r in actual if r["right_row"] is None]
        )

    def test_delimiter_join_collision_is_impossible(self):
        # With a naive "a|b" concatenation these two keys collide:
        # ("a", "b|c") -> "a|b|c" == ("a|b", "c") -> "a|b|c".
        # Component-wise encoding must keep them apart.
        write_config(
            self.config_path,
            ["k1", "k2"],
            ["r1", "r2"],
            ["string", "string"],
        )
        left = [
            {"k1": "a", "k2": "b|c", "tag": "L1"},
            {"k1": "a|b", "k2": "c", "tag": "L2"},
        ]
        right = [
            {"r1": "a", "r2": "b|c", "tag": "R1"},
            {"r1": "a|b", "r2": "c", "tag": "R2"},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        self.assertEqual(stats["output_pairs"], 2)
        self.assertEqual(
            [(r["left_row"], r["right_row"]) for r in actual],
            [(1, 1), (2, 2)],
        )

    def test_integer_concatenation_collision_is_impossible(self):
        # Decimal concatenation would collide: (1, 23) -> "123" == (12, 3).
        write_config(
            self.config_path,
            ["k1", "k2"],
            ["r1", "r2"],
            ["integer", "integer"],
        )
        left = [{"k1": 1, "k2": 23}, {"k1": 12, "k2": 3}]
        right = [{"r1": 1, "r2": 23}, {"r1": 12, "r2": 3}]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        self.assertEqual(
            [(r["left_row"], r["right_row"]) for r in actual],
            [(1, 1), (2, 2)],
        )

    def test_three_component_key_different_field_names(self):
        write_config(
            self.config_path,
            ["a", "b", "c"],
            ["A", "B", "C"],
            ["integer", "string", "string"],
        )
        left = [
            {"a": 1, "b": "p", "c": "q"},
            {"a": 1, "b": "p", "c": "Q"},
            {"a": 1, "b": "P", "c": "q"},
        ]
        right = [
            {"A": 1, "B": "p", "C": "q"},
            {"A": 1, "B": "p", "C": "q"},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job()
        self.assert_matches_naive(stats)


class TestNestedPayloads(JoinCaseBase):
    def test_nested_payloads_round_trip_without_stringification(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        left = [
            {
                "id": 1,
                "meta": {"n": BIG, "f": 1.5, "b": True, "nil": None,
                         "list": [1, "2", {"x": 3}]},
                "utf8": "观测交接包",
            }
        ]
        right = [
            {"reg_id": 1, "items": [{"k": "v"}, None, -BIG, 0, 1e100]},
        ]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        self.run_job()
        actual = load_output(self.out_path)
        with open(self.config_path, encoding="utf-8") as fh:
            components = validate_config(json.load(fh))
        expected = naive_join(
            self.left_path,
            self.right_path,
            components,
        )
        self.assertEqual(actual, expected)
        self.assertIsInstance(actual[0]["left"]["meta"]["n"], int)
        self.assertIsInstance(actual[0]["right"]["items"][2], int)
        self.assertEqual(actual[0]["right"]["items"][2], -BIG)
        self.assertIsNone(actual[0]["left"]["meta"]["nil"])


class TestEmptyInputs(JoinCaseBase):
    def config(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])

    def test_empty_left_emits_nothing(self):
        self.config()
        write_jsonl(self.left_path, [])
        write_jsonl(self.right_path, [{"reg_id": 1}, {"reg_id": 2}])
        stats = self.run_job()
        self.assertEqual(stats["output_pairs"], 0)
        self.assertEqual(stats["left_rows"], 0)
        self.assert_matches_naive(stats)
        self.assertEqual(load_output(self.out_path), [])

    def test_empty_right_keeps_every_left_row_once(self):
        self.config()
        write_jsonl(self.left_path, [{"id": 1}, {"id": 2}, {"id": 1}])
        write_jsonl(self.right_path, [])
        stats = self.run_job()
        self.assertEqual(stats["output_pairs"], 3)
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        self.assertEqual([r["right_row"] for r in actual], [None, None, None])
        self.assertEqual([r["left_row"] for r in actual], [1, 2, 3])

    def test_both_empty(self):
        self.config()
        write_jsonl(self.left_path, [])
        write_jsonl(self.right_path, [])
        stats = self.run_job()
        self.assertEqual(stats["output_pairs"], 0)
        self.assert_matches_naive(stats)


class TestTypeErrors(JoinCaseBase):
    def test_wrong_type_reports_side_and_row(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        write_jsonl(self.left_path, [{"id": 1}, {"id": "2"}, {"id": 3}])
        write_jsonl(self.right_path, [{"reg_id": 1}])
        with self.assertRaises(KeyError_) as ctx:
            self.run_job()
        self.assertEqual(ctx.exception.side, "left")
        self.assertEqual(ctx.exception.row, 2)
        self.assertEqual(ctx.exception.field, "id")

    def test_boolean_is_not_integer(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        write_jsonl(self.left_path, [{"id": True}])
        write_jsonl(self.right_path, [{"reg_id": 1}])
        with self.assertRaises(KeyError_) as ctx:
            self.run_job()
        self.assertEqual(ctx.exception.row, 1)

    def test_string_field_given_object_on_right(self):
        write_config(self.config_path, ["code"], ["reg_code"], ["string"])
        write_jsonl(self.left_path, [{"code": "x"}])
        write_jsonl(
            self.right_path,
            [{"reg_code": "x"}, {"reg_code": ["nope"]}],
        )
        with self.assertRaises(KeyError_) as ctx:
            self.run_job()
        self.assertEqual(ctx.exception.side, "right")
        self.assertEqual(ctx.exception.row, 2)

    def test_invalid_json_reports_physical_row(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        with open(self.left_path, "wb") as fh:
            fh.write(b'{"id": 1}\n{"id": 2}\nnot-json\n')
        write_jsonl(self.right_path, [{"reg_id": 1}])
        with self.assertRaises(InputDecodeError) as ctx:
            self.run_job()
        self.assertEqual(ctx.exception.side, "left")
        self.assertEqual(ctx.exception.row, 3)

    def test_crlf_line_endings_and_float_is_not_integer(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        with open(self.left_path, "wb") as fh:
            fh.write(b'{"id": 1}\r\n{"id": 2.0}\r\n')
        write_jsonl(self.right_path, [{"reg_id": 1}])
        with self.assertRaises(KeyError_) as ctx:
            self.run_job()
        # The float on physical row 2 must be rejected, not coerced.
        self.assertEqual(ctx.exception.row, 2)
        self.assertEqual(ctx.exception.side, "left")

    def test_crlf_well_formed_input_joins_normally(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["string"])
        with open(self.left_path, "wb") as fh:
            fh.write(b'{"id": "a"}\r\n{"id": "b"}\r\n')
        with open(self.right_path, "wb") as fh:
            fh.write(b'{"reg_id": "a"}\r\n')
        stats = self.run_job()
        self.assert_matches_naive(stats)


class TestMergeWavesAndOrdering(JoinCaseBase):
    def test_forced_multi_round_merge_and_final_order(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        # 30 rows with chunk_size=2 -> 15 runs; fan_in=2 -> 4 merge waves.
        left = [{"id": (i * 7) % 11, "seq": i} for i in range(30)]
        right = [{"reg_id": j, "r": j} for j in range(11)]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job(chunk_size=2, fan_in=2, pair_chunk_size=3)
        self.assertEqual(stats["left_runs"], 15)
        self.assertGreaterEqual(stats["left_waves"], 3)
        self.assertGreaterEqual(stats["right_waves"], 2)
        self.assert_matches_naive(stats)
        actual = load_output(self.out_path)
        # strict (left_row, right_row) ordering with null right row last
        keys = [
            (r["left_row"], r["right_row"] if r["right_row"] is not None else 10**9)
            for r in actual
        ]
        self.assertEqual(keys, sorted(keys))

    def test_larger_fan_in_still_correct(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        left = [{"id": i % 5} for i in range(50)]
        right = [{"reg_id": i % 5, "r": i} for i in range(20)]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job(chunk_size=3, fan_in=5)
        self.assert_matches_naive(stats)


class TestByteBudgetSpill(JoinCaseBase):
    """A large payload forces a spill even when chunk_size would allow more."""

    def test_chunk_bytes_bounds_buffered_payload(self):
        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        # Rows are large but few: chunk_size alone would keep all of them in
        # one chunk; the byte budget must force one run per oversized row.
        big = "z" * 4000
        left = [{"id": i % 3, "pad": big} for i in range(6)]
        right = [{"reg_id": i, "pad": big} for i in range(3)]
        write_jsonl(self.left_path, left)
        write_jsonl(self.right_path, right)
        stats = self.run_job(
            chunk_size=1000,
            pair_chunk_size=1000,
            fan_in=2,
            chunk_bytes=2048,
            pair_chunk_bytes=2048,
        )
        # Every frame is > 4 KiB, so each of the 6 left rows spills its own run.
        self.assertEqual(stats["left_runs"], 6)
        self.assertGreaterEqual(stats["left_waves"], 2)
        self.assert_matches_naive(stats)


class TestFileHandleBound(unittest.TestCase):
    """The number of open run files must never grow with the run count."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hj_fd_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_files_bounded_by_fan_in(self):
        left_path = os.path.join(self.tmp, "left.jsonl")
        right_path = os.path.join(self.tmp, "right.jsonl")
        config_path = os.path.join(self.tmp, "config.json")
        out_path = os.path.join(self.tmp, "out.jsonl")
        write_config(config_path, ["id"], ["reg_id"], ["integer"])
        write_jsonl(left_path, [{"id": i % 4} for i in range(60)])
        write_jsonl(right_path, [{"reg_id": i % 4} for i in range(40)])

        import builtins

        real_open = builtins.open
        peak = 0
        live = 0

        def counting_open(file, *args, **kwargs):
            nonlocal peak, live
            handle = real_open(file, *args, **kwargs)
            live += 1
            peak = max(peak, live)

            def on_close(*a, **kw):
                nonlocal live
                live -= 1
                return handle._real_close(*a, **kw)

            handle._real_close = handle.close
            handle.close = on_close
            return handle

        fan_in = 2
        builtins.open = counting_open
        try:
            stats = run_join(
                left_path,
                right_path,
                config_path,
                out_path,
                work_dir=self.tmp,
                chunk_size=2,
                fan_in=fan_in,
                pair_chunk_size=2,
            )
        finally:
            builtins.open = real_open
        # 60 rows / chunk 2 -> 30 runs each, but at most fan_in run files are
        # open concurrently, plus the input/output handles of the stage.
        self.assertGreaterEqual(stats["left_runs"], fan_in * 4)
        self.assertLessEqual(peak, fan_in + 3)


class TestCliAndCleanup(JoinCaseBase):
    def test_cli_runs_and_reports_error_code(self):
        from handover_join.cli import main as cli_main
        import contextlib
        import io

        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        write_jsonl(self.left_path, [{"id": 1}, {"id": 2}])
        write_jsonl(self.right_path, [{"reg_id": 1}])
        work = os.path.join(self.tmp, "scratch")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli_main(
            [
                "--left", self.left_path,
                "--right", self.right_path,
                "--config", self.config_path,
                "--output", self.out_path,
                "--work-dir", work,
                "--chunk-size", "1",
                "--fan-in", "2",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.out_path))
        # The job-owned scratch directory is removed on success.
        self.assertEqual(os.listdir(work), [])

    def test_cli_type_error_exit_code_and_message(self):
        from handover_join.cli import main as cli_main
        import io
        import contextlib

        write_config(self.config_path, ["id"], ["reg_id"], ["integer"])
        write_jsonl(self.left_path, [{"id": 1}, {"id": "bad"}])
        write_jsonl(self.right_path, [{"reg_id": 1}])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli_main(
                [
                    "--left", self.left_path,
                    "--right", self.right_path,
                    "--config", self.config_path,
                    "--output", self.out_path,
                    "--work-dir", self.tmp,
                    "--chunk-size", "1",
                    "--fan-in", "2",
                ]
            )
        self.assertEqual(rc, 2)
        self.assertIn("left side row 2", err.getvalue())
        # No run files are left behind after the failed run.
        leftovers = [
            n for n in os.listdir(self.tmp) if n.endswith(".fr")
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
