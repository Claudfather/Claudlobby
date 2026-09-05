"""Regression: fleet-pulse.sh must survive a bot with no events (#610).

The 2026-06 bug: ``_readback_efiles`` ended with ``[ -f ] && printf | sort -u``
and a bot with a live session and zero events all day left today's ledger
absent, so the failed ``[ -f ]`` became the pipeline's exit status under
``pipefail`` and the whole pulse aborted through ``set -e`` + the ERR trap.

F18 closure R2b-2: the events read-back reads the PLANE and nothing else —
``_readback_efiles`` and the dated files are gone, so the helper-contract
tests ``test_readback_efiles_no_files_is_success`` and
``test_readback_efiles_lists_existing_files`` went with them. The end-to-end
pin keeps its subject: a healthy, idle bot with zero events on the plane must
not abort the sweep at either read-back site.

CI runs pytest only, so the bash is exercised via subprocess. The end-to-end
test needs a real tmux server for the healthy-idle bot and skips where tmux is
unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import _scrubbed_env, read_fleet_events

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_PULSE = REPO_ROOT / "lib" / "fleet-pulse.sh"

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
    - ``zzz-logged`` — no session, so the sweep itself lands its
      session_missing on the plane (the has-events control).
    """
    root = tmp_path / "root"
    fleet = "pulsefleet"
    bots = root / "local" / fleet / "runtime" / "bots"

    idle = bots / "aaa-idle"
    (idle / "data").mkdir(parents=True)
    (idle / "bot.conf").write_text(f"TMUX_SOCKET={SOCKET}\n")

    logged = bots / "zzz-logged"
    (logged / "data").mkdir(parents=True)
    (logged / "bot.conf").write_text("TMUX_SOCKET=pulse610-none\n")

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
    read-back sites (the plane's one read for the escalation window and for
    the summary span). With a chat id resolved, the escalation loop's site runs
    — the live abort of #610 ("non-zero exit at line 332"). Without one, only
    the summary's site runs; it survives purely because its ``{ ... } && mv``
    shape suppresses ``set -e``, and this pin keeps it surviving if that shape
    ever changes.
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
        # the fleet event the trap emits goes through the shim; a counting CLI
        # stub records each batch's event name (no plane needed — R1 writes no file)
        seen = root / "emitted"
        stub = root / "cli"
        stub.write_text("#!/bin/bash\nf=\"${@: -1}\"; cat \"$f\" >> \"" + str(seen) + "\"; echo >> \"" + str(seen) + "\"\n")
        stub.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "CLAUDLOBBY_ROOT": str(root),
               "BOT_DIR": str(root / "bot"), "BOT_ID": "b", "FLEET_NAME": "f",
               "PLANE_EMIT_CLI": str(stub), "PLANE_SOCKET": str(root / "no.sock")}
        r = subprocess.run(["/bin/bash", "-c",
                            f'. "{REPO_ROOT}/lib/lib-common.sh"; install_error_trap "";'
                            f' healthy() {{ return 1; }}; {body}; echo done'],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0 and "done" in r.stdout, (shape, r.stderr)
        rows = seen.read_text() if seen.exists() else ""
        assert rows.count('"event": "script_error"') + rows.count('"event":"script_error"') == want, (shape, rows)


def test_the_handoff_status_is_captured_without_firing_the_trap():
    """pre-stop-handoff's `if _x=$(session_command_status …)` was the #1460
    shape (one phantom script_error per bot per restart, measured on the
    flip's rolling restart); the status is captured with `|| true` inside the
    substitution and judged by the predicate's own value table."""
    src = (REPO_ROOT / "lib" / "pre-stop-handoff.sh").read_text()
    assert '_handoff_status="$(session_command_status "$_HANDOFF_CMD" "$BOT_DIR" || true)"' in src
    assert 'if _handoff_status="$(session_command_status' not in src
    assert "available|unverifiable)" in src
