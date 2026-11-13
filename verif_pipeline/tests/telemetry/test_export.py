"""Export semantics: 3-hour buckets, decimal math, coverage vs arrival."""
import csv
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.export import export_observations  # noqa: E402
from telemetry.gateway import build_batch, seal_message  # noqa: E402
from telemetry.store import TelemetryStore  # noqa: E402

H = 3600 * 1000
DAY = "2026-03-01T00:00:00Z"
D0 = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)


def T(hh, mm=0, sec=0):
    return D0 + (hh * 60 + mm) * 60000 + sec * 1000


STATIONS_CSV = "station_id,lat,lon,region,weight\nA,40,116,R,1.0\nB,41,117,R,1.0\n"


@pytest.fixture()
def env(tmp_path):
    stations = tmp_path / "stations.csv"
    stations.write_text(STATIONS_CSV, encoding="utf-8")
    db = str(tmp_path / "t.sqlite3")
    store = TelemetryStore(db, str(stations))
    yield tmp_path, store, str(stations), db
    store.close()


def upload(store, station, batch_id, t0, t1, tips, tip_mm="0.2", count=None,
           seal=True):
    begin, tip_msgs = build_batch(station, batch_id, t0, t1, tips,
                                  tip_mm=tip_mm, count=count)
    assert store.handle(begin)["ack"]
    for m in tip_msgs:
        assert store.handle(m)["ack"]
    if seal:
        return store.handle(seal_message(station, batch_id))
    return None


def run_export(env, start=DAY, end="2026-03-01T06:00:00Z"):
    tmp, store, stations, db = env
    out = tmp / "obs.csv"
    return export_observations(db, stations, start, end,
                               "2026-03-02T00:00:00Z", str(out))


def test_bucket_boundary_and_valid_time(env):
    # pulse at exactly 03:00 belongs to 03-06 (valid time 06Z)
    upload(env[1], "A", "a", T(0), T(6), [(0, T(0, 30), 1), (1, T(3, 0), 2)])
    result = run_export(env)
    rows = {(r["station_id"], r["valid_time"]): r for r in result["obs_rows"]}
    assert rows[("A", "2026-03-01T03:00:00Z")]["precip_mm"] == "0.2"
    assert rows[("A", "2026-03-01T06:00:00Z")]["precip_mm"] == "0.4"


def test_one_batch_can_span_several_buckets(env):
    upload(env[1], "A", "a", T(0), T(9),
           [(0, T(1), 1), (1, T(4), 2), (2, T(7), 3)])
    result = run_export(env, end="2026-03-01T09:00:00Z")
    rows = {(r["station_id"], r["valid_time"]): r["precip_mm"]
            for r in result["obs_rows"]}
    assert rows[("A", "2026-03-01T03:00:00Z")] == "0.2"
    assert rows[("A", "2026-03-01T06:00:00Z")] == "0.4"
    assert rows[("A", "2026-03-01T09:00:00Z")] == "0.6"


def test_decimal_tip_mm_without_binary_tail(env):
    upload(env[1], "A", "a", T(0), T(3),
           [(i, T(0, 0, i), 1) for i in range(3)], tip_mm="0.1")
    result = run_export(env, end="2026-03-01T03:00:00Z")
    assert result["obs_rows"][0]["precip_mm"] == "0.3"
    upload(env[1], "B", "b", T(3), T(6),
           [(i, T(3, 0, i), 1) for i in range(7)], tip_mm="0.07")
    result = run_export(env)
    b = [r for r in result["obs_rows"] if r["station_id"] == "B"][0]
    assert b["precip_mm"] == "0.49"


def test_full_coverage_zero_rain_is_zero(env):
    upload(env[1], "A", "dry", T(0), T(3), [], count=0)
    result = run_export(env, end="2026-03-01T03:00:00Z")
    assert result["obs_rows"] == [{
        "station_id": "A", "valid_time": "2026-03-01T03:00:00Z",
        "precip_mm": "0", "version": 1,
        "issued_time": "2026-03-02T00:00:00Z"}]
    assert {g["station_id"] for g in result["gap_rows"]} == {"B"}


def test_no_coverage_means_no_obs_and_gap(env):
    result = run_export(env, end="2026-03-01T03:00:00Z")
    assert result["obs_rows"] == []
    assert len(result["gap_rows"]) == 2  # station A and B
    gap = [g for g in result["gap_rows"] if g["station_id"] == "A"][0]
    assert gap["missing_start"] == "2026-03-01T00:00:00Z"
    assert gap["missing_end"] == "2026-03-01T03:00:00Z"
    assert gap["reason"] == "no_accepted_coverage"


