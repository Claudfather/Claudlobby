"""Current-update publication recovery and read-only historical evidence."""

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from claudlobby import config_install as install
from claudlobby.config_plan import ConfigPlanBuilder, PlanError, read_rendered
from claudlobby.git_update import UpdateStore
from tests.test_config_plan import historical_plan
from tests.test_git_update import update  # noqa: F401 — real managed checkout and dependency lock


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


@contextmanager
def _publication_store(plan, installation):
    """Use the canonical process lock and original request on every resume."""
    with UpdateStore(installation) as store:
        current = store.load()
        if current is None:
            current = store.prepare(plan.revision, config_fingerprint=plan.fingerprint,
                                    scope={}, request_id=str(uuid4()))
            current.update(phase="publish", maintenance=True)
            store.save(current)
        yield store


@pytest.fixture
def proposal(update, tmp_path):  # noqa: F811
    installation, revision, _, _ = update
    root = installation.data_root
    manifest = _file(root / "fleet.yaml", b"fleet: reviewed\n")
    runtime = root / "runtime"
    settings = _file(runtime / "settings.json", b"old settings\n", 0o600)
    retired = _file(runtime / "retired.sh", b"old command\n", 0o755)
    link = runtime / "cli"
    link.symlink_to("previous-cli")
    external = tmp_path / "vault/runtime/skills"
    _file(external / "old/SKILL.md", b"old skill\n", 0o640)
    (external / "old/link").symlink_to("SKILL.md")
    (external / "old").chmod(0o550)
    builder = ConfigPlanBuilder(root, ("fleet",), effects={"reload_supervision": True})
    builder.input(manifest)
    builder.file(settings, b"candidate settings\n")
    builder.symlink(link, installation.cli)
    builder.remove(retired)
    builder.directory(runtime / "bot/data")
    builder.file(runtime / "new/channel/access.json", b'{"candidate":true}\n', mode=0o600)
    builder.tree(external, {"new/SKILL.md": (b"frozen candidate skill\n", 0o644),
                            "new/run.sh": (b"#!/bin/sh\nexit 0\n", 0o755)})
    plan = builder.freeze(revision)
    try:
        yield plan, runtime, external, manifest, settings, installation
    finally:
        for directory in external.parent.rglob("*"):
            if not directory.is_symlink() and directory.is_dir():
                directory.chmod(directory.stat().st_mode | 0o700)


def test_current_update_publication_resumes_without_a_second_journal(proposal, monkeypatch):
    plan, runtime, external, _, settings, installation = proposal
    with _publication_store(plan, installation) as store:
        assert install.prepare_publication(plan, store) == "prepared"
        directory = store.work / "publication"
        assert directory.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in (directory / "blobs").iterdir())
        journal = store.load()["publication"]
        assert journal["schema"] == 2 and "plan" not in journal
        assert not (set(journal["blobs"]) & {p.name for p in (plan.directory / "files").iterdir()})
        record = store.load()
        record.update(phase="quiesce", maintenance=False)
        store.save(record)
        with pytest.raises(install.ConfigInstallError, match="maintenance phase"):
            install.apply_publication(plan, store)
        record.update(phase="publish", maintenance=True)
        store.save(record)
        original_replace = install._replace
        def interrupted(source, target):
            original_replace(source, target)
            if target == settings:
                raise InterruptedError("after rename")
        with monkeypatch.context() as patch:
            patch.setattr(install, "_replace", interrupted)
            with pytest.raises(InterruptedError, match="after rename"):
                install.apply_publication(plan, store)
        request = store.load()["request_id"]
    assert settings.read_bytes() == b"candidate settings\n"
    mutable = _file(runtime / "bot/data/user.txt", b"keep user data")
    fresh = read_rendered(plan.data_root, plan.fingerprint)
    with _publication_store(fresh, installation) as store:
        assert store.load()["request_id"] == request
        assert install.apply_publication(fresh, store) == "applied"
        assert install.apply_publication(fresh, store) == "applied"
        assert store.load()["publication"]["status"] == "applied"
        record = store.load()
        row = next(item for item in record["publication"]["rows"] if item["target"] == str(settings))
        row["after"]["mode"] = 0o700
        store.save(record)
        with pytest.raises(install.ConfigInstallError, match="bytes changed"):
            install.apply_publication(fresh, store)
    assert mutable.read_bytes() == b"keep user data"
    assert not list((plan.data_root / "state/update-work").rglob("journal.json"))
    assert not (plan.data_root / "state/activations").exists()


