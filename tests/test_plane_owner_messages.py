"""Internal gateway against real private activation/Plane owners.

Only the native receiver is synthetic. No browser/Tailscale authentication or
real bot delivery is claimed. Its explicit received event is test data, not a
capture of an external hook. The canonical ingest and receipt join are real.
"""

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import message_operations, operation_context
from claudlobby.activation_state import ActivationError
from claudlobby.command_result import CommandFailure
from claudlobby.message_payload import MessagePayloadError
from claudlobby.message_transport import TransportOutcome
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef, VerifiedReader
from claudlobby.plane.owner_messages import OwnerMessages, _GENERATED_ENV
from claudlobby.request_queries import RequestNotFoundError
from claudlobby.request_receipts import ReceiptConflict, RequestStore
from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
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
    session = access.open_session(principal)
    reader = VerifiedReader(principal, session.token)
    adapter = OwnerMessages(root, package=package)
    options = dict(fleet="example", fleet_uid=ctx.fleet_uid,
                   recipient_uid=ctx.bots["worker"].uid, request_id=str(uuid4()))
    options["expected_grant"] = access.allow_messages(
        expected_owner=owner, fleet_uid=ctx.fleet_uid,
        actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
    # A future regression to OS attribution must fail before a send.
    monkeypatch.setattr(operation_context, "_local_operator_alias",
                        lambda: pytest.fail("gateway must not use the server OS account"))
    monkeypatch.setattr(message_operations, "read_recipient_box", lambda *a, **k: None)
    return adapter, reader, owner, ctx, options


def native_receiver(monkeypatch, *, received=True, altered=False, allow_degraded=False):
    calls = []
    original = message_operations.send_message

    def transport(package, destination, *, message_id, body):
        calls.append((message_id, body))
        wire = body.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(wire).hexdigest()
        if received:
            emit_batch(destination.root, [{
                "event_type": "transmission", "emitter": "synthetic-owner-receiver",
                "event_id": "ev_" + uuid4().hex,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "fleet": destination.fleet,
                "payload": {"msg_id": message_id, "attempt_no": 1, "carrier": "tmux",
                            "destination": f"bot:{destination.fleet}/{destination.session}",
                            "state": "received", "received_bytes": len(wire),
                            "received_sha256": "sha256:" + "0" * 64 if altered else digest},
            }])
        return TransportOutcome("submitted", digest, len(wire), 0)

    def notify(*a, **k):
        assert allow_degraded, "unexpected recording alert"
        return RecordingAlertOutcome(ChannelOutcome("unconfigured", True),
                                     ChannelOutcome("unconfigured", True))

    def send(*a, **k):
        return original(*a, **k, transport=transport,
                        notify=notify, clear=lambda *a, **k: None)

    monkeypatch.setattr(message_operations, "send_message", send)
    return calls


def test_send_records_paired_actor_and_proves_receipt_then_recovers_without_resend(gateway, monkeypatch):
    adapter, reader, _, ctx, options = gateway
    calls = native_receiver(monkeypatch)
    output = adapter.send(reader, **options, text="Review the example draft")
    assert output.data["sender"] == {"uid": ctx.caller.uid, "alias": "human:paired-owner"}
    assert output.data["transport"] == "submitted" and output.data["delivery"] == "received"
    assert output.data["integrity_verdict"] == "delivered"
    # Discard the response, recreate the adapter as after a server restart.
    recovered = OwnerMessages(adapter.root, package=adapter.package).inspect(reader, **options)
    assert recovered.request.message_id == output.data["message_id"]
    assert recovered.request.caller_uid == ctx.caller.uid
    assert recovered.receiver.integrity_verdict == "delivered"
    assert len(calls) == 1
    replay = adapter.send(reader, **options, text="Review the example draft")
    assert replay.data["replayed"] and len(calls) == 1
    with pytest.raises(ReceiptConflict):
        adapter.send(reader, **options, text="A different instruction")
    assert len(calls) == 1
    with sqlite3.connect(db_file(adapter.root)) as conn:
        assert conn.execute("SELECT sender_uid, sender_alias FROM communications").fetchall() == [
            (ctx.caller.uid, "human:paired-owner")]
    assert b"Review the example draft" not in adapter.access.path.read_bytes()
    stored = adapter.root / "state/requests" / ctx.fleet_uid / (options["request_id"] + ".json")
    assert b"Review the example draft" not in stored.read_bytes()


@pytest.mark.parametrize("received,altered,code", [
    (False, False, "delivery_unknown"), (True, True, "delivery_failed"),
])
def test_submission_is_not_receiver_delivery(gateway, monkeypatch, received, altered, code):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch, received=received, altered=altered)
    with pytest.raises(CommandFailure) as error:
        adapter.send(reader, **options, text="Check this synthetic work")
    assert error.value.error.code == code
    assert error.value.data["delivery"] != "received" and len(calls) == 1
    observed = adapter.inspect(reader, **options)
    assert observed.receiver.integrity_verdict != "delivered" and len(calls) == 1


