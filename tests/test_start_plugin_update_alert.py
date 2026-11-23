"""A bot start whose required-plugin update fails tells the manager (#3090).

A reload raises a failed `claude plugin update` as reload_failed through
emit_failure_alert: a plane event, the manager's pane and Telegram. A start runs
the same update in plugin_ensure, which wrote the failure to the bot's startup
log and went on, so nothing else heard of it. Now plugin_ensure prints a failed
update's reason, and once the bot is up the start raises one plugin_update_failed
alert naming each update that failed. A start whose updates pass raises nothing,
and a failed update never stops the start.

The start runs here from start-bot.sh's own text: its plugin step through the
session spawn, then its closing alert through its last line, with lib-common
sourced as the start sources it. Stubbed: `claude` (each update passes or fails
as the case says), the spawn (it records that it ran), the manager's session
check, and the two calls beneath the manager-pane send: its tmux call and the
verified send, which records the text bot_tmux_send hands it, cleaned as the
pane gets it. The alert's plane event lands on a served scratch plane and its
Telegram send on the tg-post stub.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from claudlobby.plane.db import connect_ro
from tests.conftest import _write_exec, constructed_env, read_fleet_events
from tests.test_maintenance_jobs import _captured, _native_fixture
from tests.test_plane_events_door import _serving
from tests.test_plugin_update_scope import UPDATE_PATHS

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby" / "_runtime_scripts"
START_BOT = LIB / "start-bot.sh"
PLUGINS = ("alpha@Market", "beta@Market")

# Stands in for Claude Code: records each call, and fails an update for a plugin
# named in CLAUDE_FAILS the way the binary the bots launch fails one for a plugin
# with no user-scope installation (#3089's scratch run printed this line), with
# no output at all for one named in CLAUDE_FAILS_SILENTLY, or with one long line
# carrying control bytes for one named in CLAUDE_FAILS_LONG.
CLAUDE_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$CLAUDE_CALLS"
for ref; do :; done
case " ${CLAUDE_FAILS_SILENTLY:-} " in *" $ref "*) exit 1 ;; esac
case " ${CLAUDE_FAILS_LONG:-} " in
    *" $ref "*)
        printf 'Failed to update plugin "%s": \033[1m%05000d\033[0m\t\r\n' "$ref" 0
        exit 1 ;;
esac
case " ${CLAUDE_FAILS:-} " in
    *" $ref "*)
        printf 'Checking for updates for plugin "%s" at user scope...\n' "$ref"
        printf 'Failed to update plugin "%s": Plugin "%s" is not installed at scope user\n' "$ref" "${ref%%@*}"
        exit 1 ;;
esac
printf 'Plugin "%s" is already at the latest version\n' "$ref"
"""

# The start's text runs after these: errexit and an ERR trap that records any
# failing command, as the start's own trap would raise script_error for it.
PRELUDE = r"""
. "$NATIVE/lib-common.sh"
set -Eeuo pipefail
trap 'printf "ERR %s\n" "$BASH_COMMAND" >> "$ERRORS"' ERR
bot_session_spawn() { printf 'spawned\n' >> "$RECORD"; }
check_tmux_session() { return 0; }
bot_tmux() { return 0; }
pane_send_verified() { printf 'pane %s %s: %s\n' "$1" "$2" "$3" >> "$RECORD"; }
"""


def _failure(ref: str) -> str:
    """The reason plugin_ensure prints for the stub's failed update of *ref*."""
    return (f'claude plugin update {ref} failed (exit 1): Failed to update plugin "{ref}": '
            f'Plugin "{ref.split("@")[0]}" is not installed at scope user')


def _long_failure(ref: str) -> str:
    """The reason for the stub's long failed update of *ref*: the line cleaned
    (the colour sequences dropped, the tab and the carriage return spaces), then
    its first 300 bytes."""
    line = f'Failed to update plugin "{ref}": ' + "0" * 5000
    return f"claude plugin update {ref} failed (exit 1): " + line[:300]


