"""Tests for `service_is_crash_looping` and `crash_loop_carry` (#1769).

`service_is_starting` ages the CURRENT start phase, and systemd stamps a fresh
phase on every attempt of a Restart= loop -- measured on systemd 252, the retry
gap itself reads `activating/auto-restart` with a new InactiveExit stamp. So a
unit that fails every start reads "boot in flight" forever: on 2026-09-23 all
21 bots failed 30,316 starts in 23 h and the watchdogs said "booting" 28,977
times (#1769).

`service_is_crash_looping` is the fact both consumers share instead of that
flip: rc 0 iff the unit is in a start state (the same states
`service_is_starting` accepts) AND systemd has AUTOMATICALLY restarted it at
least twice in the current streak. It counts from NRestarts, which is
streak-scoped by construction on these units: a manual start or a reboot zeroes
it, and a successful boot is terminal (RemainAfterExit=yes), so any NRestarts
seen while the unit is starting belongs to the current failing streak. The one
thing that breaks that -- keepalive's own `systemctl --user restart` zeroing the
counter mid-streak (measured: NRestarts 3 -> 0) -- is what `crash_loop_carry`
exists for: keepalive records the count it is about to wipe.

Driven like `test_service_is_starting.py`: `systemctl` stubbed on PATH with the
properties in a DELIBERATELY SHUFFLED order (real systemd does not answer in
request order), `_OS` forced after sourcing. The real state machine is covered
by `lib/validate-bot-change.sh` against a real failing unit.
"""

import os
import subprocess
from pathlib import Path

import pytest

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"
SUPERVISOR = Path(__file__).resolve().parent.parent / "lib" / "supervisor.sh"


