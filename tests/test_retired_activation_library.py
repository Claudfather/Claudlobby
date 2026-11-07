"""The composed library files that told bots to stage `config plan` and hand the
operator `host activate` name what the git runtime does instead: the operator's
`host update apply`, from a shell outside any bot session (#2751). Every rule
they carried stays."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from claudlobby.composer import compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from tests.conftest import install_real_template
from tests.package_fixtures import source_package

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
GUARDRAIL = LIB / "guardrails" / "claudlobby-dev-in-projects.md"
CANARY = LIB / "protocols" / "canary-rollout.md"
INTEGRATIONS = tuple(LIB / "integrations" / f"{name}.md" for name in (
    "google-analytics", "google-search-console", "meta-ads", "posthog"))
ADD_BOT = LIB / "skills" / "add-bot" / "SKILL.md"
FILES = (GUARDRAIL, CANARY, *INTEGRATIONS, ADD_BOT)

# The sealed-release route the git runtime retired (#2695): main's parser has
# neither `config plan` nor `host activate`, and no plan or release to name.
RETIRED = re.compile(r"\bconfig plan\b|\bhost activate\b|PLAN_ID|<plan-id>|RELEASE_ID|--release\b|\bsealed\b")

EQUIPPING = ("Then the operator applies the reviewed change with `claudlobby host update apply FULL_COMMIT` "
             "from a shell outside any bot session.")

# Each rule the files carried, as it reads now.
GUARDS = {
    GUARDRAIL: (
        "use your `projects/` checkout — never branch or commit from the active data root at `{{CLAUDLOBBY_ROOT}}`",
        "Give a reviewed config change to the operator, who applies it with "
        "`claudlobby --root <data-root> host update apply FULL_COMMIT` from their own shell outside a generated bot session.",
    ),
    CANARY: (
        "**Hard gate.** Mandatory for every runtime change, before merge.",
        "**Strong default.** Do it when a bad fleet-wide rollout would hurt the fleet",
        "names its check before merge; the merger posts PASS, FAIL or PENDING once the change is live.",
        "the canary is the **expected default** when that change ships *live across the fleet*",
        "A composed config change is different: an update covers the whole installation, even when only one bot's source changed.",
        "Library, skill and permission changes use the same Git update as code. They are not a restart-free exception.",
        "would a bad version of this change hurt more than one running bot at once? Yes → canary. No → ship it.",
        "Never share a production bot directory or token.",
        "**The operator prepares the candidate with `host setup`, then `fleet setup --plan-only`.**",
        "Use that installation's stable CLI and an explicit `--root` on every command.",
        "Review `config diff FINGERPRINT`: every unit, path and restart must belong to the canary.",
        "**The operator continues that operation** with `host update continue` from an external shell; "
        "a later candidate takes `host update apply FULL_COMMIT`, which is effectful, not a preview.",
        "Put the affected skill, grants and config in one candidate, then exercise real delegation and the affected operation.",
        "Observe the receiver, command result and durable record; process liveness alone is insufficient.",
        "Fix observed failures and repeat only the affected checks.",
        "**Review the production update separately.** `host update apply` covers the whole installation; "
        "there is no single-bot composition or update shortcut.",
        "Existing no-restart holds block that production update, not independent canary work.",
        "Honor the operator's rollout authorization and work-in-progress holds.",
        "Verify the affected paths and the commit `host update status` reports after rollout; never repair generated files by hand.",
        "Then run each merged PR's rollout check against it",
        "read what each does before running it, and run them only for a PR whose author can triage the repository",
    ),
    ADD_BOT: (
        "Ask one or two questions at a time, not all at once.",
        "give the operator this exact command to run from an operator shell outside the generated bot session",
        "claudlobby --root <data-root> host update apply FULL_COMMIT",
        "The update owns composition and native enrollment; `bot create` only writes source.",
        "check the bot's native enrollment and session readiness before reporting it as running",
    ),
    **{path: (EQUIPPING, "read-only tools compose in as auto-allowed") for path in INTEGRATIONS},
}


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_no_composed_file_names_a_retired_command():
    found = {p.relative_to(REPO).as_posix(): RETIRED.findall(p.read_text()) for p in FILES}
    assert {k: v for k, v in found.items() if v} == {}


def test_every_rule_stays():
    missing = {p.relative_to(REPO).as_posix(): [g for g in needles if _flat(g) not in _flat(p.read_text())]
               for p, needles in GUARDS.items()}
    assert {k: v for k, v in missing.items() if v} == {}


def test_the_protocol_and_the_skill_say_update_not_activation():
    assert [p.name for p in (CANARY, ADD_BOT) if re.search(r"activat", p.read_text(), re.I)] == []


def test_each_walkthrough_step_names_the_operators_update():
    # The numbered "Wire/Attach the MCP" step points at *Equipping a bot*.
    for path in INTEGRATIONS:
        step = next(line for line in path.read_text().split("\n\n") if "*Equipping a bot*)" in line)
        assert "host update apply" in step, path.name


def test_a_bot_composing_the_guardrail_and_the_protocol_is_told_the_update(fleet_dir):
    install_real_template(fleet_dir)
    shutil.copy(GUARDRAIL, fleet_dir / "library" / "guardrails" / GUARDRAIL.name)
    shutil.copy(CANARY, fleet_dir / "library" / "protocols" / CANARY.name)
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    lead:\n",
        "    lead:\n      protocols: [canary-rollout]\n      guardrails: [claudlobby-dev-in-projects]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    md = compose_claude_md(fleet.bots["lead"], fleet,
                           Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package()))
    assert "### Canary Rollout" in md and "### Claudlobby dev work uses projects/ checkout" in md
    assert "host update apply FULL_COMMIT` from their own shell outside a generated bot session." in md
    assert "with `host update continue` from an external shell" in md
    assert "config plan --release" not in md
    assert "host activate <plan-id>" not in md and "host activate PLAN_ID --install-directory" not in md
