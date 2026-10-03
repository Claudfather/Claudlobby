"""#2120 real-tmux gate: pytest wrapper for harness/rehearse-held-push.sh.

A FLEET-PULSE push never types into a manager's box that holds text, and the
alert is still recorded for the escalation; a push the box did not take keeps
its alert's window open; a box that takes no input costs one wait, then a floor
with no wait and no typing; once the floor lapses and the box takes input, the
push goes out, closes the window and clears the floor. It drives the real
`fleet-pulse.sh` against a throwaway worker with two real unresolved conditions
and a real manager pane: one holding text, one that takes no input, one that
does.

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


def _passed(run, *labels):
    for label in labels:
        assert f"PASS: {label}" in run.stdout, run.stdout


def test_a_held_box_gets_no_copy_and_no_enter_and_the_alert_is_still_recorded(run):
    """dara's first rule: never type into a box that already holds text."""
    _passed(run, "the box holds the manager's own text",
            "a held box gets no copy of an alert",
            "a held box gets no Enter: its text is still in it",
            "both alerts are skipped, not typed",
            "a skipped push leaves session_missing's window open",
            "the alert is still recorded for the escalation")


def test_a_box_that_takes_no_input_costs_one_wait_then_the_floor(run):
    """dara's second rule: after a push returns 3, no push to that manager until the
    floor lapses, with no wait and no typing in between."""
    _passed(run, "one push waits on the box that takes no input",
            "the other alert is held back by the floor, not typed",
            "a held push leaves session_missing's window open",
            "a held push leaves service_down's window open",
            "no push waits within the floor",
            "both alerts are held back by the floor",
            "the alert is still recorded within the floor",
            "session_missing's window stays open within the floor")


def test_the_floor_lapses_and_the_push_goes_out_once_the_box_takes_input(run):
    _passed(run, "session_missing is pushed and submitted",
            "service_down is pushed and submitted",
            "a submitted push closes session_missing's window",
            "a submitted push closes service_down's window",
            "a submitted push clears the floor",
            "no second session_missing push",
            "no second service_down push")


def test_every_check_passes(run):
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "0 failed" in run.stdout, run.stdout
