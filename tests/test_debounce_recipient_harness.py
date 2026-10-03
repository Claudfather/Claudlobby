"""#831 real-tmux gate — pytest wrapper for harness/rehearse-debounce-recipient.sh.

Unit tests prove the marker re-fires; only this proves the *notification*
arrives. It drives the real `fleet-pulse.sh` against a throwaway bot with a real
unresolved condition, restarts a real manager tmux session, and counts what that
session was submitted — the property the 2026-07-27 outage turned on.

Not opt-in: the only dependency is tmux, which per-PR CI already installs for
the move-bot integration tests. Follows tests/test_validate_harness.py — assert
rc 0 AND that the scenario markers appear, because the harness exits 0 when tmux
is absent, so rc alone would let a silent skip masquerade as a pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "harness" / "rehearse-debounce-recipient.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytest.fixture(scope="module", params=["", "3"], ids=["prompt-start", "slow-start"])
def run(request):
    """Every test runs on both arms. The slow arm starts each manager's stand-in
    3 s late, so a pulse sent at once would reach its pane before the box (#2136)."""
    r = subprocess.run(
        ["bash", str(HARNESS)], capture_output=True, text=True, timeout=300,
        env={**os.environ, "REHEARSE_MANAGER_START_DELAY": request.param},
    )
    r.delay = request.param
    return r


def test_harness_ran_rather_than_skipping(run):
    """rc 0 is not enough: the tmux-absent path also exits 0. And only an arm
    that ran slow covers the late start."""
    assert "SKIP:" not in run.stdout, run.stdout
    assert "restarted manager receives session_missing" in run.stdout, run.stdout
    assert ("starts 3 s late" in run.stdout) == bool(run.delay), run.stdout


def test_alert_survives_a_manager_restart(run):
    """The property. Red on pre-fix code, where the restarted manager gets 0."""
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "0 failed" in run.stdout, run.stdout


def test_debounce_still_debounces(run):
    """The fix must not turn a debounced alert into a per-tick alarm."""
    assert "FAIL: 3 ticks" not in run.stdout, run.stdout