@pytest.mark.parametrize("failure", ["ungranted", "expired", "wrong_principal", "wrong_fleet",
    "wrong_recipient", "actor_rebound", "revoked", "new_host", "unknown_fleet"])
def test_invalid_authority_or_binding_has_no_effect(gateway, monkeypatch, failure):
    adapter, reader, owner, ctx, options = gateway
    calls = native_receiver(monkeypatch)
    if failure == "ungranted":
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    elif failure == "expired":
        adapter.access._clock = lambda: 9_000_000_000.0
    elif failure == "wrong_principal":
        reader = VerifiedReader(PrincipalRef("synthetic-verifier", "other-owner"), reader.token)
    elif failure == "wrong_fleet":
        options["fleet_uid"] = "fleet_" + "f" * 32
    elif failure == "wrong_recipient":
        options["recipient_uid"] = "actor_" + "f" * 32
    elif failure == "actor_rebound":
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        adapter.access.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                                      actor_uid="actor_" + "f" * 32, actor_alias=ctx.caller.alias)
    elif failure == "revoked":
        adapter.access.revoke_owner(expected_revision=owner.revision)
    elif failure == "new_host":
        (adapter.root / "state/host-uid").write_text("host_" + "f" * 32 + "\n")
    elif failure == "unknown_fleet":
        options["fleet"] = ""
    with pytest.raises(AccessDenied):
        adapter.send(reader, **options, text="Must not send")
    assert calls == []
    assert not list((adapter.root / "state/requests").glob("*/*.json"))


@pytest.mark.parametrize("carrier", _GENERATED_ENV)
def test_generated_environment_never_overrides_owner(gateway, monkeypatch, carrier):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    monkeypatch.setenv(carrier, "")
    with pytest.raises(AccessDenied, match="generated_context_refused"):
        adapter.send(reader, **options, text="Must not send")
    assert calls == []


def test_revocation_during_route_lookup_refuses_dispatch(gateway, monkeypatch):
    from claudlobby.plane import owner_messages
    adapter, reader, owner, ctx, options = gateway
    original = owner_messages.resolve_message_route

    def resolve(*a, **k):
        route = original(*a, **k)
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        return route

    monkeypatch.setattr(owner_messages, "resolve_message_route", resolve)
    calls = native_receiver(monkeypatch)
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        adapter.send(reader, **options, text="Must not send")
    assert calls == []


def test_request_read_requires_original_parties_and_current_grant(gateway, monkeypatch):
    adapter, reader, owner, ctx, options = gateway
    calls = native_receiver(monkeypatch)
    adapter.send(reader, **options, text="Synthetic body")
    with pytest.raises(AccessDenied, match="request_scope_mismatch"):
        adapter.inspect(reader, **{**options, "recipient_uid": ctx.bots["manager"].uid})
    adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    with pytest.raises(AccessDenied, match="messages_not_allowed"):
        adapter.inspect(reader, **options)
    assert len(calls) == 1


