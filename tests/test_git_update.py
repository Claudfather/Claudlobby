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


def _tiny_wheel(directory, version):
    """A real, dependency-free wheel, built locally without a build backend."""
    import base64
    import csv
    import hashlib
    import io
    import zipfile
    stem = f"example-{version}.dist-info"
    files = {"example.py": f"VERSION = {version!r}\n".encode(),
             f"{stem}/METADATA": f"Metadata-Version: 2.1\nName: example\nVersion: {version}\n".encode(),
             f"{stem}/WHEEL": b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    rows = [[name, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("="),
             str(len(content))] for name, content in files.items()]
    rows.append([f"{stem}/RECORD", "", ""])
    record = io.StringIO()
    csv.writer(record).writerows(rows)
    files[f"{stem}/RECORD"] = record.getvalue().encode()
    wheel = directory / f"example-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return f"example=={version} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n".encode()


def test_unchanged_dependencies_perform_no_install_or_python_observation(update, monkeypatch):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        prepare(store, target)
        monkeypatch.setattr(store, "_dependency_run", lambda _: pytest.fail("unchanged lock ran Python/pip"))
        assert store.prepare_dependencies(interpreter=Path("/unavailable"))["dependencies"]["prepared"]
        quiet(store)
        store.cutover_git()
        record = store.load()
        record["phase"] = "environment"
        store.save(record)
        store.cutover_environment()
        assert store.load()["dependencies"]["environment_complete"]


def test_dependency_wheels_preflight_and_offline_final_path_interruption(update, tmp_path, monkeypatch):
    installation, _, _, commit = update
    wheels = tmp_path / "fixture-wheels"
    wheels.mkdir()
    old_lock = _tiny_wheel(wheels, "1.0")
    new_lock = _tiny_wheel(wheels, "2.0")
    (installation.source / "requirements-runtime.lock").write_bytes(old_lock)
    before = commit("Real wheel baseline")
    (installation.source / "requirements-runtime.lock").write_bytes(new_lock)
    target = commit("Real wheel target")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], tmp_path)
    with UpdateStore(installation) as store:
        prepare(store, target)
        run = store._dependency_run
        def local_download(arguments):
            if "download" in arguments:
                arguments = [*arguments, "--no-index", "--find-links", wheels]
            return run(arguments)
        monkeypatch.setattr(store, "_dependency_run", local_download)
        prepared = store.prepare_dependencies(interpreter=Path(sys.executable).resolve())
        assert prepared["maintenance"] is False
        assert installation.preflight() == before
        assert not installation.environment.exists()
        foreign = deepcopy(prepared)
        foreign["dependencies"]["proof"]["binding"]["request_id"] = "00000000-0000-4000-8000-000000000099"
        store.save(foreign)
        with pytest.raises(UpdateError, match="proof"):
            store.verify_dependencies()
        store.save(prepared)
        quiet(store)
        store.cutover_git()
        record = store.load()
        record["phase"] = "environment"
        store.save(record)
        install = store._install_dependencies
        def interrupted(python, name):
            # Venv exists at its final path when the process is interrupted.
            assert python == installation.environment / "bin/python"
            raise OSError("interrupted environment install")
        monkeypatch.setattr(store, "_install_dependencies", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            store.cutover_environment()
        assert (installation.environment / "bin/python").exists()
        monkeypatch.setattr(store, "_install_dependencies", install)
    with UpdateStore(installation) as resumed:
        resumed.cutover_environment()  # No network, application imports, or temporary-env rename.
        assert resumed.load()["dependencies"]["environment_complete"] is True
        output = _run([installation.environment / "bin/python", "-I", "-c",
                       "import example; print(example.VERSION)"], tmp_path)
        assert "2.0" in output
        cached = next((resumed.work / "wheels-target").glob("*.whl"))
        cached.write_bytes(b"changed immutable wheel")
        with pytest.raises(UpdateError, match="immutable"):
            resumed.verify_dependencies()


def test_target_and_dependencies_prepare_before_immutable_render_scope(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        staged = store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001")
        assert staged["config_fingerprint"] is None and staged["scope"] == {}
        assert not staged["finalized"]
        with pytest.raises(UpdateError):
            changed = deepcopy(staged)
            changed["phase"] = "quiesce"
            store.save(changed)
        frozen = store.finalize_prepare(config_fingerprint=SHA, scope={"example.service": SHA})
        assert frozen["finalized"]
        with pytest.raises(UpdateError, match="frozen"):
            store.finalize_prepare(config_fingerprint="d" * 64, scope={})
        changed = deepcopy(frozen)
        changed["finalized"] = False
        changed["config_fingerprint"] = None
        changed["scope"] = {}
        with pytest.raises(UpdateError, match="frozen"):
            store.save(changed)


def test_completed_health_errors_keep_unknown_effect_evidence(update):
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="done", health_errors={"example.service": "unknown native start"})
        store.save(record)
        with pytest.raises(UpdateError, match="evidence"):
            store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000002")
        assert store.load()["request_id"] == record["request_id"]


def test_application_preparation_finalizes_the_original_staged_target(update, monkeypatch):
    from types import SimpleNamespace
    from claudlobby import update_operation
    installation, _, target, _ = update
    current = {"identities": {}, "fingerprint": "c" * 64, "units": []}
    rendered = SimpleNamespace(revision=target, fingerprint=SHA, check_inputs=lambda: None)
    monkeypatch.setattr(update_operation, "read_current_config", lambda *_a, **_k: current)
    monkeypatch.setattr(update_operation, "read_registry_identity_bindings", lambda *_a, **_k: {})
    monkeypatch.setattr(update_operation, "snapshot_from_plan", lambda *_a, **_k: current)
    monkeypatch.setattr(update_operation, "validate_candidate_consumers", lambda *_: None)
    monkeypatch.setattr(update_operation, "_schema", lambda *_: 14)
    monkeypatch.setattr(update_operation, "freeze_service_intent", lambda *_: [])
    monkeypatch.setattr(update_operation, "prepare_publication", lambda *_: None)
    with UpdateStore(installation) as store:
        staged = store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001")
        result = update_operation.prepare_operation(
            store, rendered, SimpleNamespace(package=None), installation.data_root,
            Path(sys.executable), staged["request_id"])
        assert result["request_id"] == staged["request_id"]
        assert result["finalized"] and result["phase"] == "quiesce"
        assert result["config_fingerprint"] == SHA


def test_inherited_update_lock_validates_parent_and_remains_owned(update):
    import os
    installation, _, target, _ = update
    module = Path(__file__).resolve().parents[1] / "claudlobby/git_update.py"
    script = """import importlib.util,pathlib,sys,types
spec=importlib.util.spec_from_file_location('independent_update',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
installation=types.SimpleNamespace(directory=pathlib.Path(sys.argv[2]),data_root=pathlib.Path(sys.argv[3]))
store=module.UpdateStore.from_parent(installation,fd=int(sys.argv[4]),parent_pid=int(sys.argv[5]),request_id=sys.argv[6])
store.__exit__()
"""
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        arguments = [sys.executable, "-I", "-B", "-c", script, str(module), str(installation.directory),
                     str(installation.data_root), str(store._fd), str(os.getpid()), record["request_id"]]
        outcome = subprocess.run(arguments, pass_fds=(store._fd,), capture_output=True, text=True, timeout=10)
        assert outcome.returncode == 0, outcome.stderr
        with pytest.raises(UpdateError, match="running"):
            with UpdateStore(installation):
                pass
        arguments[-2] = str(os.getpid() + 999999)
        outcome = subprocess.run(arguments, pass_fds=(store._fd,), capture_output=True, text=True, timeout=10)
        assert outcome.returncode != 0 and "parent" in outcome.stderr


def _runner_source(installation, commit):
    root = Path(__file__).resolve().parents[1]
    package = installation.source / "claudlobby"
    for name in ("git_update.py", "supervision_runtime.py"):
        shutil.copy2(root / "claudlobby" / name, package / name)
    (package / "_runtime_scripts/supervisor.sh").write_text(
        'svc_activation_quiet() { printf "inactive\\tcgroup-empty\\n"; }\n')
    entry = '''import json,pathlib,sys
from .git_update import UpdateStore,_verify_inherited_lock
from .installation import read_installation
VERSION = "OLD"
def main(args):
    directory,stage,fd,parent,request,*runtime=args
    installation=read_installation(pathlib.Path(directory))
    store=UpdateStore.from_parent(installation,fd=int(fd),parent_pid=int(parent),request_id=request)
    record=store.load()
    record.setdefault("trace",[]).append([stage,VERSION])
    if stage in {"quiet","publish"}:
        _verify_inherited_lock(store.state/"runtime-use.lock",int(runtime[0]))
    if stage=="quiesce":
        record["consumers"]["example.service"]["stop"]="done"
    elif stage=="quiet":
        record["quiet"]={key:{"identity":value,"evidence_sha256":"c"*64} for key,value in record["scope"].items()}
    elif stage=="publish":
        record.update(phase="restore",maintenance=False,config_committed=True)
    elif stage=="restore":
        record.update(phase="done")
    store.save(record)
    store.__exit__()
    return 0
'''
    (package / "update_entry.py").write_text(entry)
    before = commit("Independent runner fixture current")
    (package / "update_entry.py").write_text(entry.replace('VERSION = "OLD"', 'VERSION = "TARGET"'))
    target = commit("Independent runner fixture target")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], installation.data_root)
    python = installation.environment / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    return before, target


