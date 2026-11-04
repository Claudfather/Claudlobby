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


def test_current_update_publication_resumes_without_a_second_journal(tmp_path, monkeypatch):
    from claudlobby.config_plan import read_rendered
    root = tmp_path / "data"
    first = _file(root / "runtime/first.conf", b"old first")
    second = _file(root / "runtime/second.conf", b"unchanged")
    mutable = _file(root / "runtime/bot/data/user.txt", b"keep user data")
    inode = second.stat().st_ino
    builder = ConfigPlanBuilder(root, "", "", ("demo",), effects={})
    builder.file(first, b"new first")
    builder.file(second, b"unchanged")
    rendered = builder.freeze("a" * 40)
    state = root / "state/update.json"

    # A real durable record stands in only for the update owner's process lock.
    # The test exercises real before-image files, renames and process re-reads.
    class Store:
        def __init__(self):
            self.root, self.work = root, root / "state/update-work"
        def load(self):
            return json.loads(state.read_text())
        def save(self, record):
            state.write_text(json.dumps(record))

    store = Store()
    record = {"config_fingerprint": rendered.fingerprint, "target": rendered.revision,
              "phase": "quiesce", "maintenance": False}
    store.save(record)
    assert install.prepare_publication(rendered, store) == "prepared"
    assert first.read_bytes() == b"old first"
    with pytest.raises(install.ConfigInstallError, match="maintenance phase"):
        install.apply_publication(rendered, store)
    record = store.load()
    record.update(phase="publish", maintenance=True)
    store.save(record)
    replace = install._replace

    def interrupted(source, target):
        replace(source, target)
        if target == first:
            raise InterruptedError("updater interrupted after rename")

    with monkeypatch.context() as patch:
        patch.setattr(install, "_replace", interrupted)
        with pytest.raises(InterruptedError, match="after rename"):
            install.apply_publication(rendered, store)
    assert first.read_bytes() == b"new first"
    fresh = read_rendered(root, rendered.fingerprint)
    assert install.apply_publication(fresh, Store()) == "applied"
    assert install.apply_publication(fresh, Store()) == "applied"
    assert second.stat().st_ino == inode
    assert mutable.read_bytes() == b"keep user data"
    assert not list((root / "state/update-work").rglob("journal.json"))
    assert not (root / "state/activations").exists()
    assert store.load()["publication"]["status"] == "applied"
    record = store.load()
    record["publication"]["rows"][0]["after"]["sha256"] = "f" * 64
    store.save(record)
    with pytest.raises(install.ConfigInstallError, match="bytes changed"):
        install.apply_publication(fresh, store)


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
    try:
        yield plan, runtime, external, manifest, settings
    finally:
        # Preserve the original read-only mode through each test's assertions.
        # Teardown also covers retained renamed backup trees in this fixture.
        for directory in external.parent.rglob("*"):
            if not directory.is_symlink() and directory.is_dir():
                directory.chmod(directory.stat().st_mode | 0o700)


def test_partial_config_activation_rolls_back_exact_before_and_candidate_only_assets(proposal, monkeypatch):
    plan, runtime, external, _, settings = proposal
    before = _tree(runtime), _tree(external)
    prepared = install.prepare_config(plan, "partial")
    assert prepared.status == "prepared"
    assert prepared.directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (prepared.directory / "blobs").iterdir())
    journal = json.loads((prepared.directory / "journal.json").read_bytes())["journal"]
    assert journal["schema"] == 2 and "plan" not in journal
    # Desired content is kept once, in the reviewed staged set. The installer
    # retains only old bytes that are not already available in that set.
    assert not (set(journal["blobs"]) & {p.name for p in (plan.directory / "files").iterdir()})
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
    # Recovery uses the same reviewed staged set as preview. Missing staged
    # bytes refuse before more target writes; authoring cannot repair them.
    staged = {blob: blob.read_bytes() for blob in (plan.directory / "files").iterdir()}
    for blob in staged:
        blob.unlink()
    interrupted = _tree(runtime), _tree(external.parent)
    with pytest.raises(install.ConfigInstallError, match="missing staged content"):
        install.apply_config(plan.data_root, "tree-resume")
    assert (_tree(runtime), _tree(external.parent)) == interrupted
    for blob, content in staged.items():
        _file(blob, content, 0o600)
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


def test_corrupt_retained_input_cannot_replace_a_before_image(proposal):
    original, runtime, external, _, settings = proposal
    builder = ConfigPlanBuilder(original.data_root, original.release_id, original.release_seal,
                                original.fleets, effects={})
    digest = builder.input_content(settings)
    builder.file(settings, b"replacement settings\n")
    builder.file(runtime / "earlier.json", b"another proposed output\n")
    plan = builder.seal()
    (plan.directory / "files" / digest).write_bytes(b"corrupt retained input\n")
    before = _tree(runtime), _tree(external)
    with pytest.raises(PlanError, match="changed staged content"):
        install.prepare_config(plan, "corrupt-retained")
    assert (_tree(runtime), _tree(external)) == before
    assert not (plan.data_root / "state/activations").exists()


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


