"""The readers of the report rows serve the PLANE — the only source since
the F18 closure (R2b): `claudlobby report-back` and brief's unacked reports
+ `--ack` read `plane-readers.report_rows` with no ledger probe, no
retirement fact and no file; an unreachable plane REFUSES (rc 3) or OMITS
the section, never an empty answer. The row shapes are the legacy ones, `ts`
in the legacy form, so every consumer and every brief cursor keeps working.
(Cutover C3 introduced these readers behind the retirement fact; R2b removed
the fact. F18 R2a: the supersede hint reads the plane unconditionally —
test_the_supersede_hint_reads_its_task_texts_from_the_plane, and its stdlib
half is test_the_planes_task_texts_carry_the_dispatch_text.)

Deleted with the ledgers (R2b): test_plane_retired_conn_is_the_one_door_fact
(the door is gone — `brief.plane_conn` replaces it, pinned in test_brief),
test_who_reviewed_auto_joins_the_plane_with_the_unretired_ledgers_and_dedupes
(`--source auto` and the ledger sources went with who-reviewed's plane-only
rewrite), and the "not retired: the ledger" halves of the report-back / brief
tests (→ test_report_back_serves_the_plane,
test_brief_unacked_from_the_plane_and_the_cursor_keeps_comparing).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from claudlobby.brief import _reports_section, read_cursor, write_cursor
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import F, REPO, _env, _report, _scene, _stdlib_readers, ro as _ro

LIB = REPO / "lib"
TERMINAL = {"completed", "failed", "blocked"}


def _drop_plane(root):
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()


def _report_back(root, *args, **extra):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", F,
                           "report-back", *args], capture_output=True, text=True, timeout=180,
                          env=_env(root, **extra))


def _rows_of(r):
    assert r.returncode == 0, r.stdout + r.stderr
    return [json.loads(l) for l in r.stdout.splitlines()]


def _wire(bot, status, summary, **extras):
    tail = "".join(f" | {k}:{v}" for k, v in extras.items())
    return f"[BOTREPORT] {bot} | {status} | {summary}{tail}"


# --- the reader ----------------------------------------------------------------

def test_report_rows_render_the_legacy_row_from_each_leg(tmp_path):
    """id'd completed (task event: status, summary, pr_url, task id), progress
    (task event with progress), an id-less terminal note (the marker), a
    body-only note (the wire line parsed), and a body the capture policy
    stripped (disclosed, never invented)."""
    root, paths, d, r = _scene(tmp_path)
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    m_done = _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed",
                     extra={"summary": "shipped it", "pr_url": "https://github.com/o/r/pull/7"})
    m_prog = _report(root, wi2, asg2, "2026-09-02T11:00:00Z", event="progress",
                     extra={"summary": "halfway", "progress": 50})
    m_note = _report(root, None, None, "2026-09-02T13:00:00Z", event=None, status="failed")
    pr = _stdlib_readers()
    # a body-only progress note (no task event, no marker) and a stripped one
    body_msg = "msg_" + "b" * 32
    emit_batch(root, [{"event_type": "communication", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:{body_msg}", "occurred_at": "2026-09-02T14:00:00Z",
                       "payload": {"msg_id": body_msg, "sender": f"bot:{F}/w2", "recipient": f"bot:{F}/mgr",
                                   "recipient_raw": "mgr", "message_class": "report",
                                   "body": _wire("w2", "progress", "reading the spec", progress=10)}}])
    with _ro(root) as conn:
        rows = pr.report_rows(conn, F)
    by_msg = {x["plane_msg_id"]: x for x in rows}
    assert [x["ts"] for x in rows] == sorted(x["ts"] for x in rows)                 # oldest first
    assert set(pr.REPORT_FIELDS) <= set(by_msg[m_done])                            # the legacy row's keys
    done = by_msg[m_done]
    assert (done["ts"], done["bot"], done["task_id"], done["status"], done["summary"], done["pr_url"]) == \
        ("2026-09-02T12:00:00Z", "w1", "t-2-bbbb", "completed", "shipped it", "https://github.com/o/r/pull/7")
    prog = by_msg[m_prog]
    assert (prog["status"], prog["progress"], prog["summary"], prog["task_id"]) == ("progress", "50", "halfway", "t-2-bbbb")
    note = by_msg[m_note]
    assert (note["status"], note["task_id"], note["_source"]) == ("failed", "", "marker")
    body = by_msg[body_msg]
    assert (body["bot"], body["status"], body["summary"], body["progress"], body["_source"]) == \
        ("w2", "progress", "reading the spec", "10", "body")
    assert not any(x["_body_stripped"] for x in rows if x["plane_msg_id"] == body_msg)
    stripped = [x for x in rows if x["_body_stripped"]]                             # the fixture's bodies are "r": kept
    assert stripped == []
    with _ro(root) as conn:
        assert [x["plane_msg_id"] for x in pr.report_rows(conn, F, bot="W2")] == [body_msg]      # case-insensitive
        assert [x["task_id"] for x in pr.report_rows(conn, F, status="completed")] == ["t-1-aaaa", "t-2-bbbb"]   # the scene's own completion too
        assert [x["plane_msg_id"] for x in pr.report_rows(conn, F, since="2026-09-02T12:30:00Z")] == [m_note, body_msg]
    assert pr.legacy_ts("2026-09-04T01:09:38.038871+00:00") == "2026-09-04T01:09:38Z"
    assert pr.parse_report_body("") == {} and pr.parse_report_body(None) == {}


def test_a_stripped_body_is_disclosed_never_invented(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    msg = "msg_" + "c" * 32
    emit_batch(root, [{"event_type": "communication", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:{msg}", "occurred_at": "2026-09-02T15:00:00Z",
                       "payload": {"msg_id": msg, "sender": f"bot:{F}/w1", "recipient": f"bot:{F}/mgr",
                                   "recipient_raw": "mgr", "message_class": "report"}}])   # no body: metadata capture
    pr = _stdlib_readers()
    with _ro(root) as conn:
        row = next(x for x in pr.report_rows(conn, F) if x["plane_msg_id"] == msg)
    assert row["_body_stripped"] and row["summary"] == "" and row["status"] == "" and row["_source"] == "none"


# --- claudlobby report-back --------------------------------------------------------

def test_report_back_serves_the_plane(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    msg = _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed",
                  extra={"summary": "shipped it", "pr_url": "https://github.com/o/r/pull/7"})
    rows = _rows_of(_report_back(root, "--json"))
    assert [x["task_id"] for x in rows] == ["t-1-aaaa", "t-2-bbbb"]
    assert set(rows[1]) == set(_stdlib_readers().REPORT_FIELDS)                     # the legacy row, private keys stripped
    assert (rows[1]["summary"], rows[1]["pr_url"], rows[1]["plane_msg_id"]) == \
        ("shipped it", "https://github.com/o/r/pull/7", msg)
    assert [x["task_id"] for x in _rows_of(_report_back(root, "--json", "--bot", "w1", "--status", "completed"))] == ["t-1-aaaa", "t-2-bbbb"]
    assert [x["task_id"] for x in _rows_of(_report_back(root, "--json", "--since", "2026-09-02T11:30:00Z"))] == ["t-2-bbbb"]
    table = _report_back(root)
    assert table.returncode == 0 and "shipped it" in table.stdout and "2 event(s)" in table.stdout
    none = _report_back(root, "--bot", "nobody")
    assert none.returncode == 0 and "0 event(s) matched" in none.stdout and f"the plane (fleet {F})" in none.stdout
    _drop_plane(root)
    gone = _report_back(root, "--json")
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr    # unreachable is not empty


def test_report_back_refuses_when_the_matcher_is_unreachable(tmp_path):
    """Every reader rides the install's matcher session (R2b-1 fold): a lib/
    without it cannot answer, and the command REFUSES — never an empty table."""
    root, paths, _, _ = _scene(tmp_path)
    (root / "lib").unlink()
    (root / "lib").mkdir()
    (root / "lib" / "plane-readers.py").symlink_to(REPO / "lib" / "plane-readers.py")
    gone = _report_back(root, "--json")
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


# --- brief: unacked reports + --ack ---------------------------------------------------

def test_brief_unacked_from_the_plane_and_the_cursor_keeps_comparing(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed", extra={"summary": "one"})
    deg = []
    before = _reports_section(paths, None, TERMINAL, deg)
    assert before["source"] == "plane"
    assert [(x["task_id"], x["summary"]) for x in before["unacked"]] == [("t-1-aaaa", ""), ("t-2-bbbb", "one")]
    write_cursor(paths, "mgr", before["unacked"][-1]["ts"])                          # the legacy-form cursor
    deg = []
    after = _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg)
    assert after["unacked"] == []                                                    # everything acked
    _report(root, wi2, asg2, "2026-09-02T12:00:01Z", event="failed", extra={"summary": "two"})
    deg = []
    later = _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg)
    assert [(x["summary"], x["status"], x["ts"]) for x in later["unacked"]] == [("two", "failed", "2026-09-02T12:00:01Z")]
    assert not any(x.field == "reports" and x.mode == "omitted" for x in deg)
    _drop_plane(root)
    deg = []
    assert _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg) == {}    # unreachable: omitted, never 0
    assert any(x.field == "reports" and x.mode == "omitted" and x.issue == "#1467" for x in deg)


def test_brief_omits_the_section_when_the_matcher_is_unreachable(tmp_path):
    """Every plane read rides the install's matcher session (R2b-1 fold): an
    install whose lib/ lacks it cannot answer, and the section is OMITTED —
    never '0 unacked'."""
    root, paths, _, _ = _scene(tmp_path)
    (root / "lib").unlink(); (root / "lib").mkdir()
    (root / "lib" / "plane-readers.py").symlink_to(REPO / "lib" / "plane-readers.py")
    deg = []
    assert _reports_section(paths, None, TERMINAL, deg) == {}
    assert any(x.field == "reports" and x.mode == "omitted" for x in deg)


# --- the supersede hint ------------------------------------------------------------------

def _hint_module():
    spec = importlib.util.spec_from_file_location("hint", LIB / "dispatch-supersede-hint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _title_with_ref(root):
    """The plane's work item for t-2 carries the text with the reference."""
    from claudlobby.plane.db import connect
    with connect(root / "state" / "plane" / "plane.db") as conn:
        conn.execute("UPDATE work_items SET title = ? WHERE work_item_id = ?",
                     ("fix the flaky test in #480", "wi_" + "2".rjust(32, "0")))


def test_the_planes_task_texts_carry_the_dispatch_text(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _title_with_ref(root)
    pr = _stdlib_readers()
    with _ro(root) as conn:
        assert pr.task_texts(conn, F, "W1")["t-2-bbbb"] == "fix the flaky test in #480"   # case-insensitive alias
        assert pr.task_texts(conn, F, "ghost") == {}


def test_the_supersede_hint_reads_its_task_texts_from_the_plane(tmp_path, monkeypatch):
    root, paths, _, _ = _scene(tmp_path)
    _title_with_ref(root)
    hint = _hint_module()
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root)); monkeypatch.setenv("CLAUDLOBBY_FLEET", F)
    monkeypatch.delenv("FLEET_NAME", raising=False)
    n, ids, note = hint.hint("w1", "another pass at #480")           # the plane's text carries #480
    assert n == 1 and ids == ["t-2-bbbb"] and "--supersedes t-2-bbbb" in note
    n, ids, note = hint.hint("w1", "unrelated work")                 # the quiet tier: counted, never spoken
    assert (n, ids, note) == (1, [], "")
