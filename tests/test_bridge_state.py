"""Tests for the `bridge_state` lib-common.sh helper (#453 Tier-1, PR1).

`bridge_state <bot_dir>` classifies a bot's Telegram inbound bridge — the
`bun server.ts` MCP child of its `claude` — into exactly one state:

    up | no_bridge | no_token | no_handle | unknown

These tests pin the two pieces of rigor the /ironclad cycle-1 review demanded:

  C1  The token VALUE is not in bot.conf (only the var NAME is); it lives in the
      .env chain, which keepalive/fleet-pulse never source. bridge_state must
      source_env_tiered or it misreads every bot as `no_token`. The C1 fixture
      below puts the token ONLY in the bot .env and asserts `no_bridge` (not
      `no_token`) — a naive implementation fails it.

  M4  `up` requires the poller to be OWNED by this bot (an exact, NUL-delimited
      TELEGRAM_STATE_DIR env match — no sibling-prefix collision) AND
      lineage-proven (parented by a live `claude`, not an orphan holding the
      single-consumer token slot while delivering nothing).
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"

# The ownership/lineage checks read /proc/<pid>/environ — Linux only.
_LINUX = Path("/proc").is_dir()
requires_proc = pytest.mark.skipif(
    not _LINUX, reason="bridge_state ownership/lineage checks require Linux /proc"
)


def _bridge_state(bot_dir: Path, home: Path) -> tuple[str, int]:
    """Source lib-common.sh and run `bridge_state <bot_dir>`; return (stdout, rc).

    HOME is pinned to an isolated dir so source_env_tiered cannot read the real
    ~/.env (hermetic; no real secrets, no cross-test bleed).
    """
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; bridge_state "$2"', "_", str(LIB_COMMON), str(bot_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    return proc.stdout.strip(), proc.returncode


def _write_bot_conf(bot_dir: Path, *, handle="b1", state_dir=None, token_env=None):
    """Write a minimal bot.conf. Note: the token VALUE is never written here —
    bot.conf only names the var via TELEGRAM_TOKEN_ENV_NAME (as composer does)."""
    bot_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"export CLAUDLOBBY_ROOT={bot_dir.parent}",
        "export FLEET_NAME=test-fleet",
        f'BOT_DIR="{bot_dir}"',
    ]
    if handle is not None:
        lines.append(f"export TELEGRAM_BOT_HANDLE={handle}")
    if state_dir is not None:
        lines.append(f'TELEGRAM_STATE_DIR="{state_dir}"')
    if token_env is not None:
        lines.append(f"export TELEGRAM_TOKEN_ENV_NAME={token_env}")
    (bot_dir / "bot.conf").write_text("\n".join(lines) + "\n")


def _fake_bins(tmp_path: Path) -> Path:
    """Copies of bash named `bun` and `claude` so /proc/<pid>/comm reads as such."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    bash = shutil.which("bash")
    assert bash, "bash not found"
    for name in ("bun", "claude"):
        dst = bindir / name
        shutil.copy(bash, dst)
        os.chmod(dst, 0o755)
    return bindir


def _wait_pidfile(pidfile: Path):
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            return
        time.sleep(0.05)
    raise AssertionError(f"pidfile never written: {pidfile}")