def test_unaccepted_batches_are_not_rain(env):
    store = env[1]
    upload(store, "A", "full", T(0), T(3), [(0, T(1), 5)])
    # collecting batch, seal refused (missing seq), covers 03-06
    upload(store, "A", "pending", T(3), T(6),
           [(0, T(4), 5)], count=2, seal=False)
    result = run_export(env)
    assert [r["valid_time"] for r in result["obs_rows"]] == \
        ["2026-03-01T03:00:00Z"]
    gap = result["gap_rows"][0]
    assert gap["missing_start"] == "2026-03-01T03:00:00Z"
    assert gap["missing_end"] == "2026-03-01T06:00:00Z"
    assert gap["reason"] == "unaccepted_batch_only"
    assert gap["pending_batch_ids"] == "pending"


def test_adjacent_batches_prove_contiguous_coverage(env):
    upload(env[1], "A", "a", T(0), T(3), [(0, T(1), 1)])
    upload(env[1], "A", "b", T(3), T(6), [(0, T(5), 2)])
    result = run_export(env)
    rows = [r for r in result["obs_rows"] if r["station_id"] == "A"]
    assert [r["valid_time"] for r in rows] == \
        ["2026-03-01T03:00:00Z", "2026-03-01T06:00:00Z"]
    assert {g["station_id"] for g in result["gap_rows"]} == {"B"}


def test_one_millisecond_hole_suppresses_the_bucket(env):
    upload(env[1], "A", "a", T(0), T(3), [(0, T(1), 1)])
    upload(env[1], "A", "b", T(3) + 1000, T(6), [(0, T(4), 1)])
    result = run_export(env)
    assert [r["valid_time"] for r in result["obs_rows"]] == \
        ["2026-03-01T03:00:00Z"]
    gap = result["gap_rows"][0]
    assert gap["missing_start"] == "2026-03-01T03:00:00Z"
    assert gap["missing_end"] == "2026-03-01T03:00:01Z"
    assert gap["reason"] == "partial_gap"


def test_window_boundaries_must_be_three_hour_utc(env):
    with pytest.raises(ValueError):
        run_export(env, start="2026-03-01T01:00:00Z")
    with pytest.raises(ValueError):
        run_export(env, end="2026-03-01T05:00:00Z")
    with pytest.raises(ValueError):
        run_export(env, end=DAY)


def test_export_rewrites_and_never_appends(env):
    upload(env[1], "A", "a", T(0), T(3), [(0, T(1), 1)])
    result1 = run_export(env, end="2026-03-01T03:00:00Z")
    out = env[0] / "obs.csv"
    first = out.read_bytes()
    result2 = run_export(env, end="2026-03-01T03:00:00Z")
    assert out.read_bytes() == first
    assert result2["obs_rows"] == result1["obs_rows"]
    with open(out, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == ["station_id", "valid_time", "precip_mm", "version",
                      "issued_time"]


def test_reversal_of_late_tips_updates_export(env):
    # a batch is accepted only after tips arrive; export reflects it
    store = env[1]
    upload(store, "A", "a", T(0), T(3), [(0, T(1), 1)], seal=False)
    assert run_export(env, end="2026-03-01T03:00:00Z")["obs_rows"] == []
    assert store.handle(seal_message("A", "a"))["ack"]
    assert run_export(env, end="2026-03-01T03:00:00Z")["obs_rows"][0][
        "precip_mm"] == "0.2"


def test_export_is_a_read_only_view_of_sqlite(tmp_path):
    # exporting must not mutate receive progress
    stations = tmp_path / "stations.csv"
    stations.write_text(STATIONS_CSV, encoding="utf-8")
    db = str(tmp_path / "t.sqlite3")
    s = TelemetryStore(db, str(stations))
    upload(s, "A", "a", T(0), T(3), [(0, T(1), 4)])
    before = (s.count_rows("batches"), s.count_rows("tips"),
              s.count_rows("accepted_batches"))
    s.close()
    export_observations(db, str(stations), DAY, "2026-03-01T03:00:00Z",
                        "2026-03-02T00:00:00Z", str(tmp_path / "obs.csv"))
    s2 = TelemetryStore(db, str(stations))
    assert (s2.count_rows("batches"), s2.count_rows("tips"),
            s2.count_rows("accepted_batches")) == before
    s2.close()
