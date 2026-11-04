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


def prepare(store, target, *, consumers=None, initialization=None):
    from claudlobby.git_update import _digest
    request = "00000000-0000-4000-8000-000000000001"
    if initialization is not None:
        store.prepare_target(target, request_id=request, initialization_scope=initialization)
    scope = {"example.service": SHA}
    if consumers is not None:
        consumers = deepcopy(consumers)
        for key, item in consumers.items():
            item["intent"]["target"] = key
        scope = {key: _digest(json.dumps(item["intent"], sort_keys=True, separators=(",", ":")).encode())
                 for key, item in consumers.items()}
    record = store.prepare(target, config_fingerprint=SHA, scope=scope, request_id=request)
    if consumers is not None:
        record["consumers"] = consumers
        store.save(record)
    return record


def quiet(store):
    record = store.load()
    record["phase"] = "quiesce"
    record["quiet"] = {key: {"identity": identity, "evidence_sha256": "c" * 64}
                       for key, identity in record["scope"].items()}
    store.save(record)
    store.begin_cutover()


def test_application_stage_recovery_clears_only_the_matching_successful_error(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        original = prepare(store, target)
        outcomes = iter([
            subprocess.CompletedProcess([], 6, "", "render refused"),
            subprocess.TimeoutExpired("render", 900),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ])
        def stage(*_args, **_kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        monkeypatch.setattr(updater.subprocess, "run", stage)
        interpreter = Path(sys.executable).resolve()
        with pytest.raises(UpdateError, match="render refused"):
            updater._application_stage(store, "render", python=interpreter)
        error = {"stage": "render", "detail": "render refused"}
        assert store.load()["stage_error"] == error
        with pytest.raises(UpdateError, match="render interrupted"):
            updater._application_stage(store, "render", python=interpreter)
        assert store.load()["stage_error"] == error
        assert updater._application_stage(store, "prepare", python=interpreter)["stage_error"] == error
        recovered = updater._application_stage(store, "render", python=interpreter)
        assert "stage_error" not in recovered and store.load() == original


def test_invalid_authorization_response_keeps_original_stage_error(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, before, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["stage_error"] = {"stage": "authorize", "detail": "original authorization refusal"}
        store.save(record)
        outcomes = iter(["invalid-json", json.dumps({"directory": "/native", "revision": before})])
        monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs:
                            subprocess.CompletedProcess([], 0, next(outcomes), ""))
        interpreter = Path(sys.executable).resolve()
        with pytest.raises(UpdateError, match="no bound installation context"):
            updater._application_stage(store, "authorize", python=interpreter)
        assert store.load() == record
        authorized = updater._application_stage(store, "authorize", python=interpreter)
        assert authorized == {"directory": "/native", "revision": before, "interpreter": str(interpreter)}
        assert "stage_error" not in store.load()


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
    default_status = subprocess.run([str(installation.cli), "host", "update"], env=constructed_env(),
                                    cwd=installation.data_root, capture_output=True, text=True, timeout=10)
    assert default_status.returncode == 0, default_status.stderr
    assert json.loads(default_status.stdout)["read_only"] is True
    assert json.loads(default_status.stdout)["update"]["target"] == target


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
    monkeypatch.setattr(update_operation, "validate_candidate_consumers", lambda *_, baseline: None if baseline is None else pytest.fail("unexpected conversion baseline"))
    monkeypatch.setattr(update_operation, "_schema", lambda *_: 14)
    monkeypatch.setattr(update_operation, "_catalog", lambda *_: (update_operation.platform.system(), "", [], set(), {}))
    monkeypatch.setattr(update_operation, "freeze_service_intent", lambda *_a, **_k: [])
    monkeypatch.setattr(update_operation, "prepare_publication", lambda *_: None)
    with UpdateStore(installation) as store:
        staged = store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001")
        result = update_operation.prepare_operation(
            store, rendered, SimpleNamespace(package=None, read=lambda *_: "catalog"), installation.data_root,
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
        record = prepare(store, target, consumers={"example.service": {"intent": {
            "installed": [{"path": str(installation.data_root / "example.service")}], "bot_binding": None}}})
        record["phase"] = "quiesce"
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
        record = prepare(store, target, consumers={"example.service": {"intent": {
            "installed": [{"path": str(installation.data_root / "example.service")}], "bot_binding": None}}})
        record["phase"] = "quiesce"
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


@pytest.mark.parametrize("occupied", [False, True])
def test_independent_quiet_rechecks_added_consumer_absence(update, occupied):
    """Uninstalled additions must still be absent after an interrupted cutover."""
    import shlex
    from claudlobby.git_update import _prepare_quiet_support, _independent_quiet
    installation, _, _, commit = update
    units = installation.data_root / "units"
    units.mkdir()
    package = installation.source / "claudlobby"
    shutil.copy2(Path(__file__).resolve().parents[1] / "claudlobby/supervision_runtime.py",
                 package / "supervision_runtime.py")
    catalog = "manager\tLinux\ndirectory\t" + str(units)
    (package / "_runtime_scripts/supervisor.sh").write_text(
        "svc_inventory_catalog() { printf '%s\\n' " + shlex.quote(catalog) + "; }\n"
        "svc_inventory_state() { printf 'not-found not-found inactive\\n'; }\n"
        "svc_activation_quiet() { return 99; }\n")
    target = commit("Retained read-only absence observer")
    with UpdateStore(installation) as store:
        record = prepare(store, target, consumers={"example.service": {"intent": {
            "kind": "added", "installed": [], "bot_binding": None,
            "manager": "Linux", "domain": "", "destination": str(units / "example.service"),
            "search_directories": [str(units)]}}})
        record["phase"] = "quiesce"
        store.save(record)
        _prepare_quiet_support(store)
        if occupied:
            (units / "example.service").write_text("foreign service")
            with pytest.raises(RuntimeError, match="already exists"):
                _independent_quiet(store)
            assert store.load()["quiet"] == {}
        else:
            _independent_quiet(store)
            assert store.load()["quiet"]["example.service"]["identity"] == record["scope"]["example.service"]


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


def test_retained_job_support_loads_canonical_owners_without_source_or_environment(update):
    from claudlobby import git_update as updater
    installation, _, _, commit = update
    root = Path(__file__).resolve().parents[1] / "claudlobby"
    for name in ("update_job.py", "supervision_inventory.py", "resources.py", "installation.py", "supervision_runtime.py"):
        shutil.copy2(root / name, installation.source / "claudlobby" / name)
    for name in ("supervisor.sh", "supervisor-caller.py"):
        shutil.copy2(root / "_runtime_scripts" / name, installation.native / name)
    before = commit("Current fixed native updater support")
    (installation.source / "templates/candidate.txt").write_text("next fixed support\n")
    target = commit("Target fixed native updater support")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], installation.data_root)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        store.save(record)
        updater._prepare_job_support(store)
        installation.source.rename(installation.directory / "source-unavailable")
        jobs, independent, adapter = updater._retained_job(store)
        assert independent.data_root == installation.data_root
        assert Path(jobs.__file__).parent == adapter.package.native
        assert hasattr(jobs, "verify_update_job_runner")
        from types import SimpleNamespace
        foreign = SimpleNamespace(directory=installation.directory,
                                  data_root=installation.data_root / "another-host")
        with pytest.raises(UpdateError, match="installation record differs"):
            jobs.update_job_definition(foreign, manager="Linux", domain="", install_directory=store.root)
        assert not installation.environment.exists()
        support = adapter.package.native / "supervisor-caller.py"
        support.write_text("foreign membership predicate")
        with pytest.raises(UpdateError, match="support changed"):
            updater._retained_job(store)


def _bound_native_job(store, state="inactive"):
    from types import SimpleNamespace
    from claudlobby import git_update as updater
    record = store.load()
    directory = store.root / "native"
    directory.mkdir(exist_ok=True)
    job = SimpleNamespace(manager="Linux", target="example-updater.service",
                          source=store.installation.bin / "example-updater.service",
                          installed=directory / "example-updater.service", content=b"fixed native job")
    record.update(native_install_directory=str(directory), updater_job={
        "manager": job.manager, "target": job.target, "source": str(job.source),
        "installed": str(job.installed), "sha256": updater._digest(job.content)})
    store.save(record)
    jobs = SimpleNamespace(_definition=lambda *_args, **_kwargs: job,
        observe_update_job=lambda *_args, **_kwargs: {"target": job.target, "state": state})
    return jobs, job


@pytest.mark.parametrize("state", ["running", "starting", "inactive", "failed"])
def test_dispatch_reconciles_same_job_and_retains_original_request(update, monkeypatch, state):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="quiesce", job_dispatch={"state": "pending", "attempt": 1})
        store.save(record)
        jobs, job = _bound_native_job(store, state)
        monkeypatch.setattr(updater, "_retained_job", lambda _: (jobs, installation, object()))
        observed, dispatch = updater._dispatch_update_job(store)
        assert store.load()["request_id"] == record["request_id"]
        if state in {"running", "starting"}:
            assert observed["dispatch"] == "attached" and dispatch is None
            assert store.load()["job_dispatch"]["attempt"] == 1
        else:
            # Native inactivity permits resuming the same coordinator, whose
            # canonical operation reconciles original business-effect receipts.
            assert observed is None and dispatch[0] is jobs
            assert store.load()["job_dispatch"] == {"state": "pending", "attempt": 2}