def _spawn_bridge(bindir, state_dir, *, owned=True, env_state_dir=None):
    """Spawn a fake `bun server.ts` poller that writes its pid to
    <state_dir>/bot.pid and stays alive.

    owned=True reproduces the PRODUCTION process tree, where `claude` is the
    poller's GRANDPARENT, not its direct parent:

        claude → bun (`bun … start`, the package.json start script bun runs in a
                 child bun) → bun server.ts   ← bot.pid

    The telegram plugin's MCP command is `bun … start`, so the leaf poller's
    direct parent is always a `bun` spawn-shim. A fixture that forked the poller
    directly under `claude` (claude == direct parent) hid a 100%-false-positive
    lineage bug — every healthy live bridge read `no_bridge`. The tree is built
    from script files (not nested `bash -c`) to dodge three-deep quote nesting;
    each level backgrounds the next and `wait`s, and the leaf ends on a builtin
    (`true`) so bash's last-command exec optimization can't replace comm=bun with
    `sleep`.

    owned=False spawns the leaf directly (orphan — its parent is the test
    runner, a non-`claude`/non-shim process). Returns the Popen heading the
    process group (kill via _kill_tree)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    env_sd = env_state_dir if env_state_dir is not None else str(state_dir)
    bun = bindir / "bun"
    pidfile = state_dir / "bot.pid"
    env = {**os.environ, "TELEGRAM_STATE_DIR": env_sd}
    leaf = bindir / "leaf.sh"
    leaf.write_text(f'echo $$ > "{pidfile}"\nsleep 60\ntrue\n')
    if owned:
        claude = bindir / "claude"
        wrapper = bindir / "wrapper.sh"  # the `bun … start` shim (comm=bun)
        wrapper.write_text(f'"{bun}" "{leaf}" server.ts &\nwait\n')
        tree = bindir / "tree.sh"  # run by claude — spawns the wrapper bun
        tree.write_text(f'"{bun}" "{wrapper}" start &\nwait\n')
        cmd = [str(claude), str(tree)]
    else:
        cmd = [str(bun), str(leaf), "server.ts"]
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    _wait_pidfile(pidfile)
    return proc


def _kill_tree(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


# --- classification (no live bridge needed) ---------------------------------


def test_no_handle(tmp_path):
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle=None)
    out, rc = _bridge_state(bot, tmp_path)
    assert out == "no_handle"
    assert rc != 0


def test_no_token_when_token_absent_everywhere(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    # B1_TG_TOKEN is defined in no .env tier at all.
    out, rc = _bridge_state(bot, tmp_path)
    assert out == "no_token"
    assert rc != 0


def test_c1_token_in_env_not_bot_conf_reads_no_bridge(tmp_path):
    """C1 (critical): token VALUE only in the bot .env. bridge_state must
    source_env_tiered → token resolves → NOT no_token. Bridge is down (no
    bot.pid) → no_bridge. A naive helper reads no_token and FAILS here."""
    sd = tmp_path / "state"
    sd.mkdir()  # exists, but no bot.pid → bridge down
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=8888888:AAAAAAAAAAAAAAAAAAAA\n")
    out, rc = _bridge_state(bot, tmp_path)
    assert out == "no_bridge", (
        f"expected no_bridge (token resolved from .env), got {out!r}"
    )
    assert rc != 0


def test_no_bridge_when_pid_dead(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "bot.pid").write_text("2147483640\n")  # almost certainly not a live pid
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=x\n")
    out, _ = _bridge_state(bot, tmp_path)
    assert out == "no_bridge"


def test_no_bridge_when_pid_alive_but_not_bun(tmp_path):
    """Footgun guard: a live non-bun process is not a bridge."""
    sd = tmp_path / "state"
    sd.mkdir()
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (sd / "bot.pid").write_text(f"{proc.pid}\n")
        bot = tmp_path / "bots" / "b1"
        _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
        (bot / ".env").write_text("B1_TG_TOKEN=x\n")
        out, _ = _bridge_state(bot, tmp_path)
        assert out == "no_bridge"
    finally:
        proc.terminate()
        proc.wait()


# --- ownership + lineage (live fake bridge; Linux /proc) ---------------------


@requires_proc
def test_up_when_claude_is_grandparent_via_bun_shim(tmp_path):
    """M4 lineage (production shape): the poller's DIRECT parent is the
    `bun … start` shim and `claude` is the GRANDPARENT (see _spawn_bridge). The
    lineage check must walk past bun shims to find the live `claude` ancestor —
    a direct-parent-only check reads `no_bridge` for every healthy live bridge."""
    bindir = _fake_bins(tmp_path)
    sd = tmp_path / "state"
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=x\n")
    proc = _spawn_bridge(bindir, sd)
    try:
        out, rc = _bridge_state(bot, tmp_path)
        assert out == "up", f"got {out!r}"
        assert rc == 0
    finally:
        _kill_tree(proc)


@requires_proc
def test_orphan_bun_without_claude_parent_is_not_up(tmp_path):
    """M4 lineage: a bun whose ancestry holds no live `claude` (orphaned →
    reparented to the session subreaper) holds the single-consumer slot but
    delivers nothing → must NOT read up. Doubles as the over-walk guard: the
    leaf's first non-shim ancestor is the test runner, so a walk that chased a
    distant unrelated `claude` (e.g. the pytest session's own) would wrongly
    read `up` here — it must stop at the first non-shim ancestor."""
    bindir = _fake_bins(tmp_path)
    sd = tmp_path / "state"
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=x\n")
    proc = _spawn_bridge(bindir, sd, owned=False)
    try:
        out, _ = _bridge_state(bot, tmp_path)
        assert out == "no_bridge", f"orphan must not be up; got {out!r}"
    finally:
        _kill_tree(proc)


@requires_proc
def test_sibling_prefix_env_does_not_match(tmp_path):
    """M4 ownership: a live owned-looking bun whose TELEGRAM_STATE_DIR is a
    PREFIX of ours (telegram-data vs telegram-data-eng) must NOT match — the env
    comparison is exact/NUL-delimited, not a substring grep."""
    bindir = _fake_bins(tmp_path)
    sd_ours = tmp_path / "ch" / "telegram-data"
    sd_sibling = tmp_path / "ch" / "telegram-data-eng"
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd_ours, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=x\n")
    sd_ours.mkdir(parents=True)
    # bot.pid (under ours) points at a bun whose environ names the SIBLING dir.
    proc = _spawn_bridge(bindir, sd_ours, env_state_dir=str(sd_sibling))
    try:
        out, _ = _bridge_state(bot, tmp_path)
        assert out == "no_bridge", f"sibling-prefix must not match; got {out!r}"
    finally:
        _kill_tree(proc)


# --- the heal trigger: kill a live bridge, confirm it becomes actionable-down ---


@requires_proc
def test_killed_bridge_flips_to_no_bridge_and_actionable_down(tmp_path):
    """The #453 Phase 5 heal trigger. A live owned bridge reads `up`; once its
    poller is killed (a deterministic, Mode-A-shaped kill), bridge_state flips to
    `no_bridge` AND bridge_down_state (grace 0) returns the actionable `no_bridge`
    verdict that keepalive's _bridge_heal consumes to bounce the bot. This is the
    unit-level companion to the end-to-end heal proof in validate-bot-change.sh."""
    bindir = _fake_bins(tmp_path)
    sd = tmp_path / "state"
    bot = tmp_path / "bots" / "b1"
    _write_bot_conf(bot, handle="b1", state_dir=sd, token_env="B1_TG_TOKEN")
    (bot / ".env").write_text("B1_TG_TOKEN=x\n")
    proc = _spawn_bridge(bindir, sd)
    try:
        out, _ = _bridge_state(bot, tmp_path)
        assert out == "up", f"live bridge should read up; got {out!r}"
    finally:
        _kill_tree(proc)

    # Poller dead (bot.pid now points at a dead pid) → classifier flips to down.
    out2, rc2 = _bridge_state(bot, tmp_path)
    assert out2 == "no_bridge", f"killed bridge must read no_bridge; got {out2!r}"
    assert rc2 != 0

    # bridge_down_state with no grace surfaces it as the actionable heal trigger.
    env = {**os.environ, "HOME": str(tmp_path)}
    down = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; bridge_down_state "$2" 0',
            "_",
            str(LIB_COMMON),
            str(bot),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert down.stdout.strip() == "no_bridge", f"got {down.stdout!r}"
    assert down.returncode == 0
