"""Structured evidence, honest redaction and explicit linked/unlinked facts."""

from dataclasses import replace
import json

import pytest

from claudlobby.plane.registries import PR_ROLES, cap_for
from claudlobby.report_payload import (
    ReportLink, ReportPayload, ReportPayloadError, decode_report_body, encode_report_facts,
)
from claudlobby.task_state import TASK_EMITTER

MSG = "msg_" + "1" * 32
WI = "wi_" + "2" * 32
ASG = "asg_" + "3" * 32
IDS = ("ev_" + "4" * 32, "ev_" + "5" * 32)


def _facts(report, link=None):
    return encode_report_facts(report, fleet="f", sender="bot:f/worker", recipient="bot:f/manager",
                               msg_id=MSG, event_ids=IDS, occurred_at="2026-09-28T12:00:00Z", link=link)


def _report():
    return ReportPayload("progress", summary='Read | the spec: "résumé"\nnext step', percent=0,
                         pr_url="https://github.com/o/r/pull/1", pr_role=PR_ROLES[1],
                         artifacts=("https://example.org/a,b?x=1|2", "s3://bucket/report.json"),
                         issues=("https://github.com/o/r/issues/2", "https://github.com/o/r/issues/3"),
                         skill="review-work")


def test_structured_repeated_evidence_round_trips_without_delimiter_loss():
    report = _report()
    body = report.to_body()
    decoded = decode_report_body(body)
    assert decoded.state == "captured" and decoded.payload == report
    assert decoded.payload.artifacts == report.artifacts and decoded.payload.issues == report.issues
    assert json.loads(body)["summary"] == report.summary
    assert decode_report_body(body, privacy="metadata").payload is None


def test_payload_refuses_invalid_flags_and_registry_byte_caps():
    for changes in ({"status": "completed"}, {"percent": True}, {"percent": 101},
                    {"pr_role": None}, {"pr_url": None}, {"pr_role": "unknown"},
                    {"artifacts": "https://example.org/not-a-list"}):
        with pytest.raises(ReportPayloadError):
            replace(_report(), **changes)
    limit = cap_for("task", "summary")
    assert ReportPayload("completed", summary="é" * (limit // 2)).summary
    with pytest.raises(ReportPayloadError, match="task.summary"):
        ReportPayload("completed", summary="é" * (limit // 2 + 1))
    with pytest.raises(ReportPayloadError, match="communication.body"):
        ReportPayload("completed", summary="done",
                      artifacts=("https://example.org/" + "a" * cap_for("communication", "body"),))


def test_decoder_distinguishes_redacted_legacy_invalid_and_future_content():
    body = _report().to_body()
    assert decode_report_body(None, privacy="metadata").state == "withheld"
    assert decode_report_body(None).state == "invalid"
    assert decode_report_body(body[:80], truncated=True).state == "truncated"
    assert decode_report_body("[BOTREPORT] worker | completed | shipped").state == "legacy"
    assert decode_report_body(body[:80]).state == "invalid"
    assert decode_report_body(body.replace('"version":1', '"version":2')).state == "unsupported"
    assert decode_report_body(body.replace('"version":1', '"version":1,"version":1')).state == "invalid"
    assert decode_report_body(body.replace('"percent":0', '"percent":false')).payload is None


def test_explicit_link_preserves_task_semantics_and_unlinked_marker_has_no_content():
    report = _report()
    communication, task = _facts(report, ReportLink(WI, ASG, "progress"))
    assert communication["payload"]["work_item_id"] == task["payload"]["work_item_id"] == WI
    assert task["emitter"] == TASK_EMITTER and task["payload"]["link_source"] == "named"
    assert task["payload"]["progress"] == 0 and task["payload"]["pr_role"] == report.pr_role
    assert communication["source_ref"] == task["source_ref"] == f"report:{MSG}"
    communication, marker = _facts(report)
    assert "work_item_id" not in communication["payload"] and marker["event_type"] == "system"
    assert marker["payload"]["data"] == {"status": "progress", "msg_id": MSG, "progress": 0,
                                           "pr_url": report.pr_url, "pr_role": report.pr_role}
    blocked = ReportPayload("blocked", reason="Need an input")
    _, waiting = _facts(blocked, ReportLink(WI, ASG, "blocked_waiting"))
    _, returned = _facts(blocked, ReportLink(WI, ASG, "returned_blocked"))
    assert waiting["payload"]["event"] != returned["payload"]["event"]
    assert returned["payload"]["reason"] == blocked.reason
    with pytest.raises(ReportPayloadError, match="disagree"):
        _facts(report, ReportLink(WI, ASG, "completed"))


def test_real_ingest_capture_preserves_full_evidence_and_withholds_metadata(tmp_path):
    # Real wire/capture/ingest owner: no mocked application dependencies.
    from claudlobby.plane.db import connect_ro, db_file
    from claudlobby.plane.emit_api import emit_batch, validate_item
    from tests.plane_setup import initialize_plane

    report = _report()
    for mode in ("full", "metadata"):
        root = tmp_path / mode
        initialize_plane(root)
        (root / "state/plane/capture.json").write_text(json.dumps({"*": mode}))
        facts = _facts(report)  # unlinked: creates no task or assignment
        for raw in facts:
            validate_item(raw, {"*": mode})
        emit_batch(root, list(facts), require_commit=True)
        conn = connect_ro(db_file(root))
        try:
            row = conn.execute("SELECT body, privacy, truncated FROM communications").fetchone()
            decoded = decode_report_body(row[0], privacy=row[1], truncated=bool(row[2]))
            assert decoded.state == ("captured" if mode == "full" else "withheld")
            assert decoded.payload == (report if mode == "full" else None)
            marker = json.loads(conn.execute("SELECT detail FROM events WHERE event='report_status'").fetchone()[0])
            assert marker["pr_role"] == report.pr_role and marker["pr_url"] == report.pr_url
            assert set(marker) == {"status", "msg_id", "progress", "pr_url", "pr_role"}
            assert conn.execute("SELECT count(*) FROM assignments").fetchone()[0] == 0
        finally:
            conn.close()
        # Linked CONTENT fields use the exact same capture owner, and retain
        # attribution even when both authored content carriers are stripped.
        linked = _facts(report, ReportLink(WI, ASG, "progress"))
        captured = [validate_item(raw, {"*": mode})[1] for raw in linked]
        detail = captured[1]["payload"]
        assert ("summary" in detail) == (mode == "full")
        assert detail["pr_role"] == report.pr_role and detail["pr_url"] == report.pr_url
