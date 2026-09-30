"""The readers of the report rows serve the PLANE — the only source since
the F18 closure (R2b): brief's unacked reports
and the report acknowledgement cursor read `plane-readers.report_rows` with no ledger probe, no
retirement fact and no file; an unreachable plane REFUSES (rc 3) or OMITS
the section, never an empty answer. The row shapes are the legacy ones, `ts`
in the legacy form, so every consumer and every brief cursor keeps working.
The historical task-text projection remains covered by
test_the_planes_task_texts_carry_the_dispatch_text.

Deleted with the ledgers (R2b): test_plane_retired_conn_is_the_one_door_fact
(the door is gone — `brief.plane_conn` replaces it, pinned in test_brief),
test_who_reviewed_auto_joins_the_plane_with_the_unretired_ledgers_and_dedupes
(`--source auto` and the ledger sources went with who-reviewed's plane-only
rewrite), and the "not retired: the ledger" half of the brief test
(→ test_brief_unacked_from_the_plane_and_the_cursor_keeps_comparing).
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from claudlobby.brief import _reports_section, ack_request
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import F, _report, _scene, _stdlib_readers, ro as _ro

TERMINAL = {"completed", "failed", "blocked"}


def _drop_plane(root):
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()


def _wire(bot, status, summary, **extras):
    tail = "".join(f" | {k}:{v}" for k, v in extras.items())
    return f"[BOTREPORT] {bot} | {status} | {summary}{tail}"


def _without_matcher(paths, native):
    """A selected private native directory with readers but no matcher."""
    native.mkdir()
    (native / "plane-readers.py").write_bytes((paths.lib / "plane-readers.py").read_bytes())
    return replace(paths, package=replace(paths.package, native=native))


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
    assert all(x["report"]["state"] == "legacy" for x in rows)
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
    assert row["report"]["state"] == "invalid" and row["report"]["payload"] is None


@pytest.fixture
def report_projection(tmp_path):
    """Real-schema retained rows: reader evidence, independent of native ingest."""
    import sqlite3
    from claudlobby.plane.identity import resolve
    from claudlobby.report_payload import ReportLink, encode_report_facts
    from tests.plane_setup import initialize_plane
    from tests.test_task_audit import _insert

    conn = sqlite3.connect(initialize_plane(tmp_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    fleet = resolve(conn, "fleet", F, now="2026-09-28T00:00:00Z")
    sender = resolve(conn, "actor", f"bot:{F}/w1", now="2026-09-28T00:00:00Z")
    work, assignment = "wi_" + "a" * 32, "asg_" + "a" * 32
    _insert(conn, "work_items", work_item_id=work, fleet_uid=fleet, title="work", created_by_uid=sender)
    _insert(conn, "assignments", assignment_id=assignment, work_item_id=work, fleet_uid=fleet,
            assignee_uid=sender, assigned_by_uid=sender, source_ref="task:new-assignment")

    def add(payload, *, transition=None, privacy="full", truncated=False, body_transform=lambda body: body):
        n = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] + 1
        msg = f"msg_{n:032x}"
        communication, companion = encode_report_facts(
            payload, fleet=F, sender=f"bot:{F}/w1", recipient=f"bot:{F}/mgr", msg_id=msg,
            event_ids=(f"ev_{n * 2:032x}", f"ev_{n * 2 + 1:032x}"),
            occurred_at="2026-09-28T12:00:00Z",
            link=ReportLink(work, assignment, transition) if transition else None)
        comm = communication["payload"]
        _insert(conn, "communications", emitter=communication["emitter"], fleet_uid=fleet,
                msg_id=msg, sender_uid=sender, sender_alias=comm["sender"], message_class="report",
                source_ref=communication["source_ref"], body=body_transform(comm["body"]),
                privacy=privacy, truncated=int(truncated), work_item_id=comm.get("work_item_id"),
                assignment_id=comm.get("assignment_id"))
        # Authored retained metadata follows the existing ingest projection.
        detail = {key: value for key, value in companion["payload"].items()
                  if key in ("summary", "reason", "progress", "pr_url", "pr_role", "link_source")}
        _insert(conn, "events", emitter=companion["emitter"], fleet_uid=fleet,
                source_ref=companion["source_ref"], kind="task" if transition else "system",
                event=transition or "report_status", actor_uid=sender if transition else None,
                subject_uid=None if transition else sender, subject_kind=None if transition else "actor",
                work_item_id=work if transition else None, assignment_id=assignment if transition else None,
                detail=json.dumps(detail if transition else companion["payload"]["data"]))
        return msg

    yield conn, _stdlib_readers(), add, work, assignment
    conn.close()


def test_typed_reports_preserve_evidence_links_and_blocked_status_filter(report_projection):
    from dataclasses import asdict
    from claudlobby.plane.queries import FLEET_REPORTS_SQL
    from claudlobby.report_payload import ReportPayload

    conn, pr, add, work, assignment = report_projection
    progress = ReportPayload("progress", summary='Read | the spec: "résumé"\nnext', percent=0,
                             pr_url="https://github.com/o/r/pull/7", pr_role="reviewed",
                             artifacts=("https://example.org/a,b?x=1|2", "s3://bucket/report.json"),
                             issues=("https://example.org/issue/1", "https://example.org/issue/2"),
                             skill="review-work")
    note = add(progress)
    blocked = ReportPayload("blocked", reason="Need input", artifacts=progress.artifacts)
    waiting = add(blocked, transition="blocked_waiting")
    returned = add(blocked, transition="returned_blocked")
    before = conn.total_changes
    rows = {r["plane_msg_id"]: r for r in pr.report_rows(conn, F)}
    assert pr.FLEET_REPORTS_SQL == FLEET_REPORTS_SQL
    assert rows[note]["report"] == {"state": "captured", "reason": None, "payload": asdict(progress)}
    assert (rows[note]["progress"], rows[note]["pr_role"], rows[note]["summary"]) == ("0", "reviewed", progress.summary)
    assert rows[note]["task_event"] is rows[note]["work_item_id"] is rows[note]["assignment_id"] is None
    assert [r["plane_msg_id"] for r in pr.report_rows(conn, F, status="blocked")] == [waiting, returned]
    assert [r["plane_msg_id"] for r in pr.report_rows(conn, F, status="progress")] == [note]
    for msg, event in ((waiting, "blocked_waiting"), (returned, "returned_blocked")):
        assert (rows[msg]["task_id"], rows[msg]["work_item_id"], rows[msg]["assignment_id"]) == (work, work, assignment)
        assert rows[msg]["task_event"] == event and rows[msg]["summary"] == blocked.reason
        assert rows[msg]["report"]["payload"] == asdict(blocked)
    assert conn.total_changes == before


@pytest.mark.parametrize(("privacy", "truncated", "body_transform", "state"), [
    ("metadata", False, lambda body: body, "withheld"),
    ("preview", False, lambda body: body[:80], "withheld"),
    ("full", True, lambda body: body[:80], "truncated"),
    ("full", False, lambda body: '{"broken": " | completed | false evidence', "invalid"),
    ("full", False, lambda body: body.replace('"version":1', '"version":2'), "unsupported"),
])
def test_unavailable_report_content_never_becomes_empty_or_legacy_evidence(
        report_projection, privacy, truncated, body_transform, state):
    from claudlobby.report_payload import ReportPayload

    conn, pr, add, _, _ = report_projection
    report = ReportPayload("failed", reason="private reason", pr_url="https://github.com/o/r/pull/7",
                           pr_role="authored", artifacts=("https://example.org/private-artifact",),
                           issues=("https://example.org/private-issue",), skill="private-skill")
    linked = add(report, transition="failed", privacy=privacy, truncated=truncated, body_transform=body_transform)
    unlinked = add(report, privacy=privacy, truncated=truncated, body_transform=body_transform)
    rows = pr.report_rows(conn, F, status="failed")
    assert [r["plane_msg_id"] for r in rows] == [linked, unlinked]
    for row in rows:
        assert row["report"]["state"] == state and row["report"]["reason"]
        assert row["report"]["payload"] is None
        assert (row["artifact"], row["issues"], row["skill"]) == ("", "", "")
        assert (row["pr_url"], row["pr_role"]) == (report.pr_url, report.pr_role)
        if privacy != "full":
            assert "private" not in json.dumps(pr.public(row))  # Even a retained companion cannot leak content.



# --- brief: unacked reports and the plane acknowledgement fact -------------------

def test_brief_unacked_from_the_plane_and_the_cursor_keeps_comparing(tmp_path, monkeypatch, scratch_plane_env):
    root, paths, d, r = _scene(tmp_path)
    wi2, asg2 = "wi_" + "2".rjust(32, "0"), "asg_" + "2".rjust(32, "0")
    _report(root, wi2, asg2, "2026-09-02T12:00:00Z", event="completed", extra={"summary": "one"})
    deg = []
    before = _reports_section(paths, "mgr", TERMINAL, deg)
    assert before["source"] == "plane"
    assert [(x["task_id"], x["summary"]) for x in before["unacked"]] == [("t-1-aaaa", ""), ("t-2-bbbb", "one")]
    for key, value in scratch_plane_env(root).items():
        monkeypatch.setenv(key, value)
    newest = max(before["unacked"], key=lambda x: x["seq"])
    acked = emit_batch(root, [ack_request(
        F, "mgr", acked_through_seq=newest["seq"], acked_through_ts=newest["ts"],
        count=len(before["unacked"]))], require_commit=True)
    assert len(acked) == 1 and acked[0].status == "committed"                    # the ack, a plane fact
    deg = []
    after = _reports_section(paths, "mgr", TERMINAL, deg)
    assert after["unacked"] == []                                                    # everything acked
    _report(root, wi2, asg2, "2026-09-02T12:00:01Z", event="failed", extra={"summary": "two"})
    deg = []
    later = _reports_section(paths, "mgr", TERMINAL, deg)
    assert [(x["summary"], x["status"], x["ts"]) for x in later["unacked"]] == [("two", "failed", "2026-09-02T12:00:01Z")]
    assert not any(x.field == "reports" and x.mode == "omitted" for x in deg)
    _drop_plane(root)
    deg = []
    assert _reports_section(paths, "mgr", TERMINAL, deg) == {}                       # unreachable: omitted, never 0
    assert any(x.field == "reports" and x.mode == "omitted" and x.issue == "#1467" for x in deg)


def test_brief_omits_the_section_when_the_matcher_is_unreachable(tmp_path):
    """Every plane read rides the install's matcher session (R2b-1 fold): an
    install whose claudlobby/_runtime_scripts/ lacks it cannot answer, and the section is OMITTED —
    never '0 unacked'."""
    root, paths, _, _ = _scene(tmp_path)
    paths = _without_matcher(paths, tmp_path / "native")
    deg = []
    assert _reports_section(paths, "mgr", TERMINAL, deg) == {}
    assert any(x.field == "reports" and x.mode == "omitted" for x in deg)


# --- the supersede hint ------------------------------------------------------------------

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
