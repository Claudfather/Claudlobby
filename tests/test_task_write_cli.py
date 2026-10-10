"""Public task writes against private selected activation and real Plane storage."""

import builtins
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import activation, context, message_operations, operation_context, runtime_admission
from claudlobby.__main__ import main
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.request_receipts import RequestStore, locked_request
from tests.package_fixtures import source_package
from tests.conftest import load_lib_module
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, release, plan, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR",
                "FLEET_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    package = replace(source_package(), native=release.native_path,
                      artifact_id=release.inputs.artifact_id)
    monkeypatch.setattr(context, "get_resources", lambda: package)
    return root, release


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    return result


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("identity_registry", "work_items", "assignments", "communications"))
        return (*counts, conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'").fetchone()[0])


def test_admit_assign_accept_are_distinct_committed_operations(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    root, release = active
    body = tmp_path / "task.txt"
    body.write_text("Review the private evidence", encoding="utf-8")
    admit_id, assign_id, accept_id = (str(uuid4()) for _ in range(3))
    admitted = _call(capsys, root, "task", "admit", "--title", "Review evidence",
                     "--body-file", str(body), "--repo", "owner/repo",
                     "--request-id", admit_id)
    task_id = admitted["data"]["task_id"]
    assert admitted["request_id"] == admit_id and admitted["release_id"] == release.release_id
    assert admitted["data"]["state"] == "queued"
    assert admitted["data"]["fleet"] == "example" and admitted["data"]["replayed"] is False
    assert admitted["data"]["recording"] == "committed"
    assert admitted["data"]["assignment_id"] is None
    assert admitted["data"]["delivery"] == "not_requested"
    before_replay = _counts(root)
    again = _call(capsys, root, "task", "admit", "--title", "Review evidence",
                  "--body-file", str(body), "--repo", "owner/repo", "--request-id", admit_id)
    assert again["data"]["outcome"] == again["data"]["recording"] == "unchanged"
    assert again["data"]["replayed"] is True and again["data"]["task_id"] == task_id
    assert _counts(root) == before_replay and before_replay[2:] == (0, 0, 0)
    assert main(["--root", str(root), "task", "admit", "--title", "Review evidence",
                 "--body-file", str(body), "--repo", "owner/repo", "--request-id", admit_id]) == 0
    plain = capsys.readouterr().out
    assert plain.count("\n") == 1 and task_id in plain
    assert "recording=unchanged" in plain and "delivery=not_requested" in plain
    assert "notification=not_requested" in plain and _counts(root) == before_replay

    assigned = _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
                     "--request-id", assign_id)
    assignment_id = assigned["data"]["assignment_id"]
    assert assigned["request_id"] == assign_id and assigned["data"]["state"] == "assigned"
    assert assigned["data"]["delivery"] == "not_requested"
    assert _counts(root)[2:] == (1, 0, 0)
    before_wrong_refs = _counts(root)
    wrong_task = _call(capsys, root, "task", "assign", assignment_id, "--bot", "worker",
                       "--request-id", str(uuid4()), expected=4)
    assert wrong_task["error"]["code"] == "wrong_reference"
    assert wrong_task["error"]["hint"]["next_command"] == ["task", "show", task_id]
    wrong_assignment = _call(capsys, root, "assignment", "accept", task_id,
                             "--request-id", str(uuid4()), expected=4)
    assert wrong_assignment["error"]["code"] == "wrong_reference"
    assert wrong_assignment["error"]["hint"]["next_command"] == [
        "assignment", "show", assignment_id]
    assert _counts(root) == before_wrong_refs

    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", "r-" + "0" * 64)
    before_stale = _counts(root)
    stale = _call(capsys, root, "assignment", "accept", assignment_id,
                  "--request-id", accept_id, expected=7)
    assert stale["error"]["code"] == "release_mismatch" and _counts(root) == before_stale
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    accepted = _call(capsys, root, "assignment", "accept", assignment_id,
                     "--request-id", accept_id)
    assert accepted["request_id"] == accept_id and accepted["release_id"] == release.release_id
    assert accepted["data"]["task_id"] == task_id
    assert accepted["data"]["assignment_id"] == assignment_id
    assert accepted["data"]["state"] == "active"
    assert accepted["data"]["delivery"] == "not_requested"
    assert _counts(root)[2:] == (1, 0, 1)


def test_bad_inputs_and_wrong_executable_refuse_before_identity_or_task_writes(active, monkeypatch, capsys,
                                                                               tmp_path):  # noqa: F811
    root, release = active
    before = _counts(root)
    _call(capsys, root, "task", "admit", "--title", "Valid", "--request-id", "not-a-uuid",
          expected=2)
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    _call(capsys, root, "task", "admit", "--title", "Valid", "--body-file", str(invalid),
          "--request-id", str(uuid4()), expected=2)
    assert _counts(root) == before

    monkeypatch.setattr(runtime_admission.RuntimeIdentity, "current", classmethod(lambda cls:
        runtime_admission.RuntimeIdentity(release.cli_path.with_name("other-cli"),
                                          release.native_path, release.inputs.artifact_id)))
    wrong = _call(capsys, root, "task", "admit", "--title", "Valid",
                  "--request-id", str(uuid4()), expected=7)
    assert wrong["error"]["code"] == "release_mismatch"
    assert _counts(root) == before


def test_reassign_closes_predecessor_and_replay_keeps_exact_provenance(active, capsys):
    root, release = active
    admitted = _call(capsys, root, "task", "admit", "--title", "Route private work",
                     "--request-id", str(uuid4()))
    task_id = admitted["data"]["task_id"]
    first = _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
                  "--request-id", str(uuid4()))
    original = first["data"]["assignment_id"]
    request_id = str(uuid4())
    before = _counts(root)
    argv = ("task", "reassign", task_id, "--bot", "manager", "--reason", "Needs manager",
            "--expected-by", "2026-10-01T00:00:00Z", "--by", "bot:example/manager",
            "--request-id", request_id)
    routed = _call(capsys, root, *argv)
    successor = routed["data"]["assignment_id"]
    assert routed["release_id"] == release.release_id
    assert routed["data"]["state"] == "assigned" and successor != original
    assert routed["data"]["recording"] == "committed"
    assert routed["data"]["delivery"] == routed["data"]["notification"] == "not_requested"
    assert _counts(root)[2:] == (before[2] + 1, 0, before[4] + 1)
    shown = _call(capsys, root, "task", "show", task_id)["data"]["task"]
    with sqlite3.connect(db_file(root)) as conn:
        manager_uid = conn.execute("SELECT uid FROM identity_registry WHERE kind='actor' AND alias=?",
                                   ("bot:example/manager",)).fetchone()[0]
    assert shown["current_assignment"]["assignment_id"] == successor
    assert shown["current_assignment"]["assigned_by_uid"] == manager_uid
    prior = next(item for item in shown["assignments"] if item["assignment_id"] == original)
    assert prior["state"] == "closed"
    assert prior["terminal_event"]["event"] == "reassigned"
    assert prior["terminal_event"]["successor_id"] == successor
    assert prior["terminal_event"]["actor_uid"] == manager_uid

    after = _counts(root)
    replay = _call(capsys, root, *argv)
    assert replay["data"]["replayed"] is True and replay["data"]["assignment_id"] == successor
    assert _counts(root) == after
    changed_by = _call(capsys, root, "task", "reassign", task_id, "--bot", "manager",
                       "--reason", "Needs manager", "--expected-by", "2026-10-01T00:00:00Z",
                       "--request-id", request_id, expected=4)
    assert changed_by["error"]["code"] == "conflict" and _counts(root) == after
    foreign = _call(capsys, root, "task", "reassign", task_id, "--bot", "worker",
                    "--reason", "Wrong attribution", "--by", "bot:elsewhere/manager",
                    "--request-id", str(uuid4()), expected=4)
    assert foreign["error"]["code"] == "conflict" and _counts(root) == after


