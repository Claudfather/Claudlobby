"""The real hook must not silently disappear without GNU realpath.

Git commands below are payload data: no git operation is ever executed.
"""
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def hook(tmp_path):
    root = tmp_path.resolve()
    home, bins, cwd, temp = [root / name for name in ("home", "bin", "cwd", "tmp")]
    for path in (home, bins, cwd, temp):
        path.mkdir()
    realpath_log = root / "realpath-called"
    (bins / "realpath").write_text(
        "#!/bin/sh\n" + f"printf called >> {shlex.quote(str(realpath_log))}\n"
        + "printf 'realpath: illegal option -- m\\n' >&2\nexit 1\n")
    (bins / "realpath").chmod(0o755)
    (bins / "python3").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (bins / "python3").chmod(0o755)
    # A hook regression must not acquire effects if an error path changes.
    effects = root / "unexpected-effects"
    for name in ("git", "gh", "curl", "wget", "ssh", "tmux", "launchctl", "systemctl", "claude", "codex"):
        p = bins / name
        p.write_text("#!/bin/sh\n" + f"printf {shlex.quote(name)} >> {shlex.quote(str(effects))}\nexit 97\n")
        p.chmod(0o755)
    env = {"PATH": str(bins) + os.pathsep + os.environ["PATH"], "HOME": str(home),
           "TMPDIR": str(temp), "LANG": "C.UTF-8", "PLANE_EMIT_DISABLED": "1",
           "CLAUDLOBBY_ROOT": str(root), "PLANE_SOCKET": str(root / "absent.sock"),
           "XDG_CONFIG_HOME": str(home / ".config"), "TELEGRAM_STATE_DIR": str(home / "channel"),
           "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

    def run(vault, command, payload_cwd=None, tool="Bash", script=None):
        child = dict(env)
        if vault is not None:
            child["CLAUDRON_VAULT_PATH"] = str(vault)
        payload = {"tool_name": tool, "cwd": str(payload_cwd or cwd),
                   "tool_input": {"command": command}}
        result = subprocess.run(
            ["/bin/bash", str(script or REPO / "lib/vault-git-guard.sh")],
            input=json.dumps(payload), capture_output=True, text=True,
            env=child, cwd=cwd, timeout=15)
        assert result.returncode == 0, result.stderr
        assert not effects.exists(), effects.read_text() if effects.exists() else ""
        return (json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
                if result.stdout.strip() else None)

    return root, cwd, run, realpath_log


@pytest.mark.parametrize("form", ["absolute", "alias", "relative", "missing", "dash_relative"])
def test_vault_denial_works_without_gnu_realpath(hook, form):
    root, cwd, run, realpath_log = hook
    vault = root / "vault with spaces"
    vault.mkdir()
    (vault / ".git").mkdir()
    alias = root / "vault-alias"
    alias.symlink_to(vault, target_is_directory=True)
    declared = vault
    if form == "alias":
        declared = alias
    elif form == "relative":
        declared = "../vault-alias"
    elif form == "missing":
        vault = root / "not-created" / "vault"
        declared = vault
    elif form == "dash_relative":
        vault = cwd / "-vault"
        vault.mkdir()
        declared = "-vault"
    command = f"git -C {shlex.quote(str(vault))} reset --hard"
    assert run(declared, command, root) == "deny"
    assert not realpath_log.exists()


def test_allow_side_and_disabled_guard_remain_unchanged(hook):
    root, _, run, _ = hook
    vault = root / "vault"
    (vault / ".git").mkdir(parents=True)
    nested = vault / "projects" / "repo"
    (nested / ".git").mkdir(parents=True)
    assert run(vault, "git reset --hard", nested) is None
    assert run(vault, "git status", vault) is None
    assert run(None, "git reset --hard", vault) is None
    assert run("", "git reset --hard", vault) is None
    assert run(vault, "git reset --hard", vault, tool="Read") is None


def test_missing_decider_still_fails_open(hook):
    root, _, run, _ = hook
    # Same wrapper bytes; no decider. The dependency failure must not block
    # arbitrary project commands or turn an error into a fabricated denial.
    script = root / "missing-helper" / "vault-git-guard.sh"
    script.parent.mkdir()
    script.write_bytes((REPO / "lib/vault-git-guard.sh").read_bytes())
    assert run(root / "vault", "git reset --hard", script=script) is None
