"""A proposed permission/config change must neither leak live nor apply stale."""

import json
from pathlib import Path

import pytest

from claudlobby.config_plan import ConfigPlanBuilder, PlanError, read_plan
from tests.test_releases import installed, r


@pytest.fixture
def proposal(installed):
    root, inputs, paths, compatibility, directory = installed
    release = r.seal_release(root, inputs, paths)
    manifest = root / "fleet.yaml"
    manifest.write_text("fleet: {name: example, manager: manager}\n")
    runtime = root / "runtime" / "bots" / "worker"
    runtime.mkdir(parents=True)
    settings = runtime / "settings.json"
    settings.write_text('{"permissions": {"allow": []}}\n')
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("example",), effects={"restart_bots": ["example/worker"]})
    builder.input(manifest)
    builder.file(settings, b'{"permissions":{"allow":["Bash(claudlobby *)"]}}\n')
    builder.symlink(runtime / "skill", directory / paths.native)
    builder.remove(runtime / "retired-tool")
    builder.tree(runtime / ".claude/skills", {
        "fleet-ops/SKILL.md": (b"# frozen operation instructions\n", 0o644),
        "fleet-ops/scripts/probe.sh": (b"#!/bin/sh\nexit 0\n", 0o755),
    })
    return builder, manifest, settings


def test_staging_is_private_and_never_changes_running_config(proposal):
    builder, manifest, settings = proposal
    previous = settings.read_bytes()
    plan = builder.seal()
    assert read_plan(builder.root, plan.plan_id) == plan
    assert builder.seal() == plan  # identical proposals are already present
    assert settings.read_bytes() == previous
    assert not (settings.parent / "skill").exists()
    assert plan.directory.stat().st_mode & 0o777 == 0o700
    assert (plan.directory / "plan.json").stat().st_mode & 0o777 == 0o600
    assert "permissions" not in (plan.directory / "plan.json").read_text()
    file_change = next(change for change in plan.changes if change.target == str(settings))
    assert b"Bash(claudlobby *)" in plan.content(file_change)
    skill_change = next(c for c in plan.changes if c.after["kind"] == "tree")
    assert plan.blob(skill_change.after["files"]["fleet-ops/SKILL.md"]["sha256"]) == b"# frozen operation instructions\n"
    assert skill_change.after["files"]["fleet-ops/scripts/probe.sh"]["mode"] == 0o755
    assert not (settings.parent / ".claude/skills").exists()
    plan.check_fresh()
    manifest.write_text("different manager declaration")
    with pytest.raises(PlanError, match="input changed"):
        plan.check_fresh()
    manifest.write_text("fleet: {name: example, manager: manager}\n")
    settings.write_text("changed after review")
    with pytest.raises(PlanError, match="destination changed"):
        plan.check_fresh()


def test_changed_payload_or_manifest_is_not_a_reviewed_plan(proposal):
    builder, _, settings = proposal
    plan = builder.seal()
    change = next(c for c in plan.changes if c.target == str(settings))
    blob = plan.directory / "files" / change.after["sha256"]
    original = blob.read_bytes()
    blob.write_bytes(b"different grants")
    with pytest.raises(PlanError, match="changed staged content"):
        read_plan(builder.root, plan.plan_id)
    blob.write_bytes(original)
    manifest = plan.directory / "plan.json"
    payload = json.loads(manifest.read_text())
    payload["fleets"].append("another-fleet")
    manifest.write_text(json.dumps(payload))
    with pytest.raises(PlanError, match="manifest changed"):
        read_plan(builder.root, plan.plan_id)


def test_mid_render_edits_and_competing_output_owners_refuse(proposal):
    builder, manifest, settings = proposal
    with pytest.raises(PlanError, match="two renderers disagree"):
        builder.file(settings, b"different renderer")
    with pytest.raises(PlanError, match="overlapping configuration outputs"):
        builder.file(settings.parent / ".claude/skills/fleet-ops/SKILL.md", b"second owner")
    manifest.write_text("changed during rendering")
    with pytest.raises(PlanError, match="input changed during rendering"):
        builder.seal()
    assert not (builder.root / "state" / "config-plans").exists()


def test_mutable_bot_data_is_not_owned_but_parent_redirection_is_checked(proposal):
    builder, _, settings = proposal
    mutable = settings.parent / "data"
    mutable.mkdir()
    builder.directory(mutable)
    plan = builder.seal()
    (mutable / "new-runtime-state").write_text("legitimate ongoing work")
    plan.check_fresh()
    original = settings.parent.rename(settings.parent.with_name("old-worker"))
    settings.parent.symlink_to(original, target_is_directory=True)
    with pytest.raises(PlanError, match="destination changed"):
        plan.check_fresh()
