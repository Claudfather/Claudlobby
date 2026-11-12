"""Git host update routing over current config; child effects remain synthetic."""
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

from tests.conftest import constructed_env, _write_exec
from tests.fixtures.current_runtime import current_runtime, tmp_path  # noqa: F401

REPO = Path(__file__).resolve().parents[1]


def test_operator_update_uses_current_native_and_host_switch(current_runtime, monkeypatch):
    from claudlobby import host_update_operations as updates
    root, installation, _, host = current_runtime
    monkeypatch.setattr(updates, "resolve_env_tiers", lambda paths: {
        "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED": SimpleNamespace(value="0")})
    monkeypatch.setenv("CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED", "1")
    calls = []
    original = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "git": return original(argv, **kwargs)
        assert argv[0] in {str(installation.native / "update-claude-code.sh"),
                           str(installation.native / "update-siblings.sh")}
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(updates.subprocess, "run", run)
    result = updates.run_host_update(root, "runtime")
    assert result.outcome == "tick_completed" and result.release_id is None
    assert result.source_revision == host.snapshot["revision"]
    assert result.config_fingerprint == host.snapshot["fingerprint"]
    argv, kwargs = calls.pop()
    assert argv == [str(installation.native / "update-claude-code.sh"), "--admitted-runtime"]
    assert kwargs["env"]["CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"] == "0"
    assert kwargs["env"]["CLAUDLOBBY_CLI"] == str(installation.cli)
    assert kwargs["env"]["CLAUDLOBBY_INSTALLATION"] == str(installation.directory)
    assert "CLAUDLOBBY_RELEASE_ID" not in kwargs["env"] and kwargs["timeout"] == 3600
    updates.run_host_update(root, "siblings", dry_run=True)
    argv, kwargs = calls.pop()
    assert argv == [str(installation.native / "update-siblings.sh"), "--admitted-runtime", "--dry-run"]
    assert "CLAUDLOBBY_FLEET" not in kwargs["env"]


def test_repo_pull_requires_declared_bot_and_current_native(current_runtime, monkeypatch):
    from claudlobby import repo_pull_operations as repos
    from claudlobby.context import BotNotFoundError
    root, installation, _, host = current_runtime
    bot_dir = root / "runtime/bots/manager"
    calls = []
    original = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "git": return original(argv, **kwargs)
        assert argv[0] == str(installation.native / "git-pull-all.sh")
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0,
            b"git-pull-all-v1\0one\0updated\0two\0unchanged\0", b"")
    monkeypatch.setattr(repos.subprocess, "run", run)
    result = repos.pull_repositories(root, "example", "manager")
    assert result.repositories == (("one", "updated"), ("two", "unchanged"))
    assert result.release_id is None and result.source_revision == host.snapshot["revision"]
    argv, kwargs = calls.pop()
    assert argv == [str(installation.native / "git-pull-all.sh"), str(bot_dir / "projects"), "--status-nul"]
    assert kwargs["env"]["CLAUDLOBBY_NATIVE_DIR"] == str(installation.native)
    assert kwargs["env"]["FLEET_NAME"] == "example"
    with pytest.raises(BotNotFoundError, match="not in the active fleet"):
        repos.pull_repositories(root, "example", "departed")
    assert calls == []


def test_repo_pull_public_gate_rejects_bot_origin_before_native_effect(monkeypatch, tmp_path):
    from claudlobby.commands import host_repos
    from claudlobby.command_result import CommandFailure
    monkeypatch.setenv("BOT_ID", "alex")
    args = SimpleNamespace(root=str(tmp_path), fleet="demo", bot="alex", seed=False)
    with pytest.raises(CommandFailure) as raised: host_repos.dispatch(args)
    assert raised.value.error.code == "conflict"


def test_repo_pull_discloses_partial_outcomes_without_claiming_all_updated(monkeypatch, tmp_path):
    from claudlobby import repo_pull_operations as repos
    from claudlobby.commands import host_repos
    from claudlobby.command_result import CommandFailure
    for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(repos, "pull_repositories", lambda *args: repos.RepositoryPullResult(
        "demo", "alex", None, (("one", "updated"), ("two", "skipped_dirty")),
        source_revision="a" * 40, config_fingerprint="b" * 64))
    args = SimpleNamespace(root=str(tmp_path), fleet="demo", bot="alex", seed=False)
    with pytest.raises(CommandFailure) as raised: host_repos.dispatch(args)
    assert raised.value.error.code == "conflict" and raised.value.data["native_outcome"] == "partial"
    assert raised.value.data["repositories"] == [
        {"repository": "one", "status": "updated"}, {"repository": "two", "status": "skipped_dirty"}]