def test_rebound_human_cannot_read_previous_actors_request(gateway, monkeypatch):
    adapter, reader, owner, ctx, options = gateway
    calls = native_receiver(monkeypatch)
    adapter.send(reader, **options, text="Original owner's work")
    other = operation_context.resolve_task_mutation_context(root=adapter.root, fleet="example",
                operator_alias="human:other-actor", package=adapter.package)
    adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
    options["expected_grant"] = adapter.access.allow_messages(
        expected_owner=owner, fleet_uid=ctx.fleet_uid,
        actor_uid=other.caller.uid, actor_alias=other.caller.alias)
    with pytest.raises(AccessDenied, match="request_scope_mismatch"):
        adapter.inspect(reader, **options)
    assert len(calls) == 1


@pytest.mark.parametrize("boundary", ["before_bind", "route", "inspect", "send_return"])
@pytest.mark.parametrize("same_actor", [False, True])
def test_expected_grant_cannot_change_during_owner_operation(gateway, monkeypatch, boundary, same_actor):
    from claudlobby.plane import owner_messages
    adapter, reader, owner, ctx, options = gateway
    calls = native_receiver(monkeypatch)
    other = operation_context.resolve_task_mutation_context(
        root=adapter.root, fleet="example", operator_alias="human:replacement", package=adapter.package)

    def regrant():
        adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
        adapter.access.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                                      actor_uid=ctx.caller.uid if same_actor else other.caller.uid,
                                      actor_alias=ctx.caller.alias if same_actor else other.caller.alias)

    if boundary == "inspect":
        adapter.send(reader, **options, text="Original message")
        original = owner_messages.receipt

        def read(*a, **k):
            result = original(*a, **k)
            regrant()
            return result

        monkeypatch.setattr(owner_messages, "receipt", read)
    elif boundary == "before_bind":
        original = adapter._bind

        def bind(*a, **k):
            regrant()
            return original(*a, **k)

        monkeypatch.setattr(adapter, "_bind", bind)
    else:
        name = "resolve_message_route" if boundary == "route" else "deliver_bound_message"
        original = getattr(owner_messages, name)

        def resolve_or_send(*a, **k):
            result = original(*a, **k)
            regrant()
            return result

        monkeypatch.setattr(owner_messages, name, resolve_or_send)
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        if boundary == "inspect":
            adapter.inspect(reader, **options)
        else:
            adapter.send(reader, **options, text="Original message")
    assert len(calls) == (1 if boundary in {"inspect", "send_return"} else 0)
    if not calls:
        assert not list((adapter.root / "state/requests").glob("*/*.json"))


@pytest.mark.parametrize("code", ["recording_degraded", "delivery_failed", "delivery_unknown"])
@pytest.mark.parametrize("change", ["unchanged", "revoke", "expire", "reallow"])
def test_failure_outcome_is_withheld_when_authority_changes(gateway, monkeypatch, code, change):
    from claudlobby.plane import owner_messages
    adapter, reader, owner, ctx, options = gateway
    outcome = CommandFailure(code, "Synthetic retained outcome", data={"message_id": "sensitive-outcome"})

    def deliver(*a, **k):
        if change in {"revoke", "reallow"}:
            adapter.access.revoke_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid)
            if change == "reallow":
                adapter.access.allow_messages(expected_owner=owner, fleet_uid=ctx.fleet_uid,
                                              actor_uid=ctx.caller.uid, actor_alias=ctx.caller.alias)
        elif change == "expire":
            adapter.access._clock = lambda: 10**12
        raise outcome

    monkeypatch.setattr(owner_messages, "deliver_bound_message", deliver)
    with pytest.raises(CommandFailure if change == "unchanged" else AccessDenied) as error:
        adapter.send(reader, **options, text="Synthetic message")
    if change == "unchanged":
        assert error.value is outcome
    else:
        assert not hasattr(error.value, "data")