def test_unknown_job_observation_does_not_dispatch_or_replace_receipt(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="quiesce", job_dispatch={"state": "pending", "attempt": 1})
        store.save(record)
        jobs, _ = _bound_native_job(store)
        jobs.observe_update_job = lambda *_args, **_kwargs: (_ for _ in ()).throw(UpdateError("unknown native state"))
        monkeypatch.setattr(updater, "_retained_job", lambda _: (jobs, installation, object()))
        original = store.load()
        with pytest.raises(UpdateError, match="unknown native"):
            updater._dispatch_update_job(store)
        assert store.load() == original


def test_public_apply_authorizes_before_record_candidate_or_native_effects(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, before, target, _ = update
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    calls = []
    def refused(_store, stage, **_kwargs):
        calls.append(stage)
        raise UpdateError("operator ancestry refused")
    monkeypatch.setattr(updater, "_application_stage", refused)
    monkeypatch.setattr(updater, "prepare_candidate_render", lambda *_args, **_kwargs: pytest.fail("candidate effect before authority"))
    with pytest.raises(UpdateError, match="operator ancestry"):
        updater.apply_or_continue(installation, target=target)
    assert calls == ["authorize"] and read_update(installation) is None
    assert installation.preflight() == before


def test_native_runner_checks_membership_before_operation_and_uses_waiting_lock(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="quiesce", job_dispatch={"state": "pending", "attempt": 1})
        store.save(record)
        jobs, _ = _bound_native_job(store)
    jobs.verify_update_job_runner = lambda *_args, **_kwargs: (_ for _ in ()).throw(UpdateError("external native caller"))
    monkeypatch.setattr(updater, "_retained_job", lambda _: (jobs, installation, object()))
    monkeypatch.setattr(updater, "run_prepared_update", lambda *_: pytest.fail("external caller reached coordinator"))
    waits = []
    def store_factory(current, *, wait=False):
        waits.append(wait)
        return UpdateStore(current, wait=wait)
    monkeypatch.setattr(updater, "UpdateStore", store_factory)
    with pytest.raises(UpdateError, match="external native"):
        updater._run_native_update(installation)
    assert waits == [True]
    assert read_update(installation)["job_dispatch"]["state"] == "pending"


@pytest.mark.parametrize("initialize", [False, True])
def test_public_continue_attaches_original_operation_without_mutable_application(update, monkeypatch, initialize):
    from types import SimpleNamespace
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        scope = {"directory": str(store.root / "native"),
                 "manifests": [str(store.root / "fleet.yaml")]} if initialize else None
        record = prepare(store, target, initialization=scope)
        record.update(phase="environment", maintenance=True, job_support={}, consumers={"example.service": {"intent": {
            "installed": [{"path": str(store.root / "example.service")}], "bot_binding": None}}})
        store.save(record)
        jobs, _ = _bound_native_job(store, "running")
    installation.source.rename(installation.directory / "source-unavailable")
    monkeypatch.setattr(updater, "_application_stage", lambda *_args, **_kwargs: pytest.fail("continuation imported mutable app"))
    adapter = SimpleNamespace(call=lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(updater, "_retained_job", lambda _: (jobs, installation, adapter))
    result = updater.apply_or_continue(installation)
    assert result["job"]["dispatch"] == "attached"
    assert result["update"]["request_id"] == record["request_id"]
    assert result["update"]["target"] == target and result["update"]["maintenance"]
    assert result["update"].get("initialization_scope") == scope


def test_copied_native_runner_verifies_retained_binding_without_src_or_env(update):
    """Fixture native observations only; no live native manager is called."""
    import hashlib
    import shlex
    from claudlobby import git_update as updater
    installation, _, _, commit = update
    root = Path(__file__).resolve().parents[1] / "claudlobby"
    for name in ("update_job.py", "supervision_inventory.py", "resources.py", "installation.py", "supervision_runtime.py"):
        shutil.copy2(root / name, installation.source / "claudlobby" / name)
    shutil.copy2(root / "_runtime_scripts/supervisor-caller.py", installation.native / "supervisor-caller.py")
    directory = installation.data_root / "native"
    directory.mkdir()
    label = "com.claudlobby.update." + hashlib.sha256(str(installation.directory).encode()).hexdigest()[:16]
    target = label + ".service"
    values = {"Id": target, "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "static",
        "FragmentPath": str(directory / target), "DropInPaths": "", "NeedDaemonReload": "no",
        "Triggers": "", "TriggeredBy": "", "WorkingDirectory": str(installation.directory),
        "Environment": "", "ExecStart": "fixture D-Bus command; exact argv is typed separately"}
    properties = "\n".join(f"{key}={value}" for key, value in values.items())
    typed = json.dumps({"type": "a(sasbttttuii)", "data": [[str(installation.bin / "claudlobby-update"),
        [str(installation.bin / "claudlobby-update"), "_run"], False, 0, 0, 0, 0, 0, 0, 0]]})
    catalog = f"manager\tLinux\ndirectory\t{directory}\n"
    fixture_native = (
        "svc_inventory_catalog() { printf '%s' " + shlex.quote(catalog) + "; }\n"
        "svc_inventory_properties() { printf '%s' " + shlex.quote(properties) + "; }\n"
        "svc_inventory_state() { printf 'static loaded active\\n'; }\n"
        "svc_update_job_exec() { printf '%s' " + shlex.quote(typed) + "; }\n"
        "svc_activation_assert_external() { return 1; }\n")
    (installation.native / "supervisor.sh").write_text(fixture_native)
    before = commit("Retained fixture native observation provider")
    (installation.source / "templates/candidate.txt").write_text("candidate fixture\n")
    target_commit = commit("Candidate fixture update")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], installation.data_root)
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    install_updater(installation, interpreter=Path(sys.executable).resolve())
    with UpdateStore(installation) as store:
        record = prepare(store, target_commit)
        record.update(phase="quiesce", consumers={})
        store.save(record)
        jobs, independent, adapter = updater._prepare_job_support(store)
        job = jobs.update_job_definition(independent, manager="Linux", domain="", install_directory=directory)
        for path in (job.source, job.installed):
            path.write_bytes(job.content)
            path.chmod(0o644)
        assert type(independent) is not type(store.installation)
        jobs.bind_update_job(store, adapter, directory)
        record = store.load()
        # A mechanical recovery can positively admit its native job without
        # src/env. Missing Git source then refuses safely before publication.
        record.update(phase="environment", maintenance=True, git_complete=True,
                      job_dispatch={"state": "pending", "attempt": 1}, native_install_directory=str(directory))
        store.save(record)
    installation.source.rename(installation.directory / "source-unavailable")
    assert not installation.environment.exists()
    result = subprocess.run([str(installation.bin / "claudlobby-update"), "_run"],
        env=constructed_env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 6
    assert "ModuleNotFoundError" not in result.stderr and "Traceback" not in result.stderr
    retained = read_update(installation)
    assert retained["request_id"] == record["request_id"]
    assert retained["job_dispatch"]["state"] == "finished"  # Membership gate was passed.
    assert retained["phase"] == "environment" and retained["maintenance"]


def test_public_continue_attaches_while_native_runner_holds_update_lock(update, monkeypatch):
    from types import SimpleNamespace
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record.update(phase="quiesce", consumers={})
        store.save(record)
        jobs, _ = _bound_native_job(store, "running")
        adapter = SimpleNamespace(call=lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
        monkeypatch.setattr(updater, "_retained_job", lambda _: (jobs, installation, adapter))
        result = updater.apply_or_continue(installation, target=target)
        assert result["job"]["dispatch"] == "attached"
        assert result["update"]["request_id"] == record["request_id"]
        with pytest.raises(UpdateError, match="frozen target"):
            updater.apply_or_continue(installation, target="f" * 40)


@pytest.mark.parametrize('changed', [False, True])
def test_explicit_same_commit_apply_renders_config_but_continue_retains_completed_receipt(update, monkeypatch, changed):
    from types import SimpleNamespace
    from claudlobby import git_update as updater
    installation, before, _, _ = update
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    authored = installation.data_root / 'authored-fleet.yaml'
    authored.write_text('original configuration')
    with UpdateStore(installation) as store:
        original = prepare(store, before)
        original.update(phase='done', maintenance=False)
        store.save(original)
    calls = []
    def application(store, stage, **kwargs):
        calls.append(stage)
        assert stage in {'authorize', 'retire-render'}
        if stage == 'authorize':
            return {'interpreter': sys.executable, 'directory': str(store.root / 'units')}
        assert store.load()['request_id'] == original['request_id']
    monkeypatch.setattr(updater, '_application_stage', application)
    def render(store, **kwargs):
        calls.append(('render', authored.read_text()))
        row = store.load()
        assert row['request_id'] != original['request_id'] and row['before'] == row['target'] == before
        row['candidate_render'] = 'd' * 64
        store.save(row)
    monkeypatch.setattr(updater, 'prepare_candidate_render', render)
    def prepare_application(store, **kwargs):
        calls.append('prepare')
        row = store.finalize_prepare(config_fingerprint='d' * 64, scope={})
        row.update(phase='quiesce', native_install_directory=str(kwargs['install_directory']),
                   previous_config='e' * 64, candidate_config={'fingerprint': ('f' if changed else 'e') * 64})
        store.save(row)
    monkeypatch.setattr(updater, 'prepare_application_operation', prepare_application)
    def bind(store, *_args):
        calls.append('bind-job')
        row = store.load(); row['updater_job'] = {}; store.save(row)
    jobs = SimpleNamespace(prepare_update_job=lambda *_a: calls.append('prepare-job'), bind_update_job=bind)
    monkeypatch.setattr(updater, '_prepare_job_support', lambda _store: (jobs, installation, None))
    monkeypatch.setattr(updater, '_dispatch_update_job', lambda _store: ({'dispatch': 'prepared'}, None))
    continued = updater.apply_or_continue(installation)
    assert continued['dispatch'] == 'nothing_pending' and continued['update'] == original
    assert calls == ['authorize']
    calls.clear()
    if changed:
        authored.write_text('reviewed configuration change')
    else:
        # Equal desired snapshot does not prove owned generated bytes are intact.
        (installation.data_root / 'generated.txt').write_text('edited generated bytes')
    result = updater.apply_or_continue(installation, target=before)
    assert calls[:4] == ['authorize', 'retire-render', ('render', authored.read_text()), 'prepare']
    assert result['update']['request_id'] != original['request_id']
    assert result['job']['dispatch'] == 'prepared' and calls[4:] == ['prepare-job', 'bind-job']
    assert result['update']['phase'] == 'quiesce'


@pytest.mark.parametrize('defect', [None, 'foreign', 'foreign-blob', 'changed', 'pending', 'health-errors'])
def test_completed_render_retirement_preserves_unknown_or_pending_scratch(update, defect):
    from claudlobby.config_plan import ConfigPlanBuilder, PlanError
    from claudlobby.update_entry import _retire_completed_render
    installation, before, _, _ = update
    with UpdateStore(installation) as store:
        builder = ConfigPlanBuilder(store.root, effects={})
        builder.file(store.root / 'generated.txt', b'owned generated bytes')
        rendered = builder.freeze(before)
        record = store.prepare(before, config_fingerprint=rendered.fingerprint, scope={},
                               request_id='00000000-0000-4000-8000-000000000001')
        record.update(phase='done', maintenance=False)
        if defect == 'pending':
            record['phase'] = 'quiesce'
        if defect == 'health-errors':
            record['health_errors'] = ['unknown stop']
        store.save(record)
        if defect == 'foreign':
            (rendered.directory / 'foreign.txt').write_text('retain')
        if defect == 'foreign-blob':
            (rendered.directory / 'files' / ('f' * 64)).write_text('retain')
        if defect == 'changed':
            (rendered.directory / 'render.json').write_text('{}')
        if defect is not None:
            with pytest.raises((UpdateError, PlanError)):
                _retire_completed_render(store)
            assert rendered.directory.exists() and store.load() == record
        else:
            _retire_completed_render(store)
            retired = store.load()
            assert not rendered.directory.exists() and retired['request_id'] == record['request_id']
            assert retired['render_retirement']['fingerprint'] == rendered.fingerprint
            _retire_completed_render(store)  # Crash after retirement, before new preparation.
            assert store.load() == retired


@pytest.mark.parametrize('remaining_change', [None, 'foreign', 'bytes', 'mode', 'link'])
def test_completed_render_retirement_resumes_partial_deletion_without_deleting_foreign_paths(update, monkeypatch,
                                                                                           remaining_change):
    from claudlobby.config_plan import ConfigPlanBuilder
    from claudlobby.update_entry import _retire_completed_render
    installation, before, _, _ = update
    with UpdateStore(installation) as store:
        builder = ConfigPlanBuilder(store.root, effects={})
        builder.file(store.root / 'generated.txt', b'owned generated bytes')
        rendered = builder.freeze(before)
        record = store.prepare(before, config_fingerprint=rendered.fingerprint, scope={},
                               request_id='00000000-0000-4000-8000-000000000001')
        record.update(phase='done', maintenance=False)
        store.save(record)
        unlink = Path.unlink
        def interrupted(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path.parent == rendered.directory / 'files':
                raise OSError('interrupted after first owned blob deletion')
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'unlink', interrupted)
            with pytest.raises(OSError, match='interrupted'):
                _retire_completed_render(store)
        retained = store.load()
        assert retained['render_retirement']['fingerprint'] == rendered.fingerprint
        assert not list((rendered.directory / 'files').iterdir())
        manifest = rendered.directory / 'render.json'
        if remaining_change == 'foreign':
            (rendered.directory / 'foreign.txt').write_text('retain foreign bytes')
        elif remaining_change == 'bytes':
            manifest.write_text('{}')
        elif remaining_change == 'mode':
            manifest.chmod(0o644)
        elif remaining_change == 'link':
            manifest.unlink()
            manifest.symlink_to(store.root / 'generated.txt')
        if remaining_change is not None:
            with pytest.raises(UpdateError):
                _retire_completed_render(store)
            assert manifest.exists() or manifest.is_symlink()
            assert store.load() == retained
            if remaining_change == 'foreign':
                assert (rendered.directory / 'foreign.txt').read_text() == 'retain foreign bytes'
        else:
            _retire_completed_render(store)
            assert not rendered.directory.exists() and store.load() == retained


@pytest.mark.parametrize("defect", ["missing", "extra", "target", "intent"])
def test_independent_quiet_refuses_changed_consumer_consent_before_native_reads(update, monkeypatch, defect):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with UpdateStore(installation) as store:
        record = prepare(store, target, consumers={"example.service": {"intent": {
            "installed": [], "bot_binding": None}}})
        if defect == "missing":
            record["consumers"].clear()
        elif defect == "extra":
            record["consumers"]["foreign.service"] = {"intent": {"target": "foreign.service"}}
        elif defect == "target":
            record["consumers"]["example.service"]["intent"]["target"] = "foreign.service"
        else:
            record["consumers"]["example.service"]["intent"]["installed"] = [{"path": "/foreign.service"}]
        store.save(record)
        monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("native read"))
        with pytest.raises(UpdateError, match="original consent"):
            updater._independent_quiet(store)
        assert store.load()["quiet"] == {}


def test_initialization_scope_is_frozen_before_configuration_finalization(update):
    installation, _, target, _ = update
    scope = {"directory": str(installation.data_root / "native"),
             "manifests": [str(installation.data_root / "fleet.yaml")]}
    with UpdateStore(installation) as store:
        staged = store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001",
                                      initialization_scope=scope)
        assert staged["initialization_scope"] == scope and not staged["finalized"]
        other = {**scope, "manifests": [str(store.root / "other/fleet.yaml")]}
        with pytest.raises(UpdateError, match="original frozen"):
            store.prepare_target(target, request_id=staged["request_id"], initialization_scope=other)
        changed = store.load()
        changed["initialization_scope"] = other
        with pytest.raises(UpdateError, match="frozen initialization"):
            store.save(changed)
        store.finalize_prepare(config_fingerprint=SHA, scope={})
        changed = store.load()
        changed["initialize"] = False
        with pytest.raises(UpdateError, match="invalid or unsupported"):
            store.save(changed)
        assert store.load()["initialization_scope"] == scope
        assert store.load()["request_id"] == staged["request_id"]


