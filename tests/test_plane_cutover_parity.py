"""Cutover chunk 2: the operator's parity door and the parity-gap importer.

Fixtures carry the SHAPE of the two live ledgers (every key the capture
showed, in order) with faked identifiers — the r4 rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.legacy_import import apply_import, plan_import
from claudlobby.plane.parity import (
    CAUSE_PRE_GO_LIVE, CAUSE_STAMPED_LOST, CAUSE_UNSTAMPED, DISPATCH, REPORT,
    compare, connect_ro,
)
from claudlobby.plane.queries import TASK_STATUS_SQL

F = "f"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def _root(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    (root / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    return root


def _live_dispatch(root, n, task_id, *, ts, bot="w1"):
    """A dispatch the LIVE door landed: three events, emitter dispatch-task."""
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"assignment_id": asg, "work_item_id": wi,
                     "assignee": f"bot:{F}/{bot}", "assigned_by": f"bot:{F}/mgr",
                     "dispatch_msg_id": msg}},
        {"event_type": "communication", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"msg_id": msg, "sender": f"bot:{F}/mgr", "recipient": f"bot:{F}/{bot}",
                     "message_class": "task_request", "command_type": "task",
                     "work_item_id": wi, "assignment_id": asg, "body": "t"}}])
    return wi, asg, msg


def _drow(ts, task_id, *, manager="mgr", bot="w1", task="do the thing\nmore",
          expected_by=1788000000, plane=("", "", "")):
    msg, wi, asg = plane
    return {"ts": ts, "manager": manager, "bot": bot, "task_id": task_id,
            "workstream": "", "task": task, "dispatched_at": 1787900000,
            "expected_by": expected_by, "claudron_hits": 0, "supersedes": "",
            "open_at_dispatch": 0, "plane_msg_id": msg, "plane_work_item_id": wi,
            "plane_assignment_id": asg}


def _rrow(ts, task_id, status, *, bot="w1", summary="done", pr_url="",
          progress="", anomaly="", plane_msg_id=""):
    return {"ts": ts, "bot": bot, "task_id": task_id, "status": status,
            "summary": summary, "pr_url": pr_url, "issues": "", "skill": "",
            "progress": progress, "artifact": "", "task_anomaly": anomaly,
            "plane_msg_id": plane_msg_id, "plane_work_item_id": "",
            "plane_assignment_id": ""}


def _write(path: Path, rows, *, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(extra_lines))


def _ro(root):
    return connect_ro(db_path(root))


# --- parity ------------------------------------------------------------------

def test_parity_joins_both_keys_and_names_a_cause_for_every_missing_row(tmp_path):
    root = _root(tmp_path)
    wi, asg, msg = _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    _write(dlog, [
        _drow("2026-08-28T15:53:32Z", "t-1-aaaa", plane=(msg, wi, asg)),   # matched by task id
        _drow("2026-08-28T15:53:34Z", "", plane=(msg, "", "")),            # query: matched by msg id
        _drow("2026-08-28T00:47:02Z", "t-0-0000"),                        # before go-live
        _drow("2026-08-29T11:34:31Z", "t-2-bbbb"),                        # after, no ids: door disarmed
        _drow("2026-08-29T12:00:00Z", "t-3-cccc",
              plane=("msg_" + "9" * 32, "wi_" + "9" * 32, "asg_" + "9" * 32)),  # ids minted, no row
    ], extra_lines=["not json\n"])
    conn = _ro(root)
    try:
        p = compare(conn, DISPATCH, dlog)
    finally:
        conn.close()
    assert p.state == "ok" and p.total == 5 and p.matched == 2 and p.malformed == 1
    assert p.go_live == "2026-08-28T15:53:33"
    assert {m.key: m.cause for m in p.missing} == {
        "task:t-0-0000": CAUSE_PRE_GO_LIVE,
        "task:t-2-bbbb": CAUSE_UNSTAMPED,
        "task:t-3-cccc": CAUSE_STAMPED_LOST,
    }
    assert p.causes() == {CAUSE_PRE_GO_LIVE: 1, CAUSE_UNSTAMPED: 1, CAUSE_STAMPED_LOST: 1}
    assert not p.clean


def test_parity_duplicates_are_per_source_ref_kind_and_event(tmp_path):
    """One report legitimately yields two DIFFERENT task events (the live
    completed + supplied_id_not_open pair) — not a duplicate. The same
    event landing twice is."""
    root = _root(tmp_path)
    wi, asg, _ = _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    ref = "report-back:msg_" + "b" * 32
    task = {"event_type": "task", "emitter": "report-back", "fleet": F, "source_ref": ref,
            "payload": {"work_item_id": wi, "assignment_id": asg, "event": "completed"}}
    anomaly = {**task, "payload": {**task["payload"], "event": "supplied_id_not_open"}}
    emit_batch(root, [
        {"event_type": "communication", "emitter": "report-back", "fleet": F, "source_ref": ref,
         "payload": {"msg_id": "msg_" + "b" * 32, "sender": f"bot:{F}/w1",
                     "message_class": "report"}},
        task, anomaly])
    rlog = tmp_path / "report-back.jsonl"
    _write(rlog, [_rrow("2026-09-01T01:32:01Z", "t-1-aaaa", "completed",
                        plane_msg_id="msg_" + "b" * 32)])
    conn = _ro(root)
    try:
        assert compare(conn, REPORT, rlog).clean
        emit_batch(root, [task])          # the same event again, a fresh event_id
        assert compare(conn, REPORT, rlog).duplicates == [f"{ref} task/completed: 2 rows"]
    finally:
        conn.close()


def test_parity_unreachable_is_never_empty(tmp_path):
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    conn = _ro(root)
    try:
        absent = compare(conn, REPORT, tmp_path / "nope" / "report-back.jsonl")
        assert absent.state == "absent" and not absent.reachable and not absent.clean
        empty_path = tmp_path / "report-back.jsonl"
        empty_path.write_text("")
        empty = compare(conn, REPORT, empty_path)
        assert empty.state == "empty" and empty.reachable and empty.clean and empty.total == 0
    finally:
        conn.close()
    with pytest.raises(FileNotFoundError):
        connect_ro(tmp_path / "no" / "plane.db")   # never auto-created


def test_parity_since_window_uses_the_comparable_instant(tmp_path):
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    _write(dlog, [_drow("2026-08-27T00:00:00Z", "t-0-0000"),
                  _drow("2026-08-29T00:00:00Z", "t-2-bbbb")])
    conn = _ro(root)
    try:
        assert [m.key for m in compare(conn, DISPATCH, dlog, since="2026-08-28T00:00:00Z").missing] \
            == ["task:t-2-bbbb"]
        assert [m.key for m in compare(conn, DISPATCH, dlog, since="2026-08-28T00:00:00+00:00").missing] \
            == ["task:t-2-bbbb"]
    finally:
        conn.close()


# --- import ------------------------------------------------------------------

def _ledgers(tmp_path, root):
    """The live-plane fixture every import test starts from: one live
    dispatch (go-live), then a pre-go-live dispatch that the fleet's ledger
    reported, and its report."""
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    rlog = tmp_path / "runtime" / "report-back.jsonl"
    _write(dlog, [_drow("2026-08-28T00:47:02Z", "t-0-0000", task="Old work\ndetails")])
    _write(rlog, [_rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed",
                        pr_url="https://example.invalid/pull/1", progress="100")])
    return dlog, rlog


def test_import_dry_run_plans_four_events_per_dispatch_and_writes_nothing(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        before = conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0]
    finally:
        conn.close()
    assert plan.dispatches == 1 and plan.reports == 1
    kinds = [e["event_type"] for e in plan.events]
    assert kinds == ["work_item", "assignment", "communication", "transmission",
                     "communication", "task"]
    assert all(e["origin"] == "legacy" and e["import_batch"] == plan.batch
               and e["emitter"] == "plane-import" for e in plan.events)
    tx = plan.events[3]["payload"]
    assert tx["state"] == "pane_submitted" and tx["carrier"] == "tmux"
    assert plan.events[5]["payload"]["event"] == "completed"
    assert plan.events[5]["payload"]["pr_url"] == "https://example.invalid/pull/1"
    assert plan.events[5]["payload"]["progress"] == 100
    assert plan.events[1]["payload"]["expected_by"] == "2026-08-29T10:40:00+00:00"  # epoch 1788000000
    conn = _ro(root)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == before
    finally:
        conn.close()


def test_import_apply_lands_a_status_bearing_row_and_reruns_as_duplicates(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    assert apply_import(root, plan) == {"committed": 6, "duplicate": 0, "spooled": 0}
    asg = plan.events[1]["payload"]["assignment_id"]
    conn = connect(db_path(root))
    try:
        status = dict(conn.execute(TASK_STATUS_SQL).fetchall())[asg]
        assert status == "completed"          # NOT created_not_sent: the four-event rule
        origin, batch = conn.execute(
            "SELECT origin, import_batch FROM work_items WHERE event_id = ?",
            (plan.events[0]["event_id"],)).fetchone()
        assert origin == "legacy" and batch == plan.batch
    finally:
        conn.close()
    conn = _ro(root)
    try:
        after = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        assert compare(conn, DISPATCH, dlog).clean and compare(conn, REPORT, rlog).clean
    finally:
        conn.close()
    assert after.events == [] and after.dispatches == 0 and after.reports == 0
    # Replaying the ORIGINAL plan lands nothing new: ids are content-hashed.
    assert apply_import(root, plan) == {"committed": 0, "duplicate": 6, "spooled": 0}


def test_import_attributes_by_the_report_ledger_only(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    _write(dlog, [
        _drow("2026-08-28T00:47:02Z", "t-0-0000", task="Old work"),   # reported here: imports
        _drow("2026-08-28T00:48:02Z", "t-9-9999", bot="elsewhere"),  # never reported here
        _drow("2026-08-28T00:49:02Z", ""),                            # a query: no task id by design
    ])
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    assert plan.dispatches == 1
    assert plan.unattributed == ["task:t-9-9999", "sha:" + plan.unattributed[1][4:]]
    assert plan.unattributed[1].startswith("sha:")


def test_import_discloses_orphan_reports_and_unknown_status(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    _write(rlog, [
        _rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed"),
        _rrow("2026-08-28T03:01:00Z", "t-7-7777", "completed"),   # dispatch row long rotated
        _rrow("2026-08-28T03:02:00Z", "t-0-0000", "sideways"),    # no plane vocabulary
        _rrow("2026-08-28T03:03:00Z", "", "completed"),           # id-less report
    ])
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    assert plan.reports == 1
    assert len(plan.orphan_reports) == 2 and len(plan.unknown_status) == 1


def test_import_links_a_report_to_a_dispatch_already_in_the_plane(tmp_path):
    root = _root(tmp_path)
    wi, asg, msg = _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    rlog = tmp_path / "runtime" / "report-back.jsonl"
    _write(dlog, [_drow("2026-08-28T15:53:32Z", "t-1-aaaa", plane=(msg, wi, asg))])
    _write(rlog, [_rrow("2026-08-28T16:00:00Z", "t-1-aaaa", "blocked",
                        anomaly="supplied-id-not-open")])
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    assert plan.dispatches == 0 and plan.reports == 1
    events = [e["payload"]["event"] for e in plan.events if e["event_type"] == "task"]
    assert events == ["returned_blocked", "supplied_id_not_open"]
    assert all(e["payload"]["assignment_id"] == asg for e in plan.events)
    assert plan.events[0]["source_ref"].startswith("report-back:sha:")


def test_import_keeps_a_stamped_rows_own_ids(tmp_path):
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    stamped = ("msg_" + "9" * 32, "wi_" + "9" * 32, "asg_" + "9" * 32)
    dlog = tmp_path / "dispatch-log.jsonl"
    rlog = tmp_path / "runtime" / "report-back.jsonl"
    _write(dlog, [_drow("2026-08-29T12:00:00Z", "t-3-cccc", plane=stamped)])
    _write(rlog, [_rrow("2026-08-29T13:00:00Z", "t-3-cccc", "progress", progress="40")])
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    assert plan.dispatch.missing[0].cause == CAUSE_STAMPED_LOST
    assert plan.events[1]["payload"]["assignment_id"] == stamped[2]
    assert plan.events[2]["payload"]["msg_id"] == stamped[0]
    assert plan.events[5]["payload"]["progress"] == 40


def test_import_refuses_when_a_ledger_is_unreachable(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    conn = _ro(root)
    try:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog,
                           report_path=tmp_path / "gone" / "report-back.jsonl", now=NOW)
    finally:
        conn.close()
    assert not plan.reachable and plan.events == []


def test_import_ids_survive_ledger_rotation(tmp_path):
    """Content, never position: rotation rewrites the file, so a row's line
    number changes while its ids must not (a position hash would land the
    same row twice across two runs). Mutation-pinned."""
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    rlog = tmp_path / "runtime" / "report-back.jsonl"
    older = _drow("2026-08-27T00:00:00Z", "t-0-0000", task="older")
    newer = _drow("2026-08-28T00:47:02Z", "t-0-1111", task="newer")
    rep_old = _rrow("2026-08-27T01:00:00Z", "t-0-0000", "completed")
    rep_new = _rrow("2026-08-28T03:00:00Z", "t-0-1111", "completed")
    _write(dlog, [older, newer])
    _write(rlog, [rep_old, rep_new])
    conn = _ro(root)
    try:
        first = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        _write(dlog, [newer])          # the 7-day rotation dropped the older row
        _write(rlog, [rep_new])
        second = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    finally:
        conn.close()
    def by_ref(plan):
        out: dict = {}
        for e in plan.events:
            out.setdefault(e["source_ref"], set()).add(
                (e["event_type"], e["event_id"], e["payload"].get("work_item_id"),
                 e["payload"].get("assignment_id"), e["payload"].get("msg_id")))
        return out
    first_refs, second_refs = by_ref(first), by_ref(second)
    assert second_refs and "dispatch-log:t-0-0000" not in second_refs
    for ref, rows in second_refs.items():          # every surviving row: identical ids
        assert first_refs[ref] == rows, ref        # (a position hash re-mints or COLLIDES)