def test_runner_uses_old_then_fresh_target_process_under_shared_parent_lock(update):
    from claudlobby.git_update import run_prepared_update
    installation, _, _, commit = update
    before, target = _runner_source(installation, commit)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        record["consumers"] = {"example.service": {"intent": {
            "installed": [{"path": str(installation.data_root / "example.service")}], "bot_binding": None}}}
        store.save(record)
        (store.state / "runtime-use.lock").touch()
        result = run_prepared_update(store)
        assert result["phase"] == "done" and result["config_committed"]
        assert installation.preflight() == target
        assert result["trace"] == [["quiesce", "OLD"], ["quiet", "OLD"],
                                    ["publish", "TARGET"], ["restore", "TARGET"]]


def test_runner_reobserves_quiet_after_git_effect_before_environment_resume(update):
    from claudlobby.git_update import run_prepared_update, _prepare_quiet_support
    installation, _, _, commit = update
    _, target = _runner_source(installation, commit)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        record["consumers"] = {"example.service": {"intent": {
            "installed": [{"path": str(installation.data_root / "example.service")}], "bot_binding": None}}}
        store.save(record)
        (store.state / "runtime-use.lock").touch()
        _prepare_quiet_support(store)
        quiet(store)
        store.cutover_git()  # Interrupted with record still at git, actual target.
        support = store.work / ("quiet-" + record["request_id"]) / "supervisor.sh"
        support.write_text('svc_activation_quiet() { return 3; }\n')
        with pytest.raises(UpdateError, match="quiet implementation changed"):
            run_prepared_update(store)
        assert store.load()["phase"] == "git"


