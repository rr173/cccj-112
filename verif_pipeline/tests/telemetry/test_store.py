"""SQLite receive-progress semantics and acceptance rules."""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.gateway import build_batch, seal_message  # noqa: E402
from telemetry.protocol import tip_digest  # noqa: E402
from telemetry.store import TelemetryStore  # noqa: E402

STATIONS = os.path.join(ROOT, "data", "obs", "stations.csv")
H = 3600 * 1000


@pytest.fixture()
def store(tmp_path):
    s = TelemetryStore(str(tmp_path / "t.sqlite3"), STATIONS)
    yield s
    s.close()


def _replies(store, begin, tips):
    out = [store.handle(begin)]
    out += [store.handle(t) for t in tips]
    return out


def test_begin_idempotent_and_conflict(store):
    begin, tips = build_batch("S1", "b1", 0, 6 * H, [(0, 1000, 1)])
    r1 = store.handle(begin)
    r2 = store.handle(begin)
    assert r1["ack"] and not r1["duplicate"]
    assert r2["ack"] and r2["duplicate"]
    assert store.count_rows("batches") == 1
    r3 = store.handle(dict(begin, count=2))
    assert not r3["ack"] and r3["reason"] == "identity_conflict"
    assert r3["detail"]["stored"]["count"] == 1
    assert store.count_rows("batches") == 1


def test_tip_requires_begin(store):
    begin, tips = build_batch("S1", "missing", 0, H, [(0, 1000, 1)])
    r = store.handle(tips[0])
    assert not r["ack"] and r["reason"] == "unknown_batch"
    assert store.count_rows("batches") == 0
    assert store.count_rows("tips") == 0


def test_tip_idempotent_by_content(store):
    begin, tips = build_batch("S1", "b1", 0, H,
                              [(0, 1000, 1), (1, 2000, 2)])
    store.handle(begin)
    assert store.handle(tips[0])["ack"]
    assert store.handle(tips[0])["duplicate"]
    r = store.handle(dict(tips[0], tips=9))
    assert not r["ack"] and r["reason"] == "tip_conflict"
    assert store.tips_for_batch("S1", "b1") == [{"seq": 0, "time_ms": 1000, "tips": 1}]


def test_tip_out_of_range_and_outside_interval(store):
    begin, tips = build_batch("S1", "b1", 0, H, [(0, 1000, 1)])
    store.handle(begin)
    r = store.handle({**tips[0], "seq": 1})
    assert not r["ack"] and r["reason"] == "seq_out_of_range"
    r = store.handle({**tips[0], "time_ms": H})
    assert not r["ack"] and r["reason"] == "time_outside_interval"
    r = store.handle({**tips[0], "time_ms": -1})
    assert not r["ack"] and r["reason"] == "time_outside_interval"


def test_begin_interval_must_be_whole_seconds(store):
    begin, _ = build_batch("S1", "b", 0, H, [])
    assert store.handle({**begin, "start_ms": 1})["reason"] == "invalid_message"
    assert store.handle({**begin, "end_ms": 1})["reason"] == "invalid_message"
    assert store.handle({**begin, "start_ms": H})["reason"] == "invalid_message"


def test_unknown_station_rejected_everywhere(store):
    begin, tips = build_batch("NOPE", "b1", 0, H, [(0, 1000, 1)])
    assert store.handle(begin)["reason"] == "unknown_station"
    assert store.handle(tips[0])["reason"] == "unknown_station"
    assert store.handle(seal_message("NOPE", "b1"))["reason"] == "unknown_station"


def test_seal_reports_exact_missing_ranges(store):
    begin, tips = build_batch("S1", "b1", 0, 6 * H,
                              [(i, 1000 * (i + 1), 1) for i in range(8)])
    store.handle(begin)
    for t in tips:
        if t["seq"] in (2, 5, 6):
            continue
        store.handle(t)
    r = store.handle(seal_message("S1", "b1"))
    assert not r["ack"] and r["reason"] == "incomplete"
    assert r["detail"]["missing_ranges"] == ["2", "5-6"]
    assert r["detail"]["missing_seq_count"] == 3
    assert store.batch_status("S1", "b1") == "collecting"
    for t in tips:
        if t["seq"] in (2, 5, 6):
            store.handle(t)
    assert store.handle(seal_message("S1", "b1"))["ack"]
    assert store.batch_status("S1", "b1") == "accepted"


def test_seal_digest_mismatch(store):
    begin, tips = build_batch("S1", "b1", 0, H, [(0, 1000, 1)])
    store.handle(dict(begin, sha256="0" * 64))
    store.handle(tips[0])
    r = store.handle(seal_message("S1", "b1"))
    assert not r["ack"] and r["reason"] == "digest_mismatch"
    accepted = store.accepted_batches("S1")
    assert accepted == []


