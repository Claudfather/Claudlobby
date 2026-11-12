"""A proposed permission/config change must neither leak live nor apply stale."""

import json
from pathlib import Path

import pytest

from claudlobby.config_plan import ConfigPlan, ConfigPlanBuilder, PlanError, read_plan, read_rendered


def test_current_render_has_no_permanent_plan_or_release_store(tmp_path):
    from claudlobby.config_plan import read_rendered
    root = tmp_path / "data"
    root.mkdir()
    source = root / "fleet.yaml"
    source.write_text("reviewed input")
    target = root / "runtime/bot.conf"
    builder = ConfigPlanBuilder(root, ("demo",), effects={})
    builder.input_snapshot(source)
    builder.file(target, b"reviewed output")
    rendered = builder.freeze("a" * 40)
    assert read_rendered(root, rendered.fingerprint) == rendered
    assert not target.exists()
    assert not (root / "state/config-plans").exists()
    assert not (root / "state/releases").exists()
    assert set(rendered.payload()).isdisjoint({"plan_id", "release_id", "release_seal"})
    assert builder.freeze("a" * 40) == rendered
    with pytest.raises(PlanError, match="changed"):
        builder.freeze("b" * 40)
    source.write_text("edited after review")
    with pytest.raises(PlanError, match="input changed"):
        rendered.check_fresh()
    assert read_rendered(root, rendered.fingerprint) == rendered


