"""Permission decisions over the existing real task and Plane fixtures."""

from dataclasses import replace
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks, task_permissions as permissions
from claudlobby.plane import PLANE_SCHEMA_VERSION
from claudlobby.plane.identity import resolve_party
from claudlobby.request_receipts import ReceiptConflict
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
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
    from pathlib import Path
    from claudlobby.commands import host_owner
    from tests.test_host_owner import unit_ancestry_refuses
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
           "CLAUDLOBBY_INSTALLATION": str(release.directory)}
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
    with monkeypatch.context() as m:
        # Nor is a terminal inside a managed unit: unit ancestry refuses before
        # the terminal opens or the request is read.
        roots = unit_ancestry_refuses(m)
        refused = _call(capsys, root, *decision, expected=4)
    assert "outside a managed unit" in refused["error"]["message"]
    assert [Path(where).resolve() for where in roots] == [Path(root).resolve()]

    @contextmanager
    def terminal(where):
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


def test_pending_requests_surface_in_board_detail_and_team_counts(estate):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app
    from tests.package_fixtures import source_package

    ctx, conn, worker, human, assigned = _setup(estate)
    task = tasks.admit(ctx, str(uuid4()), title="Review release permission")
    client = TestClient(create_app(ctx.root, package=source_package()))

    def read():
        board = client.get("/api/tasks?fleet=example").json()["data"]
        card = next(row for row in board["tasks"] if row["task_id"] == task.task_id)
        detail = client.get("/api/tasks/" + task.task_id + "?fleet=example").json()["data"]["task"]
        overview = client.get("/api/overview").json()["data"]
        team = next(row for row in overview["fleets"] if row["alias"] == "example")
        assert team["attention"] == board["attention_count"]
        assert card["attention_reason"] == detail["attention_reason"]
        return card

    assert not read()["attention"]
    first, second = _request(ctx, task), _request(ctx, task)
    card = read()
    assert card["permission_pending"] == 2 and not card["permission_incomplete"]
    assert card["attention_reason"] == ["permission_pending"]
    _decision(human, first)
    assert read()["permission_pending"] == 1
    _decision(human, second, "decline")
    assert not read()["attention"]
    _decision(human, first, "revoke")
    assert not read()["attention"]


def test_permission_discovery_does_not_invent_decisions_or_hide_unknowns(estate, monkeypatch):
    from fastapi.testclient import TestClient
    from claudlobby.plane.view import create_app
    from tests.package_fixtures import source_package

    ctx, conn, worker, human, assigned = _setup(estate)
    task = tasks.admit(ctx, str(uuid4()), title="Review a bounded request")
    permission = _request(ctx, task)
    client = TestClient(create_app(ctx.root, package=source_package()))

    def card():
        rows = client.get("/api/tasks?fleet=example").json()["data"]["tasks"]
        return next(row for row in rows if row["task_id"] == task.task_id)

    approved = _decision(human, permission)
    assert not card()["attention"]
    # The request is genuine but its later decision can no longer be proven.
    proof = ctx.root / "state/requests" / ctx.fleet_uid / (approved.request_id + ".json")
    original = proof.read_bytes()
    proof.unlink()
    assert card()["attention_reason"] == ["permission_unavailable"]
    assert card()["permission_pending"] == 0
    proof.write_bytes(original)
    # A forged request without an owned command receipt never becomes a decision.
    requested = conn.execute("SELECT detail FROM events WHERE event_id=?", (permission.request_event_id,)).fetchone()[0]
    import json
    data = json.loads(requested)
    data["operation_request_id"] = str(uuid4())
    conn.execute("UPDATE events SET detail=? WHERE event_id=?", (json.dumps(data), permission.request_event_id))
    conn.commit()
    assert not card()["attention"]
    # A bounded canonical list explicitly discloses that older requests exist.
    monkeypatch.setattr("claudlobby.plane.view.list_task_permissions", lambda *a, **k: ((), True))
    assert card()["attention_reason"] == ["permission_unavailable"]


