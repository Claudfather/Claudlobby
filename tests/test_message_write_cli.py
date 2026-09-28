"""First public ordinary send through a private active fleet and real Plane."""

import builtins
from dataclasses import replace
import json
import os
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import assignment_delivery, context, message_operations, task_operations
from claudlobby.__main__ import main
from claudlobby.message_queries import MessageIdentity, ReceiptObservation
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from claudlobby.plane.ids import mint_msg_id
from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
from tests.test_activation import cold, tmp_path  # noqa: F401 — short private activation root
from tests.test_releases import installed  # noqa: F401 — cold dependency
from tests.package_fixtures import source_package
from tests.test_task_read_cli import active  # noqa: F401 — active private Plane fixture


def _generated(monkeypatch, root, release, *, bot="manager"):
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / f"runtime/bots/{bot}"))
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", bot)
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    monkeypatch.setattr(context, "selected_cli", lambda: release.cli_path)
    package = source_package()
    monkeypatch.setattr(context, "get_resources", lambda: replace(
        package, native=release.native_path, artifact_id=release.inputs.artifact_id))


def _call(capsys, root, *args, expected):
    assert main(["--root", str(root), "--json", "message", "send", *args]) == expected
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "message.send" and output["schema_version"] == 1
    assert output["ok"] is (expected == 0)
    return output


def _reply_call(capsys, root, *args, expected):
    assert main(["--root", str(root), "--json", "message", "reply", *args]) == expected
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "message.reply" and output["schema_version"] == 1
    assert output["ok"] is (expected == 0)
    return output


def _delivery_call(capsys, root, *args, expected):
    assert main(["--root", str(root), "--json", "assignment", "deliver", *args]) == expected
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "assignment.deliver" and output["schema_version"] == 1
    assert output["ok"] is (expected == 0)
    return output


def _assigned(root):
    from claudlobby.operation_context import bind_task_context, resolve_operation_scope
    selected, origin = resolve_operation_scope(root=root)
    ctx = bind_task_context(selected, origin=origin)
    task = task_operations.admit(ctx, str(uuid4()), title="Private CLI delivery")
    assigned = task_operations.assign(ctx, str(uuid4()), task.task_id, bot_id="worker")
    return ctx, task, assigned


def _native(monkeypatch, calls, *, operation="send_message"):
    real = getattr(message_operations, operation)
    def wrapped(*args, **kwargs):
        def transport(*_, **native):
            calls.append(native["body"])
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
        return real(*args, transport=transport,
                    notify=lambda *a, **k: RecordingAlertOutcome(
                        ChannelOutcome("submitted", True), ChannelOutcome("unconfigured", True)),
                    clear=lambda *a, **k: None, **kwargs)
    monkeypatch.setattr(message_operations, operation, wrapped)


def _facts(root):
    with sqlite3.connect(db_file(root)) as conn:
        return (conn.execute("SELECT count(*) FROM communications").fetchone()[0],
                conn.execute("SELECT count(*) FROM events WHERE kind='transmission'").fetchone()[0])