@pytest.mark.parametrize("defect", ["relative", "duplicate", "not-manifest", "mixed-fields"])
def test_initialization_rejects_malformed_scope_before_record_or_native_effects(update, monkeypatch, defect):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    scope = {"directory": str(installation.data_root / "native"),
             "manifests": [str(installation.data_root / "fleet.yaml")]}
    if defect == "relative":
        scope["directory"] = "relative"
    elif defect == "duplicate":
        scope["manifests"] *= 2
    elif defect == "not-manifest":
        scope["manifests"] = [str(installation.data_root / "projects.yaml")]
    else:
        scope["release_id"] = "r-" + "a" * 64
    monkeypatch.setattr(updater, "_application_stage", lambda *_args, **_kwargs: pytest.fail("authorize"))
    with pytest.raises(UpdateError, match="initialization"):
        updater.apply_or_continue(installation, target=target, initialization=scope)
    assert read_update(installation) is None


@pytest.mark.parametrize("defect", [None, "bot", "active-config", "unobserved-directory"])
def test_initial_authority_uses_cold_operator_gate_and_observed_native_placement(update, monkeypatch, defect):
    import platform
    from types import SimpleNamespace
    from claudlobby import update_entry, supervision_inventory
    from claudlobby.commands import operator_context
    from claudlobby.command_result import CommandFailure
    installation, before, _, _ = update
    manifest = installation.data_root / "fleet.yaml"
    manifest.write_text("fleet: {name: example}\n")
    directory = installation.data_root.parent / "native-units"
    scope = {"directory": str(directory), "manifests": [str(manifest)]}
    events = []
    package = SimpleNamespace(source_revision=before)
    if defect == "bot":
        monkeypatch.setenv("BOT_ID", "worker")
    else:
        monkeypatch.setattr(operator_context, "require_operator_context", lambda root=None:
                            events.append(("operator", root)))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    observed = directory if defect != "unobserved-directory" else directory.parent / "other-native"
    def read(function):
        events.append(function)
        return f"manager\tLinux\ndirectory\t{observed}\n"
    monkeypatch.setattr(supervision_inventory, "Adapter", lambda _: SimpleNamespace(read=read))
    with UpdateStore(installation) as store:
        if defect == "active-config":
            (store.state / "config.json").write_text("unrelated active evidence")
        if defect is None:
            assert update_entry._authorize_initialization(store, scope, package) == {
                "directory": str(directory), "revision": before}
            assert events == [("operator", None), "svc_inventory_catalog"]
        elif defect == "bot":
            with pytest.raises(CommandFailure, match="operator shell"):
                update_entry._authorize_initialization(store, scope, package)
            assert events == []
        elif defect == "active-config":
            with pytest.raises(UpdateError, match="existing active"):
                update_entry._authorize_initialization(store, scope, package)
            assert events == [("operator", None)]
        else:
            with pytest.raises(UpdateError, match="not observed"):
                update_entry._authorize_initialization(store, scope, package)
        assert store.load() is None
        assert not (store.root / "state/plane/plane.db").exists()