def test_owner_replay_never_repeats_held_enter_repair(gateway, monkeypatch):
    from claudlobby import message_queries
    from claudlobby.message_queries import MessageIdentity, ReceiptObservation
    adapter, reader, _, ctx, options = gateway
    calls = native_receiver(monkeypatch, received=False)
    repairs = []

    def receipt(_ctx, message_id, **kwargs):
        return ReceiptObservation(message_id=message_id, root=str(adapter.root),
            sender=MessageIdentity(ctx.caller.uid, ctx.caller.alias, ctx.caller_fleet_uid),
            destination=MessageIdentity(ctx.bots["worker"].uid, ctx.bots["worker"].alias, ctx.fleet_uid),
            receipt_observation="missing", integrity_verdict="unknown", exit_code=5,
            code="delivery_unknown", reason="Synthetic held input")

    def repair(*a, first, **k):
        repairs.append(a[2])
        return None, first

    monkeypatch.setattr(message_queries, "receipt", receipt)
    monkeypatch.setattr(message_operations, "repair_held_delivery", repair)
    for _ in range(2):
        with pytest.raises(CommandFailure) as error:
            adapter.send(reader, **options, text="Held input")
        assert error.value.error.code == "delivery_unknown"
    assert len(calls) == 1
    assert len(repairs) == 1, "a strict replay must not enter the native Enter-repair owner"


@pytest.mark.parametrize("operation", ["send", "inspect"])
def test_owner_operation_requires_exact_expected_grant(gateway, monkeypatch, operation):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    options["expected_grant"] = replace(options["expected_grant"],
        owner=replace(options["expected_grant"].owner, revision=999))
    with pytest.raises(AccessDenied, match="message_binding_changed"):
        getattr(adapter, operation)(reader, **options, **({"text": "Must not send"} if operation == "send" else {}))
    assert calls == []


@pytest.mark.parametrize("failure", ["lock", "prepare", "reserve"])
def test_owner_send_requires_durable_preparation_and_reservation(gateway, monkeypatch, failure):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)

    def unavailable(*a, **k):
        raise OSError("synthetic request persistence failure")

    if failure == "lock":
        monkeypatch.setattr(message_operations, "locked_request", unavailable)
    else:
        monkeypatch.setattr(RequestStore, "prepare" if failure == "prepare" else "begin_native_attempt", unavailable)
    with pytest.raises(ReceiptConflict, match="durable"):
        adapter.send(reader, **options, text="Must not send without retained reservation")
    assert calls == []


def test_owner_outcome_persistence_failure_stays_unknown_and_never_resends(gateway, monkeypatch):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch, received=False, allow_degraded=True)
    original = RequestStore._save

    def fail_observation(self, retained):
        if retained.message_attempts and retained.message_attempts[-1].observation is not None:
            raise OSError("synthetic outcome persistence failure")
        return original(self, retained)

    monkeypatch.setattr(RequestStore, "_save", fail_observation)
    with pytest.raises(CommandFailure) as failure:
        adapter.send(reader, **options, text="Retain the original UUID")
    assert failure.value.error.code == "recording_degraded"
    observed = adapter.inspect(reader, **options)
    assert observed.request.transmissions[0].transport is None
    assert observed.receiver.integrity_verdict in {"unknown", "unconfirmed"}
    with pytest.raises(CommandFailure) as replay:
        adapter.send(reader, **options, text="Retain the original UUID")
    assert replay.value.error.code == "delivery_unknown"
    assert len(calls) == 1


def test_pending_activation_blocks_authorized_message(gateway, monkeypatch):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    with (adapter.root / "state/activation-pending.lock").open("a") as marker:
        fcntl.flock(marker, fcntl.LOCK_EX)
        with pytest.raises(ActivationError, match="activation is running"):
            adapter.send(reader, **options, text="Wait for activation")
    assert calls == []


def test_unknown_request_is_not_permission_to_send(gateway, monkeypatch):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    with pytest.raises(RequestNotFoundError):
        adapter.inspect(reader, **options)
    assert calls == []


