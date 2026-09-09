"""Chunk T — the stale in-flight task attention arm.

Present-tense attention for an OPEN assignment a bot is HOLDING but not moving
on: aging, no progress, no (passed) deadline — SUPPRESSED while the assignee is
presence=working (the crux: a bot deep in a long task is not stuck). Tiered by
age (amber → red). Rides the existing attention plumbing (ATTENTION_ARMS →
ATTENTION_ARMS_SQL → the rail + the header's "need you" count); the
presence-suppression heartbeat join is the one new input.

Fixtures are seeded through the real emit spine (`emit_batch`) in real shapes;
ids are faked hex. Timestamps are relative to real `now` (the view's clock).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.view import create_app  # noqa: E402

NOW = datetime.now(timezone.utc)
MGR = "bot:f/mgr"


def _ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def _full_capture(root: Path) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _dispatch(root: Path, h: str, *, dispatch_age_h: float, delivered: bool = True,
              worker: str = "bot:f/worker", heartbeat: str | None = None,
              progress_age_h: float | None = None,
              marker_progress_age_h: float | None = None,
              expected_by: str | None = None, terminal: str | None = None) -> str:
    """One dispatched assignment stamped `dispatch_age_h` hours ago, with the
    knobs the pins turn: a delivery (pane_submitted) transmission, a heartbeat
    for the assignee's instance, a linked `progress` task event and/or a
    `report_status` progress marker, a deadline, a terminal report."""
    _full_capture(root)
    stem = (h * 32)[:32]
    at = _ago(hours=dispatch_age_h)
    asg = {"assignment_id": "asg_" + stem, "work_item_id": "wi_" + stem,
           "assignee": worker, "assigned_by": MGR, "dispatch_msg_id": "msg_" + stem}
    if expected_by:
        asg["expected_by"] = expected_by
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": "f", "occurred_at": at,
         "payload": {"work_item_id": "wi_" + stem, "title": f"task {h}",
                     "created_by": MGR}},
        {"event_type": "assignment", "emitter": "t", "fleet": "f",
         "occurred_at": at, "payload": asg},
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "occurred_at": at,
         "payload": {"msg_id": "msg_" + stem, "sender": MGR, "recipient": worker,
                     "message_class": "task_request", "command_type": "task",
                     "body": f"go {h}"}}])
    if delivered:
        emit_batch(root, [{
            "event_type": "transmission", "emitter": "t", "fleet": "f",
            "occurred_at": at,
            "payload": {"msg_id": "msg_" + stem, "attempt_no": 1, "carrier": "tmux",
                        "destination": "worker", "state": "pane_submitted"}}])
    if heartbeat is not None:
        # the assignee's INSTANCE heartbeat (keepalive's shape) — ingested_at is
        # ~now, so a BUSY reading is FRESH (working) unless a test backdates it
        emit_batch(root, [{
            "event_type": "metric_sample", "emitter": "keepalive", "fleet": "f",
            "payload": {"subject_kind": "bot_instance", "subject": worker,
                        "metric": "bot.heartbeat",
                        "value": {"state": heartbeat, "marker_age_s": 3}}}])
    if progress_age_h is not None:
        emit_batch(root, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "occurred_at": _ago(hours=progress_age_h),
            "payload": {"event": "progress", "work_item_id": "wi_" + stem,
                        "assignment_id": "asg_" + stem, "actor": worker}}])
    if marker_progress_age_h is not None:
        emit_batch(root, [{
            "event_type": "system", "emitter": "report-back", "fleet": "f",
            "occurred_at": _ago(hours=marker_progress_age_h),
            "payload": {"event": "report_status", "subject_kind": "actor",
                        "subject": worker,
                        "data": {"status": "progress", "msg_id": "rmsg_" + stem}}}])
    if terminal:
        emit_batch(root, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "occurred_at": _ago(hours=0),
            "payload": {"event": terminal, "work_item_id": "wi_" + stem,
                        "assignment_id": "asg_" + stem, "actor": worker}}])
    return "asg_" + stem


def _row(root: Path, asg: str) -> dict:
    rows = {r["assignment_id"]: r for r in
            TestClient(create_app(root)).get("/api/tasks").json()["data"]["assignments"]}
    return rows[asg]


def _backdate_heartbeat_ingest(root: Path, seconds: int) -> None:
    """Push the (single) heartbeat sample's INGEST clock back — the freshness
    axis presence reads, not the producer occurred_at (test_plane_presence's
    idiom). Makes a BUSY recording STALE without touching its state."""
    db = sqlite3.connect(root / "state" / "plane" / "plane.db")
    old = (NOW - timedelta(seconds=seconds)).isoformat()
    db.execute("UPDATE ingest_ledger SET ingested_at=? WHERE ingest_seq="
               "(SELECT ingest_seq FROM metric_samples"
               " WHERE metric='bot.heartbeat' LIMIT 1)", (old,))
    db.commit()
    db.close()


# --- the arm fires ------------------------------------------------------------

def test_fires_for_aged_open_no_progress_with_idle_assignee(tmp_path):
    asg = _dispatch(tmp_path, "a", dispatch_age_h=8, heartbeat="IDLE")
    r = _row(tmp_path, asg)
    assert r["attention"] is True
    assert r["attention_reason"] == ["stale_task"]
    assert r["stale_tier"] == "amber"
    # dated from the last activity (the dispatch, no progress since)
    assert r["attention_since"] == r["occurred_at"]


def test_fires_for_a_down_assignee_with_no_heartbeat(tmp_path):
    # no heartbeat at all — presence unknown/down; a bot holding an aged open
    # task with no sign of working IS the signal
    asg = _dispatch(tmp_path, "b", dispatch_age_h=8, heartbeat=None)
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["stale_task"]


def test_a_stale_busy_recording_does_not_suppress(tmp_path):
    # BUSY, but the recorded half went quiet past the freshness horizon:
    # presence=stale, not working — the arm fires (a working verdict gone
    # silent is not a working verdict)
    asg = _dispatch(tmp_path, "c", dispatch_age_h=8, heartbeat="BUSY")
    _backdate_heartbeat_ingest(tmp_path, 3600)
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["stale_task"]


# --- the crux: suppressed while working ---------------------------------------

def test_suppressed_while_assignee_is_working(tmp_path):
    # a FRESH BUSY heartbeat = presence working — a bot deep in a long task is
    # not stuck, even on an old task: the arm raises NOTHING
    asg = _dispatch(tmp_path, "d", dispatch_age_h=8, heartbeat="BUSY")
    r = _row(tmp_path, asg)
    assert r["attention"] is False
    assert r["attention_reason"] == []
    assert r["stale_tier"] is None


# --- progress resets the clock ------------------------------------------------

def test_a_recent_progress_report_resets_the_clock(tmp_path):
    # dispatched 8h ago but a linked `progress` task event 1h ago: last activity
    # is 1h, inside amber — not stale
    asg = _dispatch(tmp_path, "e", dispatch_age_h=8, heartbeat="IDLE",
                    progress_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention"] is False


def test_an_idless_progress_marker_resets_the_clock(tmp_path):
    # the id-less path: a `report_status` progress marker on the actor 1h ago
    # (an id-less progress report resolves no assignment) also resets it
    asg = _dispatch(tmp_path, "f", dispatch_age_h=8, heartbeat="IDLE",
                    marker_progress_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention"] is False


def test_old_progress_still_leaves_it_stale(tmp_path):
    # progress, but 7h ago — still older than amber, so the task is stale and
    # the arm dates from that later activity, not the dispatch
    asg = _dispatch(tmp_path, "1a", dispatch_age_h=9, heartbeat="IDLE",
                    progress_age_h=7)
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["stale_task"]


# --- a terminal report clears it ----------------------------------------------

def test_a_terminal_report_clears_it(tmp_path):
    asg = _dispatch(tmp_path, "2a", dispatch_age_h=8, heartbeat="IDLE",
                    terminal="completed")
    r = _row(tmp_path, asg)
    assert r["attention"] is False
    assert r["status"] == "completed"


# --- overdue leads (no double-raise) ------------------------------------------

def test_overdue_leads_over_stale_task_on_a_row_that_is_both(tmp_path):
    # aged AND past a deadline: overdue is the broken promise and LEADS; stale
    # is excluded, so the row never double-raises
    asg = _dispatch(tmp_path, "3a", dispatch_age_h=8, heartbeat="IDLE",
                    expected_by=_ago(hours=2))
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["overdue"]
    assert r["stale_tier"] is None


# --- delivery is required (a never-delivered task is not "held") ---------------

def test_a_never_delivered_task_is_not_stale(tmp_path):
    # aged, but NO transmission ever went out: the bot cannot be sitting on a
    # task a send never reached it — silence, not this alarm
    asg = _dispatch(tmp_path, "4a", dispatch_age_h=8, delivered=False,
                    heartbeat="IDLE")
    r = _row(tmp_path, asg)
    assert r["attention"] is False


def test_a_recent_task_is_not_stale(tmp_path):
    # inside the amber window — quiet
    asg = _dispatch(tmp_path, "5a", dispatch_age_h=1, heartbeat="IDLE")
    r = _row(tmp_path, asg)
    assert r["attention"] is False


# --- the tier is amber then red by age ----------------------------------------

def test_tier_is_amber_below_the_red_boundary_and_red_past_it(tmp_path):
    amber = _dispatch(tmp_path, "6a", dispatch_age_h=8, heartbeat="IDLE")
    red = _dispatch(tmp_path, "7a", dispatch_age_h=96, heartbeat="IDLE",
                    worker="bot:f/older")
    rows = {r["assignment_id"]: r for r in
            TestClient(create_app(tmp_path)).get("/api/tasks")
            .json()["data"]["assignments"]}
    assert rows[amber]["stale_tier"] == "amber"
    assert rows[red]["stale_tier"] == "red"
    assert rows[red]["attention_reason"] == ["stale_task"]


# --- the header count + the rail include the amber AND red rows ---------------

def test_header_need_you_count_and_rail_include_amber_and_red(tmp_path):
    _dispatch(tmp_path, "8a", dispatch_age_h=8, heartbeat="IDLE")          # amber
    _dispatch(tmp_path, "9a", dispatch_age_h=96, heartbeat="IDLE",
              worker="bot:f/older")                                        # red
    _dispatch(tmp_path, "ba", dispatch_age_h=8, heartbeat="BUSY",
              worker="bot:f/busy")                                         # suppressed
    client = TestClient(create_app(tmp_path))
    # the rail / board: two attention rows, both stale_task, one of each tier
    rows = client.get("/api/tasks").json()["data"]["assignments"]
    stale = [r for r in rows if "stale_task" in r["attention_reason"]]
    assert {r["stale_tier"] for r in stale} == {"amber", "red"}
    # the header's "need you" total is the SERVER's attention count — it counts
    # the two stale rows and not the working (suppressed) one
    ov = client.get("/api/overview").json()["data"]
    assert ov["totals"]["attention"] == 2
    assert ov["totals"]["overdue"] == 0


# --- source_state: an unreachable plane fires no false stale_task -------------

def test_an_unreachable_plane_fires_no_false_stale_task(tmp_path):
    # positive control: readable → the aged task fires
    asg = _dispatch(tmp_path, "ca", dispatch_age_h=8, heartbeat="IDLE")
    assert _row(tmp_path, asg)["attention_reason"] == ["stale_task"]
    # now the plane cannot be opened: the attention read degrades to a typed
    # state, it does NOT fabricate a stale_task from the missing signal
    db = tmp_path / "state" / "plane" / "plane.db"
    db.chmod(0)
    try:
        body = TestClient(create_app(tmp_path)).get("/api/tasks").json()
    finally:
        db.chmod(0o600)
    assert body["state"] != "ok"
    assert not body.get("data", {}).get("assignments")
