"""Hand-calculated end-to-end telemetry demo.

Runs a real TCP server and real gateway clients over loopback, then
inspects the SQLite database and the exported observation values.  Every
expected number is derived by hand.

Stations and accepted coverage (all UTC, 2026-02-01 unless noted)::

    S1  [00:00,06:00)  tips 00:30=1 01:00=2 02:10=3 03:00=4 03:00=5
                            04:40=2 05:50=2
    S2  [00:00,03:00) + [03:00,06:00)   two adjacent batches
    S3  [00:00,06:00)  count=0          (fully covered, genuinely no rain)
    S4  [00:00,03:00)  3 tips;         [03:00,06:00) only unaccepted data

Export values (0.2 mm per tip, pulse at exactly 03:00Z -> 03-06 bucket)::

    S1 03Z = (1+2+3)*0.2 = 1.2       S1 06Z = (4+5+2+2)*0.2 = 2.6
    S2 03Z = (2+3)*0.2   = 1.0       S2 06Z = (4+3)*0.2     = 1.4
    S3 03Z = 0                       S3 06Z = 0
    S4 03Z = 3*0.2       = 0.6       S4 06Z = no row, gap 03:00-06:00

Scenarios asserted against the wire journal, the database and the export:

1.  a begin frame delivered one byte per write (split across reads)
2.  a half frame produces no reply and no row until completed
3.  seven frames glued into one write, out of order, with a duplicate
4.  garbage bytes, a bad CRC and an invalid length between valid frames
5.  one missing seq: seal NACKs with the exact missing range; a resend
    completes the batch
6.  tip before begin -> NACK unknown_batch, no batch created
7.  digest mismatch -> NACK, batch stays unaccepted
8.  two connections concurrently send different content for one seq:
    exactly one wins, the other is NACKed, first content kept
9.  concurrent seal of two overlapping batches: exactly one accepted
10. process exits after commit but before ACK: restart + gateway resend
    does not double any tip
11. export: 0.2 mm decimal arithmetic, boundary pulse, adjacency,
    zero-rain bucket, unaccepted-only bucket, one-second coverage hole
12. export rewrites (never appends) and is repeatable

Everything is written under ``runtime/demo/``; the demo deletes that
directory first, so re-running is reproducible.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from telemetry.export import export_observations  # noqa: E402
from telemetry.frame import encode_frame  # noqa: E402
from telemetry.gateway import (GatewayClient, build_batch, seal_message,  # noqa: E402
                               send_batch)
from telemetry.server import serve  # noqa: E402
from telemetry.store import TelemetryStore  # noqa: E402

DAY0_MS = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)


def T(day: int, hh_mm: str) -> int:
    """UTC epoch ms at ``HH:MM`` on 2026-02-01 plus ``day`` days."""
    hh, mm = hh_mm.split(":")
    return DAY0_MS + day * 86400000 + (int(hh) * 60 + int(mm)) * 60 * 1000


def iso(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


class Demo:
    def __init__(self, workdir: str):
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.db = os.path.join(workdir, "telemetry.sqlite3")
        self.stations = os.path.join(ROOT, "data", "obs", "stations.csv")
        self.journal = os.path.join(workdir, "gateway_journal.jsonl")
        self.log_path = os.path.join(workdir, "demo.log")
        self.export_path = os.path.join(workdir, "station_obs_telemetry.csv")
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        self.server = None
        self.thread = None
        self.port = None
        self.checks = []
        self._conn_seq = 0

    # -- infrastructure ---------------------------------------------------
    def log(self, text: str):
        print(text, flush=True)
        self.log_file.write(text + "\n")
        self.log_file.flush()

    def check(self, name: str, condition: bool, detail: str = ""):
        status = "PASS" if condition else "FAIL"
        self.log(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
        self.checks.append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def start_server(self, drop_ack_after: int = 0):
        self.server, self.thread, self.port = serve(
            self.db, self.stations, port=0, journal_path=self.journal,
            drop_ack_after=drop_ack_after)
        return self.port

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.server.server_close()
            self.server.state.store.close()
            self.server.state.close()
            self.server = None
            self.thread = None
            self.port = None

    def connect(self, timeout=10.0) -> GatewayClient:
        self._conn_seq += 1
        return GatewayClient("127.0.0.1", self.port, timeout=timeout)

    # -- database assertions ---------------------------------------------
    def query(self, sql, args=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def count(self, sql, args=()):
        return self.query(sql, args)[0]["n"]

    def journal_entries(self, direction=None, conn_id=None):
        out = []
        if not os.path.exists(self.journal):
            return out
        with open(self.journal, encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                if direction is not None and entry["direction"] != direction:
                    continue
                if conn_id is not None and entry["conn_id"] != conn_id:
                    continue
                out.append(entry)
        return out

    # -- convenience ------------------------------------------------------
    @staticmethod
    def batch(station, batch_id, t0, t1, tips, tip_mm="0.2", count=None):
        return build_batch(station, batch_id, t0, t1, tips, tip_mm=tip_mm,
                           count=count)

    def upload(self, client, station, batch_id, t0, t1, tips, **kw):
        begin, tip_msgs = self.batch(station, batch_id, t0, t1, tips, **kw)
        replies = send_batch(client, begin, tip_msgs)
        return begin, tip_msgs, replies


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
def scenario_framing(demo: Demo):
    """1-4: split frames, glued frames, duplicates, garbage, bad CRC."""
    demo.log("\n== scenario 1-4: framing, reordering, duplicates, bad frames ==")
    client = demo.connect()
    day = 3  # 2026-02-04, isolated from the export window
    begin, tips = demo.batch("S1", "s1-frame", T(day, "00:00"), T(day, "06:00"), [
        (0, T(day, "00:30"), 1), (1, T(day, "01:00"), 2),
        (2, T(day, "02:10"), 3), (3, T(day, "03:00"), 4),
        (4, T(day, "03:00"), 5), (5, T(day, "04:40"), 2),
        (6, T(day, "05:50"), 2)])

    # (1) one complete begin frame, one byte per write
    raw = encode_frame(begin)
    for i in range(len(raw)):
        client.send_raw(raw[i:i + 1])
    reply = client.read_reply()
    demo.check("begin split across many reads is ACKed",
               reply["ack"] and reply["message_type"] == "begin",
               json.dumps(reply))

    # (2) all but the last byte of a tip frame: no reply, no row
    whole = encode_frame(tips[0])
    client.send_raw(whole[:-1])
    half_reply = client.wait_reply(timeout=0.3)
    demo.check("half a frame is never treated as a full message",
               half_reply is None
               and demo.count("SELECT COUNT(*) n FROM tips") == 0,
               "no reply, 0 tips while the tail is incomplete")
    client.send_raw(whole[-1:])
    reply = client.read_reply()
    demo.check("completing the half frame ACKs seq 0",
               reply["ack"] and reply["seq"] == 0, json.dumps(reply))

    # (3+4) one write containing garbage, a bad CRC, an invalid length and
    # seven valid frames out of order (one duplicate)
    payload = bytearray()
    payload += b"\xde\xad\xbe\xef garbage bytes \xa5"
    bad = bytearray(encode_frame(tips[0]))
    bad[-1] ^= 0xFF  # corrupt the CRC of an otherwise complete frame
    payload += bad
    payload += b"\xa5\x5a\x10\x01"  # valid magic, body length 4097 (invalid)
    for m in [tips[3], tips[2], tips[1], tips[5], tips[4], tips[2], tips[6]]:
        payload += encode_frame(m)
    client.send_raw(bytes(payload))
    replies = [client.read_reply() for _ in range(7)]
    demo.check("seven glued frames ACKed despite garbage / bad CRC / bad length",
               all(r["ack"] and r["message_type"] == "tip" for r in replies),
               f"{sum(1 for r in replies if r['ack'])} ACKs")
    demo.check("out-of-order tips accepted and the duplicate detected",
               [r["seq"] for r in replies] == [3, 2, 1, 5, 4, 2, 6]
               and any(r.get("duplicate") and r["seq"] == 2 for r in replies),
               f"duplicates={[r['seq'] for r in replies if r.get('duplicate')]}")
    demo.check("bad frames created no rows; decoder resynced",
               demo.count("SELECT COUNT(*) n FROM tips WHERE batch_id="
                          "'s1-frame'") == 7,
               "7 distinct tips stored")
    demo.check("gateway received all replies with nothing left over",
               len(client.pending) == 0, f"pending={len(client.pending)}")
    time.sleep(0.1)  # let the server journal its rejections
    reject_reasons = {e["payload"]["reason"]
                      for e in demo.journal_entries(direction="reject")}
    demo.check("server decoder rejected bad crc / invalid length / garbage",
               {"bad_crc", "invalid_length", "garbage"} <= reject_reasons,
               json.dumps(sorted(reject_reasons)))

    # duplicate begin and duplicate seal
    before = demo.count("SELECT COUNT(*) n FROM batches")
    reply = client.send_message(begin)
    demo.check("identical begin resend -> ACK duplicate, no new row",
               reply["ack"] and reply.get("duplicate")
               and demo.count("SELECT COUNT(*) n FROM batches") == before,
               json.dumps({k: reply.get(k) for k in ("ack", "duplicate")}))
    reply = client.send_message(seal_message("S1", "s1-frame"))
    demo.check("seal after complete upload -> accepted",
               reply["ack"] and reply["batch_status"] == "accepted",
               json.dumps(reply.get("accepted")))
    reply = client.send_message(seal_message("S1", "s1-frame"))
    demo.check("repeated seal -> ACK, exactly one accepted row",
               reply["ack"] and reply.get("duplicate")
               and demo.count("SELECT COUNT(*) n FROM accepted_batches") == 1,
               json.dumps({k: reply.get(k) for k in ("ack", "duplicate")}))

    # same identity, different content
    conflict = dict(begin, count=99, sha256="0" * 64)
    reply = client.send_message(conflict)
    demo.check("same identity, different begin content -> NACK identity_conflict",
               not reply["ack"] and reply["reason"] == "identity_conflict")
    stored = demo.query("SELECT count FROM batches WHERE station_id='S1' "
                        "AND batch_id='s1-frame'")[0]["count"]
    demo.check("first begin content kept", stored == 7, f"count={stored}")
    client.close()


def scenario_missing_seq(demo: Demo):
    """5-7: missing seq, tip before begin, digest mismatch."""
    demo.log("\n== scenario 5-7: missing seq, orphan tip, digest mismatch ==")
    client = demo.connect()
    day = 2  # 2026-02-03
    all_tips = [(0, T(day, "00:10"), 10), (1, T(day, "00:20"), 20),
                (2, T(day, "00:30"), 30), (3, T(day, "00:40"), 40),
                (4, T(day, "00:50"), 50)]
    begin, tip_msgs = demo.batch("S2", "s2-gap", T(day, "00:00"),
                                 T(day, "03:00"), all_tips)
    client.send_message(begin)
    for m in tip_msgs:
        if m["seq"] == 2:
            continue  # simulated packet loss
        client.send_message(m)
    reply = client.send_message(seal_message("S2", "s2-gap"))
    demo.check("seal with one missing seq -> NACK incomplete",
               not reply["ack"] and reply["reason"] == "incomplete",
               json.dumps(reply["detail"]))
    demo.check("missing range reported exactly",
               reply["detail"]["missing_ranges"] == ["2"]
               and reply["detail"]["missing_seq_count"] == 1,
               json.dumps(reply["detail"]["missing_ranges"]))
    demo.check("incomplete batch is not usable for analysis",
               demo.count("SELECT COUNT(*) n FROM accepted_batches "
                          "WHERE batch_id='s2-gap'") == 0)
    client.send_message(tip_msgs[2])
    reply = client.send_message(seal_message("S2", "s2-gap"))
    demo.check("resend of the missing seq -> seal ACK", reply["ack"],
               json.dumps(reply))
    demo.check("all five tips stored exactly once",
               demo.count("SELECT COUNT(*) n FROM tips WHERE "
                          "batch_id='s2-gap'") == 5)

    orphan = {"type": "tip", "station_id": "S4", "batch_id": "no-begin",
              "seq": 0, "time_ms": T(day, "00:00"), "tips": 1}
    reply = client.send_message(orphan)
    demo.check("tip before begin -> NACK unknown_batch, no batch row",
               not reply["ack"] and reply["reason"] == "unknown_batch"
               and demo.count("SELECT COUNT(*) n FROM batches WHERE "
                              "batch_id='no-begin'") == 0,
               json.dumps({k: reply.get(k) for k in ("ack", "reason")}))

    begin, tip_msgs = demo.batch("S2", "s2-digest", T(day, "03:00"),
                                 T(day, "06:00"),
                                 [(0, T(day, "03:30"), 1), (1, T(day, "04:30"), 2)])
    client.send_message(dict(begin, sha256="f" * 64))
    for m in tip_msgs:
        client.send_message(m)
    reply = client.send_message(seal_message("S2", "s2-digest"))
    demo.check("digest mismatch -> NACK, batch stays unaccepted",
               not reply["ack"] and reply["reason"] == "digest_mismatch"
               and demo.count("SELECT COUNT(*) n FROM accepted_batches "
                              "WHERE batch_id='s2-digest'") == 0,
               json.dumps(reply.get("detail")))

    # protocol-level rejects keep the connection healthy
    for bad_msg, reason in (
            ({"type": "begin", "station_id": "S2", "batch_id": "x",
              "start_ms": T(day, "00:00") + 500, "end_ms": T(day, "03:00"),
              "count": 0, "tip_mm": "0.2", "sha256": "e" * 64},
             "invalid_message"),
            ({"type": "begin", "station_id": "S2", "batch_id": "x",
              "start_ms": T(day, "00:00"), "end_ms": T(day, "00:00"),
              "count": 0, "tip_mm": "0.2", "sha256": "e" * 64},
             "invalid_message"),
            ({"type": "begin", "station_id": "S2", "batch_id": "x",
              "start_ms": T(day, "00:00"), "end_ms": T(day, "03:00"),
              "count": 0, "tip_mm": "0.0", "sha256": "e" * 64},
             "invalid_message"),
            ({"type": "tip", "station_id": "S2", "batch_id": "s2-digest",
              "seq": 0, "time_ms": T(day, "09:00"), "tips": 1},
             "time_outside_interval"),
            ({"type": "tip", "station_id": "S2", "batch_id": "s2-digest",
              "seq": 5, "time_ms": T(day, "03:10"), "tips": 1},
             "seq_out_of_range"),
            # a tip for an already accepted batch with new content is refused
            ({"type": "tip", "station_id": "S2", "batch_id": "s2-gap",
              "seq": 0, "time_ms": T(day, "00:10"), "tips": 99},
             "batch_already_accepted"),
            ({"type": "begin", "station_id": "NOPE", "batch_id": "x",
              "start_ms": T(day, "00:00"), "end_ms": T(day, "03:00"),
              "count": 0, "tip_mm": "0.2", "sha256": "e" * 64},
             "unknown_station")):
        reply = client.send_message(bad_msg)
        demo.check(f"rejected: {reason}", not reply["ack"]
                   and reply["reason"] == reason,
                   json.dumps({k: reply.get(k) for k in ("ack", "reason")}))
    demo.check("connection still usable after NACKs",
               client.send_message(seal_message("S2", "s2-gap"))["ack"])
    client.close()


def scenario_concurrent_conflict(demo: Demo):
    """8: two connections, same seq, different content, concurrently."""
    demo.log("\n== scenario 8: two connections race the same seq ==")
    day = 4  # 2026-02-05
    a, b = demo.connect(), demo.connect()
    begin, tip_msgs = demo.batch("S2", "s2-race", T(day, "06:00"),
                                 T(day, "09:00"), [(0, T(day, "06:10"), 11)])
    a.send_message(begin)
    t0 = tip_msgs[0]
    results = {}
    barrier = threading.Barrier(2)

    def writer(name, client, message):
        barrier.wait()
        results[name] = client.send_message(message)

    threads = [threading.Thread(target=writer, args=("a", a, t0)),
               threading.Thread(target=writer, args=("b", b, dict(t0, tips=12)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    acked = [k for k, r in results.items() if r["ack"]]
    nacked = [k for k, r in results.items() if not r["ack"]]
    demo.check("exactly one concurrent writer wins",
               len(acked) == 1 and len(nacked) == 1,
               json.dumps({k: r["status"] for k, r in results.items()}))
    demo.check("loser gets tip_conflict",
               results[nacked[0]]["reason"] == "tip_conflict",
               json.dumps(results[nacked[0]].get("detail")))
    winner_tips = 11 if acked[0] == "a" else 12
    rows = demo.query("SELECT seq,tips FROM tips WHERE batch_id='s2-race'")
    demo.check("first committed content kept, exactly one row",
               len(rows) == 1 and rows[0]["tips"] == winner_tips,
               json.dumps(rows))
    winner = dict(t0, tips=winner_tips)
    reply = (a if acked[0] == "a" else b).send_message(winner)
    demo.check("resending the winning content is an ACK duplicate",
               reply["ack"] and reply.get("duplicate"),
               json.dumps({k: reply.get(k) for k in ("ack", "duplicate", "reason")}))
    a.close()
    b.close()


def scenario_concurrent_overlap(demo: Demo):
    """9: two overlapping batches seal concurrently; exactly one accepted."""
    demo.log("\n== scenario 9: concurrent seal of overlapping batches ==")
    day = 6  # 2026-02-07
    a, b = demo.connect(), demo.connect()
    b1, t1 = demo.batch("S4", "ovl-1", T(day, "00:00"), T(day, "04:00"),
                        [(0, T(day, "01:00"), 3), (1, T(day, "03:00"), 6)])
    b2, t2 = demo.batch("S4", "ovl-2", T(day, "02:00"), T(day, "06:00"),
                        [(0, T(day, "02:30"), 2), (1, T(day, "05:00"), 4)])
    for client, begin, tips in ((a, b1, t1), (b, b2, t2)):
        client.send_message(begin)
        for m in tips:
            client.send_message(m)
    replies = {}
    barrier = threading.Barrier(2)

    def sealer(name, client, batch_id):
        barrier.wait()
        replies[name] = client.send_message(seal_message("S4", batch_id))

    threads = [threading.Thread(target=sealer, args=("a", a, "ovl-1")),
               threading.Thread(target=sealer, args=("b", b, "ovl-2"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    accepted = [k for k, r in replies.items() if r["ack"]]
    rejected = [k for k, r in replies.items() if not r["ack"]]
    demo.check("exactly one overlapping batch accepted",
               len(accepted) == 1 and len(rejected) == 1,
               json.dumps({k: r["status"] for k, r in replies.items()}))
    demo.check("loser reports interval_conflict",
               replies[rejected[0]]["reason"] == "interval_conflict",
               json.dumps(replies[rejected[0]].get("detail")))
    demo.check("one accepted row for the overlapping pair",
               demo.count("SELECT COUNT(*) n FROM accepted_batches WHERE "
                          "batch_id LIKE 'ovl-%'") == 1)
    # neighbouring endpoints (end == start) are NOT an overlap
    day2 = 7
    client = demo.connect()
    demo.upload(client, "S3", "adj-a", T(day2, "00:00"), T(day2, "03:00"),
                [(0, T(day2, "00:30"), 1)])
    demo.upload(client, "S3", "adj-b", T(day2, "03:00"), T(day2, "06:00"),
                [(0, T(day2, "03:30"), 1)])
    demo.check("adjacent intervals sharing an endpoint both accepted",
               demo.count("SELECT COUNT(*) n FROM accepted_batches WHERE "
                          "batch_id LIKE 'adj-%'") == 2)
    client.close()
    a.close()
    b.close()


def scenario_crash_before_ack(demo: Demo):
    """10: commit survives a crash before the ACK; resend must not double."""
    demo.log("\n== scenario 10: commit -> crash before ACK -> restart -> resend ==")
    day = 5  # 2026-02-06
    tips = [(0, T(day, "06:10"), 7), (1, T(day, "07:10"), 7),
            (2, T(day, "08:10"), 7)]
    begin, tip_msgs = demo.batch("S1", "s1-crash", T(day, "06:00"),
                                 T(day, "09:00"), tips)
    demo.stop_server()

    ready = os.path.join(demo.workdir, "ready.port")
    if os.path.exists(ready):
        os.remove(ready)
    child = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "scripts", "telemetry_server.py"),
         "--db", demo.db, "--stations", demo.stations, "--port", "0",
         "--ready-file", ready, "--journal", demo.journal,
         "--drop-ack-after", "3"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 10
    while not os.path.exists(ready) and time.time() < deadline:
        time.sleep(0.02)
    with open(ready) as f:
        demo.port = int(f.read().strip())
    client = GatewayClient("127.0.0.1", demo.port, timeout=5)
    client.send_message(begin)
    client.send_message(tip_msgs[0])
    # tip 1 is committed, then the process exits before sending its ACK
    try:
        client.send_raw(encode_frame(tip_msgs[1]))
        client.read_reply()
    except (ConnectionError, OSError):
        pass
    out, err = child.communicate(timeout=15)
    demo.check("server exited before the ACK was sent",
               child.returncode == 77, f"exit={child.returncode}")
    demo.check("the committed tip is durably in SQLite after the crash",
               demo.count("SELECT COUNT(*) n FROM tips WHERE batch_id="
                          "'s1-crash'") == 2, "tips seq 0 and 1")
    demo.check("the un-ACKed tip counts as received but the batch is not accepted",
               demo.count("SELECT COUNT(*) n FROM accepted_batches WHERE "
                          "batch_id='s1-crash'") == 0)
    client.close()

    demo.start_server()  # fresh process, same database
    client = demo.connect()
    replies = send_batch(client, begin, tip_msgs)
    demo.check("gateway resend after restart is fully ACKed",
               all(r["ack"] for r in replies), f"{len(replies)} replies")
    demo.check("no tip duplicated by the resend",
               demo.count("SELECT COUNT(*) n FROM tips WHERE batch_id="
                          "'s1-crash'") == 3, "3 tips")
    demo.check("batch accepted exactly once",
               demo.count("SELECT COUNT(*) n FROM accepted_batches WHERE "
                          "batch_id='s1-crash'") == 1)
    client.close()


def _upload_export_coverage(demo: Demo):
    client = demo.connect()
    # S1: one 6 h batch, two pulses exactly at 03:00Z
    demo.upload(client, "S1", "s1-exp", T(0, "00:00"), T(0, "06:00"), [
        (0, T(0, "00:30"), 1), (1, T(0, "01:00"), 2), (2, T(0, "02:10"), 3),
        (3, T(0, "03:00"), 4), (4, T(0, "03:00"), 5), (5, T(0, "04:40"), 2),
        (6, T(0, "05:50"), 2)])
    # S2: two adjacent batches that jointly cover 00:00-06:00
    demo.upload(client, "S2", "s2-a", T(0, "00:00"), T(0, "03:00"),
                [(0, T(0, "00:15"), 2), (1, T(0, "01:45"), 3)])
    demo.upload(client, "S2", "s2-b", T(0, "03:00"), T(0, "06:00"),
                [(0, T(0, "03:30"), 4), (1, T(0, "05:30"), 3)])
    # S3: fully covered, genuinely no rain
    demo.upload(client, "S3", "s3-zero", T(0, "00:00"), T(0, "06:00"), [],
                count=0)
    # S4: first half accepted, second half only unaccepted (missing seq)
    demo.upload(client, "S4", "s4-a", T(0, "00:00"), T(0, "03:00"),
                [(0, T(0, "00:00"), 3)])
    begin, tips = demo.batch("S4", "s4-pending", T(0, "03:00"),
                             T(0, "06:00"), [(0, T(0, "03:40"), 9)], count=3)
    client.send_message(begin)
    client.send_message(tips[0])
    reply = client.send_message(seal_message("S4", "s4-pending"))
    assert not reply["ack"] and reply["reason"] == "incomplete", reply
    client.close()


def scenario_export(demo: Demo):
    """11-12: exported values, gaps, decimal arithmetic, no append."""
    demo.log("\n== scenario 11-12: export values, gaps, decimal, rewrite ==")
    _upload_export_coverage(demo)
    first = export_observations(
        demo.db, demo.stations, "2026-02-01T00:00:00Z",
        "2026-02-01T06:00:00Z", "2026-02-02T00:00:00Z", demo.export_path)
    rows = {(r["station_id"], r["valid_time"]): r for r in first["obs_rows"]}
    gaps = first["gap_rows"]

    expect = {
        ("S1", "2026-02-01T03:00:00Z"): "1.2",
        ("S1", "2026-02-01T06:00:00Z"): "2.6",
        ("S2", "2026-02-01T03:00:00Z"): "1",
        ("S2", "2026-02-01T06:00:00Z"): "1.4",
        ("S3", "2026-02-01T03:00:00Z"): "0",
        ("S3", "2026-02-01T06:00:00Z"): "0",
        ("S4", "2026-02-01T03:00:00Z"): "0.6",
    }
    demo.check("export writes exactly the expected observation rows",
               len(first["obs_rows"]) == len(expect)
               and set(rows) == set(expect),
               json.dumps(sorted(f"{k[0]} {k[1]}" for k in rows)))
    for key, want in expect.items():
        demo.check(f"{key[0]} {key[1][11:16]} = {want} mm",
                   rows[key]["precip_mm"] == want, rows[key]["precip_mm"])
    demo.check("pulse at exactly 03:00Z counted in 03-06, not 00-03",
               expect[("S1", "2026-02-01T03:00:00Z")] == "1.2"
               and expect[("S1", "2026-02-01T06:00:00Z")] == "2.6")
    demo.check("adjacent batches jointly prove completeness (S2)",
               ("S2", "2026-02-01T06:00:00Z") in rows)
    demo.check("full coverage with no pulses is a real 0, not missing",
               rows[("S3", "2026-02-01T03:00:00Z")]["precip_mm"] == "0"
               and rows[("S3", "2026-02-01T06:00:00Z")]["precip_mm"] == "0")
    demo.check("0.2 mm tips show no binary floating-point tail",
               all(len(r["precip_mm"].split(".")[-1]) <= 1
                   if "." in r["precip_mm"] else True
                   for r in first["obs_rows"]),
               json.dumps(sorted({r["precip_mm"] for r in first["obs_rows"]})))
    demo.check("version is 1 and issued_time is the requested one",
               all(r["version"] == 1
                   and r["issued_time"] == "2026-02-02T00:00:00Z"
                   for r in first["obs_rows"]))
    demo.check("S4 06Z is not written (only unaccepted data there)",
               ("S4", "2026-02-01T06:00:00Z") not in rows)
    s4_gaps = [g for g in gaps if g["station_id"] == "S4"
               and g["valid_time"] == "2026-02-01T06:00:00Z"]
    demo.check("S4 gap reported 03:00-06:00 as unaccepted_batch_only",
               len(s4_gaps) >= 1
               and s4_gaps[0]["missing_start"] == "2026-02-01T03:00:00Z"
               and s4_gaps[0]["missing_end"] == "2026-02-01T06:00:00Z"
               and s4_gaps[0]["reason"] == "unaccepted_batch_only"
               and s4_gaps[0]["pending_batch_ids"] == "s4-pending",
               json.dumps(s4_gaps))
    demo.check("no gap is invented for fully covered stations",
               {g["station_id"] for g in gaps} == {"S4"},
               json.dumps(sorted({g["station_id"] for g in gaps})))

    with open(demo.export_path, "rb") as f:
        bytes_first = f.read()
    second = export_observations(
        demo.db, demo.stations, "2026-02-01T00:00:00Z",
        "2026-02-01T06:00:00Z", "2026-02-02T00:00:00Z", demo.export_path)
    with open(demo.export_path, "rb") as f:
        bytes_second = f.read()
    demo.check("re-running export rebuilds the same file (no append)",
               bytes_first == bytes_second
               and second["obs_rows"] == first["obs_rows"])
    demo.check("gap sidecar exists next to the observation file",
               os.path.exists(first["gaps_path"]))
    return first


def scenario_one_second_gap(demo: Demo):
    """11b: a bucket missing exactly one second is not a zero-rain bucket."""
    demo.log("\n== scenario 11b: coverage missing exactly one second ==")
    tmp_base = tempfile.mkdtemp()
    try:
        tmp = tempfile.mkdtemp(dir=tmp_base)
        shutil.copy(os.path.join(ROOT, "data", "obs", "stations.csv"),
                    os.path.join(tmp, "all_stations.csv"))
        st_path = os.path.join(tmp, "stations.csv")
        with open(st_path, "w", encoding="utf-8") as f:
            f.write("station_id,lat,lon,region,weight\nS9,40,116,North,1.0\n")
        db = os.path.join(tmp, "t.sqlite3")
        store = TelemetryStore(db, st_path)
        plans = (("edge-a", T(0, "00:00"), T(0, "03:00")),
                 ("edge-b", T(0, "03:00") + 1000, T(0, "06:00")))
        for bid, t0, t1 in plans:
            begin, tip_msgs = build_batch("S9", bid, t0, t1, [(0, t0, 1)])
            assert store.handle(begin)["ack"]
            assert store.handle(tip_msgs[0])["ack"]
            assert store.handle(seal_message("S9", bid))["ack"]
        store.close()
        result = export_observations(db, st_path, "2026-02-01T00:00:00Z",
                                     "2026-02-01T06:00:00Z",
                                     "2026-02-02T00:00:00Z",
                                     os.path.join(tmp, "obs.csv"))
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
    s9 = [r for r in result["obs_rows"] if r["station_id"] == "S9"]
    demo.check("only the fully covered 00-03 bucket is exported",
               len(s9) == 1 and s9[0]["valid_time"] == "2026-02-01T03:00:00Z",
               json.dumps(s9))
    gap = [g for g in result["gap_rows"]
           if g["valid_time"] == "2026-02-01T06:00:00Z"]
    demo.check("one-second hole reported as 03:00:00-03:00:01",
               len(gap) == 1
               and gap[0]["missing_start"] == "2026-02-01T03:00:00Z"
               and gap[0]["missing_end"] == "2026-02-01T03:00:01Z"
               and gap[0]["reason"] == "partial_gap",
               json.dumps(gap))
    demo.check("a one-second hole is NOT exported as 0 mm",
               not any(r["valid_time"] == "2026-02-01T06:00:00Z" for r in s9))


def run(workdir: str | None = None):
    if workdir is None:
        workdir = os.path.join(ROOT, "runtime", "demo")
        if os.path.isdir(workdir):
            shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    demo = Demo(workdir)
    try:
        demo.start_server()
        demo.log(f"telemetry demo: db={demo.db} port={demo.port}")
        scenario_framing(demo)
        scenario_missing_seq(demo)
        scenario_concurrent_conflict(demo)
        scenario_concurrent_overlap(demo)
        scenario_crash_before_ack(demo)
        scenario_export(demo)
        scenario_one_second_gap(demo)
        demo.log(f"\nALL {len(demo.checks)} CHECKS PASSED")
    finally:
        demo.stop_server()
        demo.log_file.close()
    with open(os.path.join(workdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"ok": True, "checks": demo.checks, "db": demo.db,
                   "journal": demo.journal, "log": demo.log_path,
                   "export": demo.export_path,
                   "workdir": demo.workdir}, f, indent=2)
        f.write("\n")
    return demo


if __name__ == "__main__":
    run()
