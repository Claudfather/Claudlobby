"""#1924: on launchd the nightly reload-fleet booted ITSELF out and died mid-run.

`reload-fleet.sh` runs `setup-fleet --jobs-only`, which re-enrolls every
composed fleet job in alphabetical order, reload-fleet's own included, and the
launchd enroller booted each job out before bootstrapping it again. Booting out
the job that is running you stops you: launchd takes the job's process group
down, so the bootstrap after the bootout never ran, the job stayed unloaded,
every job sorting after it was never re-enrolled, and nothing alerted, because
the run never got back to `loud_fail`.

These tests drive the REAL chain -- reload-fleet.sh, setup-fleet,
install_fleet_timer_launchd.sh, lib/supervisor.sh -- against a throwaway root,
with a fake `launchctl` that behaves like launchd where it matters: `print`
reports the running job's pid, and `bootout` of that job's label kills the
job's whole process group. Each run is started as the leader of its own
process group, the way launchd starts a job, so the fake can take it down the
way launchd did. The positive control removes the guard from the copied
adapter and must reproduce the incident: a run that dies inside setup-fleet,
with reload-fleet booted out and never bootstrapped.

What this cannot show is launchd itself: whether its `print` output carries the
`pid = N` line the guard reads, and whether bootout reaches the process group
exactly as the fake does. The launchd canary in the PR body is for that.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time
from pathlib import Path

import pytest

from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.conftest import TG_STUB, _write_exec, constructed_env

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
FLEET = "tfleet"
PREFIX = "com.test.tf"
SELF = f"{PREFIX}.reload-fleet"
# task-recheck sorts AFTER reload-fleet: on the incident host, the jobs after it
# were exactly the ones the dead run never reached.
JOBS = ("creds-check", "reload-fleet", "task-recheck")
LIB_SCRIPTS = (
    "reload-fleet.sh",
    "lib-common.sh",
    "cli-context.sh",
    "supervisor.sh",
    "setup-fleet",
    "install_fleet_timer_launchd.sh",
    "install_fleet_timer.sh",
)

FAKE_LAUNCHCTL = r"""#!/bin/bash
# launchd, where #1924 lives: `print` of the job running the reload reports its
# pid, and `bootout` of that job stops it and takes its process group with it
# (this process included: it is in that group). Every other label is not loaded.
printf '%s\n' "$*" >> "$FAKE_LOG"
label="${2##*/}"
job_pid="$(cat "$JOB_PID_FILE" 2>/dev/null || true)"
case "$1" in
    print)
        if [ "$label" = "$SELF_LABEL" ] && [ -n "$job_pid" ]; then
            printf '%s = {\n\tstate = running\n\tpid = %s\n}\n' "$2" "$job_pid"
            exit 0
        fi
        exit 113 ;;
    bootout)
        if [ "$label" = "$SELF_LABEL" ] && [ -n "$job_pid" ]; then
            kill -TERM -- "-$job_pid"
            sleep 30
        fi
        exit 0 ;;
esac
exit 0
"""

FAKE_SYSTEMCTL = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
exit 0
"""

# The generate step prints one line first, so a test that kills it mid-step can
# tell streamed output from output buffered until the step returned.
FAKE_CLAUDLOBBY = """#!/bin/bash
for a in "$@"; do
    if [ "$a" = generate ]; then
        echo "generate: composing"
        [ -z "${GENERATE_SLEEP:-}" ] || sleep "$GENERATE_SLEEP"
        exit 0
    fi
done
exit 0
"""


def _plist(label, root, revision):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key>\n  <string>{label}</string>\n"
        "  <key>EnvironmentVariables</key>\n  <dict>\n"
        f"    <key>CLAUDLOBBY_ROOT</key>\n    <string>{root}</string>\n"
        f"    <key>X_REVISION</key>\n    <string>{revision}</string>\n"
        "  </dict>\n</dict>\n</plist>\n"
    )