def test_replaces_leaf_symlink_into_old_release_but_refuses_redirected_parent(installed):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    link = root / "runtime/bots/lead/.cli/bin/claudlobby"
    link.parent.mkdir(parents=True)
    link.symlink_to(release.directory / paths.interpreter)
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={})
    builder.symlink(link, release.cli_path)
    plan = builder.seal()
    assert install.prepare_config(plan, "replace-cli").status == "prepared"
    assert install.apply_config(root, "replace-cli").status == "applied"
    assert link.readlink() == release.cli_path
    assert install.rollback_config(root, "replace-cli").status == "rolled_back"
    assert link.readlink() == release.directory / paths.interpreter

    redirected = root / "runtime/redirected"
    redirected.symlink_to(release.directory, target_is_directory=True)
    unsafe = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={})
    unsafe.file(redirected / "unexpected", b"should never reach the release")
    with pytest.raises(install.ConfigInstallError, match="overlaps protected state"):
        install.prepare_config(unsafe.seal(), "redirected-parent")


def _re_record(plan):
    """Propose a plan's own recorded results again, as a later plan staged
    from unchanged inputs would."""
    builder = ConfigPlanBuilder(plan.data_root, plan.release_id, plan.release_seal,
                                plan.fleets, effects={})
    for change in plan.changes:
        target, after = Path(change.target), change.after
        if after["kind"] == "file":
            builder.file(target, plan.content(change), mode=after["mode"])
        elif after["kind"] == "symlink":
            builder.symlink(target, Path(after["target"]))
        elif after["kind"] == "tree":
            builder.tree(target, {name: (plan.blob(entry["sha256"]), entry["mode"])
                                  for name, entry in after["files"].items()})
        elif after["kind"] == "absent":
            builder.remove(target)
        else:
            builder.directory(target)  # records nothing once the directory exists
    return builder.seal()


def test_unchanged_outputs_keep_their_identity_and_need_no_backup(proposal, monkeypatch):
    plan, runtime, external, _, _ = proposal
    install.prepare_config(plan, "first")
    install.apply_config(plan.data_root, "first")
    repeated = _re_record(plan)
    identities = {Path(change.target): Path(change.target).lstat().st_ino
                  for change in repeated.changes if Path(change.target).exists()}
    prepared = install.prepare_config(repeated, "unchanged")
    assert list((prepared.directory / "blobs").iterdir()) == []

    def unexpected_replace(*args):
        raise AssertionError("unchanged configuration must not be replaced")

    monkeypatch.setattr(install, "_replace", unexpected_replace)
    assert install.apply_config(plan.data_root, "unchanged").status == "applied"
    assert install.rollback_config(plan.data_root, "unchanged").status == "rolled_back"
    assert {path: path.lstat().st_ino for path in identities} == identities


def test_legacy_journal_is_readable_without_rewriting_or_resuming_it(proposal):
    plan, runtime, external, _, _ = proposal
    prepared = install.prepare_config(plan, "legacy")
    path = prepared.directory / "journal.json"
    journal = json.loads(path.read_bytes())["journal"]
    journal.update(schema=1, plan=plan.payload())
    for blob in (plan.directory / "files").iterdir():
        _file(prepared.directory / "blobs" / blob.name, blob.read_bytes(), 0o600)
    journal["blobs"] = sorted(p.name for p in (prepared.directory / "blobs").iterdir())
    path.write_bytes(install._json({"journal": journal, "sha256": install._digest(install._json(journal))}))
    original = path.read_bytes()
    before = _tree(runtime), _tree(external)
    assert install.read_config_install(plan.data_root, "legacy").status == "prepared"
    for operation in (install.apply_config, install.rollback_config):
        with pytest.raises(install.ConfigInstallError, match="legacy configuration journal is read-only"):
            operation(plan.data_root, "legacy")
        assert path.read_bytes() == original
        assert (_tree(runtime), _tree(external)) == before


def test_change_effect_reads_what_apply_wrote_as_unchanged_and_names_what_differs(proposal):
    """#2449: a review names each path's effect from the plan alone. A path is
    unchanged exactly where its frozen state is already the node apply would
    produce, the comparison apply itself holds an applied row to."""
    plan, runtime, external, _, settings = proposal

    def effects(planned):
        return {Path(change.target): install.change_effect(change) for change in planned.changes}

    assert set(effects(plan).values()) == {"content"}
    install.prepare_config(plan, "applied")
    install.apply_config(plan.data_root, "applied")
    again = effects(_re_record(plan))
    assert again == dict.fromkeys(again, "unchanged")
    assert set(again) == {Path(change.target) for change in plan.changes} - {runtime / "bot/data"}

    access = runtime / "new/channel/access.json"
    for path, key, changed in ((external / "new/run.sh", external, 0o700),
                               (external / "new", external, 0o700),  # a directory's bits too
                               (settings, settings, 0o600), (access, access, 0o644)):
        restored = path.stat().st_mode & 0o777
        path.chmod(changed)
        assert effects(_re_record(plan))[key] == "mode", path
        path.chmod(restored)
    stale = _file(external / "new/stale.md", b"left by hand\n")
    assert effects(_re_record(plan))[external] == "content"  # apply removes it
    stale.unlink()
    (external / "empty").mkdir()
    assert effects(_re_record(plan))[external] == "content"
    (external / "empty").rmdir()
    settings.write_bytes(b"edited by hand\n")
    link = runtime / "cli"
    link.unlink()
    link.symlink_to("previous-cli")
    _file(runtime / "retired.sh", b"restored by hand\n")
    again = effects(_re_record(plan))
    assert [again[path] for path in (settings, link, runtime / "retired.sh")] == ["content"] * 3
