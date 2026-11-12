"""Reads group by workstream (#1053): the brief's open work, and the report list's filter."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from claudlobby import brief as brief_mod
from claudlobby import task_operations as tasks
from claudlobby import task_state
from claudlobby.__main__ import main
from claudlobby.brief import build_brief, format_brief, plane_session
from claudlobby.plane import queries
from claudlobby.plane.emit_api import emit_batch
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_message_read_cli import _generated, _readers_for_test_process
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_task_read_cli import active  # noqa: F401 — active release fixture
from tests.test_workstream_backfill import _legacy, _open


def _work_lines(text):
    """The WORK section's group headers and task rows, each row as its task ID."""
    work = text[text.index("\nWORK"):text.index("\nWORKSTREAMS")]
    return [line.split()[0] if line.startswith("    wi_") else line
            for line in work.splitlines()
            if line.startswith(("  workstream ", "  no workstream", "    wi_"))]


def test_the_brief_groups_each_open_task_under_its_workstream(estate, monkeypatch):
    """Each open task sits under its workstream, and the tasks in no workstream under a group of
    their own. A group stands in its oldest task's place, and the JSON names each task's
    workstream: its own, the one it joined, or the one it moved to."""
    ctx, _conn = estate
    own = tasks.admit(ctx, str(uuid4()), title="Own")
    named = _open(ctx, "Release review")
    joined = tasks.admit(ctx, str(uuid4()), title="Joined", workstream_id=named)
    legacy = _legacy(ctx, "Before workstreams")
    moved = tasks.admit(ctx, str(uuid4()), title="Moved")
    tasks.move(ctx, str(uuid4()), moved.task_id, workstream_id=named, reason="Same release")
    brief = build_brief(ctx.context.fleet, ctx.context.paths, "manager", int(time.time()))
    assert [(item["task_id"], item["workstream_id"]) for item in brief["work"]["items"]] == [
        (own.task_id, own.task.workstream_id), (joined.task_id, named),
        (legacy, None), (moved.task_id, named)]
    assert own.task.workstream_id != named
    assert _work_lines(format_brief(brief)) == [
        f"  workstream {own.task.workstream_id}: 1 open task(s)", own.task_id,
        f"  workstream {named}: 2 open task(s)", joined.task_id, moved.task_id,
        "  no workstream: 1 open task(s)", legacy]
    # Past the row limit, a group's header says how many of its open tasks are shown.
    monkeypatch.setattr(brief_mod, "TEXT_ROW_LIMIT", 2)
    text = format_brief(brief)
    assert _work_lines(text) == [
        f"  workstream {own.task.workstream_id}: 1 open task(s)", own.task_id,
        f"  workstream {named}: 1 of 2 open task(s)", joined.task_id]
    assert "showing the oldest 2 of 4" in text


def test_one_brief_reads_the_fleet_tasks_once_and_hands_them_on(estate, monkeypatch):
    """One brief reads the fleet's tasks once, and the workstream registry takes that read instead
    of making its own. The control shows that the count sees the registry's own read whenever it
    is handed no tasks, so a brief that stopped handing them on would count two reads."""
    ctx, _conn = estate
    named = _open(ctx)
    tasks.admit(ctx, str(uuid4()), title="Joined", workstream_id=named)
    tasks.admit(ctx, str(uuid4()), title="Own")
    reads = []
    real = task_state.read_tasks

    def counted(*args, **kwargs):
        reads.append(kwargs.get("task_ids"))
        return real(*args, **kwargs)

    monkeypatch.setattr(task_state, "read_tasks", counted)
    brief = build_brief(ctx.context.fleet, ctx.context.paths, "manager", int(time.time()))
    assert named in [entry["id"] for entry in brief["workstreams"]["active"]]
    assert reads == [None]
    plane, note = plane_session(ctx.context.paths, ctx.context.fleet.name)
    assert note is None
    with plane:
        plane.conn.execute("BEGIN")
        plane.pr.workstream_registry(plane.conn, plane.fleet)
    assert len(reads) == 2 and reads[1]


def test_the_workstream_read_covers_the_report_reads_own_population():
    """The list reads each report's workstream over the report read's own WHERE, so every report
    it lists has an entry. (FLEET_REPORTS_SQL is pinned to the stdlib reader's copy elsewhere.)"""
    def population(sql):
        return sql[sql.index(" FROM "):].split(" ORDER BY ")[0]

    assert population(queries.FLEET_REPORT_WORKSTREAMS_SQL) == population(queries.FLEET_REPORTS_SQL)


