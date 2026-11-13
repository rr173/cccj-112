"""Regression tests for archive_stripe.

No pytest in this environment: plain unittest.  Run with

    bash scripts/run_tests.sh        # python3 -m unittest discover -s tests -v

Every combination of one and two unavailable stripes is exercised for
k = 2, 3, 8 and compared against the original length and SHA-256.  The pack
bytes are additionally compared with the independent reference codec in
``tests/reference_decoder.py``, which shares no GF or codec code.
"""

import hashlib
import itertools
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

from archive_stripe import gf256  # noqa: E402
from archive_stripe.check import check_pack, pack_source  # noqa: E402
from archive_stripe.errors import (  # noqa: E402
    InsufficientStripeError,
    LayoutError,
    ManifestError,
    RestoreRefused,
)
from archive_stripe.manifest import read_manifest  # noqa: E402
from archive_stripe.restore import restore_pack  # noqa: E402

import reference_decoder as ref  # noqa: E402


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="archstripe-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write_source(self, data, name="source.bin"):
        path = self.path(name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def packed(self, data, k, block, name="pack"):
        src = self.write_source(data, name="src-" + name)
        pack = self.path(name)
        pack_source(src, pack, k, block)
        return src, pack

    def damage_copy(self, pack, gone=(), truncate=(), flip=()):
        """Copy *pack* and remove / truncate / byte-flip named stripes."""
        damaged = self.path("damaged-" + os.path.basename(pack))
        shutil.rmtree(damaged, ignore_errors=True)
        shutil.copytree(pack, damaged)
        for name in gone:
            os.unlink(os.path.join(damaged, name))
        for name in truncate:
            target = os.path.join(damaged, name)
            size = max(0, os.path.getsize(target) - 3)
            with open(target, "r+b") as fh:
                fh.truncate(size)
        for name in flip:
            target = os.path.join(damaged, name)
            with open(target, "r+b") as fh:
                data = bytearray(fh.read())
                data[0] ^= 0x01
                fh.seek(0)
                fh.write(data)
        return damaged


class TestGf256(unittest.TestCase):
    def test_primitive_polynomial_tables(self):
        # 2*0x8e must reduce to 1 under 0x11d.
        self.assertEqual(gf256.mul(2, 0x8E), 1)
        self.assertEqual(gf256.inv(2), 0x8E)
        self.assertEqual(gf256.coefficients(8), (1, 2, 4, 8, 16, 32, 64, 128))

    def test_every_nonzero_element_has_inverse(self):
        for a in range(1, 256):
            self.assertEqual(gf256.mul(a, gf256.inv(a)), 1)

    def test_multiplication_matches_independent_clmul(self):
        for a in (0, 1, 2, 3, 7, 0x11, 0x53, 0x80, 0xFF):
            for b in (0, 1, 2, 3, 0x1D, 0x8E, 0xFF):
                self.assertEqual(gf256.mul(a, b), ref.clmul(a, b))

    def test_addition_is_xor(self):
        self.assertEqual(gf256.add(0xA5, 0x5A), 0xFF)
        self.assertEqual(gf256.sub(0xA5, 0x5A), 0xFF)


class TestHandComputedExample(TempDirTest):
    """The 4-byte source from examples/handcalc_pq.py: k=2, B=1."""

    SOURCE = bytes.fromhex("00010203")
    EXPECTED = {
        "D0.bin": bytes.fromhex("0002"),
        "D1.bin": bytes.fromhex("0103"),
        "P.bin": bytes.fromhex("0101"),
        "Q.bin": bytes.fromhex("0204"),
    }

    def test_pack_matches_hand_values(self):
        src, pack = self.packed(self.SOURCE, 2, 1)
        for name, expected in self.EXPECTED.items():
            with open(os.path.join(pack, name), "rb") as fh:
                self.assertEqual(fh.read(), expected, name)
        ref_stripes, _ = ref.reference_pack(self.SOURCE, 2, 1)
        self.assertEqual(ref_stripes, self.EXPECTED)

    def test_each_single_loss_restores_hand_bytes(self):
        src, pack = self.packed(self.SOURCE, 2, 1)
        for gone in ("D0.bin", "D1.bin", "P.bin", "Q.bin"):
            damaged = self.damage_copy(pack, gone=(gone,))
            out = self.path("out-" + gone)
            restore_pack(damaged, out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), self.SOURCE, gone)

    def test_both_data_stripes_lost_solved_from_p_and_q(self):
        src, pack = self.packed(self.SOURCE, 2, 1)
        damaged = self.damage_copy(pack, gone=("D0.bin", "D1.bin"))
        out = self.path("out-solved")
        restore_pack(damaged, out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), self.SOURCE)

    def test_both_parities_lost_uses_intact_data(self):
        src, pack = self.packed(self.SOURCE, 2, 1)
        damaged = self.damage_copy(pack, gone=("P.bin", "Q.bin"))
        out = self.path("out-parities")
        restore_pack(damaged, out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), self.SOURCE)


