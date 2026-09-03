"""Cutover chunk 2: the operator's parity door and the parity-gap importer.

Fixtures carry the SHAPE of the two live ledgers (every key the capture
showed, in order) with faked identifiers — the r4 rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, connect_ro, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.legacy_import import apply_import, plan_import
from claudlobby.plane.parity import (
    CAUSE_PRE_GO_LIVE, CAUSE_STAMPED_LOST, CAUSE_UNSTAMPED, DISPATCH, REPORT, compare,
)
from claudlobby.plane.queries import TASK_STATUS_SQL
from tests.plane_fixtures import plane_root as _root, ro as _ro

F = "f"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


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
            "plane_msg_id": plane_msg_id}


def _write(path: Path, rows, *, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "".join(extra_lines))


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
    with _ro(root) as conn:
        p = compare(conn, DISPATCH, dlog)
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
    with _ro(root) as conn:
        assert compare(conn, REPORT, rlog).clean
        emit_batch(root, [task])          # the same event again, a fresh event_id
        assert compare(conn, REPORT, rlog).duplicates == [f"{ref} task/completed: 2 rows"]


def test_parity_unreachable_is_never_empty(tmp_path):
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    with _ro(root) as conn:
        absent = compare(conn, REPORT, tmp_path / "nope" / "report-back.jsonl")
        assert absent.state == "absent" and not absent.reachable and not absent.clean
        empty_path = tmp_path / "report-back.jsonl"
        empty_path.write_text("")
        empty = compare(conn, REPORT, empty_path)
        assert empty.state == "empty" and empty.reachable and empty.clean and empty.total == 0
    with pytest.raises(FileNotFoundError):
        connect_ro(tmp_path / "no" / "plane.db")   # never auto-created


def test_parity_since_window_uses_the_comparable_instant(tmp_path):
    root = _root(tmp_path)
    _live_dispatch(root, "a", "t-1-aaaa", ts="2026-08-28T15:53:33Z")
    dlog = tmp_path / "dispatch-log.jsonl"
    _write(dlog, [_drow("2026-08-27T00:00:00Z", "t-0-0000"),
                  _drow("2026-08-29T00:00:00Z", "t-2-bbbb")])
    with _ro(root) as conn:
        assert [m.key for m in compare(conn, DISPATCH, dlog, since="2026-08-28T00:00:00Z").missing] \
            == ["task:t-2-bbbb"]
        assert [m.key for m in compare(conn, DISPATCH, dlog, since="2026-08-28T00:00:00+00:00").missing] \
            == ["task:t-2-bbbb"]


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
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        before = conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0]
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
    with _ro(root) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == before


def test_import_apply_lands_a_status_bearing_row_and_reruns_as_duplicates(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
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
    with _ro(root) as conn:
        after = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        assert compare(conn, DISPATCH, dlog).clean and compare(conn, REPORT, rlog).clean
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
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
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
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
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
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
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
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    assert plan.dispatch.missing[0].cause == CAUSE_STAMPED_LOST
    assert plan.events[1]["payload"]["assignment_id"] == stamped[2]
    assert plan.events[2]["payload"]["msg_id"] == stamped[0]
    assert plan.events[5]["payload"]["progress"] == 40


def test_import_refuses_when_a_ledger_is_unreachable(tmp_path):
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog,
                           report_path=tmp_path / "gone" / "report-back.jsonl", now=NOW)
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
    with _ro(root) as conn:
        first = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
        _write(dlog, [newer])          # the 7-day rotation dropped the older row
        _write(rlog, [rep_new])
        second = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
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


def test_import_a_unit_the_contracts_refuse_is_skipped_and_the_rest_lands(tmp_path):
    """emit_batch validates the WHOLE batch before its one transaction, so an
    unvalidated unit would take every good row down with it. A stamped row
    whose plane id is malformed is refused per unit, counted, and disclosed."""
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    _write(dlog, [
        _drow("2026-08-28T00:47:02Z", "t-0-0000", task="Old work"),
        _drow("2026-08-28T00:48:02Z", "t-5-5555", plane=("msg_short", "", "")),   # bad msg id
    ])
    _write(rlog, [_rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed"),
                  _rrow("2026-08-28T03:01:00Z", "t-5-5555", "completed")])
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW,
                           capture={"*": "full"})
    assert plan.dispatches == 1 and len(plan.invalid) == 1 and plan.invalid[0].startswith("task:t-5-5555")
    assert plan.orphan_reports == [] or plan.reports == 1   # its report has no assignment to hang on
    assert apply_import(root, plan)["committed"] == len(plan.events) > 0


def test_import_resolves_the_manager_alias_through_the_registry(tmp_path):
    """Exactly one fleet knows the manager's name -> that alias, not assumed;
    two fleets know it -> assumed to be F and COUNTED, never picked. (The
    live fixture's own manager `mgr` is known in F, so a fresh name is used.)"""
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    _write(dlog, [_drow("2026-08-28T00:47:02Z", "t-0-0000", manager="boss", task="Old work")])
    emit_batch(root, [{"event_type": "work_item", "emitter": "t", "fleet": "g",
                       "payload": {"work_item_id": f"wi_{'e':0>32}", "title": "t",
                                   "created_by": "bot:g/boss"}}])     # boss is known in fleet g only
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    assert plan.events[0]["payload"]["created_by"] == "bot:g/boss"
    assert plan.assumed_manager_fleet == 0
    emit_batch(root, [{"event_type": "work_item", "emitter": "t", "fleet": "h",
                       "payload": {"work_item_id": f"wi_{'d':0>32}", "title": "t",
                                   "created_by": "bot:h/boss"}}])     # now two fleets know it
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    assert plan.events[0]["payload"]["created_by"] == f"bot:{F}/boss"
    assert plan.assumed_manager_fleet == 1


def test_import_an_oversized_field_refuses_only_its_own_unit(tmp_path):
    """task.summary caps at 4096 bytes and the contract REJECTS (never
    truncates). Measured before the per-unit validation: one such row raised
    out of emit_batch and zero of the other units landed."""
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    _write(dlog, [_drow("2026-08-28T00:47:02Z", "t-0-0000", task="ok"),
                  _drow("2026-08-28T00:48:02Z", "t-0-1111", task="ok2")])
    _write(rlog, [_rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed"),
                  _rrow("2026-08-28T03:01:00Z", "t-0-1111", "completed", summary="x" * 5000)])
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW,
                           capture={"*": "full"})
    assert plan.dispatches == 2 and plan.reports == 1 and len(plan.invalid) == 1
    assert "summary" in plan.invalid[0]
    assert apply_import(root, plan)["committed"] == len(plan.events) == 10


