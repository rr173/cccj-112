"""Golden-fixture tests with hardcoded bytes and stream offsets.

The expectations in :class:`GoldenOffsetTests` are written out literally --
they are not derived from the program's own counts.  An additional verifier
reads the published output independently and recomputes every length and
SHA-256 digest.
"""

import hashlib
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

from pcap_recover.cli import main  # noqa: E402
from pcap_recover.config import load_endpoints  # noqa: E402
from pcap_recover.errors import ConfigError  # noqa: E402

from pcap_builder import PcapWriter, write_tcp_stream  # noqa: E402

SERVER_IP = "10.10.0.1"
SERVER_PORT = 8080
CLIENT_IP = "192.168.1.20"
CLIENT_PORT = 44444
ISN = 4242


class GoldenOffsetTests(unittest.TestCase):
    """One tiny fixture whose every byte and offset is fixed in source."""

    # The complete reconstructed TCP stream, written explicitly.
    BODY = b"abc"
    STREAM = (
        b"HTTP/1.1 200 OK\r\n"          # 0..16  (17 bytes)
        b"Content-Length: 3\r\n"       # 17..35 (19 bytes)
        b"X-Tag: keep-me\r\n"          # 36..51 (16 bytes)
        b"\r\n"                        # 52..53 blank line
        b"abc"                         # 54..56 body
    )

    EXPECTED_OFFSETS = {
        "response_start": 0,
        "header_end": 54,
        "body_start": 54,
        "body_end": 57,
    }
    EXPECTED_STREAM_LENGTH = 57
    EXPECTED_SHA256 = hashlib.sha256(BODY).hexdigest()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcap_recover_golden_")
        self.pcap = os.path.join(self.tmp, "golden.pcap")
        self.config = os.path.join(self.tmp, "endpoints.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(
                '{"endpoints": [{"ip": "%s", "port": %d}]}'
                % (SERVER_IP, SERVER_PORT)
            )
        self.output = os.path.join(self.tmp, "out")
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=self.STREAM, mss=8,  # many tiny segments
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_bytes_and_offsets(self):
        # Sanity: the literal offsets in the class body must be self-consistent.
        self.assertEqual(len(self.STREAM), self.EXPECTED_STREAM_LENGTH)
        self.assertEqual(self.STREAM[54:57], self.BODY)

        code = main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ])
        self.assertEqual(code, 0)

        conn = "conn_192.168.1.20_44444__10.10.0.1_8080"
        body_path = os.path.join(self.output, conn, "body_1.bin")
        with open(body_path, "rb") as handle:
            body_bytes = handle.read()
        self.assertEqual(body_bytes, self.BODY)

        with open(os.path.join(self.output, conn, "metadata_1.json"),
                  encoding="utf-8") as handle:
            metadata = json.load(handle)
        self.assertEqual(
            metadata["stream_offsets"], self.EXPECTED_OFFSETS
        )
        self.assertEqual(metadata["status_code"], 200)
        self.assertEqual(
            [header["name"] for header in metadata["headers"]],
            ["Content-Length", "X-Tag"],
        )
        self.assertEqual(
            metadata["headers"][1], {"name": "X-Tag", "value": "keep-me"}
        )
        self.assertEqual(metadata["body_sha256"], self.EXPECTED_SHA256)
        self.assertEqual(metadata["raw_body_length_bytes"], 3)
        self.assertEqual(metadata["decoded_body_length_bytes"], 3)
        self.assertIsNone(metadata["content_encoding"])

    def test_reconstructed_stream_slices_match_offsets(self):
        main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ])
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        offsets = index["connections"][0]["objects"][0]["stream_offsets"]
        # Independently re-derive the stream from the fixture definition and
        # check the advertised window contains exactly the body bytes.
        self.assertEqual(
            self.STREAM[offsets["body_start"]:offsets["body_end"]],
            self.BODY,
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcap_recover_config_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_valid_config_loads(self):
        path = self._write(
            '{"endpoints": [{"ip": "10.0.0.1", "port": 80}]}'
        )
        self.assertEqual(load_endpoints(path), [("10.0.0.1", 80)])

    def test_bad_shape_and_values_are_rejected(self):
        for text in (
            "[]",
            '{"endpoints": "nope"}',
            '{"endpoints": []}',
            '{"endpoints": [{"ip": "10.0.0.1"}]}',
            '{"endpoints": [{"ip": "999.1.1.1", "port": 80}]}',
            '{"endpoints": [{"ip": "10.0.0.1", "port": 70000}]}',
            '{"endpoints": [{"ip": "10.0.0.1", "port": 80},'
            ' {"ip": "10.0.0.1", "port": 80}]}',
            "{not json",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    load_endpoints(self._write(text))


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcap_recover_contract_")
        self.pcap = os.path.join(self.tmp, "capture.pcap")
        self.config = os.path.join(self.tmp, "endpoints.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(
                '{"endpoints": [{"ip": "%s", "port": %d}]}'
                % (SERVER_IP, SERVER_PORT)
            )
        self.output = os.path.join(self.tmp, "out")
        stream = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        )
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=stream, mss=10,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_output_dir_is_refused(self):
        os.makedirs(self.output)
        marker = os.path.join(self.output, "keep.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("untouched")
        # The API surface raises, the CLI translates the same condition into
        # a non-zero exit code; in neither case is the directory touched.
        from pcap_recover.errors import FatalInputError
        with self.assertRaises(FatalInputError):
            self._call_api()
        self.assertTrue(os.path.exists(marker))
        code = main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ])
        self.assertNotEqual(code, 0)
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untouched")

    def _call_api(self):
        from pcap_recover.extract import run_recovery
        run_recovery(self.pcap, self.config, self.output)

    def test_no_matching_traffic_exits_nonzero_with_index(self):
        other_cfg = os.path.join(self.tmp, "other.json")
        with open(other_cfg, "w", encoding="utf-8") as handle:
            handle.write(
                '{"endpoints": [{"ip": "172.16.0.9", "port": 9999}]}'
            )
        code = main([
            "--pcap", self.pcap, "--endpoints", other_cfg,
            "--output", self.output,
        ])
        self.assertEqual(code, 1)
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["summary"]["selected_connections"], 0)

    def test_source_pcap_is_not_modified(self):
        with open(self.pcap, "rb") as handle:
            before = handle.read()
        main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ])
        with open(self.pcap, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_independent_output_verification(self):
        """Recompute every digest/length from files, ignoring the tool's."""
        code = main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ])
        self.assertEqual(code, 0)
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        for entry in index["connections"]:
            if entry["status"] != "ok":
                continue
            for obj in entry["objects"]:
                path = os.path.join(self.output, obj["body_file"])
                with open(path, "rb") as handle:
                    raw = handle.read()
                self.assertEqual(
                    hashlib.sha256(raw).hexdigest(), obj["body_sha256"]
                )
                self.assertEqual(
                    len(raw), obj["decoded_body_length_bytes"]
                )
                meta_path = os.path.join(
                    self.output, entry["connection_dir"],
                    f"metadata_{obj['object_index']}.json",
                )
                with open(meta_path, encoding="utf-8") as handle:
                    metadata = json.load(handle)
                self.assertEqual(
                    metadata["body_sha256"],
                    hashlib.sha256(raw).hexdigest(),
                )
                self.assertEqual(
                    metadata["decoded_body_length_bytes"], len(raw)
                )


if __name__ == "__main__":
    unittest.main()