def test_withdraw_closes_queued_task_and_retains_caller_separate_from_by(active, capsys):
    root, _ = active
    admitted = _call(capsys, root, "task", "admit", "--title", "Cancel private work",
                     "--request-id", str(uuid4()))
    task_id = admitted["data"]["task_id"]
    request_id = str(uuid4())
    argv = ("task", "withdraw", task_id, "--reason", "Work superseded",
            "--by", "bot:example/manager", "--request-id", request_id)
    before = _counts(root)
    withdrawn = _call(capsys, root, *argv)
    assert withdrawn["data"]["state"] == "cancelled"
    assert withdrawn["data"]["assignment_id"] is None
    assert withdrawn["data"]["delivery"] == "not_requested"
    assert _counts(root)[2:] == (0, 0, before[4] + 1)
    shown = _call(capsys, root, "task", "show", task_id)["data"]["task"]
    with sqlite3.connect(db_file(root)) as conn:
        manager_uid = conn.execute("SELECT uid FROM identity_registry WHERE kind='actor' AND alias=?",
                                   ("bot:example/manager",)).fetchone()[0]
    assert shown["terminal_event"]["actor_uid"] == manager_uid
    receipt = next((root / "state/requests").glob(f"*/{request_id}.json"))
    assert json.loads(receipt.read_text())["intent"]["caller_uid"] == shown["created_by_uid"]
    assert shown["created_by_uid"] != manager_uid

    after = _counts(root)
    assert _call(capsys, root, *argv)["data"]["replayed"] is True
    assert _counts(root) == after


