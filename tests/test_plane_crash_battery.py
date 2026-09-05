"""Kernel crash/concurrency battery (design v2 §15).

Covers: concurrent emitters (25-writer burst), SQLITE_BUSY under a held
write lock, disk-full via PRAGMA max_page_count, duplicate replay under
concurrency, spool fallback when the db is unopenable.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sqlite3
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.migrations import migrate


def _mk_request(i: int) -> dict:
    return {
        "event_type": "task",
        "emitter": f"writer-{i}",
        "fleet": "example-fleet",
        "payload": {
            "work_item_id": "wi_" + f"{i:032x}",
            "event": "progress",
            "progress": i % 100,
            "actor": f"bot:example-fleet/w{i}",
        },
    }


def _worker(root: str, i: int, out: mp.Queue) -> None:
    try:
        outcome = emit(Path(root), _mk_request(i))
        out.put((i, outcome.status))
    except Exception as exc:  # noqa: BLE001
        out.put((i, f"error:{exc}"))


def test_25_writer_burst_loses_nothing(tmp_path: Path):
    # Prime db + host uid once to avoid a 25-way migration race:
    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")

    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_worker, args=(str(tmp_path), i, q)) for i in range(25)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    results = [q.get(timeout=5) for _ in range(25)]
    statuses = {s for _, s in results}
    assert statuses <= {"committed", "spooled"}, results
    conn = connect(db_path(tmp_path))
    committed = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'task'").fetchone()[0]
    spooled = len(list((tmp_path / "state" / "plane" / "spool").glob("*.json")))
    conn.close()
    assert committed + spooled == 25  # nothing lost
    # Ordering authority: seqs are gapless 1..N for committed rows
    conn = connect(db_path(tmp_path))
    seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY 1")]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM ingest_ledger l WHERE NOT EXISTS"
        " (SELECT 1 FROM events e WHERE e.event_id = l.event_id)"
    ).fetchone()[0]
    conn.close()
    assert seqs == list(range(1, committed + 1))
    assert orphans == 0   # round-2 F1: ledger↔family 1:1 holds under load


def test_busy_lock_leads_to_spool_not_loss(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    ensure_host_uid(tmp_path / "state")
    # Hold an exclusive write lock from a raw connection with NO busy_timeout,
    # long enough that emit's 5s busy_timeout expires:
    blocker = sqlite3.connect(db_path(tmp_path))
    blocker.execute("PRAGMA busy_timeout = 0")
    blocker.execute("BEGIN EXCLUSIVE")
    outcome = emit(tmp_path, _mk_request(1))
    assert outcome.status == "spooled"
    blocker.rollback()
    blocker.close()
    conn.close()


def test_disk_full_spools(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    ensure_host_uid(tmp_path / "state")
    # Clamp the db to its current page count so the next insert gets SQLITE_FULL:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {pages}")
    conn.close()
    # emit opens its own connection; re-apply the clamp there by pre-shrinking:
    # max_page_count is per-connection, so simulate instead by filling: insert
    # rows until SQLITE_FULL via a clamped connection, proving the spool path.
    clamped = sqlite3.connect(db_path(tmp_path))
    clamped.execute(f"PRAGMA max_page_count = {pages}")
    with pytest.raises(sqlite3.OperationalError, match="full"):
        for i in range(10_000):
            clamped.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, 'task_event', 't')",
                (f"ev_{i:032x}",),
            )
        clamped.commit()
    clamped.close()


def test_derivation_fixtures(tmp_path: Path):
    """Round-4 F7: the reviewer's reassignment counterexample, as a fixture.
    asg1 acked+reassigned (terminal, successor asg2); asg2 acked and OVERDUE.
    Attention must return asg2 and never asg1; task-status must be terminal-
    dominant (late progress after completed does not reopen)."""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.ids import (
        mint_assignment_id, mint_msg_id, mint_work_item_id,
    )
    from claudlobby.plane.migrations import migrate

    conn = connect(db_path(tmp_path)); migrate(conn); conn.close()
    ensure_host_uid(tmp_path / "state")
    wi, a1, a2 = mint_work_item_id(), mint_assignment_id(), mint_assignment_id()
    m1, m2 = mint_msg_id(), mint_msg_id()

    def tx(msg, state):
        return {"event_type": "transmission", "emitter": "fx",
                "fleet": "fx-fleet",
                "payload": {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
                             "destination": "sock", "state": state}}

    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "title": "t",
                      "created_by": "bot:fx-fleet/mgr"}},
        {"event_type": "assignment", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"assignment_id": a1, "work_item_id": wi,
                      "assignee": "bot:fx-fleet/w1",
                      "assigned_by": "bot:fx-fleet/mgr",
                      "expected_by": "2026-01-01T00:00:00+00:00",
                      "dispatch_msg_id": m1}},
        {"event_type": "communication", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"msg_id": m1, "sender": "bot:fx-fleet/mgr",
                      "recipient": "bot:fx-fleet/w1",
                      "message_class": "task_request", "command_type": "task",
                      "work_item_id": wi, "assignment_id": a1,
                      "privacy": "full"}},
        tx(m1, "pane_submitted"), tx(m1, "recipient_acknowledged"),
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a1,
                      "event": "reassigned", "successor_id": a2}},
        {"event_type": "assignment", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"assignment_id": a2, "work_item_id": wi,
                      "assignee": "bot:fx-fleet/w2",
                      "assigned_by": "bot:fx-fleet/mgr",
                      "expected_by": "2026-01-01T00:00:00+00:00",
                      "dispatch_msg_id": m2}},
        {"event_type": "communication", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"msg_id": m2, "sender": "bot:fx-fleet/mgr",
                      "recipient": "bot:fx-fleet/w2",
                      "message_class": "task_request", "command_type": "task",
                      "work_item_id": wi, "assignment_id": a2,
                      "privacy": "full"}},
        tx(m2, "pane_submitted"), tx(m2, "recipient_acknowledged"),
    ])
    conn = connect(db_path(tmp_path))
    from claudlobby.plane.queries import ATTENTION_SQL, attention_params

    attention = [r[0] for r in conn.execute(ATTENTION_SQL,
                                            attention_params("2026-06-01"))]
    assert attention == [a2], f"attention must surface ONLY the successor: {attention}"
    # terminal dominance: complete a2, then a late progress must not reopen
    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a2,
                      "event": "completed"}},
        {"event_type": "task", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"work_item_id": wi, "assignment_id": a2,
                      "event": "progress", "progress": 10}},
    ])
    from claudlobby.plane.queries import RECONCILIATION_SQL, TASK_STATUS_SQL

    statuses = {r[0]: r[1] for r in conn.execute(TASK_STATUS_SQL)}
    assert statuses[a2] == "completed", (
        f"terminal must dominate late progress: {statuses[a2]}")
    assert statuses[a1] == "reassigned"
    # Reconciliation fixture (round-7): one submitted-never-acked transmission
    # must count exactly 1 (m1 and m2 are both acked above).
    m3 = mint_msg_id()
    emit_batch(tmp_path, [
        {"event_type": "communication", "emitter": "fx", "fleet": "fx-fleet",
         "payload": {"msg_id": m3, "sender": "bot:fx-fleet/mgr",
                      "recipient": "bot:fx-fleet/w2",
                      "message_class": "nudge", "privacy": "full"}},
        tx(m3, "pane_submitted"),
    ])
    assert conn.execute(RECONCILIATION_SQL).fetchone()[0] == 1
    conn.close()


def test_workstream_reducer_fixtures(tmp_path: Path):
    """Round-6/7 F7: the reducer's semantics gate its timing. NINE cases —
    the reviewer's three counterexamples (later-shorter renewal,
    out-of-order timestamps, RECENTLY-EXPIRED renewal) plus a currently-
    blocked case their mutation probe proved uncovered. Ledger order is
    authoritative; renewal horizons compare against NOW, activity against
    CUTOFF. SQL comes from claudlobby.plane.queries — the SAME string the
    benchmark times."""
    from claudlobby.plane.queries import WORKSTREAM_STATUS_SQL
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.migrations import migrate

    conn = connect(db_path(tmp_path))
    migrate(conn)
    eid = [0]

    def seed_ws(wsid):
        eid[0] += 1
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream', 't')", (f"ev_c{eid[0]:031x}",))
        conn.execute(
            "INSERT INTO workstreams (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, workstream_id,"
            " title, opened_by_uid) VALUES (?, ?, '1',"
            " '2026-01-01T00:00:00+00:00', 't', 'h', 'fx', ?, 't', 'actor_x')",
            (cur.lastrowid, f"ev_c{eid[0]:031x}", wsid))

    def ev(wsid, event, occurred="2026-05-01T00:00:00+00:00", renewed=None):
        eid[0] += 1
        cur = conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES (?, 'workstream_event', 't')", (f"ev_e{eid[0]:031x}",))
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version,"
            " occurred_at, ingested_at, host_uid, emitter, kind, event,"
            " workstream_id, renewed_until) VALUES (?, ?, '1', ?, 't', 'h',"
            " 'fx', 'workstream', ?, ?, ?)",
            (cur.lastrowid, f"ev_e{eid[0]:031x}", occurred, event, wsid, renewed))

    now = "2026-08-23T00:00:00+00:00"
    cutoff = "2026-08-09T00:00:00+00:00"   # now − 14d policy window
    seed_ws("ws-arch");   ev("ws-arch", "archived")
    seed_ws("ws-closed"); ev("ws-closed", "closed")
    seed_ws("ws-unblk");  ev("ws-unblk", "blocked"); ev("ws-unblk", "unblocked",
                             occurred="2026-08-20T00:00:00+00:00")
    seed_ws("ws-renew");  ev("ws-renew", "renewed",
                             renewed="2099-01-01T00:00:00+00:00")
    seed_ws("ws-stale");  ev("ws-stale", "progressed")
    # The counterexample: OLD long renewal, then LATER shortening — latest
    # (by ledger order) governs, so this is STALE:
    seed_ws("ws-short");  ev("ws-short", "renewed",
                             renewed="2099-01-01T00:00:00+00:00")
    ev("ws-short", "renewed", renewed="2026-06-01T00:00:00+00:00")
    # Out-of-order timestamps: ledger-later event carries an OLDER
    # occurred_at; ledger order still decides activity — stale:
    seed_ws("ws-ooo");    ev("ws-ooo", "progressed",
                             occurred="2026-08-20T00:00:00+00:00")
    ev("ws-ooo", "progressed", occurred="2026-04-01T00:00:00+00:00")
    # Round-7: RECENTLY-EXPIRED renewal — horizon (08-15) is past cutoff
    # (08-09) but BEFORE now (08-23): "until" means until, so with old
    # activity this is STALE (the cutoff comparison granted an unratified
    # post-expiry grace window):
    seed_ws("ws-expired"); ev("ws-expired", "renewed",
                              renewed="2026-08-15T00:00:00+00:00")
    # Round-7: currently blocked — the mutation probe proved no fixture
    # exercised the blocked branch:
    seed_ws("ws-blocked"); ev("ws-blocked", "blocked")

    res = dict(conn.execute(WORKSTREAM_STATUS_SQL, (now, cutoff)).fetchall())
    conn.close()
    assert res == {"ws-arch": "archived", "ws-closed": "closed",
                   "ws-unblk": "active", "ws-renew": "active",
                   "ws-stale": "stale", "ws-short": "stale",
                   "ws-ooo": "stale", "ws-expired": "stale",
                   "ws-blocked": "blocked"}


def test_eqp_detector_catches_aliased_bare_scan(tmp_path: Path):
    """Round-7 F7: a forced unindexed query plans as "SCAN e" (aliased) —
    the detector must flag it, and must NOT flag an index-assisted scan."""
    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.migrations import migrate
    from claudlobby.plane.queries import events_aliases, is_bare_events_scan

    conn = connect(db_path(tmp_path))
    migrate(conn)
    forced = ("SELECT COUNT(*) FROM events e"
              " WHERE json_extract(e.detail, '$.x') = 1")
    plans = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + forced)]
    aliases = events_aliases(forced)
    assert "e" in aliases
    assert any(is_bare_events_scan(d.strip(), aliases) for d in plans), plans
    indexed = ("SELECT COUNT(*) FROM events e WHERE e.kind = 'task'")
    plans2 = [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + indexed)]
    assert not any(
        is_bare_events_scan(d.strip(), events_aliases(indexed)) for d in plans2
    ), plans2
    conn.close()


def test_eqp_detector_handles_old_sqlite_table_form():
    """Post-review fix 2: SQLite < 3.36 prints "SCAN TABLE events [AS e]" —
    the detector must flag both eras' formats and stay quiet on USING."""
    from claudlobby.plane.queries import is_bare_events_scan

    aliases = frozenset({"events", "e"})
    assert is_bare_events_scan("SCAN TABLE events", aliases)
    assert is_bare_events_scan("SCAN TABLE events AS e", aliases)
    assert is_bare_events_scan("SCAN e", aliases)
    assert not is_bare_events_scan("SCAN TABLE events USING INDEX idx_x", aliases)
    assert not is_bare_events_scan("SEARCH e USING COVERING INDEX idx_x", aliases)
    assert not is_bare_events_scan("SCAN TABLE other", aliases)


