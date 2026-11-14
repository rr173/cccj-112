"""Server-to-client TCP byte stream reconstruction.

The reassembler is deliberately conservative.  For every configured
(server IP, server port) endpoint it tracks only packets travelling from
that endpoint towards a client, keyed by the full four-tuple.  Sequence
numbers are 32-bit and may wrap; the per-connection data span is
contractually smaller than 2**31, which makes unwrapping unambiguous.

Guarantees:

* a SYN+ACK fixes the starting sequence number and the SYN consumes one
  sequence number, so the first data byte is relative offset 0;
* data may arrive out of order, be retransmitted or partially overlap;
  identical bytes at the same sequence number enter the stream once,
  differing bytes fail the whole connection (no picking a "winner");
* a FIN can appear before earlier data packets in the capture --
  completeness is decided only after the whole file is read;
* RST, missing FIN, missing bytes before FIN and four-tuple reuse with a
  different starting ISN all fail the connection with a stable reason.
"""

from .errors import LimitExceeded

_FIN = 0x01
_SYN = 0x02
_RST = 0x04
_ACK = 0x10
_MOD = 1 << 32
_HALF = 1 << 31


class _Connection:
    def __init__(self, key):
        self.key = key
        self.isn = None
        self.intervals = []  # list of [start, end, data] in unwrapped space
        self.anchor = None  # first interval start, unwrapping reference
        self.fin_rel = None
        self.reset = False
        self.failed = None  # (reason, detail) once unextractable
        self.pending = []  # packets seen before the SYN+ACK
        self.first_index = None
        self.last_index = None

    @property
    def ok(self):
        return self.failed is None


