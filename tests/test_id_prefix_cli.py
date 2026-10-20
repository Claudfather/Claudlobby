"""The public read doors resolve an ID prefix and say so (#2380).

Each door answers a prefix of exactly one readable record with that record and
`data.resolved_from_prefix`, several with `ambiguous_reference` and their
candidates, none with `not_found` after a search by prefix, and a too-short
prefix with `invalid_argument`. Each door's four answers are compared in one
assertion, so a failure shows all four. Every read leaves the plane as it was.
"""

from datetime import datetime, timezone
import json
import sqlite3

from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_checkins_cli import _record
from tests.test_message_read_cli import _communication, _generated
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_read_cli import active  # noqa: F401 — active release fixture
from tests.test_task_state import _assignment, _task


def _hex(head, fill):
    return head + fill * (32 - len(head))


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        return (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])


def _answers(capsys, root, *argv_prefix, references):
    """Each reference's exit, error, resolution and candidates, as one comparable dict."""
    answers = {}
    for reference in references:
        code = main(["--root", str(root), "--json", *argv_prefix, reference])
        result = json.loads(capsys.readouterr().out)
        error = result["error"] or {}
        hint = error.get("hint") if isinstance(error.get("hint"), dict) else {}
        answers[reference] = {
            "exit": code, "error": error.get("code"), "message": error.get("message"),
            "resolved": result["data"].get("resolved_from_prefix"),
            "candidates": [c["command"][-1] for c in hint.get("candidates", [])], "total": hint.get("total_matches")}
    return answers


def _resolved(reference, full_id):
    return {"exit": 0, "error": None, "message": None, "resolved": {"prefix": reference, "id": full_id},
            "candidates": [], "total": None}


def _refused(exit_code, error, message, candidates=(), total=None):
    return {"exit": exit_code, "error": error, "message": message, "resolved": None,
            "candidates": list(candidates), "total": total}


TASK = "wi_" + _hex("bb44eda7", "1")
ASG_DA1, ASG_DA2 = "asg_" + _hex("da1585e0", "1"), "asg_" + _hex("da1585e0", "2")
ASG_ED = "asg_" + _hex("ed555799", "3")


