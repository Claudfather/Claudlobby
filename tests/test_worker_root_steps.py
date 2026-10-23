"""A worker is told what to do when its sudo-family deny refuses a command (#2464).

The compositor denies the sudo family to every bot its fleet does not name as
manager, and a refused call says only that permission was denied. So the same
role test composes a short section naming the remedy and the routes the deny
cannot see. It is always-loaded text on every worker, paid on every turn: its
size is pinned here, and a manager, which keeps sudo, composes none of it.
"""
import pytest

from claudlobby.__main__ import main
from claudlobby.composer import compose_claude_md, compose_settings_local
from tests.conftest import install_real_template, load_test_fleet, make_paths
from tests.test_composer import _SUDO_FAMILY
from tests.test_system_orientation import DOORS as ORIENTATION_DOORS, _words

HEADING = "## Commands that need root"
# About 0.53 KB. A longer section is a cost every worker pays on every turn:
# raise this in the PR that grows it, and say what it adds.
MAX_SECTION_BYTES = 600

# The remedy: block the work, naming the command and why it needs root.
REMEDY = ("`assignment block`", "naming the command and why it needs root",
          "`fleet reports submit --status blocked`")
# The routes the deny cannot see, which the PR leaves open: a wrapper, another
# path, a shell string, a script and a settings edit.
ROUTES = ("`env`", "`setsid`", "`ionice`", "`watch`", "another path", "`bash -c`",
          "a script", "editing your settings")
# The remedy's commands as a worker types them: the command's words, then its options.
COMMANDS = ((("assignment", "block"), ()), (("fleet", "reports", "submit"), ("--status", "blocked")))


def _section(text):
    assert text.count(HEADING) == 1
    return HEADING + text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _composed(fleet_dir):
    """The fleet, its paths and each bot's composed CLAUDE.md: the fixture's manager and worker."""
    install_real_template(fleet_dir)
    fleet, paths = load_test_fleet(fleet_dir), make_paths(fleet_dir)
    assert fleet.manager in fleet.bots and set(fleet.bots) - fleet.manager_bots(), "needs the manager and a worker"
    return fleet, paths, {bot_id: compose_claude_md(bot, fleet, paths) for bot_id, bot in fleet.bots.items()}


def _worker_sections(fleet_dir):
    fleet, _paths, composed = _composed(fleet_dir)
    return {bot_id: _section(text) for bot_id, text in composed.items() if bot_id not in fleet.manager_bots()}


def test_a_worker_is_told_what_to_do_when_the_deny_refuses_a_command(fleet_dir):
    for bot_id, section in _worker_sections(fleet_dir).items():
        missing = [f"`{name}`" for name in sorted(_SUDO_FAMILY) if f"`{name}`" not in section]
        missing += [phrase for phrase in REMEDY + ROUTES if phrase not in section]
        assert not missing, f"{bot_id}: the section does not name {missing}"


def test_the_section_reaches_exactly_the_bots_the_deny_reaches(fleet_dir):
    # The text and the deny share one role test: a manager keeps sudo and is
    # told nothing about a deny it does not have.
    fleet, paths, composed = _composed(fleet_dir)
    told = {bot_id: HEADING in text for bot_id, text in composed.items()}
    denied = {bot_id: "Bash(sudo *)" in compose_settings_local(bot, fleet, paths)["permissions"]["deny"]
              for bot_id, bot in fleet.bots.items()}
    assert told == denied
    assert sorted(set(told.values())) == [False, True], "needs a bot on each side"


def test_the_section_stays_small(fleet_dir):
    for bot_id, section in _worker_sections(fleet_dir).items():
        size = len(section.encode())
        assert size <= MAX_SECTION_BYTES, f"{bot_id}: {size} bytes; every worker pays this on every turn"


@pytest.mark.parametrize("words, options", [pytest.param(w, o, id=" ".join(w + o)) for w, o in COMMANDS])
def test_every_command_the_remedy_names_parses(words, options, capsys):
    # The options parse before `--help` stops the run, so a status the CLI
    # does not offer fails here rather than at a refused worker's prompt.
    with pytest.raises(SystemExit) as stop:
        main([*words, *options, "--help"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.startswith("usage: claudlobby " + " ".join(words))


def test_the_remedy_is_among_the_doors_checked_as_granted_to_every_role():
    # tests/test_system_orientation.py checks that each door it lists exists and
    # is granted to every role, so a worker that follows the remedy does not
    # stop at a permission prompt. This keeps the remedy's doors on that list.
    checked = {words for door in ORIENTATION_DOORS for words in _words(door)}
    assert {words for words, _options in COMMANDS} <= checked