def test_import_a_hand_edited_row_is_a_new_row_by_design(tmp_path):
    """Content is identity: a line whose bytes changed imports beside its
    earlier self. Pinned so the property is a decision, not a surprise."""
    root = _root(tmp_path)
    dlog, rlog = _ledgers(tmp_path, root)
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    apply_import(root, plan)
    _write(rlog, [_rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed", summary="edited")])
    with _ro(root) as conn:
        again = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW)
    assert again.dispatches == 0 and again.reports == 1
    assert apply_import(root, again)["committed"] == 2
    conn = connect(db_path(root))
    try:
        refs = [r[0] for r in conn.execute(
            "SELECT DISTINCT source_ref FROM events WHERE kind='task' AND event='completed'")]
    finally:
        conn.close()
    assert len(refs) == 2 and all(r.startswith("report-back:sha:") for r in refs)


def test_parity_cli_refusal_leaves_no_directory_behind(tmp_path):
    import subprocess, sys as _sys
    root = tmp_path / "bare"
    root.mkdir()
    r = subprocess.run([_sys.executable, "-m", "claudlobby", "--root", str(root), "plane", "parity"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 3 and "UNREACHABLE" in r.stdout
    assert not (root / "state").exists()


def test_import_an_oversized_field_refuses_under_the_default_capture_too(tmp_path):
    """The batch door validates RAW first: metadata capture STRIPS an over-cap
    summary, so a validator that only looked at the captured form accepted a
    unit the real door then refused — reopening the whole-batch abort. Pinned
    under capture={} (the default when no capture.json exists)."""
    root = _root(tmp_path, capture="{}")
    dlog, rlog = _ledgers(tmp_path, root)
    _write(rlog, [_rrow("2026-08-28T03:00:00Z", "t-0-0000", "completed", summary="x" * 5000)])
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=dlog, report_path=rlog, now=NOW,
                           capture={})
    assert plan.reports == 0 and len(plan.invalid) == 1 and "summary" in plan.invalid[0]
    assert apply_import(root, plan)["committed"] == len(plan.events) == 4