def test_task_and_assignment_show_resolve_a_prefix_and_never_guess(active, capsys):  # noqa: F811
    root, host = active
    from claudlobby.activation_identity import read_selected_identity_bindings
    fleet_uid = read_selected_identity_bindings(root, "example", package=host.package)["fleet_uid"]
    with sqlite3.connect(db_file(root)) as conn:
        _task(conn, TASK, fleet_uid=fleet_uid)
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL WHERE fleet_uid=?", (fleet_uid,))
        for aid in (ASG_DA1, ASG_DA2, ASG_ED):
            _assignment(conn, aid, TASK, fleet_uid=fleet_uid)
    before = _counts(root)

    assert _answers(capsys, root, "assignment", "show", references=(
        "asg_ed555799", "asg_da1585e0", "asg_00000000", "asg_da15")) == {
        "asg_ed555799": _resolved("asg_ed555799", ASG_ED),
        "asg_da1585e0": _refused(4, "ambiguous_reference", "reference maps to multiple canonical records",
                                 (ASG_DA1, ASG_DA2), 2),
        "asg_00000000": _refused(3, "not_found", "no assignment in this fleet starts with asg_00000000", (), 0),
        "asg_da15": _refused(2, "invalid_argument", "an ID prefix needs at least 8 hex digits after its tag: asg_da15"),
    }
    assert _answers(capsys, root, "task", "show", references=("wi_bb44eda7", TASK)) == {
        "wi_bb44eda7": _resolved("wi_bb44eda7", TASK),
        TASK: {"exit": 0, "error": None, "message": None, "resolved": None, "candidates": [], "total": None}}
    assert main(["--root", str(root), "assignment", "show", "asg_ed555799"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == f"resolved from prefix asg_ed555799: {ASG_ED}"
    assert main(["--root", str(root), "assignment", "show", "asg_da1585e0"]) == 4
    assert f"claudlobby assignment show {ASG_DA2}" in capsys.readouterr().err
    assert _counts(root) == before


REQUEST_A, REQUEST_B = "5e1ec7ed-0000-4000-8000-000000000001", "5e1ec7ed-0000-4000-8000-000000000002"
REQUEST_C = "a11ce000-1111-4111-8111-111111111111"
CK_BEEF1, CK_BEEF2 = "ck_" + _hex("beef0001", "1"), "ck_" + _hex("beef0001", "2")
CK_CAFE = "ck_" + _hex("cafe0002", "3")


def test_request_and_checkin_show_resolve_a_prefix_and_never_guess(active, capsys):  # noqa: F811
    root, _ = active
    for request_id in (REQUEST_A, REQUEST_B, REQUEST_C):
        assert main(["--root", str(root), "--json", "task", "admit", "--title", "Prefix work",
                     "--request-id", request_id]) == 0
    capsys.readouterr()
    now = datetime.now(timezone.utc).isoformat()
    emit_batch(root, [{"event_type": "system", "emitter": "checkin-record", "fleet": "example",
                       "source_ref": f"checkin:{ck}", "occurred_at": now,
                       "payload": {"event": "checkin_decision", "subject_kind": "actor",
                                   "subject": "bot:example/manager", "data": _record(ck)}}
                      for ck in (CK_BEEF1, CK_BEEF2, CK_CAFE)], require_commit=True)
    before = _counts(root)

    assert _answers(capsys, root, "request", "show", references=(
        "a11ce000", "5e1ec7ed", "00000000", "a11ce00")) == {
        "a11ce000": _resolved("a11ce000", REQUEST_C),
        "5e1ec7ed": _refused(4, "ambiguous_reference",
                             "2 retained requests start with 5e1ec7ed; select a canonical request ID",
                             (REQUEST_A, REQUEST_B), 2),
        "00000000": _refused(3, "not_found", "no retained request record in this fleet starts with 00000000; "
                             "this cannot prove that no earlier best-effort message was sent", (), 0),
        "a11ce00": _refused(2, "invalid_argument", "a request ID prefix needs at least its first 8 hex digits"),
    }
    assert _answers(capsys, root, "checkin", "show", references=(
        "ck_cafe0002", "ck_beef0001", "ck_00000000", "ck_cafe")) == {
        "ck_cafe0002": _resolved("ck_cafe0002", CK_CAFE),
        "ck_beef0001": _refused(4, "ambiguous_reference",
                                "2 check-in decisions start with ck_beef0001; select a canonical check-in ID",
                                (CK_BEEF1, CK_BEEF2), 2),
        "ck_00000000": _refused(3, "not_found", "no check-in decision in the selected fleet starts with ck_00000000",
                                (), 0),
        "ck_cafe": _refused(2, "invalid_argument", "a check-in ID prefix needs at least 8 hex digits after ck_"),
    }
    assert _counts(root) == before


def test_message_show_and_receipt_resolve_a_readable_prefix(active, monkeypatch, capsys):  # noqa: F811
    root, _ = active
    first, second, unique = (f"msg_{(0x42664fe3 << 96) | n:032x}" for n in (1, 2, 3))
    unique = "msg_77aa88bb" + unique[12:]
    for number in (int(first[4:], 16), int(second[4:], 16), int(unique[4:], 16)):
        _communication(root, number, sender="bot:example/manager", recipient="bot:example/worker")
    _generated(monkeypatch, root, "worker")
    before = _counts(root)

    assert _answers(capsys, root, "message", "show", references=(
        "msg_77aa88bb", "msg_42664fe3", "msg_00000000", "msg_77aa")) == {
        "msg_77aa88bb": _resolved("msg_77aa88bb", unique),
        "msg_42664fe3": _refused(4, "ambiguous_reference",
                                 "2 messages start with msg_42664fe3; select a canonical message ID",
                                 (first, second), 2),
        "msg_00000000": _refused(3, "not_found", "no message this caller may read starts with msg_00000000", (), 0),
        "msg_77aa": _refused(2, "invalid_argument", "a message ID prefix needs at least 8 hex digits after msg_"),
    }
    receipt = _answers(capsys, root, "message", "receipt", "--destination", "worker",
                       references=("msg_77aa88bb",))["msg_77aa88bb"]
    assert (receipt["exit"], receipt["error"], receipt["resolved"]) == (
        9, "receipt_unobservable", {"prefix": "msg_77aa88bb", "id": unique})
    # The sender's own session reads the same message by the same prefix.
    _generated(monkeypatch, root, "manager")
    assert _answers(capsys, root, "message", "show", references=("msg_77aa88bb",)) == {
        "msg_77aa88bb": _resolved("msg_77aa88bb", unique)}
    assert _counts(root) == before