def _claude(tmp_path: Path) -> Path:
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir(exist_ok=True)
    _write_exec(claude, CLAUDE_STUB)
    return claude


def _registry(home: Path, directory: Path, refs) -> None:
    """Each plugin installed for *directory* alone, with no user-scope entry: the
    case #3090 is about, which plugin_ensure counts as installed and updates."""
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": {
        ref: [{"scope": "local", "projectPath": str(directory),
               "installPath": str(home / "cache" / ref / "1.0.0"), "version": "1.0.0"}]
        for ref in refs}}) + "\n")


def _start_text() -> str:
    """start-bot.sh from the line after its launch command through the session
    spawn (its plugin step), then from the end of its bridge verify through its
    last line (its closing plugin alert). Sliced at code lines the plugin step
    and its alert do not own, so neither rewording a comment nor changing those
    lines moves the slices."""
    source = START_BOT.read_text()
    step = source[source.index("\n", source.index("\nCLAUDE_CMD=") + 1) + 1:]
    spawn = 'bot_session_spawn "$TMUX_SOCKET"'
    step = step[:step.index("\n", step.index(spawn)) + 1]
    last = source.index('echo "$BOT_LABEL started"')
    tail = source[source.rindex("esac\n", 0, last) + len("esac\n"):]
    return step + tail


def _start(tmp_path, scratch_plane_env, fails, long_fails=()):
    """Run the start's text for a bot `worker` whose fleet's manager `lead`
    declares its pane and the fleet's alert chat. Returns the run, the record of
    spawns and pane sends, the bot's startup log, the plane's fleet events, the
    severity each plugin_update_failed row was stamped with, and the Telegram
    capture."""
    root = tmp_path / "root"
    lead, worker = root / "runtime" / "bots" / "lead", root / "runtime" / "bots" / "worker"
    lead.mkdir(parents=True)
    worker.mkdir()
    (lead / "bot.conf").write_text('export MANAGER_TMUX="lead"\nexport MANAGER_TMUX_SOCKET="lead-socket"\n'
                                   'export TELEGRAM_GROUP_CHAT_ID="-100123"\n')
    home = tmp_path / "home"
    _registry(home, worker, PLUGINS)
    native = _native_fixture(tmp_path)
    record, errors = tmp_path / "record", tmp_path / "errors"
    with _serving(root, scratch_plane_env) as socket:
        env = constructed_env(
            **scratch_plane_env(root, socket=socket), HOME=home, FLEET_NAME="tf",
            TG_CAPTURE=tmp_path / "tg-capture", NATIVE=native, RECORD=record, ERRORS=errors,
            CLAUDE_CALLS=tmp_path / "claude-calls", CLAUDE_FAILS=" ".join(fails),
            CLAUDE_FAILS_LONG=" ".join(long_fails),
            BOT_DIR=worker, CLAUDE=_claude(tmp_path), FLEET_PLUGINS_REQUIRED=" ".join(PLUGINS),
            BOOT_PLUGIN_UPDATE_ONCE="0", _sb_plugin_startup_policy="manage",
            TMUX_SOCKET="worker-socket", TMUX_SESSION="worker", CLAUDE_CMD="claude",
            BOT_LABEL="worker")
        result = subprocess.run(["bash", "-c", PRELUDE + _start_text()], env=env, cwd=worker,
                                capture_output=True, text=True, timeout=120)
    assert not errors.exists(), errors.read_text() + result.stderr
    assert result.returncode == 0, result.stderr
    conn = connect_ro(root / "state" / "plane" / "plane.db")
    try:
        severities = [row[0] for row in conn.execute(
            "SELECT severity FROM events WHERE event = 'plugin_update_failed'")]
    finally:
        conn.close()
    return (result, record.read_text().splitlines() if record.exists() else [],
            (worker / "logs" / "startup.log").read_text(),
            [json.loads(line) for line in read_fleet_events(root).splitlines()],
            severities, _captured(tmp_path))


