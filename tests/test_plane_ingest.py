from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane.contracts import validate_request
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.ids import ensure_host_uid, mint_event_id
from claudlobby.plane.ingest import ingest, now_iso
from claudlobby.plane.migrations import migrate


@pytest.fixture()
def env(tmp_path: Path):
    conn = connect(db_path(tmp_path))
    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    yield conn, host
    conn.close()


def _intent_req(event_id=None) -> dict:
    return {
        "event_type": "communication",
        "emitter": "test-suite",
        "fleet": "example-fleet",
        "event_id": event_id,
        "payload": {
            "msg_id": "msg_" + "1" * 32,
            "sender": "bot:example-fleet/alpha",
            "recipient": "bot:example-fleet/beta",
            "message_class": "chat",
            "body": "hello",
            "privacy": "full",
        },
    }


def test_ingest_writes_ledger_and_family(env):
    conn, host = env
    e, p = validate_request(_intent_req())
    result = ingest(conn, e, p, host_uid=host)
    assert result.duplicate is False and result.ingest_seq == 1
    row = conn.execute("SELECT * FROM communications").fetchone()
    assert row["event_id"] == result.event_id
    assert row["ingest_seq"] == 1
    assert row["host_uid"] == host
    assert row["sender_alias"] == "bot:example-fleet/alpha"
    assert row["sender_uid"].startswith("actor_")
    assert row["fleet_uid"].startswith("fleet_")
    ledger = conn.execute("SELECT family FROM ingest_ledger").fetchone()
    assert ledger["family"] == "communication"


def test_duplicate_event_id_is_success_and_writes_nothing(env):
    conn, host = env
    eid = mint_event_id()
    e, p = validate_request(_intent_req(event_id=eid))
    first = ingest(conn, e, p, host_uid=host)
    assert first.duplicate is False
    # Same event replayed (spool drain, door retry) — different msg body even:
    again = validate_request(_intent_req(event_id=eid))
    second = ingest(conn, again[0], again[1], host_uid=host)
    assert second.duplicate is True and second.ingest_seq is None
    assert conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1


def test_family_failure_rolls_back_ledger(env, monkeypatch):
    """If the family insert dies, the ledger row must not survive —
    otherwise the event_id is burned and replay would report duplicate
    for an event that was never stored."""
    conn, host = env
    e, p = validate_request(_intent_req())
    import claudlobby.plane.ingest as mod

    def boom(*a, **k):
        raise RuntimeError("family insert failed")

    monkeypatch.setattr(mod, "_family_values", boom)
    with pytest.raises(RuntimeError):
        ingest(conn, e, p, host_uid=host)
    assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 0
    assert not conn.in_transaction   # rollback left no open transaction


def test_duplicate_with_missing_family_row_raises(env):
    """Round-2 F1: a ledger row whose family row is gone is CORRUPTION —
    replay must refuse duplicate classification, never absorb it."""
    conn, host = env
    from claudlobby.plane.ids import mint_event_id as _mint
    eid = _mint()
    e, p = validate_request(_intent_req(event_id=eid))
    ingest(conn, e, p, host_uid=host)
    conn.execute("DELETE FROM communications WHERE event_id = ?", (eid,))
    again = validate_request(_intent_req(event_id=eid))
    with pytest.raises(RuntimeError, match="divergence"):
        ingest(conn, again[0], again[1], host_uid=host)


def test_occurred_at_defaults_to_now(env):
    conn, host = env
    e, p = validate_request(_intent_req())
    ingest(conn, e, p, host_uid=host)
    row = conn.execute("SELECT occurred_at, ingested_at FROM communications").fetchone()
    assert row["occurred_at"].endswith("+00:00")
    assert row["ingested_at"].endswith("+00:00")


def test_now_iso_shape():
    s = now_iso()
    assert s.endswith("+00:00") and "T" in s
