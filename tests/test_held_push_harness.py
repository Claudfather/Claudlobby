"""#2120 real-tmux gate: pytest wrapper for harness/rehearse-held-push.sh.

A FLEET-PULSE push never types into a manager's box that holds text, and the
alert is still recorded for the escalation; a push the box did not take keeps
its alert's window open; a box that takes no input costs one wait, then a floor
with no wait and no typing; once the floor lapses and the box takes input, the
push goes out, closes the window and clears the floor. The floor is the manager
instance's, so a restarted manager is not held back by it. It drives the real
`fleet-pulse.sh` against two throwaway workers, one real unresolved condition
each, and a real manager pane: one holding text, one that takes no input, one that
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
            "a held push leaves unit_missing's window open",
            "no push waits within the floor",
            "both alerts are held back by the floor",
            "the alert is still recorded within the floor",
            "session_missing's window stays open within the floor")


def test_the_floor_lapses_and_the_push_goes_out_once_the_box_takes_input(run):
    _passed(run, "session_missing is pushed and submitted",
            "unit_missing is pushed and submitted",
            "a submitted push closes session_missing's window",
            "a submitted push closes unit_missing's window",
            "a submitted push clears the floor",
            "no second session_missing push",
            "no second unit_missing push")


def test_a_restarted_manager_is_not_held_back_by_its_predecessors_floor(run):
    """The floor is the manager instance's (#831's recipient token): a restart is a new
    box, and the alerts that re-fire to it go out."""
    _passed(run, "a second deaf manager is up and shows its box",
            "the alerts re-fire to it, and one push waits on its box",
            "its other alert is held back by its floor",
            "a manager that takes input replaces it and shows its box",
            "it reuses its predecessor's session id, so a floor keyed by the id would hold it back",
            "the new manager is not held back by its predecessor's floor",
            "session_missing reaches the new manager",
            "unit_missing reaches the new manager")


def test_a_floor_marker_dated_ahead_of_the_clock_holds_nothing_back(run):
    """A host that boots behind real time reads an older marker as dated ahead;
    plane-emit.sh treats a negative age as expired, and so must the floor."""
    _passed(run, "the planted marker is dated a day ahead",
            "a marker dated ahead holds nothing back",
            "session_missing is pushed again",
            "unit_missing is pushed again")


def test_a_restarted_manager_whose_box_draws_late_gets_the_alert_in_the_same_sweep(run):
    """#2138 (ravi): the first tick after a restart can come before the box is drawn,
    and keys typed then are lost. The push waits for the box, so the alert lands."""
    _passed(run, "the new manager's pane is still blank when the sweep starts",
            "session_missing reaches the late manager in the same sweep",
            "unit_missing reaches it in the same sweep",
            "no push to it waited out the shown budget")


def test_every_check_passes(run):
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    assert "0 failed" in run.stdout, run.stdout