def test_interrupted_tree_resume_uses_frozen_bytes_and_preserves_mutable_children(proposal, monkeypatch):
    plan, runtime, external, _, _, installation = proposal
    original_replace = install._replace
    def stop_after_old_tree_moved(source, target):
        original_replace(source, target)
        if source == external and target.name == "previous":
            assert target.parent.parent == external.parent
            raise InterruptedError("between directory renames")
    with _publication_store(plan, installation) as store:
        install.prepare_publication(plan, store)
        with monkeypatch.context() as patch:
            patch.setattr(install, "_replace", stop_after_old_tree_moved)
            with pytest.raises(InterruptedError, match="between directory renames"):
                install.apply_publication(plan, store)
    assert not external.exists()
    staged = {blob: blob.read_bytes() for blob in (plan.directory / "files").iterdir()}
    for blob in staged:
        blob.unlink()
    interrupted = _tree(runtime), _tree(external.parent)
    with _publication_store(plan, installation) as store:
        with pytest.raises(PlanError, match="missing staged content"):
            install.apply_publication(plan, store)
    assert (_tree(runtime), _tree(external.parent)) == interrupted
    for blob, content in staged.items():
        _file(blob, content, 0o600)
    remove_tree = install.shutil.rmtree
    def stop_during_private_cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == external.parent and path.name.startswith(".claudlobby-config-"):
            next(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()).unlink()
            raise InterruptedError("during owned workspace cleanup")
        return remove_tree(path, *args, **kwargs)
    with _publication_store(plan, installation) as store:
        with monkeypatch.context() as patch:
            patch.setattr(install.shutil, "rmtree", stop_during_private_cleanup)
            with pytest.raises(InterruptedError, match="owned workspace cleanup"):
                install.apply_publication(plan, store)
    with _publication_store(plan, installation) as store:
        assert install.apply_publication(plan, store) == "applied"
        durable = _file(runtime / "bot/data/new-state.json", b"durable work after publication\n")
        assert install.apply_publication(plan, store) == "applied"
        assert durable.read_bytes() == b"durable work after publication\n"
    assert (external / "new/SKILL.md").read_bytes() == b"frozen candidate skill\n"
    assert (external / "new/run.sh").stat().st_mode & 0o777 == 0o755
    assert not (external / "old").exists()
    assert not list(external.parent.glob(".claudlobby-config-*"))


def test_changed_foreign_target_and_backup_tamper_refuse_before_writes(proposal):
    plan, runtime, external, _, settings, installation = proposal
    with _publication_store(plan, installation) as store:
        install.prepare_publication(plan, store)
        install.apply_publication(plan, store)
        settings.write_bytes(b"operator changed configuration\n")
        before = _tree(runtime), _tree(external)
        with pytest.raises(install.ConfigInstallError, match="foreign configuration change"):
            install.apply_publication(plan, store)
        assert (_tree(runtime), _tree(external)) == before
        settings.write_bytes(b"candidate settings\n")
        backup = next(p for p in (store.work / "publication/blobs").iterdir()
                      if p.read_bytes() == b"old settings\n")
        backup.write_bytes(b"corrupted backup\n")
        before = _tree(runtime), _tree(external)
        with pytest.raises(install.ConfigInstallError, match="changed configuration backup"):
            install.apply_publication(plan, store)
        assert (_tree(runtime), _tree(external)) == before


