"""SQLite-backed receive progress for gauge telemetry.

Two layers are kept strictly apart:

* **receive progress** -- ``batches`` + ``tips``: what has arrived and been
  committed.  A ``begin`` or ``tip`` is acknowledged only after its
  transaction commits, so an ACK always means "durable on disk".
* **analysis-usable data** -- ``accepted_batches``: a batch appears here
  only after a ``seal`` proved every sequence number present, the tip
  digest matched the declared digest, and the batch's coverage interval
  does not overlap another accepted batch of the same station.

Idempotency rules (enforced inside a ``BEGIN IMMEDIATE`` transaction so
that two connections racing on the same identity get deterministic
answers):

* identical ``begin`` / ``tip`` resend -> ACK, no second row;
* same identity with different content -> NACK ``identity_conflict`` /
  ``tip_conflict``, first content kept;
* ``tip`` before ``begin`` -> NACK ``unknown_batch`` (never create a
  batch);
* repeated ``seal`` of an accepted batch -> ACK (safe retransmit).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from .protocol import (ProtocolError, begin_content, canonical_sha256,
                       canonical_tip_mm, empty_digest, format_ranges,
                       missing_ranges, normalize_message, tip_content,
                       tip_digest)

SCHEMA_VERSION = 1
STATION_FIELDS = ("station_id", "lat", "lon", "region", "weight")


def _now_ms() -> int:
    return int(time.time() * 1000)


class TelemetryStore:
    def __init__(self, db_path: str, stations_path: str,
                 busy_timeout_ms: int = 10000):
        self.db_path = db_path
        self.stations_path = stations_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.stations = self._load_stations(stations_path)
        with self._lock:
            self._init_schema()

    # -- schema / registry ------------------------------------------------
    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                station_id  TEXT NOT NULL,
                batch_id    TEXT NOT NULL,
                start_ms    INTEGER NOT NULL,
                end_ms      INTEGER NOT NULL,
                count       INTEGER NOT NULL,
                tip_mm      TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'collecting',
                created_ms  INTEGER NOT NULL,
                sealed_ms   INTEGER,
                PRIMARY KEY (station_id, batch_id)
            );
            CREATE TABLE IF NOT EXISTS tips (
                station_id  TEXT NOT NULL,
                batch_id    TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                time_ms     INTEGER NOT NULL,
                tips        INTEGER NOT NULL,
                received_ms INTEGER NOT NULL,
                PRIMARY KEY (station_id, batch_id, seq),
                FOREIGN KEY (station_id, batch_id)
                    REFERENCES batches(station_id, batch_id)
            );
            CREATE INDEX IF NOT EXISTS tips_time
                ON tips (station_id, time_ms);
            CREATE TABLE IF NOT EXISTS accepted_batches (
                station_id  TEXT NOT NULL,
                batch_id    TEXT NOT NULL,
                start_ms    INTEGER NOT NULL,
                end_ms      INTEGER NOT NULL,
                count       INTEGER NOT NULL,
                tip_mm      TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                accepted_ms INTEGER NOT NULL,
                PRIMARY KEY (station_id, batch_id),
                FOREIGN KEY (station_id, batch_id)
                    REFERENCES batches(station_id, batch_id)
            );
            CREATE INDEX IF NOT EXISTS accepted_span
                ON accepted_batches (station_id, start_ms, end_ms);
            """
        )
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported telemetry schema version {row['value']}")

    @staticmethod
    def _load_stations(path: str):
        import csv
        stations = {}
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                stations[r["station_id"]] = r
        if not stations:
            raise ValueError(f"no stations registered in {path}")
        return stations

    def station_registered(self, station_id: str) -> bool:
        return station_id in self.stations

    # -- transactions -----------------------------------------------------
    def _tx(self):
        return _Transaction(self.conn)

    # -- public message handling -----------------------------------------
    def handle(self, message, source: str = "") -> dict:
        """Validate and apply one message; returns the reply object.

        The reply is computed after the database transaction commits.  A
        NACK is *not* an exception: the connection stays usable.
        """
        try:
            norm = normalize_message(message)
        except ProtocolError as exc:
            return self._nack(message, None, exc.reason, exc.detail)
        with self._lock:
            if norm["type"] == "begin":
                return self._handle_begin(norm)
            if norm["type"] == "tip":
                return self._handle_tip(norm)
            return self._handle_seal(norm)

    def _base_reply(self, norm, ack, reason=None, detail=None, **extra):
        reply = {
            "schema": "telemetry-reply/1",
            "ack": ack,
            "status": "ACK" if ack else "NACK",
            "message_type": norm["type"] if norm else None,
            "station_id": norm["station_id"] if norm else None,
            "batch_id": norm["batch_id"] if norm else None,
        }
        if reason is not None:
            reply["reason"] = reason
        if detail:
            reply["detail"] = detail
        reply.update(extra)
        return reply

    def _nack(self, message, norm, reason, detail=None, **extra):
        if norm is None and isinstance(message, dict):
            station_id = message.get("station_id")
            batch_id = message.get("batch_id")
            mtype = message.get("type")
            partial = {
                "type": mtype if mtype in ("begin", "tip", "seal") else None,
                "station_id": station_id if isinstance(station_id, str) else None,
                "batch_id": batch_id if isinstance(batch_id, str) else None,
            }
            return self._base_reply(partial, False, reason, detail, **extra)
        return self._base_reply(norm, False, reason, detail, **extra)

    # -- begin ------------------------------------------------------------
    def _handle_begin(self, norm):
        sid, bid = norm["station_id"], norm["batch_id"]
        if not self.station_registered(sid):
            return self._nack(None, norm, "unknown_station",
                              {"station_id": sid})
        content = begin_content(norm)
        with self._tx() as tx:
            row = tx.execute(
                "SELECT start_ms,end_ms,count,tip_mm,sha256,status "
                "FROM batches WHERE station_id=? AND batch_id=?",
                (sid, bid)).fetchone()
            if row is not None:
                stored = {
                    "start_ms": row["start_ms"], "end_ms": row["end_ms"],
                    "count": row["count"], "tip_mm": row["tip_mm"],
                    "sha256": row["sha256"],
                }
                if stored == content:
                    return self._base_reply(
                        norm, True, duplicate=True,
                        batch_status=row["status"])
                return self._base_reply(
                    None, False, "identity_conflict",
                    {"message_type": "begin", "stored": stored,
                     "received": content}, duplicate=True,
                    batch_status=row["status"])
            tx.execute(
                "INSERT INTO batches(station_id,batch_id,start_ms,end_ms,"
                "count,tip_mm,sha256,status,created_ms) "
                "VALUES(?,?,?,?,?,?,?,'collecting',?)",
                (sid, bid, content["start_ms"], content["end_ms"],
                 content["count"], content["tip_mm"], content["sha256"],
                 _now_ms()))
        return self._base_reply(norm, True, duplicate=False,
                                batch_status="collecting")

    # -- tip --------------------------------------------------------------
    def _handle_tip(self, norm):
        reply = self._handle_tip_inner(norm)
        reply.setdefault("seq", norm["seq"])
        return reply

    def _handle_tip_inner(self, norm):
        sid, bid = norm["station_id"], norm["batch_id"]
        if not self.station_registered(sid):
            return self._nack(None, norm, "unknown_station",
                              {"station_id": sid})
        content = tip_content(norm)
        with self._tx() as tx:
            batch = tx.execute(
                "SELECT start_ms,end_ms,count,status FROM batches "
                "WHERE station_id=? AND batch_id=?",
                (sid, bid)).fetchone()
            if batch is None:
                return self._nack(None, norm, "unknown_batch",
                                  {"hint": "send begin before tips"})
            if batch["status"] == "accepted":
                existing = tx.execute(
                    "SELECT time_ms,tips FROM tips WHERE station_id=? AND "
                    "batch_id=? AND seq=?", (sid, bid, content["seq"])).fetchone()
                if existing is not None and (
                        existing["time_ms"] == content["time_ms"]
                        and existing["tips"] == content["tips"]):
                    return self._base_reply(
                        norm, True, duplicate=True, batch_status="accepted")
                return self._base_reply(
                    None, False, "batch_already_accepted",
                    {"seq": content["seq"]}, batch_status="accepted")
            if content["seq"] >= batch["count"]:
                return self._nack(
                    None, norm, "seq_out_of_range",
                    {"seq": content["seq"], "count": batch["count"]})
            if not (batch["start_ms"] <= content["time_ms"] < batch["end_ms"]):
                return self._nack(
                    None, norm, "time_outside_interval",
                    {"time_ms": content["time_ms"],
                     "start_ms": batch["start_ms"],
                     "end_ms": batch["end_ms"]})
            existing = tx.execute(
                "SELECT time_ms,tips FROM tips WHERE station_id=? AND "
                "batch_id=? AND seq=?", (sid, bid, content["seq"])).fetchone()
            if existing is not None:
                if (existing["time_ms"] == content["time_ms"]
                        and existing["tips"] == content["tips"]):
                    return self._base_reply(
                        norm, True, duplicate=True,
                        batch_status=batch["status"])
                return self._base_reply(
                    None, False, "tip_conflict",
                    {"seq": content["seq"],
                     "stored": {"time_ms": existing["time_ms"],
                                "tips": existing["tips"]},
                     "received": {"time_ms": content["time_ms"],
                                  "tips": content["tips"]}},
                    duplicate=True, batch_status=batch["status"])
            tx.execute(
                "INSERT INTO tips(station_id,batch_id,seq,time_ms,tips,"
                "received_ms) VALUES(?,?,?,?,?,?)",
                (sid, bid, content["seq"], content["time_ms"],
                 content["tips"], _now_ms()))
        return self._base_reply(norm, True, duplicate=False,
                                batch_status="collecting")

    # -- seal -------------------------------------------------------------
    def _handle_seal(self, norm):
        sid, bid = norm["station_id"], norm["batch_id"]
        if not self.station_registered(sid):
            return self._nack(None, norm, "unknown_station",
                              {"station_id": sid})
        with self._tx() as tx:
            accepted = tx.execute(
                "SELECT start_ms,end_ms,tip_mm,count,sha256,accepted_ms "
                "FROM accepted_batches WHERE station_id=? AND batch_id=?",
                (sid, bid)).fetchone()
            if accepted is not None:
                return self._base_reply(
                    norm, True, duplicate=True, batch_status="accepted",
                    accepted={"start_ms": accepted["start_ms"],
                              "end_ms": accepted["end_ms"],
                              "count": accepted["count"],
                              "tip_mm": accepted["tip_mm"],
                              "sha256": accepted["sha256"]})
            batch = tx.execute(
                "SELECT start_ms,end_ms,count,tip_mm,sha256,status "
                "FROM batches WHERE station_id=? AND batch_id=?",
                (sid, bid)).fetchone()
            if batch is None:
                return self._nack(None, norm, "unknown_batch",
                                  {"hint": "send begin before seal"})
            rows = tx.execute(
                "SELECT seq,time_ms,tips FROM tips WHERE station_id=? AND "
                "batch_id=? ORDER BY seq", (sid, bid)).fetchall()
            present = [r["seq"] for r in rows]
            missing = missing_ranges(present, batch["count"])
            if missing:
                return self._base_reply(
                    None, False, "incomplete",
                    {"missing_seq_count": sum(hi - lo + 1 for lo, hi in missing),
                     "missing_ranges": format_ranges(missing),
                     "received_count": len(present),
                     "count": batch["count"]},
                    batch_status="collecting")
            digest = tip_digest((r["seq"], r["time_ms"], r["tips"]) for r in rows)
            if digest != batch["sha256"]:
                return self._base_reply(
                    None, False, "digest_mismatch",
                    {"declared": batch["sha256"], "computed": digest},
                    batch_status="collecting")
            overlap = tx.execute(
                "SELECT batch_id,start_ms,end_ms FROM accepted_batches "
                "WHERE station_id=? AND start_ms < ? AND end_ms > ?",
                (sid, batch["end_ms"], batch["start_ms"])).fetchall()
            if overlap:
                return self._base_reply(
                    None, False, "interval_conflict",
                    {"overlapping_batches": [
                        {"batch_id": r["batch_id"],
                         "start_ms": r["start_ms"], "end_ms": r["end_ms"]}
                        for r in overlap]},
                    batch_status="collecting")
            tx.execute(
                "INSERT INTO accepted_batches(station_id,batch_id,start_ms,"
                "end_ms,count,tip_mm,sha256,accepted_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sid, bid, batch["start_ms"], batch["end_ms"],
                 batch["count"], batch["tip_mm"], batch["sha256"], _now_ms()))
            tx.execute(
                "UPDATE batches SET status='accepted', sealed_ms=? "
                "WHERE station_id=? AND batch_id=?",
                (_now_ms(), sid, bid))
        return self._base_reply(
            norm, True, duplicate=False, batch_status="accepted",
            accepted={"start_ms": batch["start_ms"], "end_ms": batch["end_ms"],
                      "count": batch["count"], "tip_mm": batch["tip_mm"],
                      "sha256": batch["sha256"]})

    # -- inspection helpers (export / demo assertions) -------------------
    def accepted_batches(self, station_id=None):
        sql = ("SELECT station_id,batch_id,start_ms,end_ms,count,tip_mm,"
               "sha256,accepted_ms FROM accepted_batches")
        args = ()
        if station_id is not None:
            sql += " WHERE station_id=?"
            args = (station_id,)
        sql += " ORDER BY station_id, start_ms, batch_id"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def pending_batches(self, station_id=None):
        sql = ("SELECT station_id,batch_id,start_ms,end_ms,count,status "
               "FROM batches WHERE status<>'accepted'")
        args = ()
        if station_id is not None:
            sql += " AND station_id=?"
            args = (station_id,)
        sql += " ORDER BY station_id, start_ms, batch_id"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def tips_for_batch(self, station_id, batch_id):
        with self._lock:
            rows = self.conn.execute(
                "SELECT seq,time_ms,tips FROM tips WHERE station_id=? AND "
                "batch_id=? ORDER BY seq", (station_id, batch_id)).fetchall()
        return [dict(r) for r in rows]

    def batch_status(self, station_id, batch_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT status FROM batches WHERE station_id=? AND batch_id=?",
                (station_id, batch_id)).fetchone()
        return None if row is None else row["status"]

    def count_rows(self, table):
        """Row count of one of the telemetry tables (diagnostics only)."""
        if table not in ("batches", "tips", "accepted_batches", "meta"):
            raise ValueError(f"unknown table {table}")
        with self._lock:
            return self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def close(self):
        with self._lock:
            self.conn.close()


class _Transaction:
    """``BEGIN IMMEDIATE`` context manager.

    Commits before the caller can send an ACK; rolls back on any error
    (including process-level exceptions raised inside the block).
    """

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        return False