def test_withdrawn_assignment_is_absent_from_the_pulse_overdue_read(active, capsys):
    root, _ = active
    due = datetime.now(timezone.utc) - timedelta(hours=1)
    task_id = _call(capsys, root, "task", "admit", "--title", "Cancel dispatched work",
                    "--request-id", str(uuid4()))["data"]["task_id"]
    _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
          "--expected-by", due.isoformat(), "--request-id", str(uuid4()))
    doors = load_lib_module("dispatch-overdue")
    now = int(datetime.now(timezone.utc).timestamp())
    assert doors.overdue_all(now, max_age=0, fleet="example", root=str(root)).get("worker")
    _call(capsys, root, "task", "withdraw", task_id, "--reason", "Cancelled by manager",
          "--request-id", str(uuid4()))
    # Read after the withdrawal, even when it crosses a wall-clock second.
    now = int(datetime.now(timezone.utc).timestamp())
    assert not doors.overdue_all(now, max_age=0, fleet="example", root=str(root)).get("worker")


def test_assign_defaults_to_the_fleet_deadline_unless_explicitly_open_ended(active, capsys):
    root, _ = active
    for bot, deadline in (("worker", ()), ("manager", ("--expected-by", "none"))):
        task_id = _call(capsys, root, "task", "admit", "--title", "Deadline " + bot,
                        "--request-id", str(uuid4()))["data"]["task_id"]
        _call(capsys, root, "task", "assign", task_id, "--bot", bot, *deadline, "--request-id", str(uuid4()))
    later = int(datetime.now(timezone.utc).timestamp()) + 2 * 86400
    overdue = load_lib_module("dispatch-overdue").overdue_all(later, max_age=0, fleet="example", root=str(root))
    assert overdue.get("worker") and not overdue.get("manager")


def test_escalate_queued_task_is_visible_and_replay_does_not_move_it(active, capsys):
    root, release = active
    admitted = _call(capsys, root, "task", "admit", "--title", "Need human guidance",
                     "--request-id", str(uuid4()))
    task_id = admitted["data"]["task_id"]
    request_id = str(uuid4())
    argv = ("task", "escalate", task_id, "--question", "Which project owns this?",
            "--by", "bot:example/manager", "--request-id", request_id)
    before = _counts(root)
    raised = _call(capsys, root, *argv)
    assert raised["release_id"] == release.release_id
    assert raised["data"]["state"] == "queued" and raised["data"]["assignment_id"] is None
    assert raised["data"]["delivery"] == raised["data"]["notification"] == "not_requested"
    assert _counts(root)[2:] == (0, 0, before[4] + 1)
    shown = _call(capsys, root, "task", "show", task_id)["data"]["task"]
    assert shown["history"][-1]["event"] == "escalated"
    with sqlite3.connect(db_file(root)) as conn:
        detail = conn.execute("SELECT assignment_id, detail FROM events "
                              "WHERE kind='task' AND event='escalated' AND work_item_id=?",
                              (task_id,)).fetchone()
    assert detail[0] is None and json.loads(detail[1])["question"] == "Which project owns this?"
    assert json.loads(detail[1])["by"] == "bot:example/manager"
    routed = _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
                   "--request-id", str(uuid4()))
    after = _counts(root)
    replay = _call(capsys, root, *argv)
    assert replay["data"]["replayed"] and replay["data"]["assignment_id"] is None
    assert replay["data"]["state"] == "assigned" and _counts(root) == after
    wrong = _call(capsys, root, "task", "escalate", routed["data"]["assignment_id"],
                  "--question", "Wrong reference", "--request-id", str(uuid4()), expected=4)
    assert wrong["error"]["code"] == "wrong_reference"
    assert wrong["error"]["hint"]["next_command"] == ["task", "show", task_id]
    assert _counts(root) == after


