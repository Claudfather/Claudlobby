"""Plugin updates name the user scope, wherever they run from (#3088).

With no ``--scope``, ``claude plugin update`` picks the installation to update
from its working directory: run from a directory that has a local entry in
installed_plugins.json, it updates that entry, and the user entry stays as it
was. A hand ``fleet reload`` runs reload-fleet.sh in its caller's directory, and
start-bot.sh changes into the bot's directory before its plugin step, so every
update the two make names the user scope.

The stub ``claude`` stands in for Claude Code: it records the directory it ran
in and its arguments, one line per call, and changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tests.conftest import constructed_env
from tests.test_reload_fleet_native import _script_host

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby" / "_runtime_scripts"
PLUGINS = ("claudna@Claudfather", "superpowers@claude-plugins-official")


def _recording_claude(path: Path, record: Path) -> Path:
    path.write_text(
        "#!/bin/bash\n"
        f'{{ printf "%s" "$(pwd -P)"; printf "\\t%s" "$@"; printf "\\n"; }} >> "{record}"\n'
    )
    path.chmod(0o755)
    return record


def _calls(record: Path) -> list[tuple[Path, list[str]]]:
    """Each recorded call: the directory it ran in, and its arguments."""
    if not record.exists():
        return []
    rows = [line.split("\t") for line in record.read_text().splitlines()]
    return [(Path(row[0]), row[1:]) for row in rows]


def _registry(home: Path, directory: Path) -> Path:
    """Write installed_plugins.json the way a host with per-directory entries
    holds it: each plugin's user entry first, then a local entry for
    ``directory``. Returns that directory, resolved."""
    plugins = {
        ref: [
            {"scope": "user", "installPath": str(home / "cache" / ref / "2.0.0"),
             "version": "2.0.0", "lastUpdated": "2026-01-02T00:00:00.000Z"},
            {"scope": "local", "projectPath": str(directory),
             "installPath": str(home / "cache" / ref / "1.0.0"),
             "version": "1.0.0", "lastUpdated": "2026-01-01T00:00:00.000Z"},
        ]
        for ref in PLUGINS
    }
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": plugins}, indent=2) + "\n")
    return directory.resolve()


def test_reload_from_a_directory_with_a_local_entry_updates_the_user_scope(tmp_path):
    root, bots, _calls_log, env, args = _script_host(tmp_path)
    holder = _registry(Path(env["HOME"]), bots / "lead")
    record = _recording_claude(Path(env["CLAUDE_BIN"]), tmp_path / "claude-record")
    at = args.index("--plugin")
    args = (*args[:at], *(part for ref in PLUGINS for part in ("--plugin", ref)), *args[at + 2:])
    result = subprocess.run(args, env=env, cwd=holder, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr + (root / "state/reload-fleet.log").read_text()
    # Each update ran in the caller's directory, which holds a local entry: an
    # update with no scope would land on that entry, not on the user one.
    assert _calls(record) == [
        (holder, ["plugin", "update", "--scope", "user", ref]) for ref in PLUGINS]


# plugin_ensure's four update paths: the set-up that reaches each one, the once
# flag it is called with, the log line that shows the path was taken, and
# whether that path writes the boot's stamp.
UPDATE_PATHS = {
    "every-start": ("", "0", "PLUGIN updating", False),
    "once-per-boot": ("CLAUDLOBBY_BOOT_EPOCH=1000000000", "1", "PLUGIN updating", True),
    "boot-epoch-unresolvable": (
        "resolve_boot_epoch() { return 1; }; CLAUDLOBBY_BOOT_EPOCH=", "1",
        "PLUGIN update-once unavailable (boot epoch unresolvable)", False),
    "stamp-directory-unwritable": (
        ': > "$CLAUDLOBBY_ROOT/state"; CLAUDLOBBY_BOOT_EPOCH=1000000000', "1",
        "PLUGIN update-once unavailable (state dir unwritable", False),
}


@pytest.mark.parametrize("update_path", sorted(UPDATE_PATHS))
def test_start_plugin_step_in_a_directory_with_a_local_entry_updates_the_user_scope(
        tmp_path, update_path):
    setup, once_flag, taken, stamps = UPDATE_PATHS[update_path]
    home, root, bot = tmp_path / "home", tmp_path / "root", tmp_path / "bots" / "lead"
    root.mkdir()
    bot.mkdir(parents=True)
    holder = _registry(home, bot)
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    record = _recording_claude(claude, tmp_path / "claude-record")
    log = tmp_path / "startup.log"
    script = (f'. "{LIB}/lib-common.sh"\n{setup}\n'
              f'plugin_ensure "{PLUGINS[0]}" "{claude}" "{log}" {once_flag}\n')
    result = subprocess.run(["bash", "-c", script], cwd=holder, capture_output=True, text=True,
                            timeout=60, env=constructed_env(HOME=home, CLAUDLOBBY_ROOT=root))
    assert result.returncode == 0, result.stderr
    assert taken in log.read_text(), log.read_text()
    stamp = root / "state" / "boot" / f"plugins-updated.1000000000.{PLUGINS[0].replace('@', '_')}"
    assert stamp.exists() is stamps
    assert _calls(record) == [(holder, ["plugin", "update", "--scope", "user", PLUGINS[0]])]
