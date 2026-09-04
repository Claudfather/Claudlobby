"""Regression: fleet-pulse.sh must survive a bot with no events file (#610).

``_readback_efiles`` ended with ``[ -f ] && printf | sort -u``: when the last
date in the read-back span has no ledger, the failed ``[ -f ]`` becomes the
pipeline's exit status under ``pipefail``, and the ``_efiles=$(...)``
assignment call sites abort the whole pulse (exit 1) via ``set -e`` + the ERR
trap — killing fleet-wide escalation and the summary.

The trigger is an idle-but-HEALTHY bot: a dead-session bot gets today's ledger
created by the main loop's own ``session_missing`` emission, so only a bot with
a live tmux session and zero events all day leaves the ledger absent. One such
bot killed the pulse for the entire fleet, every tick.

CI runs pytest only, so the bash is exercised via subprocess. The end-to-end
test needs a real tmux server for the healthy-idle bot and skips where tmux is
unavailable; the helper-contract tests always run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env, read_fleet_events

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_PULSE = REPO_ROOT / "lib" / "fleet-pulse.sh"

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


# --- helper contract: _readback_efiles ---------------------------------------


def _readback_efiles(bot_dir: Path, today: str, rb_today: str) -> tuple[str, int]:
    """Extract ``_readback_efiles`` from fleet-pulse.sh and invoke it directly,
    under pipefail like the caller — without it the pipe into ``sort -u`` masks
    the failed ``[ -f ]`` and the bug is invisible."""
    script = (
        "set -o pipefail; "
        f'fn=$(sed -n "/^_readback_efiles() {{/,/^}}/p" "{FLEET_PULSE}"); '
        f'eval "$fn"; today="{today}"; _rb_today="{rb_today}"; '
        '_readback_efiles "$1"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(bot_dir)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return proc.stdout, proc.returncode


def test_readback_efiles_no_files_is_success(tmp_path):
    """No ledger for the span is a normal state: empty output, exit 0."""
    bot = tmp_path / "bots" / "idle"
    (bot / "data" / "events").mkdir(parents=True)
    out, rc = _readback_efiles(bot, TODAY, TODAY)
    assert rc == 0, "empty read-back span must not be an error"
    assert out == ""


def test_readback_efiles_lists_existing_files(tmp_path):
    """Listing must also exit 0 on the half-present straddle span (yesterday's
    file exists, today's not yet) — a second latent trigger."""
    bot = tmp_path / "bots" / "busy"
    events = bot / "data" / "events"
    events.mkdir(parents=True)
    yesterday_file = events / f"fleet-{YESTERDAY}.jsonl"
    yesterday_file.write_text('{"type":"tool_call"}\n')
    out, rc = _readback_efiles(bot, YESTERDAY, TODAY)
    assert rc == 0
    assert out.splitlines() == [str(yesterday_file)]

    # Identical span dates (the no-midnight-crossing case) dedup via sort -u.
    out, rc = _readback_efiles(bot, YESTERDAY, YESTERDAY)
    assert rc == 0
    assert out.splitlines() == [str(yesterday_file)]


# --- end-to-end: the pulse survives a no-events bot ---------------------------


needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="end-to-end pulse fixture needs tmux"
)

SOCKET = "pulse610"


def _tmux_env(root: Path) -> dict:
    """Pin the tmux rendezvous under the fixture root (per-test private)."""
    return {**os.environ, "TMUX_TMPDIR": str(root / "tmux")}


@pytest.fixture()
def pulse_fleet(tmp_path):
    """CLAUDLOBBY_ROOT fixture: fleet of two declared bots.

    - ``aaa-idle`` — live tmux session (own socket), zero events: the trigger.
      Globs first, so the sweep hits its empty read-back before the other bot.
    - ``zzz-logged`` — pre-seeded today ledger (the has-events control).
    """
    root = tmp_path / "root"
    fleet = "pulsefleet"
    bots = root / "local" / fleet / "runtime" / "bots"

    idle = bots / "aaa-idle"
    (idle / "data").mkdir(parents=True)
    (idle / "bot.conf").write_text(f"TMUX_SOCKET={SOCKET}\n")

    logged = bots / "zzz-logged"
    events = logged / "data" / "events"
    events.mkdir(parents=True)
    (events / f"fleet-{TODAY}.jsonl").write_text('{"type":"tool_call"}\n')

    # Only the 2-space `bots:` block and 4-space bot keys are read
    # (parse_fleet_bots).
    (root / "local" / fleet / "fleet.yaml").write_text(
        "fleet:\n  bots:\n    aaa-idle:\n    zzz-logged:\n"
    )

    (root / "tmux").mkdir()
    subprocess.run(
        ["tmux", "-L", SOCKET, "new-session", "-d", "-s", "aaa-idle"],
        check=True,
        timeout=20,
        env=_tmux_env(root),
    )
    try:
        yield root, fleet
    finally:
        subprocess.run(
            ["tmux", "-L", SOCKET, "kill-server"],
            capture_output=True,
            timeout=20,
            env=_tmux_env(root),
        )


