"""Tests for HTTP/1.1 framing, chunked bodies, trailers and gzip."""

import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pcap_recover.cli import main  # noqa: E402

from pcap_builder import (  # noqa: E402
    PcapWriter,
    chunked_response,
    content_length_response,
    gzip_response,
    http_response,
    write_tcp_stream,
)

SERVER_IP = "10.10.0.1"
SERVER_PORT = 8080
CLIENT_IP = "192.168.1.20"
CLIENT_PORT = 44444
ISN = 5000


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcap_recover_http_")
        self.pcap = os.path.join(self.tmp, "capture.pcap")
        self.config = os.path.join(self.tmp, "endpoints.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            handle.write(
                '{"endpoints": [{"ip": "%s", "port": %d}]}'
                % (SERVER_IP, SERVER_PORT)
            )
        self.output = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, stream, mss=300):
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=stream, mss=mss,
            )

    def run_cli(self, *extra):
        return main([
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ] + list(extra))

    def load_index(self):
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def conn_dir(self):
        return os.path.join(
            self.output,
            "conn_192.168.1.20_44444__10.10.0.1_8080",
        )

    def body(self, index=1):
        with open(os.path.join(self.conn_dir(), f"body_{index}.bin"),
                  "rb") as handle:
            return handle.read()

    def metadata(self, index=1):
        with open(os.path.join(self.conn_dir(), f"metadata_{index}.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def expect_failure(self, reason):
        self.assertEqual(self.run_cli(), 1)
        index = self.load_index()
        self.assertEqual(index["summary"]["failed_connections"], 1)
        self.assertEqual(
            index["connections"][0]["failure_reason"], reason
        )
        self.assertEqual(os.listdir(self.output), ["index.json"])


class FramingTests(_HttpBase):
    def test_chunked_with_extensions_and_trailers(self):
        stream = chunked_response(
            [b"alpha", b"beta", b"gamma"],
            trailers=[(b"X-Checksum", b"deadbeef"), (b"X-Trailer", b"v")],
            first_extension=b"name=value",
        )
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), b"alphabetagamma")

    def test_back_to_back_responses_including_second_object(self):
        first = chunked_response([b"one-", b"two"])
        second = content_length_response(200, b"second body payload")
        self.build(first + second)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(1), b"one-two")
        self.assertEqual(self.body(2), b"second body payload")
        index = self.load_index()
        conn = index["connections"][0]
        self.assertEqual(conn["response_count"], 2)
        self.assertEqual(conn["object_count"], 2)

    def test_provisional_then_204_then_304_produce_no_objects(self):
        provisional = http_response(
            b"HTTP/1.1 100 Continue",
            [(b"Content-Length", b"0")],
        )
        no_content = http_response(
            b"HTTP/1.1 204 No Content",
            [(b"Cache-Control", b"no-store")],
        )
        not_modified = http_response(
            b"HTTP/1.1 304 Not Modified",
            [(b"ETag", b'"abc"')],
        )
        final = content_length_response(200, b"real data")
        self.build(provisional + no_content + not_modified + final)
        self.assertEqual(self.run_cli(), 0)
        index = self.load_index()
        conn = index["connections"][0]
        # Three bodyless messages plus one object-bearing message.
        self.assertEqual(conn["response_count"], 4)
        self.assertEqual(conn["object_count"], 1)
        self.assertEqual(self.body(1), b"real data")
        meta = self.metadata(1)
        self.assertEqual(meta["http_response_index"], 4)

    def test_conflicting_content_lengths_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"10"), (b"Content-Length", b"11")],
            b"0123456789",
        )
        self.build(stream)
        self.expect_failure("conflicting_content_length")

    def test_duplicate_consistent_content_length_accepted(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"4"), (b"Content-Length", b"4")],
            b"abcd",
        )
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), b"abcd")
        meta = self.metadata()
        names = [header["name"] for header in meta["headers"]]
        self.assertEqual(names.count("Content-Length"), 2)

    def test_chunked_and_length_together_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked"),
             (b"Content-Length", b"5")],
            b"0\r\n\r\n",
        )
        self.build(stream)
        self.expect_failure("conflicting_framing")

    def test_unknown_transfer_encoding_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"gzip")],
            b"whatever",
        )
        self.build(stream)
        self.expect_failure("unsupported_transfer_encoding")

    def test_body_without_boundary_is_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Connection", b"close")],
            b"some bytes with no framing header",
        )
        self.build(stream)
        self.expect_failure("unsupported_framing")

    def test_short_content_length_body_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Content-Length", b"99")],
            b"only a few bytes",
        )
        self.build(stream)
        self.expect_failure("short_body")

    def test_bad_chunk_size_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            b"zz\r\nabc\r\n0\r\n\r\n",
        )
        self.build(stream)
        self.expect_failure("bad_chunk_size")

    def test_missing_chunk_crlf_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            b"3\r\nabcXX0\r\n\r\n",
        )
        self.build(stream)
        self.expect_failure("bad_chunk_terminator")

    def test_truncated_chunk_data_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Transfer-Encoding", b"chunked")],
            b"10\r\nshort",
        )
        self.build(stream)
        self.expect_failure("short_body")

    def test_missing_crlf_after_status_line_rejected(self):
        # Valid header block otherwise; only the status line lacks CRLF, so
        # the failure must be reported as missing_crlf rather than anything
        # about framing of the body.
        stream = (
            b"HTTP/1.1 200 OK" + b"\n"
            + b"Content-Length: 0\r\n\r\n"
        )
        self.build(stream)
        self.expect_failure("missing_crlf")

    def test_no_resync_after_garbage(self):
        good = content_length_response(200, b"good")
        garbage = b"HTTP/1.1 200 OK\r\nContent-Length: 9999\r\n\r\nshort"
        second = content_length_response(200, b"ignored")
        self.build(good + garbage + second)
        # The connection as a whole fails and publishes nothing.
        self.assertEqual(self.run_cli(), 1)
        index = self.load_index()
        self.assertEqual(index["summary"]["successful_connections"], 0)
        self.assertEqual(os.listdir(self.output), ["index.json"])

    def test_header_size_limit_enforced(self):
        big_value = b"x" * 200
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"X-Big", big_value), (b"Content-Length", b"1")],
            b"a",
        )
        self.build(stream)
        self.assertEqual(self.run_cli("--max-header-bytes", "64"), 1)
        index = self.load_index()
        self.assertEqual(
            index["connections"][0]["failure_reason"], "header_too_large"
        )

    def test_case_insensitive_header_names(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"cOnTeNt-LENGTH", b"3")],
            b"abc",
        )
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), b"abc")

    def test_bad_status_line_rejected(self):
        stream = b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n"
        self.build(stream)
        self.expect_failure("bad_status_line")

    def test_non_numeric_status_rejected(self):
        stream = b"HTTP/1.1 2xx OK\r\nContent-Length: 0\r\n\r\n"
        self.build(stream)
        self.expect_failure("bad_status_code")

    def test_truncated_gzip_member_rejected(self):
        payload = b"cut off mid-stream payload"
        gz = gzip.compress(payload, mtime=0)
        gz = gz[:-4]  # remove half of the 8-byte trailer
        stream = gzip_response(payload, gz)
        self.build(stream)
        self.expect_failure("gzip_corrupt")


