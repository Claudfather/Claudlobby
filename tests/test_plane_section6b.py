"""§6b kernel obligations (PR-B): carrier-appropriate activation, missing
producers fail toward EMPTY, and the two vocabulary intakes.

Provenance: the #1341 fleet review's domain-fit findings, dispositioned in
documentation/plans/2026-08-24-observable-plane-phase2-ingest.md §6b."""

from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.ids import mint_assignment_id, mint_msg_id, mint_work_item_id
from claudlobby.plane.queries import (
    ATTENTION_SQL,
    RECONCILIATION_SQL,
    TASK_STATUS_SQL,
    attention_params,
)

FUTURE = "2027-01-01T00:00:00+00:00"
PAST = "2020-01-01T00:00:00+00:00"
CUTOFF = "2026-06-01"


def _seed(root: Path, *, expected_by: str | None = FUTURE,
          tx_events: tuple = ()) -> str:
    aid = mint_assignment_id()
    wi = mint_work_item_id()
    msg = mint_msg_id()
    batch = [
        {"event_type": "work_item", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": "bot:example-fleet/mgr"}},
        {"event_type": "assignment", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"assignment_id": aid, "work_item_id": wi,
                     "assignee": "bot:example-fleet/w1",
                     "assigned_by": "bot:example-fleet/mgr",
                     **({"expected_by": expected_by} if expected_by else {}),
                     "dispatch_msg_id": msg}},
        {"event_type": "communication", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"msg_id": msg, "sender": "bot:example-fleet/mgr",
                     "recipient": "bot:example-fleet/w1",
                     "message_class": "task_request", "command_type": "task",
                     "privacy": "full"}},
    ]
    for state, attempt_no in tx_events:
        batch.append(
            {"event_type": "transmission", "emitter": "6b",
             "fleet": "example-fleet",
             "payload": {"msg_id": msg, "attempt_no": attempt_no,
                         "carrier": "tmux", "destination": "w1",
                         "state": state}})
    emit_batch(root, batch)
    return aid


def _q(root: Path, sql: str, params: tuple = ()):
    conn = connect(db_path(root))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_zero_producer_reads_quiet_never_all_alarm(tmp_path: Path):
    """§6b #2 — the headline inversion: with NO transmission producer, the
    old attention predicate returned every non-terminal assignment forever
    and reconciliation pinned at 100%. A missing producer must read EMPTY."""
    a1 = _seed(tmp_path, tx_events=())
    a2 = _seed(tmp_path, tx_events=())
    attention = [r[0] for r in _q(tmp_path, ATTENTION_SQL, attention_params(CUTOFF))]
    assert attention == [], (
        f"zero transmission rows must not alarm: {attention}")
    assert _q(tmp_path, RECONCILIATION_SQL)[0][0] == 0
    statuses = {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}
    assert statuses[a1] == statuses[a2] == "created_not_sent", (
        "contract exists != active: no evidence means not-yet-sent, not open")


def test_overdue_overlay_still_fires_without_a_producer(tmp_path: Path):
    """The expected_by overlay is assignment-row evidence, not transmission
    evidence — it must survive the empty-not-everything rewrite."""
    late = _seed(tmp_path, expected_by=PAST, tx_events=())
    ontime = _seed(tmp_path, expected_by=FUTURE, tx_events=())
    attention = [r[0] for r in _q(tmp_path, ATTENTION_SQL, attention_params(CUTOFF))]
    assert late in attention and ontime not in attention


def test_submission_is_activation_for_tmux(tmp_path: Path):
    """§6b #1: pane_submitted occupies the open rung — submission is the
    strongest fact the carrier yields — and such a dispatch is NOT in the
    attention set."""
    aid = _seed(tmp_path, tx_events=(("send_attempted", 1),
                                     ("pane_submitted", 1)))
    assert {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}[aid] == "open"
    assert aid not in [r[0] for r in _q(tmp_path, ATTENTION_SQL, attention_params(CUTOFF))]


def test_trouble_evidence_alarms_failed_and_queued(tmp_path: Path):
    """Attention fires on EVIDENCE of trouble: a transmission trail exists
    but nothing reached activation — a failed send, or a payload parked
    behind a busy turn (carrier_queued, §6b #7)."""
    failed = _seed(tmp_path, tx_events=(("send_attempted", 1), ("failed", 1)))
    queued = _seed(tmp_path, tx_events=(("carrier_queued", 1),))
    attention = [r[0] for r in _q(tmp_path, ATTENTION_SQL, attention_params(CUTOFF))]
    assert failed in attention and queued in attention


def test_carrier_queued_is_pending_not_open(tmp_path: Path):
    """The new token ingests (manifest + DDL CHECK agree) and reads as an
    outstanding attempt: accepted-but-parked is not consumed."""
    aid = _seed(tmp_path, tx_events=(("carrier_queued", 1),))
    assert {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}[aid] == "pending_unacknowledged"


def test_queued_then_submitted_activates(tmp_path: Path):
    """The busy turn ends, the payload submits — activation follows the
    evidence."""
    aid = _seed(tmp_path, tx_events=(("carrier_queued", 1),
                                     ("pane_submitted", 1)))
    assert {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}[aid] == "open"
    assert aid not in [r[0] for r in _q(tmp_path, ATTENTION_SQL, attention_params(CUTOFF))]


def test_supplied_id_not_open_ingests_as_task_fact(tmp_path: Path):
    """§6b #6: the join anomaly our tooling already records deliberately
    becomes a first-class token instead of degrading silently."""
    wi = mint_work_item_id()
    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": "bot:example-fleet/mgr"}},
        {"event_type": "task", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "event": "supplied_id_not_open",
                     "summary": "worker reported t-123 but it was not open"}},
    ])
    conn = connect(db_path(tmp_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task'"
        " AND event='supplied_id_not_open'").fetchone()[0]
    conn.close()
    assert n == 1


def test_ack_still_tightens_and_terminal_still_dominates(tmp_path: Path):
    """The tightening case and the monotone reducer survive the rewrite."""
    aid = _seed(tmp_path, tx_events=(("pane_submitted", 1),
                                     ("recipient_acknowledged", 1)))
    assert {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}[aid] == "open"
    conn = connect(db_path(tmp_path))
    wi = conn.execute(
        "SELECT work_item_id FROM assignments WHERE assignment_id = ?",
        (aid,)).fetchone()[0]
    conn.close()
    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "assignment_id": aid,
                     "event": "completed"}},
        {"event_type": "task", "emitter": "6b", "fleet": "example-fleet",
         "payload": {"work_item_id": wi, "assignment_id": aid,
                     "event": "progress", "progress": 5}},
    ])
    assert {r[0]: r[1] for r in _q(tmp_path, TASK_STATUS_SQL)}[aid] == "completed"