"""Duplicate event IDs alone must never authorize a second operational effect."""

from dataclasses import asdict
import json

import pytest

from claudlobby.plane.contracts import validate_request
from claudlobby.plane.db import connect, db_file
from claudlobby.plane.emit_api import validate_item
from claudlobby.plane.identity import resolve, resolve_party
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.ingest import ingest_many
from claudlobby.request_facts import expected_fact, reconcile_facts
from tests.plane_setup import initialize_plane


@pytest.fixture
def batch(tmp_path):
    initialize_plane(tmp_path)
    conn = connect(db_file(tmp_path))
    host = ensure_host_uid(tmp_path / "state")
    fleet = resolve(conn, "fleet", "example", now="2026-09-28T00:00:00Z")
    parties = {alias: resolve_party(conn, alias, now="2026-09-28T00:00:00Z")
               for alias in ("bot:example/manager", "bot:example/worker")}
    raws = [
        {"event_type": "work_item", "payload": {
            "work_item_id": "wi_" + "1" * 32, "title": "Authored private task title",
            "created_by": "bot:example/manager", "body": "Authored private task body"}},
        {"event_type": "assignment", "payload": {
            "assignment_id": "asg_" + "2" * 32, "work_item_id": "wi_" + "1" * 32,
            "assignee": "bot:example/worker", "assigned_by": "bot:example/manager"}},
    ]
    raws = [{**raw, "event_id": f"ev_{index:032x}", "fleet": "example",
             "emitter": "claudlobby.tasks.v1", "schema_version": "1.0.0",
             "occurred_at": "2026-09-28T00:00:00Z"} for index, raw in enumerate(raws, 1)]
    items = [validate_request(raw) for raw in raws]
    facts = tuple(expected_fact(item, host_uid=host, fleet_uid=fleet, parties=parties) for item in items)
    yield conn, host, fleet, parties, raws, items, facts
    conn.close()


def test_atomic_batch_proof_survives_receipt_crash_without_retaining_plaintext(batch):
    conn, host, _, _, _, items, facts = batch
    assert reconcile_facts(conn, facts).status == "unrecorded"
    ingest_many(conn, items, host_uid=host)
    assert reconcile_facts(conn, facts).status == "committed"
    assert reconcile_facts(conn, facts).matched == 2
    assert "Authored private" not in json.dumps([asdict(fact) for fact in facts])
    conn.execute("PRAGMA query_only=1")
    assert reconcile_facts(conn, facts).status == "committed"


def test_duplicate_success_is_not_content_proof_and_partial_batch_refuses(batch):
    conn, host, _, _, _, items, facts = batch
    ingest_many(conn, items[:1], host_uid=host)
    assert reconcile_facts(conn, facts).status == "conflict"
    ingest_many(conn, items[1:], host_uid=host)
    conn.execute("UPDATE work_items SET title='different effect'")
    assert all(outcome.duplicate for outcome in ingest_many(conn, items, host_uid=host))
    assert reconcile_facts(conn, facts).status == "conflict"


def test_capture_projection_and_pruned_or_unavailable_proof_do_not_authorize_replay(batch):
    conn, host, fleet, parties, raws, _, _ = batch
    items = [validate_item(raw, {"example": "metadata"})[0] for raw in raws]
    facts = tuple(expected_fact(item, host_uid=host, fleet_uid=fleet, parties=parties) for item in items)
    ingest_many(conn, items, host_uid=host)
    assert conn.execute("SELECT body FROM work_items").fetchone()[0] is None
    assert reconcile_facts(conn, facts).status == "committed"
    conn.execute("DELETE FROM work_items")
    assert reconcile_facts(conn, facts).status == "unknown"
    conn.close()
    assert reconcile_facts(conn, facts).status == "unknown"
