"""#831 real-tmux gate — pytest wrapper for harness/rehearse-debounce-recipient.sh.

Unit tests prove the marker re-fires; only this proves the *notification*
arrives. It drives the real `fleet-pulse.sh` against a throwaway bot with a real
unresolved condition, restarts a real manager tmux session, and reads the real
pane — the property the 2026-07-27 outage turned on.

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


@pytest.fixture(scope="module")
def run():
    r = subprocess.run(
        ["bash", str(HARNESS)], capture_output=True, text=True, timeout=600
    )
    return r


def test_harness_ran_rather_than_skipping(run):
    """rc 0 is not enough: the tmux-absent path also exits 0."""
    assert "SKIP:" not in run.stdout, run.stdout
    assert "restarted manager receives session_missing" in run.stdout, run.stdout


def test_alert_survives_a_manager_restart(run):
    """The property. Red on pre-fix code, where the restarted manager gets 0."""
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "0 failed" in run.stdout, run.stdout


def test_debounce_still_debounces(run):
    """The fix must not turn a debounced alert into a per-tick alarm."""
    assert "FAIL: 3 ticks" not in run.stdout, run.stdout


def test_the_rehearsal_holds_when_its_manager_is_slow_to_draw_its_box():
    """#2136: CI runners sometimes start the manager's stand-in after the first push.
    The keys it missed are echoed by the tty and discarded when it enters raw mode,
    so neither alert of that tick is submitted, and the debounce then keeps them
    from being pushed again. The seam delays the stand-in's start, to force the
    race: the rehearsal must wait for the box before the first pulse."""
    r = subprocess.run(["bash", str(HARNESS)], capture_output=True, text=True, timeout=600,
                       env={**os.environ, "REHEARSAL_MANAGER_START_DELAY": "3"})
    assert "SKIP:" not in r.stdout, r.stdout
    assert r.returncode == 0 and "0 failed" in r.stdout, f"{r.stdout}\n{r.stderr}"
