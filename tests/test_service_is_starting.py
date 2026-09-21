"""Tests for the `service_is_starting` lib-common.sh helper (#1002, #1573).

The boot gate shared by fleet-pulse's alarm and keepalive's dead-session
watchdog: rc 0 iff a boot is provably in flight, so an absent tmux session is
expected rather than actionable.

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

#1573 PR B added a FIRST rung — the `data/.boot-queued` marker the admission
gate writes at acquire and removes at release — read on BOTH platforms, ahead
of the Linux SubState read. This suite is EXTENDED rather than replaced: a new
suite beside it would have left this one pinning the pre-PR-B contract and
passing green against the very branch that bypasses it.

The residual asymmetry between the platforms is REAL and is asserted, not
papered over. The SubState rung stays because it covers the one thing the
marker cannot: a start that failed before `start-bot.sh` ever ran, where no
launcher has written a marker at all. That is why the stale-marker cells expect
non-zero on Darwin and the stubbed SubState on Linux.
"""

import os
import subprocess
from pathlib import Path

LIB_COMMON = Path(__file__).resolve().parent.parent / "lib" / "lib-common.sh"
# lib-common.sh unconditionally sources supervisor.sh and boot-admission.sh
# from its own directory (#1573 task 6 and PR B) -- the patched copy below
# needs both siblings staged next to it, exactly as it already needs
# lib-common.sh itself.
SUPERVISOR = Path(__file__).resolve().parent.parent / "lib" / "supervisor.sh"
BOOT_ADMISSION = (
    Path(__file__).resolve().parent.parent / "lib" / "boot-admission.sh"
)