@pytest.mark.parametrize("fails", [PLUGINS[:1], PLUGINS], ids=["one-failed", "both-failed"])
def test_a_failed_update_at_start_reaches_the_manager_once_and_the_start_goes_on(
        tmp_path, scratch_plane_env, fails):
    result, record, log, events, severities, sent = _start(tmp_path, scratch_plane_env, fails)
    reason = ("worker start: " + "; ".join(_failure(ref) for ref in fails)
              + "; the session started on the plugins as installed")
    # The start went on: the session was spawned, the script reached its last
    # line, and the alert came after the spawn, once.
    assert result.stdout.splitlines()[-1] == "worker started"
    assert record == ["spawned", f"pane lead-socket lead: [FLEET-ALERT] plugin_update_failed: {reason}"]
    # The plane: one fleet-anchored row, stamped critical, as reload_failed is.
    alerts = [e for e in events if e["type"] == "plugin_update_failed"]
    assert [(e["bot"], e["source"], e["data"]) for e in alerts] == [("fleet", "alert", {"reason": reason})]
    assert severities == ["critical"]
    # Telegram: one FLEET ALERT to the fleet's alert chat, carrying the same
    # reason (the stub records the chat, its state directory and the text).
    assert [(chat, text) for chat, _state, text in (line.split("|", 2) for line in sent.splitlines())] == [
        ("-100123", f"FLEET ALERT [plugin_update_failed]: {reason}")]
    # The startup log: each failed update's own line, then the alert's.
    assert [line.split(" ", 1)[1] for line in log.splitlines() if " PLUGIN ERROR " in line] == [
        f"PLUGIN ERROR {_failure(ref)}" for ref in fails]
    assert [line.split(" ", 1)[1] for line in log.splitlines() if "plugin_update_failed:" in line] == [
        f"plugin_update_failed: {reason}"]


def test_passing_updates_at_start_raise_nothing_and_the_start_goes_on(tmp_path, scratch_plane_env):
    result, record, log, events, severities, sent = _start(tmp_path, scratch_plane_env, ())
    assert result.stdout.splitlines()[-1] == "worker started"
    assert record == ["spawned"]
    assert [e for e in events if e["type"] == "plugin_update_failed"] == []
    assert severities == []
    assert sent == ""
    assert "PLUGIN ERROR" not in log and "plugin_update_failed" not in log
    # Both updates ran: nothing was raised because both passed.
    assert (tmp_path / "claude-calls").read_text().splitlines() == [
        f"plugin update --scope user {ref}" for ref in PLUGINS]


def test_a_long_last_line_reaches_the_pane_and_the_chat_cut_and_cleaned(tmp_path, scratch_plane_env):
    """#3107: each failed update's last line keeps its first 300 bytes, cleaned as
    a pane send cleans text, so the alert fits one Telegram message and the
    manager's pane shows the reason the chat does."""
    result, record, log, events, severities, sent = _start(
        tmp_path, scratch_plane_env, (), long_fails=PLUGINS)
    reason = ("worker start: " + "; ".join(_long_failure(ref) for ref in PLUGINS)
              + "; the session started on the plugins as installed")
    assert result.stdout.splitlines()[-1] == "worker started"
    assert record == ["spawned", f"pane lead-socket lead: [FLEET-ALERT] plugin_update_failed: {reason}"]
    texts = [text for _chat, _state, text in (line.split("|", 2) for line in sent.splitlines())]
    assert texts == [f"FLEET ALERT [plugin_update_failed]: {reason}"]
    assert len(texts[0]) <= 4096
    assert [e["data"] for e in events if e["type"] == "plugin_update_failed"] == [{"reason": reason}]
    assert [line.split(" ", 1)[1] for line in log.splitlines() if " PLUGIN ERROR " in line] == [
        f"PLUGIN ERROR {_long_failure(ref)}" for ref in PLUGINS]


