"""Pending authoring edits cannot change active routing or project closeability."""

from dataclasses import replace
import json

import pytest

from claudlobby import active_config, activation_state
from claudlobby.config import load_fleet
from claudlobby.config_plan import ConfigPlanBuilder, PlanError, read_plan
from claudlobby.releases import seal_release
from tests.package_fixtures import source_package
from tests.test_releases import installed
from tests.test_runtime_admission import _active


@pytest.fixture
def frozen(installed):
    root, inputs, paths, _, directory = installed
    release = seal_release(root, inputs, paths)
    package = replace(source_package(), native=release.native_path, artifact_id=inputs.artifact_id)
    manifest = root / "fleet.yaml"
    manifest.write_text(json.dumps({"fleet": {
        "name": "example", "manager": "manager", "service_prefix": "com.example",
        "system_defaults": False, "bots": {
            "manager": {"expertise": ["orchestration"]},
            "worker": {"expertise": ["engineering"], "scope": {"repos": ["owner/repo"]}},
        },
    }}))
    projects = root / "projects.yaml"
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    return root, manifest, projects, builder, package


def test_active_configuration_ignores_unactivated_edits_and_removed_sources(frozen):
    root, manifest, projects, builder, package = frozen
    expected = load_fleet(manifest)
    plan = builder.seal()
    _active(plan)  # Real durable records only; no native lifecycle is asserted.
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
    with pytest.raises(PlanError, match="complete frozen"):
        active_config.resolve_active_context(root=root, fleet="not-active", package=package)
    with pytest.raises(activation_state.ActivationError, match="executing package differ"):
        active_config.resolve_active_context(root=root, package=source_package())


def test_declared_project_source_and_link_target_are_frozen_by_the_shared_parser(frozen):
    root, manifest, projects, builder, package = frozen
    target = root / "project-source.yaml"
    target.write_text("projects: {declared: {repos: [owner/repo], validation: {tier: review}}}\n")
    projects.symlink_to(target)
    # A new proposal is required when an absent source becomes present.
    with pytest.raises(PlanError, match="input changed"):
        builder.input_content(projects)
    builder = ConfigPlanBuilder(root, builder.release_id, builder.release_seal, ("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    expected = load_fleet(manifest)
    plan = builder.seal()
    _active(plan)
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


def test_lost_or_changed_frozen_inputs_refuse_without_source_fallback(frozen):
    root, manifest, projects, builder, package = frozen
    plan = builder.seal()
    _active(plan)
    assert active_config.resolve_active_context(root=root, package=package).fleet.name == "example"
    digest = plan.effects["fleet_sources"]["example"]["fleet"]["sha256"]
    blob = plan.directory / "files" / digest
    blob.write_text("changed private plan bytes")
    with pytest.raises(PlanError, match="changed staged content"):
        read_plan(root, plan.plan_id)
    with pytest.raises(PlanError, match="changed staged content"):
        active_config.resolve_active_context(root=root, package=package)
    blob.unlink()
    with pytest.raises(PlanError, match="missing staged content"):
        read_plan(root, plan.plan_id)
    assert manifest.is_file()  # Its valid authoring copy cannot repair a sealed plan.
    with pytest.raises(PlanError, match="missing staged content"):
        active_config.resolve_active_context(root=root, package=package)


def test_selected_active_fleet_refuses_redirected_directory(frozen):
    root, original_manifest, _, builder, package = frozen
    fleet_dir = root / "local" / "example"
    fleet_dir.mkdir(parents=True)
    manifest = fleet_dir / "fleet.yaml"
    manifest.write_bytes(original_manifest.read_bytes())
    projects = fleet_dir / "projects.yaml"
    builder = ConfigPlanBuilder(root, builder.release_id, builder.release_seal, ("example",),
                               effects={"fleet_manifests": {"example": str(manifest)}})
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    plan = builder.seal()
    _active(plan)
    assert active_config.resolve_active_context(root=root, package=package).paths.fleet_dir == fleet_dir

    other = root / "local" / "other"
    other.mkdir()
    (other / "fleet.yaml").write_text("fleet: {name: other}\n")
    fleet_dir.rename(root / "local" / "parked")
    fleet_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises(PlanError, match="active fleet directory is redirected"):
        active_config.resolve_active_context(root=root, package=package)
