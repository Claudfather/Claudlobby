"""Unit tests for `claudlobby.plane.workstream_import.plan` — the pure
function behind `claudlobby plane import-workstreams` (#1635). No
subprocess, no db: `plan()` takes plain dicts and returns plain dicts, so
these run in milliseconds and can afford to cover edge cases the slower
end-to-end suite (`tests/test_plane_cutover_workstreams.py`) does not.
"""

from __future__ import annotations

from claudlobby.plane.workstream_import import (
    ImportPlan,
    _plus_days_iso,
    _project_key,
    batch_id,
    plan,
)

FLEET = "f"
EMPTY = {"workstreams": {}, "archived": []}


def _row(**over):
    row = {
        "id": "ws-x",
        "fleet": FLEET,
        "title": "A row",
        "project": None,
        "status": "active",
        "owner_bot": "w1",
        "next": "keep going",
        "task_ids": [],
        "refs": {"issues": [], "prs": []},
        "opened_ts": "2026-08-01T00:00:00Z",
        "last_progress_ts": "2026-08-01T00:00:00Z",
        "lease_expires_ts": "2026-08-15T00:00:00Z",
        "renewals": [],
    }
    row.update(over)
    return {"updated": row["last_progress_ts"], "workstreams": {row["id"]: row}}


def test_project_key_passes_through_a_matching_value():
    assert _project_key("alpha-project") == "alpha-project"


def test_project_key_drops_a_display_cased_value():
    """Mirrors lib/workstream-update.sh's own gate field for field: the
    writer never normalizes, it drops. The real file on this host carries
    "Claudlobby" (display-cased) for exactly this reason."""
    assert _project_key("Claudlobby") is None


def test_project_key_drops_none_and_empty():
    assert _project_key(None) is None
    assert _project_key("") is None


def test_plus_days_iso_matches_the_readers_own_derivation():
    assert _plus_days_iso("2026-08-01T00:00:00Z", 14) == "2026-08-15T00:00:00Z"


def test_never_progressed_emits_construct_only():
    doc = _row()  # opened_ts == last_progress_ts
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    assert not p.skipped and not p.warnings
    assert len(p.events) == 1
    assert p.events[0]["event_type"] == "workstream"
    assert p.events[0]["payload"]["goal"] == "keep going"


def test_progressed_row_emits_construct_then_progressed_in_that_order():
    doc = _row(last_progress_ts="2026-08-10T00:00:00Z")
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    assert [e["event_type"] for e in p.events] == ["workstream", "workstream_event"]
    assert p.events[1]["payload"]["event"] == "progressed"
    assert p.events[1]["occurred_at"] == "2026-08-10T00:00:00Z"
    assert p.events[1]["payload"]["next_step"] == "keep going"


