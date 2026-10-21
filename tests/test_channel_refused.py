"""A Telegram channel Claude Code refused at connect reads as refused, never ready (#2423).

Claude Code records whether it delivers a channel plugin's messages in that
plugin's MCP log and nowhere else, and a refusal leaves the poller up and owned
by the session, so bridge_state reads `up` either way. These drive the doors
that read the decision (lib-common.sh's channel_mcp_log and channel_decision,
bring-up's bridge_bringup_verify, the restart gate's wait_bridge_ready) and the
real keepalive tick, against logs shaped like live captures
(tests/fixtures/channel_mcp_log.py) under a temporary HOME.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.isolation import transcript_slug
from tests.conftest import constructed_env
from tests.fixtures.channel_mcp_log import CONNECTING, REASON, capture, decision_line, log_dir, stamp, write_log
from tests.test_keepalive_usage_limit import FIXTURES as PANES
from tests.test_keepalive_usage_limit import Rig

LIB_COMMON = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts" / "lib-common.sh"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def bot(tmp_path):
    b = tmp_path / "bots" / "b1"
    (b / "data").mkdir(parents=True)
    return b


def _lib_argv(script: str, *args: str) -> list[str]:
    return ["bash", "-c", '. "$1"; shift; ' + script, "_", str(LIB_COMMON), *args]


def _lib_env(home: Path, **extra: str) -> dict:
    return constructed_env(**{"HOME": home, "XDG_CACHE_HOME": home / ".cache", **extra})


def _lib(home: Path, script: str, *args: str, timeout: int = 30, **extra: str) -> str:
    r = subprocess.run(_lib_argv(script, *args), capture_output=True, text=True,
                       env=_lib_env(home, **extra), timeout=timeout)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _decision(home: Path, log: Path) -> str:
    return _lib(home, 'channel_decision "$1"', str(log)).rstrip("\n")


def _log(home: Path, bot: Path, **extra: str) -> tuple[int, str]:
    """channel_mcp_log's answer: (0, path) or (1, why)."""
    out = _lib(home, 'if channel_mcp_log "$1"; then echo " 0"; else echo " 1"; fi', str(bot), **extra)
    text, rc = out.rstrip("\n").rsplit(" ", 1)
    return int(rc), text


def _age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


# --- channel_decision: the last decision in a log ---------------------------------


def test_a_refused_log_reads_refused_with_claude_codes_reason(home, bot):
    log = write_log(home, bot, capture("refused"))
    assert _decision(home, log) == f"refused\t{REASON}"


def test_a_registered_log_reads_registered(home, bot):
    log = write_log(home, bot, capture("registered"))
    assert _decision(home, log) == "registered"


def test_the_last_decision_in_the_log_is_the_current_one(home, bot):
    """A session decides again at every reconnect, so a log can hold both."""
    log = write_log(home, bot, capture("refused") + decision_line("registered"))
    assert _decision(home, log) == "registered"
    log.write_text(log.read_text() + decision_line("refused"))
    assert _decision(home, log) == f"refused\t{REASON}"


def test_a_log_with_no_decision_yet_reads_pending(home, bot):
    log = write_log(home, bot, CONNECTING)
    assert _decision(home, log) == "pending"


def test_the_servers_own_output_cannot_pass_for_a_decision(home, bot):
    """The plugin server's stderr lands in the log JSON-escaped: a message that
    quotes the decision is not one."""
    spoof = (
        '{"error":"Server stderr: {\\"debug\\":\\"Channel notifications registered\\"}\\n",'
        '"timestamp":"2026-10-21T16:05:30.000Z"}\n'
    )
    log = write_log(home, bot, CONNECTING + spoof)
    assert _decision(home, log) == "pending"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_log_reads_unknown(home, bot):
    log = write_log(home, bot, capture("registered"))
    log.chmod(0)
    try:
        assert _decision(home, log) == f"unknown\tcannot read {log}"
    finally:
        log.chmod(0o600)


# --- channel_mcp_log: the current session's log, where Claude Code writes it ------


