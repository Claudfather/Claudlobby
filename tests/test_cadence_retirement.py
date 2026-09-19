"""tests/test_cadence_retirement.py — chunk 4 (spec section 12, PR 4) retires the
shared library's cadence mandates that `library/protocols/checkin.md` supersedes
(design spec section 9's `## Worker`: "No milestone cadence."; its supersession
list: worker-lifecycle's milestone-every-2-3-min removed; proactivity-discipline's
"Idle silence is a bug" becomes "idle silence is recorded, not posted";
continuous-autonomous-mode's 10-15-min still-waiting beacons removed — the
check-in is the beat).

The pin is derived from the SAME grep the sweep was measured with (12 files, 21
hit lines on this tree), never from a hand-written file list: a new library file
that reintroduces a retired cadence phrase fails
test_every_surviving_hit_is_on_the_keep_list_with_a_reason by construction, not
because anyone remembered to update a list.
"""

from __future__ import annotations

import re
from pathlib import Path

from claudlobby.loader import parse_frontmatter

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"

SWEEP = re.compile(r"milestone|beacon|2.3 min|10.15 min|idle silence|never go silent"
                   r"|never licenses silence|cadence and frequency", re.I)

#: library-relative path -> why a hit SURVIVES the retirement. Every other hit must be gone.
KEEP = {
    "protocols/proactivity-discipline.md": "spec section 9's replacement sentence carries the phrase verbatim",
    "protocols/inbound-acknowledgment.md": "inbound reply loop -- a human is waiting on this turn",
    "protocols/comms-topology.md": "the bullet LABEL stays; the mandate sentence after it is gone",
    "protocols/checkin.md": "the precedence preamble names the retired rules so a fleet-local copy that still carries one knows it yields",
    "skills/autonomous-runner/SKILL.md": "quota beacon is an urgency-floor event, not a cadence",
    "skills/cross-fleet-initiative/SKILL.md": "GATE.md project milestones -- not a posting cadence",
}


def _flat(text: str) -> str:
    """Whitespace-collapsed, matching the convention tests/test_checkin_library.py
    uses for prose that wraps across lines."""
    return " ".join(text.split())


def _hits() -> dict[str, list[tuple[int, str]]]:
    """library-relative path -> [(1-based line number, line text), ...] for every
    line SWEEP matches, walking library/**/*.md exactly as the shell sweep
    (`grep -r -n -i -E ... library/`, which in practice only ever hits .md files
    here) does."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(LIB.rglob("*.md")):
        rel = path.relative_to(LIB).as_posix()
        lines = path.read_text().splitlines()
        matches = [(i, ln) for i, ln in enumerate(lines, start=1) if SWEEP.search(ln)]
        if matches:
            found[rel] = matches
    return found


def test_every_surviving_hit_is_on_the_keep_list_with_a_reason():
    # Derived from the grep, never a hand list: a new library file that
    # reintroduces a retired cadence phrase lands here as an un-KEEP-listed
    # file and fails, whether or not anyone remembered to update anything.
    found = _hits()
    unlisted = {rel: lines for rel, lines in found.items() if rel not in KEEP}
    assert not unlisted, f"cadence phrase(s) survive outside KEEP: {unlisted}"


def test_no_keep_entry_is_dead():
    # A KEEP entry with zero real hits is standing permission for a file that
    # no longer needs it — prove every entry still earns its place.
    found = _hits()
    for rel in KEEP:
        assert rel in found and found[rel], f"KEEP entry produces no hit: {rel}"


#: library-relative path -> retired phrases that must be gone after the edits.
#: Not every KEEP file is silent here — a file can survive the sweep on one hit
#: while still losing a *different*, specifically-retired phrase (proactivity-
#: discipline keeps "idle silence" but loses "is a bug"; inbound-acknowledgment
#: keeps its >60s heartbeat line but loses the "major milestone" wording).
RETIRED_PHRASES: dict[str, list[str]] = {
    "expertise/orchestration.md": [
        "Never go silent.",
    ],
    "protocols/checkin.md": [
        "No milestone cadence here",
        "which still stand",
    ],
    "protocols/comms-topology.md": [
        "This protocol never licenses silence.",
    ],
    "protocols/continuous-autonomous-mode.md": [
        "Never go silent. Silence reads as",
        'beacon every 10',
    ],
    "protocols/inbound-acknowledgment.md": [
        "At each major milestone",
        "manager wait-point beacons",
    ],
    "protocols/proactivity-discipline.md": [
        "Idle silence is a bug.",
    ],
    "protocols/telegram-routing.md": [
        "progress milestone (~2-3 min during active work)",
    ],
    "protocols/token-efficiency.md": [
        "milestone cadence, wait-point beacons",
    ],
    "protocols/worker-lifecycle.md": [
        "Telegram milestones every 2",
        "Every 2-3 min during work",
    ],
    "skills/autonomous-runner/SKILL.md": [
        "for this cadence tick",
    ],
    "skills/lifecycle/SKILL.md": [
        "Never go silent — report what's happening",
    ],
}


def test_the_retired_phrases_are_gone_from_the_edited_files():
    for rel, phrases in RETIRED_PHRASES.items():
        text = (LIB / rel).read_text()
        for phrase in phrases:
            assert phrase not in text, f"{rel} still contains retired phrase: {phrase!r}"


def test_checkin_carries_the_precedence_sentence():
    # PR 2's preamble said the older cadence rules "still stand"; chunk 4
    # retires them, so the preamble must agree with the retirement while
    # keeping the pinned phrase test_checkin_library.py also depends on.
    text = (LIB / "protocols" / "checkin.md").read_text()
    _fm, body = parse_frontmatter(text)
    preamble = _flat(body.split("## Manager")[0])
    assert "governs where it composes beside" in preamble
    assert "which still stand" not in _flat(body)
