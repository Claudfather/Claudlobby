# tests/test_eval_protocols_library.py
"""The eval protocols bring their procedure with them (#717): `eval-workflow` and
`ab-gating-rollout` each require the `run-eval` skill, so a bot that composes either
one carries the skill without declaring it. Pinned as the check-in protocol's
requirement is (tests/test_checkin_library.py)."""

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