def test_the_newest_session_is_read_not_an_older_one(home, bot):
    now = time.time()
    write_log(home, bot, capture("refused"), epoch=now - 7200)
    newest = write_log(home, bot, capture("registered"), epoch=now - 60)
    assert _log(home, bot) == (0, str(newest))


def test_no_log_reads_unknown_naming_where_it_looked(home, bot):
    rc, why = _log(home, bot)
    assert rc == 1
    assert why == f"no channel MCP log in {log_dir(home, bot)}"


def test_a_file_named_any_other_way_is_not_read(home, bot):
    d = log_dir(home, bot)
    d.mkdir(parents=True)
    (d / "latest.jsonl").write_text(capture("registered"))
    assert _log(home, bot)[0] == 1


def test_a_previous_sessions_log_is_not_this_sessions(home, bot):
    """A log named for a start before the spawn marker is a previous session's."""
    spawn = bot / "data" / ".spawn"
    spawn.touch()
    write_log(home, bot, capture("registered"), epoch=time.time() - 3600)
    rc, why = _log(home, bot)
    assert rc == 1 and why.startswith(f"no channel MCP log in {log_dir(home, bot)} from the session started at ")
    current = write_log(home, bot, capture("refused"), epoch=time.time() + 1)
    assert _log(home, bot) == (0, str(current))


def test_the_cwd_key_is_the_one_transcript_slug_states(home, tmp_path):
    """claude_cwd_slug is transcript_slug's shell twin: a cwd with dots and
    underscores lands where Claude Code puts it."""
    odd = tmp_path / "bots" / "b.one_two"
    odd.mkdir(parents=True)
    assert _lib(home, 'claude_cwd_slug "$1"', str(odd)) == transcript_slug(os.path.realpath(odd))
    log = write_log(home, odd, capture("registered"))
    assert _log(home, odd) == (0, str(log))


@pytest.mark.skipif(sys.platform == "darwin", reason="XDG_CACHE_HOME moves the cache on Linux only")
def test_xdg_cache_home_moves_the_cache_on_linux(home, bot, tmp_path):
    elsewhere = tmp_path / "xdg"
    d = elsewhere / "claude-cli-nodejs" / transcript_slug(os.path.realpath(bot)) / "mcp-logs-plugin-telegram-telegram"
    d.mkdir(parents=True)
    log = d / f"{stamp(time.time())}.jsonl"
    log.write_text(capture("refused"))
    assert _log(home, bot, XDG_CACHE_HOME=str(elsewhere)) == (0, str(log))


# --- bring-up: bridge_bringup_verify with the poller up ---------------------------

_VERIFY = (
    'bridge_state() { printf up; }\n'
    'emit_failure_alert() { printf "%s\\t%s\\n" "$2" "$3" >> "$STUB_ALERTS"; }\n'
    'bridge_bringup_verify "$1" "$(dirname "$1")" 0\n'
)


def _verify(home: Path, bot: Path, **extra: str) -> tuple[str, list[str]]:
    """The verdict, and each alert raised as `<event type>\\t<text>`."""
    alerts = home / "alerts.log"
    verdict = _lib(home, _VERIFY, str(bot), STUB_ALERTS=str(alerts), **extra)
    return verdict, alerts.read_text().splitlines() if alerts.exists() else []


def test_bring_up_reads_a_refused_channel_as_refused_not_ready(home, bot):
    """The #2423 shape: poller up and ours, channel refused. Main prints `ready`."""
    log = write_log(home, bot, capture("refused"))
    verdict, alerts = _verify(home, bot)
    assert verdict == f"refused\t{REASON}"
    assert len(alerts) == 1
    kind, text = alerts[0].split("\t", 1)
    assert kind == "channel_refused"
    assert "b1 Telegram channel refused by Claude Code (bring-up)" in text
    assert REASON in text
    assert "a restart on the same login comes up refused again" in text
    assert (bot / "data" / ".channel-refused").read_text() == f"{log.name}\n"