class TestAllLossCombinations(TempDirTest):
    """Every one- and two-stripe erasure for k = 2, 3, 8."""

    def _run_matrix(self, k, block):
        per_step = k * block
        cases = {
            "empty": b"",
            "zeros": b"\x00" * (per_step * 2),
            "non_utf8": bytes([0xFF, 0xFE, 0x80, 0x00]) * (per_step + 1),
            # exactly one full stripe step (each data stripe holds one block)
            "exact_step": bytes((i * 31 + 5) % 256 for i in range(per_step)),
            # one full step plus exactly one trailing byte
            "tail_one": bytes((i * 17 + 9) % 256 for i in range(per_step + 1)),
            # one byte in the final step and nothing else after the first
            "last_step_one": bytes(
                (i * 11 + 3) % 256 for i in range(per_step * 2 + 1)
            ),
        }
        names = [f"D{i}.bin" for i in range(k)] + ["P.bin", "Q.bin"]
        for label, data in cases.items():
            src = self.write_source(data, name=f"src-{k}-{block}-{label}")
            pack = self.path(f"pack-{k}-{block}-{label}")
            pack_source(src, pack, k, block)
            src_sha = sha(src)
            for count in (1, 2):
                for gone in itertools.combinations(names, count):
                    damaged = self.damage_copy(pack, gone=gone)
                    out = self.path("out.bin")
                    if os.path.exists(out):
                        os.unlink(out)
                    restore_pack(damaged, out)
                    self.assertEqual(
                        os.path.getsize(out), len(data),
                        (k, block, label, gone, "length"),
                    )
                    self.assertEqual(
                        sha(out), src_sha, (k, block, label, gone, "sha256")
                    )
                    # Independent reference decoder from the same stripes.
                    manifest, stripes = ref.read_pack(damaged)
                    present = [n for n in names if n not in gone]
                    rebuilt = ref.reference_restore(
                        stripes, k, block, manifest["stripe_length"],
                        manifest["source_length"], present,
                    )
                    self.assertEqual(rebuilt, data, (k, block, label, gone, "ref"))

    def test_k2(self):
        for block in (1, 4, 7):
            self._run_matrix(2, block)

    def test_k3(self):
        for block in (1, 5):
            self._run_matrix(3, block)

    def test_k8(self):
        for block in (1, 3, 16):
            self._run_matrix(8, block)

    def test_all_three_single_and_pair_losses_for_k8_sizes(self):
        # Focused sweep: all 10C2 = 45 two-stripe losses at k=8, one block.
        data = bytes((i * 7 + 1) % 256 for i in range(8))
        src, pack = self.packed(data, 8, 1)
        names = [f"D{i}.bin" for i in range(8)] + ["P.bin", "Q.bin"]
        for gone in itertools.combinations(names, 2):
            damaged = self.damage_copy(pack, gone=gone)
            out = self.path("out.bin")
            if os.path.exists(out):
                os.unlink(out)
            restore_pack(damaged, out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), data, gone)