def _report(root, number, *, author="worker", status="completed", workstream=None, minute=0):
    from claudlobby.report_payload import ReportPayload, encode_report_facts

    report = (ReportPayload(status, summary=f"Report {number}") if status == "completed"
              else ReportPayload(status, reason=f"Report {number}"))
    recipient = "bot:example/worker" if author == "manager" else "bot:example/manager"
    comm, companion = encode_report_facts(
        report, fleet="example", sender=f"bot:example/{author}", recipient=recipient,
        msg_id=f"msg_{number:032x}", event_ids=(f"ev_{number * 2:032x}", f"ev_{number * 2 + 1:032x}"),
        occurred_at=f"2026-09-28T12:{minute:02d}:00Z")
    if workstream is not None:
        comm["payload"]["workstream_id"] = workstream
    emit_batch(root, [comm, companion], require_commit=True)
    return f"msg_{number:032x}"


def _recorded(root, workstream_id, title):
    emit_batch(root, [{"event_type": "workstream", "emitter": "workstream-operation", "fleet": "example",
                       "payload": {"workstream_id": workstream_id, "title": title,
                                   "opened_by": "bot:example/manager"}}], require_commit=True)


def test_the_report_list_filters_by_workstream_and_composes_with_each_filter(active, monkeypatch, capsys):  # noqa: F811
    """`--workstream ID` lists only the reports whose communication names that workstream, and
    narrows each existing filter's view to them. A filtered view supplies no acknowledgement
    cursor. An ID the fleet never recorded, and that no report in the read names, refuses with
    not_found: an empty page would read as "no reports" for what is most likely a typo."""
    root, host = active
    _readers_for_test_process(host.package, monkeypatch)
    _generated(monkeypatch, root, "manager")
    (root / "state/plane/capture.json").write_text(json.dumps({"*": "full"}))
    unrecorded = "ws_" + "b" * 32
    _recorded(root, "release-review", "Release review")
    _recorded(root, "docs-sweep", "Docs sweep")
    named = {
        _report(root, 1, workstream="release-review", minute=0): "release-review",
        _report(root, 2, status="failed", workstream="release-review", minute=1): "release-review",
        _report(root, 3, minute=2): None,
        _report(root, 4, author="manager", workstream="release-review", minute=3): "release-review",
        _report(root, 5, workstream=unrecorded, minute=4): unrecorded,
    }

    def listed(*flags, expected=0):
        assert main(["--root", str(root), "--json", "fleet", "reports", "list", *flags]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["command"] == "fleet.reports.list" and result["ok"] is (expected == 0)
        return result

    def ids(*flags):
        return [item["message_id"] for item in listed(*flags)["data"]["items"]]

    everything = listed()["data"]["items"]
    assert {item["message_id"]: item["workstream_id"] for item in everything} == named
    assert [item["message_id"] for item in everything] == list(named)
    for flags in ((), ("--bot", "worker"), ("--status", "completed"),
                  ("--since", "2026-09-28T12:01:00Z"), ("--unacknowledged",)):
        expected = [message for message in ids(*flags) if named[message] == "release-review"]
        assert expected, flags
        filtered = listed(*flags, "--workstream", "release-review")["data"]
        assert [item["message_id"] for item in filtered["items"]] == expected, flags
        assert {item["workstream_id"] for item in filtered["items"]} == {"release-review"}
        assert (filtered["ack_cursor"], filtered["ack_available"]) == (None, False), flags
    unfiltered = listed("--unacknowledged")["data"]
    assert unfiltered["ack_cursor"] is not None and unfiltered["ack_available"] is True
    # Pages continue under the same filter, and a cursor refuses under any other.
    first = listed("--workstream", "release-review", "--limit", "2")["data"]
    assert [item["message_id"] for item in first["items"]] == list(named)[:2]
    assert ids("--workstream", "release-review", "--limit", "2", "--cursor", first["next_cursor"]) == [
        list(named)[3]]
    for flags in ((), ("--workstream", unrecorded)):
        refused = listed(*flags, "--limit", "2", "--cursor", first["next_cursor"], expected=2)
        assert refused["error"]["code"] == "invalid_argument"
    # Known IDs: one no construct records but a report names, and one recorded with no report.
    assert ids("--workstream", unrecorded) == [list(named)[4]]
    assert ids("--workstream", "docs-sweep") == []
    unknown = listed("--workstream", "no-such-stream", expected=3)
    assert unknown["error"]["code"] == "not_found"
    for value in ("", " release-review"):
        assert listed("--workstream", value, expected=2)["error"]["code"] == "invalid_argument"
    # Must refuse: a workstream read that missed a listed report never calls that report unnamed.
    monkeypatch.setattr(queries, "FLEET_REPORT_WORKSTREAMS_SQL",
                        queries.FLEET_REPORT_WORKSTREAMS_SQL + " AND c.msg_id != 'msg_" + "0" * 31 + "3'")
    drifted = listed(expected=4)
    assert drifted["error"]["code"] == "conflict"