def test_current_render_refuses_state_outputs_and_redirected_store(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    builder = ConfigPlanBuilder(root, ("demo",), effects={})
    builder.file(root / "state/config.json", b"cannot own updater state")
    with pytest.raises(PlanError, match="persistent state"):
        builder.freeze("a" * 40)
    builder = ConfigPlanBuilder(root, ("demo",), effects={})
    (root / "state").symlink_to(tmp_path)
    with pytest.raises(PlanError, match="redirected"):
        builder.freeze("a" * 40)


@pytest.fixture
def proposal(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    native = tmp_path / 'installation/src/claudlobby/_runtime_scripts'
    native.mkdir(parents=True)
    manifest = root / "fleet.yaml"
    manifest.write_text("fleet: {name: example, manager: manager}\n")
    runtime = root / "runtime" / "bots" / "worker"
    runtime.mkdir(parents=True)
    settings = runtime / "settings.json"
    settings.write_text('{"permissions": {"allow": []}}\n')
    builder = ConfigPlanBuilder(root, ("example",), effects={"restart_bots": ["example/worker"]})
    builder.input(manifest)
    builder.file(settings, b'{"permissions":{"allow":["Bash(claudlobby *)"]}}\n')
    builder.symlink(runtime / "skill", native)
    builder.remove(runtime / "retired-tool")
    builder.tree(runtime / ".claude/skills", {
        "fleet-ops/SKILL.md": (b"# frozen operation instructions\n", 0o644),
        "fleet-ops/scripts/probe.sh": (b"#!/bin/sh\nexit 0\n", 0o755),
    })
    return builder, manifest, settings


def test_staging_is_private_and_never_changes_running_config(proposal):
    builder, manifest, settings = proposal
    previous = settings.read_bytes()
    plan = builder.freeze("a" * 40)
    assert read_rendered(builder.root, plan.fingerprint) == plan
    assert builder.freeze("a" * 40) == plan  # identical proposals are already present
    assert settings.read_bytes() == previous
    assert not (settings.parent / "skill").exists()
    assert plan.directory.stat().st_mode & 0o777 == 0o700
    assert (plan.directory / "render.json").stat().st_mode & 0o777 == 0o600
    assert "permissions" not in (plan.directory / "render.json").read_text()
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
    plan = builder.freeze("a" * 40)
    change = next(c for c in plan.changes if c.target == str(settings))
    blob = plan.directory / "files" / change.after["sha256"]
    original = blob.read_bytes()
    blob.write_bytes(b"different grants")
    with pytest.raises(PlanError, match="changed staged content"):
        read_rendered(builder.root, plan.fingerprint)
    blob.write_bytes(original)
    files = plan.directory / "files"
    retained = files.rename(plan.directory / "redirected-files")
    files.symlink_to(retained, target_is_directory=True)
    with pytest.raises(PlanError, match="missing staged content"):
        read_rendered(builder.root, plan.fingerprint)
    files.unlink()
    retained.rename(files)
    manifest = plan.directory / "render.json"
    payload = json.loads(manifest.read_text())
    payload["fleets"].append("another-fleet")
    manifest.write_text(json.dumps(payload))
    with pytest.raises(PlanError, match="pending render changed"):
        read_rendered(builder.root, plan.fingerprint)


def test_mid_render_edits_and_competing_output_owners_refuse(proposal):
    builder, manifest, settings = proposal
    with pytest.raises(PlanError, match="two renderers disagree"):
        builder.file(settings, b"different renderer")
    with pytest.raises(PlanError, match="overlapping configuration outputs"):
        builder.file(settings.parent / ".claude/skills/fleet-ops/SKILL.md", b"second owner")
    manifest.write_text("changed during rendering")
    with pytest.raises(PlanError, match="input changed"):
        builder.freeze("a" * 40)
    assert not (builder.root / "state" / "config-plans").exists()


def test_mutable_bot_data_is_not_owned_but_parent_redirection_is_checked(proposal):
    builder, _, settings = proposal
    mutable = settings.parent / "data"
    mutable.mkdir()
    builder.directory(mutable)
    plan = builder.freeze("a" * 40)
    (mutable / "new-runtime-state").write_text("legitimate ongoing work")
    plan.check_fresh()
    original = settings.parent.rename(settings.parent.with_name("old-worker"))
    settings.parent.symlink_to(original, target_is_directory=True)
    with pytest.raises(PlanError, match="destination changed"):
        plan.check_fresh()


def historical_plan(builder, *, release_id='r-' + 'a' * 64, release_seal='b' * 64):
    """Author archived plan fixture bytes without a production plan writer."""
    from dataclasses import replace
    from claudlobby.config_plan import _json
    plan = ConfigPlan(builder.root / 'state/config-plans', builder.root, release_id, release_seal,
        builder.fleets, builder.inputs, tuple(builder.changes[key] for key in sorted(builder.changes)),
        builder.effects)
    plan = replace(plan, directory=plan.directory / plan.plan_id)
    (plan.directory / 'files').mkdir(parents=True)
    for digest, content in builder.contents.items():
        (plan.directory / 'files' / digest).write_bytes(content)
    (plan.directory / 'plan.json').write_bytes(_json(plan.payload()))
    return read_plan(plan.data_root, plan.plan_id)


def test_historical_plan_corruption_remains_read_only(proposal):
    builder, _, settings = proposal
    plan = historical_plan(builder)
    assert not hasattr(plan, 'check_fresh')
    assert not hasattr(builder, 'seal')
    change = next(item for item in plan.changes if item.target == str(settings))
    blob = plan.directory / 'files' / change.after['sha256']
    blob.write_bytes(b'corrupted archived permission bytes')
    corrupted = blob.read_bytes()
    with pytest.raises(PlanError, match='changed staged content'):
        read_plan(plan.data_root, plan.plan_id)
    assert blob.read_bytes() == corrupted


def test_historical_plan_manifest_and_redirected_blobs_refuse(proposal):
    builder, _, _ = proposal
    plan = historical_plan(builder)
    files = plan.directory / 'files'
    retained = files.rename(plan.directory / 'retained')
    files.symlink_to(retained, target_is_directory=True)
    with pytest.raises(PlanError, match='missing staged content'):
        read_plan(plan.data_root, plan.plan_id)
    files.unlink()
    retained.rename(files)
    path = plan.directory / 'plan.json'
    payload = json.loads(path.read_bytes())
    payload['fleets'].append('foreign')
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(PlanError, match='manifest changed'):
        read_plan(plan.data_root, plan.plan_id)
    assert path.read_bytes() == before