@pytest.mark.parametrize("crash", [True, False])
def test_missing_communication_keeps_prepared_or_transmitted_evidence(gateway, monkeypatch, crash):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch, allow_degraded=True)
    original = message_operations.emit_batch

    def fail_recording(root, events, **kwargs):
        if events[0]["event_type"] == "communication":
            if crash:
                raise KeyboardInterrupt("synthetic crash after durable preparation")
            raise sqlite3.OperationalError("synthetic communication recording failure")
        return original(root, events, **kwargs)

    monkeypatch.setattr(message_operations, "emit_batch", fail_recording)
    with pytest.raises(KeyboardInterrupt if crash else CommandFailure):
        adapter.send(reader, **options, text="Retain evidence across recording failure")
    assert len(calls) == (0 if crash else 1)
    observed = adapter.inspect(reader, **options)
    assert observed.request.message_id is not None
    assert observed.request.transmissions
    assert observed.receiver.receipt_observation == "unavailable"
    assert observed.receiver.integrity_verdict == "unknown"
    if crash:
        assert observed.request.transmissions[0].transport is None
    else:
        assert observed.request.transmissions[0].transport.status == "submitted"
    assert len(calls) == (0 if crash else 1)


@pytest.mark.parametrize("text", ["", " ", "x" * 2001, None])
def test_invalid_message_never_sends(gateway, monkeypatch, text):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    with pytest.raises((ValueError, MessagePayloadError)):
        adapter.send(reader, **options, text=text)
    assert calls == []


@pytest.mark.parametrize("field", ["native", "artifact_id"])
def test_send_admits_the_pinned_package_before_transport(gateway, monkeypatch, field):
    adapter, reader, _, _, options = gateway
    calls = native_receiver(monkeypatch)
    changed = (adapter.package.native / "foreign") if field == "native" else "foreign-artifact"
    adapter.package = replace(adapter.package, **{field: changed})
    with pytest.raises(ActivationError):
        adapter.send(reader, **options, text="Must not use unadmitted scripts")
    assert calls == []
    assert not list((adapter.root / "state/requests").glob("*/*.json"))


@pytest.mark.parametrize("mismatch", ["missing_human", "wrong_actor", "generated_with_context"])
def test_shared_delivery_refuses_conflicting_caller_before_any_effect(gateway, monkeypatch, mismatch):
    from claudlobby.commands.message_write import deliver_bound_message
    from claudlobby.message_context import resolve_message_route
    from claudlobby.message_payload import MessageBody
    adapter, _, _, ctx, options = gateway
    route = resolve_message_route("worker", root=adapter.root, fleet="example",
                                  package=adapter.package, caller_context=ctx)
    caller = ctx
    if mismatch == "missing_human":
        caller = None
    elif mismatch == "wrong_actor":
        caller = replace(ctx, caller=ctx.bots["manager"])
    else:
        route = replace(route, origin=ctx.context)
    calls = native_receiver(monkeypatch)
    with pytest.raises(CommandFailure, match="caller context differs"):
        deliver_bound_message(route, body=MessageBody.from_input("Must not send"),
                              request_id=options["request_id"], caller_context=caller)
    assert calls == []
    assert not list((adapter.root / "state/requests").glob("*/*.json"))


