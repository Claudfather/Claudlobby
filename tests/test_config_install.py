"""Configuration recovery on private filesystems; no services or application imports."""

import json
from pathlib import Path

import pytest

from claudlobby import config_install as install
from claudlobby.config_plan import ConfigPlanBuilder, PlanError
from tests.test_releases import installed, r


def _file(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _tree(root):
    return {str(p.relative_to(root)): (
        ("link", str(p.readlink())) if p.is_symlink() else
        ("dir", p.stat().st_mode & 0o777) if p.is_dir() else
        ("file", p.read_bytes(), p.stat().st_mode & 0o777)
    ) for p in root.rglob("*")}


@pytest.fixture
def proposal(installed, tmp_path):
    root, inputs, paths, _compatibility, _directory = installed
    release = r.seal_release(root, inputs, paths)
    manifest = _file(root / "fleet.yaml", b"fleet: reviewed\n")
    runtime = root / "runtime"
    settings = _file(runtime / "settings.json", b"old settings\n", 0o600)
    retired = _file(runtime / "retired.sh", b"old command\n", 0o755)
    link = runtime / "cli"
    link.symlink_to("previous-cli")
    # This destination is outside the host data tree. All its rename work
    # must stay beside it, also when a real vault is on another filesystem.
    external = tmp_path / "vault/runtime/skills"
    _file(external / "old/SKILL.md", b"old skill\n", 0o640)
    (external / "old/link").symlink_to("SKILL.md")
    (external / "old").chmod(0o550)
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("fleet",), effects={"reload_supervision": True})
    builder.input(manifest)
    builder.file(settings, b"candidate settings\n")
    builder.symlink(link, release.cli_path)
    builder.remove(retired)
    builder.directory(runtime / "bot/data")
    builder.file(runtime / "new/channel/access.json", b'{"candidate":true}\n', mode=0o600)
    builder.tree(external, {"new/SKILL.md": (b"frozen candidate skill\n", 0o644),
                            "new/run.sh": (b"#!/bin/sh\nexit 0\n", 0o755)})
    plan = builder.seal()
    return plan, runtime, external, manifest, settings


def test_partial_config_activation_rolls_back_exact_before_and_candidate_only_assets(proposal, monkeypatch):
    plan, runtime, external, _, settings = proposal
    before = _tree(runtime), _tree(external)
    prepared = install.prepare_config(plan, "partial")
    assert prepared.status == "prepared"
    assert prepared.directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (prepared.directory / "blobs").iterdir())
    replace = install._replace

    def stop_at_settings(source, target):
        if target == settings:
            raise InterruptedError("power lost during partial config activation")
        replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(install, "_replace", stop_at_settings)
        with pytest.raises(InterruptedError, match="partial config activation"):
            install.apply_config(plan.data_root, "partial")
    assert install.read_config_install(plan.data_root, "partial").status == "applying"
    assert (runtime / "new/channel/access.json").is_file()
    assert not (runtime / "retired.sh").exists()
    rolled_back = install.rollback_config(plan.data_root, "partial")
    assert rolled_back.status == "rolled_back"
    assert (_tree(runtime), _tree(external)) == before
    assert rolled_back.retained_directories == ()
    assert (prepared.directory / "blobs").is_dir()  # exact backup retained
    with pytest.raises(install.ConfigInstallError, match="rollback has begun"):
        install.apply_config(plan.data_root, "partial")


