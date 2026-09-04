"""Cutover chunk C3 — the readers of the RETIRED ledgers move to the plane.
With the dispatch, report and events writes retired (Phase C), four readers
still opened a frozen file and answered from the past: `claudlobby
report-back`, brief's unacked reports + `--ack`, `who-reviewed.py`'s default
source, and the supersede hint's task texts. Each now follows the retirement
fact itself (`legacy_write_retired` naming the door, read on the plane —
`brief.plane_retired_conn`), never a new flag: a frozen ledger is wrong on the
day it freezes. The row shapes are the legacy ones, `ts` in the legacy form,
so every consumer and every brief cursor keeps working.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from claudlobby.brief import _reports_section, plane_retired_conn, read_cursor, write_cursor
from claudlobby.plane import shadow as sh
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import _cli, _declare, _env, _stdlib_readers
from tests.test_plane_cutover_parity import _live_dispatch, _rrow, _write
from tests.test_plane_shadow import F, REPO, _report, _scene

LIB = REPO / "lib"
TERMINAL = {"completed", "failed", "blocked"}


def _retire(root):
    for reader in sh.GATED:
        _declare(root, reader)
    assert _cli(root, "cutover", "--retire-writes").returncode == 0


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

def test_report_back_serves_the_plane_once_the_report_write_is_retired(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    msg = _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed",
                  extra={"summary": "shipped it", "pr_url": "https://github.com/o/r/pull/7"})
    r.append(_rrow("2026-09-02T12:00:00Z", "t-2-bbbb", "completed", summary="shipped it",
                   pr_url="https://github.com/o/r/pull/7", plane_msg_id=msg))
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)
    before = _rows_of(_report_back(root, "--json"))                                 # not retired: the ledger
    assert [x["task_id"] for x in before] == ["t-1-aaaa", "t-2-bbbb"]
    _retire(root)
    after = _rows_of(_report_back(root, "--json"))                                  # retired: the plane
    assert [x["task_id"] for x in after] == ["t-1-aaaa", "t-2-bbbb"]
    same = {"ts", "bot", "task_id", "status", "summary", "pr_url", "plane_msg_id"}
    assert {k: before[1][k] for k in same} == {k: after[1][k] for k in same}         # the same row, either source
    assert set(after[1]) == set(before[1])                                           # the same keys
    assert [x["task_id"] for x in _rows_of(_report_back(root, "--json", "--bot", "w1", "--status", "completed"))] == ["t-1-aaaa", "t-2-bbbb"]
    assert _rows_of(_report_back(root, "--json", "--since", "2026-09-02T11:30:00Z")) and \
        [x["task_id"] for x in _rows_of(_report_back(root, "--json", "--since", "2026-09-02T11:30:00Z"))] == ["t-2-bbbb"]
    table = _report_back(root)
    assert table.returncode == 0 and "shipped it" in table.stdout and "2 event(s)" in table.stdout
    none = _report_back(root, "--bot", "nobody")
    assert none.returncode == 0 and "0 event(s) matched" in none.stdout and "the plane (the report ledger is retired)" in none.stdout
    _drop_plane(root)
    gone = _report_back(root, "--json")
    assert gone.returncode == 0 and gone.stdout != ""                              # the fact cannot be read: the ledger, LABELED
    assert "may be stale" in gone.stderr


def test_report_back_refuses_when_the_plane_cannot_answer_under_a_retirement(tmp_path, monkeypatch):
    root, paths, _, _ = _scene(tmp_path)
    _retire(root)
    (root / "lib").unlink()                                                          # the readers are not reachable
    (root / "lib").mkdir()
    (root / "lib" / "dispatch-overdue.py").symlink_to(REPO / "lib" / "dispatch-overdue.py")
    gone = _report_back(root, "--json")
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


# --- brief: unacked reports + --ack ---------------------------------------------------

def test_brief_unacked_follows_the_retirement_and_the_cursor_keeps_comparing(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    from claudlobby.brief import report_ledger_path
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    m1 = _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed", extra={"summary": "one"})
    r.append(_rrow("2026-09-02T12:00:00Z", "t-2-bbbb", "completed", summary="one", plane_msg_id=m1))
    _write(report_ledger_path(paths), r)
    deg = []
    before = _reports_section(paths, None, TERMINAL, deg)                          # the ledger
    assert [x["summary"] for x in before["unacked"]] == ["done", "one"] and "source" not in before
    write_cursor(paths, "mgr", before["unacked"][-1]["ts"])                          # the legacy-form cursor
    _retire(root)
    deg = []
    after = _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg)       # the plane
    assert after["source"] == "plane" and after["unacked"] == []                     # everything acked, on both sides
    m2 = _report(root, wi2, asg2, "2026-09-02T12:00:01Z", event="failed", extra={"summary": "two"})
    deg = []
    later = _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg)
    assert [(x["summary"], x["status"], x["ts"]) for x in later["unacked"]] == [("two", "failed", "2026-09-02T12:00:01Z")]
    assert not any(x.field == "reports" and x.mode == "omitted" for x in deg)
    _drop_plane(root)
    deg = []
    unknown = _reports_section(paths, read_cursor(paths, "mgr"), TERMINAL, deg)      # the fact cannot be read: ledger, LABELED
    assert "source" not in unknown and any(x.field == "reports" and x.mode == "labeled" and "may be stale" in x.reason for x in deg)


def test_brief_omits_the_section_when_the_plane_cannot_answer_under_a_retirement(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _retire(root)
    (root / "lib").unlink(); (root / "lib").mkdir()
    (root / "lib" / "dispatch-overdue.py").symlink_to(REPO / "lib" / "dispatch-overdue.py")
    deg = []
    assert _reports_section(paths, None, TERMINAL, deg) == {}
    assert any(x.field == "reports" and x.mode == "omitted" for x in deg)


def test_plane_retired_conn_is_the_one_door_fact(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    conn, note = plane_retired_conn(paths, "report")
    assert conn is None and note is None                                             # not retired: the ledger, quietly
    _retire(root)
    conn, note = plane_retired_conn(paths, "report")
    assert conn is not None and note is None
    conn.close()
    conn, note = plane_retired_conn(paths, "dispatch")
    assert conn is not None; conn.close()
    _drop_plane(root)
    conn, note = plane_retired_conn(paths, "report")
    assert conn is None and "may be stale" in note


# --- who-reviewed --source auto --------------------------------------------------------

def _who():
    spec = importlib.util.spec_from_file_location("who_reviewed", LIB / "who-reviewed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(path, ts):
    path.write_text(json.dumps({"reviews": [{"submittedAt": ts, "state": "APPROVED",
                                             "author": {"login": "someone"}, "body": "lgtm"}], "comments": []}))
    return path


def test_who_reviewed_auto_joins_the_plane_with_the_unretired_ledgers_and_dedupes(tmp_path, capsys):
    """Fleet f is retired (its reports are the plane's); fleet g is not (its
    report lives in its ledger alone). A report both sides hold is ONE
    candidate, so a review it matches attributes rather than reading AMBIGUOUS."""
    root, paths, d, r = _scene(tmp_path)
    from claudlobby.brief import report_ledger_path
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    m = _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed",
                extra={"summary": "Approved #46", "pr_url": "https://github.com/o/r/pull/46"})
    r.append(_rrow("2026-09-02T12:00:00Z", "t-2-bbbb", "completed", summary="Approved #46",
                   pr_url="https://github.com/o/r/pull/46", plane_msg_id=m))           # the same report, in f's ledger too
    _write(report_ledger_path(paths), r)
    gdir = root / "local" / "g" / "runtime"; gdir.mkdir(parents=True)
    (root / "local" / "g" / "fleet.yaml").write_text("fleet:\n  name: g\n  bots:\n    v1:\n")
    (gdir / "report-back.jsonl").write_text(json.dumps(_rrow("2026-09-03T09:00:00Z", "t-9-gggg", "completed",
                                                             bot="v1", summary="Approved #47",
                                                             pr_url="https://github.com/o/r/pull/47")) + "\n")
    _retire(root)
    who = _who()
    p46 = _payload(tmp_path / "p46.json", "2026-09-02T12:00:05Z")
    rc = who.main(["o/r", "46", "--reviews-json", str(p46), "--root", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["events"][0]["verdict"] == "MATCH" and out["events"][0]["bot"] == "w1", out["events"][0]
    assert out["scope"]["source"] == "auto" and "f" in out["scope"]["fleets"] and "g" in out["scope"]["fleets"]
    assert "plane" not in out["scope"]                                               # reachable: no disclosure
    p47 = _payload(tmp_path / "p47.json", "2026-09-03T09:00:03Z")
    who.main(["o/r", "47", "--reviews-json", str(p47), "--root", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["events"][0]["verdict"] == "MATCH" and out["events"][0]["bot"] == "v1"   # the unretired ledger still joins
    _drop_plane(root)
    who.main(["o/r", "46", "--reviews-json", str(p46), "--root", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["scope"]["plane"].startswith("unreachable") and out["events"][0]["bot"] == "w1"   # the ledgers serve, disclosed


# --- the supersede hint ------------------------------------------------------------------

def test_the_supersede_hint_reads_its_task_texts_from_the_plane_once_flipped(tmp_path, monkeypatch):
    root, paths, d, r = _scene(tmp_path)
    from claudlobby.brief import dispatch_ledger_path, report_ledger_path
    spec = importlib.util.spec_from_file_location("hint", LIB / "dispatch-supersede-hint.py")
    hint = importlib.util.module_from_spec(spec); spec.loader.exec_module(hint)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    # the plane's work item for t-2 carries the text with the reference; the frozen
    # log's `task` field does not (the fixture's rows say "do the thing")
    with _ro(root) as conn:
        conn.execute("SELECT 1").fetchone()
    with __import__("claudlobby.plane.db", fromlist=["connect"]).connect(root / "state" / "plane" / "plane.db") as conn:
        conn.execute("UPDATE work_items SET title = ? WHERE work_item_id = ?", ("fix the flaky test in #480", "wi_" + "2".rjust(32, "0")))
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root)); monkeypatch.setenv("CLAUDLOBBY_FLEET", F)
    monkeypatch.setenv("PLANE_READ_OPEN", "1")
    n, ids, note = hint.hint("w1", dl, rl, "another pass at #480")                    # flag alone: the log's texts (no #480)
    assert ids == []
    _declare(root, "open")
    n, ids, note = hint.hint("w1", dl, rl, "another pass at #480")                    # flipped: the plane's text
    assert ids == ["t-2-bbbb"] and "--supersedes t-2-bbbb" in note
    pr = _stdlib_readers()
    with _ro(root) as conn:
        assert pr.task_texts(conn, F, "W1")["t-2-bbbb"] == "fix the flaky test in #480"
        assert pr.task_texts(conn, F, "ghost") == {}