def test_blocked_row_emits_a_blocked_event_carrying_next_as_note():
    doc = _row(status="blocked")
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    blocked = [e for e in p.events if e.get("payload", {}).get("event") == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["note"] == "keep going"


def test_done_row_emits_closed_with_disposition_and_closed_ts():
    doc = _row(status="done", closed_ts="2026-08-20T00:00:00Z")
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    closed = [e for e in p.events if e.get("payload", {}).get("event") == "closed"]
    assert len(closed) == 1
    assert closed[0]["payload"]["disposition"] == "done"
    assert closed[0]["occurred_at"] == "2026-08-20T00:00:00Z"


def test_unrecognised_status_warns_and_emits_no_status_event():
    doc = _row(status="mystery-status")
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    assert any("unrecognised status" in w.detail for w in p.warnings)
    assert all(e["event_type"] == "workstream" for e in p.events)  # construct only


def test_missing_opened_ts_skips_the_row_with_a_warning_not_a_crash():
    doc = _row()
    del doc["workstreams"]["ws-x"]["opened_ts"]
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    assert p.events == []
    assert any("no opened_ts" in w.detail for w in p.warnings)


def test_missing_owner_bot_falls_back_to_a_synthetic_import_actor():
    doc = _row(owner_bot=None)
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    payload = p.events[0]["payload"]
    assert payload["opened_by"] == f"bot:{FLEET}/legacy-import"
    assert "owner" not in payload  # never fabricated -- optional, correctly absent


def test_multiple_renewals_ingest_in_chronological_order_not_array_order():
    """The array is deliberately fed OUT of chronological order — plan()
    must sort by each entry's own ts, not trust array position, and must
    not confuse it with the row's progressed event (also present here,
    chronologically in between the two renewals)."""
    doc = _row(
        last_progress_ts="2026-08-05T00:00:00Z",
        renewals=[
            {"ts": "2026-08-12T00:00:00Z", "note": "second renewal"},
            {"ts": "2026-08-03T00:00:00Z", "note": "first renewal"},
        ],
    )
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    verb_events = [e for e in p.events if e["event_type"] == "workstream_event"]
    occurred = [e["occurred_at"] for e in verb_events]
    assert occurred == sorted(occurred), occurred
    assert occurred == [
        "2026-08-03T00:00:00Z",  # first renewal
        "2026-08-05T00:00:00Z",  # progressed, in between
        "2026-08-12T00:00:00Z",  # second renewal
    ]
    notes = [
        e["payload"].get("note")
        for e in verb_events
        if e["payload"]["event"] == "renewed"
    ]
    assert notes == ["first renewal", "second renewal"]


def test_renewal_renewed_until_derives_from_its_own_ts():
    doc = _row(renewals=[{"ts": "2026-08-03T00:00:00Z", "note": "n"}])
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    renewed = [e for e in p.events if e.get("payload", {}).get("event") == "renewed"][0]
    assert renewed["payload"]["renewed_until"] == "2026-08-17T00:00:00Z"


def test_dedup_skips_a_live_id():
    doc = _row()
    existing = {"workstreams": {"ws-x": {}}, "archived": []}
    p = plan(doc, existing, fleet=FLEET, import_batch="b1", lease_days=14)
    assert p.events == []
    assert p.skipped and p.skipped[0].workstream_id == "ws-x"


def test_dedup_skips_an_archived_id():
    """R1 gauntlet hazard 1: a construct id is unique per fleet forever --
    archived counts exactly as live for dedup."""
    doc = _row()
    existing = {"workstreams": {}, "archived": ["ws-x"]}
    p = plan(doc, existing, fleet=FLEET, import_batch="b1", lease_days=14)
    assert p.events == []
    assert p.skipped and p.skipped[0].workstream_id == "ws-x"


def test_event_ids_are_stable_across_two_identical_plan_calls():
    """The idempotence property `plan()` itself is responsible for: same
    input twice must derive the same event_ids, never depending on
    import_batch (which the caller may vary between attempts)."""
    doc = _row(last_progress_ts="2026-08-10T00:00:00Z")
    p1 = plan(doc, EMPTY, fleet=FLEET, import_batch="batch-A", lease_days=14)
    p2 = plan(doc, EMPTY, fleet=FLEET, import_batch="batch-B", lease_days=14)
    ids1 = [e["event_id"] for e in p1.events]
    ids2 = [e["event_id"] for e in p2.events]
    assert ids1 == ids2, (ids1, ids2)
    assert p1.events[0]["import_batch"] == "batch-A"
    assert p2.events[0]["import_batch"] == "batch-B"


def test_two_different_rows_never_collide_on_event_id():
    doc = {
        "updated": "2026-08-10T00:00:00Z",
        "workstreams": {
            "ws-a": {**_row()["workstreams"]["ws-x"], "id": "ws-a"},
            "ws-b": {**_row()["workstreams"]["ws-x"], "id": "ws-b"},
        },
    }
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    ids = [e["event_id"] for e in p.events]
    assert len(ids) == len(set(ids)), ids


def test_batch_id_is_stable_for_the_same_mtime():
    assert batch_id(1758000000.4) == batch_id(1758000000.9) == "ws-import-1758000000"


def test_every_envelope_carries_origin_legacy_and_the_batch():
    doc = _row(last_progress_ts="2026-08-10T00:00:00Z")
    p = plan(doc, EMPTY, fleet=FLEET, import_batch="ws-import-123", lease_days=14)
    for e in p.events:
        assert e["origin"] == "legacy"
        assert e["import_batch"] == "ws-import-123"
        assert e["emitter"] == "workstream-import"
        assert e["source_ref"] == "workstreams:ws-x"


def test_empty_file_and_empty_existing_produce_an_empty_plan():
    p = plan({"workstreams": {}}, EMPTY, fleet=FLEET, import_batch="b1", lease_days=14)
    assert isinstance(p, ImportPlan)
    assert p.events == [] and p.skipped == [] and p.warnings == []