@pytest.fixture
def native_owner_gateway(cold, monkeypatch):
    """Existing activation owner and native observations, with one owner unit."""
    from copy import deepcopy
    from pathlib import Path
    import plistlib
    from claudlobby import activation, runtime_admission, supervision_inventory
    from claudlobby.config_plan import ConfigPlanBuilder
    from claudlobby.config_units import unit_family
    from tests.test_activation import NativeHost

    root, release, plan, old_host = cold
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                plan.fleets, effects=deepcopy(plan.effects))
    builder.inputs = deepcopy(plan.inputs)
    builder.changes = {change.target: change for change in plan.changes}
    builder.contents = {path.name: plan.blob(path.name)
                        for path in (plan.directory / 'files').iterdir()}
    env = dict(next(iter(old_host.by_name.values()))[0].environment)
    env.pop('FLEET_ROOT', None)
    stem = 'example-private-owner'
    socket_directory = root / 'private-owner'
    socket_directory.mkdir(mode=0o700)
    destination = root / 'runtime/_host/timers'
    argv = runtime_admission.wrap_unit_argv(env, unit=stem, phase='producers', mode='exec',
        argv=[str(release.cli_path), '--root', str(root), 'host', 'owner', 'serve',
              '--origin', 'https://host.example.test', '--tailscale', '/example/tailscale',
              '--socket', str(socket_directory / 'owner.sock')])
    files = {stem + '.plist': (plistlib.dumps({'Label': stem, 'WorkingDirectory': str(root),
        'EnvironmentVariables': env, 'ProgramArguments': list(argv)}), 0o644),
        stem + '.service': ((f'[Service]\nWorkingDirectory={root}\n'
            f'ExecStart={runtime_admission.unit_systemd_command(argv)}\n'
            '[Install]\nWantedBy=default.target\n').encode(), 0o644)}
    builder.effects['units'].extend(unit_family(files, destination=destination,
        scope='host', phase='producers', release_id=release.release_id))
    for name, (content, mode) in files.items():
        builder.file(destination / name, content, mode=mode)
    plan = builder.seal()
    host = NativeHost(root, release, plan, old_host.directory, old_host.package)
    monkeypatch.setattr(activation, '_probe', host.probe)
    monkeypatch.setattr(activation, '_registry_scan', host.registry_scan)
    activation.bootstrap_activation(root, 'cold', plan.plan_id, host.directory, adapter=host)
    result = gateway.__wrapped__((root, host), monkeypatch)
    # Extend the existing manager seam with its real shared ownership reader.
    import importlib.util
    import subprocess
    spec = importlib.util.spec_from_file_location('owner_action_unit',
                                                source_package().native / 'bot-unit-owner.py')
    ownership = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ownership)
    owner_installed = host.directory / (stem + '.service')
    assert owner_installed != destination / (stem + '.service')
    original_call = host.call
    def call(function, *args, **kwargs):
        if function == 'svc_bot_unit_owned_by':
            host.calls.append((function, tuple(map(str, args))))
            return subprocess.CompletedProcess([], ownership.main(*map(str, args)), '', '')
        if function == 'svc_activation_assert_external' and Path(args[0]) != owner_installed:
            # The native manager's loaded fragment is the installed copy;
            # matching generated bytes do not make its source that fragment.
            host.calls.append((function, tuple(map(str, args))))
            return subprocess.CompletedProcess([], 3, '', 'unit state/domain unavailable')
        return original_call(function, *args, **kwargs)
    monkeypatch.setattr(host, 'call', call)
    monkeypatch.setattr(supervision_inventory, 'Adapter', lambda *a, **k: host)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    host.external_result = 1  # Existing ancestry door: proven member, not external.
    host.calls.clear()
    return result, host, stem


def test_native_owner_service_can_send_once_with_exact_grant(native_owner_gateway, monkeypatch):
    (adapter, reader, owner, ctx, options), host, stem = native_owner_gateway
    calls = native_receiver(monkeypatch)
    observation = adapter.send(reader, text='native owner message', **options)
    assert observation.data['sender'] == {'uid': ctx.caller.uid, 'alias': ctx.caller.alias}
    assert observation.data['delivery'] == 'received'
    assert len(calls) == 1
    ancestry = [args for name, args in host.calls if name == 'svc_activation_assert_external']
    assert len(ancestry) == 1, 'process proof belongs to this invocation, not each grant recheck'
    assert ancestry[0][0] == str(host.directory / (stem + '.service'))
    assert ancestry[0][0] != str(adapter.root / 'runtime/_host/timers' / (stem + '.service'))
    assert ancestry[0][1] == stem + '.service'
    with pytest.raises(AccessDenied, match='generated_context_refused'):
        adapter._authorize(reader, options['fleet_uid'], options['expected_grant'])
    host.external_result = 0
    with pytest.raises(AccessDenied, match='generated_context_refused'):
        adapter.inspect(reader, **options)
    assert len(calls) == 1
    assert all(stem in args[0] for name, args in host.calls
               if name in {'svc_inventory_properties', 'svc_bot_unit_owned_by'})


