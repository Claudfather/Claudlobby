"""#1043 self-start snapshot — pytest wrapper for tests/test_selfstart_snapshot.sh.

CI runs pytest only, so a standalone bash test is not executed by CI at all.
This wrapper is what puts the classification contract in front of the gate.

Asserts the PASS/FAIL tally rather than rc alone. The harness exits 0 whenever
nothing failed, which includes the case where it failed to run any assertions —
so rc on its own would let a silently empty run masquerade as a pass. The floor
below is a floor, not an equality: adding cases must not turn the wrapper red,
but dropping them must.

No opt-in gate and no external dependency: the harness needs only bash, awk,
grep and coreutils, and it builds its own scratch CLAUDLOBBY_ROOT and
CLAUDE_CONFIG_DIR, so it can neither read the real estate nor be perturbed by
one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "tests" / "test_selfstart_snapshot.sh"

# Every case dara named in the dispatch, plus the two denominator traps and the
# two #1045-review regressions. Raise this when cases are added; never lower it
# to make a red wrapper green.
MIN_ASSERTIONS = 54


@pytest.fixture(scope="module")
def run() -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HARNESS)], capture_output=True, text=True, timeout=300
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


def test_classification_contract_holds(run):
    _, failed = _tally(run.stdout)
    assert failed == 0, run.stdout
    assert run.returncode == 0, run.stdout


def test_denominator_comes_from_declarations_not_directories(run):
    """The blind spot that survives review because the output looks complete.

    A bot declared in fleet.yaml with no transcript, and no directory at all,
    must still print — as a strand. A naive per-transcript loop drops it from
    both lists, and 6 of 21 silently becomes 6 of 19.
    """
    assert "PASS: declared bot with NO directory appears, as a strand" in run.stdout
    assert "PASS: declared bot with no transcript appears, as a strand" in run.stdout
    assert "PASS: undeclared leftover dir is NOT classified" in run.stdout
    assert (
        "PASS: denominator is the UNION across both manifests (8, not 7)" in run.stdout
    )


def test_unparseable_manifest_is_fatal_not_skipped(run):
    """Soft-skipping a manifest reintroduces the same bug one layer up."""
    assert "PASS: unparseable manifest exits non-zero" in run.stdout
    assert "PASS: no counts are printed alongside the refusal" in run.stdout


def test_each_override_is_scoped_to_its_own_condition(run):
    """#1045 review regression, found by vera.

    SELFSTART_ALLOW_PARTIAL used to waive the duplicate-name check as well as
    the manifest-parse one it was built for, and silently: no banner, no PARTIAL
    stamp, exit 0, a page that read as an ordinary trustworthy snapshot. An
    override advertised on one banner must never be honoured by a second check —
    that is the silent denominator getting back in through the door built to
    make partiality loud. Asserted in both directions, since a fix that only
    holds one way just swaps the operands.
    """
    assert "PASS: ALLOW_PARTIAL does NOT waive the duplicate check" in run.stdout
    assert "PASS: no snapshot page is printed under the bypass attempt" in run.stdout
    assert (
        "PASS: duplicate override does NOT waive an unparseable manifest" in run.stdout
    )
    assert "PASS: duplicate override stamps the headline" in run.stdout


def test_the_run_proves_it_covered_every_declared_bot(run):
    """Without `set -e`, completion cannot be inferred from the absence of a crash.

    The failure dropping -e was meant to prevent — a two-thirds page that reads
    as complete — comes straight back as a two-thirds page that exits 0 unless
    coverage is asserted positively.
    """
    assert "PASS: an incomplete run exits non-zero" in run.stdout
    assert "PASS: incomplete headline is stamped, not just the banner" in run.stdout
    assert "PASS: healthy run carries no INCOMPLETE stamp" in run.stdout
