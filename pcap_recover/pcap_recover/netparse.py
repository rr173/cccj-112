"""Parsing of Ethernet / IPv4 / TCP headers.

Only the direction-independent mechanics live here: validating header
lengths, skipping options using the on-wire header lengths and reporting
conditions (IP fragmentation, capture truncation) that make a connection
unextractable.  Direction filtering and endpoint bookkeeping belong to
:mod:`pcap_recover.reassembly`.
"""

import socket
import struct

from .errors import ConnectionFailure

_ETHERTYPE_IPV4 = 0x0800
_ETH_HEADER_SIZE = 14
_IP_PROTO_TCP = 6
_TCP_MIN_HEADER = 20
_IP_MIN_HEADER = 20


def parse_packet(frame):
    """Parse one captured Ethernet frame.

    Returns a dict with the L3/L4 fields or ``None`` when the frame is not
    IPv4/TCP traffic we care about (other ethertypes, other protocols are
    simply skipped).

    Raises :class:`ConnectionFailure` for IPv4/TCP packets whose structure
    is malformed for a tracked connection: fragmentation, capture
    truncation or impossible header lengths.  The caller applies the same
    failure to the owning connection.
    """
    if len(frame) < _ETH_HEADER_SIZE:
        # Truncated below even the Ethernet header: cannot tell whether it
        # belongs to a tracked endpoint.  Treat as a parse failure so a
        # tracked connection cannot silently lose this segment.
        raise ConnectionFailure(
            "capture_truncated",
            f"frame shorter than Ethernet header ({len(frame)} bytes)",
        )
    ethertype = struct.unpack(">H", frame[12:14])[0]
    if ethertype != _ETHERTYPE_IPV4:
        return None

    ip_packet = frame[_ETH_HEADER_SIZE:]
    if len(ip_packet) < _IP_MIN_HEADER:
        raise ConnectionFailure(
            "capture_truncated",
            "captured IPv4 packet shorter than the minimum 20-byte header",
        )
    version_ihl = ip_packet[0]
    version = version_ihl >> 4
    ihl = version_ihl & 0x0F
    if version != 4:
        return None
    ip_header_len = ihl * 4
    if ip_header_len < _IP_MIN_HEADER or ip_header_len > len(ip_packet):
        raise ConnectionFailure(
            "capture_truncated",
            f"IPv4 IHL={ihl} does not fit in captured bytes "
            f"({len(ip_packet)} captured)",
        )

    total_length = struct.unpack(">H", ip_packet[2:4])[0]
    if total_length < ip_header_len or total_length > len(ip_packet):
        # The datagram claims more (or inconsistent) bytes than the capture
        # holds: the packet was cut by the sniffer (or is malformed).
        raise ConnectionFailure(
            "capture_truncated",
            f"IPv4 total length {total_length} inconsistent with captured "
            f"{len(ip_packet)} bytes",
        )

    flags_fragment = struct.unpack(">H", ip_packet[6:8])[0]
    more_fragments = bool(flags_fragment & 0x2000)
    fragment_offset = flags_fragment & 0x1FFF
    if more_fragments or fragment_offset != 0:
        raise ConnectionFailure(
            "ip_fragmented",
            "IPv4 fragmentation is not supported for tracked downloads",
        )

    protocol = ip_packet[9]
    if protocol != _IP_PROTO_TCP:
        return None

    src_ip = socket.inet_ntoa(ip_packet[12:16])
    dst_ip = socket.inet_ntoa(ip_packet[16:20])

    tcp_segment = ip_packet[ip_header_len:total_length]
    if len(tcp_segment) < _TCP_MIN_HEADER:
        raise ConnectionFailure(
            "capture_truncated",
            f"captured TCP segment shorter than 20 bytes "
            f"({len(tcp_segment)} bytes)",
        )
    src_port, dst_port, seq, ack = struct.unpack(">HHII", tcp_segment[:12])
    data_offset_byte = tcp_segment[12]
    tcp_header_len = (data_offset_byte >> 4) * 4
    if tcp_header_len < _TCP_MIN_HEADER or tcp_header_len > len(tcp_segment):
        raise ConnectionFailure(
            "capture_truncated",
            f"TCP data offset {data_offset_byte >> 4} does not fit in "
            f"captured {len(tcp_segment)} bytes",
        )
    flags = tcp_segment[13]
    payload = tcp_segment[tcp_header_len:]

    return {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": src_port,
        "dst_port": dst_port,
        "seq": seq,
        "ack": ack,
        "flags": flags,
        "payload": payload,
    }
