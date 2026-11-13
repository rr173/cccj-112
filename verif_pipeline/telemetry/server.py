"""Local TCP receive service for gauge telemetry.

Usage::

    python3 scripts/telemetry_server.py --db runtime/telemetry.sqlite3 \\
        --stations data/obs/stations.csv --port 8765

Each TCP connection carries framed messages (see :mod:`telemetry.frame`);
every *valid* frame receives exactly one line-delimited JSON reply::

    {"schema":"telemetry-reply/1","ack":true,"status":"ACK",
     "message_type":"begin","station_id":"S1","batch_id":"b1",...}

Malformed frames (bad CRC, invalid length, undecodable body) are skipped
without a reply -- they carry no trustworthy identity -- and the decoder
keeps scanning for the next valid frame.  Replies are written only after
the database transaction that produced them has committed.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.frame import DEFAULT_MAX_BUFFER, FrameDecoder  # noqa: E402
from telemetry.store import TelemetryStore  # noqa: E402


class ServerState:
    def __init__(self, store, journal_path=None, drop_ack_after=0):
        self.store = store
        self.journal_path = journal_path
        self.drop_ack_after = int(drop_ack_after or 0)
        self._lock = threading.Lock()
        self.acked = 0
        self.connections = 0
        self.journal = open(journal_path, "a", encoding="utf-8") if journal_path else None
        self._journal_lock = threading.Lock()

    def next_conn_id(self):
        with self._lock:
            self.connections += 1
            return self.connections

    def should_drop_ack(self):
        """Fault injection: commit the transaction, then die before ACK.

        Returns True exactly once, on the N-th committed write reply, so a
        demo can prove that a lost ACK followed by a gateway resend does
        not duplicate data.
        """
        with self._lock:
            if self.drop_ack_after and self.acked + 1 == self.drop_ack_after:
                return True
            return False

    def note_ack(self):
        with self._lock:
            self.acked += 1

    def record(self, conn_id, direction, payload):
        if self.journal is None:
            return
        entry = {"ts_ms": int(time.time() * 1000), "conn_id": conn_id,
                 "direction": direction, "payload": payload}
        with self._journal_lock:
            self.journal.write(json.dumps(entry, sort_keys=True) + "\n")
            self.journal.flush()

    def close(self):
        if self.journal is not None:
            self.journal.close()
            self.journal = None


class TelemetryHandler(socketserver.BaseRequestHandler):
    def setup(self):
        self.conn_id = self.server.state.next_conn_id()
        self.decoder = FrameDecoder(max_buffer=self.server.max_buffer)

    def handle(self):
        state = self.server.state
        while not self.server.stopping:
            try:
                chunk = self.request.recv(65536)
            except OSError as exc:
                if exc.errno in (errno.ECONNRESET, errno.EPIPE):
                    return
                raise
            if not chunk:
                return
            for event in self.decoder.feed(chunk):
                if event["type"] == "reject":
                    state.record(self.conn_id, "reject", event)
                    continue
                state.record(self.conn_id, "recv", event["message"])
                reply = state.store.handle(event["message"],
                                           source=f"conn{self.conn_id}")
                state.record(self.conn_id, "send", reply)
                if state.should_drop_ack():
                    # Simulated crash: the DB transaction already committed,
                    # but the reply never reaches the gateway.
                    sys.stderr.write(
                        "[telemetry] injected exit after commit, before ACK "
                        f"(conn {self.conn_id}, {reply.get('message_type')} "
                        f"{reply.get('station_id')}/{reply.get('batch_id')})\n")
                    sys.stderr.flush()
                    os._exit(77)
                line = (json.dumps(reply, sort_keys=True,
                                   ensure_ascii=False) + "\n").encode("utf-8")
                state.note_ack()
                try:
                    self.request.sendall(line)
                except OSError:
                    return

    def finish(self):
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class TelemetryServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, state, max_buffer=DEFAULT_MAX_BUFFER):
        self.state = state
        self.max_buffer = max_buffer
        self.stopping = False
        super().__init__(address, TelemetryHandler)

    def shutdown(self):
        self.stopping = True
        super().shutdown()


def serve(db_path, stations_path, host="127.0.0.1", port=0, journal_path=None,
          drop_ack_after=0, ready_path=None):
    """Start the server; returns ``(server, thread, bound_port)``."""
    store = TelemetryStore(db_path, stations_path)
    state = ServerState(store, journal_path=journal_path,
                        drop_ack_after=drop_ack_after)
    server = TelemetryServer((host, port), state)
    bound_port = server.server_address[1]
    if ready_path:
        with open(ready_path, "w", encoding="utf-8") as f:
            f.write(str(bound_port))
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread, bound_port


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--db", default=os.path.join(root, "runtime", "telemetry.sqlite3"))
    ap.add_argument("--stations", default=os.path.join(root, "data", "obs", "stations.csv"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--journal", default=None,
                    help="append every received message and sent reply here (JSONL)")
    ap.add_argument("--max-buffer", type=int, default=DEFAULT_MAX_BUFFER)
    ap.add_argument("--drop-ack-after", type=int, default=0,
                    help="test hook: exit(77) after the N-th committed reply, "
                         "before it is sent")
    ap.add_argument("--ready-file", default=None,
                    help="write the bound port here once listening")
    args = ap.parse_args(argv)

    server, thread, port = serve(
        args.db, args.stations, args.host, args.port,
        journal_path=args.journal, drop_ack_after=args.drop_ack_after,
        ready_path=args.ready_file)
    if args.max_buffer != DEFAULT_MAX_BUFFER:
        server.max_buffer = args.max_buffer
    print(f"telemetry listening on {args.host}:{port}  db={args.db}", flush=True)

    stop = threading.Event()

    def _signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    while not stop.is_set():
        time.sleep(0.1)
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    server.state.store.close()
    server.state.close()
    print("telemetry server stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