def test_runner_waits_for_previously_admitted_start_before_quiescing(update):
    import fcntl
    from claudlobby.git_update import run_prepared_update
    installation, _, _, commit = update
    _, target = _runner_source(installation, commit)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        store.save(record)
        with (store.state / "runtime-use.lock").open("w") as reader:
            fcntl.flock(reader.fileno(), fcntl.LOCK_SH)
            with pytest.raises(UpdateError, match="still in use"):
                run_prepared_update(store)
            assert "trace" not in store.load()
            assert "quiet_support" not in store.load()


def test_completed_candidate_cleanup_preserves_dirty_work_then_removes_owned_checkout(update):
    from claudlobby.git_update import cleanup_candidate
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        source = store.work / ("candidate-" + record["request_id"])
        _run(["git", "-C", installation.source, "worktree", "add", "--detach", source, target], store.root)
        record.update(phase="done", candidate_source=str(source))
        store.save(record)
        foreign = source / "foreign.txt"
        foreign.write_text("retain")
        with pytest.raises(UpdateError, match="changed"):
            cleanup_candidate(store)
        assert foreign.read_text() == "retain"
        foreign.unlink()
        assert cleanup_candidate(store)["candidate_cleaned"]
        assert not source.exists()
        listing = _run(["git", "-C", installation.source, "worktree", "list", "--porcelain"], store.root)
        assert str(source) not in listing
