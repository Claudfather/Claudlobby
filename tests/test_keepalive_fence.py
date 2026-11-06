"""The real keepalive sweep pauses quietly under an update's fence and still fails loudly on any other refusal."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.conftest import constructed_env
from tests.test_git_native_entry import native, native_shell  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts"


@pytest.fixture
def sweep(native, native_shell, tmp_path):  # noqa: F811
    installation, _, snapshot, save, _, _ = native
    _, directory, environment, _ = native_shell
    shutil.copytree(SCRIPTS, installation.native, dirs_exist_ok=True)
    configuration = directory / "bot.conf"
    configuration.write_text("export BOT_SERVICE=example.worker\nexport FLEET_NAME=example\n"
                             f"export BOT_DIR={directory}\n")
    configuration.chmod(0o600)
    snapshot["owned_outputs"][0]["state"]["sha256"] = hashlib.sha256(configuration.read_bytes()).hexdigest()
    save()
    home = tmp_path / "private-home"
    for unit in (home / ".config/systemd/user/example.worker.service",
                 home / "Library/LaunchAgents/example.worker.plist"):
        unit.parent.mkdir(parents=True)
        unit.write_text("stub\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "stub-calls.log"
    for name in ("tmux", "curl", "claude", "systemctl"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\nexit 1\n')
        stub.chmod(0o755)
    env = constructed_env(**{**environment, "HOME": home, "PATH": f"{stubs}:/usr/bin:/bin"})
    for key in ("BOT_DIR", "BOT_SERVICE", "FLEET_NAME", "LIB_DIR"):
        env.pop(key, None)
    def run():
        return subprocess.run(["/bin/bash", str(installation.native / "keepalive-all.sh"),
                               str(directory.parent)], env=env, cwd=installation.data_root,
                              capture_output=True, text=True, timeout=60)
    log = installation.data_root / "state/logs/keepalive-all.log"
    return installation, run, log, calls


def _update(installation, phase, maintenance):
    (installation.data_root / "state/update.json").write_text(json.dumps({
        "schema": 1, "phase": phase, "maintenance": maintenance}))


def test_sweep_caught_by_the_fence_pauses_without_a_failure_or_alert(sweep):
    installation, run, log, calls = sweep
    _update(installation, "quiesce", False)
    outcome = run()
    assert outcome.returncode == 0, outcome.stderr
    text = log.read_text()
    assert "PAUSED" in text and "worker" in text and "ERROR" not in text
    assert "keepalive admission/runtime failed" not in outcome.stderr
    assert not list((installation.data_root / "state").glob("keepalive-admission-*"))
    assert not calls.exists()


def test_sweep_refused_by_maintenance_fails_and_alerts(sweep):
    installation, run, log, calls = sweep
    _update(installation, "git", True)
    outcome = run()
    assert outcome.returncode != 0
    text = log.read_text()
    assert "ERROR — keepalive.sh failed for worker (exit 6)" in text and "PAUSED" not in text
    assert "keepalive admission/runtime failed for worker" in outcome.stderr
    assert list((installation.data_root / "state").glob("keepalive-admission-*"))
    assert not calls.exists()  # the alert path reached no tmux, curl, claude or systemctl
