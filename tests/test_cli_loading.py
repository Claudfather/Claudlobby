"""CLI parsing must survive unavailable command dependencies (#1747 P1)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.conftest import constructed_env


REPO = Path(__file__).resolve().parents[1]

# Fresh -S interpreters avoid pytest's already-imported dependencies. Record
# attempts too, so optional-import exception handlers cannot hide a violation.
BOOTSTRAP = """
import sys
sys.path.insert(0, sys.argv.pop(1))
allowed = {
    'claudlobby', 'claudlobby.__main__', 'claudlobby.commands',
    'claudlobby.commands._parsers', 'claudlobby.task_defaults',
    'claudlobby.command_result', 'claudlobby.reference_hints',
}
blocked = []
class ImportBoundary:
    def find_spec(self, fullname, path=None, target=None):
        # CPython 3.11 copy.py probes Jython's optional type and catches its
        # ImportError. This stdlib probe is not a CLI command dependency.
        if fullname in {'org', 'org.python', 'org.python.core'}:
            return None
        root = fullname.split('.')[0]
        parser_module = (fullname.startswith('claudlobby.commands.')
                         and fullname.rsplit('.', 1)[-1].endswith('_parsers'))
        if ((root == 'claudlobby' and fullname not in allowed and not parser_module)
                or (root != 'claudlobby' and root not in sys.stdlib_module_names)):
            blocked.append(fullname)
            raise ModuleNotFoundError('blocked CLI dependency: ' + fullname)
sys.meta_path.insert(0, ImportBoundary())
"""
PARSE = """
from claudlobby.__main__ import main
try:
    main(sys.argv[1:])
finally:
    assert not blocked, blocked
