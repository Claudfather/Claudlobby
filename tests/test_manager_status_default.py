"""#2010 — the `status` skill ships with every composed manager by default.

The manager's readout for the human (`library/skills/status`) reached a bot
only when its fleet.yaml listed it. It is now a ROLE default on `skills`
(`defaults.REGISTRY["skills"].roles`), keyed to the `manager` role that
`FleetConfig.manager_bots()` resolves: every manager, a coordinator whose
reports are all managers included. The `leaf-manager` role (the `checkin`
overlay's) would leave a coordinator out, and the skill's audience is whoever
answers the human.

One fixture carries all three shapes, as the `checkin` default's tests do
(tests/test_defaults_registry.py): `lead` manages a worker (a leaf manager),
`coord` manages only `lead` (a coordinator), and `worker-1` manages nobody.
Names are obviously fake; the repo is public.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudlobby import defaults
from claudlobby.composer import compose_bot, resolve_effective_skills
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from tests.conftest import install_real_template

LIBRARY = Path(__file__).resolve().parent.parent / "library"

#: The skill's own `tool_grants`, read from its frontmatter at compose time.
STATUS_GRANTS = (
    "Bash(claudlobby *)",
    "Bash(gh *)",
    "mcp__plugin_telegram_telegram__reply",
)

LEAD = "    lead:\n      expertise: [orchestration]\n"


def _edit(fleet_dir: Path, old: str, new: str) -> None:
    """Replace one exact span of the fixture's fleet.yaml, refusing a miss.

    A replacement that matches nothing leaves the fixture unchanged and the
    test then measures the default fleet, so the count is asserted.
    """
    path = fleet_dir / "fleet.yaml"
    text = path.read_text()
    assert text.count(old) == 1, (
        f"fixture edit matched {text.count(old)} times: {old!r}"
    )
    path.write_text(text.replace(old, new))


def _add_coordinator(fleet_dir: Path) -> None:
    """`coord` manages a team of one, `lead`, who is itself a manager."""
    _edit(
        fleet_dir,
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n",
        "  teams:\n    eng:\n      manager: lead\n      workers: [worker-1]\n"
        "    top:\n      manager: coord\n      workers: [lead]\n",
    )
    _edit(
        fleet_dir,
        "  bots:\n    lead:\n",
        "  bots:\n    coord:\n      expertise: [orchestration]\n    lead:\n",
    )


def _compose(fleet_dir: Path, monkeypatch):
    """Compose every bot of the fixture fleet with the REAL `status` skill,
    so the grants asserted are the ones its frontmatter declares."""
    monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
    monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
    install_real_template(fleet_dir)
    dst = fleet_dir / "library" / "skills" / "status"
    if not dst.exists():
        shutil.copytree(LIBRARY / "skills" / "status", dst)
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    for bot in fleet.bots.values():
        compose_bot(bot, fleet, paths, log=lambda m: None)
    return fleet, paths


def _equipped(paths: Paths, bot_id: str) -> tuple[bool, list[str]]:
    """Whether the bot's `.claude/skills/status` is linked, and its allow list."""
    bot_dir = paths.bot_runtime(bot_id)
    settings = json.loads((bot_dir / ".claude" / "settings.local.json").read_text())
    linked = (bot_dir / ".claude" / "skills" / "status").is_symlink()
    return linked, settings["permissions"]["allow"]


class TestTheManagerRoleDefault:
    def test_every_manager_gets_the_skill_and_its_grants(self, fleet_dir, monkeypatch):
        _add_coordinator(fleet_dir)
        fleet, paths = _compose(fleet_dir, monkeypatch)
        assert fleet.manager_bots() == {"lead", "coord"}
        # coord is not a leaf manager, so the checkin role would have missed it.
        assert fleet.leaf_manager_bots() == {"lead"}
        for bot_id in ("lead", "coord"):
            assert (
                "status" not in fleet.bots[bot_id].skills
            )  # defaulted, never declared
            linked, allow = _equipped(paths, bot_id)
            assert linked, f"{bot_id}: .claude/skills/status is not linked"
            for grant in ("Skill(status)", *STATUS_GRANTS):
                assert grant in allow, f"{bot_id}: {grant} not granted"

    def test_a_worker_is_untouched(self, fleet_dir, monkeypatch):
        # Measured as a DIFFERENCE, not an absence: the worker's settings and
        # skills dir must be byte-identical whether the default is on or off.
        # An absence check alone would pass for a worker whose grants came
        # from somewhere else, and would pass vacuously if nothing composed.
        _, paths = _compose(fleet_dir, monkeypatch)
        # Positive control, so the comparison below is not two empty states:
        # the default is on, and it reached the manager.
        assert _equipped(paths, "lead")[0]
        worker = paths.bot_runtime("worker-1") / ".claude"
        on = (
            (worker / "settings.local.json").read_text(),
            sorted(p.name for p in (worker / "skills").iterdir()),
        )
        _edit(
            fleet_dir,
            "  accounts:\n",
            "  system_defaults:\n    skills: false\n\n  accounts:\n",
        )
        _, paths = _compose(fleet_dir, monkeypatch)
        off = (
            (worker / "settings.local.json").read_text(),
            sorted(p.name for p in (worker / "skills").iterdir()),
        )
        assert on == off
        assert not _equipped(paths, "worker-1")[0]
        # And the same edit did remove it from the manager.
        assert not _equipped(paths, "lead")[0]

    def test_a_manager_that_lists_status_composes_it_once(self, fleet_dir, monkeypatch):
        _edit(fleet_dir, LEAD, LEAD + "      skills: [status]\n")
        fleet, paths = _compose(fleet_dir, monkeypatch)
        lead = fleet.bots["lead"]
        assert lead.skills == ["status"]
        effective = resolve_effective_skills(lead, fleet, paths, is_manager=True)
        assert effective.count("status") == 1
        linked, allow = _equipped(paths, "lead")
        assert linked
        for grant in ("Skill(status)", *STATUS_GRANTS):
            assert allow.count(grant) == 1, (
                f"{grant} granted {allow.count(grant)} times"
            )


