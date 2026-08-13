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

# Every case dara named in the dispatch, plus the two denominator traps, the
# two #1045-review regressions, the #1043 contamination/typing cases, the #1106
# refusal-branch cases (8h-8k) and the #1203 lateness bound (case 10). Raise
# this when cases are added; never lower it to make a red wrapper green.
MIN_ASSERTIONS = 183


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


def test_a_reading_taken_before_the_boot_ladder_finishes_refuses_to_be_a_result(run):
    """#1050. The failure mode this guards is the worst-shaped one available.

    A bot whose ExecStartPre rung has not elapsed has not been launched, so it
    cannot have written a post-boot record. Counting it as stranded measures the
    elapsed clock, not self-starting — and at boot+20s on a 21-bot host that
    renders as "0 of 21" against a 6-of-21 baseline. Read during an incident by
    someone deciding whether to intervene, that is a catastrophe that has not
    happened, and it argues for exactly the panicked mass-restart the standing
    posture exists to prevent.

    The gate refuses rather than caveats: the headline stops claiming a result
    at all, the provisional number stays visible but labelled, and not-launched
    is a separate class from stranded.
    """
    assert "PASS: headline refuses to state a result" in run.stdout
    assert "PASS: unlaunched bot is NOT-YET-DUE, not stranded" in run.stdout
    assert "PASS: not-yet-due dominates the pre-crash RAW false positive" in run.stdout
    assert "PASS: banner names the re-run instant" in run.stdout
    # Positive control: past the window the same fixtures must yield a real
    # result, or this would pass against a script that gates unconditionally.
    assert "PASS: no too-early banner once the ladder has finished" in run.stdout
    assert "PASS: the unlaunched bot is now a genuine strand" in run.stdout


def test_every_refusal_carries_a_non_zero_exit_code(run):
    """A refusal that exits 0 is a caveat (#1051 review, vera).

    The banner is advisory to a human reading the text and invisible to anything
    else, so without its own code a TOO EARLY page is indistinguishable to a
    programmatic consumer from a trustworthy result. The exit code is where
    refuse-rather-than-caveat either holds or quietly does not — and it was the
    one RC in the harness with no assertion on it, which is exactly the shape
    that silently regresses.

    Precedence is asserted too: 4 (incomplete) outranks 5 (too early) when both
    hold, because re-running fixes early and need not fix incomplete.
    """
    assert "PASS: too-early run exits non-zero, and with its own code" in run.stdout
    assert "PASS: incomplete outranks too-early in the exit code" in run.stdout
    assert "PASS: but both banners still print — early" in run.stdout
    # Positive control: the healthy run must still be 0, or a blanket non-zero
    # would satisfy the assertions above.
    assert "PASS: past the window it exits 0" in run.stdout


def test_liveness_is_not_self_start(run):
    """#1043. The class every other signal calls healthy.

    A bot woken by an inbound channel message runs real work, reports normally,
    and looks alive to session checks, pane checks and the pulse — while never
    having started on its own. On 2026-08-08 the bot that ran the measurement
    and repaired twelve others was itself in this class. Presence-of-record
    cannot see it, so the first post-boot user record is TYPED before any
    instant is compared.

    The typing is a denylist: the channel injection and the tool_result record
    are the only shapes that do not vary, so those are matched and "payload" is
    whatever is left. Detectors that instead tried to recognise a startup
    payload all failed — payloads are authored per bot, one prose, the next a
    bare slash command, and a rescuer types an approximation of neither.
    """
    assert "PASS: inbound-woken is NOT a self-start" in run.stdout
    assert "PASS: tool_result records are excluded from the typing" in run.stdout
    assert "PASS: inbound-woken section says liveness is not self-start" in run.stdout
    assert (
        "PASS: assistant records with NO user record are refused, not guessed"
        in run.stdout
    )


