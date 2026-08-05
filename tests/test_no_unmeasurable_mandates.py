"""No composed asset may demand a number no bot can measure (#1011).

`context-management` told every bot to report a context percentage and gated
restarts on 50/60/70. Nothing surfaces that figure: no `statusLine` is composed
for any bot, and the pane renders no percentage — checked across four live bots,
zero showed one. `fleet-observability` even documented the gap, and proposed
capture-pane as the manager-side fallback, which cannot work for the same reason
(you cannot capture what is never drawn).

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
# No exemption list. The first version had one, on the theory that prose
# explaining the instrument's absence would trip the patterns; measured, it
# never saved a single line, while silently granting 21 unrelated lines immunity
# from every other pattern. Prose about the ABSENCE of the number does not name
# a number, so it needs no escape hatch.
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
        if _CONTEXT_PERCENTAGE.search(line)
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
