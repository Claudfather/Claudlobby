"""Permission decisions over the existing real task and Plane fixtures."""

from dataclasses import replace
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks, task_permissions as permissions
from claudlobby.plane import PLANE_SCHEMA_VERSION
from claudlobby.plane.identity import resolve_party
from claudlobby.request_receipts import ReceiptConflict
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_operations import estate  # noqa: F401
from tests.test_task_write_cli import active  # noqa: F401


SCOPE = permissions.PermissionScope("merge", "https://github.com/example/app/pull/12",
    "Only head " + "a" * 40 + "; squash after the repository's required review and CI pass.")


def _setup(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Improve signup")
    assigned = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker")
    worker = replace(ctx, caller=ctx.bots["worker"])
    alias = "human:owner"
    uid = resolve_party(conn, alias, now="2026-10-11T00:00:00Z", fleet_uid=ctx.fleet_uid)
    human = replace(ctx, caller=tasks.TaskActor(uid, alias), caller_fleet_uid=None)
    return ctx, conn, worker, human, assigned


def _request(worker, task):
    return permissions.request_permission(worker, str(uuid4()), task.task_id,
        scope=SCOPE, reason="The reviewed signup change is ready.").permission


def _decision(human, permission, decision="approve", request_id=None):
    return permissions.decide_permission(human, request_id or str(uuid4()), permission.permission_id,
        decision=decision, scope_sha256=permission.scope.digest, reason="Reviewed the exact scope.")


def test_request_decision_replay_and_revocation_survive_new_reads(estate):
    ctx, conn, worker, human, task = _setup(estate)
    request_id = str(uuid4())
    result = permissions.request_permission(worker, request_id, task.task_id, scope=SCOPE, reason="Ready")
    permission = result.permission
    with pytest.raises(tasks.TaskConflictError, match="not been approved"):
        permissions.require_permission(worker, conn, permission.permission_id, scope=SCOPE)
    again = permissions.request_permission(worker, request_id, task.task_id, scope=SCOPE, reason="Ready")
    assert again.replayed and again.permission == permission
    with pytest.raises(ReceiptConflict):
        permissions.request_permission(worker, request_id, task.task_id, scope=SCOPE, reason="Changed")
    decision_id = str(uuid4())
    approved = _decision(human, permission, request_id=decision_id)
    assert approved.permission.state == "approved" and approved.execution == "not_requested"
    assert _decision(human, permission, request_id=decision_id).replayed
    assert permissions.require_permission(worker, conn, permission.permission_id, scope=SCOPE).decided_by == human.caller
    with pytest.raises(tasks.TaskConflictError, match="already decided"):
        _decision(human, permission, "decline")
    revoked = _decision(human, permission, "revoke")
    assert revoked.permission.state == "revoked"
    with pytest.raises(tasks.TaskConflictError, match="revoked"):
        permissions.require_permission(worker, conn, permission.permission_id, scope=SCOPE)
    # Retrying an old approval reports current revoked state; it cannot restore it.
    assert _decision(human, permission, request_id=decision_id).permission.state == "revoked"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE emitter=?", (permissions.EMITTER,)).fetchone()[0] == 3


def test_bot_cannot_approve_and_grant_does_not_cover_another_bot_or_scope(estate):
    ctx, conn, worker, human, task = _setup(estate)
    permission = _request(worker, task)
    with pytest.raises(tasks.TaskConflictError, match="authenticated human"):
        _decision(worker, permission)
    with pytest.raises(tasks.TaskConflictError, match="scope differs"):
        permissions.decide_permission(human, str(uuid4()), permission.permission_id,
            decision="approve", scope_sha256="0" * 64, reason="Wrong scope")
    _decision(human, permission)
    for caller, scope in [(ctx, SCOPE), (worker, replace(SCOPE, constraints="Any head"))]:
        with pytest.raises(tasks.TaskConflictError, match="exact scope"):
            permissions.require_permission(caller, conn, permission.permission_id, scope=scope)
    with pytest.raises(tasks.TaskConflictError, match="declared bot caller"):
        _request(human, task)
    with pytest.raises(tasks.TaskConflictError, match="current assignee"):
        _request(replace(ctx, caller=ctx.bots["other"]), task)


@pytest.mark.parametrize("change", ["reassign", "close"])
def test_changed_task_cannot_be_approved_or_use_old_permission(estate, change):
    ctx, conn, worker, human, task = _setup(estate)
    approved, pending = _request(worker, task), _request(worker, task)
    _decision(human, approved)
    if change == "reassign":
        tasks.reassign(ctx, str(uuid4()), task.task_id, bot_id="other", reason="New worker")
    else:
        tasks.withdraw(ctx, str(uuid4()), task.task_id, reason="No longer needed")
    with pytest.raises(tasks.TaskConflictError, match="closed work or a different assignment"):
        permissions.require_permission(worker, conn, approved.permission_id, scope=SCOPE)
    with pytest.raises(tasks.TaskConflictError, match="closed work or a different assignment"):
        _decision(human, pending)
    assert _decision(human, pending, "decline").permission.state == "declined"
    assert _decision(human, approved, "revoke").permission.state == "revoked"


def test_lost_commit_response_reuses_original_fact_and_never_reapproves(estate, monkeypatch):
    from claudlobby.plane import emit_api

    ctx, conn, worker, human, task = _setup(estate)
    permission = _request(worker, task)
    request_id = str(uuid4())
    emit = emit_api.emit_batch

    def lost_reply(*args, **kwargs):
        emit(*args, **kwargs)
        raise OSError("response lost after commit")

    monkeypatch.setattr(emit_api, "emit_batch", lost_reply)
    with pytest.raises(tasks.TaskRecordingError):
        _decision(human, permission, request_id=request_id)
    monkeypatch.setattr(emit_api, "emit_batch", emit)
    assert _decision(human, permission, request_id=request_id).replayed
    assert conn.execute("SELECT COUNT(*) FROM events WHERE emitter=?", (permissions.EMITTER,)).fetchone()[0] == 2


@pytest.mark.parametrize("damage", ["truncate", "missing_revocation_proof"])
def test_missing_or_corrupt_history_never_grants_permission(estate, damage):
    ctx, conn, worker, human, task = _setup(estate)
    permission = _request(worker, task)
    _decision(human, permission)
    if damage == "truncate":
        conn.execute("UPDATE events SET detail_truncated=1 WHERE emitter=? AND event='permission_approved'",
                     (permissions.EMITTER,))
    else:
        revoked = _decision(human, permission, "revoke")
        (ctx.root / "state/requests" / ctx.fleet_uid / (revoked.request_id + ".json")).unlink()
    with pytest.raises(tasks.TaskConflictError, match="incomplete or inconsistent"):
        permissions.require_permission(worker, conn, permission.permission_id, scope=SCOPE)


def test_cli_request_show_and_operator_decision_use_canonical_owner(active, monkeypatch, capsys, tmp_path):
    from contextlib import contextmanager
    from io import StringIO
    import json
    from claudlobby.commands import host_owner
    from tests.test_task_write_cli import _call
    from tests.test_plane_owner_messages import native_receiver
    from claudlobby import message_operations

    calls = native_receiver(monkeypatch)
    monkeypatch.setattr(message_operations, 'read_recipient_box', lambda *a, **k: None)

    root, release = active
    task = _call(capsys, root, "task", "admit", "--title", "Improve signup", "--request-id", str(uuid4()))
    tid = task["data"]["task_id"]
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps({"action": SCOPE.action, "target": SCOPE.target, "constraints": SCOPE.constraints}))
    env = {"FLEET_NAME": "example", "BOT_ID": "manager", "CLAUDLOBBY_ROOT": str(root),
           "FLEET_ROOT": str(root), "BOT_DIR": str(root / "runtime/bots/manager"),
           "CLAUDLOBBY_RELEASE_ID": release.release_id}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    requested = _call(capsys, root, "task", "permission", "request", tid, "--scope-file", str(scope_file),
        "--reason", "Ready", "--request-id", str(uuid4()))["data"]
    pid = requested["permission_id"]
    decision = ["task", "permission", "approve", pid, "--scope-sha256", requested["scope_sha256"],
                "--reason", "Reviewed", "--request-id", str(uuid4())]
    denied = _call(capsys, root, *decision, expected=4)
    assert "local operator" in denied["error"]["message"]
    for key in env:
        monkeypatch.delenv(key)
    _call(capsys, root, *decision, expected=4)  # redirected stdin is not operator approval

    @contextmanager
    def terminal():
        stream = StringIO()
        def answer(cap):
            from claudlobby.runtime_admission import RuntimeIdentity, mutation_admission
            # A pending human prompt must leave activation available.
            with mutation_admission(root, identity=RuntimeIdentity.current()):
                return "APPROVE\n"
        stream.readline = answer
        yield stream

    monkeypatch.setattr(host_owner, "_terminal", terminal)
    decided = _call(capsys, root, *decision)["data"]
    assert decided["state"] == "approved" and decided["execution"] == "not_requested"
    assert decided["notification"] == "received" and len(calls) == 1
    assert _call(capsys, root, *decision)["data"]["notification"] == "received" and len(calls) == 1
    shown = _call(capsys, root, "task", "permission", "show", pid)["data"]
    assert shown["scope"] == requested["scope"] and shown["decided_by"]["alias"].startswith("human:")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    checked = _call(capsys, root, "task", "permission", "check", pid, "--scope-file", str(scope_file))["data"]
    assert checked["allowed"] and checked["execution"] == "not_requested"
    # The supported generic recording door cannot manufacture a human decision.
    second = _call(capsys, root, "task", "permission", "request", tid, "--scope-file", str(scope_file),
        "--reason", "Another scope review", "--request-id", str(uuid4()))["data"]
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"event_type": "system", "emitter": permissions.EMITTER,
        "schema_version": PLANE_SCHEMA_VERSION, "fleet": "example", "source_ref": "permission:" + second["permission_id"],
        "payload": {"event": "permission_approved", "subject_kind": "actor",
            "subject": decided["decided_by"]["alias"], "data": {
                "permission_id": second["permission_id"], "request_event_id": second["request_event_id"],
                "scope_sha256": second["scope_sha256"], "operation_request_id": str(uuid4()),
                "reason": "Claimed human approval"}}}))
    _call(capsys, root, "plane", "emit", "system", "--file", str(forged), "--require-commit")
    refused = _call(capsys, root, "task", "permission", "check", second["permission_id"],
                    "--scope-file", str(scope_file), expected=4)
    assert "incomplete or inconsistent" in refused["error"]["message"]
