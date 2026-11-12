"""tests/test_pr_comment_hygiene_library.py — the pr-comment-hygiene protocol
keeps every rule and comment format in the CLAUDE.md of each bot that composes
it, and its sample findings, fork preferences and verdicts live in a shared
skill partial that every bot's copy holds (#1674)."""

from __future__ import annotations

import shutil
from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from tests.conftest import install_real_template
from tests.package_fixtures import source_package

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
PROTOCOL = LIB / "protocols" / "pr-comment-hygiene.md"
PARTIAL = LIB / "skills" / "_pr-comment-examples.md"
POINTER = "$BOT_DIR/.claude/skills/_pr-comment-examples.md"

# The three comment formats, the rules beside the moved samples, and the
# [RESOLVED] sample, which a rule sentence introduces: all stay in the protocol.
KEPT = (
    "[<bot-name>] [<lens>] [<severity>] <finding summary>",
    "[<bot-name>] [FORK F<N>] <option-letter> — <reasoning>",
    "**[<bot-name>] [VERDICT] <approve|request-changes|comment>** — reviewed at `<full sha>` — <one-line summary>",
    "This is an opinion, not a lock. Only the designated ratifier locks forks (see `decision-fork-lifecycle` protocol).",
    "A verdict is always the last comment a bot posts in a review cycle. Post findings first, verdict last.",
    "the finding's top-level comment is updated by prepending `[RESOLVED]`:",
    "proposes a new SkillValidator class...",
)

# The samples: in the partial, and no longer in the protocol.
MOVED = (
    "Phase 2b proposes a new SkillValidator class, but scripts/validate-skills.py already provides this functionality.",
    "Consolidate into the existing validator rather than building a parallel path.",
    "Phase 4d (documentation + changelog) is standard housekeeping",
    "Phase 2 skills are all M-sized and fully parallel",
    "(a) — clauDNA is the right home.",
    "(a) — plan-only scope.",
    "[VERDICT] request-changes** — reviewed at a1b2c3d.",
    "[VERDICT] approve** — reviewed at a1b2c3d.",
    "[VERDICT] comment** — reviewed at a1b2c3d.",
)


def test_the_protocol_keeps_its_formats_and_the_rules_beside_the_samples():
    fm, body = parse_frontmatter(PROTOCOL.read_text())
    assert fm["title"] == "PR Comment Hygiene"
    assert [k for k in KEPT if k not in body] == []


def test_the_samples_moved_to_the_partial():
    protocol, partial = PROTOCOL.read_text(), PARTIAL.read_text()
    assert [m for m in MOVED if m in protocol] == []
    assert [m for m in MOVED if m not in partial] == []
    assert protocol.count("```") % 2 == 0 and partial.count("```") % 2 == 0


def test_each_sample_leaves_a_pointer_to_a_partial_every_bot_copy_holds(fleet_dir):
    # Staging copies each shared partial into every bot's .claude/skills/
    # (config_staging.py; pinned by test_every_bot_copy_holds_every_shared_partial).
    assert PROTOCOL.read_text().count(POINTER) == 3
    partials = Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package()).skill_partials()
    assert partials[PARTIAL.name].resolve() == PARTIAL.resolve()


def test_a_bot_that_composes_the_protocol_carries_the_formats_and_the_pointer(fleet_dir):
    install_real_template(fleet_dir)
    shutil.copy(PROTOCOL, fleet_dir / "library" / "protocols" / PROTOCOL.name)
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      protocols: [pr-comment-hygiene]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet, Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package()))
    assert "### PR Comment Hygiene" in md and POINTER in md
    assert "This is an opinion, not a lock." in md
    assert "Phase 4d (documentation + changelog)" not in md and "[VERDICT] approve** — reviewed at a1b2c3d." not in md
