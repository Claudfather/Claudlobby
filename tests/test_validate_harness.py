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


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required for the observe step")
def test_validate_bot_change_harness():
    result = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Surface the harness output on failure so the failing behavior is visible.
    assert result.returncode == 0, f"harness failed:\n{result.stdout}\n{result.stderr}"
    assert "activity_stuck event emitted" in result.stdout
    assert "overdue_dispatch event emitted" in result.stdout
    assert "manager notified" in result.stdout