def _scene(tmp_path: Path, *, active: str, sub: str, nrestarts: str, carry: str | None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    # Shuffled: NRestarts last, SubState before ActiveState. An unparseable or
    # absent counter is modelled by passing "" (the Key= line with no value).
    # And it answers ONLY what -p asked for, as systemd does (whichever spelling
    # getopt takes: -p X, -pX, --property=X, --property X), recording each
    # call's request in `requested`: a stub that printed every property whatever
    # -p said could not see a call that stopped asking for NRestarts, which on
    # real systemd reads every loop as "no verdict" forever (#1774 review).
    requested = tmp_path / "requested"
    props = (
        "ExecMainStartTimestampMonotonic=990000000",
        f"SubState={sub}",
        f"ActiveState={active}",
        "InactiveExitTimestampMonotonic=990000000",
        f"NRestarts={nrestarts}",
    )
    stub.write_text(
        "#!/bin/sh\n"
        'want=""; prev=""\n'
        'for a in "$@"; do\n'
        '  case "$prev" in -p|--property) want="$want $a" ;; esac\n'
        '  case "$a" in\n'
        '    --property=*) want="$want ${a#--property=}" ;;\n'
        '    -p?*) want="$want ${a#-p}" ;;\n'
        "  esac\n"
        '  prev="$a"\n'
        "done\n"
        'want=$(printf "%s" "$want" | tr "," " ")\n'
        f'printf "%s\\n" "$want" >> "{requested}"\n'
        "for kv in " + " ".join(f'"{kv}"' for kv in props) + "; do\n"
        # no -p at all: systemd prints every property
        '  case " $want " in "  "|*" ${kv%%=*} "*) printf "%s\\n" "$kv" ;; esac\n'
        "done\n"
    )
    stub.chmod(0o755)
    bot_dir = tmp_path / "bot"
    (bot_dir / "data").mkdir(parents=True, exist_ok=True)
    carry_file = bot_dir / "data" / ".restart-carry"
    if carry is not None:
        carry_file.write_text(carry + "\n")
    elif carry_file.exists():
        carry_file.unlink()
    return bindir, bot_dir, carry_file


def _bash(
    tmp_path: Path, bindir: Path, script: str, *args: str, force_os: str = "Linux"
):
    (tmp_path / "supervisor.sh").write_bytes(SUPERVISOR.read_bytes())
    lib = tmp_path / "lib-common.sh"
    lib.write_bytes(LIB_COMMON.read_bytes())
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; _OS="$2"; shift 2; ' + script,
            "_",
            str(lib),
            force_os,
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert "value too great for base" not in proc.stderr, proc.stderr
    return proc


def _looping(tmp_path, *, active, sub, nrestarts="0", carry=None, force_os="Linux"):
    """-> (rc, CRASH_LOOP_RESTARTS, CRASH_LOOP_VERDICT, carry file text or None)."""
    bindir, bot_dir, carry_file = _scene(
        tmp_path, active=active, sub=sub, nrestarts=nrestarts, carry=carry
    )
    proc = _bash(
        tmp_path,
        bindir,
        # `|| rc=$?`, never `; rc=$?`: sourcing lib-common.sh arms `set -e`, so
        # a bare rc-1 answer ("not a crash loop" -- the answer most cases want)
        # would kill the probe before it printed and read as a harness failure.
        'rc=0; service_is_crash_looping svc "$1" || rc=$?; '
        'printf "%s %s %s" "$rc" "${CRASH_LOOP_RESTARTS:-unset}" "${CRASH_LOOP_VERDICT:-unset}"',
        str(bot_dir),
        force_os=force_os,
    )
    rc, restarts, verdict = proc.stdout.split()
    return (
        int(rc),
        restarts,
        verdict,
        (carry_file.read_text().strip() if carry_file.exists() else None),
    )


def _carry(tmp_path, *, active, sub, nrestarts, carry=None, force_os="Linux"):
    """Run crash_loop_carry; -> carry file text afterwards, or None when absent."""
    bindir, bot_dir, carry_file = _scene(
        tmp_path, active=active, sub=sub, nrestarts=nrestarts, carry=carry
    )
    proc = _bash(
        tmp_path, bindir, 'crash_loop_carry svc "$1"', str(bot_dir), force_os=force_os
    )
    assert proc.returncode == 0, proc.stderr  # never fails the restart it precedes
    return carry_file.read_text().strip() if carry_file.exists() else None


class TestTheThresholdIsTwoAutomaticRestarts:
    def test_first_attempt_is_a_boot_not_a_crash_loop(self, tmp_path):
        assert (
            _looping(tmp_path, active="activating", sub="start-pre", nrestarts="0")[0]
            == 1
        )

    def test_one_retry_can_still_be_a_transient(self, tmp_path):
        """A single automatic restart is what a boot-lock timeout or a slow
        plugin fetch looks like; it is not yet a loop."""
        assert (
            _looping(tmp_path, active="activating", sub="auto-restart", nrestarts="1")[
                0
            ]
            == 1
        )

    def test_second_automatic_restart_is_a_crash_loop(self, tmp_path):
        rc, restarts, verdict, _ = _looping(
            tmp_path, active="activating", sub="start-pre", nrestarts="2"
        )
        assert (rc, restarts, verdict) == (0, "2", "looping")

    @pytest.mark.parametrize(
        "active,sub",
        [
            ("activating", "start-pre"),  # the stagger of the next attempt
            (
                "activating",
                "auto-restart",
            ),  # the RestartSec gap -- measured: it is 'activating'
            ("active", "running"),  # start-bot.sh executing before it fails
        ],
    )
    def test_every_start_state_of_a_loop_reads_looping(self, tmp_path, active, sub):
        """The same three states service_is_starting calls mid-start: the new
        fact must bind in exactly the states the old gate was suppressing in."""
        assert _looping(tmp_path, active=active, sub=sub, nrestarts="1503")[0] == 0


class TestAStreakThatEndedIsNotALoop:
    def test_settled_unit_with_a_stale_counter_is_not_looping(self, tmp_path):
        """NRestarts SURVIVES a successful boot: a unit that needed retries and
        then came up keeps its count while active/exited. Settled is settled."""
        rc, _, verdict, carry = _looping(
            tmp_path, active="active", sub="exited", nrestarts="5", carry="3"
        )
        assert (rc, verdict) == (1, "over")
        assert carry is None, "a completed boot ends the streak: the carry must go"

    @pytest.mark.parametrize("active,sub", [("inactive", "dead"), ("failed", "failed")])
    def test_stopped_or_given_up_ends_the_streak(self, tmp_path, active, sub):
        """Stopped on purpose, or systemd stopped retrying (start-limit-hit):
        either way nothing is looping now -- a failed unit is service_down's."""
        rc, _, verdict, carry = _looping(
            tmp_path, active=active, sub=sub, nrestarts="7", carry="2"
        )
        assert (rc, verdict, carry) == (1, "over", None)


class TestAmbiguityIsNoVerdict:
    def test_deactivating_is_no_verdict_and_keeps_the_carry(self, tmp_path):
        """deactivating/* is BOTH the stop-post between two failed attempts and
        the deliberate stop of a settled unit whose counter is stale -- the
        state cannot tell them apart, so it claims nothing and erases nothing."""
        rc, _, verdict, carry = _looping(
            tmp_path, active="deactivating", sub="stop-post", nrestarts="9", carry="2"
        )
        assert (rc, verdict, carry) == (1, "none", "2")

    def test_unreadable_counter_is_no_verdict(self, tmp_path):
        rc, _, verdict, _ = _looping(
            tmp_path, active="activating", sub="start-pre", nrestarts=""
        )
        assert (rc, verdict) == (1, "none")

    def test_non_linux_never_claims_a_loop(self, tmp_path):
        """launchd has its own throttle and no cheap counter; sound in one
        direction only, like service_is_starting."""
        rc, _, verdict, carry = _looping(
            tmp_path,
            active="activating",
            sub="start-pre",
            nrestarts="50",
            carry="4",
            force_os="Darwin",
        )
        assert (rc, verdict, carry) == (1, "unknown", "4")


class TestTheStreakSurvivesKeepalivesOwnRestart:
    def test_carried_count_plus_live_count_binds(self, tmp_path):
        """keepalive restarted mid-streak (counter zeroed), then one more
        automatic restart: 1 carried + 1 live = the second restart."""
        rc, restarts, _, _ = _looping(
            tmp_path, active="activating", sub="start-pre", nrestarts="1", carry="1"
        )
        assert (rc, restarts) == (0, "2")

    def test_carried_count_alone_binds_right_after_the_restart(self, tmp_path):
        assert (
            _looping(
                tmp_path, active="activating", sub="start-pre", nrestarts="0", carry="2"
            )[0]
            == 0
        )

    def test_garbage_carry_reads_as_zero(self, tmp_path):
        rc, restarts, _, _ = _looping(
            tmp_path, active="activating", sub="start-pre", nrestarts="1", carry="x1"
        )
        assert (rc, restarts) == (1, "1")


class TestCrashLoopCarry:
    @pytest.mark.parametrize(
        "active,sub",
        [
            ("deactivating", "stop-post"),  # between two failed attempts
            ("activating", "start-pre"),  # a phase aged past the boot grace
            ("active", "running"),
        ],
    )
    def test_a_restart_that_interrupts_a_streak_carries_the_count(
        self, tmp_path, active, sub
    ):
        assert _carry(tmp_path, active=active, sub=sub, nrestarts="3") == "3"

    def test_restarting_a_given_up_unit_starts_clean(self, tmp_path):
        """failed/* means systemd stopped retrying; the fact already calls that
        streak over, so a restart out of it must not re-enter as an instant
        crash_loop before its first attempt has even run."""
        assert (
            _carry(tmp_path, active="failed", sub="failed", nrestarts="3", carry="2")
            is None
        )

    def test_carries_accumulate_across_repeated_restarts(self, tmp_path):
        assert (
            _carry(
                tmp_path,
                active="deactivating",
                sub="stop-post",
                nrestarts="3",
                carry="2",
            )
            == "5"
        )

    def test_restarting_a_settled_unit_starts_clean(self, tmp_path):
        """The ordinary keepalive restart -- a dead session on a settled unit --
        is not part of any streak: its stale counter must not seed the next one."""
        assert (
            _carry(tmp_path, active="active", sub="exited", nrestarts="4", carry="2")
            is None
        )

    def test_restarting_a_stopped_unit_starts_clean(self, tmp_path):
        assert (
            _carry(tmp_path, active="inactive", sub="dead", nrestarts="4", carry="2")
            is None
        )

    def test_nothing_to_carry_leaves_the_file_alone(self, tmp_path):
        assert (
            _carry(
                tmp_path, active="activating", sub="start-pre", nrestarts="0", carry="1"
            )
            == "1"
        )
        assert (
            _carry(tmp_path, active="activating", sub="start-pre", nrestarts="0")
            is None
        )

    def test_non_linux_is_a_no_op(self, tmp_path):
        assert (
            _carry(
                tmp_path,
                active="deactivating",
                sub="stop-post",
                nrestarts="3",
                force_os="Darwin",
            )
            is None
        )


def test_the_one_show_asks_for_the_counter_by_name(tmp_path):
    """The stub answers only what -p asked, so this is a real request, not a
    property it would have printed anyway. Without it every loop reads as no
    verdict on real systemd, and the loop tests above go red with it."""
    _looping(tmp_path, active="activating", sub="start-pre", nrestarts="2")
    requested = (tmp_path / "requested").read_text().split()
    assert "NRestarts" in requested, requested


@pytest.mark.parametrize(
    "argv, keys",
    [
        pytest.param(["-p", "NRestarts"], {"NRestarts"}, id="p-separate"),
        pytest.param(["-pNRestarts"], {"NRestarts"}, id="p-attached"),
        pytest.param(["-p", "SubState", "-pNRestarts"], {"SubState", "NRestarts"}, id="p-mixed"),
        pytest.param(["-pNRestarts,SubState"], {"NRestarts", "SubState"}, id="p-attached-comma"),
        pytest.param(["--property=NRestarts"], {"NRestarts"}, id="property-equals"),
        pytest.param(["--property", "NRestarts"], {"NRestarts"}, id="property-separate"),
        pytest.param(["-p", "Bogus"], set(), id="p-unknown"),
        pytest.param(
            [],
            {
                "NRestarts", "SubState", "ActiveState",
                "ExecMainStartTimestampMonotonic", "InactiveExitTimestampMonotonic",
            },
            id="no-p",
        ),
    ],
)
def test_the_stub_answers_only_what_each_spelling_of_p_asks(tmp_path, argv, keys):
    """What real systemd 252 answers for each way of writing -p (measured against a running
    unit): only what was asked. A stub that read `-pNRestarts` as no -p at all answered every
    property, and one that dropped it when another -p was separate could not witness a call
    that still asks for NRestarts, so a refactor to that spelling, which systemd reads the
    same as the separate one, failed the loop tests."""
    bindir, _, _ = _scene(
        tmp_path, active="activating", sub="start-pre", nrestarts="2", carry=None
    )
    out = subprocess.run(
        [str(bindir / "systemctl"), "show", *argv, "svc"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    assert {ln.split("=", 1)[0] for ln in out.splitlines()} == keys, out


def test_the_starting_gate_is_unchanged_by_the_shared_read(tmp_path):
    """service_is_starting now reads through the same one-show helper; a unit
    looping at NRestarts 1503 is still 'starting' to it (the per-phase age is
    kept for what it was built for). Keepalive and fleet-pulse, not this gate,
    decide what a crash loop means."""
    bindir, _, _ = _scene(
        tmp_path, active="activating", sub="auto-restart", nrestarts="1503", carry=None
    )
    uptime = tmp_path / "uptime"
    uptime.write_text("1000.00 4096.39\n")
    (tmp_path / "supervisor.sh").write_bytes(SUPERVISOR.read_bytes())
    lib = tmp_path / "lib-common.sh"
    lib.write_text(LIB_COMMON.read_text().replace("/proc/uptime", str(uptime)))
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; _OS=Linux; service_is_starting svc', "_", str(lib)],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr


def test_fleet_pulse_critical_lists_are_registered_critical():
    """fleet-pulse pages and summarises from the plane's CRITICAL rows --
    plane-readers' ESCALATION_SQL filters `severity = 'critical'`, stamped at
    ingest from the registry. A type listed in _CRITICAL_ESCALATION_TYPES or
    _CRITICAL_SUMMARY_TYPES that the registry does not call critical can never
    page and never shows, silently. crash_loop is the case this pins: listed in
    the bash but forgotten in the registry, it would have looked shipped."""
    import re

    from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY

    src = (
        Path(__file__).resolve().parent.parent / "lib" / "fleet-pulse.sh"
    ).read_text()
    for var in ("_CRITICAL_ESCALATION_TYPES", "_CRITICAL_SUMMARY_TYPES"):
        m = re.search(rf'^{var}="([^"]*)"', src, re.M)
        assert m, f"{var} not found in fleet-pulse.sh"
        types = m.group(1).split()
        assert "crash_loop" in types, (var, types)
        for t in types:
            assert SYSTEM_EVENT_SEVERITY.get(t) == "critical", (var, t)