def test_spool_serializes_datetime_payloads(tmp_path: Path):
    """Post-review fix 1: an in-process caller may pass datetimes (EmitRequest
    accepts them); a spool on db-failure must serialize them, not die with a
    TypeError outside the taxonomy."""
    from datetime import datetime, timezone

    import os as _os

    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")
    _os.chmod(db_path(tmp_path), 0o400)          # retryable OperationalError
    req = _mk_request(31)
    req["occurred_at"] = datetime.now(timezone.utc)   # datetime, not string
    out = emit(tmp_path, req)
    assert out.status == "spooled"
    spooled = list((tmp_path / "state" / "plane" / "spool").glob("*.json"))
    assert len(spooled) == 1
    _os.chmod(db_path(tmp_path), 0o600)
    conn = connect(db_path(tmp_path))
    from claudlobby.plane.spool import drain
    report = drain(tmp_path, conn, ensure_host_uid(tmp_path / "state"))
    conn.close()
    assert report.ingested == 1 and report.quarantined == 0


def test_close_failure_after_commit_reports_committed(tmp_path: Path, monkeypatch):
    """Post-review fix 4: a retryable close() failure AFTER commit must not
    reclassify a committed batch as spooled."""
    import sqlite3 as sq

    import claudlobby.plane.emit_api as mod

    conn0 = connect(db_path(tmp_path))
    migrate(conn0)
    conn0.close()
    ensure_host_uid(tmp_path / "state")
    real_connect = mod.connect

    class _CloseBomb:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self._inner.close()
            raise sq.OperationalError("disk I/O error")   # retryable class

    monkeypatch.setattr(mod, "connect", lambda p: _CloseBomb(real_connect(p)))
    out = emit(tmp_path, _mk_request(32))
    assert out.status == "committed"          # not "spooled"
    assert not list((tmp_path / "state" / "plane" / "spool").glob("*.json"))


