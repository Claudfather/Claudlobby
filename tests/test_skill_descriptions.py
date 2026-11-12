"""Every library skill's description says when to use it (#2330).

Claude Code lists each installed skill to the model by its ``description``. A
description that names the situation gets the skill invoked without anyone
pointing at it; one that only names its contents waits for an instruction
elsewhere. Measured on 2026-10-13 on a host with 21 bots: 19 of the 33 library
skills composed there had no trigger, ``fleet-ops`` among them, and one more
named its situation only after what it does. All 56 plugin skills composed
beside them had one, and 54 led with it.

A trigger is a clause that opens with the imperative the plugin skills use:
"Use when ...", "Use before ...", "Use to ...", or "You MUST use this before
...". It has to open the description: one that only contains the words ("the
model to use for review") does not pass, and nor does one that names the
situation only after saying what the skill does. The situation comes first,
then what the skill does.

Bounds: this reads the library's own skills, not a fleet overlay's. Whether a
trigger makes a skill fire more often is not measured here; that needs an eval
(``eval-workflow``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claudlobby.loader import frontmatter_error, parse_frontmatter

SKILLS = Path(__file__).resolve().parent.parent / "library" / "skills"

TRIGGER = re.compile(
    r"(?:Use(?: this(?: skill)?| it)?|You (?:MUST|must) use (?:this|it))"
    r"\s+(?:when|whenever|before|after|at|during|for|to)\b"
)


def has_trigger(description: object) -> bool:
    """True when ``description`` opens with a trigger clause."""
    return isinstance(description, str) and bool(TRIGGER.match(description))


@pytest.mark.parametrize(
    "description",
    [
        "Use when a scheduled briefing timer fires `/briefing <slot>`.",
        "Use when an account must be switched. Re-authenticates the account.",
        "Use at any juncture where you want a plan stress-tested.",
        "Use before starting substantive work in a repo.",
        "Use to save knowledge to the vault.",
        "You MUST use this before any creative work.",
        "Use this skill when the user asks to pair a device.",
    ],
)
def test_a_description_opening_with_a_trigger_passes(description):
    assert has_trigger(description)


@pytest.mark.parametrize(
    "description",
    [
        # fleet-ops before #2330: what it does, and no situation ("Use the" names none)
        "Read fleet work, admit tasks, deliver and route assignments as manager, and "
        "accept or report assigned work as worker. Use the canonical claudlobby CLI "
        "and verify recorded results.",
        # A trigger after what the skill does (account-relog before it led with one)
        "Re-authenticate the account. Use when an account must be switched.",
        "Challenge a plan. Use at any juncture where you want it stress-tested.",
        "Self-diagnostic for code reviewer bot.",
        "Picks the model to use for each review.",
        "Useful when the fleet is idle.",
        "",
        None,
    ],
)
def test_a_description_with_no_trigger_fails(description):
    assert not has_trigger(description)


def test_every_library_skill_description_opens_with_its_trigger():
    skill_files = sorted(SKILLS.glob("*/SKILL.md"))
    assert skill_files, f"no skills under {SKILLS}"
    missing = []
    for skill_md in skill_files:
        text = skill_md.read_text(encoding="utf-8")
        assert frontmatter_error(text) is None, skill_md
        frontmatter, _body = parse_frontmatter(text)
        description = frontmatter.get("description")
        if not has_trigger(description):
            missing.append(f"{skill_md.parent.name}: {description!r}")
    assert not missing, (
        f"{len(missing)} of {len(skill_files)} library skills have a description "
        "that does not open with the situation to use them in. Open each with "
        "one, e.g. 'Use when ...', then say what the skill does "
        "(library/skills/README.md):\n  "
        + "\n  ".join(missing)
    )
