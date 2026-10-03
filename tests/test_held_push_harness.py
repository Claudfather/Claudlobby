"""#2120 real-tmux gate: pytest wrapper for harness/rehearse-held-push.sh.

A FLEET-PULSE push the manager's box did not take keeps its alert's debounce
window open, so the next sweep pages again; a submitted push closes it; and a
box that takes no input costs one push wait per sweep, not one per alert. It
drives the real `fleet-pulse.sh` against a throwaway worker with two real
unresolved conditions, and a real manager pane whose box takes no input, then
one whose box does.

Follows tests/test_debounce_recipient_harness.py: assert rc 0 AND the scenario
markers, because the harness exits 0 when tmux is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "harness" / "rehearse-held-push.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)


@pytest.fixture(scope="module")
def run():
    return subprocess.run(
        ["bash", str(HARNESS)], capture_output=True, text=True, timeout=600
    )


def test_harness_ran_rather_than_skipping(run):
    """rc 0 is not enough: the tmux-absent path also exits 0."""
    assert "SKIP:" not in run.stdout, run.stdout
    assert "a submitted push closes session_missing's window" in run.stdout, run.stdout


def test_a_held_push_keeps_the_window_open_and_the_next_sweep_pages_again(run):
    """Red on the pre-fix code, which counted the held push as delivered."""
    for label in (
        "a held push leaves session_missing's window open",
        "a held push leaves service_down's window open",
        "the next sweep pushes again",
    ):
        assert f"PASS: {label}" in run.stdout, run.stdout


def test_a_held_box_costs_one_push_wait_per_sweep(run):
    """Red on the pre-fix code, which waited out the budget once per alert."""
    assert "PASS: one push waits on the held box, not one per alert" in run.stdout, run.stdout


def test_a_submitted_push_closes_the_window_and_still_debounces(run):
    """Holds on the pre-fix code too: the change must not stop a delivered push
    from buying its window."""
    for label in (
        "session_missing is pushed and submitted",
        "a submitted push closes session_missing's window",
        "a submitted push closes service_down's window",
        "no second session_missing push",
        "no second service_down push",
    ):
        assert f"PASS: {label}" in run.stdout, run.stdout


def test_every_check_passes(run):
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "0 failed" in run.stdout, run.stdout
