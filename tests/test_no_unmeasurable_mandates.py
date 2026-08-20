"""No composed asset may demand a number no bot can measure (#1011).

`context-management` told every bot to report a context percentage and gated
restarts on 50/60/70. No `statusLine` is composed for any bot, and a bot cannot
self-measure the figure, so a mandate to state one forces an invention.

CORRECTED 2026-08-20 (#1285): the original rationale here went further and said
the pane renders no percentage at all, on a sweep of FOUR live bots that showed
zero. That was a zero from a sample containing no positive case. Re-measured
across all 21 bots on this host, one rendered `98% context used` while twenty
rendered nothing — above an undocumented threshold the pane DOES carry a live
figure, and below it the same slot renders empty. So capture-pane is a positive
detector after all; what it cannot do is certify health, because a blank slot
means either "never reached the threshold" or "reached it and came back down".

That correction does NOT weaken this gate, because the gate was never really
about whether the number is visible: a composed asset must not DEMAND a figure
or GATE on one, since the bot it is composed onto may well be below the
threshold and see nothing. Naming the rendered element is description and is
allowed; demanding a number is not.

So a bot complying with one composed protocol had to invent a number, colliding
with the `no-fabrication` guardrail composed onto the same bot — and a manager
would then route restarts by it.

The reason this is a TEST and not just a fix: the mandate lived in EIGHT assets
across protocols, expertise and skills, on both the worker and the manager side.
Repairing one leaves two composed files contradicting each other, which is the
defect rather than a symptom of it. This asserts the whole surface at once.
"""

from __future__ import annotations

import re
from pathlib import Path

LIBRARY = Path(__file__).resolve().parent.parent / "library"

# Every shape an asset has actually used the figure in: a demand ("report your
# context %"), a gate ("above 60% context", "context is > 70%"), or a FIELD IN
# AN OUTPUT TEMPLATE ("context 34%") — that last one is not a footnote, it is
# the shape that survived the first pass of this fix, in the one place a bot
# copies verbatim.
#
# No exemption list, and still none. The first version had one, on the theory
# that prose explaining the instrument would trip the patterns; measured, it
# never saved a single line, while silently granting 21 unrelated lines immunity
# from every other pattern.
#
# What IS carved out is one literal: `NN% context used`, the rendered element's
# own name (#1285). Describing the thing the pane draws is not demanding a
# figure, and after #1285 the protocols must be able to name it precisely. This
# is a shape, not a file list — every other pattern still applies to those same
# lines, so "stay under 70% context" fails wherever it is written.
#
# Anchoring note, because it is what made the first version useless: do NOT end
# a pattern with `\b` after `%`. A word boundary needs an adjacent word
# character, and `%` is followed by `*`, `:`, or a space in every real case —
# so `\bAbove \d{2}%\b` matched NOTHING, and the three canonical
# `**Above 50/60/70%**` bullets this issue is about sailed through green.
_CONTEXT_PERCENTAGE = re.compile(
    r"context\s*%"
    r"|\breads? your own context\b"
    r"|\d{1,3}\s?%\s*context\b"
    r"|\bcontext\b[^.\n]{0,24}[>\u2265]\s*~?\d{1,3}\s?%"
    r"|\bcontext \d{1,3}\s?%"
    r"|^[^A-Za-z\n]*(?:above|if)\s*[>\u2265~]*\s*\d{1,3}\s?%",
    re.I,
)

# The rendered element's own name, stripped before matching so that DESCRIBING
# it is allowed while DEMANDING a number is not (#1285).
#
# BACKTICKS ARE LOAD-BEARING. The first version of this carve-out matched the
# bare words, which silently punched a hole straight through the gate: "stay
# under 70% context used" is a mandate, and stripping the literal left "stay
# under " behind, which no remaining pattern matches. Requiring the element to
# be quoted as code keeps description legible while leaving every prose
# mandate exposed. Pinned by test_the_element_carveout_does_not_blunt_the_gate.
_RENDERED_ELEMENT = re.compile(r"`\s*\d{1,3}\s?%\s*context used\s*`", re.I)


def _composable_assets() -> list[Path]:
    return sorted(
        p for p in LIBRARY.rglob("*.md") if p.is_file() and "README" not in p.name
    )