def test_interrupted_tree_resume_uses_frozen_bytes_and_rollback_preserves_mutable_children(proposal, monkeypatch):
    plan, runtime, external, _, _ = proposal
    old_tree = _tree(external)
    prepared = install.prepare_config(plan, "tree-resume")
    replace = install._replace
    moved = []

    def stop_after_old_tree_moved(source, target):
        replace(source, target)
        if source == external and target.name == "previous":
            assert target.parent.parent == external.parent
            assert not target.is_relative_to(prepared.directory)
            moved.append(target)
            raise InterruptedError("between directory renames")

    with monkeypatch.context() as patch:
        patch.setattr(install, "_replace", stop_after_old_tree_moved)
        with pytest.raises(InterruptedError, match="between directory renames"):
            install.apply_config(plan.data_root, "tree-resume")
    assert moved and not external.exists()
    # Recovery reads its own verified frozen blobs, not mutable authoring or
    # a plan-store blob that could disappear after activation started.
    for blob in (plan.directory / "files").iterdir():
        blob.unlink()
    assert install.apply_config(plan.data_root, "tree-resume").status == "applied"
    assert (external / "new/SKILL.md").read_bytes() == b"frozen candidate skill\n"
    assert (external / "new/run.sh").stat().st_mode & 0o777 == 0o755
    assert not (external / "old").exists()
    durable = _file(runtime / "bot/data/new-state.json", b"durable work after application\n")
    with monkeypatch.context() as patch:
        patch.setattr(install, "_replace", stop_after_old_tree_moved)
        with pytest.raises(InterruptedError, match="between directory renames"):
            install.rollback_config(plan.data_root, "tree-resume")
    assert install.read_config_install(plan.data_root, "tree-resume").status == "rolling_back"
    remove_tree = install.shutil.rmtree

    def stop_during_private_cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == external.parent and path.name.startswith(".claudlobby-config-"):
            next(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()).unlink()
            raise InterruptedError("during owned workspace cleanup")
        return remove_tree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(install.shutil, "rmtree", stop_during_private_cleanup)
        with pytest.raises(InterruptedError, match="owned workspace cleanup"):
            install.rollback_config(plan.data_root, "tree-resume")
    recovered = install.rollback_config(plan.data_root, "tree-resume")
    assert recovered.status == "rolled_back"
    assert _tree(external) == old_tree
    assert durable.read_bytes() == b"durable work after application\n"
    assert runtime / "bot/data" in recovered.retained_directories
    assert not (runtime / "new").exists()
    assert not list(external.parent.glob(".claudlobby-config-*"))


def test_changed_foreign_target_refuses_before_other_writes_and_backup_tamper_refuses(proposal):
    plan, runtime, external, _, settings = proposal
    prepared = install.prepare_config(plan, "foreign")
    install.apply_config(plan.data_root, "foreign")
    settings.write_bytes(b"operator changed configuration\n")
    before = _tree(runtime), _tree(external)
    for operation in (install.apply_config, install.rollback_config):
        with pytest.raises(install.ConfigInstallError, match="foreign configuration change"):
            operation(plan.data_root, "foreign")
        assert (_tree(runtime), _tree(external)) == before
    settings.write_bytes(b"candidate settings\n")
    backup = next(p for p in (prepared.directory / "blobs").iterdir()
                  if p.read_bytes() == b"old settings\n")
    backup.write_bytes(b"corrupted backup\n")
    before = _tree(runtime), _tree(external)
    with pytest.raises(install.ConfigInstallError, match="changed configuration backup"):
        install.rollback_config(plan.data_root, "foreign")
    assert (_tree(runtime), _tree(external)) == before


def test_stale_plan_or_wrong_activation_identity_never_prepares_or_overwrites(proposal):
    plan, runtime, external, manifest, _ = proposal
    manifest.write_bytes(b"changed fleet\n")
    before = _tree(runtime), _tree(external)
    with pytest.raises(PlanError, match="input changed"):
        install.prepare_config(plan, "stale")
    assert not (plan.data_root / "state/activations").exists()
    assert (_tree(runtime), _tree(external)) == before
    manifest.write_bytes(b"fleet: reviewed\n")
    prepared = install.prepare_config(plan, "bound")
    journal = (prepared.directory / "journal.json").read_bytes()
    other = ConfigPlanBuilder(plan.data_root, plan.release_id, plan.release_seal,
                              ("different",), effects={}).seal()
    with pytest.raises(install.ConfigInstallError, match="different plan"):
        install.prepare_config(other, "bound")
    assert (prepared.directory / "journal.json").read_bytes() == journal
    envelope = json.loads(journal)
    envelope["journal"]["plan_id"] = other.plan_id
    (prepared.directory / "journal.json").write_text(json.dumps(envelope))
    with pytest.raises(install.ConfigInstallError, match="identity changed"):
        install.apply_config(plan.data_root, "bound")
    assert (_tree(runtime), _tree(external)) == before
