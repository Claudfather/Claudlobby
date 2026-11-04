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


@pytest.mark.parametrize("phase, maintenance", [("quiesce", False), ("git", True)])
def test_native_entry_is_fenced_on_reboot_before_source_or_environment_access(native, phase, maintenance):
    installation, marker, _, _, run, _ = native
    (installation.data_root / "state/update.json").write_text(json.dumps({
        "schema": 1, "phase": phase, "maintenance": maintenance}))
    import shutil
    shutil.rmtree(installation.source)
    outcome = run()
    assert outcome.returncode != 0 and not marker.exists()
    assert "maintenance" in outcome.stderr or "quiesce" in outcome.stderr
    assert "Traceback" not in outcome.stderr


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