class TestDamageDetection(TempDirTest):
    def test_manifest_is_ascii_and_self_describing(self):
        src, pack = self.packed(b"\xff\xfe\x80 binary", 2, 3)
        raw = open(os.path.join(pack, "manifest.json"), "rb").read()
        raw.decode("ascii")  # no legacy-encoded payload, no non-ASCII bytes
        obj = json.loads(raw)
        for key in ("version", "k", "block_size", "source_length",
                    "source_sha256", "stripe_length", "stripes"):
            self.assertIn(key, obj)
        for entry in obj["stripes"]:
            self.assertIn("name", entry)
            self.assertIn("length", entry)
            self.assertIn("sha256", entry)
        self.assertEqual(len(obj["stripes"]), 4)

    def test_manifest_lists_exact_geometry_and_digests(self):
        data = b"abc" * 11
        src, pack = self.packed(data, 3, 5)
        manifest = read_manifest(pack)
        self.assertEqual(manifest.k, 3)
        self.assertEqual(manifest.block_size, 5)
        self.assertEqual(manifest.source_length, len(data))
        # 33 bytes at k=3, B=5: one step holds 15 source bytes, so 3 steps.
        self.assertEqual(manifest.stripe_length, 15)
        self.assertEqual(manifest.stripe_count(), 5)
        self.assertEqual(
            os.listdir(pack).__len__(), 6  # 5 stripes + manifest
        )
        for name in manifest.all_names():
            self.assertEqual(
                manifest.expected_sha256(name), sha(os.path.join(pack, name))
            )

    def test_truncated_and_rewritten_are_distinguished(self):
        src, pack = self.packed(b"hello binary \xff\x00 world" * 4, 3, 6)
        damaged = self.damage_copy(
            pack, truncate=("D0.bin",), flip=("Q.bin",)
        )
        report = check_pack(damaged)
        by_name = {r["name"]: r for r in report["stripes"]}
        self.assertEqual(by_name["D0.bin"]["status"], "truncated")
        self.assertEqual(by_name["Q.bin"]["status"], "rewritten")
        for name in ("D1.bin", "D2.bin", "P.bin"):
            self.assertEqual(by_name[name]["status"], "ok")
        self.assertFalse(by_name["D0.bin"]["trustworthy"])
        self.assertFalse(by_name["Q.bin"]["trustworthy"])
        self.assertTrue(report["recoverable"])
        self.assertEqual(report["trusted"], ["D1.bin", "D2.bin", "P.bin"])

    def test_oversized_stripe_is_untrustworthy(self):
        src, pack = self.packed(b"x" * 40, 2, 4)
        target = os.path.join(pack, "D1.bin")
        with open(target, "ab") as fh:
            fh.write(b"extra")
        report = check_pack(pack)
        statuses = {r["name"]: r["status"] for r in report["stripes"]}
        self.assertEqual(statuses["D1.bin"], "length_mismatch")
        self.assertTrue(report["recoverable"])

    def test_truncate_one_flip_another_restores_exactly(self):
        data = bytes((i * 13 + 2) % 256 for i in range(53))
        src, pack = self.packed(data, 4, 8)
        damaged = self.damage_copy(pack, truncate=("D1.bin",), flip=("P.bin",))
        report = check_pack(damaged)
        self.assertTrue(report["recoverable"])
        self.assertFalse(report["healthy"])
        out = self.path("out.bin")
        restore_pack(damaged, out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(sha(out), sha(src))

    def test_single_flip_and_single_truncate_each_restore(self):
        data = bytes((i * 29 + 4) % 256 for i in range(29))
        src, pack = self.packed(data, 3, 4)
        for name in ("D0.bin", "D1.bin", "P.bin", "Q.bin"):
            for kind in ("truncate", "flip"):
                kwargs = {kind: (name,)}
                damaged = self.damage_copy(pack, **kwargs)
                report = check_pack(damaged)
                self.assertTrue(report["recoverable"], (name, kind))
                out = self.path(f"out-{name}-{kind}")
                restore_pack(damaged, out)
                with open(out, "rb") as fh:
                    self.assertEqual(fh.read(), data, (name, kind))

    def test_recovery_modes_reported(self):
        data = b"mode check payload" * 3
        src, pack = self.packed(data, 3, 5)
        self.assertEqual(check_pack(pack)["recovery_mode"], "identity")
        cases = {
            ("D0.bin",): "single_p",
            ("P.bin",): "identity",
            ("D0.bin", "P.bin"): "single_q",
            ("D0.bin", "D1.bin"): "double",
            ("P.bin", "Q.bin"): "identity",
        }
        for gone, want in cases.items():
            damaged = self.damage_copy(pack, gone=gone)
            report = check_pack(damaged)
            self.assertTrue(report["recoverable"], gone)
            self.assertEqual(report["recovery_mode"], want, gone)

    def test_third_damage_refuses_and_preserves_existing_target(self):
        src, pack = self.packed(b"payload" * 20, 4, 7)
        damaged = self.damage_copy(
            pack, truncate=("D0.bin",), flip=("D1.bin", "Q.bin")
        )
        report = check_pack(damaged)
        self.assertFalse(report["recoverable"])
        target = self.path("precious.bin")
        with open(target, "wb") as fh:
            fh.write(b"do not touch")
        before = sha(target)
        with self.assertRaises(InsufficientStripeError):
            restore_pack(damaged, target)
        self.assertEqual(sha(target), before)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"do not touch")
        self.assertFalse(os.path.exists(self.path(".restore.tmp")))

    def test_consistent_manifest_lie_fails_final_digest_keeps_target(self):
        # Change source_length in a way that still satisfies the manifest's
        # own geometry check (same number of stripe steps).  Every stripe is
        # then still "trustworthy", reconstruction runs, but the final
        # source digest must disagree and the target must survive.
        data = bytes((i * 3 + 1) % 256 for i in range(24))
        src, pack = self.packed(data, 2, 4)  # 3 steps of 8 bytes
        manifest_path = os.path.join(pack, "manifest.json")
        with open(manifest_path, "rb") as fh:
            obj = json.loads(fh.read())
        self.assertEqual(obj["stripe_length"], 12)
        obj["source_length"] = 23  # ceil(23/8)*4 == 12: geometry still valid
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "ok")
        self.assertTrue(report["recoverable"])
        target = self.path("existing.bin")
        with open(target, "wb") as fh:
            fh.write(b"survive")
        # An existing target is refused before any work; a fresh target gets
        # as far as the post-reconstruction digest check and is still not
        # published, nor left behind as a temporary file.
        with self.assertRaises(RestoreRefused):
            restore_pack(pack, target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"survive")
        fresh = self.path("fresh.bin")
        with self.assertRaises(InsufficientStripeError):
            restore_pack(pack, fresh)
        self.assertFalse(os.path.exists(fresh))
        leftovers = [
            n for n in os.listdir(self.tmp) if n.startswith(".restore.")
        ]
        self.assertEqual(leftovers, [])

    def test_restore_leaves_pack_directory_untouched(self):
        data = b"pack stays intact" * 4
        src, pack = self.packed(data, 3, 5)
        before = {
            name: sha(os.path.join(pack, name)) for name in os.listdir(pack)
        }
        out = self.path("out.bin")
        restore_pack(pack, out)
        after = {
            name: sha(os.path.join(pack, name)) for name in os.listdir(pack)
        }
        self.assertEqual(before, after)
        os.unlink(out)
        os.unlink(os.path.join(pack, "D1.bin"))
        os.unlink(os.path.join(pack, "P.bin"))
        before = {
            name: sha(os.path.join(pack, name)) for name in os.listdir(pack)
        }
        restore_pack(pack, out)
        after = {
            name: sha(os.path.join(pack, name)) for name in os.listdir(pack)
        }
        self.assertEqual(before, after)

    def test_restore_never_overwrites_existing_target(self):
        src, pack = self.packed(b"abc" * 9, 3, 3)
        target = self.path("existing.bin")
        with open(target, "wb") as fh:
            fh.write(b"old")
        with self.assertRaises(RestoreRefused):
            restore_pack(pack, target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"old")

    def test_missing_stripe_is_reported_not_invented(self):
        src, pack = self.packed(b"0123456789" * 5, 3, 4)
        os.unlink(os.path.join(pack, "P.bin"))
        report = check_pack(pack)
        by_name = {r["name"]: r for r in report["stripes"]}
        self.assertEqual(by_name["P.bin"]["status"], "missing")
        self.assertTrue(report["recoverable"])
        self.assertEqual(report["recovery_mode"], "identity")


