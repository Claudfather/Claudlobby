"""Leaf-manager defaults apply to the declared fleet manager with local workers.

Reporting links across fleets do not create another local manager or worker.
The coordinator and worker fleets below remain distinct accountability scopes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudlobby import defaults
from claudlobby.composer import compose_bot, resolve_effective_protocols
from claudlobby.config import BotConfig, FleetConfig, TeamConfig, load_fleet
from tests.package_fixtures import source_package
from claudlobby.paths import Paths
from claudlobby.plane.registry_emit import bot_payload
from claudlobby.validator import validate

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fleet-shape factories — pure FleetConfig construction, no disk I/O. The
# predicate never touches library files or paths, so none of these need the
# fleet_dir fixture (test_composer.py's `_fleet` helper precedent).
# ---------------------------------------------------------------------------


def _bot(bot_id: str, **kw) -> BotConfig:
    return BotConfig(bot_id=bot_id, name=bot_id, expertise=["eng"], **kw)


def _fleet_team_manager_with_worker() -> FleetConfig:
    """lead manages team eng (worker-1) — the ordinary single-team shape.
    lead is a leaf manager; worker-1 is never a manager at all."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="lead",
        bots={"lead": _bot("lead"), "worker-1": _bot("worker-1")},
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["worker-1"])},
    )


def _fleet_coordinator() -> tuple[FleetConfig, FleetConfig]:
    """A singleton coordinator reports across fleets to a manager with workers."""
    coordinator = FleetConfig(
        name="coord-fleet", service_prefix="p.coord", manager="coord",
        bots={"coord": _bot("coord", manages=["lead"])},
    )
    return coordinator, _fleet_team_manager_with_worker()


def _fleet_empty_team() -> FleetConfig:
    """lead manages a team with no workers -> not leaf (no in-fleet report at all)."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="lead",
        bots={"lead": _bot("lead")},
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=[])},
    )


def _fleet_manages_only_self() -> FleetConfig:
    """A singleton manager gains no local worker by reporting to itself."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="solo",
        bots={"solo": _bot("solo", manages=["solo"])},
    )


def _fleet_manager_via_teams_and_manages() -> FleetConfig:
    """Team and reporting metadata coexist under the one declared manager."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="lead",
        bots={
            "lead": _bot("lead", manages=["worker-2"]),
            "worker-1": _bot("worker-1"),
            "worker-2": _bot("worker-2"),
        },
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["worker-1"])},
    )


def _fleet_team_worker_absent_from_bots() -> FleetConfig:
    """An undeclared team member is not a local bot and cannot make a leaf."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="lead",
        bots={"lead": _bot("lead")},
        teams={"eng": TeamConfig(name="eng", manager="lead", workers=["ghost"])},
    )


