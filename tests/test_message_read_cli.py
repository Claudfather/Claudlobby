"""Held public message-read grammar and facade over an active private Plane."""

import builtins
import json
import sqlite3

import pytest

from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_read_cli import active  # noqa: F401 — active release fixture


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", "message", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["request_id"] is None
    assert result["command"] == "message." + argv[0]
    assert result["ok"] is (expected == 0)
    return result


def _generated(monkeypatch, root, bot):
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", bot)


def _communication(root, number, *, sender, recipient, reply_to=None, privacy="metadata"):
    message_id = f"msg_{number:032x}"
    # Capture is selected by host policy, never by a sender's payload field.
    (root / "state/plane/capture.json").write_text(json.dumps({"*": privacy}))
    payload = {"msg_id": message_id, "sender": sender, "recipient": recipient,
               "recipient_raw": recipient, "message_class": "chat", "body": "private message body",
               "privacy": "full"}
    if reply_to is not None:
        payload["reply_to_msg_id"] = reply_to
    emit_batch(root, [{"event_type": "communication", "fleet": sender.split(":", 1)[1].split("/", 1)[0],
                       "emitter": "message-read-fixture", "payload": payload}], require_commit=True)
    return message_id


def _transmission(root, message_id, *, fleet, state):
    digest = "sha256:" + "a" * 64
    payload = {"msg_id": message_id, "attempt_no": 1, "carrier": "tmux",
               "destination": "bot:example/worker", "state": state}
    if state == "received":
        payload.update(received_bytes=12, received_sha256=digest)
    else:
        payload.update(wire_bytes=12, wire_sha256=digest)
    emit_batch(root, [{"event_type": "transmission", "fleet": fleet,
                       "emitter": "message-read-fixture", "payload": payload}], require_commit=True)


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        return (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])


def test_cross_fleet_participant_capture_and_receipt_proof(active, monkeypatch, capsys):  # noqa: F811
    root, host = active
    parent = _communication(root, 1, sender="bot:other/worker", recipient="bot:example/worker")
    before = _counts(root)
    _generated(monkeypatch, root, "worker")
    shown = _call(capsys, root, "show", parent)
    assert shown["release_id"] == host.release.release_id
    message = shown["data"]["message"]
    assert message["sender"]["alias"] == "bot:other/worker"
    assert message["destination"]["alias"] == "bot:example/worker"
    assert message["content"] == "withheld" and message["body"] is None
    assert "private message body" not in json.dumps(shown)
    assert main(["--root", str(root), "message", "show", parent]) == 0
    plain = capsys.readouterr().out
    assert "[content withheld]" in plain and "private message body" not in plain

    _generated(monkeypatch, root, "manager")
    assert _call(capsys, root, "show", parent, expected=3)["error"]["code"] == "not_found"
    _generated(monkeypatch, root, "worker")
    no_history = _call(capsys, root, "receipt", parent, "--destination", "worker", expected=9)
    assert no_history["data"]["receipt_observation"] == "no_history"
    assert no_history["data"]["integrity_verdict"] == "unknown"
    assert no_history["data"]["root"] == str(root)
    assert _call(capsys, root, "receipt", parent, "--destination", "other/worker",
                 expected=2)["error"]["code"] == "invalid_argument"
    assert _counts(root) == before

    _transmission(root, parent, fleet="example", state="received")
    pending = _call(capsys, root, "receipt", parent, expected=8)
    assert (pending["data"]["receipt_observation"], pending["data"]["integrity_verdict"]) == (
        "received", "unconfirmed")
    _transmission(root, parent, fleet="other", state="pane_submitted")
    verified = _call(capsys, root, "receipt", parent)
    assert verified["data"]["integrity_verdict"] == "delivered"
    assert verified["data"]["sender"]["alias"] == "bot:other/worker"

    captured = _communication(root, 4, sender="bot:other/worker",
                              recipient="bot:example/worker", privacy="full")
    assert main(["--root", str(root), "message", "show", captured]) == 0
    assert "private message body" in capsys.readouterr().out


def test_reply_wait_facade_and_read_only_context_refusal(active, monkeypatch, capsys):  # noqa: F811
    from claudlobby import message_queries

    root, host = active
    parent = _communication(root, 2, sender="bot:other/worker", recipient="bot:example/worker")
    _generated(monkeypatch, root, "worker")
    with monkeypatch.context() as patch:
        patch.setattr(message_queries, "wait_for_reply", lambda _ctx, mid, *, timeout:
                      message_queries.ReplyObservation(mid, None, 8, "timeout", "no direct reply yet"))
        timed = _call(capsys, root, "wait", parent, "--for", "reply", "--timeout", "1", expected=8)
    assert timed["release_id"] == host.release.release_id
    assert timed["data"] == {"message_id": parent, "reply": None}
    reply = _communication(root, 3, sender="bot:example/worker", recipient="bot:other/worker",
                           reply_to=parent)
    observed = _call(capsys, root, "wait", parent, "--for", "reply", "--timeout", "1")
    assert observed["data"]["reply"]["message_id"] == reply

    monkeypatch.delenv("BOT_ID")
    monkeypatch.delenv("FLEET_NAME")
    monkeypatch.setattr("claudlobby.operation_context._local_operator_alias", lambda: "human:unrecorded")
    before = _counts(root)
    missing = _call(capsys, root, "show", parent, expected=6)
    assert missing["error"]["code"] == "unavailable"
    assert "do not register" in missing["error"]["message"]
    assert _counts(root) == before
    _generated(monkeypatch, root, "worker")
    offline = root / "plane-db-offline"
    db_file(root).rename(offline)
    assert _call(capsys, root, "show", parent, expected=6)["error"]["code"] == "unavailable"
    assert not db_file(root).exists()


def test_help_does_not_import_message_query_owner(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.endswith(("message_read", "message_queries")):
            raise ImportError("read dependency loaded during help")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["message", "receipt", "--help"])
        assert exited.value.code == 0
        assert "--wait" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exited:
        main(["--json", "message", "wait", "--bad=private-value"])
    assert exited.value.code == 2
    syntax = json.loads(capsys.readouterr().out)
    assert syntax["command"] == "message.wait"
    assert syntax["error"]["code"] == "invalid_argument"
    assert "private-value" not in json.dumps(syntax)