def test_a_half_submitted_boot_is_not_a_self_start(run):
    """#843/#1043. Asserting "something startup-shaped arrived" is not enough.

    A boot is TWO sends — a bare slash command and the composed prose prompt.
    Measured on real bots, one composed prompt was still unsubmitted 39 minutes
    after boot and another never arrived at all; both read as clean self-starters
    under the weaker contract. A bot running without the instructions it was
    composed with has not booted, however alive it looks.

    Note the deliberate asymmetry with the typing above: this half compares
    against the bot's OWN composed value, because "did THIS bot's known prompt
    land" has exactly one right answer per bot.
    """
    assert (
        "PASS: only the slash half submitted is PARTIAL, not a self-start" in run.stdout
    )
    assert "PASS: a half-booted bot is excluded from the headline count" in run.stdout
    assert (
        "PASS: a prompt that is not THIS bot composed one does not satisfy it"
        in run.stdout
    )
    # Positive control: a complete injection must still read as a self-start,
    # or a blanket downgrade would satisfy every assertion above.
    assert "PASS: both halves submitted is a genuine self-start" in run.stdout
    # The assertion cannot run without a composed prompt, so that is disclosed
    # rather than silently credited as a clean boot.
    assert "PASS: a bot with no composed prompt is disclosed" in run.stdout


def test_a_reading_taken_after_a_rescue_refuses_to_be_a_result(run):
    """#1043, the mirror of the too-early gate — and the one that already bit.

    On 2026-08-08 the same host read 7 of 21 before a rescue and 19-20 of 21
    three minutes after one, both stamped `result valid: yes`. The defect was
    never the number; it was the validity claim printed over it. So a run that
    a rescue receipt covers refuses, with its own exit code, rather than
    caveating.

    The receipt carries two independent facts and a real one disagreed with
    itself, so both are used and a contradiction is refused BY NAME rather than
    resolved toward either side. A receipt that discloses it was written after
    the event has a TYPED stamp, so the comparison is suppressed — but its name
    list still stands, because a list of who was touched is not reconstructed by
    being written down late.
    """
    assert "PASS: a contaminated page refuses with its own exit code" in run.stdout
    assert "PASS: headline stops claiming a result" in run.stdout
    assert "PASS: payload after the boundary is RESCUED, not self-started" in run.stdout
    assert (
        "PASS: named-as-rescued yet predating the boundary is a CONTRADICTION"
        in run.stdout
    )
    assert "PASS: and the contradiction is spelled out" in run.stdout
    assert "PASS: a retroactive receipt still refuses" in run.stdout
    assert "PASS: a named bot is still RESCUED without any comparison" in run.stdout
    # The name list is NON-EXHAUSTIVE: presence means rescued definitively,
    # absence means UNKNOWN. A second rescue went unrecorded hours after the
    # receipt was designed, by the person who proposed it — so a list is only as
    # complete as someone's discipline, and the design must not lean on it.
    assert (
        "PASS: absence from a non-exhaustive list is NOT evidence of self-start"
        in run.stdout
    )
    assert (
        "PASS: an unnamed bot is decided by the boundary, not by the list" in run.stdout
    )
    # The boundary is compared, never clustered: a receipt for an earlier boot
    # must not bleed forward, and a correction row is not a receipt.
    assert (
        "PASS: a boundary predating this boot belongs to an earlier one" in run.stdout
    )
    assert "PASS: a correction row alone does not contaminate" in run.stdout
    assert (
        "PASS: half a second past a fractionless boundary is still RESCUED"
        in run.stdout
    )
    # Precedence, and the positive control that the gate is not unconditional.
    assert "PASS: contaminated outranks too-early in the exit code" in run.stdout
    assert "PASS: incomplete outranks contaminated" in run.stdout
    assert "PASS: no receipt: exits 0" in run.stdout