def _fleet_cross_fleet_target_only() -> FleetConfig:
    """Cross-fleet reporting metadata cannot create a local worker."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="coord",
        bots={"coord": _bot("coord", manages=["elsewhere-manager"])},
    )


def _fleet_cross_fleet_target_and_in_fleet_worker() -> FleetConfig:
    """F5's pair: manages: names one cross-fleet target (dropped) and one
    in-fleet non-manager (grunt) -> leaf, because the in-fleet report is what
    decides it, not the cross-fleet one."""
    return FleetConfig(
        name="t",
        service_prefix="p",
        manager="coord",
        bots={
            "coord": _bot("coord", manages=["elsewhere-manager", "grunt"]),
            "grunt": _bot("grunt"),
        },
    )


#: name -> factory, for the subset property test below.
ALL_SHAPES = [
    ("team_manager_with_worker", _fleet_team_manager_with_worker),
    ("coordinator", lambda: _fleet_coordinator()[0]),
    ("empty_team", _fleet_empty_team),
    ("manages_only_self", _fleet_manages_only_self),
    ("manager_via_teams_and_manages", _fleet_manager_via_teams_and_manages),
    ("team_worker_absent_from_bots", _fleet_team_worker_absent_from_bots),
    ("cross_fleet_target_only", _fleet_cross_fleet_target_only),
    (
        "cross_fleet_target_and_in_fleet_worker",
        _fleet_cross_fleet_target_and_in_fleet_worker,
    ),
]


class TestLeafManagerBots:
    def test_a_team_manager_with_a_worker_is_leaf(self):
        fleet = _fleet_team_manager_with_worker()
        assert fleet.leaf_manager_bots() == {"lead"}

    def test_a_cross_fleet_coordinator_is_not_leaf_but_its_worker_fleet_manager_is(self):
        coordinator, workers = _fleet_coordinator()
        assert coordinator.manager_bots() == {"coord"}
        assert coordinator.leaf_manager_bots() == set()
        assert workers.manager_bots() == workers.leaf_manager_bots() == {"lead"}

    def test_a_manager_with_an_empty_team_is_not_leaf(self):
        fleet = _fleet_empty_team()
        assert fleet.manager_bots() == {"lead"}
        assert fleet.leaf_manager_bots() == set()

    def test_a_bot_that_manages_only_itself_is_not_leaf(self):
        fleet = _fleet_manages_only_self()
        assert fleet.manager_bots() == {"solo"}
        assert fleet.leaf_manager_bots() == set()

    def test_grouping_and_reporting_metadata_preserve_one_declared_manager(
        self,
    ):
        fleet = _fleet_manager_via_teams_and_manages()
        assert fleet.manager_bots() == {"lead"}
        assert fleet.leaf_manager_bots() == {"lead"}

    def test_a_team_worker_absent_from_fleet_bots_is_not_leaf(self):
        fleet = _fleet_team_worker_absent_from_bots()
        assert fleet.manager_bots() == {"lead"}
        assert fleet.leaf_manager_bots() == set()

    def test_a_worker_is_never_leaf(self):
        fleet = _fleet_team_manager_with_worker()
        assert "worker-1" not in fleet.manager_bots()
        assert "worker-1" not in fleet.leaf_manager_bots()

    def test_a_cross_fleet_manages_target_does_NOT_make_a_manager_leaf(self):
        fleet = _fleet_cross_fleet_target_only()
        assert fleet.manager_bots() == {"coord"}
        assert fleet.leaf_manager_bots() == set()

    def test_a_manager_with_a_cross_fleet_target_AND_an_in_fleet_worker_is_leaf(self):
        fleet = _fleet_cross_fleet_target_and_in_fleet_worker()
        assert fleet.leaf_manager_bots() == {"coord"}

    def test_the_single_team_fleet_shape_names_the_same_bot_as_manager_bots(self):
        # spec §10: "where a fleet has one team and no manages: chain — the
        # shape every manifest inspected so far has — the two roles name the
        # same bot."
        fleet = _fleet_team_manager_with_worker()
        assert fleet.leaf_manager_bots() == fleet.manager_bots()

    @pytest.mark.parametrize(
        "fleet_factory", [f for _, f in ALL_SHAPES], ids=[n for n, _ in ALL_SHAPES]
    )
    def test_leaf_manager_is_a_subset_of_manager_bots(self, fleet_factory):
        fleet = fleet_factory()
        assert fleet.leaf_manager_bots() <= fleet.manager_bots()


# ---------------------------------------------------------------------------
# The composer — resolve_effective_protocols passes BOTH roles for a leaf,
# only the first for a coordinator (composer.py, resolve_effective_protocols).
# ---------------------------------------------------------------------------


class TestComposerPassesBothRoles:
    def test_the_composer_passes_both_roles(self, tmp_path, monkeypatch):
        from dataclasses import replace

        # A probe registry entry with one role-scoped member per role, so the
        # RETURNED protocol list reveals exactly which roles were resolved —
        # observable behavior through the real composer path, not a spy on a
        # private call. Task 3 populates the real "leaf-manager" overlay;
        # here nothing does yet, so this proves the WIRING, independent of it.
        probe = replace(
            defaults.REGISTRY["protocols"],
            roles={
                defaults.ROLE_MANAGER: ("manager-only-protocol",),
                defaults.ROLE_LEAF_MANAGER: ("leaf-only-protocol",),
            },
        )
        monkeypatch.setitem(defaults.REGISTRY, "protocols", probe)
        paths = Paths(root=tmp_path, fleet_dir=None, package=source_package())

        leaf_fleet = _fleet_team_manager_with_worker()
        leaf_result = resolve_effective_protocols(
            leaf_fleet.bots["lead"], leaf_fleet, paths, is_manager=True
        )
        assert "manager-only-protocol" in leaf_result
        assert "leaf-only-protocol" in leaf_result

        coord_fleet, _worker_fleet = _fleet_coordinator()
        coord_result = resolve_effective_protocols(
            coord_fleet.bots["coord"], coord_fleet, paths, is_manager=True
        )
        assert "manager-only-protocol" in coord_result
        assert "leaf-only-protocol" not in coord_result


# ---------------------------------------------------------------------------
# The validator surface — spec §10 / §5 step 1: declaring `checkin` on a
# WORKER is a WARNING that says why, never an error — the skill still links,
# /checkin still runs by hand, and the trigger will never inject into it
# (`bot_is_manager` is false for a worker). Declaring it on a COORDINATOR
# gets a DIFFERENT warning: `bot_is_manager` reads true for a coordinator
# too, so once the skill links (which the declaration itself does) both of
# the trigger's gates pass, and the beat WILL inject once the fleet arms
# manager-checkin — the warning says that instead of claiming it never will.
# ---------------------------------------------------------------------------


def _make_paths(root):
    return Paths(root=root, fleet_dir=None, package=source_package())


def _write_checkin_library(fleet_dir):
    """A minimal checkin protocol + skill, so the new leaf-manager warning is
    the only checkin-related warning in the report (isolates the assertion
    from the pre-existing "protocol/skill not found" warnings)."""
    (fleet_dir / "library" / "protocols" / "checkin.md").write_text(
        "---\ntitle: Check-in\n---\n\n# Check-in\n\nProtocol body.\n"
    )
    skill_dir = fleet_dir / "library" / "skills" / "checkin"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: checkin\n---\n\n# Check-in\n\nSkill body.\n"
    )


class TestCheckinValidatorSurface:
    def test_declaring_checkin_on_a_worker_warns_and_says_why(
        self, fleet_dir, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        _write_checkin_library(fleet_dir)
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "expertise: [software-engineering]",
            "expertise: [software-engineering]\n      protocols: [checkin]",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        assert "worker-1" not in fleet.leaf_manager_bots()

        report = validate(fleet, _make_paths(fleet_dir))
        matches = [w for w in report.warnings if "worker-1" in w and "checkin" in w]
        assert matches, report.warnings
        assert any("worker" in w for w in matches)
        assert any("/checkin" in w and "by hand" in w for w in matches)

    def test_declaring_checkin_on_a_non_leaf_warns_and_says_why(
        self, fleet_dir, monkeypatch
    ):
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        _write_checkin_library(fleet_dir)
        workers, _md = load_fleet(fleet_dir / "fleet.yaml")
        assert workers.leaf_manager_bots() == {"lead"}

        # Keep cross-fleet coordination distinct from the lead's worker fleet.
        coordinator_dir = fleet_dir.parent / "coordinator"
        coordinator_dir.mkdir()
        coordinator_yaml = coordinator_dir / "fleet.yaml"
        coordinator_yaml.write_text(
            "fleet:\n  name: coordinator\n  manager: coord\n  bots:\n"
            "    coord:\n      expertise: [orchestration]\n"
            "      manages: [lead]\n      protocols: [checkin]\n"
        )
        fleet, _md = load_fleet(coordinator_yaml)
        assert fleet.manager_bots() == {"coord"}
        assert fleet.leaf_manager_bots() == set()

        report = validate(fleet, _make_paths(coordinator_dir))
        matches = [w for w in report.warnings if "coord" in w and "checkin" in w]
        assert matches, report.warnings
        assert any("manager with no local workers" in w for w in matches)
        # Unlike the worker case, this bot's beat WILL fire once armed — the
        # skill links because it was declared, and bot_is_manager reads true
        # for a coordinator too, so both of the trigger's gates pass.
        assert any("leaf-manager default" in w for w in matches)
        assert any("WILL inject" in w and "manager-checkin" in w for w in matches)
        worker_report = validate(workers, _make_paths(fleet_dir))
        assert not any("lead" in w and "checkin" in w for w in worker_report.warnings)


# ---------------------------------------------------------------------------
# The default must not double a hand-declared protocol (fix wave A group 4,
# mutant `default-doubles-a-hand-declared-protocol`): a leaf manager that
# ALSO hand-declares `protocols: [checkin]` and `skills: [checkin]` composes
# the check-in ONCE, even though the leaf-manager registry default
# (`REGISTRY["protocols"].roles = {"leaf-manager": ("checkin",)}`) tries to
# add the very name the bot already declared.
# ---------------------------------------------------------------------------


class TestHandDeclaredCheckinComposesOnce:
    def test_a_leaf_manager_hand_declaring_checkin_composes_it_once(
        self, fleet_dir, monkeypatch
    ):
        """Removing `and name not in protocol_names` from
        `resolve_effective_protocols` (composer.py) was verified, empirically,
        to leave THREE composed artifacts unchanged: `load_library_items_
        overlay`'s own `seen_paths` dedup still renders the "### Check-in"
        heading once, `resolve_effective_skills`'s `if required not in skills`
        guard still yields one linked skill, and `link_skills`'s own `linked`
        dict still creates one symlink -- each of those three consumers
        already dedupes its OWN input, independent of this guard. What the
        mutation actually doubles, confirmed the same way, is the RAW return
        value of `resolve_effective_protocols` itself (`['report-back',
        'checkin', 'checkin']`) and everything that reads it WITHOUT its own
        dedup -- the plane registry keyframe's `equipment.protocols`
        (`bot_payload`) chief among them, since `sorted()` does not drop
        duplicates. So this test pins all four: the three composed artifacts
        the guard's removal turns out NOT to touch (regression coverage for
        the observable behavior staying correct), plus the registry keyframe,
        which is the one that actually goes red under that mutation.
        """
        monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
        monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")
        _write_checkin_library(fleet_dir)
        yaml_text = (fleet_dir / "fleet.yaml").read_text()
        yaml_text = yaml_text.replace(
            "    lead:\n      expertise: [orchestration]\n",
            "    lead:\n      expertise: [orchestration]\n"
            "      protocols: [checkin]\n"
            "      skills: [checkin]\n",
        )
        (fleet_dir / "fleet.yaml").write_text(yaml_text)
        # The fleet_dir fixture ships a minimal claude.md.j2 stub with no
        # "## Protocols" section at all; install the real template so the
        # check-in heading is actually there to count.
        (fleet_dir / "templates" / "claude.md.j2").write_text(
            (REPO_ROOT / "templates" / "claude.md.j2").read_text()
        )
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        assert "lead" in fleet.leaf_manager_bots()  # the default applies at all
        paths = _make_paths(fleet_dir)
        bot = fleet.bots["lead"]

        # The registry keyframe reads resolve_effective_protocols directly and
        # applies no dedup of its own (#1405's "effective, not declared" —
        # sorted() does not drop duplicates) -- the one place this specific
        # guard's removal is actually observable at a composed artifact.
        payload = bot_payload(paths, fleet, bot, None)
        assert payload["equipment"]["protocols"].count("checkin") == 1

        compose_bot(bot, fleet, paths, log=lambda m: None)

        bot_dir = paths.bot_runtime("lead")
        claude_md = (bot_dir / "CLAUDE.md").read_text()
        assert claude_md.count("### Check-in") == 1, claude_md

        settings = json.loads(
            (bot_dir / ".claude" / "settings.local.json").read_text()
        )
        allow = settings["permissions"]["allow"]
        assert allow.count("Skill(checkin)") == 1, allow

        skills_dir = bot_dir / ".claude" / "skills"
        checkin_links = [p for p in skills_dir.iterdir() if p.name == "checkin"]
        assert len(checkin_links) == 1, list(skills_dir.iterdir())