def test_no_asset_demands_an_unmeasurable_context_percentage():
    """One loop, not 187 parametrized cases.

    Parametrizing bought nothing: `ids=lambda p: p.name` collides across the 50
    files named SKILL.md, so pytest emitted SKILL.md0..SKILL.md49 and the
    per-file identification was gone anyway. A single assert lists EVERY
    offender with path:lineno, which is the more useful failure.
    """
    assets = _composable_assets()
    assert len(assets) > 20, f"only {len(assets)} assets found — glob is wrong"

    offenders = [
        f"{p.relative_to(LIBRARY)}:{n}: {line.strip()}"
        for p in assets
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if _CONTEXT_PERCENTAGE.search(_RENDERED_ELEMENT.sub("", line))
    ]
    assert not offenders, (
        "Composed asset names a context percentage no bot can measure (#1011).\n"
        "Every bot also carries the no-fabrication guardrail, so complying means\n"
        "violating that one. Gate on observable signals instead — units of work\n"
        "finished, or the worker's own `context-degraded` report:\n  "
        + "\n  ".join(offenders)
    )


def test_context_management_still_carries_a_restart_duty():
    """Dropping the metric must not drop the discipline (#1011 constraint 1).

    The thresholds existed because degraded context produces bad output, and
    that problem is real even though the metric never was. A worker still owes
    the manager a signal, and the manager still needs something to route on.
    """
    text = (LIBRARY / "protocols" / "context-management.md").read_text()
    assert "context-degraded" in text, "no greppable signal for the manager to route on"
    assert re.search(r"report-back", text), "no duty to raise it"
    assert re.search(r"symptom", text, re.I), "no observable trigger named"
    assert re.search(r"re-read|look up the same", text, re.I), "symptoms not concrete"


def test_the_signal_token_is_shared_by_both_sides():
    """A worker-side token nothing consumes is a dead letter, and a
    manager-side trigger no worker emits never fires. Both halves, or neither."""
    worker = (LIBRARY / "protocols" / "context-management.md").read_text()
    managers = [
        LIBRARY / "expertise" / "orchestration.md",
        LIBRARY / "protocols" / "safe-worker-restart.md",
        LIBRARY / "protocols" / "continuous-autonomous-mode.md",
    ]
    assert "context-degraded" in worker
    for m in managers:
        assert "context-degraded" in m.read_text(), (
            f"{m.name} does not consume the signal"
        )


# --- #1285 -------------------------------------------------------------------
# A composed line asserting that a capability does not exist is a claim about
# the world, checkable in seconds, and being composed is not evidence for it.
# This one was false for as long as it had been composed, and undetectable by
# construction: it removed the reason to look. It was found only because a bot
# captured another bot's pane for an unrelated purpose.
_DENIES_RENDERED_PERCENTAGE = re.compile(
    r"pane (?:carries|renders|shows) no (?:context )?percentage"
    r"|no percentage is ever (?:rendered|drawn|shown)"
    r"|no status line is composed for any bot"
    r"|nothing (?:is )?(?:ever )?(?:rendered|drawn) in the pane"
    r"|you cannot capture what is never drawn",
    re.I,
)



def _search_across_lines(text: str, pattern: re.Pattern) -> list[tuple[int, str]]:
    """Match a pattern that may be hard-wrapped across lines, reporting real linenos.

    Not a nicety: the original #1285 claim was wrapped as "no percentage\\n  is
    ever rendered", so the obvious line-by-line scan could never have matched
    it — and a detector that cannot match the very string it was written for is
    green for the wrong reason. Found by this file's own positive control.

    Whitespace is collapsed into a single normalised string with a parallel
    char -> lineno map, so there is no window bound: a claim spread over any
    number of lines still matches, and the lineno reported is where it starts.
    """
    normalised: list[str] = []
    line_of: list[int] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for chunk in line.split():
            if normalised:
                normalised.append(" ")
                line_of.append(lineno)
            normalised.append(chunk)
            line_of.extend([lineno] * len(chunk))
    flat = "".join(normalised)
    return [
        (line_of[m.start()], flat[m.start() : m.end()])
        for m in pattern.finditer(flat)
    ]

