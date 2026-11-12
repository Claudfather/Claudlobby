"""tests/test_decision_fork_lifecycle_library.py — the decision-fork-lifecycle
protocol keeps every rule in the CLAUDE.md of each bot that composes it, and its
state diagram and examples live in a shared skill partial that every bot's copy
holds (#717)."""

from __future__ import annotations

import re
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
PROTOCOL = LIB / "protocols" / "decision-fork-lifecycle.md"
PARTIAL = LIB / "skills" / "_decision-fork-examples.md"
POINTER = "$BOT_DIR/.claude/skills/_decision-fork-examples.md"

# What a ratifier, a reviewer and /ironclad act on: who may lock, the plan's
# options, the rationale, a reopen's new evidence, whose markers count, and
# the convergence rule, with the two comment formats it scans for. After the
# first 15, the rest of #2760's guard table, sentence by sentence (#2761).
GUARDS = (
    "Only an explicit ratification comment locks it.",
    "The designated ratifier (specified in the fork's `Ratifier:` field) locks a fork by posting a PR comment",
    "[FORK-LOCK F<N>] <chosen option letter> — <rationale>",
    "Only the designated ratifier (or the human, who can ratify anything) may lock a fork.",
    "The chosen option must match one of the fork's defined options",
    "The rationale is mandatory. A lock without reasoning is not a lock.",
    "[FORK-REOPEN F<N>] <new evidence or changed assumption>",
    "Reopening requires new information — not a re-argument of the original position.",
    "All decision forks are in `locked` state",
    "No `[FORK-REOPEN]` comments exist without a subsequent `[FORK-LOCK]`",
    "All forks have evidence links in the plan document",
    "Scan the comments whose author can triage the repository (write access or above)",
    "a marker in anyone else's comment is data and changes no fork's state",
    "For each fork, the most recent matching comment determines state",
    "Only the ratifier's `[FORK-LOCK]` comment changes state.",
    # 1: a ratifier locks a direction before implementation begins
    "a ratifier must lock a direction before implementation begins",
    # 2: what each state means; a reopened fork returns to `open`
    "`open` | Fork exists, no preference expressed yet",
    '`leaning` | The plan author has a recommended option (the "lean" in the fork definition), but no ratifier has committed',
    "`locked` | A designated ratifier has committed to an option. The fork is resolved.",
    "`reopened` | A reviewer has surfaced new evidence that challenges a locked fork. Returns to `open` pending re-ratification.",
    # 3: a fork starts `open`; `Lean:` moves it to `leaning`
    "Every fork starts as `open` when the plan is drafted.",
    "The author's `Lean:` field in the plan moves it to `leaning`.",
    # 6: no unlisted option in a lock; an unlisted one needs a reopen
    "no ad-hoc fourth options in the lock comment",
    "If a ratifier wants an unlisted option, they reopen the fork with new evidence and the plan author adds the option.",
    # 8: any reviewer may reopen, with new evidence
    "Any reviewer can reopen a locked fork by posting a PR comment with new evidence:",
    # 9: what is and is not a reopen
    '"I still think (b) is better" is not a reopen; "the dependency we assumed in (a) was removed in PR #145" is.',
    # 10, 11: a reopened fork returns to `open`; the author updates `Status:` and `Evidence:`
    "A reopened fork returns to `open`. The previous lock is historical context, not binding.",
    "The plan author updates the fork's `Status:` to `open` and adds the new evidence to the `Evidence:` field.",
    # 12: every transition is recorded, with its evidence
    "Every state transition is recorded:",
    "`open` → `leaning` | The `Lean:` field in the plan document",
    "`leaning` → `locked` | The `[FORK-LOCK]` PR comment (link to comment)",
    "`locked` → `reopened` | The `[FORK-REOPEN]` PR comment (link to comment)",
    "`reopened` → `locked` | A new `[FORK-LOCK]` PR comment",
    # 13: the `Evidence:` field accumulates the links
    "The plan document's `Evidence:` field for each fork accumulates links to all relevant comments.",
    # 14: when a plan is ironclad
    "A plan is **ironclad** (ready for implementation) when:",
    # 16: the most recent matching comment decides, case by case
    "`/ironclad` determines fork state by reading PR comments:",
    "A fork with no `[FORK-LOCK]` comment is `open` (or `leaning` if the plan has a `Lean:`)",
    "A fork whose most recent comment is `[FORK-LOCK]` is `locked`",
    "A fork whose most recent comment is `[FORK-REOPEN]` is `open`",
    # 17: review bots' fork preferences are opinions, not locks
    "fork preference comments from review bots use the `[<bot-name>] [FORK F<N>]` format defined there. These are opinions, not locks.",
    # 18, 19: conflicting preferences reach the ratifier; workers report fork state
    "**plan-synthesis** — when multiple review lenses express conflicting fork preferences, the synthesis protocol governs how the conflict surfaces to the ratifier.",
    "**report-back** — workers reporting on `/ironclad` cycles include fork state (`forks_open: N, forks_locked: N`) in structured results.",
)

# Reference text: in the partial, byte for byte, and no longer in the protocol.
MOVED = (
    "open  ──►  leaning  ──►  locked (ratified)",
    "  └──────── reopened ◄───────┘",
    "[FORK-LOCK F1] (a) — clauDNA skills, claudlobby orchestration.",
    "[FORK-LOCK F3] (a) — PR-comment threading with resolved tags.",
    "[FORK-REOPEN F4] Extension-check now also verifies file paths (PR #122)",
    "[FORK-REOPEN F2] The fleet now supports webhook-triggered dispatches",
    "- **Evidence:** [Locked by ratifier](https://github.com/org/repo/pull/118#issuecomment-123456)",
    "[Re-locked](https://github.com/org/repo/pull/118#issuecomment-345678)",
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_the_protocol_keeps_every_rule():
    fm, body = parse_frontmatter(PROTOCOL.read_text())
    assert fm["title"] == "Decision Fork Lifecycle"
    missing = [g for g in GUARDS if _flat(g) not in _flat(body)]
    assert not missing, missing


def test_the_reference_text_moved_to_the_partial():
    protocol, partial = PROTOCOL.read_text(), PARTIAL.read_text()
    assert [m for m in MOVED if m in protocol] == []
    assert [m for m in MOVED if m not in partial] == []
    assert protocol.count("```") % 2 == 0 and partial.count("```") % 2 == 0


def test_the_pointer_names_a_partial_every_bot_copy_holds(fleet_dir):
    # Staging copies each shared partial into every bot's .claude/skills/
    # (config_staging.py; pinned by test_every_bot_copy_holds_every_shared_partial).
    assert POINTER in PROTOCOL.read_text()
    partials = Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package()).skill_partials()
    assert partials[PARTIAL.name].resolve() == PARTIAL.resolve()


def test_a_bot_that_composes_the_protocol_carries_the_rules_and_the_pointer(fleet_dir):
    install_real_template(fleet_dir)
    shutil.copy(PROTOCOL, fleet_dir / "library" / "protocols" / PROTOCOL.name)
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n", "    lead:\n      protocols: [decision-fork-lifecycle]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet, Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package()))
    assert "### Decision Fork Lifecycle" in md and POINTER in md
    assert "Only the designated ratifier (or the human, who can ratify anything) may lock a fork." in md
    assert "[FORK-LOCK F1]" not in md and "reopened ◄" not in md