def test_bring_up_raises_one_alert_per_session(home, bot):
    write_log(home, bot, capture("refused"))
    _verify(home, bot)
    verdict, alerts = _verify(home, bot)
    assert verdict.startswith("refused\t")
    assert len(alerts) == 1


def test_bring_up_reads_a_registered_channel_as_ready(home, bot):
    (bot / "data" / ".channel-refused").write_text("old.jsonl\n")
    write_log(home, bot, capture("registered"))
    assert _verify(home, bot) == ("ready", [])
    assert not (bot / "data" / ".channel-refused").exists()


def test_bring_up_with_no_log_is_unknown_never_ready(home, bot):
    assert _verify(home, bot) == (f"unknown:channel\tno channel MCP log in {log_dir(home, bot)}", [])


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_bring_up_with_an_unreadable_log_is_unknown_never_ready(home, bot):
    log = write_log(home, bot, capture("refused"))
    log.chmod(0)
    try:
        assert _verify(home, bot) == (f"unknown:channel\tcannot read {log}", [])
    finally:
        log.chmod(0o600)


def test_bring_up_with_no_decision_in_time_is_unknown(home, bot):
    log = write_log(home, bot, CONNECTING)
    assert _verify(home, bot, CHANNEL_DECISION_WAIT_S="1") == (f"unknown:channel\tno decision in {log} after 1s", [])


