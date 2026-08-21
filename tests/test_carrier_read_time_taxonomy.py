"""The update-carrier taxonomy must sort by READ TIME, not artifact type (#1310).

`CLAUDE.md` and `documentation/fleet-update-lifecycle.md` both said a composed
file "does not reach a bot until it restarts". A composed SKILL is a composed
file by that taxonomy, and it is live the instant `generate` writes the symlink
-- measured on three bots mid-session, no restart.

Why this needed a test rather than a fix. It is not an omission: the model
MIS-SORTS skills into the row that promises a restart-shaped canary window. A
reader does not reason badly from a missing rule, they reason correctly from a
wrong one -- and one did, running `generate` across a live estate without a
canary on the strength of it.

Same shape as #1011/#1285: a composed line asserting a property of the world,
checkable in seconds, where being written down is not evidence.

## What this file can and cannot guard, stated because the first version implied more

`_RESTART_ABSOLUTE` matches the claim as a **string**, so **any paraphrase walks
straight past it**. That is not a hypothetical: the first version of this guard
passed while the identical claim survived 70 lines below the fix, reworded as
"A bot already running does *not* have it ... false until each bot restarts".
Review caught it; the regex did not.

A pattern list cannot enumerate the ways English can assert something, and a
guard that implies coverage it lacks is worse than none. So the negative patterns
are a **best-effort tripwire for known phrasings only**, and the load-bearing
checks here are the POSITIVE ones: the carve-out that must be PRESENT in each
place the doc discusses carriers. Absence is bounded and mechanically checkable;
paraphrase is not. If the skills carve-out disappears from any of those places,
the positive tests fail regardless of how the surrounding prose is worded.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = ROOT / "CLAUDE.md"
LIFECYCLE = ROOT / "documentation" / "fleet-update-lifecycle.md"

# The claim, in the shapes it has actually been written in. Deliberately narrow:
# it must not fire on a correctly-SCOPED statement ("composed instructions ...
# arrive at the next restart"), which is true and is the corrected row.
#
# The gap character class must NOT exclude "." -- the real table row reads
# "Composed file (`CLAUDE.md`, `bot.conf`, `settings`) | no -- arrives at the
# next restart", and a [^.] gap cannot cross `CLAUDE.md`. The first version of
# this pattern had exactly that bug and its positive control caught it, which
# is the entire reason the control is here.
_RESTART_ABSOLUTE = re.compile(
    r"composed file[^\n]{0,80}?(?:does not reach|does not arrive)[^\n]{0,60}?restart"
    r"|composed file[^\n]{0,80}?arrives at the next restart"
    r"|all composed (?:files|artifacts)[^\n]{0,60}?restart"
    # Added after review: the claim survived 70 lines below the fix in a
    # REWORDED form -- "A bot already running does *not* have it ... false until
    # each bot restarts" -- which matched none of the three patterns above.
    r"|composed file[^\n]{0,120}?does\s+\*?not\*?\s+have it"
    r"|(?:false|not true) until each bot restarts",
    re.I,
)


def _flat(text: str) -> str:
    """Collapse hard-wrapped prose so a claim spanning lines still matches (#1285)."""
    return re.sub(r"\s+", " ", text)


def test_no_doc_claims_every_composed_file_is_restart_gated():
    offenders = [
        f"{p.relative_to(ROOT)}: {m.group(0)}"
        for p in (CLAUDE_MD, LIFECYCLE)
        for m in _RESTART_ABSOLUTE.finditer(_flat(p.read_text()))
    ]
    assert not offenders, (
        "Doc sorts update carriers by ARTIFACT TYPE, not read time (#1310).\n"
        "A composed skill is a composed file and is live at compose time --\n"
        "no restart, no canary window. Sort by when the artifact is READ:\n"
        "  read once at session start -> canary window EXISTS\n"
        "  read on demand per use     -> NO canary window\n  "
        + "\n  ".join(offenders)
    )


def test_the_absolute_detector_actually_detects():
    """Positive control -- the verbatim pre-#1310 wording, still hard-wrapped.

    A gate that has never been shown to fire is indistinguishable from a broken
    one, and this file's whole subject is a claim nobody re-checked.
    """
    was_documented = [
        # CLAUDE.md:70, pre-#1310
        "and they differ on whether they reach a *running* process and whether\n"
        "they survive a restart. A composed file does not reach a bot until it\n"
        "restarts; a hook script is live on every bot at its next tool call.",
        # fleet-update-lifecycle.md:23, pre-#1310 -- the table row
        "| **Composed file** (`CLAUDE.md`, `bot.conf`, `settings`) | **no** "
        "— arrives at the next restart | yes | `claudlobby generate` |",
        # fleet-update-lifecycle.md:100-102 -- the REWORDED survivor that the
        # first version of this guard missed entirely, found in review 70 lines
        # below the fix. This row is why the patterns were widened.
        "**Composed file — the one that misleads.** A bot already running does *not* have\n"
        "it. \"It is composed\" reads as \"they have it\" and is false until each bot\n"
        "restarts.",
    ]
    for passage in was_documented:
        assert _RESTART_ABSOLUTE.search(_flat(passage)), f"detector missed: {passage!r}"


def test_the_detector_permits_the_corrected_scoped_rows():
    """Negative control -- the fix must pass its own gate.

    The corrected table still says composed INSTRUCTIONS arrive at the next
    restart, which is true and measured. A guard that also killed that would
    push the next author back toward a single undifferentiated row.
    """
    corrected = [
        "| **Composed instructions** (`CLAUDE.md`, `bot.conf` env) | **once, at "
        "session start** | **no** — arrives at the next restart | yes |",
        "| **Composed skills** (`.claude/skills/` symlinks) | **on demand, per "
        "use** | **YES — live the instant the symlink lands** | yes |",
        "| **Composed permissions** (`settings.local.json`) | **UNMEASURED** |",
    ]
    for line in corrected:
        assert not _RESTART_ABSOLUTE.search(_flat(line)), f"gate over-fires on: {line!r}"


def test_both_docs_name_read_time_as_the_discriminator():
    """The positive half: removing the absolute is not enough if nothing replaces it.

    #1310's fix is a re-sort, not a deletion. If a future edit drops the read-time
    framing, the type-sorted taxonomy grows back and this file is the only thing
    that would notice.
    """
    for p in (CLAUDE_MD, LIFECYCLE):
        flat = _flat(p.read_text()).lower()
        assert "read" in flat and "on demand" in flat, (
            f"{p.relative_to(ROOT)} no longer names read time as the discriminator"
        )


# --- positive coverage: what a string guard cannot do, structure can ---------

def _lifecycle() -> str:
    return _flat(LIFECYCLE.read_text())


def test_the_prose_elaboration_carves_out_skills_not_just_the_table():
    """The blocking review finding: the table was fixed, the prose was not.

    A reader who continues past the table hit a flat contradiction in the same
    file. Fixing the table alone leaves the mis-sort intact for anyone who reads
    the elaboration, which is the part that explains WHY it matters.
    """
    flat = _lifecycle()
    table_end = flat.find("What was measured, and how")
    assert table_end > 0, "table section marker missing — test needs updating"
    prose = flat[table_end:]
    assert "Composed skills" in prose, (
        "the carrier-cost elaboration no longer carves out composed skills. "
        "The table alone is not enough — the prose is where the reasoning lives."
    )
    assert "symlink" in prose.lower() and "no restart" in prose.lower(), (
        "the skills carve-out no longer states the live-at-compose-time property"
    )


def test_reload_mechanism_does_not_promise_idle_gating_for_composed_skills():
    """Flagged in review as in tension; measurement resolved it.

    Mechanism 1 says activation is idle-gated via `.reload-pending` + keepalive.
    True for the plugin-cache half, and NOT true for composed skill symlinks —
    which are live at `generate` time with no marker and no keystroke. Read
    without the caveat, this section *promises* a deferral that does not exist,
    which is the #1310 defect in its most dangerous form.
    """
    flat = _lifecycle()
    idx = flat.find("Mechanism 1")
    assert idx > 0, "Mechanism 1 section missing — test needs updating"
    section = flat[idx : idx + 4000]
    assert "does NOT cover composed skill symlinks" in section, (
        "Mechanism 1 no longer scopes its idle-gating claim away from composed "
        "skills, so it reads as promising a canary window that does not exist"
    )


def test_the_string_guard_cannot_catch_paraphrase_and_says_so():
    """Documents the limit rather than leaving it implied.

    This asserts a LIMITATION, deliberately. A semantically identical claim in
    wording the patterns do not enumerate is NOT caught, and anyone extending
    this file should know that before trusting a green run. The positive tests
    above are what actually hold the line.
    """
    unenumerated = "Anything the compositor writes only takes effect once the bot comes back up."
    assert not _RESTART_ABSOLUTE.search(_flat(unenumerated)), (
        "if this now matches, the patterns grew — good, but the docstring's "
        "claim about coverage limits needs re-checking rather than deleting"
    )
