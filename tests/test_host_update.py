"""Selected host update routing; native updater behavior remains in its owner."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env, _write_exec


REPO = Path(__file__).resolve().parents[1]


def test_operator_update_uses_selected_native_and_host_switch(tmp_path, monkeypatch):
    from claudlobby import host_update_operations as updates

    root = tmp_path / "root"
    native = tmp_path / "selected" / "lib"
    release = SimpleNamespace(release_id="r-selected", native_path=native,
                              cli_path=tmp_path / "selected" / "claudlobby")

    @contextmanager
    def admitted(root_arg, *, expected_release):
        assert root_arg == root and expected_release is None
        yield release

    monkeypatch.setattr(updates, "mutation_admission", admitted)
    monkeypatch.setattr(updates, "resolve_paths", lambda **kwargs: SimpleNamespace(lib=native))
    monkeypatch.setattr(updates, "resolve_env_tiers", lambda paths: {
        "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED": SimpleNamespace(value="0")})
    monkeypatch.setenv("CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED", "1")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(updates.subprocess, "run", run)
    result = updates.run_host_update(root, "runtime")
    assert result.outcome == "tick_completed"
    argv, kwargs = calls.pop()
    assert argv == [str(native / "update-claude-code.sh"), "--selected-release", "r-selected"]
    assert kwargs["env"]["CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"] == "0"
    assert kwargs["env"]["CLAUDLOBBY_CLI"] == str(release.cli_path)
    assert kwargs["timeout"] == 3600

    updates.run_host_update(root, "siblings", dry_run=True)
    argv, kwargs = calls.pop()
    assert argv == [str(native / "update-siblings.sh"), "--selected-release", "r-selected",
                    "--dry-run"]
    assert "CLAUDLOBBY_FLEET" not in kwargs["env"]


def test_scheduled_siblings_require_effective_and_selected_enrollment(tmp_path, monkeypatch):
    from claudlobby import host_update_operations as updates

    root = tmp_path / "root"
    native = tmp_path / "selected" / "lib"
    release = SimpleNamespace(release_id="r-selected", native_path=native,
                              cli_path=tmp_path / "selected" / "claudlobby")

    @contextmanager
    def admitted(*args, **kwargs):
        yield release

    monkeypatch.setattr(updates, "mutation_admission", admitted)
    monkeypatch.setattr(updates, "resolve_paths", lambda **kwargs: SimpleNamespace(lib=native))
    monkeypatch.setattr(updates, "selected_phase_entries", lambda *a: [])
    monkeypatch.setattr(updates.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("updater ran without selected enrollment")))
    monkeypatch.setattr(updates, "load_host_jobs", lambda: {"update-siblings": {"enroll": False}})
    with pytest.raises(updates.HostUpdateError, match="not enrolled"):
        updates.run_host_update(root, "siblings", scheduled=True)
    monkeypatch.setattr(updates, "load_host_jobs", lambda: {"update-siblings": {"enroll": True}})
    with pytest.raises(updates.HostUpdateError, match="no selected timer"):
        updates.run_host_update(root, "siblings", scheduled=True)

    monkeypatch.setattr(updates, "load_host_jobs", lambda: {"claude-update": {"enroll": True}})
    monkeypatch.setattr(updates, "selected_phase_entries", lambda *a: [{
        "source": str(native / "claudlobby-claude-update.plist"),
        "environment": {"CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED": "1"}}])
    monkeypatch.setenv("CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED", "0")
    with pytest.raises(updates.HostUpdateError, match="switch differs"):
        updates.run_host_update(root, "runtime", scheduled=True)


@pytest.mark.parametrize(("script", "command"), [
    ("update-claude-code.sh", "runtime"), ("update-siblings.sh", "siblings")])
def test_selected_timer_enters_public_owner_before_any_update(tmp_path, script, command):
    called = tmp_path / "called"
    cli = tmp_path / "claudlobby"
    _write_exec(cli, f'#!/bin/bash\nprintf "%s\\n" "$@" > "{called}"\n')
    root = tmp_path / "root"
    root.mkdir()
    env = constructed_env(HOME=str(tmp_path), CLAUDLOBBY_ROOT=str(root),
                          CLAUDLOBBY_RELEASE_ID="r-selected", CLAUDLOBBY_CLI=str(cli),
                          CLAUDLOBBY_NATIVE_DIR=str(REPO / "lib"), PLANE_EMIT_DISABLED="1")
    result = subprocess.run(["bash", str(REPO / "lib" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert called.read_text().splitlines() == ["--root", str(root), "host", "update", command]
    called.unlink()
    env.pop("CLAUDLOBBY_CLI")
    result = subprocess.run(["bash", str(REPO / "lib" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not called.exists()
    result = subprocess.run(["bash", str(REPO / "lib" / script),
                             "--selected-release", "wrong-release"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not called.exists()
