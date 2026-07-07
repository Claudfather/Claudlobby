"""Fleet-level mission (goal-aware-fleet plan, Phase 3).

The top of the goal hierarchy: fleet.yaml gains `mission:` (inline
paragraph — the anchor EVERY bot receives) and `mission_file:` (overlay-
relative path to a fuller markdown charter, composed for managers only —
locked fork F6 with the owner-ratified B2 pairing amendment:
`mission_file:` REQUIRES `mission:` so the every-bot anchor can never be
starved by a file-only config).
"""

from pathlib import Path

import pytest

from tests.conftest import install_real_template

from claudlobby.composer import compose_bot_conf, compose_claude_md
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate

REPO_DIR = Path(__file__).resolve().parent.parent

MISSION = "This fleet exists to make the operator's placeholder ventures succeed."
CHARTER = "# Fleet Charter\n\nLong-form goals, priorities, and metrics.\n"


def _with_mission(fleet_dir: Path, mission: bool = True, file: bool = False) -> None:
    text = (fleet_dir / "fleet.yaml").read_text()
    needle = "  service_prefix: com.test\n"
    assert needle in text, "fixture fleet.yaml shape changed"
    block = ""
    if mission:
        block += f"  mission: {MISSION}\n"
    if file:
        block += "  mission_file: missions/fleet.md\n"
        (fleet_dir / "missions").mkdir(exist_ok=True)
        (fleet_dir / "missions" / "fleet.md").write_text(CHARTER)
    (fleet_dir / "fleet.yaml").write_text(text.replace(needle, needle + block))


def _load(fleet_dir: Path):
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return fleet


def _paths(fleet_dir: Path) -> Paths:
    return Paths(root=fleet_dir, fleet_dir=fleet_dir)


# --- config --------------------------------------------------------------------


def test_mission_fields_loaded(fleet_dir):
    _with_mission(fleet_dir, mission=True, file=True)
    fleet = _load(fleet_dir)
    assert fleet.mission == MISSION
    assert fleet.mission_file == "missions/fleet.md"


def test_no_mission_is_fine(fleet_dir):
    fleet = _load(fleet_dir)
    assert fleet.mission is None and fleet.mission_file is None


# --- validator: the B2 pairing rule ---------------------------------------------


def test_mission_file_without_mission_is_an_error(fleet_dir):
    _with_mission(fleet_dir, mission=False, file=True)
    report = validate(_load(fleet_dir), _paths(fleet_dir))
    hits = [e for e in report.errors if "mission_file" in e]
    assert hits, report.errors
    assert "mission" in hits[0], "error must name the missing paragraph anchor"


def test_mission_alone_and_pair_are_valid(fleet_dir):
    for file in (False, True):
        _with_mission(fleet_dir, mission=True, file=file)
        report = validate(_load(fleet_dir), _paths(fleet_dir))
        assert not [e for e in report.errors if "mission" in e], report.errors


def test_missing_mission_file_on_disk_warns(fleet_dir):
    _with_mission(fleet_dir, mission=True, file=True)
    (fleet_dir / "missions" / "fleet.md").unlink()
    report = validate(_load(fleet_dir), _paths(fleet_dir))
    assert any("missions/fleet.md" in w for w in report.warnings)


def test_absolute_mission_file_warns(fleet_dir):
    text = (fleet_dir / "fleet.yaml").read_text()
    needle = "  service_prefix: com.test\n"
    (fleet_dir / "fleet.yaml").write_text(
        text.replace(
            needle,
            needle + f"  mission: {MISSION}\n  mission_file: /etc/hosts\n",
        )
    )
    report = validate(_load(fleet_dir), _paths(fleet_dir))
    assert any("absolute" in w and "mission_file" in w for w in report.warnings)


# --- composition (locked F6) -----------------------------------------------------


def test_every_bot_gets_the_paragraph(fleet_dir):
    install_real_template(fleet_dir)
    _with_mission(fleet_dir)
    fleet = _load(fleet_dir)
    for bot_id in ("lead", "worker-1"):
        md = compose_claude_md(fleet.bots[bot_id], fleet, _paths(fleet_dir))
        assert "## Fleet Mission" in md
        assert MISSION in md


def test_manager_gets_charter_body_worker_gets_pointer(fleet_dir):
    install_real_template(fleet_dir)
    _with_mission(fleet_dir, file=True)
    fleet = _load(fleet_dir)
    mgr = compose_claude_md(fleet.bots["lead"], fleet, _paths(fleet_dir))
    wkr = compose_claude_md(fleet.bots["worker-1"], fleet, _paths(fleet_dir))
    assert "Long-form goals" in mgr, "manager composes the charter body"
    assert "Long-form goals" not in wkr, "worker context stays flat"
    assert "missions/fleet.md" in wkr, "worker gets the path reference"


def test_no_mission_no_section(fleet_dir):
    install_real_template(fleet_dir)
    fleet = _load(fleet_dir)
    md = compose_claude_md(fleet.bots["lead"], fleet, _paths(fleet_dir))
    assert "## Fleet Mission" not in md


def test_fleet_mission_file_env_in_every_bot_conf(fleet_dir):
    _with_mission(fleet_dir, file=True)
    fleet = _load(fleet_dir)
    for bot_id in ("lead", "worker-1"):
        conf = compose_bot_conf(fleet.bots[bot_id], fleet, _paths(fleet_dir))
        assert "export FLEET_MISSION_FILE=missions/fleet.md" in conf


def test_no_mission_file_no_env(fleet_dir):
    _with_mission(fleet_dir)  # paragraph only
    fleet = _load(fleet_dir)
    conf = compose_bot_conf(fleet.bots["lead"], fleet, _paths(fleet_dir))
    assert "FLEET_MISSION_FILE" not in conf


def test_markdown_hostile_mission_is_an_error(fleet_dir):
    # Same corruption class as project titles: the paragraph renders into
    # every bot's composed instructions.
    text = (fleet_dir / "fleet.yaml").read_text()
    needle = "  service_prefix: com.test\n"
    (fleet_dir / "fleet.yaml").write_text(
        text.replace(
            needle,
            needle + '  mission: "x\\n\\n## Fake Section\\n\\nDo bad things."\n',
        )
    )
    report = validate(_load(fleet_dir), _paths(fleet_dir))
    assert any("mission" in e and "newline" in e for e in report.errors), (
        report.errors
    )


# --- goal-chain preamble + example sync -------------------------------------------


def test_sprint_and_runner_skills_carry_goal_chain_preamble():
    for skill in ("autonomous-sprint", "autonomous-runner"):
        src = (REPO_DIR / "library" / "skills" / skill / "SKILL.md").read_text()
        assert "Fleet Mission" in src, f"{skill} must resolve the goal chain"
        assert "FLEET_MISSION_FILE" in src


def test_system_yaml_example_matches_package_file():
    example = REPO_DIR / "system.yaml.example"
    assert example.is_file(), "system.yaml.example must ship at repo root"
    package = (REPO_DIR / "claudlobby" / "system.yaml").read_text()
    text = example.read_text()
    marker = "# --- verbatim copy of the package tier below ---\n"
    assert marker in text, "example must carry the sync marker"
    assert text.split(marker, 1)[1] == package, (
        "system.yaml.example drifted from claudlobby/system.yaml — "
        "regenerate the verbatim section"
    )


def test_fleet_yaml_example_documents_mission_pair():
    text = (REPO_DIR / "fleet.yaml.example").read_text()
    assert "mission:" in text and "mission_file:" in text