def test_git_permission_decision_provenance_survives_replay_and_legacy_digest_is_unchanged(estate):
    import json
    from claudlobby.request_receipts import semantic_digest
    ctx, conn, worker, human, task = _setup(estate)
    permission = _request(worker, task)
    reason = 'Reviewed original Git runtime'
    base = dict(permission_id=permission.permission_id, scope_sha256=permission.scope.digest, reason=reason)
    assert permissions.permission_decision_digest(**base) == semantic_digest(base)
    release = 'r-' + 'c' * 64
    assert permissions.permission_decision_digest(**base, release_id=release) == semantic_digest({**base, 'release_id': release})
    runtime = dict(release_id=None, source_revision='a' * 40, config_fingerprint='b' * 64)
    request = str(uuid4())
    result = permissions.decide_permission(human, request, permission.permission_id, decision='approve',
        scope_sha256=permission.scope.digest, reason=reason, **runtime)
    assert result.permission.state == 'approved' and result.execution == 'not_requested'
    row = conn.execute("SELECT detail FROM events WHERE emitter=? AND event='permission_approved'",
                       (permissions.EMITTER,)).fetchone()
    detail = json.loads(row[0])
    assert {key: detail[key] for key in runtime} == runtime
    assert permissions.decide_permission(human, request, permission.permission_id, decision='approve',
        scope_sha256=permission.scope.digest, reason=reason, **runtime).replayed
    with pytest.raises(ReceiptConflict):
        permissions.decide_permission(human, request, permission.permission_id, decision='approve',
            scope_sha256=permission.scope.digest, reason=reason,
            **{**runtime, 'source_revision': 'd' * 40})
    assert permissions.read_permission(conn, permission.permission_id, root=ctx.root,
        host_uid=ctx.host_uid, fleet_uid=ctx.fleet_uid).state == 'approved'


@pytest.mark.parametrize('runtime', [
    {'release_id': 'r-' + 'c' * 64, 'source_revision': 'a' * 40, 'config_fingerprint': 'b' * 64},
    {'source_revision': 'a' * 40}, {'config_fingerprint': 'b' * 64},
])
def test_permission_digest_refuses_mixed_or_partial_git_provenance(runtime):
    from claudlobby.task_queries import TaskQueryError
    with pytest.raises(TaskQueryError):
        permissions.permission_decision_digest('perm_' + '1' * 32, '2' * 64, 'Reviewed', **runtime)


def _git_cli(active, monkeypatch):
    """Commit private Git config and use real local admission, with synthetic process identity."""
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    from claudlobby import context, runtime_admission
    from claudlobby.installation import write_installation, install_launcher
    from tests.test_task_read_cli import _use_git_current
    root, _ = active
    root, package, snapshot = _use_git_current((root, SimpleNamespace(package=context.get_resources())), monkeypatch)
    write_installation(package.installation)
    install_launcher(package.installation, interpreter=Path(sys.executable).resolve())
    package = replace(package, native=package.installation.native)
    monkeypatch.setattr(context, 'get_resources', lambda: package)
    monkeypatch.setattr(context, 'selected_cli', lambda *, installation: installation.cli)
    identity = runtime_admission.RuntimeIdentity(package.installation.cli, package.native, '',
                                                 package.installation, package.source_revision)
    monkeypatch.setattr(runtime_admission.RuntimeIdentity, 'current', lambda: identity)
    return root, package, snapshot