# plugin_ensure's cases besides the four update paths: one that skips the update
# (this boot already ran it) and one that installs (no registry entry).
OTHER_CASES = {
    "skipped-this-boot": ('CLAUDLOBBY_BOOT_EPOCH=1000000000; mkdir -p "$CLAUDLOBBY_ROOT/state/boot"; '
                          ': > "$CLAUDLOBBY_ROOT/state/boot/plugins-updated.1000000000.alpha_Market"',
                          "1", "PLUGIN update skipped (already run this boot)"),
    "installed-at-a-cold-start": ('rm "$HOME/.claude/plugins/installed_plugins.json"', "0",
                                  "PLUGIN installing alpha@Market (cold start)"),
}
# (case, the stub's outcome): the set-up, the once flag, the log line that shows
# the case was taken, and whether plugin_ensure prints a reason: only an update
# that ran and failed does.
CASES = {**{(path, outcome): (setup, once, taken, outcome == "fails")
            for path, (setup, once, taken, _stamps) in UPDATE_PATHS.items()
            for outcome in ("fails", "passes")},
         **{(case, "fails"): (*spec, False) for case, spec in OTHER_CASES.items()}}


@pytest.mark.parametrize("case", sorted(CASES), ids=lambda c: "-".join(c))
def test_plugin_ensure_prints_a_failed_updates_reason_and_nothing_else(tmp_path, case):
    setup, once, taken, prints = CASES[case]
    _path, outcome = case
    home, root, bot = tmp_path / "home", tmp_path / "root", tmp_path / "bot"
    root.mkdir()
    bot.mkdir()
    _registry(home, bot, PLUGINS[:1])
    log = tmp_path / "startup.log"
    script = (f'. "{LIB}/lib-common.sh"\n{setup}\n'
              f'plugin_ensure "{PLUGINS[0]}" "{_claude(tmp_path)}" "{log}" {once}\n')
    result = subprocess.run(
        ["bash", "-c", script], cwd=bot, capture_output=True, text=True, timeout=60,
        env=constructed_env(HOME=home, CLAUDLOBBY_ROOT=root, CLAUDE_CALLS=tmp_path / "claude-calls",
                            CLAUDE_FAILS=PLUGINS[0] if outcome == "fails" else ""))
    assert result.returncode == 0, result.stderr
    assert taken in log.read_text(), log.read_text()
    assert result.stdout == (_failure(PLUGINS[0]) + "\n" if prints else "")
    assert ("PLUGIN ERROR " in log.read_text()) is prints


def test_an_update_that_fails_with_no_output_reports_its_status_alone(tmp_path):
    """The reason is read from this update's own output, never from earlier lines
    of the log: an update that printed nothing reports its exit status, not the
    plugin step's last line."""
    home, root, bot = tmp_path / "home", tmp_path / "root", tmp_path / "bot"
    root.mkdir()
    bot.mkdir()
    _registry(home, bot, PLUGINS[:1])
    log = tmp_path / "startup.log"
    script = (f'. "{LIB}/lib-common.sh"\n'
              f'plugin_ensure "{PLUGINS[0]}" "{_claude(tmp_path)}" "{log}" 0\n')
    result = subprocess.run(
        ["bash", "-c", script], cwd=bot, capture_output=True, text=True, timeout=60,
        env=constructed_env(HOME=home, CLAUDLOBBY_ROOT=root, CLAUDE_CALLS=tmp_path / "claude-calls",
                            CLAUDE_FAILS_SILENTLY=PLUGINS[0]))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"claude plugin update {PLUGINS[0]} failed (exit 1)\n"
    assert log.read_text().splitlines()[-1].endswith(
        f" PLUGIN ERROR claude plugin update {PLUGINS[0]} failed (exit 1)")
