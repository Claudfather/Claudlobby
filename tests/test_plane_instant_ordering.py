"""Offset representation must not choose current registry state or utilization.

Real emit fixtures live wholly below tmp_path; historical malformed rows are
introduced only into that private DB. No daemon, live socket or service runs.
"""
from datetime import datetime, timezone
import sqlite3
import time

import pytest

from claudlobby.plane import db, registry_read as rr
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.queries import REG_CURRENT_KEYS_SQL
from claudlobby.plane.utilization import heartbeat_series, bot_utilization
from tests.test_plane_registry_read import BOT, _bot, _conn, _done, _root, _snap, _tomb


@pytest.fixture(autouse=True)
def pinned_timezone(monkeypatch):
    # Restore tzset() as well as TZ: process-local timezone state outlives env.
    try:
        with monkeypatch.context() as m:
            m.setenv("TZ", "UTC")
            time.tzset()
            yield
    finally:
        time.tzset()


def _stored(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()


@pytest.mark.parametrize("connection", ["plain", "factory", "readonly"])
def test_registry_windows_current_and_predecessors_order_instants(tmp_path, connection):
    root = _root(tmp_path)
    stamps = ["2026-09-01T23:30:00Z", "2026-09-02T01:45:00+02:00",
              "2026-09-01T19:50:00-04:00", "2026-09-01T23:50:00Z",
              "2026-09-01T23:50:00.000001Z"]
    # Shuffled ingest; equal instants 2/3 still break by ingest sequence.
    events = [_snap(BOT, f"s{i}", stamps[i], _bot(BOT, f"m{i}"))
              for i in (2, 0, 4, 1, 3)]
    assert all(r.status == "committed" for r in emit_batch(root, events))
    path = root / "state/plane/plane.db"
    conn = _conn(root) if connection == "plain" else (
        db.connect(path) if connection == "factory" else db.connect_ro(path))
    if connection != "plain":
        assert conn.execute("SELECT claudlobby_instant_key(?)", (stamps[0],)).fetchone()[0] == "2026-09-01T23:30:00.000000Z"
    with conn:
        before = conn.execute("SELECT occurred_at, payload_hash FROM registry_snapshots ORDER BY ingest_seq").fetchall()
        current = rr.current_entities(conn)
        assert current[0]["payload"]["model"] == "m4"
        history = rr.entity_history(conn, BOT)
        assert [r["payload"]["model"] for r in history] == [f"m{i}" for i in range(5)]
        assert [r["valid_from"] for r in history] == [_stored(t) for t in stamps]
        assert [r["valid_to"] for r in history] == [_stored(t) for t in stamps[1:]] + [None]
        changes = {r["payload"]["model"]: r for r in rr.recent_changes(conn)}
        assert changes["m0"]["change"] == "first_observed"
        assert {k: v["fields"]["model"] for k, v in changes.items() if k != "m0"} == {
            f"m{i}": (f"m{i-1}", f"m{i}") for i in range(1, 5)}
        row = current[0]
        assert rr.current_hash(conn, row["host_uid"], "bot", row["entity_uid"]) == row["payload_hash"]
        assert tuple(conn.execute(REG_CURRENT_KEYS_SQL, (row["host_uid"],)).fetchone()) == ("bot", row["entity_uid"])
        assert conn.execute("SELECT occurred_at, payload_hash FROM registry_snapshots ORDER BY ingest_seq").fetchall() == before
    conn.close()


def test_offset_hash_gate_does_not_suppress_honest_rescan(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "new", "2026-09-01T09:00:00-04:00", _bot(BOT, "new"))])
    emit_batch(root, [_snap(BOT, "stale", "2026-09-01T12:00:00Z", _bot(BOT, "backfill"))])
    result = emit_batch(root, [_snap(BOT, "rescan", "2026-09-01T14:00:00Z", _bot(BOT, "backfill"))])
    assert result[0].status == "committed"
    with _conn(root) as conn:
        assert rr.current_entities(conn)[0]["scan_id"] == "rescan"


def test_offset_tombstone_and_stale_rescan_share_current_definition(tmp_path):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "initial", "2026-09-01T12:00:00Z", _bot(BOT))])
    at = "2026-09-01T09:00:00-04:00"  # 13:00Z, later than initial
    emit_batch(root, [_tomb(BOT, "delete", at), _done("delete", at)])
    emit_batch(root, [_snap(BOT, "stale", "2026-09-01T14:30:00+02:00", _bot(BOT, "backfill"))])
    with _conn(root) as conn:
        assert rr.current_entities(conn) == []
        hist = rr.entity_history(conn, BOT)
        assert [r["scan_id"] for r in hist] == ["initial", "stale", "delete"]
        host, uid = conn.execute("SELECT host_uid, entity_uid FROM registry_snapshots LIMIT 1").fetchone()
        assert rr.current_hash(conn, host, "bot", uid) is None
        assert conn.execute(REG_CURRENT_KEYS_SQL, (host,)).fetchall() == []
    # Recreating the stale payload after deletion must not hit its old hash.
    result = emit_batch(root, [_snap(BOT, "recreate", "2026-09-01T14:00:00Z", _bot(BOT, "backfill"))])
    assert result[0].status == "committed"
    with _conn(root) as conn:
        assert rr.current_entities(conn)[0]["scan_id"] == "recreate"
        assert rr.recent_changes(conn, limit=1)[0]["change"] == "recreated"


