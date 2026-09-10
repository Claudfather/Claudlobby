"""Chunk T — the stale in-flight task attention arm.

Present-tense attention for an OPEN assignment a bot is HOLDING but not moving
on: aging, no progress, no (passed) deadline — SUPPRESSED while the assignee is
presence=working (the crux: a bot deep in a long task is not stuck). Tiered by
age (amber → red). Rides the existing attention plumbing (ATTENTION_ARMS →
ATTENTION_ARMS_SQL → the rail + the header's "need you" count); the
presence-suppression heartbeat join is the one new input.

Chunk U adds the sibling `blocked_waiting` arm at the tail of this file: a bot
that REPORTED it is blocked (newest task event `blocked_waiting`) leads a stale
row, which excludes it, so an aged block reads `blocked_waiting`, never both.

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
              idless: bool = False,
              blocked_age_h: float | None = None,
              resumed_age_h: float | None = None,
              at_override: str | None = None,
              progress_at_override: str | None = None,
              expected_by: str | None = None, terminal: str | None = None) -> str:
    """One dispatched assignment stamped `dispatch_age_h` hours ago, with the
    knobs the pins turn: a delivery (pane_submitted) transmission, a heartbeat
    for the assignee's instance, a linked `progress` task event and/or a
    `report_status` progress marker, a deadline, a terminal report."""
    _full_capture(root)
    stem = (h * 32)[:32]
    at = at_override or _ago(hours=dispatch_age_h)
    # the envelope source_ref: an id-less dispatch is `dispatch-log:sha:<key>`,
    # an id'd one `dispatch-log:<task_id>` (dispatch-task.sh's two shapes). The
    # id-less shape is what the actor `report_status` marker resets (branch 2).
    ref = ("dispatch-log:sha:" if idless else "dispatch-log:t-") + stem
    asg = {"assignment_id": "asg_" + stem, "work_item_id": "wi_" + stem,
           "assignee": worker, "assigned_by": MGR, "dispatch_msg_id": "msg_" + stem}
    if expected_by:
        asg["expected_by"] = expected_by
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": "f", "occurred_at": at,
         "payload": {"work_item_id": "wi_" + stem, "title": f"task {h}",
                     "created_by": MGR}},
        {"event_type": "assignment", "emitter": "t", "fleet": "f",
         "occurred_at": at, "source_ref": ref, "payload": asg},
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
            "occurred_at": progress_at_override or _ago(hours=progress_age_h),
            "payload": {"event": "progress", "work_item_id": "wi_" + stem,
                        "assignment_id": "asg_" + stem, "actor": worker}}])
    if blocked_age_h is not None:
        # the bot reports it is blocked and waiting (a non-terminal task event) —
        # the assignment stays open, its newest event becomes `blocked_waiting`
        emit_batch(root, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "occurred_at": _ago(hours=blocked_age_h),
            "payload": {"event": "blocked_waiting", "work_item_id": "wi_" + stem,
                        "assignment_id": "asg_" + stem, "actor": worker}}])
    if resumed_age_h is not None:
        # the block cleared: a later `resumed` makes the newest event no longer
        # `blocked_waiting`, so the arm falls away
        emit_batch(root, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "occurred_at": _ago(hours=resumed_age_h),
            "payload": {"event": "resumed", "work_item_id": "wi_" + stem,
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
    # the id-less path: for an ID-LESS assignment (source_ref `dispatch-log:sha:`)
    # an actor `report_status` progress marker 1h ago (which resolves no specific
    # assignment) resets the clock — because that is the only fresh-activity
    # signal an id-less task has.
    asg = _dispatch(tmp_path, "f", dispatch_age_h=8, heartbeat="IDLE",
                    idless=True, marker_progress_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention"] is False


def test_an_idd_task_is_not_reset_by_a_siblings_idless_marker(tmp_path):
    # fold F1: the actor marker resets ONLY id-less tasks. An ID'D assignment
    # carries its own linked `progress` event (branch 1); a bare actor marker is
    # a DIFFERENT task's id-less report and must not silence this one. Before the
    # fold the marker matched on the actor alone and masked a genuinely stuck
    # id'd task — the whole point of stale_task. Here: an id'd task dispatched 8h
    # ago, idle, with an actor marker 1h ago → STILL stale.
    asg = _dispatch(tmp_path, "e", dispatch_age_h=8, heartbeat="IDLE",
                    idless=False, marker_progress_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention"] is True
    assert r["attention_reason"] == ["stale_task"]


def test_since_uses_the_epoch_latest_instant_under_mixed_offsets(tmp_path):
    # fold F2: the display "since" (attention_since / the tier clock) is the
    # instant whose EPOCH is greatest, not the lexically-greatest string. Every
    # estate door stamps UTC so this cannot fire today, but the envelope accepts
    # any offset and ingest stores it RAW (isoformat, no to-UTC), so a non-UTC
    # emitter would trip a lexical MAX. Construct the disagreement: the dispatch
    # is real-EARLIER (NOW-8h) but written +09:00 so its STRING sorts LATER; the
    # linked progress is real-LATER (NOW-7h) at +00:00. The arm must date from
    # the progress (epoch-latest), never the dispatch (lexical-latest).
    disp = (NOW - timedelta(hours=8)).astimezone(timezone(timedelta(hours=9)))
    prog = NOW - timedelta(hours=7)  # +00:00
    # self-guard: the two orderings genuinely disagree for this NOW
    assert disp.isoformat() > prog.isoformat()   # lexical: the dispatch wins
    assert disp < prog                           # epoch:   the progress wins
    asg = _dispatch(tmp_path, "d", dispatch_age_h=8, heartbeat="IDLE",
                    progress_age_h=7, at_override=disp.isoformat(),
                    progress_at_override=prog.isoformat())
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["stale_task"]
    # the arm dates from the epoch-latest activity (the progress), NOT the
    # lexically-latest string (the dispatch). Old lexical MAX returned `disp`.
    assert r["attention_since"] == prog.isoformat()
    assert r["attention_since"] != disp.isoformat()


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


# --- the blocked_waiting arm (chunk U) ----------------------------------------

def test_blocked_waiting_fires_for_an_open_block(tmp_path):
    # the bot reported `blocked_waiting` 1h ago and is idle: it needs the
    # operator, and the newest task event is the block
    asg = _dispatch(tmp_path, "b1", dispatch_age_h=2, heartbeat="IDLE",
                    blocked_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention"] is True
    assert r["attention_reason"] == ["blocked_waiting"]
    # dated from the block itself (the newest task event)
    assert r["attention_since"] == _ago(hours=1)


def test_an_aged_block_reads_blocked_not_stale(tmp_path):
    # THE exclusion pin. An old dispatch (8h), idle, now `blocked_waiting`: a
    # block is not "progress", so without the _STALE_TASK exclusion this would
    # ALSO trip stale_task and double-raise `["blocked_waiting","stale_task"]`.
    # The explicit block must lead alone.
    asg = _dispatch(tmp_path, "b2", dispatch_age_h=8, heartbeat="IDLE",
                    blocked_age_h=1)
    r = _row(tmp_path, asg)
    assert r["attention_reason"] == ["blocked_waiting"]
    assert "stale_task" not in r["attention_reason"]


def test_a_resumed_block_falls_away(tmp_path):
    # `blocked_waiting` then a later `resumed`: the newest event is no longer
    # the block, and the task is moving again recently — no arm fires
    asg = _dispatch(tmp_path, "b3", dispatch_age_h=3, heartbeat="IDLE",
                    blocked_age_h=2, resumed_age_h=0.5)
    r = _row(tmp_path, asg)
    assert r["attention"] is False


def test_a_completed_block_leaves_the_queue(tmp_path):
    # `blocked_waiting` then a terminal `completed`: the assignment closes, so
    # it is out of the non-terminal queue entirely
    asg = _dispatch(tmp_path, "b4", dispatch_age_h=3, heartbeat="IDLE",
                    blocked_age_h=2, terminal="completed")
    r = _row(tmp_path, asg)
    assert r["attention"] is False
