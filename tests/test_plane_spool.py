from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.migrations import migrate
from claudlobby.plane.spool import (
    MAX_ATTEMPTS,
    drain,
    quarantine_dir,
    spool_dir,
    spool_entries,
    spool_write,
)


def _req(msg_suffix="2") -> dict:
    return {
        "event_type": "communication",
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "payload": {
            "msg_id": "msg_" + msg_suffix * 32,
            "sender": "bot:example-fleet/alpha",
            "message_class": "notice",
            "body": "spooled hello",
            "privacy": "full",
        },
    }


def _fin(req: dict, eid: str) -> dict:
    return {**req, "event_id": eid, "occurred_at": "2026-08-24T00:00:00+00:00"}


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield tmp_path, conn, host
    conn.close()


def test_spool_write_is_fsynced_0600_json(env):
    root, conn, host = env
    eid = mint_event_id()
    p = spool_write(root, [_fin(_req(), eid)], "db locked")
    import os as _os, stat as _stat
    assert p.parent == spool_dir(root)
    assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600
    data = json.loads(p.read_text())
    assert data["event_ids"] == [eid] and data["attempts"] == 0
    assert data["requests"][0]["occurred_at"]  # F6: event time survives the spool
    assert not list(spool_dir(root).glob("*.tmp"))


def test_drain_ingests_and_deletes(env):
    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "db locked")
    report = drain(root, conn, host)
    assert report.ingested == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
    assert spool_entries(root) == []


def test_drain_duplicate_is_success(env):
    root, conn, host = env
    eid = mint_event_id()
    spool_write(root, [_fin(_req(), eid)], "x")
    drain(root, conn, host)
    spool_write(root, [_fin(_req(), eid)], "x")
    report = drain(root, conn, host)
    assert report.duplicates == 1 and report.remaining == 0
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1


def test_malformed_spool_file_quarantined(env):
    root, conn, host = env
    (spool_dir(root) / "garbage.json").write_text("{not json")
    report = drain(root, conn, host)
    assert report.quarantined == 1
    assert list(quarantine_dir(root).iterdir())


def test_contract_violation_quarantined_not_retried(env):
    root, conn, host = env
    req = _req()
    req["payload"]["message_class"] = "no-such-class"
    spool_write(root, [_fin(req, mint_event_id())], "x")
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0


def test_operational_errors_retry_then_quarantine(env, monkeypatch):
    """Only OperationalErrors accepted by is_retryable() are retryable."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def busy(*a, **k):
        raise sq.OperationalError("database is locked")

    monkeypatch.setattr(mod, "ingest_many", busy)
    for _ in range(MAX_ATTEMPTS):
        drain(root, conn, host)
    assert spool_entries(root) == []
    assert len(list(quarantine_dir(root).glob("*.json"))) == 1


def test_quarantine_artifacts_are_0600(env):
    """Round-4 F6: reason sidecars and MOVED files both end 0600, whatever
    mode the malformed file arrived with."""
    import os as _os, stat as _stat

    root, conn, host = env
    bad = spool_dir(root) / "garbage.json"
    bad.write_text("{not json")          # arrives 0644 by umask — the trap
    drain(root, conn, host)
    q = quarantine_dir(root)
    moved = q / "garbage.json"
    sidecar = q / "garbage.json.reason"
    assert _stat.S_IMODE(_os.stat(moved).st_mode) == 0o600
    assert _stat.S_IMODE(_os.stat(sidecar).st_mode) == 0o600


def test_sql_bug_operational_errors_quarantine_not_retry(env, monkeypatch):
    """Round-5 F6: EXACT assertion — a code-less 'no such table' (the
    synthetic/3.10 form) quarantines immediately, never retries. The round-4
    either/or blessed the wrong path."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def missing_table(*a, **k):
        raise sq.OperationalError("no such table: events")   # code=None

    monkeypatch.setattr(mod, "ingest_many", missing_table)
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0


def test_codeless_infra_message_still_retries(env, monkeypatch):
    """The fallback's other half: a code-less LOCKED message retries."""
    import sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def locked(*a, **k):
        raise sq.OperationalError("database is locked")      # code=None

    monkeypatch.setattr(mod, "ingest_many", locked)
    report = drain(root, conn, host)
    assert report.remaining == 1 and report.quarantined == 0
    assert json.loads(next(spool_dir(root).glob("*.json")).read_text())["attempts"] == 1


def test_retry_rewrite_preserves_0600(env, monkeypatch):
    """Round-3 F6: the executed probe caught retry rewrites at 0644."""
    import os as _os, stat as _stat, sqlite3 as sq

    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def busy(*a, **k):
        raise sq.OperationalError("database is locked")

    monkeypatch.setattr(mod, "ingest_many", busy)
    drain(root, conn, host)
    f = next(spool_dir(root).glob("*.json"))
    assert _stat.S_IMODE(_os.stat(f).st_mode) == 0o600
    assert json.loads(f.read_text())["attempts"] == 1


def test_non_retryable_quarantines_immediately(env, monkeypatch):
    root, conn, host = env
    spool_write(root, [_fin(_req(), mint_event_id())], "x")
    import claudlobby.plane.spool as mod

    def poison(*a, **k):
        raise RuntimeError("ledger/family divergence")

    monkeypatch.setattr(mod, "ingest_many", poison)
    report = drain(root, conn, host)
    assert report.quarantined == 1 and report.remaining == 0