def test_a_boundary_that_is_not_one_is_refused_never_guessed(run):
    """#1106. The three ways a receipt fails to carry a comparable boundary.

    All three land in the same class — boundary suppressed, name list still
    applied — so the class cannot tell them apart, and asserting on it alone
    would pass against any arm being wired to any other. The reason string is
    the discriminator, and it is the operator-facing half too: a field the
    writer omitted, two the writer emitted, and one emitted in the wrong shape
    are repaired in three different places.

    These branches shipped with #1103 uncovered. vera confirmed by hand that
    each already behaves to contract, so this closes a coverage gap rather than
    a defect — but refuse-rather-than-guess is the load-bearing property of that
    change, and an untested refusal path is how a gate quietly stops gating.

    Mutation-checked rather than assumed: deleting each guarded line in turn
    kills at least one assertion below. The ambiguity arm is the sharpest —
    without it the two candidate stamps survive as a two-line string that
    iso_utc_shaped still ACCEPTS, since its trailing-Z glob spans the newline,
    so the run adopts a garbage boundary and stamps it USABLE.
    """
    # Absent, ambiguous and uncomparable are reported as three distinct things.
    assert "PASS: and reports the field as ABSENT" in run.stdout
    assert "PASS: and names ambiguity as the reason" in run.stdout
    assert "PASS: and echoes the stamp so it can be repaired" in run.stdout
    # ...and none of them is conflated with either of the others.
    assert "PASS: absence is not reported as a present-but-unusable stamp" in run.stdout
    assert "PASS: ambiguity is not reported as absence" in run.stdout
    assert "PASS: a present stamp is not reported as absent" in run.stdout
    # No unusable stamp is ever adopted as the boundary.
    assert "PASS: and neither of the two candidates is adopted" in run.stdout
    assert "PASS: and the uncomparable stamp is never adopted" in run.stdout
    # Positive control on the ambiguity count: it is over DISTINCT values, so a
    # repeated-but-consistent boundary must still be adopted AND must still
    # decide. Without this, a helper that refused any repetition at all would
    # satisfy the assertions above — an over-refusal that throws away a good
    # boundary and drops every unnamed bot to ADJUDICATE for nothing.
    assert "PASS: yet the boundary is ADOPTED, not refused as ambiguous" in run.stdout
    assert "PASS: a payload after the adopted boundary is RESCUED" in run.stdout
    assert "PASS: a payload before it is still a self-start" in run.stdout


def test_the_absence_of_a_receipt_is_not_the_absence_of_a_rescue(run):
    """Coverage honesty: with no external boundary, contamination is undecidable.

    Saying nothing there would read as "no rescue happened", which is the one
    thing it never means.
    """
    assert (
        "PASS: absence of a receipt is disclosed, not read as absence of rescue"
        in run.stdout
    )
    assert "PASS: prior figure is printed" in run.stdout
    assert "PASS: and is labelled not comparable" in run.stdout
    assert "PASS: it is NOT offered as a target to beat" in run.stdout