def test_linked_reports_commit_before_notification_and_replay_never_resends(active, monkeypatch, capsys):
    root, release = active
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
                       "FLEET_NAME": "example",
                       "CLAUDLOBBY_RELEASE_ID": release.release_id}.items():
        monkeypatch.setenv(key, value)
    def generated(bot):
        monkeypatch.setenv("BOT_ID", bot)
        monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots" / bot))

    calls = []
    strict = message_operations.send_committed_native_attempt
    digest = "sha256:" + "a" * 64

    def send_once(*args, **kwargs):
        def native(*_, **options):
            calls.append(options["body"])
            return TransportOutcome("submitted", digest, 99, 0)
        return strict(*args, transport=native, **kwargs)

    monkeypatch.setattr(message_operations, "send_committed_native_attempt", send_once)

    def assigned(title):
        generated("manager")
        task = _call(capsys, root, "task", "admit", "--title", title,
                     "--request-id", str(uuid4()))["data"]["task_id"]
        assignment = _call(capsys, root, "task", "assign", task, "--bot", "worker",
                           "--request-id", str(uuid4()))["data"]["assignment_id"]
        generated("worker")
        return task, assignment

    task, assignment = assigned("Report actual work")
    before_invalid = _counts(root)
    invalid = _call(capsys, root, "assignment", "progress", assignment,
                    "--summary", "Started", "--pr-role", "reviewed",
                    "--request-id", str(uuid4()), expected=2)
    assert invalid["error"]["code"] == "invalid_argument"
    assert _counts(root) == before_invalid and calls == []
    report_id = str(uuid4())
    progress = ("assignment", "progress", assignment, "--summary", "Started work",
                "--percent", "0", "--pr", "https://example.org/pull/1",
                "--pr-role", "reviewed", "--artifact", "https://example.org/artifact",
                "--issue", "https://example.org/issue", "--skill", "review",
                "--request-id", report_id)
    first = _call(capsys, root, *progress, expected=5)
    assert first["error"]["code"] == "notification_failed"
    assert first["data"]["task_id"] == task and first["data"]["assignment_id"] == assignment
    assert first["data"]["state"] == "active" and first["data"]["recording"] == "committed"
    assert first["data"]["notification"] == first["data"]["transport"] == "submitted"
    assert first["data"]["request_persisted"] is True and len(calls) == 1
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("SELECT count(*) FROM communications WHERE msg_id=?",
                            (first["data"]["message_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events WHERE kind='task' AND source_ref=?",
                            ("report:" + first["data"]["message_id"],)).fetchone()[0] == 1
    receipt_path = next((root / "state/requests").glob(f"*/{report_id}.json"))
    retained = json.loads(receipt_path.read_text())
    assert retained["intent"]["route"]["recipient_alias"] == "bot:example/manager"
    assert b"Started work" not in receipt_path.read_bytes()
    assert _call(capsys, root, *progress, expected=5)["data"]["replayed"] is True
    assert len(calls) == 1

    with locked_request(root, retained["intent"]["fleet_uid"], report_id) as store:
        attempt = store.load().message_attempts[0].attempt_no
    emit_batch(root, [{"event_type": "transmission", "fleet": "example",
                       "emitter": "linked-report-test",
                       "payload": {"msg_id": first["data"]["message_id"],
                                   "attempt_no": attempt, "carrier": "tmux",
                                   "destination": "bot:example/manager", "state": "received",
                                   "received_sha256": digest, "received_bytes": 99}}], require_commit=True)
    proved = _call(capsys, root, *progress)
    assert proved["data"]["notification"] == "received"
    assert proved["data"]["integrity_verdict"] == "delivered"
    assert proved["data"]["replayed"] is True and len(calls) == 1

    for verb, field, text, expected_state in (
        ("block", "--reason", "Need input", "blocked"),
        ("return", "--reason", "Cannot continue", "queued"),
    ):
        output = _call(capsys, root, "assignment", verb, assignment, field, text,
                       "--request-id", str(uuid4()), expected=5)
        assert output["data"]["state"] == expected_state
        assert output["data"]["recording"] == "committed"
    for verb, field, text, expected_state in (
        ("complete", "--summary", "Finished", "completed"),
        ("fail", "--reason", "Cannot finish", "failed"),
    ):
        _, other_assignment = assigned("Separate " + verb)
        output = _call(capsys, root, "assignment", verb, other_assignment, field, text,
                       "--request-id", str(uuid4()), expected=5)
        assert output["data"]["state"] == expected_state
        assert output["data"]["recording"] == "committed"
    assert len(calls) == 5

    committed_task, committed_assignment = assigned("Receipt outcome failure")
    committed_id = str(uuid4())
    command = ("assignment", "progress", committed_assignment, "--summary", "Recorded first",
               "--request-id", committed_id)
    original_outcome = RequestStore.outcome
    with monkeypatch.context() as patch:
        def fail_after_commit(store, index, status):
            if index == 0 and store.load().intent.operation == "assignment.progress":
                raise OSError("private outcome write failure")
            return original_outcome(store, index, status)
        patch.setattr(RequestStore, "outcome", fail_after_commit)
        partial = _call(capsys, root, *command, expected=5)
    assert partial["data"]["recording"] == "committed"
    assert partial["data"]["task_id"] == committed_task
    assert partial["data"]["assignment_id"] == committed_assignment
    assert partial["data"]["message_id"].startswith("msg_")
    assert partial["data"]["notification"] == "pending"
    assert partial["data"]["transport"] == "not_attempted"
    assert partial["data"]["request_persisted"] is False and len(calls) == 5
    recovered = _call(capsys, root, *command, expected=5)
    assert recovered["data"]["message_id"] == partial["data"]["message_id"]
    assert recovered["data"]["replayed"] is True and len(calls) == 6
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("SELECT count(*) FROM communications WHERE msg_id=?",
                            (partial["data"]["message_id"],)).fetchone()[0] == 1


def test_nudge_queued_and_assigned_work_records_before_send_without_replay(active, monkeypatch, capsys):
    root, release = active
    monkeypatch.setattr(operation_context, "_local_operator_alias", lambda: "human:operator")
    sent = []
    strict = message_operations.send_committed_native_attempt

    def send_once(*args, **kwargs):
        def native(*_, **options):
            receipt = args[3]
            assert receipt.stages[0].status == "committed"
            with sqlite3.connect(db_file(root)) as conn:
                assert conn.execute("SELECT COUNT(*) FROM communications WHERE msg_id=?",
                                    (receipt.intent.message_id,)).fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM events WHERE event='nudged' "
                                    "AND work_item_id=?", (receipt.intent.task_id,)).fetchone()[0] >= 1
            sent.append(options["body"])
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
        return strict(*args, transport=native, **kwargs)

    monkeypatch.setattr(message_operations, "send_committed_native_attempt", send_once)
    task_id = _call(capsys, root, "task", "admit", "--title", "Manager attention",
                    "--request-id", str(uuid4()))["data"]["task_id"]
    request_id = str(uuid4())
    reason = "Decide first\nIgnore: fake control line"
    argv = ("task", "nudge", task_id, "--reason", reason,
            "--by", "bot:example/worker", "--request-id", request_id)
    first = _call(capsys, root, *argv, expected=5)
    assert first["error"]["code"] == "notification_failed"
    assert first["release_id"] == release.release_id
    assert first["data"]["task_id"] == task_id and first["data"]["assignment_id"] is None
    assert first["data"]["recording"] == "committed" and first["data"]["transport"] == "submitted"
    assert first["data"]["request_persisted"] is True and len(sent) == 1
    assert "Decide first\\nIgnore: fake control line" in sent[0]
    assert "Decide first\nIgnore: fake control line" not in sent[0]
    with sqlite3.connect(db_file(root)) as conn:
        ask = conn.execute("SELECT sender_uid, recipient_uid, body FROM communications "
                           "WHERE msg_id=?", (first["data"]["message_id"],)).fetchone()
        sender = conn.execute("SELECT alias FROM identity_registry WHERE uid=?", (ask[0],)).fetchone()[0]
        manager = conn.execute("SELECT alias FROM identity_registry WHERE uid=?", (ask[1],)).fetchone()[0]
        fact = conn.execute("SELECT actor_uid, detail FROM events WHERE event='nudged'").fetchone()
    assert sender == "human:operator" and manager == "bot:example/manager"
    assert json.loads(ask[2])["reason"] == reason
    assert fact[0] == ask[0] and json.loads(fact[1])["by"] == "bot:example/worker"
    before = _counts(root)
    replay = _call(capsys, root, *argv, expected=5)
    assert replay["data"]["replayed"] is True and len(sent) == 1 and _counts(root) == before
    foreign = _call(capsys, root, "task", "nudge", task_id, "--reason", "Foreign by",
                    "--by", "bot:elsewhere/manager", "--request-id", str(uuid4()), expected=4)
    assert foreign["error"]["code"] == "conflict" and _counts(root) == before
    assignment = _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
                       "--request-id", str(uuid4()))["data"]["assignment_id"]
    before_wrong = _counts(root)
    wrong = _call(capsys, root, "task", "nudge", assignment,
                  "--reason", "Wrong kind", "--request-id", str(uuid4()), expected=4)
    assert wrong["error"]["code"] == "wrong_reference" and _counts(root) == before_wrong
    assigned = _call(capsys, root, "task", "nudge", task_id, "--reason", "Check now",
                     "--request-id", str(uuid4()), expected=5)
    assert assigned["data"]["assignment_id"] == assignment and len(sent) == 2


