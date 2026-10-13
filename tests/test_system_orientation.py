"""Every composed bot is told what system it runs in, and the text stays small (#2331).

The section is always-loaded text on every bot, paid on every turn, so it points
to doors and leaves the commands to /fleet-ops. Its size is pinned here.
"""
import pytest

from claudlobby.__main__ import main
from claudlobby.composer import compose_claude_md
from tests.conftest import install_real_template, load_test_fleet, make_paths

HEADING = "## System orientation"
# About 1.1 KB today. A longer section is a decision about a cost every bot pays
# on every turn: raise this in the PR that grows it, and say what it adds.
MAX_SECTION_BYTES = 1300

# The doors the section names, as the CLI spells them ("a b|c" is "a b" and "a c").
DOORS = (
    "brief", "task list", "task show", "fleet inbox", "event list",
    "assignment accept|progress|block|complete", "fleet reports submit",
    "message send|reply", "bot show", "library list",
)


def _words(door):
    *head, last = door.split()
    return [(*head, alternative) for alternative in last.split("|")]


def _section(text):
    return HEADING + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _composed(fleet_dir):
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    assert len(fleet.bots) >= 2
    return {bot.bot_id: compose_claude_md(bot, fleet, paths) for bot in fleet.bots.values()}


def test_every_composed_bot_is_told_what_system_it_runs_in(fleet_dir):
    # The fixture declares no mission, scope or shared docs: the section is not
    # conditional on any of them.
    for bot_id, text in _composed(fleet_dir).items():
        assert text.count(HEADING) == 1, bot_id
        section = _section(text)
        assert "the **plane**" in section, bot_id
        assert "tasks, assignments, messages, reports and events" in section, bot_id
        assert "A **task** is a unit of work; an **assignment** is one bot's claim on it." in section, bot_id
        assert f"claudlobby --json bot capabilities {bot_id}`" in section, bot_id
        assert "`/fleet-ops`" in section, bot_id
        assert "{{" not in section and "{%" not in section, bot_id


def test_the_section_is_the_same_for_every_role_but_the_bot_id(fleet_dir):
    shapes = {_section(text).replace(f"capabilities {bot_id}`", "capabilities BOT`")
              for bot_id, text in _composed(fleet_dir).items()}
    assert len(shapes) == 1


def test_the_section_names_each_door_it_is_checked_for(fleet_dir):
    section = _section(next(iter(_composed(fleet_dir).values())))
    for door in DOORS:
        assert f"`{door}`" in section, door


def test_the_section_stays_small(fleet_dir):
    for bot_id, text in _composed(fleet_dir).items():
        size = len(_section(text).encode())
        assert size <= MAX_SECTION_BYTES, (
            f"{bot_id}: {size} bytes; every bot pays this on every turn")


@pytest.mark.parametrize("words", [w for door in DOORS for w in _words(door)] + [("bot", "capabilities")],
                         ids=lambda words: " ".join(words))
def test_every_command_the_section_names_exists(words, capsys):
    with pytest.raises(SystemExit) as stop:
        main([*words, "--help"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("usage: claudlobby " + " ".join(words))