def test_corrupt_retained_input_cannot_replace_a_before_image(proposal):
    original, runtime, external, _, settings, installation = proposal
    builder = ConfigPlanBuilder(original.data_root, original.fleets, effects={})
    digest = builder.input_content(settings)
    builder.file(settings, b"replacement settings\n")
    builder.file(runtime / "earlier.json", b"another proposed output\n")
    shutil.rmtree(original.directory)  # This test's first proposal was never prepared.
    plan = builder.freeze(original.revision)
    (plan.directory / "files" / digest).write_bytes(b"corrupt retained input\n")
    before = _tree(runtime), _tree(external)
    with _publication_store(plan, installation) as store:
        with pytest.raises(PlanError, match="changed staged content"):
            install.prepare_publication(plan, store)
        assert "publication" not in store.load()
    assert (_tree(runtime), _tree(external)) == before


def test_stale_render_or_wrong_current_operation_never_prepares_or_overwrites(proposal):
    plan, runtime, external, manifest, _, installation = proposal
    manifest.write_bytes(b"changed fleet\n")
    before = _tree(runtime), _tree(external)
    with _publication_store(plan, installation) as store:
        with pytest.raises(PlanError, match="input changed"):
            install.prepare_publication(plan, store)
        assert "publication" not in store.load()
        assert (_tree(runtime), _tree(external)) == before
        manifest.write_bytes(b"fleet: reviewed\n")
        install.prepare_publication(plan, store)
        record = store.load()
        original = json.loads(json.dumps(record))
        wrong = replace(plan, effects={"different": True})
        with pytest.raises(install.ConfigInstallError, match="differs from the current update"):
            install.prepare_publication(wrong, store)
        assert store.load() == original
        record["publication"]["fingerprint"] = "f" * 64
        store.save(record)
        with pytest.raises(install.ConfigInstallError, match="identity changed"):
            install.apply_publication(plan, store)
        assert (_tree(runtime), _tree(external)) == before


def test_leaf_symlink_replacement_and_redirected_parent_refusal(proposal):
    plan, runtime, _, _, _, installation = proposal
    link = runtime / "cli"
    with _publication_store(plan, installation) as store:
        install.prepare_publication(plan, store)
        install.apply_publication(plan, store)
    assert link.readlink() == installation.cli
    redirected = runtime / "redirected"
    redirected.symlink_to(plan.data_root / "state", target_is_directory=True)
    unsafe = ConfigPlanBuilder(plan.data_root, effects={})
    unsafe.file(redirected / "unexpected", b"must never reach state")
    with pytest.raises(PlanError, match="persistent state"):
        unsafe.freeze(plan.revision)


def _re_record(plan):
    """Review the same desired bytes against current generated destinations."""
    builder = ConfigPlanBuilder(plan.data_root, plan.fleets, effects={})
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
            builder.directory(target)
    shutil.rmtree(plan.directory)  # Prior publication completed; retire this fixture's owned render.
    return builder.freeze(plan.revision)


def test_unchanged_outputs_keep_their_identity_and_need_no_backup(proposal, monkeypatch):
    plan, _, _, _, _, installation = proposal
    with _publication_store(plan, installation) as store:
        install.prepare_publication(plan, store)
        install.apply_publication(plan, store)
        record = store.load()
        record.update(phase="done", maintenance=False)
        store.save(record)
    repeated = _re_record(plan)
    identities = {Path(change.target): Path(change.target).lstat().st_ino
                  for change in repeated.changes if Path(change.target).exists()}
    with UpdateStore(installation) as store:
        record = store.prepare(repeated.revision, config_fingerprint=repeated.fingerprint,
                               scope={}, request_id=str(uuid4()))
        record.update(phase="publish", maintenance=True)
        store.save(record)
        assert install.prepare_publication(repeated, store) == "prepared"
        assert store.load()["publication"]["blobs"] == []
        def unexpected_replace(*args):
            raise AssertionError("unchanged configuration must not be replaced")
        monkeypatch.setattr(install, "_replace", unexpected_replace)
        assert install.apply_publication(repeated, store) == "applied"
    assert {path: path.lstat().st_ino for path in identities} == identities