def test_empty_batch_is_acccepted_and_marked_zero_rain(store):
    begin, _ = build_batch("S1", "dry", 0, 3 * H, [])
    r = store.handle(begin)
    assert r["ack"] and begin["sha256"] == tip_digest([])
    r = store.handle(seal_message("S1", "dry"))
    assert r["ack"] and r["batch_status"] == "accepted"
    assert store.accepted_batches("S1")[0]["count"] == 0


def test_overlap_conflicts_but_adjacency_is_fine(store):
    for bid, t0, t1 in (("a", 0, 3 * H), ("b", 3 * H, 6 * H),
                        ("c", 2 * H, 4 * H)):
        begin, tips = build_batch("S2", bid, t0, t1,
                                  [(0, t0 + H, 1)] if t0 < 3 * H else [])
        store.handle(begin)
        for t in tips:
            store.handle(t)
        r = store.handle(seal_message("S2", bid))
        if bid == "c":
            assert not r["ack"] and r["reason"] == "interval_conflict"
            assert r["detail"]["overlapping_batches"][0]["batch_id"] in ("a", "b")
        else:
            assert r["ack"], r
    assert [b["batch_id"] for b in store.accepted_batches("S2")] == ["a", "b"]


def test_repeated_seal_of_accepted_batch_is_safe(store):
    begin, tips = build_batch("S1", "b1", 0, H, [(0, 1000, 1)])
    store.handle(begin)
    store.handle(tips[0])
    assert store.handle(seal_message("S1", "b1"))["ack"]
    r = store.handle(seal_message("S1", "b1"))
    assert r["ack"] and r["duplicate"]
    assert store.count_rows("accepted_batches") == 1


def test_new_content_for_accepted_batch_is_refused(store):
    begin, tips = build_batch("S1", "b1", 0, H, [(0, 1000, 1)])
    store.handle(begin)
    store.handle(tips[0])
    store.handle(seal_message("S1", "b1"))
    r = store.handle({**tips[0], "tips": 2})
    assert not r["ack"] and r["reason"] == "batch_already_accepted"
    # identical content is still idempotent
    assert store.handle(tips[0])["ack"]


def test_receive_progress_survives_reopen(tmp_path):
    db = str(tmp_path / "t.sqlite3")
    s = TelemetryStore(db, STATIONS)
    begin, tips = build_batch("S1", "b1", 0, H,
                              [(0, 1000, 1), (1, 2000, 2)])
    s.handle(begin)
    s.handle(tips[0])
    s.close()

    s2 = TelemetryStore(db, STATIONS)
    assert s2.tips_for_batch("S1", "b1")[0]["seq"] == 0
    assert s2.batch_status("S1", "b1") == "collecting"
    assert s2.handle(tips[0])["duplicate"]
    s2.handle(tips[1])
    assert s2.handle(seal_message("S1", "b1"))["ack"]
    s2.close()


def test_two_connections_same_seq_different_content(tmp_path):
    db = str(tmp_path / "t.sqlite3")
    stations = STATIONS
    a = TelemetryStore(db, stations)
    b = TelemetryStore(db, stations)
    begin, tips = build_batch("S1", "race", 0, H, [(0, 1000, 5)])
    a.handle(begin)
    barrier = threading.Barrier(2)
    results = {}

    def write(name, st, msg):
        barrier.wait()
        results[name] = st.handle(msg)

    threads = [threading.Thread(target=write, args=("a", a, tips[0])),
               threading.Thread(target=write, args=("b", b, {**tips[0], "tips": 6}))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results.values() if r["ack"]]) == 1
    assert sorted(r["reason"] for r in results.values() if not r["ack"]) == \
        ["tip_conflict"]
    assert a.tips_for_batch("S1", "race") == [
        {"seq": 0, "time_ms": 1000, "tips": 5 if results["a"]["ack"] else 6}]
    a.close()
    b.close()


def test_concurrent_seal_overlap_only_one_wins(tmp_path):
    db = str(tmp_path / "t.sqlite3")
    stations = STATIONS
    a = TelemetryStore(db, stations)
    b = TelemetryStore(db, stations)
    for st, bid, t0, t1 in ((a, "one", 0, 4 * H), (b, "two", 2 * H, 6 * H)):
        begin, tips = build_batch("S3", bid, t0, t1, [(0, t0 + H, 1)])
        st.handle(begin)
        for t in tips:
            st.handle(t)
    barrier = threading.Barrier(2)
    results = {}

    def seal(name, st, bid):
        barrier.wait()
        results[name] = st.handle(seal_message("S3", bid))

    threads = [threading.Thread(target=seal, args=("a", a, "one")),
               threading.Thread(target=seal, args=("b", b, "two"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results.values() if r["ack"]]) == 1
    loser = [r for r in results.values() if not r["ack"]][0]
    assert loser["reason"] == "interval_conflict"
    assert len(a.accepted_batches("S3")) == 1
    a.close()
