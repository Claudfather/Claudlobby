"""The heavy slot's render profile: `heavy-slot.py run --profile render -- ARGV`.

A headless browser render starts only inside the host's render start line, with
the heavy slot taken through its queue; it runs in a systemd user scope of its
own with the session bus cut; and its runner kills the whole scope when a
reading crosses a stop line, or when the runner itself dies. After a kill no render starts for 30
minutes, and two kills in one local day hold renders until the fleet's manager
or the operator lifts the hold. Each test drives
the real wrapper with injected readings: HEAVY_SLOT_PROC is a directory
standing in for /proc's meminfo and loadavg, and a job moves a reading across a
line by rewriting that directory, so nothing here loads the host. The retry
rule's tests date their kills by writing the render record the runner reads.
Tests that start a scope need a systemd user manager and skip without one; the
refusals made before any scope exists run on every Linux host.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env, load_lib_module

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py"
LINUX = sys.platform.startswith("linux")
EX_TEMPFAIL = 75
EX_STOPPED = 79

needs_linux = pytest.mark.skipif(not LINUX, reason="a render runs in a systemd user scope: Linux only")

# `cross MEM_MIB LOAD1 LOAD5`: what a job runs to move the injected readings
# across a line. Each file is replaced whole, so a reading is never half written.
CROSS = """#!/bin/sh
printf 'MemTotal:       16384000 kB\\nMemAvailable:   %s kB\\n' "$(($1 * 1024))" > "$HEAVY_SLOT_PROC/.meminfo"
mv "$HEAVY_SLOT_PROC/.meminfo" "$HEAVY_SLOT_PROC/meminfo"
printf '%s %s 1.00 2/700 4242\\n' "$2" "$3" > "$HEAVY_SLOT_PROC/.loadavg"
mv "$HEAVY_SLOT_PROC/.loadavg" "$HEAVY_SLOT_PROC/loadavg"
"""


def _readings(proc: Path, mem_mib: int, load1: float = 2.0, load5: float = 2.0) -> None:
    for name, text in (
        ("meminfo", f"MemTotal:       16384000 kB\nMemFree:          512000 kB\n"
                    f"MemAvailable:   {mem_mib * 1024} kB\n"),
        ("loadavg", f"{load1:.2f} {load5:.2f} 1.00 2/700 4242\n"),
    ):
        tmp = proc / f".{name}"
        tmp.write_text(text)
        os.replace(tmp, proc / name)


@pytest.fixture(scope="module")
def user_scope():
    """A systemd user manager that can make a scope here, or a skip naming why not."""
    if not LINUX:
        pytest.skip("a render scope is a systemd user scope: Linux only")
    if shutil.which("systemd-run") is None:
        pytest.skip("no systemd-run on this host")
    probe = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"no systemd user manager to make a scope: {probe.stderr.strip()[:200]}")


@pytest.fixture()
def rs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cross").write_text(CROSS)
    (bin_dir / "cross").chmod(0o755)
    proc = tmp_path / "proc"
    proc.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    # Fast polls and a short limit for a test; every line is the shipped default.
    (state / "render-lines").write_text("# a test's polls\npoll_s = 0.1\nmax_minutes = 2\n")
    env = constructed_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HEAVY_SLOT_DIR=state,
        HEAVY_SLOT_EVENTS_FILE=tmp_path / "events.jsonl",
        HEAVY_SLOT_PROC=proc,
        HEAVY_SLOT_BOOT_ID="this-boot",
        FLEET_NAME="testfleet",
        BOT_ID="alpha",
        STUB_DIR=tmp_path,
        PLANE_EMIT_DISABLED="1",
        TZ="UTC",
    )
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):  # the user's own manager makes the scope
        if key in os.environ:
            env[key] = os.environ[key]
    _readings(proc, 6000)
    ns = SimpleNamespace(env=env, tmp=tmp_path, state=state, proc=proc, events=tmp_path / "events.jsonl")
    yield ns
    # A test that failed part way may leave its scope: end it, as the runner would.
    try:
        cgroup = json.loads((state / "slot-0.lock").read_text()).get("cgroup") or ""
    except (OSError, ValueError):
        cgroup = ""
    if cgroup.endswith(".scope") and "/heavy-render-" in cgroup:
        try:
            Path(f"/sys/fs/cgroup{cgroup}/cgroup.kill").write_text("1")
        except OSError:
            pass


def _render(rs, *argv, timeout: float = 60, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), "run", "--profile", "render", "--", *argv],
        env={**rs.env, **{k: str(v) for k, v in extra.items()}},
        capture_output=True, text=True, timeout=timeout,
    )


def _events(rs) -> list:
    if not rs.events.exists():
        return []
    return [json.loads(line) for line in rs.events.read_text().splitlines() if line.strip()]


def _of(rs, kind: str) -> list:
    return [e["data"] for e in _events(rs) if e["type"] == kind]


def _record(rs) -> dict:
    return json.loads((rs.state / "slot-0.lock").read_text())


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _alive(pid: int, word: str = "sleep") -> bool:
    """A live process with this pid that still runs `word` (never a zombie)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return stat[stat.rindex(b")") + 2:][:1] not in (b"Z", b"X") and word.encode() in cmd


