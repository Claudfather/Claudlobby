"""`mcp_auth_cache_clear` and the readiness poll's tick hook (lib-common.sh, #1962).

Claude Code SKIPS spawning any MCP server listed in the host-global
`mcp-needs-auth-cache.json`, and any bot on the host can write it. One failed
Telegram channel start in one bot therefore left every bot started after it
without a poller, restart-immune, until a human emptied the file (#1358, #1962).

The fix empties the cache before a bot's session starts its MCP servers, and
again on every readiness-poll tick while the bot waits for its own poller, so
one bot's failure cannot reach the others. Pinned here:

- an armed cache is emptied to `{}` by a rename beside it (a new inode), never
  a truncate in place that a concurrent reader could catch half-written, and
  the line names what it removed;
- a cache with nothing in it is never rewritten;
- a cache that cannot be emptied says so, keeps the remedy, and still returns 0;
- the poll calls the tick hook, and nothing the hook prints reaches the one
  state the poll reports.

Every call runs `/bin/bash` with a PATH that holds no `timeout` or `gtimeout`,
the macOS hosts' conditions. On the macOS CI job (`.github/workflows/macos-shell.yml`)
`/bin/bash` is 3.2 and the userland is BSD.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"

# The shape a live armed host cache holds (see test_mcp_auth_cache_note.py).
ARMED = (
    '{"plugin:telegram:telegram":{"timestamp":1789912141541,"id":"3eaf116ce58465c5"}}'
)


@pytest.fixture()
def run(sysbin_excluding):
    """Run a snippet under /bin/bash with lib-common sourced, HOME pinned to a
    throwaway, and no timeout/gtimeout on PATH. Returns (stdout, rc)."""
    path = str(sysbin_excluding("timeout", "gtimeout"))

    def _run(home: Path, snippet: str, *args: str):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env.update(HOME=str(home), PATH=path)
        proc = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'. "$1"; shift; {snippet}',
                "_",
                str(LIB_COMMON),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return proc.stdout.strip(), proc.returncode

    return _run


def _cache(home: Path, content: str | None) -> Path:
    cfg = home / ".claude"
    cfg.mkdir(parents=True, exist_ok=True)
    f = cfg / "mcp-needs-auth-cache.json"
    if content is not None:
        f.write_text(content)
    return f


def test_the_conditions_are_the_macos_ones(run, tmp_path):
    """The positive control for the fixture: no timeout or gtimeout resolves."""
    out, rc = run(tmp_path, "command -v timeout gtimeout || echo none")
    assert out == "none", out


def test_an_armed_cache_is_emptied_by_a_rename_and_the_line_names_what_went(
    run, tmp_path
):
    f = _cache(tmp_path, ARMED)
    inode = f.stat().st_ino
    out, rc = run(tmp_path, "mcp_auth_cache_clear")
    assert rc == 0
    assert f.read_text().strip() == "{}"
    assert f.stat().st_ino != inode, (
        "emptied in place: a concurrent reader can catch it torn"
    )
    assert out.startswith("AUTH_CACHE_CLEARED — "), out
    assert "plugin:telegram:telegram (recorded 2026-" in out
    assert str(f) in out
    assert out.count("\n") == 0, "one greppable line"
    assert not list(f.parent.glob(".mcp-needs-auth-cache.*")), "temp file left behind"


@pytest.mark.parametrize("content", [None, "{}", "", "  \n", "{ }\n"])
def test_a_cache_holding_nothing_is_never_rewritten(run, tmp_path, content):
    f = _cache(tmp_path, content)
    before = f.stat() if f.exists() else None
    out, rc = run(tmp_path, "mcp_auth_cache_clear")
    assert (out, rc) == ("", 0)
    if before is None:
        assert not f.exists(), "an absent cache must not be created"
    else:
        after = f.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_an_unparseable_cache_is_emptied_too(run, tmp_path):
    """Whatever it holds, a session may skip on it: it goes, and the line says
    its content could not be read rather than inventing a listing."""
    f = _cache(tmp_path, "{not json")
    out, rc = run(tmp_path, "mcp_auth_cache_clear")
    assert rc == 0
    assert f.read_text().strip() == "{}"
    assert out.startswith("AUTH_CACHE_CLEARED — ") and "could not be read" in out, out


def test_a_cache_this_cannot_read_is_replaced_not_taken_for_empty(run, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    f = _cache(tmp_path, ARMED)
    f.chmod(0o000)
    out, rc = run(tmp_path, "mcp_auth_cache_clear")
    assert rc == 0
    assert out.startswith("AUTH_CACHE_CLEARED — ") and "could not be read" in out, out
    assert f.read_text().strip() == "{}"


def test_a_cache_that_cannot_be_emptied_says_so_and_never_fails_the_caller(
    run, tmp_path
):
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only directory")
    f = _cache(tmp_path, ARMED)
    f.parent.chmod(0o555)
    try:
        out, rc = run(tmp_path, "mcp_auth_cache_clear; echo rc=$?")
    finally:
        f.parent.chmod(0o755)
    assert out.endswith("rc=0"), out
    line = out.splitlines()[0]
    assert line.startswith("AUTH_CACHE_NOT_CLEARED — "), line
    assert "plugin:telegram:telegram" in line
    assert f"printf '{{}}' > {f}" in line, "the remedy stays on the line"
    assert f.read_text() == ARMED


def test_a_given_cache_file_is_the_one_emptied(run, tmp_path):
    """start-bot resolves the path once and hands it to every poll tick."""
    other = tmp_path / "elsewhere" / "mcp-needs-auth-cache.json"
    other.parent.mkdir()
    other.write_text(ARMED)
    home_cache = _cache(tmp_path, ARMED)
    out, rc = run(tmp_path, 'mcp_auth_cache_clear "" "$1"', str(other))
    assert rc == 0 and other.read_text().strip() == "{}"
    assert home_cache.read_text() == ARMED


def test_the_poll_calls_its_tick_hook_and_keeps_its_stdout_to_the_state(run, tmp_path):
    """A channel bot with a token and no poller: the poll runs to its 1 s
    ceiling. The hook must run on every tick, and its noise on stdout must not
    reach the single state the poll prints."""
    bot = tmp_path / "bot"
    (bot / "state").mkdir(parents=True)
    (bot / "bot.conf").write_text(
        'TELEGRAM_BOT_HANDLE="tbot"\n'
        'TELEGRAM_TOKEN_ENV_NAME="TBOT_TOKEN"\n'
        f'TELEGRAM_STATE_DIR="{bot}/state"\n'
    )
    ticks = tmp_path / "ticks"
    out, rc = run(
        tmp_path,
        'T="$2"; tick() { echo NOISE; echo x >> "$T"; }; '
        'wait_bridge_ready_state "$1" 1 "" "8888888:AAAAAAAAAAAAAAAAAAAA" "" "" tick || rc=$?; echo " rc=${rc:-0}"',
        str(bot),
        str(ticks),
    )
    assert out == "no_bridge rc=1", out
    assert ticks.exists() and len(ticks.read_text().splitlines()) >= 2