"""


def _run(code, *argv, tmp_path):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", BOOTSTRAP + dedent(code), str(REPO), *argv],
        cwd=tmp_path, env=constructed_env(), capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize(("argv", "expected"), [
    (("--help",), "Compositor for Claude Code agent fleets"),
    (("brief", "--help"), "--boot"),
    (("bot", "automation", "status", "--help"), "BOT"),
    (("bot", "create", "--help"), "--expertise"),
    (("bot", "handoff", "--help"), "BOT"),
    (("plane", "view", "--help"), "--host"),
    (("host", "setup", "--help"), "--wheel"),
    (("fleet", "setup", "--help"), "--install-directory"),
    (("migration", "data", "--help"), "--source"),
    (("message", "show", "--help"), "MESSAGE_ID"),
    (("message", "send", "--help"), "--request-id"),
    (("message", "reply", "--help"), "MESSAGE_ID"),
    (("fleet", "reports", "submit", "--help"), "--summary"),
    (("fleet", "reports", "list", "--help"), "--unacknowledged"),
    (("fleet", "reports", "ack", "--help"), "ACK_CURSOR"),
    (("fleet", "inbox", "--help"), "VIEWER"),
    (("library", "list", "--help"), "schema-1"),
    (("library", "create", "--help"), "--kind"),
    (("fleet", "status", "--help"), "schema-1"),
    (("bot", "status", "--help"), "BOT"),
    (("bot", "session", "--help"), "BOT"),
    (("bot", "logs", "--help"), "--lines"),
    (("fleet", "logs", "--help"), "--lines"),
    (("fleet", "uptime", "--help"), "30d"),
    (("assignment", "deliver", "--help"), "--file"),
    (("request", "show", "--help"), "REQUEST_ID"),
    (("workstream", "open", "--help"), "--request-id"),
    (("checkin", "record", "--help"), "--selection-file"),
])
def test_help_needs_only_stdlib(argv, expected, tmp_path):
    result = _run(PARSE, *argv, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_invalid_arguments_refuse_before_loading_commands(tmp_path):
    result = _run(PARSE, "task", "recheck", "--max-age-h", "bad", tmp_path=tmp_path)
    assert result.returncode == 2, result.stderr
    assert result.stderr == "invalid argument: command syntax\ninspect claudlobby task --help\n"
    assert result.stdout == ""
    assert "Traceback" not in result.stderr

    brief = _run(PARSE, "--json", "brief", "--unexpected=private-value", tmp_path=tmp_path)
    assert brief.returncode == 2 and '"command":"brief"' in brief.stdout
    assert "private-value" not in brief.stdout + brief.stderr


def test_retired_host_timers_is_not_a_public_route(tmp_path):
    result = _run(PARSE, "host-timers", tmp_path=tmp_path)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_main_passes_namespace_to_only_selected_handler_and_returns_its_result(tmp_path):
    result = _run("""
        import argparse
        from types import ModuleType
        from claudlobby.__main__ import main

        captured = []
        parse_args = argparse.ArgumentParser.parse_args
        def capture(self, *args, **kwargs):
            parsed = parse_args(self, *args, **kwargs)
            captured.append((parsed, vars(parsed).copy()))
            return parsed
        argparse.ArgumentParser.parse_args = capture

        fake = ModuleType('claudlobby.commands.events')
        def handler(args):
            assert len(captured) == 1 and args is captured[0][0]
            assert vars(args) == captured[0][1]
            assert args.cmd == 'event' and args.event_action == 'list' and args.limit == 7
            assert args.root == 'example' and args.fleet == 'test-fleet'
            from claudlobby.command_result import CommandOutput
            return CommandOutput({'items': [], 'next_cursor': None}, lines=('selected',))
        fake.dispatch = handler
        sys.modules[fake.__name__] = fake
        rc = main(['--root', 'example', '--fleet', 'test-fleet', 'event', 'list', '--limit', '7'])
        assert not blocked, blocked
        sys.exit(rc)
    """, tmp_path=tmp_path)
    assert result.returncode == 0 and result.stdout == 'selected\n', result.stderr


@pytest.mark.parametrize("old", (
    "env-migrate", "data-migrate", "cron-migrate", "memory-migrate",
    "lessons-migrate", "plane import-workstreams",
))
def test_retired_converter_spellings_are_not_aliases(old, tmp_path):
    result = _run(PARSE, *old.split(), "--help", tmp_path=tmp_path)
    assert result.returncode == 2


def test_converter_syntax_error_uses_public_result_without_echoing_values(tmp_path):
    result = _run(PARSE, "--json", "migration", "env", "--source", "private-value",
                  "--map", "bad", "--unexpected", tmp_path=tmp_path)
    assert result.returncode == 2
    assert '"command":"migration.env"' in result.stdout
    assert "private-value" not in result.stdout + result.stderr


@pytest.mark.parametrize("action", ["allow-nudges", "revoke-nudges", "allow-feedback", "revoke-feedback"])
def test_owner_nudge_syntax_error_names_exact_command_and_help(action, tmp_path):
    result = _run(PARSE, "--json", "host", "owner", action, tmp_path=tmp_path)
    assert result.returncode == 2 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == f"host.owner.{action}"
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["error"]["hint"] == f"inspect claudlobby host owner {action} --help"


def test_selected_import_failure_is_reported_by_common_result(tmp_path):
    result = _run("""
        from claudlobby.__main__ import main
        sys.exit(main(['event', 'list']))
    """, tmp_path=tmp_path)
    assert result.returncode == 6, result.stderr
    assert result.stderr == "unavailable: application dependencies\n"


@pytest.mark.parametrize(("argv", "module"), [
    (("workstream", "list"), "claudlobby.commands.workstream"),
    (("checkin", "list"), "claudlobby.commands.checkin"),
])
def test_new_parser_modules_keep_domain_imports_lazy(argv, module, tmp_path):
    result = _run("""
        from claudlobby.__main__ import main
        module = sys.argv.pop()
        main(sys.argv[1:])
        assert blocked == [module], blocked
    """, *argv, module, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr


def test_feedback_syntax_error_names_exact_command_and_required_selection(tmp_path):
    result = _run(PARSE, '--json', 'task', 'feedback', 'private-task-value', tmp_path=tmp_path)
    assert result.returncode == 2 and result.stderr == ''
    payload = json.loads(result.stdout)
    assert payload['command'] == 'task.feedback'
    assert payload['error']['code'] == 'invalid_argument'
    assert payload['error']['hint'] == 'inspect claudlobby task feedback --help'
    assert 'private-task-value' not in result.stdout