@pytest.fixture
def native_owner_gateway_with_template(installed, monkeypatch, tmp_path):  # noqa: F811
    """The same owner unit in a selected plan that also holds the fleet's failure
    template, a unit with no admission (#2522)."""
    from tests.test_activation import _cold_case

    return native_owner_gateway.__wrapped__(
        _cold_case(installed, monkeypatch, tmp_path, fleet_job=True), monkeypatch)


def test_native_owner_service_acts_beside_a_failure_template_in_the_selected_plan(
        native_owner_gateway_with_template, monkeypatch):
    """#2522: the owner check walks every planned unit, and the failure template has
    no admission to read, so it is passed over and the owner service still acts."""
    from claudlobby.activation_state import read_selection
    from claudlobby.config_plan import read_plan
    from claudlobby.config_units import planned_units

    (adapter, reader, owner, ctx, options), host, stem = native_owner_gateway_with_template
    plan = read_plan(adapter.root, read_selection(adapter.root)['plan_id'])
    assert [item['admission'] for declaration, item in planned_units(plan, 'Linux')
            if declaration.source.name == 'com.example.job-failed@.service'] == [None]
    calls = native_receiver(monkeypatch)
    observation = adapter.send(reader, text='native owner message', **options)
    assert observation.data['delivery'] == 'received'
    assert len(calls) == 1


@pytest.mark.parametrize('defect', ['root', 'release', 'external', 'unknown', 'missing',
                                  'changed', 'drift', 'cli', 'native', 'artifact', 'BOT_ID', 'BOT_NAME', 'BOT_DIR',
                                  'FLEET_ROOT', 'FLEET_NAME', 'CLAUDLOBBY_FLEET',
                                  'BOT_SERVICE', 'CLAUDLOBBY_TIMER_CONTEXT'])
def test_native_owner_process_proof_fails_closed(native_owner_gateway, monkeypatch, defect):
    (adapter, reader, owner, ctx, options), host, stem = native_owner_gateway
    calls = native_receiver(monkeypatch)
    if defect in {'root', 'release'}:
        monkeypatch.setenv('CLAUDLOBBY_ROOT' if defect == 'root' else 'CLAUDLOBBY_RELEASE_ID', 'wrong')
    elif defect in {'cli', 'native', 'artifact'}:
        key = {'cli': 'CLAUDLOBBY_CLI', 'native': 'CLAUDLOBBY_NATIVE_DIR',
               'artifact': 'CLAUDLOBBY_ARTIFACT_ID'}[defect]
        monkeypatch.setenv(key, 'wrong')
    elif defect in {'external', 'unknown'}:
        host.external_result = 0 if defect == 'external' else 3
    elif defect == 'missing':
        (host.directory / (stem + '.service')).unlink()
    elif defect == 'drift':
        original_call = host.call
        catalogs = 0
        def drift(function, *args, **kwargs):
            nonlocal catalogs
            if function == 'svc_inventory_catalog':
                catalogs += 1
                if catalogs == 2:
                    declaration, item = host.by_name[stem + '.service']
                    for path in (declaration.source, host.directory / (stem + '.service')):
                        path.write_bytes(path.read_bytes() + b'# concurrent drift\n')
            return original_call(function, *args, **kwargs)
        monkeypatch.setattr(host, 'call', drift)
    elif defect == 'changed':
        declaration, item = host.by_name[stem + '.service']
        declaration.source.write_text(declaration.source.read_text() + '# changed\n')
    else:
        monkeypatch.setenv(defect, '')
    with pytest.raises(AccessDenied, match='generated_context_refused'):
        adapter.send(reader, text='must not send', **options)
    assert calls == []
