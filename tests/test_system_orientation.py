"""Every composed bot is told what system it runs in, and the text stays small (#2331).

The section is always-loaded text on every bot, paid on every turn, so it points
to doors and leaves the commands to /fleet-ops. Its size is pinned here, and so is
its promise: one text goes to every role, so every door it names is granted to all.
"""
import fnmatch
import re

import pytest

from claudlobby.__main__ import main
from claudlobby.composer import compose_claude_md, compose_settings_local
from tests.conftest import install_real_template, load_test_fleet, make_paths

HEADING = "## System orientation"
# About 0.9 KB today. A longer section is a decision about a cost every bot pays
# on every turn: raise this in the PR that grows it, and say what it adds.
MAX_SECTION_BYTES = 1050

# The doors the section names, as the CLI spells them ("a b|c" is "a b" and "a c").
# Each must be a door every role is granted: only the fleet manager holds `event list`,
# so a worker that followed it would stop at a permission prompt.
DOORS = (
    "brief", "task list", "task show", "fleet inbox",
    "assignment accept|progress|block|complete", "fleet reports submit",
    "message send|reply", "bot show", "library list",
)
DOOR_BULLETS = ("- **Read:**", "- **Write:**", "- **What you have at hand:**")


def _words(door):
    *head, last = door.split()
    return [(*head, alternative) for alternative in last.split("|")]


def _section(text):
    return HEADING + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _named_doors(section, bot_id):
    """The doors a section names: the code spans of its door bullets, but the flags."""
    spans = [span for line in section.splitlines() if line.startswith(DOOR_BULLETS)
             for span in re.findall(r"`([^`]+)`", line)]
    return [span.removesuffix(f" {bot_id}") for span in spans if not span.startswith("--")]


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
        assert f"`bot show {bot_id}`" in section, bot_id
        assert "`/fleet-ops`" in text, bot_id   # the flags live there; the file says so once, above the sections
        assert "{{" not in section and "{%" not in section, bot_id


def test_the_section_is_the_same_for_every_role_but_the_bot_id(fleet_dir):
    shapes = {_section(text).replace(f"bot show {bot_id}`", "bot show BOT`")
              for bot_id, text in _composed(fleet_dir).items()}
    assert len(shapes) == 1


def test_the_section_names_exactly_the_doors_it_is_checked_for(fleet_dir):
    for bot_id, text in _composed(fleet_dir).items():
        assert sorted(_named_doors(_section(text), bot_id)) == sorted(DOORS), bot_id


def test_the_section_stays_small(fleet_dir):
    for bot_id, text in _composed(fleet_dir).items():
        size = len(_section(text).encode())
        assert size <= MAX_SECTION_BYTES, (
            f"{bot_id}: {size} bytes; every bot pays this on every turn")


@pytest.mark.parametrize("words", [w for door in DOORS for w in _words(door)],
                         ids=lambda words: " ".join(words))
def test_every_command_the_section_names_exists(words, capsys):
    with pytest.raises(SystemExit) as stop:
        main([*words, "--help"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("usage: claudlobby " + " ".join(words))


def _allowed(allow, command):
    """Whether a Bash allow rule matches the command line: a glob, and a trailing ' *' needs a space."""
    return any(rule.startswith("Bash(") and fnmatch.fnmatchcase(command, rule[5:-1]) for rule in allow)


def test_every_door_the_section_names_is_granted_to_every_role(fleet_dir):
    # One text goes to every bot, so it may name only doors every role holds: a worker
    # that follows a door only the manager is granted stops at a permission prompt.
    fleet, paths = load_test_fleet(fleet_dir), make_paths(fleet_dir)
    assert fleet.manager in fleet.bots and set(fleet.bots) - fleet.manager_bots(), "needs the manager and a worker"
    refused = {}
    for bot_id, bot in fleet.bots.items():
        allow = compose_settings_local(bot, fleet, paths)["permissions"]["allow"]
        for door in DOORS:
            for words in _words(door):
                command = " ".join(("claudlobby", "--json", *words))
                if not (_allowed(allow, command) or _allowed(allow, command + " ARG")):
                    refused.setdefault(bot_id, []).append(command)
    assert not refused, f"doors with no allow rule: {refused}"