def test_task_write_help_and_syntax_do_not_import_mutation_owner(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.endswith(".task_write") or name.endswith(".task_operations"):
            raise ImportError("task mutation owner is unavailable")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["task", "admit", "--help"])
        assert exited.value.code == 0
        assert "--request-id" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "assignment", "accept", "--bad=private-value"])
    output = capsys.readouterr().out
    assert exited.value.code == 2 and "private-value" not in output
    assert json.loads(output)["command"] == "assignment.accept"


def test_feedback_cli_binds_existing_explicit_human_and_original_receipt(active, monkeypatch, capsys):
    from tests.test_plane_owner_feedback import receiver
    root, _ = active
    monkeypatch.setattr(operation_context, '_local_operator_alias', lambda: 'human:reviewer')
    task_id = _call(capsys, root, 'task', 'admit', '--title', 'Review result',
                    '--request-id', str(uuid4()))['data']['task_id']
    monkeypatch.setattr(operation_context, '_local_operator_alias',
                        lambda: pytest.fail('feedback must not resolve an ambient actor'))
    calls, repairs = receiver(monkeypatch)
    before = _counts(root)
    bad = _call(capsys, root, 'task', 'feedback', task_id, '--actor', 'human:unregistered',
                '--expected-assignment', 'none', '--text', 'Comment', '--request-id', str(uuid4()), expected=4)
    assert bad['error']['code'] == 'conflict' and _counts(root) == before
    request = str(uuid4())
    argv = ('task', 'feedback', task_id, '--actor', 'human:reviewer', '--expected-assignment', 'none',
            '--text', '  Please consider café\nNext iteration  ', '--request-id', request)
    first = _call(capsys, root, *argv)
    assert first['command'] == 'task.feedback' and first['request_id'] == request
    assert first['data']['recording'] == 'committed' and first['data']['current_task_state'] == 'queued'
    assert first['data']['notification'] == 'received'
    replay = _call(capsys, root, *argv)
    assert replay['data']['replayed'] and replay['data']['message_id'] == first['data']['message_id']
    assert replay['data']['recording'] == 'committed'
    retained = _call(capsys, root, 'request', 'show', request)['data']['request']
    assert retained['operation'] == 'task.feedback' and retained['assignment_id'] is None
    assert len(calls) == 1 and repairs == []
    after = _counts(root)
    assert after[:3] == before[:3] and after[3] == before[3] + 1 and after[4] == before[4]
    monkeypatch.setenv('BOT_ID', 'worker')
    denied = _call(capsys, root, *argv, expected=4)
    assert denied['error']['code'] == 'conflict' and _counts(root) == after