class GzipTests(_HttpBase):
    def test_gzip_body_roundtrip_with_crc_check(self):
        payload = (
            b"directory-entry-001\n" * 20
        )
        gz = gzip.compress(payload, mtime=0)
        stream = gzip_response(payload, gz)
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), payload)
        meta = self.metadata()
        self.assertEqual(meta["content_encoding"], "gzip")
        self.assertEqual(
            meta["raw_body_length_bytes"], len(gz)
        )
        self.assertEqual(
            meta["decoded_body_length_bytes"], len(payload)
        )

    def test_bad_gzip_crc_rejected(self):
        payload = b"important catalog rows"
        gz = bytearray(gzip.compress(payload, mtime=0))
        gz[-8] ^= 0xFF  # corrupt the CRC-32 trailer
        stream = gzip_response(payload, bytes(gz))
        self.build(stream)
        self.expect_failure("gzip_corrupt")

    def test_bad_gzip_stream_rejected(self):
        gz = b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"not-deflate-data"
        stream = gzip_response(b"x", gz)
        self.build(stream)
        self.expect_failure("gzip_corrupt")

    def test_decompressed_size_limit_enforced(self):
        payload = b"y" * 1000
        gz = gzip.compress(payload, mtime=0)
        stream = gzip_response(payload, gz)
        self.build(stream)
        self.assertEqual(
            self.run_cli("--max-decoded-bytes", "100"), 1
        )
        index = self.load_index()
        self.assertEqual(
            index["connections"][0]["failure_reason"], "body_limit"
        )

    def test_unsupported_content_encoding_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Content-Encoding", b"br"),
             (b"Content-Length", b"4")],
            b"abcd",
        )
        self.build(stream)
        self.expect_failure("unsupported_content_encoding")

    def test_multiple_content_encodings_rejected(self):
        stream = http_response(
            b"HTTP/1.1 200 OK",
            [(b"Content-Encoding", b"gzip"),
             (b"Content-Encoding", b"br"),
             (b"Content-Length", b"4")],
            b"abcd",
        )
        self.build(stream)
        self.expect_failure("unsupported_content_encoding")

    def test_identity_bytes_are_preserved_unchanged(self):
        payload = bytes(range(256))
        stream = content_length_response(200, payload)
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), payload)

    def test_gzip_with_fname_and_fcomment_headers(self):
        payload = b"catalog with gzip header fields"
        compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        deflated = compressor.compress(payload) + compressor.flush()
        gz = (
            b"\x1f\x8b\x08"
            + bytes([0x08 | 0x10])  # FNAME and FCOMMENT
            + b"\x00\x00\x00\x00\x00"
            + b"dir.jsonl\x00"
            + b"a comment\x00"
            + deflated
            + zlib.crc32(payload).to_bytes(4, "little")
            + (len(payload) & 0xFFFFFFFF).to_bytes(4, "little")
        )
        stream = gzip_response(payload, gz)
        self.build(stream)
        self.assertEqual(self.run_cli(), 0)
        self.assertEqual(self.body(), payload)


if __name__ == "__main__":
    unittest.main()
