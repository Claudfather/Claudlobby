"""#1265 boot-capture recorder — pytest wrapper for tests/test_boot_capture.sh.

CI runs pytest only, so a standalone bash test is not executed by CI at all.
This wrapper is what puts the first-observation contract in front of the gate.

Asserts the PASS/FAIL tally rather than rc alone. The harness exits 0 whenever
nothing failed, which includes the case where it failed to run any assertions —
so rc on its own would let a silently empty run masquerade as a pass.

Needs tmux, because the property under test is about a real session appearing
between two ticks and there is no way to fake that without faking the probe
itself — which would test the fake. Skipped, loudly, where tmux is absent; the
tally floor is what stops a skip from hiding inside a green run.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "test_boot_capture.sh"

# Raise this when cases are added; never lower it to make a red wrapper green.
MIN_ASSERTIONS = 39

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="boot-capture asserts on real tmux sessions appearing between ticks",
)


@pytest.fixture(scope="module")
def run() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HARNESS)], capture_output=True, text=True, timeout=600
    )


def _tally(stdout: str) -> tuple[int, int]:
    m = re.search(r"---- (\d+)/(\d+) passed, (\d+) failed ----", stdout)
    assert m, f"harness printed no tally:\n{stdout}"
    return int(m.group(2)), int(m.group(3))


def test_harness_ran_its_cases(run):
    """A run that asserted nothing also exits 0 — rc alone cannot see that."""
    total, _ = _tally(run.stdout)
    assert total >= MIN_ASSERTIONS, (
        f"harness ran {total} assertions, expected at least {MIN_ASSERTIONS}"
    )


def test_recorder_contract_holds(run):
    _, failed = _tally(run.stdout)
    assert failed == 0, run.stdout
    assert run.returncode == 0, run.stdout


def test_first_observation_survives_the_restart_that_rewrites_it(run):
    """The one property a single-pass recorder cannot have.

    .spawn is rewritten by every start-bot.sh run, so a keepalive restart ~60s
    in replaces a boot's value with a plausible one describing a different
    event. A recorder that reads at close reports the restart and looks right.
    """
    assert "PASS: the first observation survives a .spawn rewrite" in run.stdout
    assert "PASS: and it is not the post-restart value" in run.stdout


def test_shortfall_is_named_not_counted(run):
    """A recorder that quietly covers 12 of 21 reads as complete — and worse,
    reads as nine bots stranded."""
    assert "PASS: the unaccounted bot is named, not merely counted" in run.stdout


def test_ladder_end_is_derived_from_the_composed_rungs(run):
    """The 3s stagger is a composer constant that will move; a copy decays."""
    assert "PASS: ladder end is the max composed rung, not a constant" in run.stdout
    assert "PASS: a different rung moves the ladder end" in run.stdout


def test_an_empty_roster_refuses_instead_of_closing_the_boot(run):
    """Empty licenses a LOSS here, not a no-op (#1146): a boot closed against a
    zero denominator is a boot recorded as nothing having happened."""
    assert "PASS: exit 2 on an untrustworthy denominator" in run.stdout
    assert "PASS: the boot is NOT closed, so a later tick can still capture it" in run.stdout
