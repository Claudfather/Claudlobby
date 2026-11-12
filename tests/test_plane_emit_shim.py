"""Collect the hermetic native shim suite and its source-boundary checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _write_exec, constructed_env

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE = REPO_ROOT / "tests" / "test_plane_emit.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed"
)


def test_the_shim_ladder_suite_passes(test_cli):
    env = constructed_env(CLAUDLOBBY_CLI=test_cli)
    run = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True, text=True, timeout=600,
        cwd=str(REPO_ROOT), env=env,
    )
    tail = "\n".join((run.stdout + run.stderr).splitlines()[-40:])
    assert run.returncode == 0, f"rc={run.returncode}\n{tail}"
    # The suite prints this only after its last case; a bare rc 0 could also
    # mean it exited early.
    assert "PASS: plane-emit committed/pending/refusal paths" in run.stdout, tail
    # A passing run trips no ERR trap.
    assert "FAIL(" not in run.stderr, tail


@pytest.mark.parametrize("step, where", [
    pytest.param('chmod +x "$recorder"', "", id="top-level"),
    pytest.param("sleep 0.05", " in start_daemon, called from line {call}", id="in-a-function"),
])
def test_a_step_that_fails_outside_a_check_is_named(tmp_path, step, where):
    """A stub that fails without a word stands in for a step outside the suite's
    checks; the suite names that step's line and command."""
    lines = SUITE.read_text(encoding="utf-8").splitlines()
    line = next(n for n, text in enumerate(lines, 1) if step in text)
    call = next(n for n, text in enumerate(lines, 1) if text.startswith("start_daemon "))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _write_exec(stubs / step.split()[0], "#!/bin/sh\nexit 1\n")
    env = constructed_env(PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}")
    run = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True, text=True, timeout=60,
        cwd=str(REPO_ROOT), env=env,
    )
    expected = f"FAIL(line {line}{where.format(call=call)}): exit 1: {step}"
    assert run.returncode == 1, run.stdout + run.stderr
    assert expected in run.stderr.splitlines(), run.stdout + run.stderr


def test_daemon_down_stages_without_a_selected_cli(tmp_path):
    """A missing full CLI does not turn durable pending telemetry into loss."""
    root = tmp_path / "data"
    (root / "state" / "plane").mkdir(parents=True)
    env = constructed_env(
        CLAUDLOBBY_ROOT=root,
        CLAUDLOBBY_CLI=tmp_path / "missing-cli",
        PLANE_EMIT_DISABLED="0",
    )
    run = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "claudlobby/_runtime_scripts" / "plane-emit.sh")],
        input='{"events":[{"event_type":"system","emitter":"test",'
              '"payload":{"event":"daemon_started"}}]}',
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert run.returncode == 6, run.stderr
    assert len(list((root / "state" / "plane" / "staged").glob("*.batch"))) == 1
    assert "NOT in the plane" in run.stderr
    assert "missing-cli" not in run.stderr


@pytest.mark.parametrize("root", [None, "relative/data"])
def test_shim_refuses_missing_or_relative_data_root(root):
    env = constructed_env(PLANE_EMIT_DISABLED="0")
    if root is not None:
        env["CLAUDLOBBY_ROOT"] = root
    run = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "claudlobby/_runtime_scripts" / "plane-emit.sh")],
        input="{}", capture_output=True, text=True, timeout=10, env=env,
    )
    assert run.returncode == 127
    assert "CLAUDLOBBY_ROOT" in run.stderr
    assert run.stdout == ""