class Host:
    """A throwaway root, HOME and PATH of fakes for one platform."""

    def __init__(self, tmp_path, os_name, lib_edit=None):
        self.tmp = tmp_path
        self.os_name = os_name
        self.root = tmp_path / "root"
        self.home = tmp_path / "home"
        lib = self.root / "lib"
        lib.mkdir(parents=True)
        for name in LIB_SCRIPTS:
            text = (LIB / name).read_text()
            if lib_edit and name in lib_edit:
                old, new = lib_edit[name]
                assert text.count(old) == 1, f"mutation anchor not unique in {name}"
                text = text.replace(old, new)
            _write_exec(str(lib / name), text)
        _write_exec(
            str(lib / "check-npx-cache.sh"),
            '#!/bin/bash\necho "npx: checking"\n[ -z "${NPX_SLEEP:-}" ] || sleep "$NPX_SLEEP"\n',
        )
        _write_exec(str(lib / "tg-post.sh"), TG_STUB)

        fleet_dir = self.root / "local" / FLEET
        self.timers = fleet_dir / "runtime" / "fleet" / "timers"
        self.timers.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text(
            f"fleet:\n  name: {FLEET}\n  service_prefix: {PREFIX}\n"
            "bots:\n  tbot:\n    expertise: [software-engineering]\n"
        )
        bot = fleet_dir / "runtime" / "bots" / "tbot"
        bot.mkdir(parents=True)
        (bot / "bot.conf").write_text(
            'export TELEGRAM_GROUP_CHAT_ID="-1001234567890"\n'
        )
        for job in JOBS:
            unit = f"{PREFIX}.{job}"
            if os_name == "Darwin":
                (self.timers / f"{unit}.plist").write_text(
                    _plist(unit, self.root, "v2")
                )
            else:
                (self.timers / f"{unit}.service").write_text(
                    f"[Service]\nEnvironment=CLAUDLOBBY_ROOT={self.root}\nExecStart=/bin/true\n"
                )
                (self.timers / f"{unit}.timer").write_text(
                    "[Timer]\nOnCalendar=daily\n"
                )

        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        (self.home / ".config" / "systemd" / "user").mkdir(parents=True)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _write_exec(
            str(bindir / "uname"), '#!/bin/bash\nprintf "%s\\n" "$FAKE_UNAME"\n'
        )
        _write_exec(str(bindir / "launchctl"), FAKE_LAUNCHCTL)
        _write_exec(str(bindir / "systemctl"), FAKE_SYSTEMCTL)
        _write_exec(str(bindir / "claude"), "#!/bin/bash\nexit 0\n")
        _write_exec(str(bindir / "claudlobby"), FAKE_CLAUDLOBBY)

        self.fake_log = tmp_path / "supervisor-calls.log"
        self.out_file = tmp_path / "run.out"
        self.job_pid_file = tmp_path / "job.pid"
        self.env = constructed_env(
            PATH=f"{bindir}:{os.environ['PATH']}",
            HOME=self.home,
            CLAUDLOBBY_ROOT=self.root,
            CLAUDLOBBY_CLI=bindir / "claudlobby",
            FAKE_UNAME=os_name,
            FAKE_LOG=self.fake_log,
            JOB_PID_FILE=self.job_pid_file,
            SELF_LABEL=SELF,
            TG_CAPTURE=tmp_path / "tg-capture",
            TMUX_TMPDIR=tmp_path / "no-tmux",
            PLANE_EMIT_DISABLED="1",
            # A host without flock (macOS) locks with mkdir, which a killed run
            # leaves behind; the next run would spin the default 30s on it.
            WITH_LOCK_WAIT_S="2",
        )

    def install_self(self, revision):
        """The reload-fleet job as launchd has it loaded: its installed plist."""
        (self.agents / f"{SELF}.plist").write_text(_plist(SELF, self.root, revision))

    def start(self, **extra):
        """Start reload-fleet as launchd starts a job: the leader of its own
        process group, its pid recorded before exec so `print` can report it.
        Output goes to a file, never a pipe: a pipe stays open for as long as
        an orphaned step still holds it, so waiting on one would time the
        step, not the run."""
        env = dict(self.env, **{k: str(v) for k, v in extra.items()})
        with open(self.out_file, "a") as out:    # the child keeps its own copy
            return subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'printf "%s" "$$" > "$JOB_PID_FILE"; exec "$0" "$@"',
                    str(self.root / "lib" / "reload-fleet.sh"),
                    FLEET,
                ],
                env=env,
                start_new_session=True,
                stdout=out,
                stderr=subprocess.STDOUT,
            )

    def run(self, timeout=120, **extra):
        p = self.start(**extra)
        p.wait(timeout=timeout)
        return p.returncode, self.out_file.read_text()

    def log(self):
        f = self.root / "state" / "reload-fleet.log"
        return f.read_text() if f.exists() else ""

    def calls(self):
        return self.fake_log.read_text() if self.fake_log.exists() else ""

    def records(self):
        d = self.root / "state" / "reload-fleet.inflight"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def wait_for_log(self, needle, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.log():
                return
            time.sleep(0.1)
        raise AssertionError(f"{needle!r} never reached the log:\n{self.log()}")


def _telegram(tmp_path):
    """Every message the run sent to Telegram, as the TG_STUB captured it."""
    cap = tmp_path / "tg-capture"
    return cap.read_text().splitlines() if cap.exists() else []


def _wait_group_gone(p, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(p.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError("the run's process group outlived its step")


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


UID = os.getuid()
OK_LINE = "download + generate OK"
# The kill tests are about bash and signals, not the supervisor: they run on
# the host's own OS, so on a Mac they exercise its /bin/bash 3.2.
HOST_OS = platform.system()


# --- point 1: a job never boots itself out mid-run ---------------------------


def test_reload_job_is_never_booted_out_while_it_runs(tmp_path):
    host = Host(tmp_path, "Darwin")
    host.install_self("v2")  # loaded, and identical to what generate composed
    rc, out = host.run()
    log, calls = host.log(), host.calls()
    assert rc == 0, out + log
    assert OK_LINE in log, log
    assert f"bootout gui/{UID}/{SELF}" not in calls, calls
    assert f"current: {SELF} is running this enrollment" in log, log
    # every other job still re-installs, the one sorting AFTER reload-fleet included
    for job in ("creds-check", "task-recheck"):
        assert f"bootout gui/{UID}/{PREFIX}.{job}" in calls, calls
        assert f"bootstrap gui/{UID} {host.agents}/{PREFIX}.{job}.plist" in calls, calls
        assert (host.agents / f"{PREFIX}.{job}.plist").exists()
    assert "reload_failed" not in log, log
    # the nightly case has nothing to apply, so it says nothing to anyone
    assert _telegram(tmp_path) == []


def test_a_changed_reload_plist_is_deferred_with_nothing_touched(tmp_path):
    host = Host(tmp_path, "Darwin")
    host.install_self("v1")  # loaded from an OLDER plist than generate composed
    rc, out = host.run()
    log, calls = host.log(), host.calls()
    assert rc == 0, out + log
    assert OK_LINE in log, log  # the run itself finishes
    assert f"DEFERRED: {SELF} is running this enrollment" in log, log
    assert (
        "job re-enroll DEFERRED" in log
        and f"lib/setup-fleet {FLEET} --jobs-only" in log
    ), log
    # untouched: the loaded job's own file still describes the loaded job
    assert "v1" in (host.agents / f"{SELF}.plist").read_text()
    assert f"bootout gui/{UID}/{SELF}" not in calls, calls
    assert f"bootstrap gui/{UID} {host.agents}/{PREFIX}.task-recheck.plist" in calls, (
        calls
    )
    # The log line is read by nobody: reload-fleet runs this step non-fatally,
    # and no doctor rung compares a loaded job with its composed plist. So the
    # deferral also goes out as ONE notice naming the job and the command, and
    # it is registered as a notice, so it never pages as critical.
    sent = _telegram(tmp_path)
    assert len(sent) == 1, sent
    assert f"FLEET NOTICE [job_reenroll_deferred]: {SELF} changed" in sent[0], sent
    assert f"lib/setup-fleet {FLEET} --jobs-only" in sent[0], sent
    assert SYSTEM_EVENT_SEVERITY["job_reenroll_deferred"] == "notice"


def test_removing_the_guard_reproduces_1924(tmp_path):
    """Positive control: with the guard cut out of the copied adapter, the
    harness must show the incident -- and point 2 must make it visible."""
    host = Host(
        tmp_path,
        "Darwin",
        lib_edit={
            "supervisor.sh": (
                '    [ "$_OS" = "Darwin" ] || return 1\n    local out line pids=""',
                '    return 1  # mutant: the #1924 guard removed\n    local out line pids=""',
            )
        },
    )
    host.install_self("v2")
    p = host.start()
    try:
        p.wait(timeout=120)
        out = host.out_file.read_text()
    finally:
        _kill_group(p)
    log, calls = host.log(), host.calls()
    assert p.returncode != 0, out + log
    assert OK_LINE not in log, log
    assert f"bootout gui/{UID}/{SELF}" in calls, calls
    assert f"{SELF}.plist" not in "".join(
        ln for ln in calls.splitlines(True) if "bootstrap" in ln
    )
    assert "task-recheck" not in calls, calls  # never reached, as on the incident host
    # point 2 on the incident's own shape: the step, its streamed output, the alert
    assert "step: lib/setup-fleet --jobs-only" in log, log
    assert f"installed + loaded: {PREFIX}.creds-check" in log, log
    failed = [ln for ln in log.splitlines() if "reload_failed:" in ln]
    assert (
        failed and "killed" in failed[0] and "lib/setup-fleet --jobs-only" in failed[0]
    ), log
    assert host.records() == []


@pytest.mark.skipif(HOST_OS != "Linux", reason="faking Linux needs Linux userland under lib-common")
def test_systemd_reenrolls_every_job_its_own_included(tmp_path):
    """Linux is unaffected: enable --now of the reload-fleet timer does not stop
    the service running it, so the guard never applies and nothing is skipped."""
    host = Host(tmp_path, "Linux")
    rc, out = host.run()
    log, calls = host.log(), host.calls()
    assert rc == 0, out + log
    assert OK_LINE in log, log
    for job in JOBS:
        assert f"--user enable --now {PREFIX}.{job}.timer" in calls, calls
    assert "print gui/" not in calls and "bootout" not in calls, calls
    assert "current:" not in log and "DEFERRED" not in log, log


# --- point 2: a mid-step kill leaves a trace and an alert --------------------


def test_a_sigterm_mid_step_is_raised_at_once_naming_the_step(tmp_path):
    """Only the run's own process is signalled, while its step still has 30s
    to go: the alert must not wait for the step to finish."""
    host = Host(tmp_path, HOST_OS)
    p = host.start(GENERATE_SLEEP=30)
    try:
        host.wait_for_log("generate: composing")
        t0 = time.monotonic()
        os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=60)
        elapsed = time.monotonic() - t0
    finally:
        _kill_group(p)
    log = host.log()
    assert elapsed < 15, f"the alert waited {elapsed:.1f}s for the step"
    assert "step: claudlobby generate" in log, log
    assert "generate: composing" in log, log  # streamed, not lost with the step
    failed = [ln for ln in log.splitlines() if "reload_failed:" in ln]
    assert (
        len(failed) == 1
        and "killed" in failed[0]
        and "claudlobby generate" in failed[0]
    ), log
    assert OK_LINE not in log
    assert host.records() == []


def test_a_sigkill_mid_step_is_raised_by_the_next_run(tmp_path):
    host = Host(tmp_path, HOST_OS)
    p = host.start(GENERATE_SLEEP=30)
    try:
        host.wait_for_log("generate: composing")
        killed_pid = p.pid
        _kill_group(p)
        p.wait(timeout=60)
    finally:
        _kill_group(p)
    log = host.log()
    assert "step: claudlobby generate" in log and "generate: composing" in log, log
    assert "reload_failed" not in log, log  # nothing can run on a SIGKILL
    assert host.records() == [f"{FLEET}.{killed_pid}"]

    rc, out = host.run()
    log = host.log()
    assert rc == 0, out + log
    failed = [ln for ln in log.splitlines() if "reload_failed:" in ln]
    assert len(failed) == 1, log
    assert f"pid {killed_pid}" in failed[0] and "claudlobby generate" in failed[0], (
        failed
    )
    assert log.rstrip().endswith("live reload"), log  # and this run still finished
    assert host.records() == []


def test_the_next_run_leaves_live_runs_and_other_fleets_alone(tmp_path):
    host = Host(tmp_path, HOST_OS)
    records = host.root / "state" / "reload-fleet.inflight"
    records.mkdir(parents=True)
    # a run still going (its args name reload-fleet), and a dead record of a
    # fleet whose name merely extends this one
    live = subprocess.Popen(["bash", "-c", "exec -a reload-fleet.sh sleep 60"])
    try:
        time.sleep(0.3)
        (records / f"{FLEET}.{live.pid}").write_text(
            "started=x\nstep=claudlobby generate\n"
        )
        (records / f"{FLEET}.x.999999").write_text(
            "started=x\nstep=claudlobby generate\n"
        )
        rc, out = host.run()
    finally:
        live.kill()
        live.wait()
    log = host.log()
    assert rc == 0, out + log
    assert "reload_failed" not in log, log
    assert host.records() == sorted([f"{FLEET}.x.999999", f"{FLEET}.{live.pid}"])



def test_an_orphaned_lock_subshell_starts_no_further_step(tmp_path):
    """With flock the steps run in with_lock's subshell, so a SIGTERM aimed at
    the run's own shell leaves that subshell running on. Killed between steps
    (here during the npx preflight), it must start no further step -- and must
    not re-create the record the trap already raised, or the next run would
    raise the same kill a second time."""
    host = Host(tmp_path, HOST_OS)
    p = host.start(NPX_SLEEP=3)
    try:
        host.wait_for_log("npx: checking")
        os.kill(p.pid, signal.SIGTERM)
        p.wait(timeout=60)
        _wait_group_gone(p)    # the orphan ends its preflight, then stops
    finally:
        _kill_group(p)
    log = host.log()
    assert "npx cache preflight; the run did not finish" in log, log
    assert "step: claudlobby generate" not in log, log
    assert host.records() == [], host.records()
    rc, out = host.run()
    log = host.log()
    assert rc == 0, out + log
    assert len([ln for ln in log.splitlines() if "reload_failed:" in ln]) == 1, log