def historical_config_install(plan, activation_id, *, schema=2, status="prepared"):
    """Author archived journal evidence directly, without a deployment writer."""
    directory = plan.data_root / "state/activations" / activation_id / "config"
    (directory / "blobs").mkdir(parents=True)
    journal = {"schema": schema, "directory": str(directory), "plan_id": plan.plan_id,
               "status": status, "rows": [], "blobs": []}
    if schema == 1:
        journal["plan"] = plan.payload()
    path = directory / "journal.json"
    path.write_bytes(install._json({"journal": journal, "sha256": install._digest(install._json(journal))}))
    return path


@pytest.mark.parametrize("schema", [1, 2])
def test_historical_journal_is_readable_without_rewriting_or_resuming_it(proposal, schema):
    plan, runtime, external, _, _, _ = proposal
    builder = ConfigPlanBuilder(plan.data_root, plan.fleets, effects={})
    archived = historical_plan(builder)
    path = historical_config_install(archived, "legacy", schema=schema)
    original = path.read_bytes()
    before = _tree(runtime), _tree(external)
    assert install.read_config_install(plan.data_root, "legacy").status == "prepared"
    assert path.read_bytes() == original and (_tree(runtime), _tree(external)) == before
    # Archived backups remain digest-checked, without repairing their bytes.
    backup_bytes = b'archived before image'
    digest = install._digest(backup_bytes)
    backup = path.parent / 'blobs' / digest
    backup.write_bytes(backup_bytes)
    envelope = json.loads(original)
    envelope['journal']['blobs'] = [digest]
    envelope['sha256'] = install._digest(install._json(envelope['journal']))
    path.write_bytes(install._json(envelope))
    assert install.read_config_install(plan.data_root, 'legacy').status == 'prepared'
    backup.write_bytes(b'corrupt archived backup')
    with pytest.raises(install.ConfigInstallError, match='changed configuration backup'):
        install.read_config_install(plan.data_root, 'legacy')
    assert backup.read_bytes() == b'corrupt archived backup'
    backup.write_bytes(backup_bytes)
    original = path.read_bytes()
    assert all(not hasattr(install, name) for name in ("prepare_config", "apply_config", "rollback_config"))
    journal = json.loads(original)["journal"]
    with pytest.raises(install.ConfigInstallError, match="read-only"):
        install._save(path.parent, journal)
    assert path.read_bytes() == original
    envelope = json.loads(original)
    envelope["journal"]["status"] = "tampered"
    path.write_text(json.dumps(envelope))
    corrupted = path.read_bytes()
    with pytest.raises(install.ConfigInstallError, match="identity changed"):
        install.read_config_install(plan.data_root, "legacy")
    assert path.read_bytes() == corrupted


def test_change_effect_reads_what_apply_wrote_as_unchanged_and_names_what_differs(proposal):
    plan, runtime, external, _, settings, installation = proposal
    def effects(planned):
        return {Path(change.target): install.change_effect(change) for change in planned.changes}
    assert set(effects(plan).values()) == {"content"}
    with _publication_store(plan, installation) as store:
        install.prepare_publication(plan, store)
        install.apply_publication(plan, store)
    again = effects(_re_record(plan))
    assert again == dict.fromkeys(again, "unchanged")
    assert set(again) == {Path(change.target) for change in plan.changes} - {runtime / "bot/data"}
    access = runtime / "new/channel/access.json"
    for path, key, changed in ((external / "new/run.sh", external, 0o700),
                               (external / "new", external, 0o700),
                               (settings, settings, 0o600), (access, access, 0o644)):
        restored = path.stat().st_mode & 0o777
        path.chmod(changed)
        assert effects(_re_record(plan))[key] == "mode", path
        path.chmod(restored)
    stale = _file(external / "new/stale.md", b"left by hand\n")
    assert effects(_re_record(plan))[external] == "content"
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
