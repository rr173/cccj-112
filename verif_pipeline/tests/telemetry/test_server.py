"""TCP-level server behaviour over real loopback sockets."""
import os
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.frame import encode_frame  # noqa: E402
from telemetry.gateway import GatewayClient, build_batch, seal_message  # noqa: E402
from telemetry.server import serve  # noqa: E402

STATIONS = os.path.join(ROOT, "data", "obs", "stations.csv")
H = 3600 * 1000


@pytest.fixture()
def server(tmp_path):
    db = str(tmp_path / "t.sqlite3")
    journal = str(tmp_path / "journal.jsonl")
    srv, thread, port = serve(db, STATIONS, port=0, journal_path=journal)
    yield port, db, journal
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()
    srv.state.store.close()
    srv.state.close()


def _client(port, timeout=5.0):
    return GatewayClient("127.0.0.1", port, timeout=timeout)


def test_replies_are_one_json_object_per_line(server):
    port, _, _ = server
    c = _client(port)
    begin, _ = build_batch("S1", "b", 0, H, [])
    raw = encode_frame(begin)
    c.send_raw(raw * 3)  # three identical frames in ONE write
    replies = [c.read_reply() for _ in range(3)]
    assert all(r["schema"] == "telemetry-reply/1" for r in replies)
    assert all(r["ack"] and r["message_type"] == "begin" for r in replies)
    assert [r["duplicate"] for r in replies] == [False, True, True]
    c.close()


def test_split_and_glued_frames_over_socket(server):
    port, _, _ = server
    tips = [(i, 1000 * (i + 1), i + 1) for i in range(10)]
    begin, tip_msgs = build_batch("S1", "b", 0, H, tips)
    c = _client(port)
    raw = encode_frame(begin)
    for i in range(0, len(raw), 3):  # awkward chunks
        c.send_raw(raw[i:i + 3])
        time.sleep(0.002)
    assert c.read_reply()["ack"]
    blob = b"".join(encode_frame(m) for m in tip_msgs[::-1])
    c.send_raw(blob)  # ten frames glued, reverse order
    replies = [c.read_reply() for _ in range(10)]
    assert all(r["ack"] for r in replies)
    assert c.send_message(seal_message("S1", "b"))["ack"]
    c.close()


def test_bad_frames_do_not_break_the_stream(server):
    port, db, journal = server
    c = _client(port)
    good = encode_frame({"type": "seal", "station_id": "S1", "batch_id": "nope"})
    bad_crc = bytearray(good)
    bad_crc[-1] ^= 0xFF
    frames = (b"\x00garbage\xa5" + bytes(bad_crc)
              + b"\xa5\x5a\x30\x39"  # invalid length 12345
              + good)
    c.send_raw(frames)
    reply = c.read_reply()
    assert reply["ack"] is False and reply["reason"] == "unknown_batch"
    c.close()
    import json
    with open(journal, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    reasons = {e["payload"]["reason"] for e in lines
               if e["direction"] == "reject"}
    assert {"bad_crc", "invalid_length", "garbage"} <= reasons


def test_half_frame_waits_for_the_rest(server):
    port, db, _ = server
    c = _client(port)
    begin, _ = build_batch("S1", "b", 0, H, [])
    raw = encode_frame(begin)
    c.send_raw(raw[:-1])
    assert c.wait_reply(timeout=0.3) is None
    c.send_raw(raw[-1:])
    assert c.read_reply()["ack"]
    c.close()


def test_several_connections_at_once(server):
    port, db, _ = server
    errors = []

    def worker(n):
        try:
            c = _client(port)
            station = "S1"
            batch_id = f"b{n}"
            begin, tips = build_batch(station, batch_id, n * H, (n + 1) * H,
                                      [(0, n * H + 1000, 1)])
            assert c.send_message(begin)["ack"]
            assert c.send_message(tips[0])["ack"]
            assert c.send_message(seal_message(station, batch_id))["ack"]
            c.close()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM accepted_batches").fetchone()[0] == 8
    conn.close()


def test_connection_closed_mid_batch_keeps_received_rows(server):
    port, db, _ = server
    c = _client(port)
    begin, tips = build_batch("S2", "b", 0, 2 * H,
                              [(0, 1000, 1), (1, H, 2)])
    c.send_message(begin)
    c.send_message(tips[0])
    c.close()
    time.sleep(0.05)
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM tips").fetchone()[0] == 1
    conn.close()
    # and the gateway can resume on a new connection
    c2 = _client(port)
    assert c2.send_message(tips[0])["duplicate"]
    assert c2.send_message(tips[1])["ack"]
    assert c2.send_message(seal_message("S2", "b"))["ack"]
    c2.close()


def test_only_registered_stations_are_received(server):
    port, db, _ = server
    c = _client(port)
    begin, _ = build_batch("XX", "b", 0, H, [])
    reply = c.send_message(begin)
    assert not reply["ack"] and reply["reason"] == "unknown_station"
    c.close()
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
    conn.close()