def _run_pulse(root: Path, fleet: str, extra_env: dict) -> subprocess.CompletedProcess:
    """Run the real fleet-pulse.sh against the fixture root, hermetically:
    no inherited FLEET_*/BOT_*/TELEGRAM* (would reroute socket or chat
    resolution) and HOME pointed away from the real ~/.env."""
    env = _scrubbed_env(
        HOME=str(root / "home"),
        CLAUDLOBBY_ROOT=str(root),
        TMUX_TMPDIR=str(root / "tmux"),
        **extra_env,
    )
    return subprocess.run(
        ["bash", str(FLEET_PULSE), fleet],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _script_errors(root: Path) -> str:
    return "".join(
        line
        for line in read_fleet_events(root).splitlines(keepends=True)
        if '"type":"script_error"' in line
    )


@needs_tmux
@pytest.mark.parametrize(
    "extra_env",
    [
        pytest.param({}, id="summary-site"),
        pytest.param(
            {"FLEET_PULSE_ESCALATION_CHAT_ID": "-1001234567890"}, id="escalation-site"
        ),
    ],
)
def test_pulse_completes_with_no_events_bot(pulse_fleet, extra_env):
    """The pulse exits 0 with a full summary and no script_error, through both
    ``_readback_efiles`` call sites. With a chat id resolved, the escalation
    loop's site runs — the live abort of #610 ("non-zero exit at line 332").
    Without one, only the summary's site runs; it survives today purely because
    its ``{ ... } && mv`` shape suppresses ``set -e``, and this pin keeps it
    surviving if that shape ever changes.
    """
    root, fleet = pulse_fleet
    proc = _run_pulse(root, fleet, extra_env)
    assert proc.returncode == 0, (
        f"pulse aborted (rc={proc.returncode})\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}\nscript_error events:\n{_script_errors(root)}"
    )
    # The summary is the last section — reaching it with every declared bot
    # listed proves the sweep completed past the empty read-back.
    assert "aaa-idle" in proc.stdout
    assert "zzz-logged" in proc.stdout
    assert _script_errors(root) == ""


def test_a_healthy_bridge_check_fires_no_phantom_script_error(tmp_path):
    """Live-found 2026-09-03/04 (~2,500 rows a day on a 9-bot fleet, one per
    live bot per sweep, every one `non-zero exit at line 387`): on bash 3.2
    a function that returns 1 as the LAST command inside a `$( )` fires the
    inherited ERR trap even when the substitution is an `if` condition —
    bridge_down_state returns 1 on every HEALTHY bot. The demonstration runs
    the two shapes under the real trap installer; the shape pin guards the
    line in fleet-pulse.sh."""
    src = (REPO_ROOT / "lib" / "fleet-pulse.sh").read_text()
    assert '_bridge_st=$(bridge_down_state "$bot_dir" "$_bridge_grace" || true)' in src
    assert 'if _bridge_st=$(bridge_down_state' not in src
    # The class is bash 3.2's (macOS /bin/bash — the Mini, where it was
    # measured); bash 4+ (Linux, CI) exempts the substitution as part of the
    # `if` test, so there the old shape is quiet too. The demonstration asserts
    # the phantom only where the shell exhibits it; the fixed shape is quiet
    # everywhere.
    major = int(subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"],
                               capture_output=True, text=True).stdout.strip() or "0")
    for shape, body, want in (
        ("old", 'if x=$(healthy); then :; fi', 1 if major < 4 else 0),
        ("new", 'x=$(healthy || true); if [ -n "$x" ]; then :; fi', 0),
    ):
        root = tmp_path / shape
        (root / "bot" / "data").mkdir(parents=True)
        (root / "state").mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "CLAUDLOBBY_ROOT": str(root),
               "BOT_DIR": str(root / "bot"), "BOT_ID": "b", "PLANE_EMIT_ENABLED": "0"}
        r = subprocess.run(["/bin/bash", "-c",
                            f'. "{REPO_ROOT}/lib/lib-common.sh"; install_error_trap "";'
                            f' healthy() {{ return 1; }}; {body}; echo done'],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "done" in r.stdout, (shape, r.stderr)
        rows = "".join(p.read_text() for p in root.rglob("fleet-*.jsonl"))
        assert rows.count('"type":"script_error"') == want, (shape, rows)
