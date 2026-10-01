"""#2010 — the `status` skill ships with the fleet's manager by default.

The manager's readout for the human (`library/skills/status`) reached a bot
only when its fleet.yaml listed it. It is now a ROLE default on `skills`
(`defaults.REGISTRY["skills"].roles`), keyed to the `manager` role that
`FleetConfig.manager_bots()` resolves: the fleet's one declared manager, which
owns intake, routing and follow-up. Teams group workers under it; they never
declare a second manager.

The fixture fleet has that shape: `lead` is the manager (and, with a local
worker to route, the leaf manager), and `worker-1` manages nobody. Names are
obviously fake; the repo is public.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from claudlobby import defaults
from claudlobby.composer import compose_bot, resolve_effective_skills
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from tests.conftest import install_real_template, make_paths
from tests.package_fixtures import source_package

LIBRARY = Path(__file__).resolve().parent.parent / "library"

#: The skill's own `tool_grants`, read from its frontmatter at compose time:
#: the bot's own-context reads, the PR read, and the reply tool.
STATUS_GRANTS = (
    "Bash(claudlobby --json brief)",
    "Bash(claudlobby --json fleet inbox)",
    "Bash(gh pr list *)",
    "mcp__plugin_telegram_telegram__reply",
)

#: What the skill used to grant. A default that reaches every manager must not
#: bring a CLI wildcard or a general `gh` beside fleet-ops' exact commands.
BROAD_GRANTS = ("Bash(claudlobby *)", "Bash(gh *)")

LEAD = "    lead:\n      expertise: [orchestration]\n"
WORKER = "    worker-1:\n      expertise: [software-engineering]\n"


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
    paths = make_paths(fleet_dir)
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
    def test_the_manager_gets_the_skill_and_its_narrow_grants(self, fleet_dir, monkeypatch):
        fleet, paths = _compose(fleet_dir, monkeypatch)
        assert fleet.manager_bots() == {"lead"}
        assert "status" not in fleet.bots["lead"].skills  # defaulted, never declared
        linked, allow = _equipped(paths, "lead")
        assert linked, "lead: .claude/skills/status is not linked"
        for grant in ("Skill(status)", *STATUS_GRANTS):
            assert grant in allow, f"lead: {grant} not granted"
        for grant in BROAD_GRANTS:
            assert grant not in allow, f"lead: the status default composed {grant}"

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
    def test_the_fleet_wide_switch_removes_it_from_the_manager(
        self, fleet_dir, monkeypatch
    ):
        _, paths = _compose(fleet_dir, monkeypatch)
        assert _equipped(paths, "lead")[0]
        _edit(
            fleet_dir,
            "  accounts:\n",
            "  system_defaults:\n    skills: false\n\n  accounts:\n",
        )
        _, paths = _compose(fleet_dir, monkeypatch)
        linked, allow = _equipped(paths, "lead")
        assert not linked, "lead: still linked after the fleet opted out"
        assert "Skill(status)" not in allow

    def test_the_managers_own_switch_removes_it(self, fleet_dir, monkeypatch):
        _edit(fleet_dir, LEAD, LEAD + "      system_defaults:\n        skills: false\n")
        _, paths = _compose(fleet_dir, monkeypatch)
        linked, allow = _equipped(paths, "lead")
        assert not linked and "Skill(status)" not in allow

    def test_another_bots_switch_does_not_reach_the_manager(
        self, fleet_dir, monkeypatch
    ):
        # The per-bot switch is that bot's own: set on the worker, the
        # manager's default still composes.
        _edit(fleet_dir, WORKER, WORKER + "      system_defaults:\n        skills: false\n")
        _, paths = _compose(fleet_dir, monkeypatch)
        assert _equipped(paths, "lead")[0], (
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
        # The leaf-manager role alone carries it no further: the leaf manager
        # is the manager, and a fleet without a local worker has none.
        assert defaults.resolve("skills", (defaults.ROLE_LEAF_MANAGER,)) == []
        assert (LIBRARY / "skills" / "status" / "SKILL.md").is_file()

    def test_the_skill_grants_only_what_it_declares(self):
        block = (LIBRARY / "skills" / "status" / "SKILL.md").read_text().split("---", 2)[1]
        grants = tuple(
            ln.strip().lstrip("-").strip().strip('"')
            for ln in block.splitlines()
            if ln.startswith("  - ")
        )
        assert grants == STATUS_GRANTS


class TestTheDefaultNeedsTheLibraryToProvideIt:
    """#2016 CI: the manager default named `status`, but a fleet whose library
    has no `status` skill then composed a dangling default and the validator
    flagged `skill-missing` (validator.py, over `resolve_effective_skills`). The
    default is gated on availability, so a library without the skill gets no
    default and no warning; the compose path still injects it where the library
    provides it (`TestTheManagerRoleDefault`)."""

    def _compose_without_status(self, fleet_dir: Path, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        install_real_template(fleet_dir)
        # deliberately do NOT copy library/skills/status in, and bind the
        # package base to the same status-less library: the source package's
        # own library provides status.
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        paths = Paths(
            root=fleet_dir,
            fleet_dir=fleet_dir,
            package=replace(source_package(), library=fleet_dir / "library"),
        )
        return fleet, paths

    def test_a_library_without_status_gives_the_manager_no_default(self, fleet_dir, monkeypatch):
        fleet, paths = self._compose_without_status(fleet_dir, monkeypatch)
        assert paths.find_library_dir("skills", "status") is None  # library truly lacks it
        skills = resolve_effective_skills(
            fleet.bots["lead"], fleet, paths, is_manager=True
        )
        assert "status" not in skills, skills

    def test_a_library_without_status_raises_no_skill_missing_warning(self, fleet_dir, monkeypatch):
        # skill-missing is emitted per effective skill with no library dir
        # (validator.py). No status in the effective set => nothing to flag
        # for it. fleet-ops is universal equipment, not this default.
        fleet, paths = self._compose_without_status(fleet_dir, monkeypatch)
        for bot in fleet.bots.values():
            is_mgr = bot.bot_id in fleet.manager_bots()
            effective = resolve_effective_skills(bot, fleet, paths, is_manager=is_mgr)
            missing = [s for s in effective if paths.find_library_dir("skills", s) is None]
            assert "status" not in missing, (bot.bot_id, missing)

    def test_with_status_present_the_manager_still_gets_it(self, fleet_dir, monkeypatch):
        # control: the gate does not suppress a default the library provides.
        fleet, paths = self._compose_without_status(fleet_dir, monkeypatch)
        dst = fleet_dir / "library" / "skills" / "status"
        shutil.copytree(LIBRARY / "skills" / "status", dst)
        skills = resolve_effective_skills(
            fleet.bots["lead"], fleet, paths, is_manager=True
        )
        assert "status" in skills, skills
