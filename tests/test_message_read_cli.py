"""Held public message-read grammar and facade over an active private Plane."""

import builtins
import json
import sqlite3
import stat

import pytest

from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
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


def _readers_for_test_process(package, monkeypatch):
    """Keep native reader imports beside the package pytest actually executes.

    current_runtime supplies the real private Git/config authority; its minimal
    Python tree is not the interpreter running these in-process CLI tests.
    """
    from pathlib import Path
    from claudlobby import brief
    from claudlobby.paths import load_lib_module

    native = Path(brief.__file__).resolve().parent / "_runtime_scripts"
    for name in ("dispatch-overdue.py", "plane-readers.py"):
        assert (native / name).read_bytes() == (package.runtime_native / name).read_bytes()
    def doors(paths):
        assert paths.package == package
        return load_lib_module(native, "dispatch-overdue.py")
    monkeypatch.setattr(brief, "load_dispatch_doors", doors)


def test_report_list_pages_ingest_order_and_discloses_capture(active, monkeypatch, capsys):  # noqa: F811
    from claudlobby import brief
    from claudlobby.activation_identity import read_current_identity_bindings
    from claudlobby.report_cursors import decode_cursor
    from claudlobby.report_payload import ReportPayload, encode_report_facts

    root, host = active
    _readers_for_test_process(host.package, monkeypatch)
    _generated(monkeypatch, root, "manager")
    capture = root / "state/plane/capture.json"
    for number, at, privacy, report in (
        (1, "2026-09-28T12:00:00Z", "full", ReportPayload("completed", summary="Visible result")),
        (2, "2026-09-27T12:00:00Z", "metadata", ReportPayload(
            "failed", reason="Private reason", pr_url="https://github.com/o/r/pull/1",
            pr_role="reviewed")),
    ):
        capture.write_text(json.dumps({"*": privacy}))
        facts = encode_report_facts(
            report, fleet="example", sender="bot:example/worker",
            recipient="bot:example/manager", msg_id=f"msg_{number:032x}",
            event_ids=(f"ev_{number * 2:032x}", f"ev_{number * 2 + 1:032x}"),
            occurred_at=at)
        emit_batch(root, facts, require_commit=True)
    before = _counts(root)

    def listed(*flags, expected=0):
        assert main(["--root", str(root), "--json", "fleet", "reports", "list", *flags]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["command"] == "fleet.reports.list" and result["request_id"] is None
        assert result["release_id"] == None
        return result

    first = listed("--unacknowledged", "--limit", "1")["data"]
    assert [row["message_id"] for row in first["items"]] == [f"msg_{1:032x}"]
    first_item = first["items"][0]
    assert set(first_item) == {"message_id", "author", "occurred_at", "ingest_seq",
                               "task_id", "historical_task_reference", "assignment_id",
                               "task_event", "status", "summary", "report", "evidence"}
    assert (first_item["author"], first_item["task_id"], first_item["status"]) == (
        "worker", None, "completed")
    assert first_item["report"]["state"] == "captured"
    assert first["next_cursor"] == first["ack_cursor"] and first["ack_available"] is True
    key = root / "state/report-cursor.key"
    assert key.is_file() and stat.S_IMODE(key.stat().st_mode) == 0o600
    bindings = read_current_identity_bindings(root, "example", package=host.package)
    identity = {"host_uid": bindings["host_uid"], "fleet_uid": bindings["fleet_uid"],
                "viewer_uid": first["viewer_uid"], "source_revision": host.snapshot["revision"],
                "config_fingerprint": host.snapshot["fingerprint"]}
    second = listed("--unacknowledged", "--limit", "1", "--cursor", first["next_cursor"])["data"]
    assert [row["message_id"] for row in second["items"]] == [f"msg_{2:032x}"]
    assert second["items"][0]["report"]["state"] == "withheld"
    assert second["items"][0]["summary"] == "" and second["next_cursor"] is None
    assert second["ack_cursor"] and second["ack_available"] is True
    assert second["items"][0]["historical_task_reference"] is None
    assert second["items"][0]["evidence"]["pr_url"] == "https://github.com/o/r/pull/1"
    assert second["items"][0]["evidence"]["pr_role"] == "reviewed"
    assert "Private reason" not in json.dumps(second)
    prefix = decode_cursor(root, second["ack_cursor"], identity=identity)
    assert (prefix.count, prefix.prior_seq, prefix.prior_ack_seq,
            prefix.through_seq, prefix.through_message_id, prefix.through_ts) == (
                2, None, None, second["items"][0]["ingest_seq"],
                second["items"][0]["message_id"], second["items"][0]["occurred_at"])
    tampered = ("A" if first["ack_cursor"][0] != "A" else "B") + first["ack_cursor"][1:]
    assert listed("--unacknowledged", "--cursor", tampered,
                  expected=2)["error"]["code"] == "invalid_argument"
    assert listed("--unacknowledged", "--status", "failed")["data"]["ack_cursor"] is None
    assert listed("--status", "failed", "--limit", "1", "--cursor", first["next_cursor"],
                  expected=2)["error"]["code"] == "invalid_argument"

    for key in ("BOT_ID", "FLEET_NAME", "CLAUDLOBBY_ROOT"):
        monkeypatch.delenv(key)
    ordinary_page = listed("--limit", "1")["data"]
    assert ordinary_page["viewer"] is ordinary_page["viewer_uid"] is None
    assert ordinary_page["ack_cursor"] is None and ordinary_page["ack_available"] is False
    assert [row["message_id"] for row in ordinary_page["items"]] == [f"msg_{1:032x}"]
    assert ordinary_page["next_cursor"]
    ordinary_next = listed("--limit", "1", "--cursor", ordinary_page["next_cursor"])["data"]
    assert [row["message_id"] for row in ordinary_next["items"]] == [f"msg_{2:032x}"]
    ordinary = listed("--status", "failed")["data"]
    assert ordinary["viewer"] is ordinary["viewer_uid"] is None
    assert [row["message_id"] for row in ordinary["items"]] == [f"msg_{2:032x}"]
    assert ordinary["items"][0]["report"]["state"] == "withheld"
    assert ordinary["ack_cursor"] is None and ordinary["ack_available"] is False
    assert main(["--root", str(root), "fleet", "reports", "list", "--status", "failed"]) == 0
    plain = capsys.readouterr().out
    assert "[content withheld]" in plain and "capture policy" in plain
    assert "future records" in plain and "cannot recover" in plain
    assert "Private reason" not in plain
    empty = listed("--since", "2026-09-29T00:00:00Z")["data"]
    assert empty["items"] == [] and empty["next_cursor"] is None
    with monkeypatch.context() as patch:
        patch.setattr(brief, "plane_session", lambda paths, fleet: (None, "plane db unreadable: SECRET-storage-error"))
        unavailable = listed("--since", "2026-09-29T00:00:00Z", expected=6)
    assert unavailable["ok"] is False and unavailable["error"]["code"] == "unavailable"
    assert unavailable["data"] == {}
    assert "unreadable" in unavailable["error"]["hint"]
    assert "plane status" in unavailable["error"]["hint"]
    assert "SECRET-storage-error" not in json.dumps(unavailable)
    for note, says in (("no plane db at /private/example", "No Plane database"),
                       ("the matcher installed at /private/example is unreadable", "Plane reader is unavailable"),
                       ("the plane at /private/example holds no bot of fleet 'example'", "no recorded bot roster")):
        with monkeypatch.context() as patch:
            patch.setattr(brief, "plane_session", lambda paths, fleet, note=note: (None, note))
            refusal = listed(expected=6)
        assert says in refusal["error"]["hint"] and "plane status" in refusal["error"]["hint"]
    refused = listed("--unacknowledged", expected=4)["error"]
    assert refused["code"] == "conflict" and "generated viewer" in refused["message"]
    _generated(monkeypatch, root, "manager")
    assert listed("--unacknowledged", "--cursor", ordinary_page["next_cursor"],
                  expected=2)["error"]["code"] == "invalid_argument"
    assert _counts(root) == before


def test_cross_fleet_participant_capture_and_receipt_proof(active, monkeypatch, capsys):  # noqa: F811
    root, host = active
    parent = _communication(root, 1, sender="bot:other/worker", recipient="bot:example/worker")
    before = _counts(root)
    _generated(monkeypatch, root, "worker")
    shown = _call(capsys, root, "show", parent)
    assert shown["release_id"] == None
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
    assert pending["error"]["retryable"] is True
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
    assert timed["release_id"] == None
    assert timed["data"] == {"message_id": parent, "reply": None,
        "source_revision": host.snapshot["revision"], "config_fingerprint": host.snapshot["fingerprint"]}
    assert timed["error"]["retryable"] is True
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
    for args in (("show", parent), ("receipt", parent)):
        unavailable = _call(capsys, root, *args, expected=6)
        assert unavailable["error"]["code"] == "unavailable"
        assert unavailable["error"]["retryable"] is True
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


def test_git_message_read_keeps_private_viewer_gate_and_honest_provenance(active, monkeypatch, capsys):
    from tests.test_task_read_cli import _use_git_current
    root = active[0]
    message_id = _communication(root, 902, sender="bot:example/manager", recipient="bot:example/worker")
    root, package, snapshot = _use_git_current(active, monkeypatch)
    _generated(monkeypatch, root, "worker")
    result = _call(capsys, root, "show", message_id)
    assert result["release_id"] is None
    assert result["data"]["config_fingerprint"] == snapshot["fingerprint"]
    assert result["data"]["source_revision"] == package.source_revision
    _generated(monkeypatch, root, "observer")
    denied = _call(capsys, root, "show", message_id, expected=4)
    assert denied["ok"] is False


def test_git_report_cursor_binds_revision_and_config_and_legacy_shape_is_unchanged(tmp_path):
    from claudlobby.report_cursors import _identity, CursorError
    git = {"host_uid": "host-example", "fleet_uid": "fleet-example", "viewer_uid": "actor-example",
           "source_revision": "a" * 40, "config_fingerprint": "b" * 64}
    assert _identity(git) == git
    legacy = {"host_uid": "host-example", "fleet_uid": "fleet-example", "viewer_uid": "actor-example",
              "release_id": "r-example", "activation_id": "a-example", "plan_id": "p-example"}
    assert _identity(legacy) == legacy
    with pytest.raises(CursorError):
        _identity({**git, "release_id": "r-example"})
    with pytest.raises(CursorError):
        _identity({**git, "config_fingerprint": None})


def test_git_report_pages_and_ack_bind_same_current_config_and_replay_once(active, monkeypatch, capsys):
    from uuid import uuid4
    from claudlobby import active_config
    from claudlobby.report_payload import ReportPayload, encode_report_facts
    from tests.test_task_read_cli import _use_git_current
    root = active[0]
    (root / "state/plane/capture.json").write_text('{"*":"full"}')
    for number in (910, 911):
        emit_batch(root, encode_report_facts(ReportPayload("completed", summary="Git report"),
            fleet="example", sender="bot:example/worker", recipient="bot:example/manager",
            msg_id=f"msg_{number:032x}", event_ids=(f"ev_{number * 2:032x}", f"ev_{number * 2 + 1:032x}"),
            occurred_at="2026-10-01T12:00:00Z"), require_commit=True)
    root, package, snapshot = _use_git_current(active, monkeypatch)
    _readers_for_test_process(package, monkeypatch)
    _generated(monkeypatch, root, "manager")
    def call(*args, expected=0):
        assert main(["--root", str(root), "--json", "fleet", "reports", *args]) == expected
        return json.loads(capsys.readouterr().out)
    first = call("list", "--unacknowledged", "--limit", "1")
    assert first["release_id"] is None and first["data"]["source_revision"] == package.source_revision
    second = call("list", "--unacknowledged", "--limit", "1", "--cursor", first["data"]["next_cursor"])
    assert len(second["data"]["items"]) == 1
    request = str(uuid4())
    cursor = second["data"]["ack_cursor"]
    ack = call("ack", "--through", cursor, "--request-id", request)
    assert ack["data"]["count"] == 2 and ack["data"]["replayed"] is False
    assert ack["data"]["config_fingerprint"] == snapshot["fingerprint"]
    replay = call("ack", "--through", cursor, "--request-id", request)
    assert replay["data"]["replayed"] is True
    changed = json.loads(json.dumps(snapshot))
    changed["fleets"]["example"]["fleet"] += "\n# next reviewed epoch\n"
    changed["fingerprint"] = active_config._fingerprint(changed)
    active_config.publish_current_config(changed, installation=package.installation,
                                         expected_previous=snapshot["fingerprint"])
    mismatch = call("list", "--unacknowledged", "--cursor", cursor, expected=2)
    assert mismatch["error"]["code"] == "invalid_argument"