class TestManifestFailures(TempDirTest):
    def test_missing_manifest_refused_without_output(self):
        src, pack = self.packed(b"manifest test", 2, 3)
        # An existing target must survive even when the failure is earlier.
        existing = self.path("existing.bin")
        with open(existing, "wb") as fh:
            fh.write(b"keep")
        os.unlink(os.path.join(pack, "manifest.json"))
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "invalid")
        self.assertFalse(report["recoverable"])
        out = self.path("out.bin")
        with self.assertRaises(ManifestError):
            restore_pack(pack, out)
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(ManifestError):
            restore_pack(pack, existing)
        with open(existing, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")

    def test_block_size_contradicting_stripe_size_refused(self):
        src, pack = self.packed(b"geometry" * 9, 3, 5)
        manifest_path = os.path.join(pack, "manifest.json")
        with open(manifest_path, "rb") as fh:
            obj = json.loads(fh.read())
        obj["block_size"] = 7  # no longer divides the stripe length
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "invalid")
        self.assertIn("contradicts", report["manifest"]["error"])
        with self.assertRaises(ManifestError):
            restore_pack(pack, self.path("out.bin"))
        self.assertFalse(os.path.exists(self.path("out.bin")))

    def test_source_length_tampered_refused_and_target_kept(self):
        data = b"tamper" * 12
        src, pack = self.packed(data, 2, 5)
        manifest_path = os.path.join(pack, "manifest.json")
        with open(manifest_path, "rb") as fh:
            obj = json.loads(fh.read())
        # A smaller, internally consistent geometry: fewer steps than the
        # real stripe files have, so the reconstruction digest cannot match.
        obj["source_length"] = len(data) - 6
        obj["stripe_length"] = 5  # no longer ceil(len/(k*B))*B either
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        target = self.path("existing.bin")
        with open(target, "wb") as fh:
            fh.write(b"keep me")
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "invalid")
        self.assertIn("contradicts", report["manifest"]["error"])
        with self.assertRaises(ManifestError):
            restore_pack(pack, target)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"keep me")

    def test_k_out_of_range_refused(self):
        src, pack = self.packed(b"range", 2, 2)
        manifest_path = os.path.join(pack, "manifest.json")
        with open(manifest_path, "rb") as fh:
            obj = json.loads(fh.read())
        obj["k"] = 9
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "invalid")
        self.assertIsInstance(
            read_manifest_error(pack), (LayoutError, ManifestError)
        )

    def test_missing_field_refused(self):
        src, pack = self.packed(b"missing field", 2, 2)
        manifest_path = os.path.join(pack, "manifest.json")
        with open(manifest_path, "rb") as fh:
            obj = json.loads(fh.read())
        del obj["source_length"]
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        report = check_pack(pack)
        self.assertEqual(report["manifest"]["status"], "invalid")
        self.assertIn("source_length", report["manifest"]["error"])

    def test_pack_rejects_k_out_of_bounds(self):
        src = self.write_source(b"x")
        for k in (0, 1, 9, 100):
            with self.assertRaises(LayoutError):
                pack_source(src, self.path(f"bad{k}"), k, 4)

    def test_pack_rejects_bad_block_size(self):
        src = self.write_source(b"x")
        for block in (0, -1):
            with self.assertRaises(LayoutError):
                pack_source(src, self.path(f"badb{block}"), 2, block)


