"""Build the two-connection demonstration capture (fully offline).

The generated PCAP contains two simultaneous downloads from the same
configured server endpoint, distinguished by their full four-tuples:

* connection A (client port 51001): a chunked, gzip-encoded catalog
  response followed immediately by a second plain Content-Length response;
* connection B (client port 51002): an ordinary response whose middle data
  segment is retransmitted with one byte changed, which must make the
  whole connection unextractable.

The script also writes the endpoint configuration consumed by the
recovery command.  No network access is involved.
"""

import gzip
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pcap_builder import (  # noqa: E402
    PcapWriter,
    chunked_response,
    content_length_response,
    retransmit_packet,
    server_data,
    server_fin,
    server_synack,
    stream_packets,
)

SERVER_IP = "203.0.113.10"
SERVER_PORT = 8080
CLIENT_A_IP = "198.51.100.7"
CLIENT_A_PORT = 51001
CLIENT_B_IP = "198.51.100.8"
CLIENT_B_PORT = 51002

CATALOG_TEXT = (
    "vendor-directory snapshot\n"
    "entry,partner,updated\n"
    "1,Northwind Spares,2026-11-13\n"
    "2,Contoso Components,2026-11-13\n"
    "3,Fabrikam Fittings,2026-11-13\n"
).encode("utf-8")

SECOND_BODY = b'{"status": "catalog-fetch-complete", "count": 3}\n'


def _chunked_gzip_stream():
    gz = gzip.compress(CATALOG_TEXT, mtime=0)
    first = chunked_response(
        [gz[:12], gz[12:24], gz[24:]],
        extra_headers=[
            (b"Content-Type", b"application/jsonl"),
            (b"Content-Encoding", b"gzip"),
            (b"X-Catalog", b"vendor-directory"),
        ],
        trailers=[(b"ETag", b'"d41d8cd9-51001"')],
        first_extension=b"seg=1",
    )
    second = content_length_response(
        200, SECOND_BODY,
        extra_headers=[(b"Content-Type", b"application/json")],
    )
    return gz, first + second


def build(demo_dir):
    os.makedirs(demo_dir, exist_ok=True)
    pcap_path = os.path.join(demo_dir, "two_downloads.pcap")
    config_path = os.path.join(demo_dir, "endpoints.json")
    expected_path = os.path.join(demo_dir, "expected.json")

    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"endpoints": [{"ip": SERVER_IP, "port": SERVER_PORT}]},
            handle, indent=2,
        )

    gz, stream_a = _chunked_gzip_stream()
    body_b = b"directory rows that will disagree on retransmission!!\n" * 3
    stream_b = content_length_response(200, body_b)

    with PcapWriter(pcap_path) as writer:
        # Interleave the two handshakes so the capture really shows two
        # simultaneous downloads.
        isn_a, isn_b = 0x10000000, 0x20000000
        writer.write_packet(server_synack(
            SERVER_IP, SERVER_PORT, CLIENT_A_IP, CLIENT_A_PORT, isn_a,
        ))
        writer.write_packet(server_synack(
            SERVER_IP, SERVER_PORT, CLIENT_B_IP, CLIENT_B_PORT, isn_b,
        ))

        packets_a = stream_packets(
            SERVER_IP, SERVER_PORT, CLIENT_A_IP, CLIENT_A_PORT,
            isn=isn_a, stream=stream_a, mss=120,
        )
        packets_b = stream_packets(
            SERVER_IP, SERVER_PORT, CLIENT_B_IP, CLIENT_B_PORT,
            isn=isn_b, stream=stream_b, mss=80,
        )
        data_a = [p for _k, p in packets_a[1:-1]]
        data_b = [p for _k, p in packets_b[1:-1]]

        # Alternate packets between the connections.
        for index in range(max(len(data_a), len(data_b))):
            if index < len(data_a):
                writer.write_packet(data_a[index])
            if index < len(data_b):
                writer.write_packet(data_b[index])

        # Connection B retransmits its first data segment with one changed
        # byte: the overlap is real but the bytes conflict.
        conflict_offset = 25
        conflict_byte = body_b[conflict_offset] ^ 0x01
        writer.write_packet(retransmit_packet(
            SERVER_IP, SERVER_PORT, CLIENT_B_IP, CLIENT_B_PORT,
            seq=(isn_b + 1) & 0xFFFFFFFF, ack=1000,
            payload=stream_b[:80],
            mutate_offset=conflict_offset,
            mutate_byte=conflict_byte,
        ))

        writer.write_packet(packets_a[-1][1])  # FIN for A
        writer.write_packet(packets_b[-1][1])  # FIN for B

    expected = {
        "catalog_body": {
            "sha256_hex": __import__("hashlib").sha256(CATALOG_TEXT)
            .hexdigest(),
            "decoded_length": len(CATALOG_TEXT),
            "gzip_wire_length": len(gz),
        },
        "second_body": {
            "exact_bytes_hex": SECOND_BODY.hex(),
            "length": len(SECOND_BODY),
        },
        "connection_b_failure_reason": "byte_conflict",
        "expected_exit_code": 1,
        "expected_published_object_count": 2,
    }
    with open(expected_path, "w", encoding="utf-8") as handle:
        json.dump(expected, handle, indent=2)
        handle.write("\n")
    return pcap_path, config_path, expected_path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        _ROOT, "demo", "data"
    )
    paths = build(target)
    for path in paths:
        print(path)
