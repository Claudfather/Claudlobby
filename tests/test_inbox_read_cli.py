"""Public fleet inbox projection over selected private Plane history."""

import json
import sqlite3

from claudlobby import brief
from claudlobby.__main__ import main
from claudlobby.activation_identity import read_selected_identity_bindings
from claudlobby.paths import load_lib_module
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.report_payload import ReportPayload, encode_report_facts
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private socket root
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency
from tests.test_task_read_cli import active  # noqa: F401 — selected private release
from tests.test_task_state import _assignment, _event, _task


def test_inbox_keeps_fleet_work_and_viewer_reports_separate(active, monkeypatch, capsys):  # noqa: F811
    root, host = active
    # The sealed fixture's native package is intentionally minimal. Use the
    # real installed reader through brief's existing import seam.
    monkeypatch.setattr(brief, "load_dispatch_doors",
                        lambda paths: load_lib_module(source_package().native, "dispatch-overdue.py"))
    bindings = read_selected_identity_bindings(root, "example", package=host.package)
    with sqlite3.connect(db_file(root)) as conn:
        _task(conn, "wi_inbox", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL "
                     "WHERE work_item_id='wi_inbox'")
        raise_id = _event(conn, "wi_inbox", None, "escalated", fleet_uid=bindings["fleet_uid"],
                          actor_uid="actor_manager", detail='{"by":"manager","question":"Which priority?"}')
        _assignment(conn, "asg_orphan", "wi_missing", fleet_uid=bindings["fleet_uid"])

    (root / "state/plane/capture.json").write_text('{"*":"full"}')
    for number in (1, 2):
        facts = encode_report_facts(
            ReportPayload("completed", summary=f"Result {number}"),
            fleet="example", sender="bot:example/worker", recipient="bot:example/manager",
            msg_id=f"msg_{number:032x}",
            event_ids=(f"ev_{number * 2:032x}", f"ev_{number * 2 + 1:032x}"),
            occurred_at=f"2026-09-28T12:0{number}:00Z")
        emit_batch(root, facts, require_commit=True)
    with sqlite3.connect(db_file(root)) as conn:
        first_seq = conn.execute("SELECT ingest_seq FROM communications WHERE msg_id=?",
                                 (f"msg_{1:032x}",)).fetchone()[0]
    emit_batch(root, [brief.ack_request("example", "worker", acked_through_seq=first_seq,
                                        acked_through_ts="2026-09-28T12:01:00Z", count=1)],
               require_commit=True)

    def counts():
        with sqlite3.connect(db_file(root)) as conn:
            return (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])

    before = counts()

    def inbox(*flags, expected=0):
        assert main(["--root", str(root), "--json", "fleet", "inbox", *flags]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["schema_version"] == 1 and result["request_id"] is None
        assert result["command"] == "fleet.inbox" and result["ok"] is (expected == 0)
        assert result["release_id"] == host.release.release_id
        return result

    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_NAME": "example",
                       "BOT_ID": "manager"}.items():
        monkeypatch.setenv(key, value)
    first = inbox("--limit", "1")["data"]
    assert first["viewer"] == "bot:example/manager"
    assert first["viewer_selection"] == "generated"
    assert [item["task_id"] for item in first["work"]["items"]] == ["wi_inbox"]
    assert first["work"]["items"][0]["state"] == "queued"  # unassigned intake remains visible
    assert [item["message_id"] for item in first["reports"]["items"]] == [f"msg_{1:032x}"]
    assert "ack_cursor" not in first and first["reports"]["ack_cursor"] is None
    assert first["attention"]["recorded_escalations"] == [{
        "task_id": "wi_inbox", "assignment_id": None, "event_id": raise_id,
        "actor_uid": "actor_manager", "occurred_at": "2026-09-01T00:00:00Z",
        "by": "manager", "question": "Which priority?"}]
    assert first["attention"]["escalation_observation"] == (
        "currently_open_task_escalations_including_queued_work")
    assert first["attention"]["alert_window_hours"] == brief.ALERT_WINDOW_H
    assert first["attention"]["alerts_observation"] == "recent_recorded_incomplete"
    assert first["attention"]["alert_read_position"] is None
    assert first["next_cursor"]
    second = inbox("--limit", "1", "--cursor", first["next_cursor"])["data"]
    assert second["work"]["items"] == []  # exhausted work lane cannot restart
    assert [(issue["code"], issue["task_id"], issue["assignment_id"])
            for issue in second["work"]["issues"]] == [
                ("dangling_assignment", "wi_missing", "asg_orphan")]
    assert second["attention"]["recorded_escalations"] == first["attention"]["recorded_escalations"]
    assert [item["message_id"] for item in second["reports"]["items"]] == [f"msg_{2:032x}"]
    assert second["next_cursor"] is None
    assert main(["--root", str(root), "fleet", "inbox", "--limit", "1",
                 "--cursor", first["next_cursor"]]) == 0
    plain = capsys.readouterr().out
    assert "Open work on this page: 0" in plain
    assert "currently open task escalations (including queued work): 1" in plain
    assert f"task escalation wi_inbox\tassignment=-\tevent={raise_id}\tquestion=Which priority?" in plain
    assert "Work history caveat: 1 recorded issue(s), 1 blocking" in plain
    assert "work issue dangling_assignment\ttask=wi_missing assignment=asg_orphan" in plain

    worker = inbox("--bot", "worker")["data"]
    assert worker["viewer"] == "bot:example/worker" and worker["viewer_selection"] == "explicit"
    assert [item["task_id"] for item in worker["work"]["items"]] == ["wi_inbox"]
    assert [item["message_id"] for item in worker["reports"]["items"]] == [f"msg_{2:032x}"]
    assert worker["reports"]["read_position"]["acked_through_seq"] == first_seq
    assert inbox("--bot", "worker", "--limit", "1", "--cursor", first["next_cursor"],
                 expected=2)["error"]["code"] == "invalid_argument"

    for key in ("BOT_ID", "FLEET_NAME", "CLAUDLOBBY_ROOT"):
        monkeypatch.delenv(key)
    operator = inbox()["data"]
    assert operator["viewer"] == "bot:example/manager"
    assert operator["viewer_selection"] == "manager_default"
    assert [item["task_id"] for item in operator["work"]["items"]] == ["wi_inbox"]
    assert counts() == before  # reads never register a human or ACK a report