@pytest.mark.parametrize('change_during_approval', [False, True])
def test_git_permission_cli_binds_full_runtime_across_terminal_approval(active, monkeypatch, capsys, tmp_path,
                                                                      change_during_approval):
    from contextlib import contextmanager
    from io import StringIO
    import json
    from claudlobby import active_config, message_operations
    from claudlobby.commands import host_owner
    from tests.test_task_write_cli import _call
    from tests.test_plane_owner_messages import native_receiver
    root, package, snapshot = _git_cli(active, monkeypatch)
    calls = native_receiver(monkeypatch)
    monkeypatch.setattr(message_operations, 'read_recipient_box', lambda *a, **k: None)
    task_id = _call(capsys, root, 'task', 'admit', '--title', 'Synthetic Git scope',
                    '--request-id', str(uuid4()))['data']['task_id']
    scope_file = tmp_path / 'scope.json'
    scope_file.write_text(json.dumps({'action': SCOPE.action, 'target': SCOPE.target, 'constraints': SCOPE.constraints}))
    generated = {'FLEET_NAME': 'example', 'BOT_ID': 'manager', 'CLAUDLOBBY_ROOT': str(root),
                 'FLEET_ROOT': str(root), 'BOT_DIR': str(root / 'runtime/bots/manager')}
    for key, value in generated.items():
        monkeypatch.setenv(key, value)
    requested = _call(capsys, root, 'task', 'permission', 'request', task_id, '--scope-file', str(scope_file),
                       '--reason', 'Ready', '--request-id', str(uuid4()))['data']
    for key in generated:
        monkeypatch.delenv(key)
    permission_id = requested['permission_id']
    shown = _call(capsys, root, 'task', 'permission', 'show', permission_id)
    assert shown['release_id'] is None
    assert shown['data']['config_fingerprint'] == snapshot['fingerprint']
    @contextmanager
    def terminal(_root):
        stream = StringIO()
        def answer(_cap):
            if change_during_approval:
                changed = json.loads(json.dumps(snapshot))
                changed['fleets']['example']['fleet'] += '\n# Changed during confirmation\n'
                changed['fingerprint'] = active_config._fingerprint(changed)
                active_config.publish_current_config(changed, installation=package.installation,
                                                     expected_previous=snapshot['fingerprint'])
            return 'APPROVE\n'
        stream.readline = answer
        yield stream
    monkeypatch.setattr(host_owner, '_terminal', terminal)
    request_id = str(uuid4())
    result = _call(capsys, root, 'task', 'permission', 'approve', permission_id,
        '--scope-sha256', requested['scope_sha256'], '--reason', 'Reviewed', '--request-id', request_id,
        expected=4 if change_during_approval else 0)
    if change_during_approval:
        assert not calls
        assert not list((root / 'state/requests').rglob(request_id + '.json'))
        assert 'confirmation' in result['error']['message']
    else:
        assert result['release_id'] is None and result['data']['state'] == 'approved' and len(calls) == 1
        from claudlobby.plane.db import connect_ro, db_file
        with connect_ro(db_file(root)) as conn:
            detail = json.loads(conn.execute("SELECT detail FROM events WHERE event='permission_approved'").fetchone()[0])
        assert detail['release_id'] is None and detail['source_revision'] == snapshot['revision']
        assert detail['config_fingerprint'] == snapshot['fingerprint']
        for key, value in generated.items():
            monkeypatch.setenv(key, value)
        checked = _call(capsys, root, 'task', 'permission', 'check', permission_id, '--scope-file', str(scope_file))
        assert checked['data']['allowed'] and checked['data']['source_revision'] == snapshot['revision']


def test_permission_show_refuses_uninitialized_current_identities(cold, capsys):
    from claudlobby import active_config
    from tests.test_task_write_cli import _call, _counts

    root, installation, _, host = cold
    snapshot = dict(host.snapshot, identities=None)
    snapshot["fingerprint"] = active_config._fingerprint(snapshot)
    active_config.publish_current_config(snapshot, installation=installation,
        expected_previous=host.snapshot["fingerprint"])
    before = _counts(root)
    result = _call(capsys, root, "task", "permission", "show", "perm_" + "1" * 32,
                   expected=4)
    assert result["error"]["code"] == "conflict"
    assert "identities are not initialized" in result["error"]["message"]
    assert _counts(root) == before