@pytest.mark.parametrize('change', ['body', 'selection', 'actor'])
def test_feedback_cli_invalid_input_has_no_recording(active, monkeypatch, capsys, change):
    root, _ = active
    monkeypatch.setattr(operation_context, '_local_operator_alias', lambda: 'human:reviewer')
    task_id = _call(capsys, root, 'task', 'admit', '--title', 'Selected task',
                    '--request-id', str(uuid4()))['data']['task_id']
    before = _counts(root)
    result = _call(capsys, root, 'task', 'feedback', task_id,
        '--actor', 'bot:example/manager' if change == 'actor' else 'human:reviewer',
        '--expected-assignment', '' if change == 'selection' else 'none',
        '--text', 'x' * 16385 if change == 'body' else 'Comment', '--request-id', str(uuid4()), expected=2)
    assert result['error']['code'] == 'invalid_argument' and _counts(root) == before


@pytest.mark.parametrize('carrier', ['fleet-name', 'fleet-selector', 'timer', 'empty-timer', 'service'])
def test_feedback_cli_refuses_generated_carrier_with_existing_human(active, monkeypatch, capsys, carrier):
    from tests.test_plane_owner_feedback import receiver
    from claudlobby.active_config import resolve_active_context

    root, _ = active
    monkeypatch.setattr(operation_context, '_local_operator_alias', lambda: 'human:reviewer')
    task_id = _call(capsys, root, 'task', 'admit', '--title', 'Existing human selected task',
                    '--request-id', str(uuid4()))['data']['task_id']
    selected = resolve_active_context(root=root, fleet='example', package=context.get_resources())
    calls, repairs = receiver(monkeypatch)
    if carrier in {'fleet-name', 'fleet-selector'}:
        monkeypatch.setenv('CLAUDLOBBY_ROOT', str(root))
        monkeypatch.setenv('FLEET_ROOT', str(selected.paths.fleet_config_dir))
        monkeypatch.setenv('FLEET_NAME' if carrier == 'fleet-name' else 'CLAUDLOBBY_FLEET', 'example')
    elif carrier in {'timer', 'empty-timer'}:
        monkeypatch.setenv('CLAUDLOBBY_TIMER_CONTEXT', 'fleet' if carrier == 'timer' else '')
    else:
        monkeypatch.setenv('BOT_SERVICE', 'fixture-worker')
    before = _counts(root)
    result = _call(capsys, root, 'task', 'feedback', task_id, '--actor', 'human:reviewer',
                   '--expected-assignment', 'none', '--text', 'A timer must not impersonate this human',
                   '--request-id', str(uuid4()), expected=4)
    assert result['error']['code'] == 'conflict'
    assert result['error']['message'] == 'task feedback requires an explicit local human caller'
    assert _counts(root) == before and calls == repairs == []


@pytest.mark.parametrize('selector', ['FLEET_NAME', 'CLAUDLOBBY_FLEET'])
def test_feedback_cli_allows_manual_root_and_fleet_selectors(active, monkeypatch, capsys, selector):
    from tests.test_plane_owner_feedback import receiver

    root, _ = active
    monkeypatch.setattr(operation_context, '_local_operator_alias', lambda: 'human:reviewer')
    task_id = _call(capsys, root, 'task', 'admit', '--title', 'Manual human selected task',
                    '--request-id', str(uuid4()))['data']['task_id']
    monkeypatch.setenv('CLAUDLOBBY_ROOT', str(root))
    monkeypatch.setenv(selector, 'example')
    calls, repairs = receiver(monkeypatch)
    result = _call(capsys, root, 'task', 'feedback', task_id, '--actor', 'human:reviewer',
                   '--expected-assignment', 'none', '--text', 'Explicit local operator comment',
                   '--request-id', str(uuid4()))
    assert result['data']['recording'] == 'committed' and result['data']['notification'] == 'received'
    assert len(calls) == 1 and repairs == []