class Reassembler:
    def __init__(self, endpoints, max_connections, max_stream_bytes):
        # endpoints: iterable of (ip, port) server pairs
        self._endpoints = set(endpoints)
        self._max_connections = max_connections
        self._max_stream_bytes = max_stream_bytes
        self._connections = {}
        self._packet_index = 0

    @property
    def connections(self):
        return self._connections

    def handle_packet(self, parsed):
        """Process one parsed IPv4/TCP packet (other frames are skipped)."""
        self._packet_index += 1
        idx = self._packet_index
        if (parsed["src_ip"], parsed["src_port"]) not in self._endpoints:
            return  # client->server or unrelated traffic

        key = (
            parsed["dst_ip"],
            parsed["dst_port"],
            parsed["src_ip"],
            parsed["src_port"],
        )
        state = self._connections.get(key)
        is_synack = (
            (parsed["flags"] & (_SYN | _ACK)) == (_SYN | _ACK)
            and not (parsed["flags"] & _RST)
        )

        if state is None:
            if len(self._connections) >= self._max_connections:
                raise LimitExceeded(
                    "connection_limit",
                    f"more than {self._max_connections} distinct selected "
                    "connections in input",
                )
            state = _Connection(key)
            state.first_index = idx
            self._connections[key] = state
        state.last_index = idx
        if state.failed is not None:
            return

        if is_synack:
            self._handle_synack(state, parsed, idx)
            if state.failed is not None:
                return
        elif state.isn is None:
            # Data/control before any handshake on this four-tuple: keep it
            # until (and unless) a SYN+ACK establishes the start point.  The
            # buffer is bounded so a handshake-less flow cannot grow memory
            # without limit; such a connection fails no_syn_ack anyway.
            if sum(len(p["payload"]) for p, _i in state.pending) \
                    >= self._max_stream_bytes:
                state.failed = (
                    "stream_limit",
                    f"more than {self._max_stream_bytes} bytes arrived "
                    "before any SYN+ACK",
                )
                state.pending = []
                return
            state.pending.append((parsed, idx))
            if parsed["flags"] & _RST:
                state.reset = True
            return

        if parsed["flags"] & _RST:
            state.failed = (
                "rst_terminated",
                f"RST observed in packet {idx}; connection cannot be trusted "
                "to hold a complete response stream",
            )
            state.pending = []
            return

        if parsed["payload"]:
            self._insert_payload(state, parsed, idx)
            if state.failed is not None:
                return
        if parsed["flags"] & _FIN:
            self._record_fin(state, parsed)

    def _handle_synack(self, state, parsed, idx):
        if state.isn is not None:
            if state.isn == parsed["seq"]:
                return  # retransmission of the same handshake
            state.failed = (
                "connection_reused",
                f"second handshake on the same four-tuple with a different "
                f"ISN ({state.isn} -> {parsed['seq']}) at packet {idx}",
            )
            return
        state.isn = parsed["seq"]
        pending = state.pending
        state.pending = []
        for old_packet, old_idx in pending:
            if old_packet["flags"] & _RST:
                state.failed = (
                    "rst_terminated",
                    f"RST observed in packet {old_idx}",
                )
                return
            if old_packet["payload"]:
                self._insert_payload(state, old_packet, old_idx)
                if state.failed is not None:
                    return
            if old_packet["flags"] & _FIN:
                self._record_fin(state, old_packet)
        if parsed["payload"]:
            self._insert_payload(state, parsed, idx)
        if parsed["flags"] & _FIN:
            self._record_fin(state, parsed)

    def _unwrap(self, state, relative):
        """Place a modulo-32 relative offset into unwrapped stream space."""
        if state.anchor is None:
            return relative  # first segment anchors the [0, 2**32) window
        candidates = [relative - _MOD, relative, relative + _MOD]
        return min(candidates, key=lambda value: abs(value - state.anchor))

    def _insert_payload(self, state, parsed, idx):
        # The SYN occupies sequence number ISN, so the first data byte
        # (sequence ISN+1) maps to stream offset 0.
        rel = (parsed["seq"] - state.isn - 1) % _MOD
        start = self._unwrap(state, rel)
        if state.anchor is None:
            state.anchor = start
        end = start + len(parsed["payload"])
        if end < 0 or start < 0:
            state.failed = (
                "data_before_syn",
                f"packet {idx} maps to negative stream offset {start}",
            )
            return
        if end > self._max_stream_bytes:
            state.failed = (
                "stream_limit",
                f"reconstructed stream would exceed "
                f"{self._max_stream_bytes} bytes (packet {idx} reaches "
                f"{end})",
            )
            return
        new_data = parsed["payload"]
        for s, e, data in state.intervals:
            overlap_start = max(s, start)
            overlap_end = min(e, end)
            if overlap_start < overlap_end:
                old_part = data[overlap_start - s: overlap_end - s]
                new_part = new_data[overlap_start - start: overlap_end - start]
                if old_part != new_part:
                    state.failed = (
                        "byte_conflict",
                        f"different bytes at stream offset {overlap_start} "
                        f"(packet {idx})",
                    )
                    return
        state.intervals.append([start, end, new_data])
        state.intervals.sort(key=lambda item: item[0])
        state.intervals = self._merge(state.intervals)

    @staticmethod
    def _merge(intervals):
        merged = []
        for start, end, data in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end, data])
                continue
            prev_start, prev_end, prev_data = merged[-1]
            if end <= prev_end:
                continue  # fully contained, already verified identical
            merged[-1][1] = end
            merged[-1][2] = prev_data + data[prev_end - start:]
        return merged

    def _record_fin(self, state, parsed):
        # FIN's sequence number is one past the last data byte; relative to
        # the SYN it equals the total stream length.
        fin_raw = (parsed["seq"] + len(parsed["payload"])) % _MOD
        rel = (fin_raw - state.isn - 1) % _MOD
        state.fin_rel = self._unwrap(state, rel)

    def finalize(self):
        """Return ``[(key, stream_bytes, state), ...]`` for good connections.

        Failed connections remain in ``self.connections`` with their reason;
        the caller turns both kinds into total-index entries.
        """
        good = []
        for key, state in self._connections.items():
            if state.failed is not None:
                continue
            if state.isn is None:
                state.failed = (
                    "no_syn_ack",
                    "no SYN+ACK from the configured server endpoint; the "
                    "stream start sequence is unknown",
                )
                continue
            if state.reset:
                state.failed = (
                    "rst_terminated",
                    "connection ended with RST rather than server FIN",
                )
                continue
            if state.fin_rel is None:
                state.failed = (
                    "no_fin",
                    "capture ends without a server FIN closing this "
                    "direction",
                )
                continue
            if state.fin_rel >= _HALF:
                state.failed = (
                    "span_exceeds_2gb",
                    f"FIN implies a {state.fin_rel}-byte span; the supported "
                    "contract is less than 2**31 bytes per connection",
                )
                continue
            if state.fin_rel > self._max_stream_bytes:
                state.failed = (
                    "stream_limit",
                    f"complete stream of {state.fin_rel} bytes exceeds the "
                    f"configured limit of {self._max_stream_bytes}",
                )
                continue
            stream, reason, detail = self._build_stream(state)
            if reason is not None:
                state.failed = (reason, detail)
                continue
            good.append((key, stream, state))
        good.sort(key=lambda item: item[2].first_index)
        return good

    def _build_stream(self, state):
        expected_end = state.fin_rel
        if not state.intervals:
            if expected_end == 0:
                return b"", None, None
            return None, "missing_bytes", (
                f"FIN expects {expected_end} bytes but no data was captured"
            )
        first = state.intervals[0]
        if first[0] != 0:
            return None, "missing_bytes", (
                f"stream has a gap at offset 0; first seen byte is at "
                f"{first[0]}"
            )
        chunks = []
        cursor = 0
        for start, end, data in state.intervals:
            if start > cursor:
                return None, "missing_bytes", (
                    f"gap of {start - cursor} byte(s) at stream offset "
                    f"{cursor}"
                )
            if end <= cursor:
                continue
            chunks.append(data[cursor - start:])
            cursor = end
        if cursor < expected_end:
            return None, "missing_bytes", (
                f"{expected_end - cursor} byte(s) missing before FIN "
                f"(have {cursor}, need {expected_end})"
            )
        if cursor != expected_end:
            return None, "inconsistent_fin", (
                f"data ends at {cursor} but FIN sequence implies "
                f"{expected_end}"
            )
        return b"".join(chunks), None, None
