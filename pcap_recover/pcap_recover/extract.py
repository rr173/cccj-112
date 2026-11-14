"""Offline recovery pipeline: PCAP in, verified response objects out.

The pipeline is all-or-nothing *per connection* and all-or-nothing *per
run* for structural input damage:

1. the whole PCAP is read first -- FIN can precede earlier data packets in
   the capture, so completeness cannot be judged mid-file;
2. each good connection's reconstructed stream is parsed and decoded fully
   before any of its files are visible;
3. results are assembled in a hidden sibling directory and renamed into the
   caller-given (new) output directory in one ``os.rename`` step, so a
   structural failure publishes nothing and a connection failure never
   leaves a partial connection behind.
"""

import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile

from .config import load_endpoints
from .encoding import decode_body
from .errors import (
    ConnectionFailure,
    FatalInputError,
    HttpParseError,
    LimitExceeded,
)
from .http import extract_chunked_payload, parse_responses
from .netparse import parse_packet
from .pcap import PcapReader
from .reassembly import Reassembler

_DEFAULT_LIMITS = {
    "max_connections": 1024,
    "max_stream_bytes": 256 * 1024 * 1024,
    "max_header_bytes": 64 * 1024,
    "max_decoded_bytes": 256 * 1024 * 1024,
}


def run_recovery(pcap_path, config_path, output_dir, limits=None):
    """Execute one offline recovery run.

    Returns the total-index dict (also written to ``output_dir``).  Raises
    :class:`FatalInputError` / :class:`LimitExceeded` for run-aborting
    conditions; per-connection failures are returned in the index.
    """
    effective_limits = dict(_DEFAULT_LIMITS)
    if limits:
        effective_limits.update(limits)
    endpoints = load_endpoints(config_path)

    # Reserve the output location before touching the input: refusing an
    # existing directory must never leave partial results behind.
    staging = _prepare_staging(output_dir)
    reassembler = Reassembler(
        endpoints,
        max_connections=effective_limits["max_connections"],
        max_stream_bytes=effective_limits["max_stream_bytes"],
    )
    try:
        with open(pcap_path, "rb") as handle:
            reader = PcapReader(handle)
            for record in reader:
                try:
                    parsed = parse_packet(record["data"])
                except ConnectionFailure as exc:
                    _attribute_parse_failure(reassembler, record["data"], exc)
                    continue
                if parsed is None:
                    continue
                reassembler.handle_packet(parsed)

        good = reassembler.finalize()
        index = _build_index(
            staging, reassembler, good, effective_limits,
            pcap_path, config_path,
        )
        with open(os.path.join(staging, "index.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.rename(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


def _attribute_parse_failure(reassembler, frame, exc):
    """Attach a frame-level parse failure to its four-tuple when known."""
    candidate = _best_effort_tuple(frame)
    if candidate is None:
        # The frame is too damaged even to identify its endpoints; skipping
        # it is safe because any tracked connection that needed those bytes
        # will later fail its own gap/FIN completeness check.
        return
    state = reassembler.connections.get(candidate)
    if state is not None and state.ok and state.isn is not None:
        state.failed = (exc.reason, exc.detail)


def _best_effort_tuple(frame):
    if len(frame) < 14 + 20 + 4:
        return None
    if struct.unpack(">H", frame[12:14])[0] != 0x0800:
        return None
    ip = frame[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ihl < 20 or len(ip) < ihl + 4 or ip[9] != 6:
        return None
    src_ip = socket.inet_ntoa(ip[12:16])
    dst_ip = socket.inet_ntoa(ip[16:20])
    tcp = ip[ihl:]
    if len(tcp) < 4:
        return None
    src_port, dst_port = struct.unpack(">HH", tcp[:4])
    return dst_ip, dst_port, src_ip, src_port


def _prepare_staging(output_dir):
    parent = os.path.dirname(os.path.abspath(output_dir))
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(output_dir):
        raise FatalInputError(
            "output_exists",
            f"output directory {output_dir} already exists; refusing to "
            "overwrite",
        )
    return tempfile.mkdtemp(prefix=".pcap_recover-", dir=parent)


def _build_index(staging, reassembler, good, limits, pcap_path, config_path):
    connection_entries = []
    object_count = 0

    for key, stream, state in good:
        client_ip, client_port, server_ip, server_port = key
        conn_dir_name = _connection_dir_name(key)
        conn_dir = os.path.join(staging, conn_dir_name)
        os.makedirs(conn_dir)
        try:
            responses = parse_responses(stream, limits["max_header_bytes"])
            objects = _publish_responses(
                conn_dir, conn_dir_name, responses, stream, key, limits
            )
        except (HttpParseError, LimitExceeded) as exc:
            reason = getattr(exc, "reason", "limit_exceeded")
            # Back-fill the connection state so the summary has a single
            # source of truth: every failed connection lives in state.failed.
            state.failed = (reason, str(exc))
            shutil.rmtree(conn_dir, ignore_errors=True)
            continue

        object_count += len(objects)
        connection_entries.append(
            {
                "status": "ok",
                "four_tuple": {
                    "client_ip": client_ip,
                    "client_port": client_port,
                    "server_ip": server_ip,
                    "server_port": server_port,
                },
                "first_packet_index": state.first_index,
                "last_packet_index": state.last_index,
                "stream_length_bytes": len(stream),
                "response_count": len(responses),
                "object_count": len(objects),
                "connection_dir": conn_dir_name,
                "objects": objects,
            }
        )

    failed_connections = 0
    for key, state in reassembler.connections.items():
        if state.failed is None:
            continue
        failed_connections += 1
        reason, detail = state.failed
        connection_entries.append(_failure_entry(key, state, reason, detail))

    connection_entries.sort(key=lambda entry: (
        entry["status"] != "ok",
        entry["four_tuple"]["client_ip"],
        entry["four_tuple"]["client_port"],
        entry["four_tuple"]["server_ip"],
        entry["four_tuple"]["server_port"],
    ))
    total = len(reassembler.connections)
    return {
        "schema_version": 1,
        "source_pcap": os.path.abspath(pcap_path),
        "endpoint_config": os.path.abspath(config_path),
        "summary": {
            "selected_connections": total,
            "successful_connections": total - failed_connections,
            "failed_connections": failed_connections,
            "published_objects": object_count,
        },
        "connections": connection_entries,
    }


def _publish_responses(conn_dir, conn_dir_name, responses, stream, key,
                       limits):
    objects = []
    next_object_index = 1
    client_ip, client_port, server_ip, server_port = key
    for response in responses:
        encodings = [value for name, _orig, value in response.header_fields
                     if name == "content-encoding"]
        if len(encodings) > 1:
            raise HttpParseError(
                "unsupported_content_encoding",
                f"multiple Content-Encoding headers are not supported: "
                f"{encodings!r}",
            )
        encoding = encodings[0] if encodings else None
        if response.body_start is None:
            continue  # 1xx / 204 / 304: no object is produced
        wire_body = stream[response.body_start:response.body_end]
        if response.framing == "chunked":
            # De-chunk first; any Content-Encoding applies to these bytes.
            raw_body = extract_chunked_payload(
                stream, response.body_start, response.body_end
            )
        else:
            raw_body = wire_body
        decoded, applied_encoding = decode_body(
            raw_body, encoding, limits["max_decoded_bytes"]
        )
        body_name = f"body_{next_object_index}.bin"
        with open(os.path.join(conn_dir, body_name), "wb") as handle:
            handle.write(decoded)
        digest = hashlib.sha256(decoded).hexdigest()
        metadata = {
            "schema_version": 1,
            "four_tuple": {
                "client_ip": client_ip,
                "client_port": client_port,
                "server_ip": server_ip,
                "server_port": server_port,
            },
            "http_response_index": response.http_index,
            "object_index": next_object_index,
            "status_code": response.status_code,
            "reason_phrase": response.reason_phrase,
            "headers": [
                {"name": name, "value": value}
                for _lower, name, value in response.header_fields
            ],
            "stream_offsets": {
                "response_start": response.stream_start,
                "header_end": response.header_end,
                "body_start": response.body_start,
                "body_end": response.body_end,
            },
            "framing": response.framing,
            "wire_body_length_bytes": len(wire_body),
            "content_encoding": applied_encoding,
            "raw_body_length_bytes": len(raw_body),
            "decoded_body_length_bytes": len(decoded),
            "body_sha256": digest,
            "body_file": body_name,
        }
        with open(
            os.path.join(conn_dir, f"metadata_{next_object_index}.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        objects.append(
            {
                "object_index": next_object_index,
                "http_response_index": response.http_index,
                "status_code": response.status_code,
                "framing": response.framing,
                "body_file": f"{conn_dir_name}/{body_name}",
                "body_sha256": digest,
                "wire_body_length_bytes": len(wire_body),
                "raw_body_length_bytes": len(raw_body),
                "decoded_body_length_bytes": len(decoded),
                "stream_offsets": metadata["stream_offsets"],
            }
        )
        next_object_index += 1
    return objects


def _connection_dir_name(key):
    client_ip, client_port, server_ip, server_port = key
    return f"conn_{client_ip}_{client_port}__{server_ip}_{server_port}"


def _failure_entry(key, state, reason, detail):
    client_ip, client_port, server_ip, server_port = key
    return {
        "status": "failed",
        "four_tuple": {
            "client_ip": client_ip,
            "client_port": client_port,
            "server_ip": server_ip,
            "server_port": server_port,
        },
        "first_packet_index": state.first_index,
        "last_packet_index": state.last_index,
        "failure_reason": reason,
        "failure_detail": detail,
    }
