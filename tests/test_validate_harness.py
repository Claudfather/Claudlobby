"""Run the empirical validation harness as part of the suite.

lib/validate-bot-change.sh stands up a throwaway bot + tmux sessions and asserts
that the observability/trust-loop behaviors (activity_stuck, overdue_dispatch,
manager notification) actually fire end-to-end. Gating it here means "the
behavior fires" stays under CI, not just "the config composes". Skips when tmux
isn't available (the harness needs it to back the observe step).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "lib" / "validate-bot-change.sh"


@pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux required for the observe step"
)
def test_validate_bot_change_harness():
    # F18 R1: every door in the harness records through the plane shim (the
    # cold-CLI rung, ~0.5-1s per emission, ~150 emissions a run), so the
    # harness takes minutes rather than seconds; the timeout is the measured
    # wall time with headroom, not a budget.
    result = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    # Surface the harness output on failure so the failing behavior is visible.
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    assert "activity_stuck event emitted" in result.stdout
    assert "overdue_dispatch event emitted" in result.stdout
    assert "manager notified" in result.stdout
    # #591 P1: the bridge-hijack scenario either runs or SKIPs with a printed
    # reason (bun/plugin absent) — it must never silently disappear.
    assert "bridge-hijack" in result.stdout
