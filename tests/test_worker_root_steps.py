"""A worker is told what to do when its sudo-family deny refuses a command (#2464).

The compositor denies the sudo family to every bot its fleet does not name as
manager, and a refused call says only that permission was denied. So the same
role test composes a short section naming the remedy and the routes the deny
cannot see. It is always-loaded text on every worker, paid on every turn: its
size is pinned here, and a manager, which keeps sudo, composes none of it.
"""
import fnmatch

import pytest

from claudlobby.__main__ import main
from claudlobby.composer import compose_claude_md, compose_settings_local
from tests.conftest import install_real_template, load_test_fleet, make_paths

HEADING = "## Commands that need root"
# About 0.6 KB. A longer section is a cost every worker pays on every turn:
# raise this in the PR that grows it, and say what it adds.
MAX_SECTION_BYTES = 700

# Spelled out here rather than read from the compositor, so a wrong constant
# there cannot also be the expectation.
SUDO_FAMILY = ("sudo", "sudoedit", "su", "pkexec", "doas", "run0")
# The remedy: block the work, naming the command and why it needs root.
REMEDY = ("`assignment block`", "naming the command and why it needs root",
          "`fleet reports submit --status blocked`")
# The routes the deny cannot see, which the PR leaves open: a wrapper, another
# path, a shell string, a script and a settings edit.
ROUTES = ("`env`", "`setsid`", "`ionice`", "`watch`", "another path", "`bash -c`",
          "a script", "editing your settings")
# The doors the remedy names, as the CLI spells them.
DOORS = (("assignment", "block"), ("fleet", "reports", "submit"))


def _section(text):
    assert text.count(HEADING) == 1
    return HEADING + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _composed(fleet_dir):
    """Each bot's composed CLAUDE.md and settings, from the fixture's manager and worker."""
    install_real_template(fleet_dir)
    fleet, paths = load_test_fleet(fleet_dir), make_paths(fleet_dir)
    assert fleet.manager in fleet.bots and set(fleet.bots) - fleet.manager_bots(), "needs the manager and a worker"
    return fleet, {bot_id: (compose_claude_md(bot, fleet, paths), compose_settings_local(bot, fleet, paths))
                   for bot_id, bot in fleet.bots.items()}


def _workers(fleet, composed):
    return {bot_id: pair for bot_id, pair in composed.items() if bot_id not in fleet.manager_bots()}


def test_a_worker_is_told_what_to_do_when_the_deny_refuses_a_command(fleet_dir):
    fleet, composed = _composed(fleet_dir)
    for bot_id, (text, _settings) in _workers(fleet, composed).items():
        section = _section(text)
        missing = [f"`{name}`" for name in SUDO_FAMILY if f"`{name}`" not in section]
        missing += [phrase for phrase in REMEDY + ROUTES if phrase not in section]
        assert not missing, f"{bot_id}: the section does not name {missing}"
        assert "{{" not in section and "{%" not in section, bot_id


def test_the_section_reaches_exactly_the_bots_the_deny_reaches(fleet_dir):
    # The text and the deny share one role test: a manager keeps sudo and is
    # told nothing about a deny it does not have.
    _fleet, composed = _composed(fleet_dir)
    told = {bot_id: HEADING in text for bot_id, (text, _settings) in composed.items()}
    denied = {bot_id: "Bash(sudo *)" in settings["permissions"]["deny"]
              for bot_id, (_text, settings) in composed.items()}
    assert told == denied
    assert sorted(set(told.values())) == [False, True], "needs a bot on each side"


def test_the_section_stays_small(fleet_dir):
    fleet, composed = _composed(fleet_dir)
    for bot_id, (text, _settings) in _workers(fleet, composed).items():
        size = len(_section(text).encode())
        assert size <= MAX_SECTION_BYTES, f"{bot_id}: {size} bytes; every worker pays this on every turn"


@pytest.mark.parametrize("words", DOORS, ids=" ".join)
def test_every_command_the_remedy_names_exists(words, capsys):
    with pytest.raises(SystemExit) as stop:
        main([*words, "--help"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("usage: claudlobby " + " ".join(words))


def _allowed(allow, command):
    """Whether a Bash allow rule matches the command line: a glob, and a trailing ' *' needs a space."""
    return any(rule.startswith("Bash(") and fnmatch.fnmatchcase(command, rule[5:-1]) for rule in allow)


def test_every_door_the_remedy_names_is_granted_to_a_worker(fleet_dir):
    # A worker that follows the remedy must not stop at a permission prompt.
    fleet, composed = _composed(fleet_dir)
    refused = [(bot_id, words) for bot_id, (_text, settings) in _workers(fleet, composed).items()
               for words in DOORS
               if not _allowed(settings["permissions"]["allow"], " ".join(("claudlobby", "--json", *words, "ARG")))]
    assert not refused, f"doors with no allow rule: {refused}"