def read_manifest_error(pack):
    try:
        read_manifest(pack)
    except (ManifestError, LayoutError) as exc:
        return exc
    raise AssertionError("manifest should have been rejected")


class TestEmptyFile(TempDirTest):
    def test_empty_file_has_readable_manifest_and_zero_stripes(self):
        src, pack = self.packed(b"", 3, 16)
        manifest = read_manifest(pack)
        self.assertEqual(manifest.source_length, 0)
        self.assertEqual(manifest.stripe_length, 0)
        for name in manifest.all_names():
            self.assertEqual(os.path.getsize(os.path.join(pack, name)), 0)
        report = check_pack(pack)
        self.assertTrue(report["healthy"])
        self.assertTrue(report["recoverable"])
        for gone in (["D0.bin"], ["P.bin"], ["D1.bin", "Q.bin"]):
            damaged = self.damage_copy(pack, gone=gone)
            out = self.path("empty-" + "-".join(gone))
            restore_pack(damaged, out)
            self.assertEqual(os.path.getsize(out), 0)
            self.assertEqual(sha(out), hashlib.sha256(b"").hexdigest())


class TestPackerSafety(TempDirTest):
    def test_refuses_non_empty_pack_dir(self):
        src = self.write_source(b"abc")
        pack = self.path("pack")
        os.mkdir(pack)
        with open(os.path.join(pack, "junk"), "w") as fh:
            fh.write("x")
        with self.assertRaises(LayoutError):
            pack_source(src, pack, 2, 2)

    def test_force_replaces_pack_and_does_not_leave_staging(self):
        src = self.write_source(b"first-version")
        pack = self.path("pack")
        pack_source(src, pack, 2, 3)
        with open(src, "wb") as fh:
            fh.write(b"second-version-longer")
        pack_source(src, pack, 3, 4, force=True)
        manifest = read_manifest(pack)
        self.assertEqual(manifest.k, 3)
        self.assertEqual(manifest.source_length, len(b"second-version-longer"))
        leftovers = [n for n in os.listdir(self.tmp) if "staging" in n]
        self.assertEqual(leftovers, [])


class TestCliExitCodes(TempDirTest):
    def run_cli(self, *args):
        import contextlib
        import io

        from archive_stripe.cli import main

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(list(args))

    def test_pack_check_restore_round_trip(self):
        src = self.write_source(b"cli bytes")
        pack = self.path("pack")
        self.assertEqual(self.run_cli("pack", src, pack, "-k", "2", "-B", "4"), 0)
        self.assertEqual(self.run_cli("check", pack), 0)
        out = self.path("out.bin")
        self.assertEqual(self.run_cli("restore", pack, "--output", out), 0)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), b"cli bytes")

    def test_check_unrecoverable_returns_2(self):
        src, pack = self.packed(b"z" * 30, 3, 4)
        for name in ("D0.bin", "D1.bin", "P.bin"):
            os.unlink(os.path.join(pack, name))
        self.assertEqual(self.run_cli("check", pack), 2)
        self.assertEqual(
            self.run_cli("restore", pack, "--output", self.path("out")), 2
        )
        self.assertFalse(os.path.exists(self.path("out")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
