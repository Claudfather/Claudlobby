"""Generated message routes use committed Git configuration, even without Plane."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from claudlobby import active_config
from claudlobby.activation_state import ActivationError
from claudlobby.activation_identity import read_current_identity_bindings
from claudlobby.config_plan import PlanError
from claudlobby.message_context import MessageContextError, resolve_message_route
from claudlobby.plane.db import db_file
from claudlobby.plane.ids import derive_uid, ensure_host_uid
from tests.package_fixtures import source_package
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — committed configuration and short socket root


@pytest.fixture
def selected(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, _, _, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT"):
        monkeypatch.delenv(key, raising=False)
    package = host.package
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("BOT_NAME", "Manager Display Name")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    db_file(root).rename(root / "plane-offline")
    return root, package


@pytest.fixture
def two_fleet(cold, monkeypatch):  # noqa: F811
    root, installation, _, host = cold
    package = host.package
    snapshot = deepcopy(host.snapshot)
    snapshot['fleets'] = {}
    for name in ('origin', 'other'):
        fleet_dir = root / 'local' / name
        fleet_dir.mkdir(parents=True)
        manifest = fleet_dir / 'fleet.yaml'
        content = json.dumps({'fleet': {
            'name': name, 'manager': 'manager', 'service_prefix': name,
            'system_defaults': False, 'bots': {
                bot: {'expertise': ['software-engineering'], 'channels': []}
                for bot in ('manager', 'worker')}}})
        manifest.write_text(content)
        snapshot['fleets'][name] = {'manifest': str(manifest), 'fleet': content, 'projects': None}
    # Route reads need retained identities, not a registry enrollment or native
    # start. Commit these distinct, well-formed parties through the real owner.
    fleets = {}
    for name in snapshot['fleets']:
        bots = {bot: derive_uid('actor', f'fixture:{name}/{bot}')
                for bot in ('manager', 'worker')}
        fleets[name] = {'fleet_uid': derive_uid('fleet', f'fixture:{name}'),
                        'manager': 'manager', 'manager_uid': bots['manager'], 'bots': bots}
    snapshot['identities'] = {'schema': 1, 'host_uid': ensure_host_uid(root / 'state'), 'fleets': fleets}
    snapshot['units'] = []
    snapshot['owned_outputs'] = []
    snapshot['fingerprint'] = active_config._fingerprint(snapshot)
    active_config.publish_current_config(snapshot, installation=installation,
                                         expected_previous=host.snapshot['fingerprint'])
    db_file(root).rename(root / 'plane-offline')
    for key in ('FLEET_NAME', 'CLAUDLOBBY_FLEET', 'BOT_ID', 'BOT_NAME', 'BOT_DIR', 'FLEET_ROOT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CLAUDLOBBY_ROOT', str(root))
    monkeypatch.setenv('FLEET_NAME', 'origin')
    monkeypatch.setenv('BOT_ID', 'manager')
    monkeypatch.setenv('FLEET_ROOT', str(root / 'local/origin'))
    monkeypatch.setenv('BOT_DIR', str(root / 'local/origin/runtime/bots/manager'))
    return root, package, fleets


def test_generated_route_keeps_caller_and_peer_ids_with_plane_absent(selected):
    root, package = selected
    bindings = read_current_identity_bindings(root, "example", package=package)
    bare = resolve_message_route("worker", package=package)
    qualified = resolve_message_route("example/worker", package=package)
    assert bare == qualified
    assert bare.host_uid == bindings["host_uid"]
    assert bare.caller_fleet_uid == bare.selected_fleet_uid == bare.peer_fleet_uid == bindings["fleet_uid"]
    assert (bare.caller.alias, bare.caller.uid) == ("bot:example/manager", bindings["bots"]["manager"])
    assert (bare.peer.alias, bare.peer.uid) == ("bot:example/worker", bindings["bots"]["worker"])
    assert (bare.manager.alias, bare.manager.uid) == ("bot:example/manager", bindings["manager_uid"])
    assert (bare.peer_destination.socket, bare.peer_destination.session) == ("com.example.worker", "worker")
    assert (bare.manager_destination.socket, bare.manager_destination.session) == ("com.example.manager", "manager")
    assert bare.peer_destination.root == bare.manager_destination.root == root
    assert bare.peer_destination.tmux_tmpdir == bare.manager_destination.tmux_tmpdir
    assert not db_file(root).exists()


def test_qualified_and_explicit_fleet_routes_preserve_origin_with_repeated_bot_names(two_fleet):
    root, package, fleets = two_fleet
    local = resolve_message_route("worker", package=package)
    qualified = resolve_message_route("other/worker", package=package)
    explicit = resolve_message_route("worker", fleet="other", package=package)
    assert not db_file(root).exists()
    assert local.caller.alias == qualified.caller.alias == explicit.caller.alias == "bot:origin/manager"
    assert local.caller.uid == qualified.caller.uid == explicit.caller.uid == fleets["origin"]["bots"]["manager"]
    assert local.peer.alias == "bot:origin/worker"
    assert local.peer.uid == fleets["origin"]["bots"]["worker"]
    assert qualified.peer.alias == explicit.peer.alias == "bot:other/worker"
    assert qualified.peer.uid == explicit.peer.uid == fleets["other"]["bots"]["worker"]
    assert qualified.peer.uid != local.peer.uid
    assert qualified.caller_fleet_uid == explicit.caller_fleet_uid == fleets["origin"]["fleet_uid"]
    assert qualified.selected_fleet_uid == fleets["origin"]["fleet_uid"]
    assert qualified.peer_fleet_uid == explicit.peer_fleet_uid == fleets["other"]["fleet_uid"]
    assert explicit.selected_fleet_uid == fleets["other"]["fleet_uid"]
    assert qualified.origin.fleet.name == explicit.origin.fleet.name == "origin"
    assert qualified.selected.fleet.name == "origin" and qualified.peer_context.fleet.name == "other"
    assert qualified.manager.alias == "bot:origin/manager"
    assert qualified.manager.uid == fleets["origin"]["manager_uid"]
    assert (qualified.peer_destination.socket, qualified.manager_destination.socket) == (
        "other.worker", "origin.manager")
    assert explicit.selected.fleet.name == explicit.peer_context.fleet.name == "other"
    assert explicit.manager.alias == "bot:other/manager"
    assert explicit.manager.uid == fleets["other"]["manager_uid"]
    assert (explicit.peer_destination.socket, explicit.manager_destination.socket) == (
        "other.worker", "other.manager")
    qualified_binding = qualified.receipt_binding()
    explicit_binding = explicit.receipt_binding()
    assert qualified_binding != explicit_binding
    assert qualified_binding.peer_destination == explicit_binding.peer_destination
    assert qualified_binding.manager_destination.socket == "origin.manager"
    assert explicit_binding.manager_destination.socket == "other.manager"
    assert qualified_binding.caller_alias == explicit_binding.caller_alias == "bot:origin/manager"
    assert qualified_binding.recipient_alias == explicit_binding.recipient_alias == "bot:other/worker"
    assert qualified_binding.manager_uid != explicit_binding.manager_uid
    assert json.loads(json.dumps(asdict(qualified_binding)))["peer_destination"]["root"] == str(root)


def test_unactivated_authoring_edits_do_not_retarget_and_lost_bindings_refuse(selected):
    root, package = selected
    original = resolve_message_route("worker", package=package)
    manifest = root / "fleet.yaml"
    manifest.write_text(json.dumps({"fleet": {"name": "example", "manager": "worker",
                                               "service_prefix": "foreign", "bots": {}}}))
    assert resolve_message_route("worker", package=package) == original
    manifest.unlink()
    assert resolve_message_route("worker", package=package) == original

    snapshot = active_config.read_current_config(root, installation=package.installation)
    updated = deepcopy(snapshot)
    updated['identities'] = None
    updated['fingerprint'] = active_config._fingerprint(updated)
    active_config.publish_current_config(updated, installation=package.installation,
                                         expected_previous=snapshot['fingerprint'])
    with pytest.raises(ActivationError, match="identities are not initialized"):
        resolve_message_route("worker", package=package)


def test_unknown_or_foreign_target_bad_origin_and_package_skew_refuse(selected, monkeypatch):
    _, package = selected
    with pytest.raises(MessageContextError, match="not in active fleet"):
        resolve_message_route("unknown", package=package)
    with pytest.raises(PlanError, match="no selected frozen fleet"):
        resolve_message_route("other/worker", package=package)
    for target in ("", "/worker", "example/", "example/worker/other", "../worker"):
        with pytest.raises(MessageContextError):
            resolve_message_route(target, package=package)
    monkeypatch.setenv("BOT_ID", "")
    with pytest.raises(ValueError, match="invalid generated selector"):
        resolve_message_route("worker", package=package)
    monkeypatch.delenv("BOT_ID")
    monkeypatch.delenv("BOT_NAME")
    monkeypatch.delenv("BOT_DIR")
    monkeypatch.delenv("FLEET_ROOT")
    with pytest.raises(MessageContextError, match="local human messaging requires a bound selected caller"):
        resolve_message_route("worker", package=package)
    monkeypatch.setenv("BOT_ID", "manager")
    with pytest.raises(PlanError, match="managed Git installation"):
        resolve_message_route("worker", package=source_package())


@pytest.fixture
def git_selected(selected):
    root, package = selected
    return root, package, active_config.read_current_config(root, installation=package.installation)


def test_git_route_uses_current_config_and_retained_ids_without_plane(git_selected):
    root, package, snapshot = git_selected
    route = resolve_message_route('worker', package=package)
    assert route.activation_id is route.plan_id is route.release_id is None
    assert (route.revision, route.config_fingerprint) == (snapshot['revision'], snapshot['fingerprint'])
    assert route.caller.uid == snapshot['identities']['fleets']['example']['bots']['manager']
    assert route.peer.uid == snapshot['identities']['fleets']['example']['bots']['worker']
    binding = route.receipt_binding()
    assert binding.revision == route.revision and binding.config_fingerprint == route.config_fingerprint
    assert binding.peer_destination.socket == 'com.example.worker'
    assert not db_file(root).exists()
    from claudlobby.message_context import resolve_human_reply_route
    from claudlobby.task_operations import TaskActor
    reply = resolve_human_reply_route(TaskActor('actor_' + '9' * 32, 'human:owner'), package=package)
    assert reply.release_id is None and reply.revision == route.revision
    assert reply.receipt_binding('msg_' + '8' * 32).config_fingerprint == route.config_fingerprint


def test_git_route_refuses_current_snapshot_change_during_resolution(git_selected, monkeypatch):
    from claudlobby import active_config, message_context
    root, package, snapshot = git_selected
    original = message_context.read_current_identity_bindings
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        updated = deepcopy(snapshot)
        updated['fleets']['example']['fleet'] += '\n# approved configuration changed\n'
        updated['fingerprint'] = active_config._fingerprint(updated)
        active_config.publish_current_config(updated, installation=package.installation,
                                             expected_previous=snapshot['fingerprint'])
        return result
    monkeypatch.setattr(message_context, 'read_current_identity_bindings', changed)
    with pytest.raises(MessageContextError, match='selection changed'):
        resolve_message_route('worker', package=package)
