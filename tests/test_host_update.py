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


def test_repo_pull_requires_declared_bot_and_selected_native(tmp_path, monkeypatch):
    from claudlobby import repo_pull_operations as repos

    root = tmp_path / "root"
    native = tmp_path / "selected/lib"
    native.mkdir(parents=True)
    (native / "git-pull-all.sh").write_text("# selected native fixture\n")
    bot_dir = root / "local/demo/runtime/bots/alex"
    (bot_dir / "projects").mkdir(parents=True)
    release = SimpleNamespace(release_id="r-selected", native_path=native,
                              cli_path=tmp_path / "selected/claudlobby")

    @contextmanager
    def admitted(selected_root, *, expected_release):
        assert selected_root == root and expected_release is None
        yield release

    paths = SimpleNamespace(lib=native, bot_runtime=lambda bot: bot_dir)
    context = SimpleNamespace(paths=paths, fleet=SimpleNamespace(name="demo", bots={"alex": object()}))
    monkeypatch.setattr(repos, "mutation_admission", admitted)
    monkeypatch.setattr(repos, "resolve_active_context", lambda **kwargs: context)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0,
            b"git-pull-all-v1\0one\0updated\0two\0unchanged\0", b"")

    monkeypatch.setattr(repos.subprocess, "run", run)
    result = repos.pull_repositories(root, "demo", "alex")
    assert result.repositories == (("one", "updated"), ("two", "unchanged"))
    argv, kwargs = calls.pop()
    assert argv == [str(native / "git-pull-all.sh"), str(bot_dir / "projects"), "--status-nul"]
    assert kwargs["env"]["CLAUDLOBBY_NATIVE_DIR"] == str(native)
    assert kwargs["env"]["FLEET_NAME"] == "demo"
    with pytest.raises(repos.RepositoryPullError, match="not declared"):
        repos.pull_repositories(root, "demo", "departed")
    assert calls == []


def test_repo_pull_public_gate_rejects_bot_origin_before_native_effect(monkeypatch, tmp_path):
    from claudlobby.commands import host_repos
    from claudlobby.command_result import CommandFailure

    monkeypatch.setenv("BOT_ID", "alex")
    args = SimpleNamespace(root=str(tmp_path), fleet="demo", bot="alex", seed=False)
    with pytest.raises(CommandFailure) as raised:
        host_repos.dispatch(args)
    assert raised.value.error.code == "conflict"


def test_repo_pull_discloses_partial_outcomes_without_claiming_all_updated(monkeypatch, tmp_path):
    from claudlobby import repo_pull_operations as repos
    from claudlobby.commands import host_repos
    from claudlobby.command_result import CommandFailure

    for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(repos, "pull_repositories", lambda *args: repos.RepositoryPullResult(
        "demo", "alex", "r-selected", (("one", "updated"), ("two", "skipped_dirty"))))
    args = SimpleNamespace(root=str(tmp_path), fleet="demo", bot="alex", seed=False)
    with pytest.raises(CommandFailure) as raised:
        host_repos.dispatch(args)
    assert raised.value.error.code == "conflict"
    assert raised.value.data["native_outcome"] == "partial"
    assert raised.value.data["repositories"] == [
        {"repository": "one", "status": "updated"},
        {"repository": "two", "status": "skipped_dirty"}]


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

    # Selected metadata from the real unit owner keeps the composed switch the
    # unit runs with (Pi 2026-10-01: =1 in the unit, missing from metadata), and
    # still no other environment value.
    import plistlib
    from claudlobby.config_units import unit_family
    from claudlobby.runtime_admission import wrap_unit_argv
    flag = "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"
    unit_env = {"CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_RELEASE_ID": "r-" + "a" * 64,
                "CLAUDLOBBY_CLI": str(tmp_path / "selected/bin/claudlobby"), flag: "1",
                "PRIVATE_TOKEN": "never-metadata"}
    argv = wrap_unit_argv(unit_env, unit="claudlobby-claude-update", phase="producers",
                          mode="oneshot", argv=[str(native / "update-claude-code.sh")])
    items = unit_family({"claudlobby-claude-update.plist": (plistlib.dumps({
        "Label": "claudlobby-claude-update", "WorkingDirectory": str(root),
        "EnvironmentVariables": unit_env, "ProgramArguments": list(argv)}), 0o644),
        "claudlobby-claude-update.service": (b"[Service]\n", 0o644)},
        destination=native, scope="host", phase="producers", release_id=unit_env["CLAUDLOBBY_RELEASE_ID"])
    selected = next(item for item in items if item["source"].endswith(".plist"))
    assert selected["environment"][flag] == "1" and "PRIVATE_TOKEN" not in selected["environment"]
    monkeypatch.setattr(updates, "selected_phase_entries", lambda *a: [selected])
    monkeypatch.setenv(flag, "1")
    ran = []
    monkeypatch.setattr(updates.subprocess, "run", lambda argv, **kwargs: (
        ran.append(kwargs["env"]) or subprocess.CompletedProcess(argv, 0, "", "")))
    updates.run_host_update(root, "runtime", scheduled=True)
    assert ran[-1][flag] == "1"


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
                          CLAUDLOBBY_NATIVE_DIR=str(REPO / "claudlobby/_runtime_scripts"), PLANE_EMIT_DISABLED="1")
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert called.read_text().splitlines() == ["--root", str(root), "host", "update", command]
    called.unlink()
    env.pop("CLAUDLOBBY_CLI")
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not called.exists()
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script),
                             "--selected-release", "wrong-release"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert not called.exists()