def _wait_for(pred, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _kill(epoch: float, line: str = "stop_mib", by: str = "runner") -> dict:
    """A kill as the render record holds one."""
    return {"kind": "kill", "epoch": epoch, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
            "unit": f"heavy-render-testfleet-alpha-{int(epoch)}-1", "line": line, "by": by}


def _write_render_record(rs, *entries: dict) -> None:
    (rs.state / "render-record").write_text("".join(json.dumps(e) + "\n" for e in entries))


def _render_record(rs) -> list:
    path = rs.state / "render-record"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _noon(days_ago: int) -> float:
    """Noon UTC that many days ago: the fixture's TZ is UTC, so it is that local day."""
    return float((int(time.time()) // 86400 - days_ago) * 86400 + 12 * 3600)


def _lift(rs, *args: str, env: dict = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(WRAPPER), "lift-hold", *args], env=env or rs.env,
                          capture_output=True, text=True, timeout=60)


def _refused_at(rs) -> list:
    return [d["where"] for d in _of(rs, "heavy_slot_refused")]


# --- the lines themselves ---------------------------------------------------------

def test_the_shipped_lines_and_their_edges():
    """The primary host's ruled lines (2026-10-19) are the defaults, and each
    is read in the direction the rule states: a start line holds AT its number,
    a stop line is crossed only PAST it."""
    hs = load_lib_module("heavy-slot")
    lines = dict(hs.RENDER_LINES)
    assert {k: lines[k] for k in ("start_mib", "start_load1", "stop_mib", "stop_load5", "stop_load1")} == {
        "start_mib": 4820, "start_load1": 16, "stop_mib": 3800, "stop_load5": 25, "stop_load1": 40}

    def at(mem, load1=2.0, load5=2.0):
        return {"mem_available_mib": mem, "load1": load1, "load5": load5}

    assert hs._gate_refusal(at(4820), lines) is None
    assert hs._gate_refusal(at(4819), lines)
    assert hs._gate_refusal(at(6000, load1=15.99), lines) is None
    assert hs._gate_refusal(at(6000, load1=16.0), lines)
    assert hs._gate_refusal(at(6000, load5=25.01), lines)  # past a stop line: it would die at once
    assert hs._breach(at(3800), lines) is None
    assert hs._breach(at(3799), lines)[0] == "stop_mib"
    assert hs._breach(at(6000, load5=25.0), lines) is None
    assert hs._breach(at(6000, load5=25.01), lines)[0] == "stop_load5"
    assert hs._breach(at(6000, load1=40.0), lines) is None
    assert hs._breach(at(6000, load1=40.01), lines)[0] == "stop_load1"


# --- refused before anything starts --------------------------------------------------

@needs_linux
@pytest.mark.parametrize("mem, load1, load5, says", [
    (4189, 2.0, 2.0, "MemAvailable 4189 MiB"),   # the reading a render once started at
    (6000, 16.0, 2.0, "load1 16.00"),
    (6000, 2.0, 25.5, "load5 25.50"),            # already past a stop line
])
def test_refused_outside_the_start_line(rs, mem, load1, load5, says):
    _readings(rs.proc, mem, load1, load5)
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL, r.stderr
    assert not (rs.tmp / "ran").exists()
    assert _last_line(r.stderr).startswith("heavy-slot: NOT RUN"), r.stderr
    assert says in r.stderr, r.stderr
    refused = _of(rs, "heavy_slot_refused")
    assert len(refused) == 1, _events(rs)
    assert refused[0]["where"] == "gate" and refused[0]["profile"] == "render"
    assert refused[0]["readings"] == {"mem_available_mib": mem, "load1": load1, "load5": load5}
    assert _of(rs, "heavy_slot_acquired") == []
    assert not (rs.state / "slot-0.lock").exists()  # the slot was never taken
    queue = rs.state / "queue.lock"
    assert not queue.exists() or json.loads(queue.read_text() or "{}").get("tickets", []) == []


@needs_linux
def test_a_refusal_through_a_pipe_still_starts_nothing(rs):
    """`runner ... | tail` gives the pipeline tail's status: the shell's rule, not
    the runner's. What the caller cannot lose is the refusal itself: the gate is
    read in the process that would start the job, so a closed gate starts
    nothing whatever becomes of the status; the refusal is on stderr, which the
    pipe does not take, and is the last line through `2>&1 |`; and it is
    recorded each time."""
    _readings(rs.proc, 4189)
    env = {**rs.env, "W": str(WRAPPER)}
    job = """"$W" run --profile render -- sh -c 'touch "$STUB_DIR/ran"'"""
    plain = subprocess.run(["bash", "-c", job + ' | tail -n 5; echo "pipeline=$?"'],
                           env=env, capture_output=True, text=True, timeout=60)
    assert "pipeline=0" in plain.stdout, plain.stdout
    assert "NOT RUN" in plain.stderr and "4189 MiB" in plain.stderr, plain.stderr
    merged = subprocess.run(["bash", "-c", job + ' 2>&1 | tail -n 5; echo "pipeline=$?"'],
                            env=env, capture_output=True, text=True, timeout=60)
    out = [line for line in merged.stdout.splitlines() if line.strip()]
    assert out[-1] == "pipeline=0" and out[-2].startswith("heavy-slot: NOT RUN"), merged.stdout
    strict = subprocess.run(["bash", "-c", "set -o pipefail; " + job + ' | tail -n 5; echo "pipeline=$?"'],
                            env=env, capture_output=True, text=True, timeout=60)
    assert f"pipeline={EX_TEMPFAIL}" in strict.stdout, strict.stdout
    assert not (rs.tmp / "ran").exists()
    assert [d["where"] for d in _of(rs, "heavy_slot_refused")] == ["gate"] * 3


@needs_linux
def test_the_host_sets_its_own_lines(rs):
    (rs.state / "render-lines").write_text("poll_s = 0.1\nstart_mib = 7000\n")
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL, r.stderr
    assert "7000 MiB" in r.stderr and not (rs.tmp / "ran").exists()


@needs_linux
@pytest.mark.parametrize("text", ["start_mib = lots\n", "start_mb = 4820\n", "poll_s = 0\n", "stop_mib\n"])
def test_a_lines_file_it_cannot_read_refuses(rs, text):
    (rs.state / "render-lines").write_text(text)
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2, r.stderr
    assert not (rs.tmp / "ran").exists()
    assert "render-lines" in r.stderr and _last_line(r.stderr).startswith("heavy-slot: NOT RUN"), r.stderr


def test_an_unknown_profile_is_refused(rs):
    r = subprocess.run([str(WRAPPER), "run", "--profile", "nope", "--", "sh", "-c", 'touch "$STUB_DIR/ran"'],
                       env=rs.env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "unknown profile" in r.stderr, r.stderr
    assert not (rs.tmp / "ran").exists()


@pytest.mark.skipif(LINUX, reason="checks a host with no systemd")
def test_refused_where_there_is_no_systemd(rs):
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2 and "systemd user scope" in r.stderr, r.stderr
    assert not (rs.tmp / "ran").exists()


@needs_linux
def test_no_scope_no_render(rs):
    """A scope that cannot be made (no user manager, say) starts nothing, and
    the slot it took is released with the reason."""
    fake = rs.tmp / "bin" / "systemd-run"
    fake.write_text("#!/bin/sh\necho 'Failed to connect to bus: No medium found' >&2\nexit 1\n")
    fake.chmod(0o755)
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2, r.stderr
    assert not (rs.tmp / "ran").exists()
    rec = _record(rs)
    assert rec["state"] == "released" and rec["ended"] == "not-started", rec
    assert _last_line(r.stderr).startswith("heavy-slot: NOT RUN"), r.stderr
    assert [d["ended"] for d in _of(rs, "heavy_slot_released")] == ["not-started"]


@needs_linux
def test_a_render_waits_its_turn_in_the_queue(rs):
    """The slot part of the gate is the slot's own queue, never a look at it."""
    import fcntl

    (rs.state / "slot-0.lock").write_text(json.dumps({
        "state": "held", "fleet": "otherfleet", "bot": "beta", "shape": "pytest", "tool": "pytest",
        "started_epoch": int(time.time()), "started_at": "2026-10-19T00:00:00Z"}))
    fd = os.open(rs.state / "slot-0.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    finally:
        os.close(fd)
    assert r.returncode == EX_TEMPFAIL, r.stderr
    assert not (rs.tmp / "ran").exists()
    assert "otherfleet/beta" in r.stderr, r.stderr
    refused = _of(rs, "heavy_slot_refused")
    assert len(refused) == 1 and refused[0]["where"] == "wrapper" and refused[0]["profile"] == "render"
    assert refused[0]["ticket"]["bot"] == "alpha"


# --- in a scope -------------------------------------------------------------------------

@needs_linux
def test_a_render_runs_in_its_own_scope_with_the_session_bus_cut(rs, user_scope):
    r = _render(rs, "sh", "-c",
                'sed -n "s/^0:://p" /proc/self/cgroup > "$STUB_DIR/cgroup"; '
                'printf %s "$DBUS_SESSION_BUS_ADDRESS" > "$STUB_DIR/bus"')
    assert r.returncode == 0, r.stderr
    rec = _record(rs)
    assert rec["profile"] == "render" and rec["state"] == "released"
    assert rec["ended"] == "done" and rec["exit"] == 0
    assert (rs.tmp / "cgroup").read_text().strip() == rec["cgroup"]
    assert rec["cgroup"].endswith(f"/{rec['unit']}.scope")
    assert (rs.tmp / "bus").read_text() == "unix:path=/nonexistent"
    assert rec["peak"]["mem_available_mib_start"] == 6000 and rec["peak"]["mem_available_mib_low"] == 6000
    acquired, released = _of(rs, "heavy_slot_acquired"), _of(rs, "heavy_slot_released")
    assert acquired[0]["profile"] == "render" and acquired[0]["unit"] == rec["unit"]
    assert acquired[0]["readings"] == {"mem_available_mib": 6000, "load1": 2.0, "load5": 2.0}
    assert released[0]["ended"] == "done" and released[0]["peak"] == rec["peak"]
    assert _of(rs, "heavy_slot_stopped") == []
    assert "render done" in _last_line(r.stderr), r.stderr
    # nothing left behind: systemd removes the emptied scope's cgroup on its own time
    _wait_for(lambda: not Path(f"/sys/fs/cgroup{rec['cgroup']}").exists(), timeout=10)


@needs_linux
def test_through_a_pipe_an_open_gate_runs_the_job(rs, user_scope):
    """The pipe test's positive control: the same pipeline, the gate open."""
    env = {**rs.env, "W": str(WRAPPER)}
    out = subprocess.run(["bash", "-c", """"$W" run --profile render -- sh -c 'touch "$STUB_DIR/ran"' | tail -n 5"""],
                         env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and (rs.tmp / "ran").exists(), out.stderr


@needs_linux
def test_the_job_cannot_reach_the_session_bus(rs, user_scope):
    """Unset, the variable is no cut at all: libdbus falls back to
    $XDG_RUNTIME_DIR/bus. So the profile points it at nothing."""
    if shutil.which("dbus-send") is None:
        pytest.skip("no dbus-send here")
    probe = ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.GetId"]
    if subprocess.run(probe, env=rs.env, capture_output=True, timeout=30).returncode != 0:
        pytest.skip("no session bus to cut here")
    unset = {k: v for k, v in rs.env.items() if k != "DBUS_SESSION_BUS_ADDRESS"}
    assert subprocess.run(probe, env=unset, capture_output=True, timeout=30).returncode == 0
    r = _render(rs, "sh", "-c", " ".join(probe) + '; echo "rc=$?" > "$STUB_DIR/rc"')
    assert r.returncode == 0, r.stderr
    assert (rs.tmp / "rc").read_text().strip() != "rc=0"


@needs_linux
@pytest.mark.parametrize("cross, line, says", [
    ("3700 2 2", "stop_mib", "MemAvailable 3700 MiB"),
    ("6000 2 25.5", "stop_load5", "load5 25.50"),
    ("6000 40.5 2", "stop_load1", "load1 40.50"),
])
def test_killed_when_a_reading_crosses_a_stop_line(rs, user_scope, cross, line, says):
    job = ('setsid sleep 300 & echo $! > "$STUB_DIR/detached"; touch "$STUB_DIR/started"; '
           f'cross {cross}; sleep 300')
    began = time.monotonic()
    r = _render(rs, "sh", "-c", job)
    assert r.returncode == EX_STOPPED, r.stderr
    assert time.monotonic() - began < 30
    assert (rs.tmp / "started").exists()
    detached = int((rs.tmp / "detached").read_text())
    assert not _alive(detached), "a detached child outlived its scope"
    rec = _record(rs)
    assert rec["state"] == "released" and rec["ended"] == "killed", rec
    assert rec["stop"]["line"] == line and says in rec["stop"]["reading"], rec["stop"]
    stopped = _of(rs, "heavy_slot_stopped")
    assert len(stopped) == 1 and stopped[0]["line"] == line and stopped[0]["unit"] == rec["unit"]
    assert [d["ended"] for d in _of(rs, "heavy_slot_released")] == ["killed"]
    assert "KILLED" in _last_line(r.stderr) and says in _last_line(r.stderr), r.stderr
    _wait_for(lambda: not Path(f"/sys/fs/cgroup{rec['cgroup']}").exists(), timeout=10)
    status = subprocess.run([str(WRAPPER), "status"], env=rs.env, capture_output=True, text=True, timeout=60)
    assert "render killed" in status.stdout and says in status.stdout, status.stdout
    # the kill is the retry rule's: with the readings back inside the lines, the next render is refused
    _readings(rs.proc, 6000)
    again = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert again.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), again.stderr
    assert _refused_at(rs) == ["retry"], _events(rs)
    assert [(e["kind"], e["line"], e["by"]) for e in _render_record(rs)] == [("kill", line, "runner")]


@needs_linux
def test_a_process_that_left_the_scope_is_killed_with_it(rs, user_scope):
    """With the bus cut, `systemd-run --user` still reaches the manager (its
    private socket) and can move a child into a scope of its own. Found by its
    parent link, that escapee is killed with the scope."""
    job = ('systemd-run --user --scope --quiet --collect -- sleep 301 & echo $! > "$STUB_DIR/escapee"; '
           'sleep 1; cross 3700 2 2; sleep 300')
    r = _render(rs, "sh", "-c", job)
    assert r.returncode == EX_STOPPED, r.stderr
    escapee = int((rs.tmp / "escapee").read_text())
    _wait_for(lambda: not _alive(escapee), timeout=10)
    assert _record(rs)["escapees"] >= 1


@needs_linux
def test_a_render_past_its_time_limit_is_killed(rs, user_scope):
    (rs.state / "render-lines").write_text("poll_s = 0.1\nmax_minutes = 0.02\n")
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/started"; sleep 300')
    assert r.returncode == EX_STOPPED, r.stderr
    rec = _record(rs)
    assert rec["ended"] == "killed" and rec["stop"]["line"] == "max_minutes", rec


@needs_linux
def test_the_scope_dies_with_its_runner(rs, user_scope):
    """SIGKILL leaves the runner no last act, so the scope's keeper does it: the
    kernel tells it its parent died, and it kills the scope and records why."""
    p = subprocess.Popen([str(WRAPPER), "run", "--profile", "render", "--", "sh", "-c",
                          'setsid sleep 300 & echo $! > "$STUB_DIR/detached"; touch "$STUB_DIR/started"; sleep 300'],
                         env=rs.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(lambda: (rs.tmp / "started").exists() and (rs.tmp / "detached").exists())
        detached = int((rs.tmp / "detached").read_text())
        p.send_signal(signal.SIGKILL)
        p.communicate(timeout=30)
        _wait_for(lambda: not _alive(detached), timeout=10)
        rec = _wait_for(lambda: _record(rs) if _record(rs).get("state") == "released" else None, timeout=10)
        assert rec["ended"] == "killed" and rec["stop"]["line"] == "runner", rec
        # on the plane too, as a stop the runner makes is: a count of kills reads these rows
        stopped = _wait_for(lambda: _of(rs, "heavy_slot_stopped"), timeout=10)
        assert len(stopped) == 1 and stopped[0]["line"] == "runner" and stopped[0]["unit"] == rec["unit"], _events(rs)
        assert [d["ended"] for d in _of(rs, "heavy_slot_released")] == ["killed"], _events(rs)
        assert [e["by"] for e in _render_record(rs)] == ["keeper"]
        again = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
        assert again.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), again.stderr
        assert _refused_at(rs) == ["retry"], _events(rs)
    finally:
        if p.poll() is None:
            p.kill()


@needs_linux
def test_the_keepers_events_reach_the_door_before_its_scope_dies(rs, user_scope):
    """The keeper's events go through the real door, emit_fleet_event in a shell
    of its own, and that shell runs in the scope the keeper is about to kill. A
    copy of the runner beside a stand-in lib-common, whose door takes half a
    second and then writes what reached it, shows the keeper waits for them: an
    event sent off without waiting dies with the scope."""
    rt = rs.tmp / "rt"
    rt.mkdir()
    shutil.copy2(WRAPPER, rt / "heavy-slot.py")
    (rt / "lib-common.sh").write_text(
        'emit_fleet_event() { sleep 0.5; printf "%s\\n" "$1" >> "$STUB_DIR/door"; }\n')
    env = {k: v for k, v in rs.env.items() if k not in ("HEAVY_SLOT_EVENTS_FILE", "PLANE_EMIT_DISABLED")}
    p = subprocess.Popen([str(rt / "heavy-slot.py"), "run", "--profile", "render", "--", "sh", "-c",
                          'touch "$STUB_DIR/started"; sleep 300'],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(lambda: (rs.tmp / "started").exists())
        p.send_signal(signal.SIGKILL)
        p.communicate(timeout=60)
        door = rs.tmp / "door"
        reached = _wait_for(lambda: door.exists() and "heavy_slot_released" in door.read_text()
                            and door.read_text().split(), timeout=20)
        assert "heavy_slot_stopped" in reached, reached
    finally:
        if p.poll() is None:
            p.kill()


@needs_linux
def test_a_signal_to_the_runner_ends_the_render_first(rs, user_scope):
    p = subprocess.Popen([str(WRAPPER), "run", "--profile", "render", "--", "sh", "-c",
                          'setsid sleep 300 & echo $! > "$STUB_DIR/detached"; touch "$STUB_DIR/started"; sleep 300'],
                         env=rs.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(lambda: (rs.tmp / "started").exists() and (rs.tmp / "detached").exists())
        detached = int((rs.tmp / "detached").read_text())
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=30)
        assert p.returncode == -signal.SIGTERM
        assert not _alive(detached)
        rec = _record(rs)
        assert rec["ended"] == "killed" and rec["stop"]["line"] == "signal", rec
    finally:
        if p.poll() is None:
            p.kill()


# --- the retry rule (the render rule, 2026-10-19) ---------------------------------------
# Under the start line, a render the rule lets through is refused at the gate
# instead (`where: gate`), so these need no scope.

@needs_linux
@pytest.mark.parametrize("minutes_ago, refused", [(29, True), (31, False)])
def test_no_render_starts_for_30_minutes_after_a_kill(rs, minutes_ago, refused):
    _readings(rs.proc, 4189)
    killed = time.time() - minutes_ago * 60
    _write_render_record(rs, _kill(killed))
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), r.stderr
    if refused:
        assert _refused_at(rs) == ["retry"], _events(rs)
        assert f"may start again at {time.strftime('%H:%M', time.gmtime(killed + 30 * 60))}" in r.stderr, r.stderr
        assert _last_line(r.stderr).startswith("heavy-slot: NOT RUN"), r.stderr
    else:
        assert _refused_at(rs) == ["gate"], _events(rs)
    assert not (rs.state / "slot-0.lock").exists()  # refused before the slot was asked for


@needs_linux
def test_the_rule_reads_the_last_runs_record_too(rs):
    """A kill the render record lacks (its write failed) is still in the slot's
    record until another job replaces it."""
    _readings(rs.proc, 4189)
    killed = time.time() - 5 * 60
    (rs.state / "slot-0.lock").write_text(json.dumps({
        "state": "released", "profile": "render", "ended": "killed", "unit": "heavy-render-x-1",
        "fleet": "otherfleet", "bot": "beta", "shape": "node", "tool": "node",
        "started_epoch": int(killed) - 60, "released_epoch": killed,
        "stop": {"line": "stop_mib", "reading": "MemAvailable 3700 MiB, under 3800 MiB"}}))
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), r.stderr
    assert _refused_at(rs) == ["retry"], _events(rs)


@needs_linux
def test_two_kills_in_one_local_day_hold_renders_until_the_manager_lifts_the_hold(rs):
    """Three days on the hold stands: time passing never lifts it. A worker
    cannot lift it; the manager can, and the lift records who lifted it."""
    _readings(rs.proc, 4189)
    day = _noon(3)
    _write_render_record(rs, _kill(day), _kill(day + 600, "runner", "keeper"))
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2 and not (rs.tmp / "ran").exists(), r.stderr
    assert "on hold" in r.stderr and "lift-hold" in r.stderr, r.stderr
    assert time.strftime("%Y-%m-%d", time.gmtime(day)) in r.stderr, r.stderr
    assert _refused_at(rs) == ["hold"], _events(rs)

    manager = {**rs.env, "MANAGER_TMUX": "alpha"}
    worker = _lift(rs, "--by", "alpha", "--reason", "my render", env={**rs.env, "MANAGER_TMUX": "boss"})
    assert worker.returncode == 1 and "manager" in worker.stderr, worker.stderr
    assert _lift(rs, env=manager).returncode == 2  # who lifts it must be named
    assert len(_render_record(rs)) == 2  # neither recorded anything
    lifted = _lift(rs, "--by", "alpha", "--reason", "host checked", env=manager)
    assert lifted.returncode == 0, lifted.stderr
    lift = _render_record(rs)[-1]
    assert (lift["kind"], lift["by"], lift["reason"]) == ("lift", "alpha", "host checked"), lift
    assert [d["by"] for d in _of(rs, "heavy_slot_hold_lifted")] == ["alpha"], _events(rs)

    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), r.stderr
    assert _refused_at(rs) == ["hold", "gate"], _events(rs)
    assert _lift(rs, "--by", "alpha", env=manager).returncode == 1  # nothing left to lift


@needs_linux
def test_the_operator_lifts_a_hold_from_a_shell_of_their_own(rs):
    day = _noon(1)
    _write_render_record(rs, _kill(day), _kill(day + 60))
    operator = {k: v for k, v in rs.env.items() if k not in ("BOT_ID", "FLEET_NAME")}
    lifted = _lift(rs, "--by", "the operator", env=operator)
    assert lifted.returncode == 0, lifted.stderr
    assert _render_record(rs)[-1]["by"] == "the operator"


@needs_linux
def test_kills_on_different_local_days_do_not_hold(rs):
    _readings(rs.proc, 4189)
    _write_render_record(rs, _kill(_noon(3)), _kill(_noon(2)))
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL and not (rs.tmp / "ran").exists(), r.stderr
    assert _refused_at(rs) == ["gate"], _events(rs)


@needs_linux
def test_a_lift_starts_the_count_again(rs):
    """After a lift, one more kill that day does not hold renders; a second does."""
    _readings(rs.proc, 4189)
    day = _noon(2)
    lift = {"kind": "lift", "epoch": day + 1200, "at": "", "by": "alpha", "reason": ""}
    _write_render_record(rs, _kill(day), _kill(day + 600), lift, _kill(day + 1800))
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == EX_TEMPFAIL and _refused_at(rs) == ["gate"], (r.stderr, _events(rs))
    with (rs.state / "render-record").open("a") as fh:
        fh.write(json.dumps(_kill(day + 2400)) + "\n")
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2 and _refused_at(rs) == ["gate", "hold"], (r.stderr, _events(rs))
    assert not (rs.tmp / "ran").exists()


@needs_linux
@pytest.mark.parametrize("text", ["not json\n", '{"kind": "kill"}\n', '{"kind": "nap", "epoch": 1}\n'])
def test_a_render_record_it_cannot_read_refuses(rs, text):
    _readings(rs.proc, 4189)
    (rs.state / "render-record").write_text(text)
    r = _render(rs, "sh", "-c", 'touch "$STUB_DIR/ran"')
    assert r.returncode == 2 and not (rs.tmp / "ran").exists(), r.stderr
    assert "render-record" in r.stderr and _last_line(r.stderr).startswith("heavy-slot: NOT RUN"), r.stderr