@pytest.mark.parametrize("bad", ["not-an-instant", "9999-12-31T23:59:59", "9999-12-31T23:59:59-01:00"])
def test_malformed_historical_registry_stamp_cannot_be_current(tmp_path, bad):
    root = _root(tmp_path)
    emit_batch(root, [_snap(BOT, "good", "2026-09-01T12:00:00Z", _bot(BOT, "good")),
                      _snap(BOT, "broken", "2026-09-01T13:00:00Z", _bot(BOT, "broken"))])
    with _conn(root) as conn:
        conn.execute("UPDATE registry_snapshots SET occurred_at=? WHERE scan_id='broken'", (bad,))
        assert rr.current_entities(conn)[0]["scan_id"] == "good"
        assert [r["scan_id"] for r in rr.entity_history(conn, BOT)] == ["good"]
        assert [r["scan_id"] for r in rr.recent_changes(conn)] == ["good"]


def _heartbeat(root, at, state, bot="a"):
    out = emit_batch(root, [{"event_type": "metric_sample", "emitter": "test", "fleet": "f", "occurred_at": at,
        "payload": {"subject_kind": "bot_instance", "subject": f"bot:f/{bot}",
                    "metric": "bot.heartbeat", "value": {"state": state}}}])
    assert out[0].status == "committed"


def test_heartbeat_offset_cutoff_and_order_cross_midnight(tmp_path):
    root = _root(tmp_path)
    stamps = ["2026-08-31T20:10:00-04:00", "2026-09-02T01:55:00+02:00", "2026-09-01T20:00:00-04:00"]
    for i in (2, 0, 1):
        _heartbeat(root, stamps[i], "BUSY" if i == 2 else "IDLE")
    _heartbeat(root, "2026-09-01T02:09:59.999999+02:00", "UNKNOWN")
    with _conn(root) as conn:
        entries = heartbeat_series(conn, now=datetime(2026, 9, 2, 0, 10, tzinfo=timezone.utc), fleet="f", days=1)["bot:f/a"]
    assert [at.isoformat() for at, _ in entries] == [_stored(t) for t in stamps]


def test_heartbeat_utilization_keeps_gap_cap_with_mixed_offsets(tmp_path):
    root = _root(tmp_path)
    _heartbeat(root, "2026-09-01T09:00:00-04:00", "BUSY")
    _heartbeat(root, "2026-09-01T12:00:00Z", "IDLE")
    _heartbeat(root, "2026-09-01T14:00:00+02:00", "UNKNOWN", bot="other")
    with _conn(root) as conn:
        rows = bot_utilization(conn, now=datetime(2026, 9, 1, 13, 10, tzinfo=timezone.utc), fleet="f")
    row = next(r for r in rows if r["alias"] == "bot:f/a")
    assert row["last_state"] == "BUSY"
    assert row["busy_pct_24h"] == 50.0
    assert row["samples"] == 2


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_instant_key_is_fixed_width_precise_and_host_timezone_independent(monkeypatch, tz):
    from claudlobby.plane.time import instant_key
    with monkeypatch.context() as m:
        m.setenv("TZ", tz)
        time.tzset()
        assert instant_key("0001-01-01T00:00:00Z") == "0001-01-01T00:00:00.000000Z"
        assert instant_key("2026-09-01T19:50:00-04:00") == instant_key("2026-09-02T01:50:00+02:00") == "2026-09-01T23:50:00.000000Z"
        assert instant_key("2026-09-01T23:50:00.000001Z") > instant_key("2026-09-01T23:50:00Z")
        assert all(instant_key(value) is None for value in (None, 42, "bad", "2026-09-01T00:00:00", "0001-01-01T00:00:00+01:00"))

def test_registration_is_idempotent_even_with_an_active_cursor():
    from claudlobby.plane.time import register_instant_key
    conn = sqlite3.connect(":memory:")
    register_instant_key(conn)
    cur = conn.execute("SELECT claudlobby_instant_key('2026-09-01T12:00:00Z') UNION ALL SELECT claudlobby_instant_key('2026-09-01T08:00:00-04:00')")
    first = cur.fetchone()
    register_instant_key(conn)
    assert cur.fetchone() == first
    assert conn.execute("SELECT claudlobby_instant_key('bad')").fetchone() == (None,)
    conn.close()
