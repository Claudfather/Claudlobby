"""Pending authoring edits cannot change active routing or project closeability."""

from dataclasses import replace
from copy import deepcopy
import json
import stat
from types import SimpleNamespace

import pytest

from claudlobby import active_config, activation_identity, activation_state, operation_context
from claudlobby.config import load_fleet
from claudlobby.config_plan import ConfigPlanBuilder, PlanError, RenderedConfiguration, read_rendered
from claudlobby.installation import Installation
from tests.package_fixtures import source_package


@pytest.fixture
def frozen(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    installation = Installation(tmp_path / "installation", root)
    package = replace(source_package(), installation=installation, source_revision="a"*40, artifact_id="")
    manifest = root / "fleet.yaml"
    manifest.write_text(json.dumps({"fleet": {
        "name": "example", "manager": "manager", "service_prefix": "com.example",
        "system_defaults": False, "bots": {
            "manager": {"expertise": ["orchestration"]},
            "worker": {"expertise": ["engineering"], "scope": {"repos": ["owner/repo"]}},
        },
    }}))
    projects = root / "projects.yaml"
    builder = ConfigPlanBuilder(root, fleets=("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    return root, manifest, projects, builder, package


def _commit(plan, package):
    snapshot = active_config.snapshot_from_plan(plan,package.installation,package.source_revision)
    active_config.publish_current_config(snapshot,installation=package.installation,expected_previous=None)


def test_active_configuration_ignores_unactivated_edits_and_removed_sources(frozen):
    root, manifest, projects, builder, package = frozen
    expected = load_fleet(manifest)
    plan = builder.freeze(package.source_revision)
    _commit(plan, package)  # Real durable records only; no native lifecycle is asserted.
    document = json.loads(manifest.read_text())
    document["fleet"]["manager"] = "worker"
    manifest.write_text(json.dumps(document))
    projects.write_text("projects: {other: {repos: [owner/other], validation: {tier: operator}}}\n")
    with pytest.raises(PlanError, match="input changed"):
        plan.check_fresh()
    manifest.unlink()
    projects.unlink()
    context = active_config.resolve_active_context(root=root, bot="worker", package=package)
    assert (context.fleet, context.merged_defaults) == expected
    assert context.fleet.manager == "manager" and context.fleet.projects_derived
    assert context.paths.fleet_yaml == manifest and context.bot_id == "worker"
    with pytest.raises(PlanError, match="no selected frozen fleet"):
        active_config.resolve_active_context(root=root, fleet="not-active", package=package)
    with pytest.raises(PlanError, match="managed Git installation"):
        active_config.resolve_active_context(root=root, package=source_package())


def test_declared_project_source_and_link_target_are_frozen_by_the_shared_parser(frozen):
    root, manifest, projects, builder, package = frozen
    target = root / "project-source.yaml"
    target.write_text("projects: {declared: {repos: [owner/repo], validation: {tier: review}}}\n")
    projects.symlink_to(target)
    # A new proposal is required when an absent source becomes present.
    with pytest.raises(PlanError, match="input changed"):
        builder.input_content(projects)
    builder = ConfigPlanBuilder(root, fleets=("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    expected = load_fleet(manifest)
    plan = builder.freeze(package.source_revision)
    _commit(plan, package)
    document = json.loads(manifest.read_text())
    document["fleet"]["manager"] = "worker"
    manifest.write_text(json.dumps(document))
    target.write_text("projects: {declared: {repos: [owner/other], validation: {tier: human}}}\n")
    context = active_config.resolve_active_context(root=root, package=package)
    assert (context.fleet, context.merged_defaults) == expected
    assert context.fleet.manager == "manager"
    assert not context.fleet.projects_derived and set(context.fleet.projects) == {"declared"}
    assert context.fleet.projects["declared"].repos == ["owner/repo"]
    assert context.fleet.projects["declared"].validation.tier == "review"
    with pytest.raises(PlanError, match="input changed"):
        plan.check_fresh()
    manifest.unlink()
    projects.unlink()
    target.unlink()
    context = active_config.resolve_active_context(root=root, package=package)
    assert (context.fleet, context.merged_defaults) == expected
    assert context.fleet.manager == "manager"
    assert context.fleet.projects["declared"].validation.tier == "review"


def test_committed_snapshot_does_not_reread_pending_render_or_authored_fallback(frozen):
    root, manifest, projects, builder, package = frozen
    plan = builder.freeze(package.source_revision)
    _commit(plan, package)
    digest = plan.effects["fleet_sources"]["example"]["fleet"]["sha256"]
    blob = plan.directory / "files" / digest
    blob.write_text("changed pending render bytes")
    with pytest.raises(PlanError, match="changed staged content"):
        read_rendered(root, plan.fingerprint)
    assert active_config.resolve_active_context(root=root, package=package).fleet.manager == "manager"
    blob.unlink()
    manifest.unlink()
    assert active_config.resolve_active_context(root=root, package=package).fleet.manager == "manager"


def test_selected_active_fleet_refuses_redirected_directory(frozen):
    root, original_manifest, _, builder, package = frozen
    fleet_dir = root / "local" / "example"
    fleet_dir.mkdir(parents=True)
    manifest = fleet_dir / "fleet.yaml"
    manifest.write_bytes(original_manifest.read_bytes())
    projects = fleet_dir / "projects.yaml"
    builder = ConfigPlanBuilder(root, fleets=("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    plan = builder.freeze(package.source_revision)
    _commit(plan, package)
    assert active_config.resolve_active_context(root=root, package=package).paths.fleet_dir == fleet_dir

    other = root / "local" / "other"
    other.mkdir()
    (other / "fleet.yaml").write_text("fleet: {name: other}\n")
    fleet_dir.rename(root / "local" / "parked")
    fleet_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises(PlanError, match="active fleet directory is redirected"):
        active_config.resolve_active_context(root=root, package=package)


@pytest.fixture
def git_snapshot(frozen):
    root, manifest, projects, builder, _ = frozen
    installation = Installation(root.parent / "installation", root)
    revision = "a" * 40
    package = replace(source_package(), installation=installation, source_revision=revision,
                      artifact_id="", content_sha256="")
    builder.file(root / "runtime/bots/worker/bot.conf", b"frozen generated bytes\n", mode=0o600)
    builder.tree(root / "runtime/bots/worker/.claude/skills", {"example/SKILL.md": (b"skill", 0o644)})
    plan = builder.freeze(package.source_revision)
    return root, manifest, projects, plan, installation, package


@pytest.fixture
def git_registry(git_snapshot):
    root = git_snapshot[0]
    host = "host_" + "1" * 32
    path = root / "state/host-uid"
    path.write_text(host + "\n")
    path.chmod(0o600)
    return [{"fleet": "example", "bindings": {
        "host_uid": host, "fleet_uid": "fleet_" + "2" * 32,
        "manager_uid": "actor_" + "3" * 32,
        "bots": {"manager": "actor_" + "3" * 32, "worker": "actor_" + "4" * 32}}}]


def test_git_uninitialized_identity_keeps_context_readable_but_refuses_actions(git_snapshot, monkeypatch):
    root, _, _, plan, installation, package = git_snapshot
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    assert snapshot["identities"] is None
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail("sealed fallback"))
    assert active_config.resolve_active_context(root=root, package=package).fleet.manager == "manager"
    with pytest.raises(activation_state.ActivationError, match="not initialized"):
        activation_identity.read_current_identity_bindings(root, "example", package=package)


def test_native_placement_is_part_of_the_committed_configuration(git_snapshot):
    root, _, _, plan, installation, package = git_snapshot
    placement = {"manager": "Linux", "domain": "", "directory": str(root.parent / "native-units")}
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision,
                                                supervision=placement)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    saved = active_config.read_current_config(root, installation=installation)
    assert saved["supervision"] == placement
    saved["supervision"]["directory"] = str(root.parent / "other-units")
    with pytest.raises(PlanError, match="fingerprint"):
        active_config._validate_snapshot(saved, installation)
    with pytest.raises(PlanError, match="placement"):
        active_config.snapshot_from_plan(plan, installation, package.source_revision,
                                         supervision={"manager": "Linux", "domain": "", "directory": str(root / "runtime")})


def test_git_identity_is_committed_with_frozen_roster_and_current_host(git_snapshot, git_registry, monkeypatch):
    root, manifest, _, plan, installation, package = git_snapshot
    identities = activation_identity.identity_bindings_from_registry(plan, git_registry, package=package)
    assert set(identities) == {"schema", "host_uid", "fleets"}
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision, identities=identities)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail("sealed fallback"))
    manifest.unlink()  # Existing committed identities do not rediscover authoring or Plane.
    selected = activation_identity.read_current_identity_bindings(root, "example", package=package)
    assert selected == {**git_registry[0]["bindings"], "manager": "manager"}
    identities["fleets"]["example"]["bots"]["worker"] = "actor_" + "5" * 32
    assert activation_identity.read_current_identity_bindings(root, "example", package=package) == selected
    (root / "state/host-uid").write_text("host_" + "6" * 32 + "\n")
    with pytest.raises(PlanError, match="identity"):
        activation_identity.read_current_identity_bindings(root, "example", package=package)


@pytest.mark.parametrize("drift", ["host", "fleet", "manager", "bot", "ambiguous"])
def test_git_identity_commit_refuses_foreign_or_torn_bindings(git_snapshot, git_registry, drift):
    _, _, _, plan, installation, package = git_snapshot
    identities = activation_identity.identity_bindings_from_registry(plan, git_registry, package=package)
    item = identities["fleets"]["example"]
    if drift == "host":
        identities["host_uid"] = "host_" + "0" * 32
    elif drift == "fleet":
        identities["fleets"]["other"] = identities["fleets"].pop("example")
    elif drift == "manager":
        item["manager"] = "worker"
    elif drift == "bot":
        item["bots"].pop("worker")
    else:
        item["bots"]["worker"] = item["manager_uid"]
    with pytest.raises(PlanError, match="identity"):
        active_config.snapshot_from_plan(plan, installation, package.source_revision, identities=identities)


def test_git_registry_binding_uses_existing_read_owner_without_registration(
        git_snapshot, git_registry, monkeypatch):
    root, manifest, _, plan, _, package = git_snapshot
    original = deepcopy(git_registry[0]["bindings"])
    observed = []

    def bind(context, *, origin):
        observed.append((context.fleet.manager, origin.bot_id, set(context.fleet.bots)))
        return SimpleNamespace(host_uid=original["host_uid"], fleet_uid=original["fleet_uid"],
                               caller=SimpleNamespace(uid=original["manager_uid"]),
                               bots={name: SimpleNamespace(uid=uid) for name, uid in original["bots"].items()})

    monkeypatch.setattr(operation_context, "bind_task_context", bind)
    manifest.unlink()
    payload = activation_identity.read_registry_identity_bindings(plan, package=package)
    assert observed == [("manager", "manager", {"manager", "worker"})]
    assert payload["fleets"]["example"]["bots"] == original["bots"]
    def unavailable(*_args, **_kwargs):
        raise operation_context.OperationContextUnavailableError("registry unavailable")
    monkeypatch.setattr(operation_context, "bind_task_context", unavailable)
    with pytest.raises(activation_state.ActivationError, match="registry identity bindings are unavailable"):
        activation_identity.read_registry_identity_bindings(plan, package=package)
    assert not (root / "state/plane/plane.db").exists()


@pytest.fixture
def registry_render(git_snapshot):
    root, _, _, plan, _, package = git_snapshot
    render = RenderedConfiguration(plan.directory, root, package.source_revision, plan.fleets,
                                   plan.inputs, plan.changes, plan.effects)
    return render, package


def test_git_registry_scan_commits_frozen_roster_then_binds_existing_ids(
        registry_render, git_registry, monkeypatch):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    original = git_registry[0]["bindings"]
    observed = []

    def preflight(paths, fleet):
        observed.append(("preflight", fleet.name))
        assert paths.package == package

    def scan(paths, fleet, *, require_commit):
        observed.append(("scan", fleet.name))
        assert paths.root == render.data_root and require_commit is True
        assert fleet.manager == "manager" and set(fleet.bots) == {"manager", "worker"}
        return {"complete": True, "recording": "committed", "scan_id": "existing-scan"}

    def bind(context, *, origin):
        observed.append(("bind", context.fleet.name))
        assert origin.bot_id == context.fleet.manager
        return SimpleNamespace(host_uid=original["host_uid"], fleet_uid=original["fleet_uid"],
                               caller=SimpleNamespace(uid=original["manager_uid"]),
                               bots={name: SimpleNamespace(uid=uid) for name, uid in original["bots"].items()})

    monkeypatch.setattr(registry_emit, "activation_scan_refusal", preflight)
    monkeypatch.setattr(registry_emit, "run_generate_scan", scan)
    monkeypatch.setattr(operation_context, "bind_task_context", bind)
    (render.data_root / "fleet.yaml").unlink()
    contexts = [active_config.context_from_plan(render, "example", package=package)]
    rows = activation_identity.scan_registry(render, package=package, contexts=contexts)
    assert observed == [("preflight", "example"), ("scan", "example"), ("bind", "example")]
    assert rows == [{**git_registry[0], "scan": {
        "complete": True, "recording": "committed", "scan_id": "existing-scan"}}]
    identities = activation_identity.identity_bindings_from_registry(render, rows, package=package)
    assert identities["fleets"]["example"]["bots"] == original["bots"]
    assert not (render.data_root / "state/config.json").exists()
    assert not (render.data_root / "state/plane/plane.db").exists()


def test_registry_scan_preflights_all_fleets_before_first_emission(monkeypatch):
    from claudlobby.plane import registry_emit
    observed = []
    reasons = {"alpha": "unavailable", "beta": "opted out", "gamma": "unavailable"}

    def refusal(_paths, fleet):
        observed.append(fleet.name)
        return reasons[fleet.name]

    monkeypatch.setattr(registry_emit, "activation_scan_refusal", refusal)
    contexts = [SimpleNamespace(paths=None, fleet=SimpleNamespace(name=name)) for name in reasons]
    with pytest.raises(activation_state.ActivationRefusal, match="^unavailable; opted out$"):
        activation_identity.preflight_registry_scan(contexts)
    assert observed == list(reasons)


@pytest.mark.parametrize("refusal", ["opted out", "tier resolution unavailable"])
def test_git_registry_scan_refusal_has_no_scan_or_binding_effect(registry_render, monkeypatch, refusal):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    monkeypatch.setattr(registry_emit, "activation_scan_refusal", lambda *_: refusal)
    monkeypatch.setattr(registry_emit, "run_generate_scan", lambda *_args, **_kwargs: pytest.fail("scan"))
    monkeypatch.setattr(operation_context, "bind_task_context", lambda *_args, **_kwargs: pytest.fail("bind"))
    with pytest.raises(activation_state.ActivationRefusal, match=refusal):
        activation_identity.scan_registry(render, package=package)


@pytest.mark.parametrize("result", [None, {"complete": False, "recording": "committed"},
                                     {"complete": True, "recording": "uncommitted"}])
def test_git_registry_scan_requires_complete_committed_result_before_binding(
        registry_render, monkeypatch, result):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    monkeypatch.setattr(registry_emit, "activation_scan_refusal", lambda *_: None)
    monkeypatch.setattr(registry_emit, "run_generate_scan", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(operation_context, "bind_task_context", lambda *_args, **_kwargs: pytest.fail("bind"))
    with pytest.raises(activation_state.ActivationError, match="disabled, incomplete or uncommitted"):
        activation_identity.scan_registry(render, package=package)


def test_git_registry_scan_preserves_original_unknown_scan_exception(registry_render, monkeypatch):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    failure = registry_emit.RegistryScanError("recording unknown", {"recording": "unknown", "scan_id": "same-scan"})
    calls = []

    def scan(*_args, **_kwargs):
        calls.append("scan")
        raise failure

    monkeypatch.setattr(registry_emit, "activation_scan_refusal", lambda *_: None)
    monkeypatch.setattr(registry_emit, "run_generate_scan", scan)
    monkeypatch.setattr(operation_context, "bind_task_context", lambda *_args, **_kwargs: pytest.fail("bind"))
    with pytest.raises(registry_emit.RegistryScanError) as raised:
        activation_identity.scan_registry(render, package=package)
    assert raised.value is failure and calls == ["scan"]
    assert raised.value.summary == {"recording": "unknown", "scan_id": "same-scan"}


def test_git_registry_scan_rejects_foreign_frozen_context_before_preflight(registry_render, monkeypatch):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    context = active_config.context_from_plan(render, "example", package=package)
    monkeypatch.setattr(registry_emit, "activation_scan_refusal", lambda *_: pytest.fail("preflight"))
    with pytest.raises(activation_state.ActivationError, match="contexts differ"):
        activation_identity.scan_registry(render, package=package, contexts=[replace(context, bot_id="worker")])


def test_git_registry_scan_refuses_unavailable_binding_after_committed_scan(registry_render, monkeypatch):
    from claudlobby.plane import registry_emit
    render, package = registry_render
    observed = []

    def scan(*_args, **_kwargs):
        observed.append("committed")
        return {"complete": True, "recording": "committed"}

    def bind(*_args, **_kwargs):
        raise operation_context.OperationContextUnavailableError("existing registry unavailable")

    monkeypatch.setattr(registry_emit, "activation_scan_refusal", lambda *_: None)
    monkeypatch.setattr(registry_emit, "run_generate_scan", scan)
    monkeypatch.setattr(operation_context, "bind_task_context", bind)
    with pytest.raises(activation_state.ActivationError, match="registry identity binding failed") as raised:
        activation_identity.scan_registry(render, package=package)
    assert isinstance(raised.value.__cause__, operation_context.OperationContextUnavailableError)
    assert observed == ["committed"]


def test_git_snapshot_commit_precedes_consumers_and_survives_startup_failure(git_snapshot):
    root, manifest, projects, plan, installation, package = git_snapshot
    expected = load_fleet(manifest)
    with pytest.raises(PlanError, match="missing or invalid"):
        active_config.load_active_context(root, None, package=package)
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    assert snapshot["fleets"]["example"]["fleet"].encode() == manifest.read_bytes()
    assert snapshot["fleets"]["example"]["projects"] is None
    assert snapshot["owned_outputs"][0]["target"] == plan.changes[0].target
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    path = root / "state/config.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    inode = path.stat().st_ino
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    assert path.stat().st_ino == inode  # Recovery observes the existing commit.
    (root / "state/update.json").write_text(json.dumps({
        "schema": 1, "phase": "starting", "consumers": {"worker": {"error": "failed readiness"}}}))
    manifest.unlink()
    projects.write_text("projects: {pending: {repos: [owner/other]}}\n")
    context = active_config.load_active_context(root, None, bot="worker", package=package)
    assert (context.fleet, context.merged_defaults) == expected
    assert context.bot_id == "worker"
    # The effective record needs neither staged blobs nor any activation journal.
    import shutil
    shutil.rmtree(plan.directory)
    assert active_config.load_active_context(root, "example", package=package).fleet.manager == "manager"


def test_git_snapshot_does_not_fall_back_to_legacy_or_pending_sources(git_snapshot):
    root, manifest, _, plan, installation, package = git_snapshot
    (root / "state/selected-release.json").write_text("historical evidence never supplies current config")
    with pytest.raises(PlanError, match="missing or invalid"):
        active_config.load_active_context(root, None, package=package)
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    path = root / "state/config.json"
    path.write_text("broken snapshot")
    assert manifest.is_file()
    with pytest.raises(PlanError, match="missing or invalid"):
        active_config.load_active_context(root, None, package=package)


def test_git_snapshot_refuses_provenance_drift_and_redirects(git_snapshot, tmp_path):
    root, _, _, plan, installation, package = git_snapshot
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    with pytest.raises(PlanError, match="provenance"):
        active_config.load_active_context(root, None, package=replace(package, source_revision="b" * 40))
    other = Installation(tmp_path / "other-installation", root)
    with pytest.raises(PlanError, match="provenance"):
        active_config.read_current_config(root, installation=other)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(PlanError, match="another data root"):
        active_config.read_current_config(alias, installation=installation)
    path = root / "state/config.json"
    parked = root / "snapshot.json"
    path.rename(parked)
    path.symlink_to(parked)
    with pytest.raises(PlanError, match="redirected"):
        active_config.read_current_config(root, installation=installation)


def test_git_snapshot_publish_checks_previous_fingerprint(git_snapshot):
    root, _, _, plan, installation, package = git_snapshot
    first = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    active_config.publish_current_config(first, installation=installation, expected_previous=None)
    second = active_config.snapshot_from_plan(replace(plan, revision="b" * 40), installation, "b" * 40)
    with pytest.raises(PlanError, match="changed before commit"):
        active_config.publish_current_config(second, installation=installation, expected_previous=None)
    assert active_config.read_current_config(root, installation=installation) == first
    active_config.publish_current_config(second, installation=installation,
                                         expected_previous=first["fingerprint"])
    assert active_config.read_current_config(root, installation=installation) == second


@pytest.mark.parametrize("destination", ["state/owner/grants.json", "runtime/bots/worker/memory/notes",
                                         "runtime/bots/worker/projects/repo/file", "runtime/bots/worker/data/private"])
def test_git_snapshot_ownership_cannot_include_private_mutable_files(git_snapshot, destination):
    root, _, _, plan, installation, package = git_snapshot
    from claudlobby.config_plan import ConfigChange
    bad = replace(plan, changes=(*plan.changes, ConfigChange(
        str(root / destination), {"node": {"kind": "absent"}},
        {"kind": "file", "mode": 0o600, "sha256": "0" * 64})))
    with pytest.raises(PlanError, match="escapes generated"):
        active_config.snapshot_from_plan(bad, installation, package.source_revision)


def test_git_read_stamp_ignores_historical_selection_and_detects_config_change(git_snapshot, monkeypatch):
    from claudlobby.command_result import CommandFailure
    root, _, _, plan, installation, package = git_snapshot
    snapshot = active_config.snapshot_from_plan(plan, installation, package.source_revision)
    active_config.publish_current_config(snapshot, installation=installation, expected_previous=None)
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail("historical selection read"))
    stamp = active_config.runtime_read_stamp(root, package=package)
    assert stamp.release_id is None and stamp.cursor_scope == stamp.provenance
    assert stamp.provenance == {"source_revision": package.source_revision, "config_fingerprint": snapshot["fingerprint"]}
    stamp.check(root, package=package)
    changed = deepcopy(snapshot)
    changed["identities"] = None
    changed["fleets"]["example"]["fleet"] += "\n# new reviewed configuration epoch\n"
    changed["fingerprint"] = active_config._fingerprint(changed)
    active_config.publish_current_config(changed, installation=installation, expected_previous=snapshot["fingerprint"])
    with pytest.raises(CommandFailure, match="changed"):
        stamp.check(root, package=package)


def test_unmanaged_read_stamp_never_uses_historical_selection(frozen, monkeypatch):
    root, _, _, _, package = frozen
    monkeypatch.setattr(activation_state,"read_selection",lambda *_:pytest.fail("historical selection read"))
    with pytest.raises(PlanError,match="managed Git installation"):
        active_config.runtime_read_stamp(root,package=replace(package,installation=None))