def test_scheduled_siblings_require_committed_job_before_effects(current_runtime, monkeypatch):
    from claudlobby import host_update_operations as updates, host_job_operations as jobs
    root, _, _, host = current_runtime
    # The committed fixture has bots but no host timer. Pending authoring cannot enroll one.
    (root / "host-system.yaml").write_text("host: {jobs: {update-siblings: {enroll: true}}}\n")
    class Native:
        package = host.package
        def read(self, function):
            assert function == "svc_inventory_catalog"
            return f"manager\tLinux\ndirectory\t{host.directory}\n"
    monkeypatch.setattr(jobs, "Adapter", Native)
    original = subprocess.run
    def refuse_child(argv, **kwargs):
        if argv[0] == "git": return original(argv, **kwargs)
        pytest.fail("updater ran without committed enrollment")
    monkeypatch.setattr(updates.subprocess, "run", refuse_child)
    with pytest.raises(updates.HostUpdateError, match="no verified committed job") as raised:
        updates.run_host_update(root, "siblings", scheduled=True)
    assert not raised.value.effect_attempted


@pytest.mark.parametrize("committed_switch", ["1", "0", ""])
def test_scheduled_host_update_uses_shared_binding_and_exact_committed_switch(
        current_runtime, monkeypatch, committed_switch):
    from claudlobby import host_update_operations as updates
    root, installation, _, host = current_runtime
    flag = "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"
    bindings, calls = [], []
    def bind(current, name):
        assert current.installation == installation and current.snapshot == host.snapshot
        bindings.append(name)
        return None, SimpleNamespace(environment=((flag, committed_switch),)), "exact-target", "exact-file"
    monkeypatch.setattr(updates, "_bind_git_job", bind)
    original = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == "git": return original(argv, **kwargs)
        assert argv[0] in {str(installation.native / "update-claude-code.sh"),
                           str(installation.native / "update-siblings.sh")}
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setenv(flag, "wrong")
    with pytest.raises(updates.HostUpdateError, match="switch differs"):
        updates.run_host_update(root, "runtime", scheduled=True)
    assert not calls
    monkeypatch.setenv(flag, committed_switch)
    result = updates.run_host_update(root, "runtime", scheduled=True)
    assert result.release_id is None and result.source_revision == host.snapshot["revision"]
    assert result.config_fingerprint == host.snapshot["fingerprint"]
    assert calls[0][0] == [str(installation.native / "update-claude-code.sh"), "--admitted-runtime"]
    assert calls[0][1][flag] == committed_switch and calls[0][1]["CLAUDLOBBY_NATIVE_ADMITTED"] == "1"
    assert "CLAUDLOBBY_RELEASE_ID" not in calls[0][1]
    assert bindings == ["claude-update", "claude-update"]
    updates.run_host_update(root, "siblings", dry_run=True, scheduled=True)
    assert bindings[-1] == "update-siblings"
    assert calls[-1][0] == [str(installation.native / "update-siblings.sh"), "--admitted-runtime", "--dry-run"]


@pytest.mark.parametrize(("script", "command"), [
    ("update-claude-code.sh", "runtime"), ("update-siblings.sh", "siblings")])
def test_current_timer_enters_public_owner_before_any_update(tmp_path, script, command):
    called, cli, root = tmp_path / "called", tmp_path / "claudlobby", tmp_path / "root"
    _write_exec(cli, f'#!/bin/bash\nprintf "%s\\n" "$@" > "{called}"\n')
    root.mkdir()
    env = constructed_env(HOME=str(tmp_path), CLAUDLOBBY_ROOT=str(root),
        CLAUDLOBBY_INSTALLATION=str(tmp_path / "installation"), CLAUDLOBBY_CLI=str(cli),
        CLAUDLOBBY_NATIVE_DIR=str(REPO / "claudlobby/_runtime_scripts"), PLANE_EMIT_DISABLED="1")
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert called.read_text().splitlines() == ["--root", str(root), "host", "update", command]
    called.unlink()
    env.pop("CLAUDLOBBY_CLI")
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and not called.exists()
    result = subprocess.run(["bash", str(REPO / "claudlobby/_runtime_scripts" / script),
        "--selected-release", "wrong-release"], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and not called.exists()
