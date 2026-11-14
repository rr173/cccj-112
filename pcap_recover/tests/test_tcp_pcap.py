"""Tests for PCAP container handling and TCP stream reassembly."""

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
from pcap_recover.errors import FatalInputError  # noqa: E402
from pcap_recover.extract import run_recovery  # noqa: E402

from pcap_builder import (  # noqa: E402
    PcapWriter,
    client_packet,
    fragmented_frame,
    partial_overlap_packet,
    retransmit_packet,
    server_data,
    server_fin,
    server_rst,
    server_synack,
    stream_packets,
    write_tcp_stream,
)

SERVER_IP = "10.10.0.1"
SERVER_PORT = 8080
CLIENT_IP = "192.168.1.20"
CLIENT_PORT = 44444
ISN = 0xFFFFFFD0


class _BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcap_recover_test_")
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

    def run_cli(self, *extra):
        argv = [
            "--pcap", self.pcap, "--endpoints", self.config,
            "--output", self.output,
        ] + list(extra)
        return main(argv)

    def run_api(self, limits=None):
        return run_recovery(self.pcap, self.config, self.output, limits)


class ContainerTests(_BaseCase):
    def _one_stream_pcap(self, big_endian):
        body = b"plain body bytes"
        stream = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n" + body
        )
        with PcapWriter(self.pcap, big_endian=big_endian) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=12345, stream=stream,
            )

    def test_both_endiannesses_recover_same_bytes(self):
        results = {}
        for big_endian in (True, False):
            self._one_stream_pcap(big_endian)
            code = self.run_cli()
            self.assertEqual(code, 0)
            body_path = os.path.join(
                self.output,
                "conn_192.168.1.20_44444__10.10.0.1_8080",
                "body_1.bin",
            )
            with open(body_path, "rb") as handle:
                results[big_endian] = handle.read()
            shutil.rmtree(self.output)
        self.assertEqual(results[True], results[False])
        self.assertEqual(results[True], b"plain body bytes")

    def test_truncated_global_header_publishes_nothing(self):
        with open(self.pcap, "wb") as handle:
            handle.write(b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00")
        with self.assertRaises(FatalInputError):
            self.run_api()
        self.assertFalse(os.path.exists(self.output))

    def test_truncated_packet_record_publishes_nothing(self):
        self._one_stream_pcap(True)
        with open(self.pcap, "rb") as handle:
            blob = handle.read()
        with open(self.pcap, "wb") as handle:
            handle.write(blob[:-7])  # cut bytes off the last record
        with self.assertRaises(FatalInputError):
            self.run_api()
        self.assertFalse(os.path.exists(self.output))

    def test_truncated_packet_header_publishes_nothing(self):
        self._one_stream_pcap(True)
        with open(self.pcap, "rb") as handle:
            blob = handle.read()
        with open(self.pcap, "wb") as handle:
            handle.write(blob[:-4])  # packet header itself incomplete
        with self.assertRaises(FatalInputError):
            self.run_api()
        self.assertFalse(os.path.exists(self.output))

    def test_bad_magic_is_fatal(self):
        with open(self.pcap, "wb") as handle:
            handle.write(b"\x00" * 24)
        with self.assertRaises(FatalInputError):
            self.run_api()
        self.assertFalse(os.path.exists(self.output))

    def test_nanosecond_magic_is_rejected(self):
        with open(self.pcap, "wb") as handle:
            import struct
            handle.write(struct.pack(
                "<IHHIIII", 0x34CDB2A1, 2, 4, 0, 0, 65535, 1,
            ))
        with self.assertRaises(FatalInputError):
            self.run_api()

    def test_non_ethernet_linktype_is_rejected(self):
        import struct
        with open(self.pcap, "wb") as handle:
            handle.write(struct.pack(
                "<IHHIIII", 0xD4C3B2A1, 2, 4, 0, 0, 65535, 12,
            ))
        with self.assertRaises(FatalInputError):
            self.run_api()


class ReassemblyTests(_BaseCase):
    def _simple_stream(self):
        body = b"".join(bytes([65 + (i % 26)]) for i in range(250))
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 250\r\n\r\n" + body
        )
        return body, response

    def test_wraparound_sequence_space(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            for _kind, packet in packets:
                writer.write_packet(packet)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(
            self.output,
            "conn_192.168.1.20_44444__10.10.0.1_8080",
            "body_1.bin",
        ), "rb") as handle:
            self.assertEqual(handle.read(), body)

    def test_reverse_packet_order_with_early_fin(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            synack = packets[0][1]
            fin = packets[-1][1]
            data_packets = [packet for _kind, packet in packets[1:-1]]
            writer.write_packet(fin)  # FIN before all data in the capture
            for packet in reversed(data_packets):
                writer.write_packet(packet)
            writer.write_packet(synack)  # handshake last in the file
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(
            self.output,
            "conn_192.168.1.20_44444__10.10.0.1_8080",
            "body_1.bin",
        ), "rb") as handle:
            self.assertEqual(handle.read(), body)

    def test_identical_retransmit_and_partial_overlap_count_once(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            synack = packets[0][1]
            fin = packets[-1][1]
            data_packets = [packet for _kind, packet in packets[1:-1]]
            writer.write_packet(synack)
            writer.write_packet(data_packets[0])
            # Exact retransmit of segment 0.
            writer.write_packet(retransmit_packet(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1) & 0xFFFFFFFF, ack=1000,
                payload=response[0:70],
            ))
            # Partial overlap: starts 30 bytes into segment 0, same bytes.
            writer.write_packet(partial_overlap_packet(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1) & 0xFFFFFFFF, ack=1000,
                payload=response[0:70], skip=30,
            ))
            for packet in data_packets[1:]:
                writer.write_packet(packet)
            writer.write_packet(fin)
        self.assertEqual(self.run_cli(), 0)
        index_path = os.path.join(self.output, "index.json")
        import json
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["summary"]["successful_connections"], 1)
        conn = index["connections"][0]
        self.assertEqual(conn["stream_length_bytes"], len(response))

    def test_conflicting_retransmit_fails_connection(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            synack = packets[0][1]
            fin = packets[-1][1]
            data_packets = [packet for _kind, packet in packets[1:-1]]
            writer.write_packet(synack)
            writer.write_packet(data_packets[0])
            writer.write_packet(retransmit_packet(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1) & 0xFFFFFFFF, ack=1000,
                payload=response[0:70], mutate_offset=10, mutate_byte=0x5A,
            ))
            for packet in data_packets[1:]:
                writer.write_packet(packet)
            writer.write_packet(fin)
        code = self.run_cli()
        self.assertEqual(code, 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["summary"]["failed_connections"], 1)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "byte_conflict"
        )
        # No per-connection directory may be published for the failure.
        entries = os.listdir(self.output)
        self.assertEqual(entries, ["index.json"])

    def test_missing_segment_fails_without_joining_gap(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            writer.write_packet(packets[0][1])  # SYN+ACK
            data_packets = [packet for _kind, packet in packets[1:-1]]
            for packet in data_packets[:1] + data_packets[2:]:  # drop one
                writer.write_packet(packet)
            writer.write_packet(packets[-1][1])  # FIN
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "missing_bytes"
        )

    def test_no_synack_is_rejected(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            for _kind, packet in packets[1:]:  # skip the SYN+ACK
                writer.write_packet(packet)
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "no_syn_ack"
        )

    def test_no_fin_is_rejected(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            for _kind, packet in packets[:-1]:  # skip the FIN
                writer.write_packet(packet)
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["connections"][0]["failure_reason"], "no_fin")

    def test_rst_termination_is_rejected(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            for _kind, packet in packets[:-1]:
                writer.write_packet(packet)
            writer.write_packet(server_rst(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1 + len(response)) & 0xFFFFFFFF, ack=1000,
            ))
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "rst_terminated"
        )

    def test_reused_four_tuple_with_new_isn_is_rejected(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            # A second handshake on the exact same four-tuple, different ISN.
            writer.write_packet(server_synack(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=0x11111111,
            ))
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "connection_reused"
        )

    def test_identical_synack_retransmit_is_allowed(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            writer.write_packet(server_synack(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, isn=ISN,
            ))
            writer.write_packet(server_synack(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, isn=ISN,
            ))
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            for _kind, packet in packets[1:]:
                writer.write_packet(packet)
        self.assertEqual(self.run_cli(), 0)

    def test_client_direction_is_ignored(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            # Client packets must not open or affect tracked connections.
            writer.write_packet(client_packet(
                CLIENT_IP, CLIENT_PORT, SERVER_IP, SERVER_PORT,
                seq=999, ack=ISN + 1, flags=0x10, payload=b"GET / HTTP/1.1",
            ))
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
        self.assertEqual(self.run_cli(), 0)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["summary"]["selected_connections"], 1)

    def test_ip_fragment_fails_connection(self):
        with PcapWriter(self.pcap) as writer:
            writer.write_packet(server_synack(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT, isn=ISN,
            ))
            writer.write_packet(fragmented_frame(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1) & 0xFFFFFFFF,
            ))
            writer.write_packet(server_fin(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1) & 0xFFFFFFFF, ack=1000,
            ))
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "ip_fragmented"
        )

    def test_capture_truncation_fails_connection(self):
        body, response = self._simple_stream()
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=70,
            )
            writer.write_packet(packets[0][1])
            # Cut the frame inside the TCP payload: record length < on-wire
            # frame, so total_length exceeds captured bytes.
            writer.write_packet(packets[1][1], truncate_to=14 + 20 + 10)
            for _kind, packet in packets[2:]:
                writer.write_packet(packet)
        self.assertEqual(self.run_cli(), 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "capture_truncated"
        )

    def test_variable_header_lengths_are_skipped_correctly(self):
        body = b"options-header payload!!"  # 24 bytes
        self.assertEqual(len(body), 24)
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 24\r\n\r\n" + body
        )
        ip_options = b"\x01\x01\x01\x01" * 2  # 8 bytes -> IHL 7
        tcp_options = b"\x01\x01\x05\x0a\x00\x00\x00\x00"  # 8 -> offset 7
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=200,
                ip_options=ip_options, tcp_options=tcp_options,
            )
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(
            self.output,
            "conn_192.168.1.20_44444__10.10.0.1_8080",
            "body_1.bin",
        ), "rb") as handle:
            self.assertEqual(handle.read(), body)

    def test_stream_size_limit_is_diagnostic(self):
        body = b"z" * 64
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 64\r\n\r\n" + body
        )
        with PcapWriter(self.pcap) as writer:
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=32,
            )
        code = self.run_cli("--max-stream-bytes", "20")
        self.assertEqual(code, 1)
        import json
        with open(os.path.join(self.output, "index.json"),
                  encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(
            index["connections"][0]["failure_reason"], "stream_limit"
        )

    def test_fin_piggybacked_on_last_data_segment(self):
        body = b"fin rides on the final segment!!!!!!"  # 36 bytes
        self.assertEqual(len(body), 36)
        response = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 36\r\n\r\n" + body
        )
        with PcapWriter(self.pcap) as writer:
            packets = stream_packets(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response, mss=40,
            )
            writer.write_packet(packets[0][1])  # SYN+ACK
            data_packets = [packet for _kind, packet in packets[1:-1]]
            for packet in data_packets[:-1]:
                writer.write_packet(packet)
            # Replace the standalone FIN with a FIN flag set on the last
            # data segment (same sequence semantics).
            last_payload = response[40:]
            fin_segment = server_fin(
                SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                seq=(ISN + 1 + 40) & 0xFFFFFFFF, ack=1000,
                payload=last_payload,
            )
            writer.write_packet(fin_segment)
        self.assertEqual(self.run_cli(), 0)
        with open(os.path.join(
            self.output,
            "conn_192.168.1.20_44444__10.10.0.1_8080",
            "body_1.bin",
        ), "rb") as handle:
            self.assertEqual(handle.read(), body)

    def test_connection_limit_aborts_the_run_diagnostically(self):
        from pcap_recover.errors import LimitExceeded
        body = b"x"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx"
        with PcapWriter(self.pcap) as writer:
            # Two distinct four-tuples talking to the same endpoint.
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, CLIENT_IP, CLIENT_PORT,
                isn=ISN, stream=response,
            )
            write_tcp_stream(
                writer, SERVER_IP, SERVER_PORT, "192.168.1.21",
                CLIENT_PORT + 100, isn=0x12340000, stream=response,
            )
        with self.assertRaises(LimitExceeded):
            self.run_api(limits={
                "max_connections": 1,
                "max_stream_bytes": 256 * 1024 * 1024,
                "max_header_bytes": 65536,
                "max_decoded_bytes": 256 * 1024 * 1024,
            })
        self.assertFalse(os.path.exists(self.output))


if __name__ == "__main__":
    unittest.main()