def test_initial_apply_authorizes_before_effects_and_continue_retains_same_scope_request(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, before, target, _ = update
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    manifest = installation.data_root / "fleet.yaml"
    manifest.write_text("fleet: {name: example}\n")
    scope = {"directory": str(installation.data_root.parent / "native"), "manifests": [str(manifest)]}
    calls = []
    def authorize(store, stage, **kwargs):
        assert stage == "authorize" and kwargs == {"initialization": scope}
        calls.append("authorize")
        if store.load() is not None:
            assert store.load()["initialization_scope"] == scope
        return {"directory": scope["directory"], "interpreter": str(Path(sys.executable).resolve()),
                "revision": before}
    monkeypatch.setattr(updater, "_application_stage", authorize)
    def interrupt(store, **_kwargs):
        assert calls[-1] == "authorize"
        assert store.load()["initialization_scope"] == scope
        calls.append("candidate")
        raise OSError("preparation interrupted before dependencies")
    monkeypatch.setattr(updater, "prepare_candidate_render", interrupt)
    with pytest.raises(OSError, match="preparation interrupted"):
        updater.apply_or_continue(installation, target=target, initialization=scope)
    original = read_update(installation)
    with pytest.raises(OSError, match="preparation interrupted"):
        updater.apply_or_continue(installation)
    assert read_update(installation)["request_id"] == original["request_id"]
    assert read_update(installation)["initialization_scope"] == scope
    with pytest.raises(UpdateError, match="original frozen"):
        updater.apply_or_continue(installation, initialization={**scope, "directory": scope["directory"] + "-other"})
    assert calls == ["authorize", "candidate", "authorize", "candidate"]
    assert installation.preflight() == before


@pytest.mark.parametrize("replace_after_dispatch", [False, True])
def test_first_install_preview_retains_original_operation_for_continue(update, monkeypatch, replace_after_dispatch):
    from types import SimpleNamespace
    from claudlobby import git_update as updater
    installation, before, target, _ = update
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    scope = {"directory": str(installation.data_root / "native"),
             "manifests": [str(installation.data_root / "fleet.yaml")]}
    calls = []
    def authorize(_store, stage, **kwargs):
        assert stage == "authorize" and kwargs == {"initialization": scope}
        return {"directory": scope["directory"], "interpreter": str(Path(sys.executable).resolve())}
    def render(store, **_kwargs):
        calls.append("render")
        record = store.load(); record["candidate_render"] = SHA; store.save(record)
    def finalize(store, **_kwargs):
        calls.append("finalize")
        record = store.load()
        store.prepare(target, config_fingerprint=SHA, scope={}, request_id=record["request_id"])
        record = store.load()
        record.update(phase="quiesce", native_install_directory=scope["directory"])
        store.save(record)
    def support(store):
        calls.append("native")
        jobs, job = _bound_native_job(store)
        jobs.prepare_update_job = lambda *_: None
        jobs.bind_update_job = lambda *_: None
        def run(*_args, **_kwargs):
            if replace_after_dispatch:
                replacement = read_update(installation)
                replacement["request_id"] = "00000000-0000-4000-8000-000000000099"
                updater._write(store.state / "update.json", json.dumps(replacement).encode())
            return {"dispatch": "started"}
        jobs.run_update_job = run
        return jobs, installation, SimpleNamespace()
    monkeypatch.setattr(updater, "_application_stage", authorize)
    monkeypatch.setattr(updater, "prepare_candidate_render", render)
    monkeypatch.setattr(updater, "prepare_application_operation", finalize)
    monkeypatch.setattr(updater, "_prepare_job_support", support)
    monkeypatch.setattr(updater, "_retained_job", lambda store: support(store))
    result = updater.apply_or_continue(installation, target=target, initialization=scope, prepare_only=True)
    with UpdateStore(installation) as borrowed:
        prepared = updater._prepare_update_locked(borrowed, target=target, initialization=scope, prepare_only=True)
        assert prepared == result["update"]
        with pytest.raises(updater.UpdateBusyError):
            with UpdateStore(installation):
                pytest.fail("first preparation released its authoring lock")
    assert result["dispatch"] == "prepared" and calls == ["render", "finalize"]
    original = result["update"]
    assert original["phase"] == "quiesce" and original["initialization_scope"] == scope
    assert "updater_job" not in original and installation.preflight() == before
    if replace_after_dispatch:
        with pytest.raises(UpdateError, match=original["request_id"]):
            updater.apply_or_continue(installation, expected_request_id=original["request_id"])
        assert read_update(installation)["request_id"] == "00000000-0000-4000-8000-000000000099"
        return
    resumed = updater.apply_or_continue(installation, expected_request_id=original["request_id"])
    assert resumed["job"]["dispatch"] == "started" and "native" in calls
    assert resumed["update"]["request_id"] == original["request_id"]
    assert resumed["update"]["initialization_scope"] == scope
    assert calls.count("render") == calls.count("finalize") == 1
    with pytest.raises(UpdateError, match="undispatched first-install"):
        updater.apply_or_continue(installation, prepare_only=True)


def test_preview_refuses_noninitial_or_busy_operations_without_effects(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with pytest.raises(UpdateError, match="process lock"):
        updater._prepare_update_locked(UpdateStore(installation), target=target, prepare_only=True)
    with pytest.raises(UpdateError, match="undispatched first-install"):
        updater.apply_or_continue(installation, target=target, prepare_only=True)
    assert read_update(installation) is None
    with UpdateStore(installation) as store:
        original = prepare(store, target)
        with pytest.raises(updater.UpdateBusyError, match="original operation"):
            updater.apply_or_continue(installation, prepare_only=True)
        assert store.load() == original
    with pytest.raises(UpdateError, match="explicit boolean"):
        updater.apply_or_continue(installation, prepare_only="yes")
    assert read_update(installation) == original


@pytest.mark.parametrize("busy", [False, True])
def test_expected_request_refuses_replacement_before_effects(update, monkeypatch, busy):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    def no_effect(*_args, **_kwargs):
        pytest.fail("replacement operation must not be authorized, prepared or dispatched")
    monkeypatch.setattr(updater, "_application_stage", no_effect)
    monkeypatch.setattr(updater, "_retained_job", no_effect)
    monkeypatch.setattr(updater, "_prepare_job_support", no_effect)
    with UpdateStore(installation) as store:
        original = prepare(store, target)
        replacement = deepcopy(original)
        replacement["request_id"] = "00000000-0000-4000-8000-000000000099"
        updater._write(store.state / "update.json", json.dumps(replacement).encode())
        if busy:
            with pytest.raises(UpdateError, match=original["request_id"]):
                updater.apply_or_continue(installation, expected_request_id=original["request_id"])
    if not busy:
        with pytest.raises(UpdateError, match=original["request_id"]):
            updater.apply_or_continue(installation, expected_request_id=original["request_id"])
    assert read_update(installation) == replacement


def test_initial_render_uses_original_manifests_and_candidate_resources_with_final_runtime_paths(update, monkeypatch):
    from claudlobby import update_entry, active_config
    from claudlobby.resources import get_resources
    installation, _, target, _ = update
    root_manifest = installation.data_root / "fleet.yaml"
    root_manifest.write_text("fleet: {name: example}\n")
    overlay = installation.data_root / "local/second"
    overlay.mkdir(parents=True)
    (overlay / "fleet.yaml").write_text("fleet: {name: second}\n")
    scope = {"directory": str(installation.data_root.parent / "native"),
             "manifests": [str(root_manifest), str(overlay / "fleet.yaml")]}
    with UpdateStore(installation) as store:
        record = store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001",
                                      initialization_scope=scope)
        source = store.work / "candidate"
        _run(["git", "-C", installation.source, "worktree", "add", "--detach", source, target], store.root)
        package = get_resources(installation=installation, checkout=source)
        monkeypatch.setattr(active_config, "read_current_config", lambda *_args, **_kwargs:
                            pytest.fail("initial render cannot read sealed/current configuration"))
        paths = update_entry._render_paths(store, record, package)
        assert {str(path.fleet_yaml) for path in paths} == set(scope["manifests"])
        assert all(path.package.library == source / "library" for path in paths)
        assert all(path.package.source_revision == target for path in paths)
        assert all(path.package.runtime_native == installation.native for path in paths)
        assert all(path.package.runtime_library == installation.source / "library" for path in paths)
        assert store.load()["request_id"] == record["request_id"]


def _prepared_update_remote(update, tmp_path):
    """A new commit exists only in an explicit local Git remote, never fetched yet."""
    from claudlobby.git_update import _digest
    installation, before, _, _ = update
    remote = tmp_path / "trusted-remote"
    _run(["git", "clone", "--quiet", "--no-hardlinks", installation.source, remote], tmp_path)
    _run(["git", "-C", remote, "checkout", "--quiet", "-b", "updates", update[2]], tmp_path)
    (remote / "templates/day-two.txt").write_text("new upstream source\n")
    _run(["git", "-C", remote, "add", "."], tmp_path)
    _run(["git", "-C", remote, "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
          "commit", "--quiet", "-m", "Day-two update"], tmp_path)
    target = _run(["git", "-C", remote, "rev-parse", "HEAD"], tmp_path).strip()
    interpreter = Path(sys.executable).resolve()
    receipt = {"schema": 1, "purpose": "installation-preparation-only",
        "installation": str(installation.directory), "data_root": str(installation.data_root),
        "remote": str(remote), "ref": "refs/heads/updates", "interpreter": str(interpreter),
        "interpreter_sha256": _digest(interpreter.read_bytes()), "revision": before,
        "lock_sha256": _digest(LOCK), "environment_owned": True, "complete": True}
    (installation.directory / "preparation.json").write_text(json.dumps(receipt))
    install_launcher(installation, interpreter=interpreter)
    return installation, before, target, receipt


def _stop_after_public_target(monkeypatch):
    """Substitute authorization/candidate boundaries, retaining real Git/UpdateStore."""
    from claudlobby import git_update as updater
    calls = []
    def application(store, stage, **kwargs):
        calls.append(stage)
        assert stage == "authorize"
        return {"directory": str(store.root / "native"), "interpreter": str(Path(sys.executable).resolve())}
    monkeypatch.setattr(updater, "_application_stage", application)
    def render(store, **kwargs):
        raise UpdateError("target frozen before candidate effects")
    monkeypatch.setattr(updater, "prepare_candidate_render", render)
    return calls


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("after_update", [False, True])
def test_public_apply_fetches_missing_commit_only_from_prepared_source(
        update, tmp_path, monkeypatch, explicit, after_update):
    from claudlobby import git_update as updater
    installation, before, target, receipt = _prepared_update_remote(update, tmp_path)
    if after_update:
        _run(["git", "-C", installation.source, "checkout", "--quiet", update[2]], tmp_path)
        before = update[2]  # The completed bootstrap receipt intentionally keeps its initial revision.
    assert updater._git(installation, "cat-file", "-e", target, check=False).returncode != 0
    calls = _stop_after_public_target(monkeypatch)
    actual_git, fetches = updater._git, []
    def git(current, *args, **kwargs):
        if args[0] == "fetch":
            assert calls == ["authorize"] and read_update(installation) is None
            fetches.append(args)
        return actual_git(current, *args, **kwargs)
    monkeypatch.setattr(updater, "_git", git)
    # Ambient Git selectors cannot redirect either preflight or fetch.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "foreign.git"))
    with pytest.raises(UpdateError, match="target frozen before candidate"):
        updater.apply_or_continue(installation, target=target if explicit else None, from_prepared_remote=True)
    record = read_update(installation)
    assert record["before"] == before and record["target"] == target
    assert record["phase"] == "prepare" and record["maintenance"] is False
    assert fetches == [("fetch", "--no-tags", "--no-recurse-submodules", "--",
                        receipt["remote"], target if explicit else receipt["ref"])]
    assert installation.preflight() == before
    assert not (installation.source / "templates/day-two.txt").exists()


def test_public_apply_pending_uses_original_request_without_receipt_or_refetch(update, tmp_path, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, _, receipt = _prepared_update_remote(update, tmp_path)
    target = update[2]
    with UpdateStore(installation) as store:
        record = prepare(store, target)
    # A moved/unavailable preparation source cannot alter an already frozen operation.
    (installation.directory / "preparation.json").unlink()
    calls = _stop_after_public_target(monkeypatch)
    monkeypatch.setattr(updater, "_fetch_prepared_target", lambda *_: pytest.fail("pending operation refetched"))
    with pytest.raises(UpdateError, match="target frozen before candidate"):
        updater.apply_or_continue(installation, from_prepared_remote=True,
                                  expected_request_id=record["request_id"])
    assert read_update(installation) == record and calls == ["authorize"]
    with pytest.raises(UpdateError, match="existing frozen target"):
        updater.apply_or_continue(installation, target="f" * 40, from_prepared_remote=True)
    assert read_update(installation) == record


@pytest.mark.parametrize("defect", ["root", "alias", "incomplete", "redirected", "dirty"])
def test_public_fetch_refuses_changed_preparation_or_dirty_source_before_network(
        update, tmp_path, monkeypatch, defect):
    from claudlobby import git_update as updater
    installation, before, target, receipt = _prepared_update_remote(update, tmp_path)
    path = installation.directory / "preparation.json"
    if defect == "root":
        receipt["data_root"] = str(tmp_path / "another-root")
    elif defect == "alias":
        receipt["remote"] = "origin"
    elif defect == "incomplete":
        receipt["complete"] = False
    elif defect == "redirected":
        path.rename(tmp_path / "foreign-preparation.json")
        path.symlink_to(tmp_path / "foreign-preparation.json")
    elif defect == "dirty":
        (installation.source / "untracked.txt").write_text("keep operator work\n")
    if defect != "redirected":
        path.write_text(json.dumps(receipt))
    _stop_after_public_target(monkeypatch)
    actual_git = updater._git
    def git(current, *args, **kwargs):
        assert args[0] != "fetch", "refused inputs reached fetch"
        return actual_git(current, *args, **kwargs)
    monkeypatch.setattr(updater, "_git", git)
    with pytest.raises(UpdateError):
        updater.apply_or_continue(installation, target=target, from_prepared_remote=True)
    assert read_update(installation) is None
    if defect == "dirty":
        assert (installation.source / "untracked.txt").read_text() == "keep operator work\n"
    else:
        assert installation.preflight() == before


def test_public_fetch_operator_refusal_precedes_remote_resolution(update, tmp_path, monkeypatch):
    from claudlobby import git_update as updater
    installation, _, target, _ = _prepared_update_remote(update, tmp_path)
    def refused(*_args, **_kwargs):
        raise UpdateError("operator ancestry refused")
    monkeypatch.setattr(updater, "_application_stage", refused)
    monkeypatch.setattr(updater, "_fetch_prepared_target", lambda *_: pytest.fail("fetch preceded authority"))
    with pytest.raises(UpdateError, match="operator ancestry"):
        updater.apply_or_continue(installation, target=target, from_prepared_remote=True)
    assert read_update(installation) is None


@pytest.mark.parametrize("arguments, target, fetch", [
    (["apply"], None, True), (["apply", "a" * 40], "a" * 40, True),
    (["continue"], None, False),
])
def test_public_update_parser_opts_only_apply_into_prepared_remote(update, monkeypatch, capsys,
                                                                  arguments, target, fetch):
    from claudlobby import git_update as updater
    installation = update[0]
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    monkeypatch.setattr(updater, "__file__", str(installation.bin / "update.py"))
    seen = []
    def apply(current, **kwargs):
        seen.append((current.directory, current.data_root, kwargs))
        return {"update": None}
    monkeypatch.setattr(updater, "apply_or_continue", apply)
    assert updater.main(["--root", str(installation.data_root), "host", "update", *arguments]) == 0
    assert seen == [(installation.directory, installation.data_root,
                     {"target": target, "from_prepared_remote": fetch, "convert_sealed": False, "prepare_only": False})]
    assert json.loads(capsys.readouterr().out) == {"update": None}


def test_public_remote_fetch_failure_does_not_prepare_or_cutover(update, tmp_path, monkeypatch):
    from claudlobby import git_update as updater
    installation, before, _, receipt = _prepared_update_remote(update, tmp_path)
    receipt["remote"] = str(tmp_path / "unavailable-remote")
    (installation.directory / "preparation.json").write_text(json.dumps(receipt))
    calls = _stop_after_public_target(monkeypatch)
    with pytest.raises(UpdateError, match="Git observation or cutover refused"):
        updater.apply_or_continue(installation, from_prepared_remote=True)
    assert calls == ["authorize"] and read_update(installation) is None
    assert installation.preflight() == before


@pytest.mark.parametrize("interrupt", [False, True])
def test_conversion_retires_selector_before_effect_and_resumes_rename_without_new_request(update, monkeypatch, interrupt):
    import hashlib
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    selection = {"schema": 1, "activation_id": "original", "release_id": "r-" + "a" * 64,
                 "plan_id": "p-" + "b" * 64}
    raw = json.dumps(selection).encode()
    scope = {"selection": selection, "selector_sha256": hashlib.sha256(raw).hexdigest(),
             "evidence": {str(installation.data_root / "historical.json"): SHA}}
    monkeypatch.setattr(updater, "_application_stage", lambda store, stage: None)
    with UpdateStore(installation) as store:
        store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001", conversion_scope=scope)
        selected = store.state / "selected-release.json"
        selected.write_bytes(raw)
        (store.state / "activation.lock").touch()
        save = store.save
        if interrupt:
            def interrupted(record):
                if record.get("conversion_selector", {}).get("retired"):
                    raise OSError("death after rename")
                save(record)
            monkeypatch.setattr(store, "save", interrupted)
            with pytest.raises(OSError, match="death after rename"):
                with updater._conversion_fence(store):
                    pytest.fail("no effect may precede retirement receipt")
            assert not selected.exists()
            monkeypatch.setattr(store, "save", save)
        with updater._conversion_fence(store):
            archive = updater.assert_conversion_retired(store)
            assert not selected.exists()
            assert Path(archive["path"]).read_bytes() == raw
            assert store.load()["request_id"] == "00000000-0000-4000-8000-000000000001"
        assert Path(archive["path"]).read_bytes() == raw
        selected.write_bytes(raw)  # Reopened old authority is never silently adopted.
        with pytest.raises(UpdateError):
            updater.assert_conversion_retired(store)


def test_conversion_drains_legacy_admission_before_retiring_selector(update, monkeypatch):
    import fcntl
    import hashlib
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    selection = {"schema": 1, "activation_id": "original", "release_id": "r-" + "a" * 64,
                 "plan_id": "p-" + "b" * 64}
    raw = json.dumps(selection).encode()
    scope = {"selection": selection, "selector_sha256": hashlib.sha256(raw).hexdigest(),
             "evidence": {str(installation.data_root / "historical.json"): SHA}}
    monkeypatch.setattr(updater, "_application_stage", lambda store, stage: None)
    with UpdateStore(installation) as store:
        store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001", conversion_scope=scope)
        selected = store.state / "selected-release.json"
        selected.write_bytes(raw)
        with (store.state / "activation.lock").open("w") as admitted:
            fcntl.flock(admitted.fileno(), fcntl.LOCK_SH)
            with pytest.raises(UpdateError, match="old activation or admitted work"):
                with updater._conversion_fence(store):
                    pytest.fail("old admitted work must drain first")
            assert selected.read_bytes() == raw
            assert store.load().get("conversion_selector") is None
        with updater._conversion_fence(store):
            updater.assert_conversion_retired(store)


def test_conversion_requires_explicit_preview_before_any_effect(update):
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    with pytest.raises(UpdateError, match="prepare-only"):
        updater.apply_or_continue(installation, target=target, convert_sealed=True)
    assert read_update(installation) is None


def test_conversion_preview_freezes_original_evidence_before_candidate_and_continue_reuses_it(update, monkeypatch):
    from claudlobby import git_update as updater
    installation, before, target, _ = update
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    scope = {"selection": {"schema": 1, "activation_id": "original", "release_id": "r-" + "a" * 64,
                           "plan_id": "p-" + "b" * 64}, "selector_sha256": SHA,
             "evidence": {str(installation.data_root / "original.json"): SHA}}
    calls = []
    def authorize(store, stage, **kwargs):
        assert stage == "authorize" and kwargs == {"convert_sealed": True}
        calls.append("authorize")
        return {"directory": str(installation.directory / "native"), "revision": before,
                "interpreter": str(Path(sys.executable).resolve()), "conversion_scope": scope}
    monkeypatch.setattr(updater, "_application_stage", authorize)
    def interrupt(store, **kwargs):
        assert store.load()["conversion_scope"] == scope
        calls.append("candidate")
        raise OSError("candidate interruption")
    monkeypatch.setattr(updater, "prepare_candidate_render", interrupt)
    with pytest.raises(OSError, match="candidate interruption"):
        updater.apply_or_continue(installation, target=target, convert_sealed=True, prepare_only=True)
    original = read_update(installation)
    with pytest.raises(OSError, match="candidate interruption"):
        updater.apply_or_continue(installation)
    assert read_update(installation)["request_id"] == original["request_id"]
    assert read_update(installation)["conversion_scope"] == scope
    assert calls == ["authorize", "candidate", "authorize", "candidate"]



def test_conversion_changed_history_under_legacy_ex_preserves_original_selector(update, monkeypatch):
    import fcntl
    import hashlib
    from claudlobby import git_update as updater
    installation, _, target, _ = update
    selection = {"schema": 1, "activation_id": "original", "release_id": "r-" + "a" * 64,
                 "plan_id": "p-" + "b" * 64}
    raw = json.dumps(selection).encode()
    scope = {"selection": selection, "selector_sha256": hashlib.sha256(raw).hexdigest(),
             "evidence": {str(installation.data_root / "historical.json"): SHA}}
    with UpdateStore(installation) as store:
        store.prepare_target(target, request_id="00000000-0000-4000-8000-000000000001", conversion_scope=scope)
        selected = store.state / "selected-release.json"
        selected.write_bytes(raw)
        (store.state / "activation.lock").touch()
        def check(held, stage):
            assert held is store and stage == "conversion-check"
            assert selected.read_bytes() == raw
            for name in ("activation-pending.lock", "activation.lock"):
                with (store.state / name).open("r") as other:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(other.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            raise UpdateError("another sealed lifecycle is unfinished")
        monkeypatch.setattr(updater, "_application_stage", check)
        with pytest.raises(UpdateError, match="unfinished"):
            with updater._conversion_fence(store):
                pytest.fail("no retirement or quiescence after changed history")
        assert selected.read_bytes() == raw
        assert store.load().get("conversion_selector") is None
        assert not list(store.work.glob("sealed-selector-*"))


@pytest.mark.parametrize("wrong_head", [False, True])
def test_conversion_check_entry_requires_original_before_revision(update, monkeypatch, wrong_head):
    import os
    from claudlobby import update_entry as entry, update_operation, resources, supervision_inventory
    installation, before, target, _ = update
    assert before != target
    with UpdateStore(installation) as parent:
        record = prepare(parent, target)
        # Exercise main's real stage/source/revision routing against the fixture
        # Git checkout; only process-origin and package/native boundaries differ.
        monkeypatch.setattr(entry, "__file__", str(installation.source / "claudlobby/update_entry.py"))
        monkeypatch.setattr(entry.sys, "executable", str(installation.environment / "bin/python"))
        def inherited(cls, actual, *, fd, parent_pid, request_id):
            assert actual == installation and fd == parent._fd
            assert parent_pid == os.getpid() and request_id == record["request_id"]
            borrowed = UpdateStore(actual)
            borrowed._fd = os.dup(fd)
            return borrowed
        monkeypatch.setattr(UpdateStore, "from_parent", classmethod(inherited))
        package, adapter, calls = object(), object(), []
        monkeypatch.setattr(resources, "get_resources", lambda **kwargs: package)
        monkeypatch.setattr(supervision_inventory, "Adapter", lambda actual: adapter if actual is package else None)
        monkeypatch.setattr(update_operation, "check_conversion_before_retirement",
                            lambda store, actual: calls.append((store.load()["request_id"], actual)))
        if wrong_head:
            _run(["git", "-C", installation.source, "checkout", "--quiet", target], installation.source.parent)
        args = [str(installation.directory), "conversion-check", str(parent._fd), str(os.getpid()), record["request_id"]]
        if wrong_head:
            with pytest.raises(UpdateError, match="required Git revision"):
                entry.main(args)
            assert calls == []
        else:
            assert entry.main(args) == 0
            assert calls == [(record["request_id"], adapter)]
        parent._assert_locked()


@pytest.mark.parametrize("case", ["retained_gui", "initial", "foreign_uid", "wrong_label", "unknown_gui", "background_from_gui"])
def test_retained_job_uses_only_its_frozen_same_uid_gui_domain_without_application(update, monkeypatch, case):
    import base64
    import os
    from claudlobby import git_update as updater
    installation, _, _, commit = update
    source = Path(__file__).resolve().parents[1] / "claudlobby"
    for name in ("update_job.py", "supervision_inventory.py", "resources.py", "installation.py", "supervision_runtime.py"):
        shutil.copy2(source / name, installation.source / "claudlobby" / name)
    for name in ("supervisor.sh", "supervisor-caller.py"):
        shutil.copy2(source / "_runtime_scripts" / name, installation.native / name)
    before = commit("Retain exact native updater support")
    (installation.source / "templates/candidate.txt").write_text("next support\n")
    target = commit("Next support")
    _run(["git", "-C", installation.source, "checkout", "--quiet", before], installation.data_root)
    with UpdateStore(installation) as store:
        record = prepare(store, target)
        record["phase"] = "quiesce"
        store.save(record)
        jobs, _, _ = updater._prepare_job_support(store)
        native_directory = store.root / "native"
        native_directory.mkdir()
        if case != "initial":
            executable = installation.bin / "claudlobby-update"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            domain = f"user/{os.getuid()}" if case == "background_from_gui" else f"gui/{os.getuid()}"
            job = jobs.update_job_definition(installation, manager="Darwin", domain=domain,
                                             install_directory=native_directory)
            binding = {"manager": "Darwin", "target": job.target, "source": str(job.source),
                "installed": str(job.installed), "sha256": updater._digest(job.content),
                "content_base64": base64.b64encode(job.content).decode("ascii")}
            if case == "foreign_uid":
                binding["target"] = f"gui/{os.getuid() + 1}/" + job.target.rsplit("/", 1)[1]
            elif case == "wrong_label":
                binding["target"] += "-foreign"
            record = store.load()
            record.update(updater_job=binding, native_install_directory=str(native_directory))
            store.save(record)
        original = store.load()
        installation.source.rename(installation.directory / "source-unavailable")
        calls = []
        def native_runner(argv, **kwargs):
            # Exercise the real Adapter's command construction; no native binary runs.
            assert argv[-1] == "svc_inventory_catalog"
            selected = argv[:3] == ["/bin/launchctl", "asuser", str(os.getuid())]
            calls.append(selected)
            if selected and case == "unknown_gui":
                return subprocess.CompletedProcess(argv, 3, "", "unavailable")
            observed = "gui" if selected or case == "background_from_gui" else "user"
            output = f"manager\tDarwin\ndomain\t{observed}/{os.getuid()}\ndirectory\t{native_directory}\nPID\tStatus\tLabel\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        import_module = updater.importlib.import_module
        def copied_module(name, *args, **kwargs):
            module = import_module(name, *args, **kwargs)
            if name.endswith(".supervision_inventory"):
                monkeypatch.setattr(module.Adapter, "_run", staticmethod(native_runner))
            return module
        monkeypatch.setattr(updater.importlib, "import_module", copied_module)
        if case in {"foreign_uid", "wrong_label", "unknown_gui", "background_from_gui"}:
            with pytest.raises(UpdateError, match="retained updater|retained updater job"):
                updater._retained_job(store)
        else:
            _, retained, adapter = updater._retained_job(store)
            assert retained.data_root == installation.data_root
            assert adapter._selected_gui_uid == (os.getuid() if case == "retained_gui" else None)
        assert calls == ({"retained_gui": [False, True], "unknown_gui": [False, True],
                          "background_from_gui": [False]}.get(case, []))
        assert store.load() == original and not installation.environment.exists()


@pytest.mark.parametrize("defect", [None, "removed_mask", "new_definition", "live_socket"])
def test_independent_quiet_rechecks_preserved_mask_before_cutover(update, defect):
    """Retained checks cannot skip a quiet mask because installed definitions are absent."""
    import hashlib
    import os
    import socket
    import tempfile
    from claudlobby.git_update import _prepare_quiet_support, _independent_quiet

    installation, _, _, commit = update
    package = installation.source / "claudlobby"
    shutil.copy2(Path(__file__).resolve().parents[1] / "claudlobby/supervision_runtime.py",
                 package / "supervision_runtime.py")
    (package / "_runtime_scripts/supervisor.sh").write_text(
        "svc_inventory_state() { printf 'masked-runtime masked inactive\\n'; }\n"
        "svc_activation_quiet() { printf 'inactive\\tno-cgroup-witness\\n'; }\n")
    target = commit("Retained masked quiet observer")
    installed = installation.data_root / "example.service"
    mask = installation.data_root / "runtime-mask" / "example.service"
    mask.parent.mkdir()
    mask.symlink_to("/dev/null")
    saved = {"path": str(mask), "resolved": "/dev/null", "mode": mask.lstat().st_mode & 0o777,
             "link": "/dev/null", "content": {"base64": ""}, "sha256": hashlib.sha256(b"").hexdigest()}
    with tempfile.TemporaryDirectory(prefix="mask-", dir="/tmp") as temporary:
        tmpdir = Path(temporary).resolve()
        endpoint = tmpdir / f"tmux-{os.getuid()}" / "example"
        endpoint.parent.mkdir()
        server = None
        try:
            with UpdateStore(installation) as store:
                record = prepare(store, target, consumers={"example.service": {"intent": {
                    "installed": [], "bot_binding": [str(installation.data_root), "example", str(tmpdir)],
                    "preserved_mask": {"snapshot": saved, "installed_path": str(installed)}}}})
                record["phase"] = "quiesce"
                store.save(record)
                _prepare_quiet_support(store)
                if defect == "removed_mask":
                    mask.unlink()
                elif defect == "new_definition":
                    installed.write_text("foreign native definition")
                elif defect == "live_socket":
                    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    server.bind(str(endpoint)); server.listen(1)
                if defect:
                    with pytest.raises(UpdateError if defect != "live_socket" else RuntimeError,
                                       match="mask" if defect != "live_socket" else "socket"):
                        _independent_quiet(store)
                    assert store.load()["quiet"] == {} and store.load()["phase"] == "quiesce"
                else:
                    _independent_quiet(store)
                    assert store.load()["quiet"]["example.service"]["identity"] == record["scope"]["example.service"]
                    assert mask.is_symlink() and not installed.exists()
        finally:
            if server is not None:
                server.close()


@pytest.mark.parametrize("failure", ["refusal", "interrupted"])
def test_application_authorization_failure_without_record_reports_actual_boundary(update, monkeypatch, failure):
    from claudlobby import git_update as updater
    installation, before, _, _ = update
    with UpdateStore(installation) as store:
        assert store.load() is None
        def refused(*_args, **_kwargs):
            if failure == "interrupted":
                raise subprocess.TimeoutExpired("authorize", 900)
            return subprocess.CompletedProcess([], 6, "", "earlier traceback\n  inventory refused unknown native state\x00  \n")
        monkeypatch.setattr(updater.subprocess, "run", refused)
        with pytest.raises(UpdateError) as raised:
            updater._application_stage(store, "authorize", python=Path(sys.executable).resolve())
        message = str(raised.value)
        assert "no update operation was recorded" in message or "before an update operation was recorded" in message
        assert "continue" not in message and "retained operation" not in message
        if failure == "refusal":
            assert "exit 6" in message and "inventory refused unknown native state" in message
            assert "traceback" not in message and "\x00" not in message
        assert store.load() is None
    monkeypatch.undo()
    assert installation.preflight() == before