def test_bring_up_waits_for_a_decision_that_follows_the_connect(home, bot):
    """The decision lands a fraction of a second after the server connects."""
    log = write_log(home, bot, CONNECTING)
    proc = subprocess.Popen(_lib_argv(_VERIFY, str(bot)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=_lib_env(home, STUB_ALERTS=str(home / "alerts.log"), CHANNEL_DECISION_WAIT_S="20"))
    time.sleep(1.5)
    with log.open("a") as fh:
        fh.write(decision_line("refused"))
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    assert out == f"refused\t{REASON}"


def test_bring_up_does_not_read_a_log_from_before_the_session(home, bot):
    """The previous session's refusal is not this session's decision."""
    (bot / "data" / ".spawn").touch()
    write_log(home, bot, capture("refused"), epoch=time.time() - 3600)
    verdict, alerts = _verify(home, bot, CHANNEL_DECISION_WAIT_S="0")
    assert verdict.startswith("unknown:channel\tno channel MCP log in ")
    assert alerts == []


# --- the restart gate: wait_bridge_ready after the fence -------------------------

_GATE = (
    'started=$(date +%s)\n'
    'if wait_bridge_ready "$1" "$2" RR_FENCE_1_b1; then rc=0; else rc=$?; fi\n'
    'printf "%s %s %s" "$rc" "${WAIT_BRIDGE_READY_OUTCOME:-none}" "$(( $(date +%s) - started ))"\n'
)


def _gate(home: Path, bot: Path, lines: list[str], ceiling: int = 9) -> tuple[int, str, int]:
    (bot / "logs").mkdir(exist_ok=True)
    (bot / "logs" / "startup.log").write_text("".join(f"2026-10-21T12:47:1{i}Z {line}\n" for i, line in enumerate(lines)))
    rc, outcome, waited = _lib(home, _GATE, str(bot), str(ceiling), timeout=ceiling + 20).split()
    return int(rc), outcome, int(waited)


def test_the_gate_reads_a_refused_boot_as_not_ready_at_once(home, bot):
    rc, outcome, waited = _gate(home, bot, [
        "RR_FENCE_1_b1",
        "READY — Telegram poller up after 3s",
        'BRIDGE_REFUSED — Telegram poller up, but Claude Code refused the channel at connect ("x")',
    ])
    assert (rc, outcome) == (1, "refused")
    assert waited < 3


def test_the_gate_reads_an_unread_channel_decision_as_not_ready_at_once(home, bot):
    rc, outcome, waited = _gate(home, bot, [
        "RR_FENCE_1_b1",
        "BRIDGE_UNVERIFIED — Telegram poller up, but the channel decision could not be read (x); not ready",
    ])
    assert (rc, outcome) == (1, "unknown")
    assert waited < 3


def test_the_gate_still_passes_a_ready_boot(home, bot):
    rc, outcome, _ = _gate(home, bot, ["RR_FENCE_1_b1", "BRIDGE_READY — Telegram poller up"])
    assert (rc, outcome) == (0, "bridge")


def test_a_refusal_before_the_fence_is_a_previous_boots(home, bot):
    rc, outcome, waited = _gate(home, bot, [
        'BRIDGE_REFUSED — Telegram poller up, but Claude Code refused the channel at connect ("x")',
        "RR_FENCE_1_b1",
    ], ceiling=3)
    assert (rc, outcome) == (1, "none")
    assert waited >= 3


# --- keepalive: the real tick reads a running session's decision ----------------


def _channel_rig(tmp_path: Path, *, handle: bool = True, spawn_age: int = 600) -> Rig:
    """test_keepalive_usage_limit's rig (the real keepalive.sh tick against a tmux
    stub serving a live idle frame, HOME at tmp_path) on a channel bot."""
    r = Rig(tmp_path, {}, False)
    if handle:
        with (r.bot / "bot.conf").open("a") as fh:
            fh.write('TELEGRAM_BOT_HANDLE="example_bot"\n')
    spawn = r.bot / "data" / ".spawn"
    spawn.touch()
    _age(spawn, spawn_age)
    r.show((PANES / "idle-prompt.txt").read_text())
    return r


def _alerts(proc: subprocess.CompletedProcess) -> int:
    """emit_failure_alert's own disclosure of an alert it could not deliver: this
    rig has no alert target, so every raised alert prints one."""
    return proc.stderr.count("ALERT-DELIVERY-FAILED channel_refused")


def test_keepalive_raises_a_refused_channel_once_per_session(tmp_path):
    r = _channel_rig(tmp_path)
    log = write_log(r.tmp, r.bot, capture("refused"), epoch=time.time() - 500)
    first = r.tick()
    assert _alerts(first) == 1, first.stderr
    assert r.log().count("CHANNEL_REFUSED") == 1
    assert REASON in r.log()
    assert r.data(".channel-refused").read_text() == f"{log.name}\n"
    second = r.tick()
    assert _alerts(second) == 0
    assert r.log().count("CHANNEL_REFUSED") == 1


def test_keepalive_raises_a_refusal_a_reconnect_brought_into_a_registered_session(tmp_path):
    r = _channel_rig(tmp_path)
    log = write_log(r.tmp, r.bot, capture("registered"), epoch=time.time() - 500)
    assert _alerts(r.tick()) == 0
    log.write_text(log.read_text() + decision_line("refused"))
    assert _alerts(r.tick()) == 1
    assert "CHANNEL_REFUSED" in r.log()


def test_keepalive_clears_the_refusal_once_registered_again(tmp_path):
    r = _channel_rig(tmp_path)
    log = write_log(r.tmp, r.bot, capture("refused"), epoch=time.time() - 500)
    r.tick()
    log.write_text(log.read_text() + decision_line("registered"))
    assert _alerts(r.tick()) == 0
    assert not r.data(".channel-refused").exists()
    assert "registered the Telegram channel again; refusal cleared" in r.log()


def test_keepalive_leaves_a_fresh_session_to_bring_up(tmp_path):
    r = _channel_rig(tmp_path, spawn_age=10)
    write_log(r.tmp, r.bot, capture("refused"))
    assert _alerts(r.tick()) == 0
    assert "CHANNEL_REFUSED" not in r.log()


def test_keepalive_does_not_read_a_bot_without_a_channel(tmp_path):
    r = _channel_rig(tmp_path, handle=False)
    write_log(r.tmp, r.bot, capture("refused"), epoch=time.time() - 500)
    assert _alerts(r.tick()) == 0
    assert "CHANNEL" not in r.log()


def test_keepalive_changes_nothing_on_an_unknown_decision(tmp_path):
    r = _channel_rig(tmp_path)
    r.data(".channel-refused").write_text("old.jsonl\n")
    assert _alerts(r.tick()) == 0
    assert r.data(".channel-refused").exists()
    assert "CHANNEL" not in r.log()