def test_readonly_db_emit_spools_end_to_end(tmp_path: Path):
    """Round-3 F6: the disk-full raw demo never exercised emit. This does,
    via the same error CLASS (OperationalError at write — readonly here,
    SQLITE_FULL in the wild): emit → spooled entry on disk → drain recovers."""
    import os as _os

    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")
    _os.chmod(db_path(tmp_path), 0o400)
    out = emit(tmp_path, _mk_request(9))
    assert out.status == "spooled"
    spooled = list((tmp_path / "state" / "plane" / "spool").glob("*.json"))
    assert len(spooled) == 1
    _os.chmod(db_path(tmp_path), 0o600)
    conn = connect(db_path(tmp_path))
    from claudlobby.plane.spool import drain
    report = drain(tmp_path, conn, ensure_host_uid(tmp_path / "state"))
    conn.close()
    assert report.ingested == 1 and report.remaining == 0


def test_duplicate_event_id_under_concurrency(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    conn.close()
    ensure_host_uid(tmp_path / "state")
    fixed = {"event_id": "ev_" + "d" * 32, **_mk_request(1)}
    first = emit(tmp_path, dict(fixed))
    second = emit(tmp_path, dict(fixed))
    assert first.status == "committed" and second.status == "duplicate"


def test_unopenable_db_spools(tmp_path: Path):
    # A directory where the db file should be → connect raises → spool.
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "plane.db").mkdir()
    outcome = emit(tmp_path, _mk_request(2))
    assert outcome.status == "spooled"
