"""Selected reload refreshes plugins and marks live sessions without composing units."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parent.parent


def _script_host(tmp_path: Path):
    root = tmp_path / "data"
    native = tmp_path / "native"
    native.mkdir()
    root.mkdir()
    for name in ("reload-fleet.sh", "lib-common.sh", "cli-context.sh", "supervisor.sh"):
        target = native / name
        target.write_bytes((REPO / "lib" / name).read_bytes())
        target.chmod(0o755)
    (native / "check-npx-cache.sh").write_text("#!/bin/bash\nexit 0\n")
    (native / "check-npx-cache.sh").chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    calls = tmp_path / "calls"
    (tools / "claude").write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\n')
    (tools / "claude").chmod(0o755)
    (tools / "tmux").write_text('#!/bin/bash\ncase "$*" in *"-t lead"*) exit 0;; esac\nexit 1\n')
    (tools / "tmux").chmod(0o755)
    (tools / "claudlobby").write_text(f'#!/bin/bash\nprintf "CLI %s\\n" "$*" >> "{calls}"\nexit 2\n')
    (tools / "claudlobby").chmod(0o755)
    bots = root / "local" / "demo" / "runtime" / "bots"
    for bot in ("lead", "worker", "residue"):
        directory = bots / bot
        directory.mkdir(parents=True)
        (directory / "bot.conf").write_text(f'export BOT_SERVICE="demo-{bot}"\n')
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PATH": f'{tools}:{os.environ["PATH"]}',
           "CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_NATIVE_DIR": str(native),
           "CLAUDLOBBY_CLI": str(tools / "claudlobby"), "CLAUDLOBBY_RELEASE_ID": "release-one",
           "CLAUDLOBBY_FLEET": "demo", "CLAUDE_BIN": str(tools / "claude"),
           "CLAUDLOBBY_NATIVE_PYTHON": str(Path(os.sys.executable)),
           "PLANE_EMIT_DISABLED": "1", "TMUX_TMPDIR": str(tmp_path / "tmux")}
    args = (str(native / "reload-fleet.sh"), "--selected-release", "release-one", "--fleet", "demo",
            "--bots-dir", str(bots), "--plugin", "claudna@Claudfather", "--bot", "lead",
            "--bot", "worker")
    return root, bots, calls, env, args


@pytest.mark.parametrize("with_plugin", [True, False])
def test_selected_reload_only_marks_running_selected_bot(tmp_path, with_plugin):
    root, bots, calls, env, args = _script_host(tmp_path)
    if not with_plugin:
        # Live macOS canary: Bash 3.2 nounset rejects an empty array expansion.
        index = args.index("--plugin")
        args = args[:index] + args[index + 2:]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + (root / "state/reload-fleet.log").read_text()
    assert result.stdout.splitlines() == ["marked\tlead"] + (
        ["refreshed\tclaudna@Claudfather"] if with_plugin else [])
    assert (calls.read_text().splitlines() if calls.exists() else []) == (
        ["plugin update claudna@Claudfather"] if with_plugin else [])
    assert (bots / "lead/data/.reload-pending").exists()
    assert not (bots / "worker/data/.reload-pending").exists()
    assert not (bots / "residue/data/.reload-pending").exists()
    assert "plugin refresh OK" in (root / "state/reload-fleet.log").read_text()


def test_timer_entry_dispatches_selected_public_command_only(tmp_path):
    root, bots, calls, env, args = _script_host(tmp_path)
    result = subprocess.run((args[0], "demo"), env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2  # the fake CLI exits 2 after recording argv
    assert calls.read_text().splitlines() == [f"CLI --root {root} --fleet demo fleet reload"]
    assert not (root / "state/reload-fleet.log").exists()
    assert not (bots / "lead/data/.reload-pending").exists()


@pytest.mark.parametrize("change", [{"CLAUDLOBBY_RELEASE_ID": "wrong"},
                                     {"CLAUDLOBBY_FLEET": "other"}])
def test_reload_refuses_mismatched_selected_context_before_effect(tmp_path, change):
    root, bots, calls, env, args = _script_host(tmp_path)
    result = subprocess.run(args, env={**env, **change}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 2
    assert not calls.exists()
    assert not (bots / "lead/data/.reload-pending").exists()
    assert not (root / "state/reload-fleet.log").exists()
