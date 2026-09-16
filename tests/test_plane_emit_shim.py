"""#1485 fold — pytest wrapper for tests/test_plane_emit.sh.

That bash suite is the ONLY pin on the ladder itself: which refusals fall
back, which pass through, and that a replayed batch carries the id the daemon
already saw. Nothing ran it. pytest collects `tests/test_*.py`, so a `.sh`
sibling is invisible to CI and to the local before/after recipe in CLAUDE.md —
the #1485 shim change shipped with two new cases in a file no gate opened.
Precedent for wrapping a shell gate this way: tests/test_debounce_recipient_harness.py.

The suite is hermetic (fake unix-socket daemon, `PLANE_EMIT_CLI` recorder
stub, no network, no claudlobby import), so this is a plain rc assertion plus
the suite's own completion marker — rc 0 alone would let an early `exit 0`
masquerade as a pass. `.venv/bin` leads PATH so that any path which falls
through to lib-common's `claudlobby_cli` resolves THIS checkout rather than
an ambient install.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE = REPO_ROOT / "tests" / "test_plane_emit.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not installed"
)


def test_the_shim_ladder_suite_passes():
    env = dict(os.environ)
    venv_bin = REPO_ROOT / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
    run = subprocess.run(
        ["bash", str(SUITE)],
        capture_output=True, text=True, timeout=600,
        cwd=str(REPO_ROOT), env=env,
    )
    tail = "\n".join((run.stdout + run.stderr).splitlines()[-40:])
    assert run.returncode == 0, f"rc={run.returncode}\n{tail}"
    # The suite prints this only after its last case; a bare rc 0 could also
    # mean it exited early.
    assert "PASS: all plane-emit shim tests passed" in run.stdout, tail
