"""Owner feedback over private activation/Plane, with a synthetic native receiver.

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
from claudlobby.plane import owner_feedback
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef, VerifiedReader
from claudlobby.plane.owner_feedback import OwnerFeedback
from claudlobby.plane.owner_source import SourceDenied, SourceUnavailable, bind_source
from claudlobby.request_queries import RequestNotFoundError
from claudlobby.request_receipts import ReceiptBusy, ReceiptConflict, RequestStore
from claudlobby.runtime_admission import ReleaseMismatch
from claudlobby.task_queries import TaskNotFoundError, TaskQueryError
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
    grant = access.allow_feedback(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                               actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    task = task_operations.admit(ctx, str(uuid4()), title="Selected work")
    bind_source(root)
    adapter = OwnerFeedback(root, package=package)
    options = dict(fleet="example", fleet_uid=ctx.fleet_uid, task_id=task.task_id,
        manager_uid=ctx.bots[ctx.context.fleet.manager].uid, request_id=str(uuid4()), expected_grant=grant)
    selection = dict(expected_assignment_id=None, expected_release_id=host.release.release_id)
    monkeypatch.setattr(operation_context, "_local_operator_alias",
                        lambda: pytest.fail("owner feedback must not use the OS account"))
    return adapter, reader, ctx, options, selection


def receiver(monkeypatch, *, received=True, altered=False):
    calls, repairs = [], []
    original = message_operations.send_committed_native_attempt
    def transport(package, destination, *, message_id, body, timeout):
        assert timeout == 120
        with sqlite3.connect(db_file(destination.root)) as conn:
            assert conn.execute("SELECT count(*) FROM events WHERE event='nudged'").fetchone()[0] == 0
            assert conn.execute("SELECT message_class FROM communications WHERE msg_id=?", (message_id,)).fetchone()[0] == "chat"
        calls.append((message_id, body))
        wire = body.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(wire).hexdigest()
        if received:
            emit_batch(destination.root, [{"event_type": "transmission", "emitter": "synthetic-feedback-receiver",
                "event_id": "ev_" + uuid4().hex, "occurred_at": datetime.now(timezone.utc).isoformat(),
                "fleet": destination.fleet, "payload": {"msg_id": message_id, "attempt_no": 1,
                    "carrier": "tmux", "destination": f"bot:{destination.fleet}/{destination.session}",
                    "state": "received", "received_bytes": len(wire),
                    "received_sha256": "sha256:" + "0" * 64 if altered else digest}}])
        return TransportOutcome("submitted", digest, len(wire), 0)
    monkeypatch.setattr(message_operations, "send_committed_native_attempt",
                        lambda *a, **k: original(*a, **k, transport=transport))
    monkeypatch.setattr(message_operations, "read_recipient_box", lambda *a, **k: pytest.fail("feedback must not inspect the recipient box"))
    def repair(*a, first, **k):
        repairs.append(1)
        return None, first
    monkeypatch.setattr(message_operations, "repair_held_delivery", repair)
    observe = message_queries.receipt
    monkeypatch.setattr(message_queries, "receipt", lambda ctx, mid, **k: observe(ctx, mid, destination=k["destination"], wait=0))
    monkeypatch.setattr(message_operations, "notify_recording_degraded", lambda *a, **k: pytest.fail("feedback must not send a recording alert"))
    return calls, repairs


def invoke(gateway, **updates):
    adapter, reader, _, options, selection = gateway
    return adapter.submit(reader, **{**options, **selection, "text": "Check the selected work", **updates})


def test_feedback_records_human_then_proves_manager_receipt_and_replay_has_no_effect(gateway, monkeypatch):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    result = invoke(gateway)
    assert result.data["recording"] == "committed" and result.data["notification"] == "received"
    assert result.data["integrity_verdict"] == "delivered"
    assert len(calls) == 1 and repairs == []
    observed = OwnerFeedback(adapter.root, package=adapter.package).inspect(reader, **options)
    assert observed.request.operation == "task.feedback"
    assert observed.request.caller_uid == ctx.caller.uid
    assert observed.request.recipient_uid == ctx.bots[ctx.context.fleet.manager].uid
    assert observed.receiver.integrity_verdict == "delivered"
    replay = invoke(gateway)
    assert replay.data["replayed"] and replay.data["message_id"] == result.data["message_id"]
    assert len(calls) == 1 and repairs == []
    with pytest.raises(ReceiptConflict):
        invoke(gateway, text="Changed reason")
    assert len(calls) == 1 and repairs == []
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT sender_uid, recipient_uid FROM communications").fetchall() == [(ctx.caller.uid, options["manager_uid"])]
    receipt_file = adapter.root / "state/requests" / ctx.fleet_uid / (options["request_id"] + ".json")
    assert b"Check the selected work" not in receipt_file.read_bytes()


@pytest.mark.parametrize("received,altered", [(False, False), (True, True)])
def test_committed_feedback_and_submission_are_not_delivery(gateway, monkeypatch, received, altered):
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
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize("failure", ["read_only", "messages_only", "nudges_only", "wrong_type", "regrant", "owner", "session", "principal", "fleet", "manager", "release", "actor", "generated", "source", "unbound_source", "foreign_source"])
def test_invalid_authority_and_scope_refuse_before_task_or_native_effect(gateway, monkeypatch, failure):
    adapter, reader, ctx, options, selection = gateway
    calls, repairs = receiver(monkeypatch)
    grant = options["expected_grant"]
    if failure in {"read_only", "messages_only", "nudges_only", "regrant"}:
        adapter.access.revoke_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid)
        if failure == "regrant":
            replacement = adapter.access.allow_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
            assert replacement.generation != grant.generation
        elif failure == "messages_only":
            adapter.access.allow_messages(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
        elif failure == "nudges_only":
            adapter.access.allow_nudges(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
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
        adapter.submit(reader, **options, **selection, text="No effect")
    assert calls == repairs == []
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT count(*) FROM communications").fetchone()[0] == 0


@pytest.mark.parametrize("carrier", ["BOT_SERVICE", "CLAUDLOBBY_TIMER_CONTEXT", "CLAUDLOBBY_RELEASE_ID"])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("operation", ["submit", "inspect"])
def test_generated_carriers_refuse_submit_and_retained_inspection(gateway, monkeypatch, carrier, empty, operation):
    adapter, reader, _, options, selection = gateway
    calls, repairs = receiver(monkeypatch)
    if operation == "inspect":
        invoke(gateway)  # Valid original evidence exists before the carrier arrives.
        assert len(calls) == 1
    with sqlite3.connect(db_file(adapter.root)) as conn:
        before = conn.execute("SELECT count(*) FROM communications").fetchone()[0]
    value = selection["expected_release_id"] if carrier == "CLAUDLOBBY_RELEASE_ID" else "synthetic-carrier"
    monkeypatch.setenv(carrier, "" if empty else value)
    with pytest.raises(AccessDenied, match="generated_context_refused"):
        if operation == "submit":
            adapter.submit(reader, **options, **selection, text="No generated feedback")
        else:
            adapter.inspect(reader, **options)
    assert len(calls) == (1 if operation == "inspect" else 0) and repairs == []
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT count(*) FROM communications").fetchone()[0] == before


@pytest.mark.parametrize("problem", ["assignment", "wrong_fleet", "unresolved"])
def test_task_policy_refusals_cannot_notify(gateway, monkeypatch, problem):
    adapter, _, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    if problem == "assignment":
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
        adapter.access.revoke_feedback(expected_owner=old.owner, fleet_uid=ctx.fleet_uid)
        replacement = adapter.access.allow_feedback(expected_owner=old.owner, fleet_uid=ctx.fleet_uid,
            actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
        assert replacement.generation != old.generation
    name = "receipt" if boundary == "inspect" else "resolve_message_route" if boundary == "route" else "feedback_bound_task"
    if boundary == "inspect":
        invoke(gateway)
    original = getattr(owner_feedback, name)
    def race(*a, **k):
        try:
            return original(*a, **k)
        finally:
            regrant()
    monkeypatch.setattr(owner_feedback, name, race)
    with pytest.raises(AccessDenied, match="feedback_binding_changed"):
        if boundary == "inspect":
            adapter.inspect(reader, **options)
        else:
            invoke(gateway)
    assert len(calls) == (0 if boundary == "route" else 1)


@pytest.mark.parametrize("error_type", [task_operations.TaskConflictError, TaskQueryError,
                                       TaskNotFoundError, ReceiptConflict, ReceiptBusy])
@pytest.mark.parametrize("change", [None, "grant", "session", "source"])
def test_raw_canonical_failure_requires_current_authority(gateway, monkeypatch, error_type, change):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    failure = error_type("private canonical task or request state")

    def fail(*args, **kwargs):
        if change == "grant":
            adapter.access.revoke_feedback(expected_owner=options["expected_grant"].owner,
                                        fleet_uid=ctx.fleet_uid)
        elif change == "session":
            adapter.access.renew_session(reader.token, reader.principal)
        elif change == "source":
            with sqlite3.connect(db_file(adapter.root)) as conn:
                conn.execute("UPDATE work_items SET host_uid='foreign-host'")
        raise failure

    monkeypatch.setattr(owner_feedback, "feedback_bound_task", fail)
    if change is None:
        with pytest.raises(error_type) as raised:
            invoke(gateway)
        assert raised.value is failure
    else:
        with pytest.raises(SourceDenied if change == "source" else AccessDenied) as raised:
            invoke(gateway)
        assert "private canonical" not in str(raised.value)
    assert calls == repairs == []


def test_authorized_raw_failure_after_delivery_keeps_original_error_and_receipt(gateway, monkeypatch):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    original = owner_feedback.feedback_bound_task
    failure = TaskQueryError("canonical result unavailable after delivery")

    def fail_after_delivery(*args, **kwargs):
        original(*args, **kwargs)
        raise failure

    monkeypatch.setattr(owner_feedback, "feedback_bound_task", fail_after_delivery)
    with pytest.raises(TaskQueryError) as raised:
        invoke(gateway)
    assert raised.value is failure
    observed = adapter.inspect(reader, **options)
    assert observed.request.request_id == options["request_id"]
    assert any(stage.kind == "recording" and stage.proof.status == "committed"
               for stage in observed.request.stages)
    assert observed.receiver.integrity_verdict == "delivered"
    assert len(calls) == 1 and repairs == []


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
    assert len(calls) == 1 and repairs == []


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
    assert len(calls) == 1 and repairs == []


@pytest.mark.parametrize("text", ["", " ", "x" * 2001, "nul\x00", None])
def test_invalid_text_never_records_or_sends(gateway, monkeypatch, text):
    calls, repairs = receiver(monkeypatch)
    with pytest.raises((ValueError, CommandFailure)):
        invoke(gateway, text=text)
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
        name = "feedback_bound_task" if boundary == "response" else "receipt"
        original = getattr(owner_feedback, name)
        def changed(*a, **k):
            result = original(*a, **k)
            foreign()
            return result
        monkeypatch.setattr(owner_feedback, name, changed)
    with pytest.raises(SourceDenied):
        if boundary == "inspect":
            adapter.inspect(reader, **options)
        else:
            invoke(gateway)
    assert len(calls) == (0 if boundary == "task_read" else 1)
    assert repairs == []


@pytest.mark.parametrize('boundary', ['read_request', 'receipt'])
@pytest.mark.parametrize('change', ['grant', 'session', 'source'])
def test_inspection_errors_are_closed_against_current_authority(gateway, monkeypatch, boundary, change):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    invoke(gateway)
    failure = TaskQueryError('private receipt state')
    def fail(*a, **k):
        if change == 'grant':
            adapter.access.revoke_feedback(expected_owner=options['expected_grant'].owner, fleet_uid=ctx.fleet_uid)
        elif change == 'session':
            adapter.access.renew_session(reader.token, reader.principal)
        else:
            with sqlite3.connect(db_file(adapter.root)) as conn:
                conn.execute("UPDATE work_items SET host_uid='foreign-host'")
        raise failure
    monkeypatch.setattr(owner_feedback, boundary, fail)
    with pytest.raises(SourceDenied if change == 'source' else AccessDenied):
        adapter.inspect(reader, **options)
    assert len(calls) == 1 and repairs == []


def test_feedback_of_completed_work_binds_null_and_never_changes_completion(gateway, monkeypatch):
    from claudlobby.report_payload import ReportPayload
    adapter, reader, ctx, options, _ = gateway
    assigned = task_operations.assign(ctx, str(uuid4()), options['task_id'], bot_id='worker')
    worker = replace(ctx, caller=ctx.bots['worker'], caller_fleet_uid=ctx.fleet_uid)
    task_operations.complete(worker, str(uuid4()), assigned.assignment_id, ReportPayload('completed', summary='Done'))
    with sqlite3.connect(db_file(adapter.root)) as conn:
        before = conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0]
    calls, repairs = receiver(monkeypatch)
    with pytest.raises(task_operations.TaskConflictError):
        invoke(gateway, expected_assignment_id=assigned.assignment_id)
    result = invoke(gateway)
    assert result.data['current_task_state'] == 'completed' and result.data['assignment_id'] is None
    assert adapter.inspect(reader, **options).request.assignment_id is None
    assert len(calls) == 1 and repairs == []
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE kind='task'").fetchone()[0] == before


def test_unrecorded_feedback_is_inspect_only_and_never_fills_recording_gap(gateway, monkeypatch):
    adapter, reader, _, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    with sqlite3.connect(db_file(adapter.root)) as conn:
        conn.execute("CREATE TRIGGER refuse_feedback BEFORE INSERT ON communications "
                     "BEGIN SELECT RAISE(ABORT, 'private refusal'); END")
    with pytest.raises(CommandFailure):
        invoke(gateway)
    with sqlite3.connect(db_file(adapter.root)) as conn:
        conn.execute('DROP TRIGGER refuse_feedback')
    retained = adapter.inspect(reader, **options)
    assert retained.request.stages[0].proof.status == 'unrecorded'
    with pytest.raises(CommandFailure):
        invoke(gateway)
    assert calls == repairs == []
    assert adapter.inspect(reader, **options).request.transmissions == ()


@pytest.mark.parametrize('operation', ['submit', 'inspect'])
@pytest.mark.parametrize('boundary', ['bind_task_context', 'resolve_active_context'])
def test_identity_errors_cannot_disclose_after_revocation(gateway, monkeypatch, operation, boundary):
    adapter, reader, ctx, options, _ = gateway
    calls, repairs = receiver(monkeypatch)
    def fail(*a, **k):
        adapter.access.revoke_feedback(expected_owner=options['expected_grant'].owner, fleet_uid=ctx.fleet_uid)
        raise TaskQueryError('private identity state')
    monkeypatch.setattr(owner_feedback, boundary, fail)
    with pytest.raises(AccessDenied, match='feedback_not_allowed'):
        if operation == 'submit':
            invoke(gateway)
        else:
            adapter.inspect(reader, **options)
    assert calls == repairs == []


def test_feedback_grant_rotation_preserves_message_and_nudge_authority(gateway):
    adapter, reader, ctx, options, _ = gateway
    grant = options['expected_grant']
    binding = dict(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid,
                   actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    message = adapter.access.allow_messages(**binding)
    nudge = adapter.access.allow_nudges(**binding)
    adapter.access.revoke_feedback(expected_owner=grant.owner, fleet_uid=ctx.fleet_uid)
    replacement = adapter.access.allow_feedback(**binding)
    assert replacement.generation != grant.generation
    scope = dict(host_uid=adapter.host_uid, fleet_uid=ctx.fleet_uid)
    assert adapter.access.authorize_message(reader.token, reader.principal, **scope) == message
    assert adapter.access.authorize_nudge(reader.token, reader.principal, **scope) == nudge
    with pytest.raises(AccessDenied, match='feedback_binding_changed'):
        adapter.inspect(reader, **options)
