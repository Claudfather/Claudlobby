"""Stable native admission executes only committed commands, without services."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from claudlobby.installation import install_launcher, native_start_fence
from claudlobby.runtime_admission import wrap_unit_argv
from tests.conftest import constructed_env
from tests.test_package_resources import _source_installation


def test_launcher_setup_refuses_wrong_record_before_creating_coordination_state(tmp_path):
    from claudlobby.installation import Installation, InstallationError
    installation = _source_installation(tmp_path)
    other = Installation(installation.directory, tmp_path / "wrong-data")
    with pytest.raises(InstallationError, match="recorded paths"):
        install_launcher(other, interpreter=Path(sys.executable).resolve())
    assert not other.data_root.exists()
    assert installation.cli.read_text() == "# fixture launcher\n"


@pytest.fixture
def native(tmp_path):
    installation = _source_installation(tmp_path)
    install_launcher(installation, interpreter=Path(sys.executable).resolve())
    marker = installation.data_root / "executed"
    command = (str(Path(sys.executable).resolve()), "-I", "-B", "-c",
               "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('executed')", str(marker))
    environment = {"CLAUDLOBBY_INSTALLATION": str(installation.directory),
                   "CLAUDLOBBY_ROOT": str(installation.data_root), "CLAUDLOBBY_CLI": str(installation.cli),
                   "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
                   "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
    argv = wrap_unit_argv(environment, unit="example-job", phase="producers", mode="oneshot", argv=command)
    unit = installation.data_root / "example-job.service"
    unit.write_text("approved native definition\n")
    unit.chmod(0o644)
    item = {"source": str(unit), "scope": "host", "phase": "producers",
            "working_directory": str(installation.data_root), "environment": environment,
            "fleet": None, "bot": None, "enroll": True, "service": None,
            "platform": "Darwin" if sys.platform == "darwin" else "Linux",
            "sha256": hashlib.sha256(unit.read_bytes()).hexdigest(), "mode": 0o644,
            "admission": {"kind": "git-native-v1", "unit": "example-job", "mode": "oneshot",
                          "argv": list(argv), "command": list(command)}}
    snapshot = {"schema": 1, "data_root": str(installation.data_root),
                "installation": str(installation.directory), "source": str(installation.source),
                "revision": installation.preflight(), "fleets": {"example": {}},
                "owned_outputs": [], "units": [item]}
    def save():
        body = {key: value for key, value in snapshot.items() if key != "fingerprint"}
        snapshot["fingerprint"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (installation.data_root / "state/config.json").write_text(json.dumps(snapshot))
    save()
    def run(arguments=argv, **extra):
        return subprocess.run(arguments, env=constructed_env(**{**environment, **extra}),
                              cwd=installation.data_root, capture_output=True, text=True, timeout=10)
    return installation, marker, snapshot, save, run, argv


def test_native_entry_executes_exact_committed_command_without_runtime_environment(native):
    installation, marker, _, _, run, _ = native
    assert not installation.environment.exists()
    outcome = run()
    assert outcome.returncode == 0, outcome.stderr
    assert marker.read_text() == "executed"
    # Native entry releases its shared exclusion at exec, not service lifetime.
    with native_start_fence(installation):
        pass


@pytest.mark.parametrize("change", ["argv", "environment", "phase", "unit", "working", "file", "fingerprint"])
def test_native_entry_refuses_unapproved_or_changed_native_binding(native, change):
    installation, marker, snapshot, save, run, original = native
    argv = list(original)
    extra = {}
    if change == "argv":
        argv[-1] += "-unapproved"
    elif change == "environment":
        extra["CLAUDLOBBY_NATIVE_DIR"] = str(installation.data_root)
    elif change == "phase":
        argv[7] = "ingest"
    elif change == "unit":
        argv[5] = "foreign-job"
    elif change == "working":
        snapshot["units"][0]["working_directory"] = str(installation.source)
        save()
    elif change == "file":
        Path(snapshot["units"][0]["source"]).write_text("foreign native change")
    else:
        snapshot["fingerprint"] = "0" * 64
        (installation.data_root / "state/config.json").write_text(json.dumps(snapshot))
    outcome = run(argv, **extra)
    assert outcome.returncode != 0 and not marker.exists()
    assert "Traceback" not in outcome.stderr


def _write_update(installation, phase, maintenance=False):
    (installation.data_root / "state/update.json").write_text(json.dumps({
        "schema": 1, "phase": phase, "maintenance": maintenance}))


def test_native_entry_is_refused_by_maintenance_before_source_or_environment_access(native):
    installation, marker, _, _, run, _ = native
    _write_update(installation, "git", True)
    import shutil
    shutil.rmtree(installation.source)
    outcome = run()
    assert outcome.returncode == 6 and not marker.exists()
    assert "maintenance" in outcome.stderr
    assert "Traceback" not in outcome.stderr


def test_fenced_oneshot_skips_its_tick_without_taking_the_lock(native):
    installation, marker, _, _, run, _ = native
    _write_update(installation, "quiesce")
    import shutil
    shutil.rmtree(installation.source)
    with native_start_fence(installation):
        outcome = run()
    assert outcome.returncode == 0, outcome.stderr
    assert not marker.exists()
    assert "skipped example-job" in outcome.stdout and "fenced" in outcome.stdout
    assert "Traceback" not in outcome.stdout + outcome.stderr


def test_fenced_exec_unit_exits_75_and_runs_nothing(native):
    installation, marker, snapshot, _, run, _ = native
    _write_update(installation, "quiesce")
    item = snapshot["units"][0]
    command = tuple(item["admission"]["command"])
    argv = wrap_unit_argv(item["environment"], unit="example-job", phase="producers", mode="exec", argv=command)
    outcome = run(argv)
    assert outcome.returncode == 75, outcome.stderr
    assert not marker.exists()
    assert "fenced" in outcome.stderr
    assert "Traceback" not in outcome.stderr


@pytest.mark.parametrize("change", ["maintenance", "busy-lock", "unreadable-record"])
def test_oneshot_refusals_other_than_the_fence_stay_failures(native, change):
    installation, marker, _, _, run, _ = native
    if change == "maintenance":
        _write_update(installation, "git", True)
    elif change == "busy-lock":
        _write_update(installation, "prepare")
    else:
        (installation.data_root / "state/update.json").write_text("{")
    if change == "busy-lock":
        with native_start_fence(installation):
            outcome = run()
    else:
        outcome = run()
    assert outcome.returncode == 6, outcome.stdout + outcome.stderr
    assert not marker.exists()
    assert "Traceback" not in outcome.stdout + outcome.stderr


@pytest.mark.parametrize("phase", ["prepare", "commit", "restore", "done"])
def test_oneshot_runs_in_every_phase_that_admits_native_starts(native, phase):
    installation, marker, _, _, run, _ = native
    _write_update(installation, phase)
    outcome = run()
    assert outcome.returncode == 0, outcome.stderr
    assert marker.read_text() == "executed"


def test_native_and_short_cli_cannot_enter_during_cutover_exclusion(native):
    installation, marker, _, _, run, _ = native
    with native_start_fence(installation):
        outcome = run()
        assert outcome.returncode != 0 and not marker.exists()
        assert "runtime" in outcome.stderr
        cli = subprocess.run([str(installation.cli), "--version"], env=constructed_env(),
                             cwd=installation.data_root, capture_output=True, text=True, timeout=10)
        assert cli.returncode != 0 and "runtime" in cli.stderr


@pytest.mark.parametrize("entry", ["native-command", "native-cli", "short-cli"])
def test_lock_covers_cli_lifetime_but_not_native_service_lifetime(native, entry):
    from claudlobby.installation import InstallationError
    installation, marker, snapshot, save, _, _ = native
    released = installation.data_root / "finish"
    program = ("import pathlib,time; "
               f"pathlib.Path({str(marker)!r}).write_text('running'); "
               f"finish=pathlib.Path({str(released)!r})\n"
               "while not finish.exists(): time.sleep(0.02)\n")
    if entry in {"native-cli", "short-cli"}:
        python = installation.environment / "bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(Path(sys.executable).resolve())
        (installation.source / "claudlobby/__main__.py").write_text(
            "def main(argv):\n" + "\n".join("    " + line for line in program.splitlines()) + "\n")
        command = (str(installation.cli), "--root", str(installation.data_root), "--version")
    else:
        command = (str(Path(sys.executable).resolve()), "-I", "-B", "-c", program)
    item = snapshot["units"][0]
    argv = wrap_unit_argv(item["environment"], unit="example-job", phase="producers", mode="oneshot", argv=command)
    item["admission"].update(argv=list(argv), command=list(command))
    save()
    if entry == "short-cli":
        argv = command
    process = subprocess.Popen(argv, env=constructed_env(**item["environment"]),
                               cwd=installation.data_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), process.communicate(timeout=2)
        assert process.poll() is None
        if entry == "short-cli":
            with pytest.raises(InstallationError, match="runtime"):
                with native_start_fence(installation):
                    pass
        else:
            with native_start_fence(installation):
                assert process.poll() is None
    finally:
        released.write_text("finish")
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 0, stdout + stderr


@pytest.fixture
def native_shell(native):
    import shutil
    installation, _, snapshot, save, _, _ = native
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/runtime-admission.sh"
    shutil.copy2(script, installation.native / script.name)
    directory = installation.data_root / "local/example/runtime/bots/worker"
    directory.mkdir(parents=True)
    configuration = directory / "bot.conf"
    configuration.write_text("# approved generated bot configuration\n")
    configuration.chmod(0o600)
    python = installation.environment / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    item = snapshot["units"][0]
    environment = item["environment"]
    environment["FLEET_ROOT"] = str(installation.data_root / "local/example")
    command = (str(installation.native / "start-bot.sh"), str(directory))
    argv = wrap_unit_argv(environment, unit="example.worker", phase="bots", mode="exec", argv=command)
    item.update(scope="bot", phase="bots", fleet="example", bot="worker", working_directory=str(directory),
                admission={"kind": "git-native-v1", "unit": "example.worker", "mode": "exec",
                           "argv": list(argv), "command": list(command)})
    snapshot["owned_outputs"] = [{"target": str(configuration), "state": {
        "kind": "file", "mode": 0o600, "sha256": hashlib.sha256(configuration.read_bytes()).hexdigest()}}]
    save()
    environment = {**environment, "BOT_DIR": str(directory), "BOT_SERVICE": "example.worker",
                   "FLEET_NAME": "example", "LIB_DIR": str(installation.native)}
    # Only the actual admission helper runs: no application, tmux or services.
    body = '''set -euo pipefail
_lc_cleanup() { :; }
. "$LIB_DIR/runtime-admission.sh"
native_admission "$1"
printf '%s\\n' "$_NATIVE_ADMISSION_PYTHON" > "$CLAUDLOBBY_ROOT/acquired"
read -r finish
'''
    def start(operation="start-bot", body=body, **extra):
        return subprocess.Popen(["/bin/bash", "-c", body, "native-shell-test", operation],
                                env=constructed_env(**{**environment, **extra}), cwd=directory,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return installation, directory, environment, start


def _await_native_shell(process, marker):
    for _ in range(500):
        if marker.exists():
            return
        if process.poll() is not None:
            _, errors = process.communicate(timeout=5)
            pytest.fail(f"native shell refused unexpectedly: {errors}")
        time.sleep(0.01)
    pytest.fail("native shell did not acquire its lock")


@pytest.mark.parametrize("operation", ["start-bot", "keepalive"])
def test_actual_shell_admission_holds_lock_and_resolves_recorded_python(native_shell, operation):
    from claudlobby.installation import InstallationError
    installation, _, _, start = native_shell
    process = start(operation)
    try:
        marker = installation.data_root / "acquired"
        _await_native_shell(process, marker)
        assert marker.read_text().strip() == str(installation.environment / "bin/python")
        assert not (installation.data_root / "state/activation.lock").exists()
        with pytest.raises(InstallationError):
            with native_start_fence(installation):
                pass
        # Cleanup must work after the coordinator fences new native entries.
        (installation.data_root / "state/update.json").write_text(json.dumps({
            "schema": 1, "phase": "quiesce", "maintenance": False}))
        _, errors = process.communicate("finish\n", timeout=10)
        assert process.returncode == 0, errors
        with native_start_fence(installation):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("change", ["configuration", "directory", "label", "fleet", "native", "installation", "operation", "quiesce", "maintenance"])
def test_actual_shell_refuses_stale_binding_and_update_fence(native_shell, change):
    installation, directory, _, start = native_shell
    extra = {}
    operation = "start-bot"
    if change == "configuration":
        (directory / "bot.conf").write_text("# unapproved generated change\n")
    elif change == "directory":
        extra["BOT_DIR"] = str(installation.data_root)
    elif change == "label":
        extra["BOT_SERVICE"] = "foreign.worker"
    elif change == "fleet":
        extra["FLEET_NAME"] = "foreign"
    elif change == "native":
        extra["CLAUDLOBBY_NATIVE_DIR"] = str(directory)
    elif change == "installation":
        extra["CLAUDLOBBY_INSTALLATION"] = str(directory)
    elif change == "operation":
        operation = "unrecognized-start"
    else:
        (installation.data_root / "state/update.json").write_text(json.dumps({
            "schema": 1, "phase": "quiesce" if change == "quiesce" else "git",
            "maintenance": change == "maintenance"}))
    process = start(operation, **extra)
    _, errors = process.communicate("finish\n", timeout=10)
    assert process.returncode != 0
    assert not (installation.data_root / "acquired").exists()
    assert "Traceback" not in errors
    with native_start_fence(installation):
        pass


@pytest.mark.parametrize("step", ["start-bot", "keepalive", "python-step"])
def test_actual_shell_exits_75_under_the_fence_and_leaves_no_lock(native_shell, step):
    installation, directory, _, start = native_shell
    _write_update(installation, "quiesce")
    if step == "python-step":
        body = '''set -euo pipefail
exec 9<"$CLAUDLOBBY_ROOT/state/runtime-use.lock"
"$CLAUDLOBBY_CLI" --root "$CLAUDLOBBY_ROOT" _native-shell python 9
'''
        process = start(body=body)
    else:
        process = start(step)
    _, errors = process.communicate("finish\n", timeout=10)
    assert process.returncode == 75, errors
    assert not (installation.data_root / "acquired").exists()
    assert "Traceback" not in errors
    with native_start_fence(installation):
        pass


def test_actual_shell_explicit_unlock_releases_inherited_child_descriptor(native_shell):
    installation, _, _, start = native_shell
    body = '''set -euo pipefail
_lc_cleanup() { :; }
. "$LIB_DIR/runtime-admission.sh"
native_admission start-bot
"$_NATIVE_ADMISSION_PYTHON" -I -B -c 'import os,pathlib,sys,time
os.fstat(9)
root=pathlib.Path(sys.argv[1]); (root/"child").write_text(str(os.getpid()))
while not (root/"finish-child").exists(): time.sleep(.01)
' "$CLAUDLOBBY_ROOT" >/dev/null 2>&1 &
printf '%s\\n' "$_NATIVE_ADMISSION_PYTHON" > "$CLAUDLOBBY_ROOT/acquired"
read -r finish
'''
    process = start(body=body)
    try:
        _await_native_shell(process, installation.data_root / "acquired")
        _await_native_shell(process, installation.data_root / "child")
        _, errors = process.communicate("finish\n", timeout=10)
        assert process.returncode == 0, errors
        # The child still owns fd 9, but explicit unlock releases its flock.
        import os
        os.kill(int((installation.data_root / "child").read_text()), 0)
        with native_start_fence(installation):
            pass
    finally:
        (installation.data_root / "finish-child").touch()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)



@pytest.mark.parametrize("binding", ["canonical", "foreign-installation", "foreign-root", "maintenance"])
def test_generated_bot_cli_symlink_uses_stable_installation_and_preserves_fences(native, binding):
    installation, _, _, _, _, _ = native
    python = installation.environment / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(Path(sys.executable).resolve())
    (installation.source / "claudlobby/__main__.py").write_text(
        "import json,os\n"
        "def main(argv):\n"
        "    print(json.dumps({'installation':os.environ['CLAUDLOBBY_INSTALLATION'],"
        "'root':os.environ['CLAUDLOBBY_ROOT'],'arguments':argv}))\n"
        "    return 0\n")
    bot = installation.data_root / "local/example/runtime/bots/worker"
    cli_dir = bot / ".cli/bin"
    cli_dir.mkdir(parents=True)
    alias = cli_dir / "claudlobby"
    alias.symlink_to(installation.cli)
    environment = {"CLAUDLOBBY_ROOT": str(installation.data_root),
                   "CLAUDLOBBY_INSTALLATION": str(installation.directory)}
    if binding == "foreign-installation":
        # Even a real alternative record cannot retarget this launcher alias.
        from claudlobby.installation import Installation, write_installation
        other = Installation(installation.directory.parent / "other-installation",
                             installation.data_root.parent / "other-data")
        other.directory.mkdir()
        write_installation(other)
        environment["CLAUDLOBBY_INSTALLATION"] = str(other.directory)
    elif binding == "foreign-root":
        environment["CLAUDLOBBY_ROOT"] = str(installation.data_root.parent / "other-data")
    elif binding == "maintenance":
        (installation.data_root / "state/update.json").write_text(json.dumps(
            {"schema": 1, "phase": "publish", "maintenance": True}))
    result = subprocess.run([str(alias), "--json", "message", "receipt", "example"],
                            cwd=bot, env=constructed_env(**environment),
                            capture_output=True, text=True, timeout=10)
    if binding in {"foreign-root", "maintenance"}:
        assert result.returncode != 0
        expected_error = "root" if binding == "foreign-root" else "maintenance"
        assert expected_error in result.stderr
        assert not result.stdout and "Traceback" not in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output == {"installation": str(installation.directory), "root": str(installation.data_root),
                          "arguments": ["--root", str(installation.data_root), "--json", "message", "receipt", "example"]}
