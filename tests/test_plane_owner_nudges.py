"""Owner nudges over private activation/Plane, with a synthetic native receiver.

No installed CLI, browser authentication, real bot or native delivery is claimed.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import message_operations, message_queries, operation_context, task_operations
from claudlobby.command_result import CommandFailure
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane import owner_nudges
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef, VerifiedReader
from claudlobby.plane.owner_nudges import OwnerNudges
from claudlobby.plane.owner_source import SourceDenied, SourceUnavailable, bind_source
from claudlobby.request_queries import RequestNotFoundError
from claudlobby.request_receipts import ReceiptConflict, RequestStore
from claudlobby.runtime_admission import ReleaseMismatch
from claudlobby.task_queries import TaskQueryError
from claudlobby.task_state import TaskStateError
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401
from tests.test_releases import installed  # noqa: F401
from tests.test_task_read_cli import active  # noqa: F401
from tests.test_message_write_cli import _human


@pytest.fixture
def gateway(active, monkeypatch):  # noqa: F811
    root, host = active
    _human(monkeypatch, host.release)
    package = replace(source_package(), native=host.release.native_path,
                      artifact_id=host.release.inputs.artifact_id)
    ctx = operation_context.resolve_task_mutation_context(
        root=root, fleet="example", operator_alias="human:paired-owner", package=package)
    access = OwnerAccess.initialize(root)
    principal = PrincipalRef("synthetic-verifier", "owner-001")
    challenge = access.begin_pairing(principal)
    owner = access.confirm_pairing(challenge.token, expected_principal=principal)
    reader = VerifiedReader(principal, access.open_session(principal).token)
    grant = access.allow_nudges(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                               actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    task = task_operations.admit(ctx, str(uuid4()), title="Selected work")
    bind_source(root)
    adapter = OwnerNudges(root, package=package)
    options = dict(fleet="example", fleet_uid=ctx.fleet_uid, task_id=task.task_id,
        manager_uid=ctx.bots[ctx.context.fleet.manager].uid, request_id=str(uuid4()), expected_grant=grant)
    selection = dict(expected_assignment_id=None, expected_release_id=host.release.release_id)
    monkeypatch.setattr(operation_context, "_local_operator_alias",
                        lambda: pytest.fail("owner nudge must not use the OS account"))
    return adapter, reader, ctx, options, selection


def receiver(monkeypatch, *, received=True, altered=False):
    calls, repairs = [], []
    original = message_operations.send_committed_native_attempt
    def transport(package, destination, *, message_id, body):
        with sqlite3.connect(db_file(destination.root)) as conn:
            assert conn.execute("SELECT count(*) FROM events WHERE event='nudged'").fetchone()[0] == 1
            assert conn.execute("SELECT message_class FROM communications WHERE msg_id=?", (message_id,)).fetchone()[0] == "task_request"
        calls.append((message_id, body))
        wire = body.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(wire).hexdigest()
        if received:
            emit_batch(destination.root, [{"event_type": "transmission", "emitter": "synthetic-nudge-receiver",
                "event_id": "ev_" + uuid4().hex, "occurred_at": datetime.now(timezone.utc).isoformat(),
                "fleet": destination.fleet, "payload": {"msg_id": message_id, "attempt_no": 1,
                    "carrier": "tmux", "destination": f"bot:{destination.fleet}/{destination.session}",
                    "state": "received", "received_bytes": len(wire),
                    "received_sha256": "sha256:" + "0" * 64 if altered else digest}}])
        return TransportOutcome("submitted", digest, len(wire), 0)
    monkeypatch.setattr(message_operations, "send_committed_native_attempt",
                        lambda *a, **k: original(*a, **k, transport=transport))
    monkeypatch.setattr(message_operations, "read_recipient_box", lambda *a, **k: None)
    def repair(*a, first, **k):
        repairs.append(1)
        return None, first
    monkeypatch.setattr(message_operations, "repair_held_delivery", repair)
    observe = message_queries.receipt
    monkeypatch.setattr(message_queries, "receipt", lambda ctx, mid, **k: observe(ctx, mid, destination=k["destination"], wait=0))
    monkeypatch.setattr(message_operations, "notify_recording_degraded", lambda *a, **k: pytest.fail("nudge must not send a recording alert"))
    return calls, repairs


def invoke(gateway, **updates):
    adapter, reader, _, options, selection = gateway
    return adapter.nudge(reader, **{**options, **selection, "reason": "Check the selected work", **updates})


def test_nudge_records_human_then_proves_manager_receipt_and_replay_has_no_effect(gateway, monkeypatch):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    result = invoke(gateway)
    assert result.data["recording"] == "committed" and result.data["notification"] == "received"
    assert result.data["integrity_verdict"] == "delivered"
    assert len(calls) == len(repairs) == 1
    observed = OwnerNudges(adapter.root, package=adapter.package).inspect(reader, **options)
    assert observed.request.operation == "task.nudge"
    assert observed.request.caller_uid == ctx.caller.uid
    assert observed.request.recipient_uid == ctx.bots[ctx.context.fleet.manager].uid
    assert observed.receiver.integrity_verdict == "delivered"
    replay = invoke(gateway)
    assert replay.data["replayed"] and replay.data["message_id"] == result.data["message_id"]
    assert len(calls) == len(repairs) == 1
    with pytest.raises(ReceiptConflict):
        invoke(gateway, reason="Changed reason")
    assert len(calls) == len(repairs) == 1
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT sender_uid, recipient_uid FROM communications").fetchall() == [(ctx.caller.uid, options["manager_uid"])]
    receipt_file = adapter.root / "state/requests" / ctx.fleet_uid / (options["request_id"] + ".json")
    assert b"Check the selected work" not in receipt_file.read_bytes()


@pytest.mark.parametrize("received,altered", [(False, False), (True, True)])
def test_committed_nudge_and_submission_are_not_delivery(gateway, monkeypatch, received, altered):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch, received=received, altered=altered)
    with pytest.raises(CommandFailure) as failed:
        invoke(gateway)
    assert failed.value.error.code == "notification_failed"
    assert failed.value.data["recording"] == "committed"
    assert failed.value.data["notification"] != "received"
    observed = adapter.inspect(reader, **options)
    assert observed.receiver.integrity_verdict != "delivered"
    with pytest.raises(CommandFailure):
        invoke(gateway)
    assert len(calls) == len(repairs) == 1


@pytest.mark.parametrize("failure", ["read_only", "messages_only", "wrong_type", "regrant", "owner", "session", "principal", "fleet", "manager", "release", "actor", "generated", "source", "unbound_source", "foreign_source"])
def test_invalid_authority_and_scope_refuse_before_task_or_native_effect(gateway, monkeypatch, failure):
    adapter, reader, ctx, options, selection = gateway
    calls, repairs = receiver(monkeypatch)
    grant = options["expected_grant"]
    if failure in {"read_only", "messages_only", "regrant"}:
        adapter.access.revoke_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid)
        if failure == "regrant":
            replacement = adapter.access.allow_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
            assert replacement.generation != grant.generation
        elif failure == "messages_only":
            adapter.access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    elif failure == "wrong_type":
        options["expected_grant"] = adapter.access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
            actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    elif failure == "owner":
        adapter.access.revoke_owner(expected_revision=grant.owner.revision)
    elif failure == "session":
        adapter.access.renew_session(reader.token, reader.principal)
    elif failure == "principal":
        reader = VerifiedReader(PrincipalRef("synthetic-verifier", "other-owner"), reader.token)
    elif failure == "fleet":
        options["fleet_uid"] = "fleet_" + "a" * 32
    elif failure == "manager":
        options["manager_uid"] = ctx.bots["worker"].uid
    elif failure == "release":
        selection["expected_release_id"] = "r-" + "0" * 64
    elif failure == "actor":
        with sqlite3.connect(db_file(adapter.root)) as conn:
            conn.execute("UPDATE identity_registry SET uid=? WHERE alias=?", ("actor_" + uuid4().hex, ctx.caller.alias))
    elif failure == "generated":
        monkeypatch.setenv("BOT_ID", "")
    else:
        with sqlite3.connect(db_file(adapter.root)) as conn:
            if failure == "unbound_source":
                conn.execute("DROP TABLE owner_source_binding")
            elif failure == "source":
                conn.execute("DELETE FROM owner_source_binding")
            else:
                conn.execute("UPDATE work_items SET host_uid='foreign-host'")
    with pytest.raises((AccessDenied, SourceUnavailable, SourceDenied, ReleaseMismatch)):
        adapter.nudge(reader, **options, **selection, reason="No effect")
    assert calls == repairs == []
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event='nudged'").fetchone()[0] == 0


@pytest.mark.parametrize("problem", ["terminal", "assignment", "wrong_fleet", "unresolved"])
def test_task_policy_refusals_cannot_notify(gateway, monkeypatch, problem):
    adapter, _, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    if problem == "terminal":
        task_operations.withdraw(ctx, str(uuid4()), options["task_id"], reason="Closed")
    elif problem == "assignment":
        task_operations.assign(ctx, str(uuid4()), options["task_id"], bot_id="worker")
    elif problem == "wrong_fleet":
        with sqlite3.connect(db_file(adapter.root)) as conn:
            conn.execute("UPDATE work_items SET fleet_uid=NULL WHERE work_item_id=?", (options["task_id"],))
    else:
        from tests.test_task_state import _insert
        with sqlite3.connect(db_file(adapter.root)) as conn:
            for worker in ctx.bots.values():
                _insert(conn, "assignments", assignment_id="asg_" + uuid4().hex,
                    work_item_id=options["task_id"], fleet_uid=ctx.fleet_uid,
                    assignee_uid=worker.uid, assigned_by_uid=ctx.caller.uid)
            conn.execute("UPDATE assignments SET host_uid=?", (ctx.host_uid,))
    with pytest.raises((TaskQueryError, TaskStateError)):
        invoke(gateway)
    assert calls == repairs == []


@pytest.mark.parametrize("boundary", ["route", "result", "failure", "inspect"])
def test_exact_grant_is_rechecked_before_effect_and_before_any_outcome(gateway, monkeypatch, boundary):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch, received=boundary != "failure")
    old = options["expected_grant"]
    def regrant():
        adapter.access.revoke_nudges(expected_owner=old.owner, fleet_uid=ctx.fleet_uid)
        replacement = adapter.access.allow_nudges(expected_owner=old.owner, fleet_uid=ctx.fleet_uid,
            actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
        assert replacement.generation != old.generation
    name = "receipt" if boundary == "inspect" else "resolve_message_route" if boundary == "route" else "nudge_bound_task"
    if boundary == "inspect":
        invoke(gateway)
    original = getattr(owner_nudges, name)
    def race(*a, **k):
        try:
            return original(*a, **k)
        finally:
            regrant()
    monkeypatch.setattr(owner_nudges, name, race)
    with pytest.raises(AccessDenied, match="nudge_binding_changed"):
        if boundary == "inspect":
            adapter.inspect(reader, **options)
        else:
            invoke(gateway)
    assert len(calls) == (0 if boundary == "route" else 1)


@pytest.mark.parametrize("failure", ["lock", "prepare", "reserve", "committed_outcome"])
def test_persistence_failures_never_notify(gateway, monkeypatch, failure):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    def unavailable(*a, **k):
        raise OSError("synthetic persistence failure")
    if failure == "lock":
        monkeypatch.setattr(task_operations, "locked_request", unavailable)
    else:
        method = {"prepare": "prepare", "reserve": "begin_native_attempt", "committed_outcome": "outcome"}[failure]
        original = getattr(RequestStore, method)
        monkeypatch.setattr(RequestStore, method, unavailable)
    with pytest.raises((OSError, CommandFailure)):
        invoke(gateway)
    assert calls == repairs == []
    if failure in {"reserve", "committed_outcome"}:
        observed = adapter.inspect(reader, **options)
        assert observed.request.stages[0].proof.status == "committed"
        assert observed.request.transmissions == ()
        # Even when preparation resumes, committed replay never fills the send gap.
        monkeypatch.setattr(RequestStore, method, original)
        with pytest.raises(CommandFailure):
            invoke(gateway)
        assert calls == repairs == []


def test_lost_native_outcome_remains_unknown_and_replay_never_repairs(gateway, monkeypatch):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch, received=False)
    save = RequestStore._save
    def lose(self, retained):
        if retained.message_attempts and retained.message_attempts[-1].observation is not None:
            raise OSError("lost native outcome persistence")
        return save(self, retained)
    monkeypatch.setattr(RequestStore, "_save", lose)
    with pytest.raises(CommandFailure):
        invoke(gateway)
    observed = adapter.inspect(reader, **options)
    assert len(observed.request.transmissions) == 1 and observed.request.transmissions[0].transport is None
    assert observed.receiver.integrity_verdict != "delivered"
    with pytest.raises(CommandFailure):
        invoke(gateway)
    assert len(calls) == 1 and len(repairs) <= 1


def test_inspection_is_pure_and_keeps_original_assignment(gateway, monkeypatch):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    with pytest.raises(RequestNotFoundError):
        adapter.inspect(reader, **options)
    result = invoke(gateway)
    task_operations.assign(ctx, str(uuid4()), options["task_id"], bot_id="worker")
    observed = adapter.inspect(reader, **options)
    assert observed.request.assignment_id is None
    assert invoke(gateway).data["assignment_id"] is None
    assert result.data["message_id"] == observed.request.message_id
    with pytest.raises(AccessDenied, match="request_scope_mismatch"):
        adapter.inspect(reader, **{**options, "task_id": "wi_" + "a" * 32})
    assert len(calls) == len(repairs) == 1


@pytest.mark.parametrize("reason", ["", " ", "x" * 2001, "nul\x00", None])
def test_invalid_reason_never_records_or_sends(gateway, monkeypatch, reason):
    calls, repairs = receiver(monkeypatch)
    with pytest.raises((ValueError, CommandFailure)):
        invoke(gateway, reason=reason)
    assert calls == repairs == []


@pytest.mark.parametrize("boundary", ["task_read", "response", "inspect"])
def test_source_changes_are_refused_at_read_and_response_boundaries(gateway, monkeypatch, boundary):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    def foreign():
        with sqlite3.connect(db_file(adapter.root)) as conn:
            conn.execute("UPDATE work_items SET host_uid='foreign-host'")
    if boundary == "task_read":
        original = adapter._admit_read
        def check(conn):
            foreign()
            original(conn)
        monkeypatch.setattr(adapter, "_admit_read", check)
    else:
        if boundary == "inspect":
            invoke(gateway)
        name = "nudge_bound_task" if boundary == "response" else "receipt"
        original = getattr(owner_nudges, name)
        def changed(*a, **k):
            result = original(*a, **k)
            foreign()
            return result
        monkeypatch.setattr(owner_nudges, name, changed)
    with pytest.raises(SourceDenied):
        if boundary == "inspect":
            adapter.inspect(reader, **options)
        else:
            invoke(gateway)
    assert len(calls) == (0 if boundary == "task_read" else 1)
    assert len(repairs) == len(calls)
