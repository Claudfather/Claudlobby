"""Tests for the `service_is_starting` lib-common.sh helper (#1002).

`service_is_starting <bot_service>` is the boot gate shared by fleet-pulse's
alarm and keepalive's dead-session watchdog: rc 0 iff the unit is provably
mid-start, so an absent tmux session is expected rather than actionable.

Sibling of `test_service_is_active.py` and driven the same way — `systemctl`
stubbed on PATH, `_OS` forced after sourcing — because the real state machine
needs a live systemd user bus, which macOS (the documented baseline host) does
not have. `lib/validate-bot-change.sh` covers the state machine against a real
unit and SKIPs where the bus is absent; this file covers the parsing, the state
matching and the grace arithmetic everywhere, always.

The pairing matters: the two halves of a suppression predicate fail in opposite
directions. A predicate that never returns 0 silently restores the boot-storm
restart loop; one that always returns 0 silently disables the watchdog while
every surface reads healthy. Both are asserted below.
"""

import os
import subprocess
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"


def _run(
    tmp_path: Path,
    *,
    active: str,
    sub: str,
    inactive_exit_us: str = "990000000",
    exec_main_us: str = "990000000",
    uptime_s: str = "1000.00",
    force_os: str = "Linux",
    grace_env: str | None = None,
) -> int:
    """Run `service_is_starting` against a stubbed unit state; return its rc.

    The systemctl stub emits `Key=Value` lines in a DELIBERATELY SHUFFLED order —
    ExecMainStart first, ActiveState second — because that is what real systemd
    does for this property set, and it is not the order the helper asks for.
    A stub that echoed the request order would encode the caller's assumption
    rather than test it, and would pass a helper that reads a timestamp as the
    ActiveState.

    Clock: the helper reads /proc/uptime by literal path, so the run uses a copy
    of lib-common.sh with that path repointed at a fixture — which keeps the real
    arithmetic under test instead of mocking it away. Default stamps are 10s
    before the default uptime, i.e. a fresh start.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "ExecMainStartTimestampMonotonic={exec_main_us}" '
        f'"ActiveState={active}" "SubState={sub}" '
        f'"InactiveExitTimestampMonotonic={inactive_exit_us}"\n'
    )
    stub.chmod(0o755)

    uptime_file = tmp_path / "uptime"
    uptime_file.write_text(f"{uptime_s} 4096.39\n")
    patched = tmp_path / "lib-common.sh"
    patched.write_text(LIB_COMMON.read_text().replace("/proc/uptime", str(uptime_file)))

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
    }
    if grace_env is not None:
        env["KEEPALIVE_BOOT_GRACE_S"] = grace_env

    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; _OS="$3"; service_is_starting "$2"',
            "_",
            str(patched),
            "svc",
            force_os,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert "value too great for base" not in proc.stderr, proc.stderr
    return proc.returncode


class TestStatesThatMeanMidStart:
    def test_activating_is_mid_start(self, tmp_path):
        """ExecStartPre — the composed boot-stagger sleep."""
        assert _run(tmp_path, active="activating", sub="start-pre") == 0

    def test_active_running_is_mid_start(self, tmp_path):
        """ExecStart executing: the spawner is alive, tmux is not up yet.

        The window ActiveState alone cannot see, and where all three of rajan's
        boot-storm restarts landed.
        """
        assert _run(tmp_path, active="active", sub="running") == 0


class TestStatesThatDoNotMeanMidStart:
    def test_settled_unit_is_not_mid_start(self, tmp_path):
        """active/exited is the STEADY state — a missing session here is real.

        If this ever passes for active/running, the unit shape changed and the
        watchdog is silently dead; tests/test_composer.py pins that shape.
        """
        assert _run(tmp_path, active="active", sub="exited") != 0

    def test_dead_unit_is_not_mid_start(self, tmp_path):
        assert _run(tmp_path, active="inactive", sub="dead") != 0

    def test_failed_unit_is_not_mid_start(self, tmp_path):
        assert _run(tmp_path, active="failed", sub="failed") != 0

    def test_deactivating_is_not_mid_start(self, tmp_path):
        """A unit on its way down is not a unit coming up."""
        assert _run(tmp_path, active="deactivating", sub="stop") != 0

    def test_unknown_unit_is_not_mid_start(self, tmp_path):
        assert _run(tmp_path, active="", sub="") != 0


class TestGraceCap:
    """The bound that stops a wedged spawner suppressing the watchdog forever."""

    def test_young_start_is_mid_start(self, tmp_path):
        # spawner started at 900s, now 1000s → 100s old, under the 300s default.
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="900000000",
                uptime_s="1000.00",
            )
            == 0
        )

    def test_start_older_than_grace_is_not_mid_start(self, tmp_path):
        # spawner started at 100s, now 1000s → 900s old, past the 300s default.
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
            )
            != 0
        )

    def test_grace_is_overridable(self, tmp_path):
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                grace_env="1200",
            )
            == 0
        )

    def test_garbage_grace_falls_back_to_the_default(self, tmp_path):
        """A typo in the knob must not silently mean 'no cap'."""
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                grace_env="not-a-number",
            )
            != 0
        )

    def test_activating_ages_from_the_start_attempt_not_the_spawner(self, tmp_path):
        """During ExecStartPre the spawner has not run, so ExecMainStart is 0.

        Ageing an activating unit from ExecMainStart would make every boot look
        1000s old and defeat the gate on its first state.
        """
        assert (
            _run(
                tmp_path,
                active="activating",
                sub="start-pre",
                inactive_exit_us="990000000",
                exec_main_us="0",
                uptime_s="1000.00",
            )
            == 0
        )

    def test_running_ages_from_the_spawner_not_the_start_attempt(self, tmp_path):
        """The stagger must not be billed to the ExecStart budget.

        A unit 290s into a 300s grace by InactiveExit, but only 10s into its
        spawner, is early in the phase the grace is about. Billing the composed
        ExecStartPre (host-global, and it grows with every fleet added) to this
        budget would shrink it silently as the estate grows — hurting the tail
        bot, the one the ladder pushed latest.
        """
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                inactive_exit_us="710000000",
                exec_main_us="990000000",
                uptime_s="1000.00",
            )
            == 0
        )


class TestFailureModes:
    def test_non_linux_is_never_mid_start(self, tmp_path):
        """launchctl exposes no cheap sub-state, so macOS keeps prior behaviour
        rather than inheriting a suppression this helper cannot justify."""
        assert (
            _run(tmp_path, active="activating", sub="start-pre", force_os="Darwin") != 0
        )

    def test_unreadable_age_trusts_the_state(self, tmp_path):
        """A missing timestamp must not turn a real boot into a restart."""
        assert _run(tmp_path, active="active", sub="running", exec_main_us="") == 0

    def test_sub_second_uptime_does_not_abort(self, tmp_path):
        """Regression: `$((008))` is invalid octal and aborts under `set -e`.

        Two-digit uptime fractions occur in exactly the window this predicate
        exists for — the first seconds after a host boot.
        """
        assert (
            _run(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="0",
                uptime_s="0.08",
            )
            == 0
        )