# A DEAD pid, deterministically: a child that has been reaped. The same shape
# tests/test_boot_admission.sh's dead_pid() uses, and the same residual — pid
# reuse inside the few milliseconds before the assertion — which no portable
# construct removes.
def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _install_stat_stub(bindir: Path) -> None:
    """A format-agnostic `stat`, needed ONLY because this file forces `_OS`.

    `stat_mtime` picks its spelling from `$_OS` — `stat -f %m` on Darwin,
    `stat -c %Y` elsewhere — which is always right in production and always
    wrong for a forced-OS run on the other platform. Without this, every cell
    that ages a file would pass on macOS and fail on Linux CI (or the reverse)
    for a reason that has nothing to do with the predicate.

    Both spellings are `stat <flag> <format> <file>`, so the file is $3 either
    way, and the stub answers with the REAL mtime — the age arithmetic under
    test stays the real arithmetic. Absolute paths for the underlying binary:
    this stub IS `stat` on the PATH these runs use, so a bare call would
    recurse.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "stat"
    stub.write_text(
        "#!/bin/sh\n"
        "for s in /usr/bin/stat /bin/stat; do\n"
        '  [ -x "$s" ] || continue\n'
        '  "$s" -f %m "$3" 2>/dev/null && exit 0\n'
        '  "$s" -c %Y "$3" 2>/dev/null && exit 0\n'
        "done\n"
        "exit 1\n"
    )
    stub.chmod(0o755)


def _make_bot(
    tmp_path: Path,
    *,
    service: str = "svc",
    boot_grace_conf: str | None = None,
    marker: str | None = None,
    marker_age_s: int = 0,
) -> Path:
    """A throwaway bot dir, optionally carrying a `data/.boot-queued` marker.

    `marker` is "live" (this process's pid — alive by construction), "dead" (a
    reaped child), or "malformed" (no parseable pid line), mirroring the three
    states the reaper distinguishes.

    `marker_age_s` rewrites the marker's mtime with `os.utime`. The bash suite
    refuses `touch -t` / `date -v` / `date -d` backdating because that trio is
    the portability trap this repo has got wrong in both directions; `os.utime`
    carries none of that — it is one syscall with the same meaning on both
    platforms — so the age boundary is crossed exactly rather than by sleeping.
    """
    bot_dir = tmp_path / "bots" / "probe"
    (bot_dir / "data").mkdir(parents=True, exist_ok=True)
    conf = [f"BOT_NAME=probe\nBOT_SERVICE={service}\n"]
    if boot_grace_conf is not None:
        conf.append(f"BOOT_GRACE_S={boot_grace_conf}\n")
    (bot_dir / "bot.conf").write_text("".join(conf))

    if marker is not None:
        # Branched, not a dict literal: a literal would evaluate BOTH arms and
        # so fork a child on every call, including the "live" ones.
        pid: int | None = None
        if marker == "live":
            pid = os.getpid()
        elif marker == "dead":
            pid = _dead_pid()
        body = "epoch=1700000000\n" if pid is None else f"pid={pid}\nepoch=1700000000\n"
        path = bot_dir / "data" / ".boot-queued"
        path.write_text(body)
        if marker_age_s:
            when = path.stat().st_mtime - marker_age_s
            os.utime(path, (when, when))
    return bot_dir


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
    bot_dir: Path | None = None,
    svc_arg: str = "svc",
) -> "subprocess.CompletedProcess[str]":
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

    `stat` is stubbed format-agnostically, and ONLY because this file forces
    `_OS`. `stat_mtime` picks its spelling from `$_OS` — `stat -f %m` on Darwin,
    `stat -c %Y` elsewhere — which is always right in production and always
    wrong for a forced-OS run on the other platform. Without the stub every
    marker cell would pass on macOS and fail on Linux CI for a reason that has
    nothing to do with the predicate. It answers with the real mtime, so the age
    arithmetic is still the real arithmetic.

    `bot_dir` is passed through as the door's optional SECOND argument. Omitted
    (the default) the call is one-argument, byte-identical to every pre-PR-B
    caller — which is what makes "no bot dir changes nothing" assertable.
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

    _install_stat_stub(bindir)

    uptime_file = tmp_path / "uptime"
    uptime_file.write_text(f"{uptime_s} 4096.39\n")
    patched = tmp_path / "lib-common.sh"
    patched.write_text(LIB_COMMON.read_text().replace("/proc/uptime", str(uptime_file)))
    (tmp_path / "supervisor.sh").write_bytes(SUPERVISOR.read_bytes())
    (tmp_path / "boot-admission.sh").write_bytes(BOOT_ADMISSION.read_bytes())

    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
    }
    # An ambient KEEPALIVE_BOOT_GRACE_S on the developer's host would silently
    # widen every grace cell; unset it unless the cell names one.
    env.pop("KEEPALIVE_BOOT_GRACE_S", None)
    if grace_env is not None:
        env["KEEPALIVE_BOOT_GRACE_S"] = grace_env

    argv = [
        "bash",
        "-c",
        # $1 is the library, $2 the forced OS; whatever is left is the door's
        # own argument list, so a cell can call it with one argument or two
        # through ONE snippet rather than two that could drift apart.
        '. "$1"; shift; _OS="$1"; shift; service_is_starting "$@"',
        "_",
        str(patched),
        force_os,
        svc_arg,
    ]
    if bot_dir is not None:
        argv.append(str(bot_dir))

    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert "value too great for base" not in proc.stderr, proc.stderr
    return proc


def _rc(*args, **kwargs) -> int:
    """`_run`'s return code alone — what almost every cell below asserts on.

    `_run` itself hands back the whole CompletedProcess because rc is not
    sufficient for one question: bash exits 1 both for a plain "not starting"
    and for a `${1:?}` abort, so the cell that pins R16 has to read stderr to
    tell those apart.
    """
    return _run(*args, **kwargs).returncode


class TestStatesThatMeanMidStart:
    def test_activating_is_mid_start(self, tmp_path):
        """ExecStartPre — the composed boot-stagger sleep."""
        assert _rc(tmp_path, active="activating", sub="start-pre") == 0

    def test_active_running_is_mid_start(self, tmp_path):
        """ExecStart executing: the spawner is alive, tmux is not up yet.

        The window ActiveState alone cannot see, and where all three of rajan's
        boot-storm restarts landed.
        """
        assert _rc(tmp_path, active="active", sub="running") == 0


class TestStatesThatDoNotMeanMidStart:
    def test_settled_unit_is_not_mid_start(self, tmp_path):
        """active/exited is the STEADY state — a missing session here is real.

        If this ever passes for active/running, the unit shape changed and the
        watchdog is silently dead; tests/test_composer.py pins that shape.
        """
        assert _rc(tmp_path, active="active", sub="exited") != 0

    def test_dead_unit_is_not_mid_start(self, tmp_path):
        assert _rc(tmp_path, active="inactive", sub="dead") != 0

    def test_failed_unit_is_not_mid_start(self, tmp_path):
        assert _rc(tmp_path, active="failed", sub="failed") != 0

    def test_deactivating_is_not_mid_start(self, tmp_path):
        """A unit on its way down is not a unit coming up."""
        assert _rc(tmp_path, active="deactivating", sub="stop") != 0

    def test_unknown_unit_is_not_mid_start(self, tmp_path):
        assert _rc(tmp_path, active="", sub="") != 0


class TestGraceCap:
    """The bound that stops a wedged spawner suppressing the watchdog forever."""

    def test_young_start_is_mid_start(self, tmp_path):
        # spawner started at 900s, now 1000s → 100s old, under the 300s default.
        assert (
            _rc(
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
            _rc(
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
            _rc(
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
            _rc(
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
            _rc(
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
            _rc(
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
    def test_non_linux_has_no_substate_rung(self, tmp_path):
        """`launchctl print` exposes no cheap sub-state, so rung 2 is Linux-only
        and macOS keeps prior behaviour rather than inheriting a suppression
        this rung cannot justify.

        Renamed from `test_non_linux_is_never_mid_start`, and the rename is the
        point: since #1573 PR B a Darwin bot CAN be mid-start — through rung 1,
        asserted below. What stays true is the narrower claim this cell makes,
        that no SUBSTATE answer reaches a non-Linux caller. Leaving the old name
        in place would have read as a standing promise the branch had already
        broken.
        """
        assert (
            _rc(tmp_path, active="activating", sub="start-pre", force_os="Darwin") != 0
        )

    def test_unreadable_age_trusts_the_state(self, tmp_path):
        """A missing timestamp must not turn a real boot into a restart."""
        assert _rc(tmp_path, active="active", sub="running", exec_main_us="") == 0

    def test_sub_second_uptime_does_not_abort(self, tmp_path):
        """Regression: `$((008))` is invalid octal and aborts under `set -e`.

        Two-digit uptime fractions occur in exactly the window this predicate
        exists for — the first seconds after a host boot.
        """
        assert (
            _rc(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="0",
                uptime_s="0.08",
            )
            == 0
        )

    def test_an_empty_service_name_does_not_abort_the_caller(self, tmp_path):
        """`${1:?}` would kill a non-interactive caller on an empty name (R16).

        Nine harness bot.confs and every pre-generate fleet carry
        `BOT_SERVICE=""`, and the short-circuits that used to hide that from
        this door are gone. An empty name simply cannot satisfy rung 2 — which
        is an ANSWER, not an error.

        Asserted on STDERR, not on rc: bash exits 1 both for a plain "not
        starting" and for a `${1:?}` abort, so rc alone cannot tell the
        regression from the correct answer. `_OS=Darwin` keeps the stubbed
        systemctl — which ignores its arguments and would answer for any name —
        out of the way, so the cell measures the argument handling and nothing
        else.
        """
        proc = _run(
            tmp_path,
            active="active",
            sub="running",
            svc_arg="",
            force_os="Darwin",
        )
        assert "Usage" not in proc.stderr and "null or not set" not in proc.stderr, (
            f"an empty service name aborted the caller: {proc.stderr!r}"
        )
        assert proc.returncode == 1, f"expected a plain 'not starting', got {proc}"


class TestTheMarkerRung:
    """Rung 1: `data/.boot-queued`, read on BOTH platforms, before rung 2.

    The unit-state stub is pinned to `inactive/dead` in the positive cells —
    the one state rung 2 is certain to refuse — so a passing cell can only have
    come from the marker. A stub left at `active/running` would have made every
    Linux cell here pass for the wrong reason.
    """

    def test_a_fresh_live_marker_is_mid_start_on_linux(self, tmp_path):
        bot = _make_bot(tmp_path, marker="live")
        assert _rc(tmp_path, active="inactive", sub="dead", bot_dir=bot) == 0

    def test_a_fresh_live_marker_is_mid_start_on_darwin(self, tmp_path):
        """The whole reason the rung is platform-neutral.

        Before PR B a launchd host had NO boot-progress signal at all: rung 2
        returns 1 unconditionally off Linux, so every macOS bot was invisible to
        both consumers for its entire bring-up — and with the admission gate
        that window now legitimately includes the queue wait.
        """
        bot = _make_bot(tmp_path, marker="live")
        assert (
            _rc(
                tmp_path, active="inactive", sub="dead", bot_dir=bot, force_os="Darwin"
            )
            == 0
        )

    def test_a_marker_naming_a_DEAD_launcher_suppresses_nothing(self, tmp_path):
        """Liveness is what makes a window this wide honest.

        The #933 manufactured-all-clear bound is carried by the pid, not by
        mtime: a SIGKILLed launcher stops suppressing within one reaper poll,
        which no amount of age could achieve. Checked in both directions —
        Darwin has nothing to fall through to, Linux falls through to its own
        rung and answers from that.
        """
        bot = _make_bot(tmp_path, marker="dead")
        assert (
            _rc(
                tmp_path, active="inactive", sub="dead", bot_dir=bot, force_os="Darwin"
            )
            != 0
        )
        assert (
            _rc(tmp_path, active="active", sub="running", bot_dir=bot) == 0
        ), "on Linux a dead marker must FALL THROUGH to rung 2, not short-circuit to 1"

    def test_a_malformed_marker_suppresses_nothing(self, tmp_path):
        """A marker with no parseable pid is not proof of anything.

        It is also the shape a half-written file has, so this is the truncation
        case as well as the corruption one.
        """
        bot = _make_bot(tmp_path, marker="malformed")
        assert (
            _rc(
                tmp_path, active="inactive", sub="dead", bot_dir=bot, force_os="Darwin"
            )
            != 0
        )

    def test_a_STALE_marker_falls_through_in_both_directions(self, tmp_path):
        """Past the grace the marker stops being read as proof.

        Both directions, because the two platforms must disagree here and that
        disagreement is a real residual rather than an oversight: Darwin has no
        second rung, Linux has one and is entitled to answer from it.
        """
        bot = _make_bot(tmp_path, marker="live", boot_grace_conf="60", marker_age_s=600)
        assert (
            _rc(
                tmp_path, active="inactive", sub="dead", bot_dir=bot, force_os="Darwin"
            )
            != 0
        )
        assert _rc(tmp_path, active="active", sub="running", bot_dir=bot) == 0

    def test_a_marker_inside_a_WIDER_composed_grace_still_counts(self, tmp_path):
        """The F15 pair to the cell above, and the reason the grace moved.

        600s is past the old 300s default and inside the composed
        BOOT_GRACE_S — which is exactly the state PR B created, since the
        admission wait now sits inside the phase the grace bounds. Without the
        composed value this bot would read as settled while it was still
        queued.
        """
        bot = _make_bot(
            tmp_path, marker="live", boot_grace_conf="1200", marker_age_s=600
        )
        assert (
            _rc(
                tmp_path, active="inactive", sub="dead", bot_dir=bot, force_os="Darwin"
            )
            == 0
        )

    def test_no_marker_is_byte_identical_to_today(self, tmp_path):
        """A bot dir with nothing in it changes no answer, on either platform."""
        bot = _make_bot(tmp_path)
        assert (
            _rc(tmp_path, active="activating", sub="start-pre", bot_dir=bot) == 0
        ), "Linux must still answer from its own rung"
        assert (
            _rc(
                tmp_path,
                active="activating",
                sub="start-pre",
                bot_dir=bot,
                force_os="Darwin",
            )
            != 0
        )

    def test_a_marker_fresh_bot_with_an_EMPTY_service_is_mid_start(self, tmp_path):
        """The deleted short-circuit, asserted.

        Every consumer used to gate this call on `[ -n "$BOT_SERVICE" ]`, which
        made the marker rung unreachable for precisely the bots that have no
        service name. The marker needs no unit name at all.
        """
        bot = _make_bot(tmp_path, service="", marker="live")
        assert (
            _rc(
                tmp_path,
                active="inactive",
                sub="dead",
                bot_dir=bot,
                svc_arg="",
                force_os="Darwin",
            )
            == 0
        )

    def test_an_unreadable_bot_dir_is_not_proof_of_a_boot(self, tmp_path):
        """A path that does not exist means "nothing to read", never "starting"."""
        assert (
            _rc(
                tmp_path,
                active="inactive",
                sub="dead",
                bot_dir=tmp_path / "no" / "such" / "bot",
                force_os="Darwin",
            )
            != 0
        )


class TestTheWriterAndTheReaderAgree:
    """The marker format is written in one file and parsed in another.

    A drift between `boot_admission_acquire`'s `printf` and this door's `sed`
    would be SILENT — the predicate would simply never fire, every queued bot
    would read as down, and the storm PR B exists to close would come back with
    every unit test still green. So the cell below does not hand-write a
    marker: it runs the REAL gate and then asks the REAL predicate.

    It is also the granted-but-not-ready case, which the marker's one lifetime
    (acquire -> release) makes indistinguishable from the queued case on
    purpose: this bot has been GRANTED a slot and has no session yet, and both
    consumers must still leave it alone.
    """

    def _acquire_then_ask(self, tmp_path: Path, force_os: str) -> tuple[int, str]:
        bot_dir = _make_bot(tmp_path, service="probe-svc")
        patched = tmp_path / "lib-common.sh"
        patched.write_text(LIB_COMMON.read_text())
        (tmp_path / "supervisor.sh").write_bytes(SUPERVISOR.read_bytes())
        (tmp_path / "boot-admission.sh").write_bytes(BOOT_ADMISSION.read_bytes())
        # The gate itself ages files (its reaper, its claim grace), so the
        # forced-OS `stat` artifact reaches it too, not just the predicate.
        bindir = tmp_path / "bin"
        _install_stat_stub(bindir)
        env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "CLAUDLOBBY_ROOT": str(tmp_path / "root"),
            "CLAUDLOBBY_BOOT_EPOCH": "1700000000",
            "PLANE_EMIT_DISABLED": "1",
            "BOOT_ADMISSION_POLL_S": "0.2",
        }
        env.pop("KEEPALIVE_BOOT_GRACE_S", None)
        proc = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1"; _OS="$4"; LOG="$2/logs/startup.log"; setup_log_dir "$LOG";'
                ' v="$(boot_admission_acquire "$2")";'
                ' if service_is_starting "$3" "$2"; then r=0; else r=$?; fi;'
                ' printf "%s %s\\n" "$v" "$r"',
                "_",
                str(patched),
                str(bot_dir),
                "probe-svc",
                force_os,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        verdict, rc = proc.stdout.split()
        return int(rc), verdict

    def test_a_granted_bot_reads_as_mid_start_on_darwin(self, tmp_path):
        rc, verdict = self._acquire_then_ask(tmp_path, "Darwin")
        assert verdict.startswith("granted:"), f"gate did not grant: {verdict}"
        assert rc == 0, "the gate's own marker did not satisfy the door that reads it"

    def test_a_granted_bot_reads_as_mid_start_on_linux(self, tmp_path):
        rc, verdict = self._acquire_then_ask(tmp_path, "Linux")
        assert verdict.startswith("granted:"), f"gate did not grant: {verdict}"
        assert rc == 0


class TestTheComposedGraceIsTheWindow:
    """F15: `BOOT_GRACE_S` in the bot's own bot.conf is the window BOTH rungs
    age against, with the pre-#1573 env knob behind it for an un-regenerated
    bot.conf — F4's shape.

    Driven through rung 2 here, where the arithmetic is already instrumented by
    the uptime fixture; the marker rung's half of the same resolution is
    asserted by the stale/wider pair above. One resolution, two rungs: two
    private copies is how the marker window and the SubState window come to
    disagree about one boot.
    """

    def test_the_composed_value_widens_the_window(self, tmp_path):
        # spawner 900s old — past the 300s default, inside a composed 1200.
        bot = _make_bot(tmp_path, boot_grace_conf="1200")
        assert (
            _rc(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                bot_dir=bot,
            )
            == 0
        )

    def test_the_composed_value_wins_over_the_env_knob(self, tmp_path):
        """Goal 1: boot policy is composed once and read from there.

        An operator's leftover `KEEPALIVE_BOOT_GRACE_S` must not quietly
        override a value the compositor derived.
        """
        bot = _make_bot(tmp_path, boot_grace_conf="1200")
        assert (
            _rc(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                grace_env="10",
                bot_dir=bot,
            )
            == 0
        )

    def test_an_un_regenerated_bot_conf_falls_back_to_the_env_knob(self, tmp_path):
        """No operator step: a fleet that has not regenerated keeps today's
        behaviour rather than losing its window."""
        bot = _make_bot(tmp_path)
        assert (
            _rc(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                grace_env="1200",
                bot_dir=bot,
            )
            == 0
        )

    def test_a_garbage_composed_value_falls_back_to_the_default(self, tmp_path):
        """A typo in the composed value must not silently mean 'no cap'."""
        bot = _make_bot(tmp_path, boot_grace_conf="not-a-number")
        assert (
            _rc(
                tmp_path,
                active="active",
                sub="running",
                exec_main_us="100000000",
                uptime_s="1000.00",
                bot_dir=bot,
            )
            != 0
        )
