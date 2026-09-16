"""PR-#1345 review F1 + F13: kind=system trust semantics.

§9b's contract, pinned: severity is REGISTRY-OWNED (ingest stamps it from
registries.SYSTEM_EVENT_SEVERITY; callers cannot set it; unknown token =>
NULL), and DIAGNOSTIC data over-cap TRUNCATES with the detail_truncated flag
— it never costs the event that carried it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby.plane.contracts import ContractViolation, FIELD_POLICY
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY


def _sys(event: str, **payload) -> dict:
    return {
        "event_type": "system",
        "emitter": "sysfam-test",
        "payload": {"event": event, **payload},
    }


def _row(root: Path, event: str):
    conn = connect(db_path(root))
    row = conn.execute(
        "SELECT severity, detail, detail_truncated FROM events"
        " WHERE kind='system' AND event = ?", (event,)
    ).fetchone()
    conn.close()
    return row


def test_caller_supplied_severity_is_a_contract_violation(tmp_path: Path):
    """severity is not a wire field at all — a caller setting it is a bug."""
    with pytest.raises(ContractViolation):
        emit(tmp_path, _sys("intrusion_detected", severity="critical"))
    assert not db_path(tmp_path).exists() or connect(db_path(tmp_path)).execute(
        "SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_known_token_gets_registry_stamped_severity(tmp_path: Path):
    assert SYSTEM_EVENT_SEVERITY["daemon_started"] == "notice"
    emit(tmp_path, _sys("daemon_started"))
    assert _row(tmp_path, "daemon_started")["severity"] == "notice"


def test_unknown_token_ingests_with_null_severity(tmp_path: Path):
    emit(tmp_path, _sys("brand_new_machinery_event"))
    row = _row(tmp_path, "brand_new_machinery_event")
    assert row is not None, "F19: an unknown token must INGEST, never vanish"
    assert row["severity"] is None


def test_overcap_data_truncates_with_flag_never_rejects(tmp_path: Path):
    cap = FIELD_POLICY[("system", "data")]["cap"]
    emit(tmp_path, _sys("big_diag", data={"blob": "x" * (cap + 100)}))
    row = _row(tmp_path, "big_diag")
    assert row is not None, "an oversized diagnostic must not cost the event"
    assert row["detail_truncated"] == 1
    assert len(row["detail"].encode("utf-8")) <= cap
    assert row["detail"].startswith('{"blob"')


def test_undercap_data_stored_whole_and_unflagged(tmp_path: Path):
    emit(tmp_path, _sys("small_diag", data={"n": 3}))
    row = _row(tmp_path, "small_diag")
    assert row["detail_truncated"] == 0
    assert json.loads(row["detail"]) == {"n": 3}


def test_wire_pattern_is_wire_tier_only_ddl_stays_loose(tmp_path: Path):
    """F13, pinned as the DELIBERATE asymmetry: the wire rejects a
    non-snake token, the DDL accepts it — registry-governed means a
    direct-SQL writer is not shape-gated (the manifest rules vocab None)."""
    with pytest.raises(ContractViolation):
        emit(tmp_path, _sys("Brand-New-Machinery"))
    from claudlobby.plane.ids import ensure_host_uid, mint_event_id
    from claudlobby.plane.ingest import ingest_many
    from claudlobby.plane.contracts import validate_request

    env, payload = validate_request(_sys("seed_token"))
    conn = connect(db_path(tmp_path))
    from claudlobby.plane.migrations import migrate

    migrate(conn)
    host = ensure_host_uid(tmp_path / "state")
    ingest_many(conn, [(env, payload)], host_uid=host)
    probe_id = mint_event_id()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
        " VALUES (?, 'system', '2026-08-28T00:00:00+00:00')", (probe_id,),
    )
    conn.execute(
        "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
        " ingested_at, host_uid, emitter, kind, event, detail_truncated)"
        " SELECT ?, ?, schema_version, occurred_at,"
        " ingested_at, host_uid, emitter, 'system', 'Direct-SQL-Token', 0"
        " FROM events LIMIT 1",
        (cur.lastrowid, probe_id),
    )
    conn.execute("ROLLBACK")   # acceptance proven; the row itself not kept
    conn.close()