def test_no_asset_claims_the_pane_never_renders_a_percentage():
    """The absolute this replaced was false, and unfalsifiable by its own effect.

    Measured across all 21 bots on this host: one rendered `98% context used`,
    twenty rendered nothing, panes structurally identical. The original claim
    rested on a four-bot sweep that happened to contain no positive case.

    The anti-fabrication rule is untouched and is asserted separately above:
    never state a figure you have not seen. What is forbidden here is telling a
    reader the figure can never exist, because that is what stopped anyone
    looking.
    """
    offenders = []
    for path in _composable_assets():
        for lineno, snippet in _search_across_lines(
            path.read_text(), _DENIES_RENDERED_PERCENTAGE
        ):
            offenders.append(f"{path.relative_to(LIBRARY)}:{lineno}: {snippet}")
    assert not offenders, (
        "Composed asset denies a capability that MEASURABLY EXISTS (#1285).\n"
        "Above an undocumented threshold the pane renders `NN% context used`.\n"
        "Absence proves nothing (two causes, indistinguishable); presence is\n"
        "real and actionable. Describe the bound, do not deny the instrument:\n  "
        + "\n  ".join(offenders)
    )


def test_the_denial_detector_actually_detects():
    """Positive control — a zero from a detector never shown to fire is not absence.

    These are the passages this PR removed, VERBATIM AND STILL WRAPPED, run
    through the same `_search_across_lines` door the gate uses. Feeding it
    pre-joined text would certify a path production never takes: the first
    version of this control did exactly that and passed a detector that could
    not match the wrapped original.
    """
    was_composed = [
        # library/protocols/context-management.md, pre-#1285
        "**No tool reports your context usage to you.** No status line is composed for\n"
        "any bot, and the pane carries no percentage.",
        # library/protocols/fleet-observability.md, pre-#1285 — wrapped mid-claim
        "- **`context_warning`** — not present in the payload, **and not obtainable by\n"
        "  any other route either.** Capture-pane does not close this gap: no percentage\n"
        "  is ever rendered, so there is nothing to capture, and a bot cannot\n"
        "  self-measure it.",
        # this file's own former docstring
        "capture-pane as the manager-side fallback, which cannot work for the same reason\n"
        "(you cannot capture what is never drawn).",
    ]
    for passage in was_composed:
        assert _search_across_lines(passage, _DENIES_RENDERED_PERCENTAGE), (
            f"detector missed a real pre-fix passage:\n{passage}"
        )


def test_the_denial_detector_does_not_fire_on_the_replacement():
    """Negative control — the corrected wording must pass its own gate.

    Without this, the gate could be satisfied by prose that merely avoids the
    banned phrasings while still telling a reader not to look.
    """
    replacement = (
        "Above some threshold the pane carries a `NN% context used` figure;\n"
        "below it, the same slot renders empty. Absence proves nothing —\n"
        "presence is real and actionable."
    )
    assert not _search_across_lines(replacement, _DENIES_RENDERED_PERCENTAGE)


def test_the_element_carveout_does_not_blunt_the_mandate_gate():
    """Positive control on the #1285 carve-out — narrowing a gate owes proof it still bites.

    The first carve-out matched the element's name unquoted, which let a real
    mandate ("stay under 70% context used") through: stripping the literal left
    a fragment no other pattern matches. These are the shapes that must still
    fail, including that one.
    """
    must_still_fail = [
        "Report your context % at each handoff.",
        "**Above 60%** — hand off before starting new work.",
        "If context > 70%, restart the bot.",
        "Status: bot healthy, context 34%.",
        "Stay under 70% context used before taking a new task.",
        "Restart any worker above 80% context used.",
    ]
    for line in must_still_fail:
        stripped = _RENDERED_ELEMENT.sub("", line)
        assert _CONTEXT_PERCENTAGE.search(stripped), f"gate went blind to: {line!r}"


def test_the_carveout_still_permits_naming_the_element():
    """Negative control — the corrected protocols must actually pass the gate."""
    permitted = [
        "Measured: one bot rendered `98% context used` while twenty rendered nothing.",
        "Above the threshold the pane carries a `NN% context used` figure.",
    ]
    for line in permitted:
        stripped = _RENDERED_ELEMENT.sub("", line)
        assert not _CONTEXT_PERCENTAGE.search(stripped), f"gate over-fires on: {line!r}"