def test_send_is_submitted_once_and_replay_waits_for_final_integrity(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    calls = []
    _native(monkeypatch, calls)
    request_id = str(uuid4())
    argv = ("--to", "worker", "--text", "Private SECRET message", "--request-id", request_id)
    first = _call(capsys, root, *argv, expected=5)
    assert first["release_id"] == host.release.release_id
    assert first["error"]["code"] == "delivery_unknown"
    assert first["data"]["recording"] == "committed"
    assert first["data"]["transport"] == first["data"]["delivery"] == "submitted"
    assert first["data"]["request_persisted"] and not first["data"]["replayed"]
    assert _facts(root) == (1, 1) and len(calls) == 1
    assert "Private SECRET message" not in json.dumps(first)
    receipt_file = next((root / "state/requests").glob(f"*/{request_id}.json"))
    assert b"Private SECRET message" not in receipt_file.read_bytes()

    replay = _call(capsys, root, *argv, expected=5)
    assert replay["data"]["message_id"] == first["data"]["message_id"]
    assert replay["data"]["replayed"] and len(calls) == 1 and _facts(root) == (1, 1)


def test_final_receipt_is_required_for_success_and_mismatch_fails(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    calls = []
    _native(monkeypatch, calls)
    from claudlobby import message_queries
    def observation(ctx, message_id, *, destination, wait):
        assert destination == "bot:example/worker" and 0 <= wait <= 60
        sender = MessageIdentity(ctx.caller.uid, ctx.caller.alias, ctx.caller_fleet_uid)
        peer = ctx.bots["worker"]
        recipient = MessageIdentity(peer.uid, peer.alias, ctx.fleet_uid)
        verdict = observation.verdict
        return ReceiptObservation(message_id, str(root), sender, recipient, "received",
                                  verdict, 0 if verdict == "delivered" else 10,
                                  None if verdict == "delivered" else "receipt_mismatch", None)
    monkeypatch.setattr(message_queries, "receipt", observation)
    observation.verdict = "delivered"
    good = _call(capsys, root, "--to", "worker", "--text", "Private body",
                 "--request-id", str(uuid4()), expected=0)
    assert good["data"]["delivery"] == "received"
    assert good["data"]["transport"] == "submitted"
    observation.verdict = "altered"
    bad = _call(capsys, root, "--to", "worker", "--text", "Other private body",
                "--request-id", str(uuid4()), expected=5)
    assert bad["error"]["code"] == "delivery_failed"
    assert bad["data"]["delivery"] == "failed" and len(calls) == 2


def test_recording_outage_keeps_submitted_effect_and_alert_truth(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    calls = []
    _native(monkeypatch, calls)
    original = message_operations.emit_batch
    def recorder(root_path, raws, **kwargs):
        if raws[0]["event_type"] == "communication":
            raise sqlite3.OperationalError("private recorder outage")
        return original(root_path, raws, **kwargs)
    monkeypatch.setattr(message_operations, "emit_batch", recorder)
    request_id = str(uuid4())
    output = _call(capsys, root, "--to", "worker", "--text", "Private O1 body",
                   "--request-id", request_id, expected=11)
    assert output["error"]["code"] == "recording_degraded"
    assert output["error"]["retryable"] is False
    assert output["data"]["recording"] == "unrecorded"
    assert output["data"]["transport"] == "submitted"
    assert output["data"]["alert"]["manager"]["status"] == "submitted"
    assert len(calls) == 1 and "Recording degraded" in calls[0]
    assert "Private O1 body" not in json.dumps(output)


def test_reply_requires_recorded_recipient_and_replays_to_parent_sender(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release)
    calls = []
    _native(monkeypatch, calls)
    parent = _call(capsys, root, "--to", "worker", "--text", "Private parent",
                   "--request-id", str(uuid4()), expected=5)["data"]["message_id"]
    reply_id = str(uuid4())
    args = (parent, "--text", "Private answer", "--request-id", reply_id)
    wrong_peer = _reply_call(capsys, root, *args, expected=4)
    assert wrong_peer["error"]["code"] == "conflict" and len(calls) == 1
    _generated(monkeypatch, root, host.release, bot="worker")
    missing = _reply_call(capsys, root, mint_msg_id(), "--text", "Private answer",
                          "--request-id", str(uuid4()), expected=3)
    assert missing["error"]["code"] == "not_found" and len(calls) == 1
    first = _reply_call(capsys, root, *args, expected=5)
    assert first["data"]["reply_to_message_id"] == parent
    assert first["data"]["destination"]["alias"] == "bot:example/manager"
    assert first["data"]["transport"] == "submitted" and len(calls) == 2
    replay = _reply_call(capsys, root, *args, expected=5)
    assert replay["data"]["replayed"] and replay["data"]["message_id"] == first["data"]["message_id"]
    assert len(calls) == 2 and "Private answer" not in json.dumps(replay)
    with sqlite3.connect(db_file(root)) as conn:
        row = conn.execute("SELECT message_class, reply_to_msg_id FROM communications WHERE msg_id=?",
                           (first["data"]["message_id"],)).fetchone()
    assert row == ("answer", parent)


def test_bad_context_release_and_file_refuse_before_native(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    root, host = active
    calls = []
    _native(monkeypatch, calls)
    no_origin = _call(capsys, root, "--to", "worker", "--text", "Valid",
                      "--request-id", str(uuid4()), expected=4)
    assert no_origin["error"]["code"] == "conflict" and calls == []
    _generated(monkeypatch, root, host.release)
    invalid = tmp_path / "invalid-message.txt"
    invalid.write_bytes(b"\xff")
    _call(capsys, root, "--to", "worker", "--file", str(invalid),
          "--request-id", str(uuid4()), expected=2)
    fifo = tmp_path / "message.fifo"
    os.mkfifo(fifo)
    _call(capsys, root, "--to", "worker", "--file", str(fifo),
          "--request-id", str(uuid4()), expected=2)
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", "r-" + "0" * 64)
    wrong = _call(capsys, root, "--to", "worker", "--text", "Valid",
                  "--request-id", str(uuid4()), expected=7)
    assert wrong["error"]["code"] == "release_mismatch" and calls == []


def test_unlinked_report_routes_to_own_manager_without_task_effect(active, monkeypatch, capsys):
    root, host = active
    _generated(monkeypatch, root, host.release, bot="worker")
    calls = []
    _native(monkeypatch, calls, operation="send_unlinked_report")
    args = ["--root", str(root), "--json", "fleet", "reports", "submit",
            "--status", "completed", "--summary", "Private unlinked result",
            "--artifact", "https://example.test/evidence", "--request-id", str(uuid4())]
    assert main(args + ["--percent", "50"]) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["command"] == "fleet.reports.submit" and calls == []
    assert main(args) == 5
    first = json.loads(capsys.readouterr().out)
    assert first["command"] == "fleet.reports.submit"
    assert first["data"]["destination"]["alias"] == "bot:example/manager"
    assert first["data"]["recording"] == "committed"
    assert first["data"]["report_status"] == "completed"
    assert first["data"]["task_id"] is first["data"]["assignment_id"] is None
    assert len(calls) == 1 and "Private unlinked result" not in json.dumps(first)
    assert main(args) == 5
    replay = json.loads(capsys.readouterr().out)
    assert replay["data"]["replayed"] and len(calls) == 1
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0] == 0
        row = conn.execute("SELECT message_class, work_item_id, assignment_id, body FROM communications "
                           "WHERE msg_id=?", (first["data"]["message_id"],)).fetchone()
        assert row[:3] == ("report", None, None)
        assert json.loads(row[3])["artifacts"] == ["https://example.test/evidence"]


def test_help_and_syntax_do_not_import_message_effect_owner(monkeypatch, capsys):
    original = builtins.__import__
    def reject(name, *args, **kwargs):
        if name.endswith(("message_write", "message_operations", "message_transport")):
            raise ImportError("message effect owner loaded during help")
        return original(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["message", "send", "--help"])
        assert exited.value.code == 0
        assert "--retry-uncertain" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "message", "send", "--bad=private-value"])
    output = json.loads(capsys.readouterr().out)
    assert exited.value.code == 2 and output["command"] == "message.send"
    assert "private-value" not in json.dumps(output)
    with pytest.raises(SystemExit) as exited:
        main(["--json", "fleet", "reports", "submit", "--bad=private-value"])
    output = json.loads(capsys.readouterr().out)
    assert exited.value.code == 2 and output["command"] == "fleet.reports.submit"
    assert "private-value" not in json.dumps(output)


def test_assignment_deliver_submits_once_and_requires_final_receipt(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    root, host = active
    _generated(monkeypatch, root, host.release)
    ctx, task, assigned = _assigned(root)
    body = tmp_path / "assignment.txt"
    body.write_text("Private assignment delivery", encoding="utf-8")
    calls = []
    real = assignment_delivery.deliver
    def injected(*args, **kwargs):
        def transport(*_, **native):
            calls.append(native["body"])
            return TransportOutcome("submitted", "sha256:" + "a" * 64, 99, 0)
        return real(*args, transport=transport, **kwargs)
    monkeypatch.setattr(assignment_delivery, "deliver", injected)
    argv = (assigned.assignment_id, "--file", str(body), "--request-id", str(uuid4()))
    first = _delivery_call(capsys, root, *argv, expected=5)
    assert first["error"]["code"] == "delivery_unknown"
    assert first["data"]["recording"] == "committed"
    assert first["data"]["transport"] == first["data"]["delivery"] == "submitted"
    assert first["data"]["task_id"] == task.task_id
    assert first["data"]["assignment_id"] == assigned.assignment_id
    assert first["data"]["request_persisted"] and len(calls) == 1
    assert "Private assignment delivery" not in json.dumps(first)
    stored = next((root / "state/requests").glob(f"*/{argv[-1]}.json"))
    assert b"Private assignment delivery" not in stored.read_bytes()
    replay = _delivery_call(capsys, root, *argv, expected=5)
    assert replay["data"]["replayed"] and replay["data"]["message_id"] == first["data"]["message_id"]
    assert len(calls) == 1 and _facts(root) == (1, 1)

    from claudlobby import message_queries
    def verified(query_ctx, message_id, *, destination, wait):
        assert message_id == first["data"]["message_id"]
        assert destination == "bot:example/worker" and 0 <= wait <= 60
        sender = MessageIdentity(query_ctx.caller.uid, query_ctx.caller.alias, query_ctx.caller_fleet_uid)
        peer = query_ctx.bots["worker"]
        recipient = MessageIdentity(peer.uid, peer.alias, query_ctx.fleet_uid)
        return ReceiptObservation(message_id, str(root), sender, recipient, "received",
                                  "delivered", 0, None, None)
    monkeypatch.setattr(message_queries, "receipt", verified)
    success = _delivery_call(capsys, root, *argv, expected=0)
    assert success["data"]["delivery"] == "received" and len(calls) == 1
    # A matching receiver proof does not restore a failed request-history write.
    def persistence_lost(*args, **kwargs):
        return replace(injected(*args, **kwargs), request_persisted=False)
    monkeypatch.setattr(assignment_delivery, "deliver", persistence_lost)
    partial = _delivery_call(capsys, root, *argv, expected=5)
    assert partial["data"]["delivery"] == "received"
    assert partial["data"]["request_persisted"] is False and len(calls) == 1


def test_assignment_deliver_refuses_stale_before_native(active, monkeypatch, capsys, tmp_path):  # noqa: F811
    root, host = active
    _generated(monkeypatch, root, host.release)
    ctx, task, assigned = _assigned(root)
    task_operations.withdraw(ctx, str(uuid4()), task.task_id, reason="No longer assigned")
    body = tmp_path / "assignment.txt"
    body.write_text("Private stale delivery", encoding="utf-8")
    result = _delivery_call(capsys, root, assigned.assignment_id, "--file", str(body),
                            "--request-id", str(uuid4()), expected=4)
    assert result["error"]["code"] == "conflict"
    assert _facts(root) == (0, 0)