def test_an_unrecorded_wake_is_a_gap_not_a_self_start(run):
    """#1203. SELF-STARTED was reached by ELIMINATION, so it absorbed everything.

    A payload no receipt covers and that is not channel-shaped falls through to
    SELF-STARTED. Compose that with #1110 — the receipt list is known-incomplete,
    a real rescue went unrecorded — and every wake nobody wrote down was reported
    as good news. Measured on the 2026-08-13 19:17:18Z boot: ari's payload landed
    26 minutes after boot, predating the only applicable receipt boundary, and
    printed as "unaided". The honest figure was 2 of 21; the script said 3.

    The bound is the instant the page ALREADY claims to be a result (ladder end +
    first-turn allowance). Not a second knob: the too-early gate stops refusing
    there, which is the script asserting every bot has had its chance, so a later
    arrival is late by the script's own claim.

    Not the bot's OWN rung, and that is measured rather than argued: otis (rung
    15s, payload +97s) and ravi (rung 12s, +96s) are the only two confirmed
    self-starts the estate has, and a per-bot bound flags both.
    """
    assert "PASS: a payload past the bound is LATE-UNEXPLAINED" in run.stdout
    assert "PASS: one second past the bound is enough" in run.stdout
    # Its own answer. Folding it into RESCUED would assert a rescue nothing
    # evidences — the same fabrication as asserting none.
    assert "PASS: late is NOT folded into RESCUED" in run.stdout
    assert "PASS: the section reads as a gap, not a pass" in run.stdout
    # Positive controls. Without these the whole case is satisfied by a script
    # that downgrades every self-start, which reads as a fix and measures nothing.
    assert "PASS: a payload inside the bound is still a self-start" in run.stdout
    assert "PASS: a payload exactly ON the bound is not past it" in run.stdout
    assert "PASS: half a second past the bound is late, not rounded down" in run.stdout
    assert (
        "PASS: a bot far past its OWN rung but inside the ladder bound survives"
        in run.stdout
    )
    # Derived, not hardcoded: raising the composed ladder moves the bound.
    assert "PASS: raising the composed ladder moves the bound with it" in run.stdout
    assert (
        "PASS: a bot 26 minutes out is late even against the longer ladder"
        in run.stdout
    )
    assert "PASS: widening the allowance widens the bound" in run.stdout
    # A row verdict, not a page refusal — 4/5/6 say the PAGE is not a result.
    assert "PASS: a late bot does not make the page refuse" in run.stdout
    assert "PASS: and does not trip the completeness assertion" in run.stdout
    assert "PASS: but keeps them in the upper range" in run.stdout
    # Evidence still beats absence of evidence, and the live case is the half
    # where the receipt does NOT reach: that is ari on 2026-08-13, exactly.
    assert (
        "PASS: a late payload AFTER the rescue boundary is RESCUED, not late"
        in run.stdout
    )
    assert "PASS: a late payload the receipt does NOT cover is still late" in run.stdout
    # Suppressed where the bound would be asserted rather than derived — and a
    # suppression that says nothing is itself a silent pass, so it is disclosed
    # in the header AND by name for every bot credited under it.
    assert "PASS: with no ladder the bound is not applied" in run.stdout
    assert "PASS: every bot credited under the suppression is NAMED" in run.stdout
    assert "PASS: a stale boot clock suppresses the bound" in run.stdout
    assert "PASS: so no bot is called late on an untrusted clock" in run.stdout
    # A disclosure keyed on the wrong condition is longest and loudest exactly
    # where it is false: with no rung anywhere, the no-rung note lists EVERY bot
    # and must not tell them the bound still covers them.
    assert "PASS: the no-rung note does not claim the bound still applies" in run.stdout
    # An unformattable bound instant yields an EMPTY boundary, which sorts before
    # every record — adopting it flags every self-starter at once, silently, on a
    # page that otherwise reads normally. Mutation-confirmed: removing the guard
    # flags `intime` too, so this is the whole-fleet failure, not a corner.
    assert "PASS: an unformattable bound instant suppresses the bound" in run.stdout
    assert "PASS: no bot is flagged late off an empty boundary" in run.stdout
    assert "PASS: and the genuine self-starters are still credited" in run.stdout
    assert (
        "PASS: and with a formattable bound the same fixtures still flag" in run.stdout
    )


def test_the_run_proves_it_covered_every_declared_bot(run):
    """Without `set -e`, completion cannot be inferred from the absence of a crash.

    The failure dropping -e was meant to prevent — a two-thirds page that reads
    as complete — comes straight back as a two-thirds page that exits 0 unless
    coverage is asserted positively.
    """
    assert "PASS: an incomplete run exits non-zero" in run.stdout
    assert "PASS: incomplete headline is stamped, not just the banner" in run.stdout
    assert "PASS: healthy run carries no INCOMPLETE stamp" in run.stdout
