# tests/test_eval_protocols_library.py
"""The eval protocols bring their procedure with them (#717): `eval-workflow` and
`ab-gating-rollout` each require the `run-eval` skill, so a bot that composes either
one carries the skill without declaring it. Pinned as the check-in protocol's
requirement is (tests/test_checkin_library.py). Each also keeps its rules in its own
text, one `in` check per kept rule sentence (#2761)."""

import shutil
from pathlib import Path

import pytest

from claudlobby.composer import resolve_effective_skills
from claudlobby.config import load_fleet
from claudlobby.loader import parse_frontmatter
from claudlobby.paths import Paths
from tests.package_fixtures import source_package

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "library"
EVAL_PROTOCOLS = ("eval-workflow", "ab-gating-rollout")


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_the_protocol_requires_the_run_eval_skill(name):
    fm, _body = parse_frontmatter((LIB / "protocols" / f"{name}.md").read_text())
    assert fm["requires"] == {"skills": ["run-eval"]}


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_a_bot_that_declares_only_the_protocol_carries_the_skill(fleet_dir, name):
    shutil.copy(LIB / "protocols" / f"{name}.md", fleet_dir / "library" / "protocols" / f"{name}.md")
    text = (fleet_dir / "fleet.yaml").read_text().replace(
        "    worker-1:\n", f"    worker-1:\n      protocols: [{name}]\n", 1)
    (fleet_dir / "fleet.yaml").write_text(text)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    bot = fleet.bots["worker-1"]
    assert name in bot.protocols and "run-eval" not in bot.skills  # required, never declared
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir, package=source_package())
    is_manager = bot.bot_id in fleet.manager_bots()
    assert "run-eval" in resolve_effective_skills(bot, fleet, paths, is_manager=is_manager)


def _flat(text: str) -> str:
    """Whitespace-collapsed, as tests/test_checkin_library.py reads a body."""
    return " ".join(text.split())


# The rules each protocol keeps in its own text, word for word: #2753's guard table
# places rows 1, 2 and 10 in `eval-workflow` and rows 2, 10, 13, 15, 18 and 26-30 in
# `ab-gating-rollout`, and its review checked each against the text. A later trim that
# drops one fails here; a rule reworded on purpose changes its line here in the same PR.
KEPT = {
    "eval-workflow": (
        "An eval exists to change a decision: if no result would change what you ship, do not run one.",
        "When one is due, invoke the `run-eval` skill **before the first run**.",
        "It starts by writing down what would convince you, which is worth nothing once you have seen results, and it holds every other rule of the eval.",
        "**INCONCLUSIVE is a real result and is never rounded up to PASS.**",
    ),
    "ab-gating-rollout": (
        "A change whose justification is a measurable effect lands on evidence, not argument.",
        'The gate fires when the PR body states a number, a percentage or a comparative ("cheaper", "faster", "fewer tokens"), when a protocol, prompt, model or library change exists to change behaviour, or when landing it would change what the fleet spends.',
        "It does not fire for correctness fixes, refactors that claim no effect, or docs.",
        "An effect you cannot state so that an eval could refute it is no claim: drop it from the PR body and land the change on its other merits.",
        "When the gate fires, the PR needs a verdict from the `run-eval` skill, which also says what each outcome does to the PR.",
        "A verdict counts only if its statistic was registered before the first run, no axis or threshold changed after the results, and its pins (model, artifact, battery and harness versions) still match the running system.",
        "**INCONCLUSIVE never passes the gate.**",
    ),
}


@pytest.mark.parametrize("name", EVAL_PROTOCOLS)
def test_the_protocol_keeps_its_rule_sentences(name):
    _fm, body = parse_frontmatter((LIB / "protocols" / f"{name}.md").read_text())
    missing = [s for s in KEPT[name] if _flat(s) not in _flat(body)]
    assert not missing, missing