class TestTheOptOuts:
    def test_the_fleet_wide_switch_removes_it_from_every_manager(
        self, fleet_dir, monkeypatch
    ):
        _add_coordinator(fleet_dir)
        _, paths = _compose(fleet_dir, monkeypatch)
        assert _equipped(paths, "lead")[0] and _equipped(paths, "coord")[0]
        _edit(
            fleet_dir,
            "  accounts:\n",
            "  system_defaults:\n    skills: false\n\n  accounts:\n",
        )
        _, paths = _compose(fleet_dir, monkeypatch)
        for bot_id in ("lead", "coord"):
            linked, allow = _equipped(paths, bot_id)
            assert not linked, f"{bot_id}: still linked after the fleet opted out"
            assert "Skill(status)" not in allow

    def test_a_per_bot_switch_removes_it_from_that_bot_only(
        self, fleet_dir, monkeypatch
    ):
        _add_coordinator(fleet_dir)
        _edit(fleet_dir, LEAD, LEAD + "      system_defaults:\n        skills: false\n")
        _, paths = _compose(fleet_dir, monkeypatch)
        linked, allow = _equipped(paths, "lead")
        assert not linked and "Skill(status)" not in allow
        assert _equipped(paths, "coord")[0], (
            "the opt-out reached a bot that did not ask for it"
        )

    def test_a_per_bot_switch_does_not_remove_a_declared_skill(
        self, fleet_dir, monkeypatch
    ):
        # The switch turns off the DEFAULT. A bot that lists the skill itself
        # asked for it, as `system_defaults.protocols: false` leaves a declared
        # protocol in place.
        _edit(
            fleet_dir,
            LEAD,
            LEAD
            + "      skills: [status]\n      system_defaults:\n        skills: false\n",
        )
        _, paths = _compose(fleet_dir, monkeypatch)
        assert _equipped(paths, "lead")[0]

    @pytest.mark.parametrize(
        "key", ["protocols", "guardrails", "hooks", "enabled", "not_a_type"]
    )
    def test_a_per_bot_key_it_does_not_honour_is_refused(self, fleet_dir, key):
        # A key the per-bot mapping silently dropped would read exactly like a
        # working opt-out (#1168 Phase 3 finding 2), so it is refused by name.
        _edit(fleet_dir, LEAD, LEAD + f"      system_defaults:\n        {key}: false\n")
        with pytest.raises(ValueError, match="system_defaults"):
            load_fleet(fleet_dir / "fleet.yaml")

    @pytest.mark.parametrize("value", ["false", "true", "[skills]"])
    def test_a_per_bot_value_that_is_not_a_mapping_is_refused(self, fleet_dir, value):
        _edit(fleet_dir, LEAD, LEAD + f"      system_defaults: {value}\n")
        with pytest.raises(ValueError, match="system_defaults"):
            load_fleet(fleet_dir / "fleet.yaml")


class TestTheRegistry:
    def test_status_is_a_manager_role_overlay_and_nothing_else(self):
        assert defaults.resolve("skills") == []
        assert defaults.resolve("skills", (defaults.ROLE_MANAGER,)) == ["status"]
        # The leaf-manager role alone carries it no further: every leaf manager
        # is also a manager, and a coordinator is only a manager.
        assert defaults.resolve("skills", (defaults.ROLE_LEAF_MANAGER,)) == []
        assert (LIBRARY / "skills" / "status" / "SKILL.md").is_file()
