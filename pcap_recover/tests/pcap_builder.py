"""Synthetic PCAP fixture builder for the pcap_recover test suite.

The builder writes real classic-PCAP containers and real Ethernet/IPv4/TCP
headers from scratch (only the test data itself is synthetic).  It supports
both container byte orders, variable IPv4 IHL and TCP data offset, packet
reordering, retransmissions, partial overlaps, capture truncation and IP
fragmentation flags.
"""

import struct

MICRO_BE_MAGIC = 0xA1B2C3D4
MICRO_LE_MAGIC = 0xD4C3B2A1
LINKTYPE_ETHERNET = 1

_FIN = 0x01
_SYN = 0x02
_RST = 0x04
_ACK = 0x10
_PSH = 0x08

# Deterministic, locally administered MACs.
SERVER_MAC = b"\x02\x00\x00\x00\x00\x01"
CLIENT_MAC = b"\x02\x00\x00\x00\x00\x02"


class PcapWriter:
    def __init__(self, path, big_endian=True, snaplen=65535):
        self.endian = ">" if big_endian else "<"
        self._handle = open(path, "wb")
        # The magic is written in the file's own byte order, so the same
        # numeric value packs differently for each endianness.
        magic = MICRO_BE_MAGIC
        global_header = struct.pack(
            self.endian + "IHHIIII",
            magic, 2, 4, 0, 0, snaplen, LINKTYPE_ETHERNET,
        )
        self._handle.write(global_header)
        self._index = 0

    def write_packet(self, frame, ts_sec=None, ts_usec=None,
                     truncate_to=None):
        self._index += 1
        if ts_sec is None:
            ts_sec = 1_700_000_000 + self._index
            ts_usec = 0
        data = frame if truncate_to is None else frame[:truncate_to]
        record = struct.pack(
            self.endian + "IIII",
            ts_sec, ts_usec or 0, len(data), len(frame),
        )
        self._handle.write(record + data)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def ipv4_packet(src_ip, dst_ip, tcp_segment, ip_options=b"",
                identification=1, flags_fragment=0, ttl=64):
    """Build an Ethernet/IPv4 frame carrying one TCP segment."""
    version_ihl = 0x40 | ((20 + len(ip_options)) // 4)
    total_length = 20 + len(ip_options) + len(tcp_segment)
    header = struct.pack(
        ">BBHHHBBH4s4s",
        version_ihl, 0, total_length, identification, flags_fragment,
        ttl, 6, 0,
        _pack_ip(src_ip), _pack_ip(dst_ip),
    )
    ip = header + ip_options
    return b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" \
        + struct.pack(">H", 0x0800) + ip + tcp_segment


def _pack_ip(text):
    return bytes(int(part) for part in text.split("."))


def tcp_segment(src_port, dst_port, seq, ack, flags, payload=b"",
                tcp_options=b""):
    data_offset = (20 + len(tcp_options)) // 4
    header = struct.pack(
        ">HHIIBBHHH",
        src_port, dst_port, seq, ack, (data_offset << 4), flags,
        8192, 0, 0,
    )
    return header + tcp_options + payload


def server_synack(server_ip, server_port, client_ip, client_port, isn,
                  ip_options=b"", tcp_options=b""):
    seg = tcp_segment(
        server_port, client_port, isn, 1000, _SYN | _ACK, b"", tcp_options
    )
    return ipv4_packet(server_ip, client_ip, seg, ip_options)


def server_data(server_ip, server_port, client_ip, client_port, seq, ack,
                payload, flags=_ACK | _PSH, ip_options=b"",
                tcp_options=b""):
    seg = tcp_segment(
        server_port, client_port, seq, ack, flags, payload, tcp_options
    )
    return ipv4_packet(server_ip, client_ip, seg, ip_options)


def server_fin(server_ip, server_port, client_ip, client_port, seq, ack,
               payload=b"", ip_options=b"", tcp_options=b""):
    seg = tcp_segment(
        server_port, client_port, seq, ack, _FIN | _ACK, payload,
        tcp_options,
    )
    return ipv4_packet(server_ip, client_ip, seg, ip_options)


def server_rst(server_ip, server_port, client_ip, client_port, seq, ack):
    seg = tcp_segment(server_port, client_port, seq, ack, _RST | _ACK)
    return ipv4_packet(server_ip, client_ip, seg)


def client_packet(client_ip, client_port, server_ip, server_port, seq, ack,
                  flags, payload=b""):
    """Client-to-server packet; the tool must ignore this direction."""
    seg = tcp_segment(client_port, server_port, seq, ack, flags, payload)
    frame = (
        b"\x02\x00\x00\x00\x00\x01"
        + b"\x02\x00\x00\x00\x00\x02"
        + struct.pack(">H", 0x0800)
    )
    header = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0, 20 + len(seg), 7, 0, 64, 6, 0,
        _pack_ip(client_ip), _pack_ip(server_ip),
    )
    return frame + header + seg


