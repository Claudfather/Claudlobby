"""Mechanical update recovery against real disposable Git repositories."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from claudlobby.installation import install_launcher, install_updater
from claudlobby.git_update import UpdateError, UpdateStore, read_update
from tests.conftest import constructed_env
from tests.test_package_resources import _run, _source_installation


LOCK = b"example==1.0 --hash=sha256:" + b"a" * 64 + b"\n"
SHA = "b" * 64


@pytest.fixture
def update(tmp_path):
    installation = _source_installation(tmp_path)
    (installation.source / "requirements-runtime.lock").write_bytes(LOCK)
    def commit(message):
        _run(["git", "-C", installation.source, "add", "."], tmp_path)
        _run(["git", "-C", installation.source, "-c", "user.name=Example", "-c",
              "user.email=example@example.invalid", "commit", "--quiet", "-m", message], tmp_path)
        return installation.preflight()
    before = commit("Current dependencies")
    (installation.source / "templates/candidate.txt").write_text("target bytes\n")
    target = commit("Target source")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], tmp_path)
    return installation, before, target, commit


def prepare(store, target):
    return store.prepare(target, config_fingerprint=SHA, scope={"example.service": SHA},
                         request_id="00000000-0000-4000-8000-000000000001")


def quiet(store):
    record = store.load()
    record["phase"] = "quiesce"
    record["quiet"] = {"example.service": {"identity": SHA, "evidence_sha256": "c" * 64}}
    store.save(record)
    store.begin_cutover()


def test_update_freezes_target_and_resumes_after_git_effect_before_receipt(update, monkeypatch):
    installation, before, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        assert record["before"] == before and record["target"] == target
        assert record["dependencies"]["prepared"] is True
        assert record["maintenance"] is False
        quiet(store)
        save = store.save
        monkeypatch.setattr(store, "save", lambda _record: (_ for _ in ()).throw(OSError("interrupted save")))
        with pytest.raises(OSError, match="interrupted"):
            store.cutover_git()
        assert installation.preflight() == target
        monkeypatch.setattr(store, "save", save)
    with UpdateStore(installation) as resumed:
        assert prepare(resumed, target)["target"] == target
        resumed.cutover_git()
        assert resumed.load()["git_complete"] is True
        assert resumed.load()["maintenance"] is True


def test_update_rejects_dirty_wrong_and_non_fast_forward_source(update):
    installation, before, target, commit = update
    (installation.source / "untracked.txt").write_text("keep local work\n")
    with UpdateStore(installation) as store:
        with pytest.raises(UpdateError, match="dirty"):
            prepare(store, target)
    assert not (installation.data_root / "state/update.json").exists()
    (installation.source / "untracked.txt").unlink()
    (installation.source / "different.txt").write_text("divergent source\n")
    other = commit("Other branch")
    with UpdateStore(installation) as store:
        with pytest.raises(UpdateError, match="fast-forward"):
            prepare(store, target)
    assert installation.preflight() == other


def test_update_cutover_requires_exact_quiet_scope_and_unchanged_preview(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        store.save(record)
        with pytest.raises(UpdateError, match="quiet"):
            store.begin_cutover()
        changed = deepcopy(record)
        changed["config_fingerprint"] = "d" * 64
        with pytest.raises(UpdateError, match="frozen"):
            store.save(changed)
        changed = deepcopy(record)
        changed["quiet"] = {"example.service": {"identity": "d" * 64, "evidence_sha256": "c" * 64}}
        store.save(changed)
        with pytest.raises(UpdateError, match="quiet"):
            store.begin_cutover()
        quiet(store)
        (installation.source / "templates/candidate.txt").write_text("foreign edit\n")
        with pytest.raises(UpdateError, match="dirty"):
            store.cutover_git()
        assert (installation.source / "templates/candidate.txt").read_text() == "foreign edit\n"


def test_changed_dependencies_are_not_claimed_prepared_or_installed(update):
    installation, before, _, commit = update
    (installation.source / "requirements-runtime.lock").write_bytes(LOCK.replace(b"1.0", b"2.0"))
    target = commit("Changed dependencies")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], installation.data_root)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        assert record["dependencies"]["prepared"] is False
        with pytest.raises(UpdateError, match="dependencies"):
            quiet(store)
        assert installation.preflight() == before
        assert "example==" not in (installation.data_root / "state/update.json").read_text()


def test_update_lock_and_changed_frozen_files_refuse(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        prepare(store, target)
        with pytest.raises(UpdateError, match="running"):
            with UpdateStore(installation):
                pass
        quiet(store)
        (store.work / "dependencies-target.lock").write_bytes(b"changed frozen input")
        with pytest.raises(UpdateError, match="dependency"):
            store.cutover_git()


def test_update_never_changes_git_without_lock_or_at_unexpected_head(update):
    installation, _, target, commit = update
    with pytest.raises(UpdateError, match="lock"):
        UpdateStore(installation).cutover_git()
    with UpdateStore(installation) as store:
        prepare(store, target)
        quiet(store)
        (installation.source / "unexpected.txt").write_text("foreign committed work\n")
        other = commit("Unexpected HEAD")
        with pytest.raises(UpdateError, match="actual Git HEAD"):
            store.cutover_git()
        assert installation.preflight() == other


def test_completed_update_does_not_become_a_historical_global_gate(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="done", maintenance=False)
        store.save(record)
        next_record = store.prepare(target, config_fingerprint=SHA, scope={"example.service": SHA},
                                    request_id="00000000-0000-4000-8000-000000000002")
        assert next_record["phase"] == "prepare" and not next_record["quiet"]
        assert next_record["request_id"] != record["request_id"]


def test_standalone_update_status_needs_neither_source_nor_runtime(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        prepare(store, target)
    interpreter = Path(sys.executable).resolve()
    install_launcher(installation, interpreter=interpreter)
    install_updater(installation, interpreter=interpreter)
    shutil.rmtree(installation.source)
    outcome = subprocess.run([str(installation.cli), "--json", "host", "update", "status"],
                             env=constructed_env(), cwd=installation.data_root,
                             capture_output=True, text=True, timeout=10)
    assert outcome.returncode == 0, outcome.stderr
    assert json.loads(outcome.stdout)["update"]["target"] == target
    assert read_update(installation)["phase"] == "prepare"
    unsupported = subprocess.run([str(installation.cli), "host", "update"], env=constructed_env(),
                                 cwd=installation.data_root, capture_output=True, text=True, timeout=10)
    assert unsupported.returncode != 0 and "application update owner" in unsupported.stderr