def fragmented_frame(server_ip, server_port, client_ip, client_port, seq):
    """An IPv4 frame explicitly marked as a non-first fragment."""
    payload = b"x" * 40
    seg = tcp_segment(server_port, client_port, seq, 1000, _ACK, payload)
    # MF=1, fragment offset 10 (in 8-byte units).
    return ipv4_packet(
        server_ip, client_ip, seg, identification=99,
        flags_fragment=0x2000 | (10 & 0x1FFF),
    )


# --------------------------------------------------------------------- HTTP


def http_response(status_line, headers, body=b""):
    """Build raw HTTP/1.1 response bytes (headers already include framing)."""
    out = status_line + b"\r\n"
    for name, value in headers:
        out += name + b": " + value + b"\r\n"
    out += b"\r\n" + body
    return out


def content_length_response(code, body, extra_headers=(), reason=None):
    if reason is None:
        reason = b"OK"
    headers = [(b"Content-Length", str(len(body)).encode())]
    headers.extend(extra_headers)
    return http_response(b"HTTP/1.1 " + str(code).encode() + b" " + reason,
                         headers, body)


def chunked_response(chunks, code=200, extra_headers=(), trailers=(),
                     first_extension=None):
    headers = [(b"Transfer-Encoding", b"chunked")]
    headers.extend(extra_headers)
    head = http_response(
        b"HTTP/1.1 " + str(code).encode() + b" OK", headers, b""
    )
    body = b""
    for index, chunk in enumerate(chunks):
        size_line = f"{len(chunk):X}".encode()
        if index == 0 and first_extension is not None:
            size_line += b";" + first_extension
        body += size_line + b"\r\n" + chunk + b"\r\n"
    body += b"0\r\n"
    for name, value in trailers:
        body += name + b": " + value + b"\r\n"
    body += b"\r\n"
    return head + body


def gzip_response(body, gz, extra_headers=()):
    headers = [
        (b"Content-Encoding", b"gzip"),
        (b"Content-Length", str(len(gz)).encode()),
    ]
    headers.extend(extra_headers)
    return http_response(b"HTTP/1.1 200 OK", headers, gz)


def write_tcp_stream(writer, server_ip, server_port, client_ip, client_port,
                     isn, stream, ack=1000, mss=100, ip_options=b"",
                     tcp_options=b""):
    """Write a complete server->client exchange in natural capture order."""
    for _kind, packet in stream_packets(
        server_ip, server_port, client_ip, client_port, isn, stream,
        ack=ack, mss=mss, ip_options=ip_options, tcp_options=tcp_options,
    ):
        writer.write_packet(packet)


def stream_packets(server_ip, server_port, client_ip, client_port, isn,
                   stream, ack=1000, mss=100, ip_options=b"",
                   tcp_options=b""):
    """Return the natural-order packet list (SYN+ACK, data..., FIN)."""
    packets = [("synack", server_synack(
        server_ip, server_port, client_ip, client_port, isn,
        ip_options, tcp_options,
    ))]
    for offset in range(0, len(stream), mss):
        piece = stream[offset:offset + mss]
        seq = (isn + 1 + offset) & 0xFFFFFFFF
        packets.append(("data", server_data(
            server_ip, server_port, client_ip, client_port, seq, ack, piece,
            ip_options=ip_options, tcp_options=tcp_options,
        )))
    fin_seq = (isn + 1 + len(stream)) & 0xFFFFFFFF
    packets.append(("fin", server_fin(
        server_ip, server_port, client_ip, client_port, fin_seq, ack,
        ip_options=ip_options, tcp_options=tcp_options,
    )))
    return packets


def retransmit_packet(server_ip, server_port, client_ip, client_port, seq,
                      ack, payload, mutate_offset=None, mutate_byte=None,
                      ip_options=b"", tcp_options=b""):
    if mutate_offset is not None:
        data = bytearray(payload)
        data[mutate_offset] = mutate_byte
        payload = bytes(data)
    return server_data(
        server_ip, server_port, client_ip, client_port, seq, ack, payload,
        ip_options=ip_options, tcp_options=tcp_options,
    )


def partial_overlap_packet(server_ip, server_port, client_ip, client_port,
                           seq, ack, payload, skip=0,
                           ip_options=b"", tcp_options=b""):
    """Retransmit starting at ``seq+skip`` with identical overlapping bytes."""
    return server_data(
        server_ip, server_port, client_ip, client_port,
        (seq + skip) & 0xFFFFFFFF, ack, payload[skip:],
        ip_options=ip_options, tcp_options=tcp_options,
    )
