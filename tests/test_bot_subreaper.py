"""Each bot session gets its own child subreaper (#2158).

On 2026-10-05 a cleanup loop killed a pattern match and then its parent, read
back with `ps -o ppid=`. The match was an orphaned job, and an orphan in a bot
session re-parented to the systemd user manager, the child subreaper that runs
every bot on the host: the manager took SIGTERM and stopped them all for 15
hours.

Each test starts a real tmux session the way start-bot.sh does
(bot_session_spawn in lib-common.sh) under MANAGER, a stand-in for the user
manager: a child subreaper that records each termination signal it receives
and survives it. The outage loop is the recorded command (test_signal_guard's
OUTAGE_LOOP), run from inside the session with its kill confined to the
stand-in's own tree, so no test can signal a process it did not start. That the
subreaper never keeps the activation lock is pinned beside bot_tmux's own check,
in test_native_admission.py.
"""

import ast
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.conftest import constructed_env
from tests.test_signal_guard import OUTAGE_LOOP

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "claudlobby/_runtime_scripts/lib-common.sh"
SUBREAPER = REPO / "claudlobby/_runtime_scripts/bot-subreaper.py"
REAPER = REPO / "claudlobby/_runtime_scripts/orphan-browser-reaper.sh"
TMUX = shutil.which("tmux")

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="a child subreaper is a Linux facility"),
    pytest.mark.skipif(TMUX is None, reason="needs tmux"),
]

# What a stray kill sends to an orphan's parent; the subreaper survives each.
STRAY = ("HUP", "INT", "QUIT", "TERM", "USR1", "USR2", "PIPE", "ALRM")
# The pattern in the recorded loop, replaced by a token only this test uses.
RECORDED_PATTERN = "notify-when-idle.sh worker .*msg-stale"

MANAGER = textwrap.dedent("""\
    import ctypes, os, signal, sys
    log, starter = sys.argv[1], sys.argv[2:]
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        sys.exit("cannot become a child subreaper")
    watched = {signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGCHLD}
    signal.pthread_sigmask(signal.SIG_BLOCK, watched)
    if os.fork() == 0:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, watched)
        os.execvp(starter[0], starter)
    while not os.path.exists(log + ".stop"):
        info = signal.sigtimedwait(watched, 0.1)
        if info is not None and info.si_signo != signal.SIGCHLD:
            with open(log, "a") as out:
                out.write(f"{info.si_signo} {info.si_pid}\\n")
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass
""")

# The loop's kill, confined: it signals a pid only inside the stand-in's tree,
# logs every pid it was handed, and waits for each victim to go so the loop
# reads the next parent after the kernel has re-parented its orphans.
KILL_SHIM = """\
ppid_of() {{
    local stat
    stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 0
    set -- ${{stat##*) }}
    echo "$2"
}}
kill() {{
    local pid p i
    for pid in "$@"; do
        p=$pid
        while [ -n "$p" ] && [ "$p" -gt 1 ] && [ "$p" -ne {manager} ]; do
            p=$(ppid_of "$p")
        done
        if [ "$p" = {manager} ]; then
            echo "$pid" >> {log}; command kill "$pid"
            for i in 1 2 3 4 5 6 7 8 9 10; do
                case "$(cat "/proc/$pid/stat" 2>/dev/null)" in ""|*") Z "*) break ;; esac
                sleep 0.05
            done
        else
            echo "outside $pid" >> {log}
        fi
    done
}}
"""


def stat(pid):
    """(state, ppid) of a pid, or None once it is gone."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    return fields[0], int(fields[1])


def live(pid):
    found = stat(pid)
    return found is not None and found[0] != "Z"


def parent(pid):
    found = stat(pid)
    return found[1] if found and found[0] != "Z" else None


def tree(root):
    """Every pid below root, with its state."""
    kids = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and (found := stat(entry.name)):
            kids.setdefault(found[1], []).append((int(entry.name), found[0]))
    out, todo = [], [root]
    while todo:
        for pid, state in kids.get(todo.pop(), []):
            out.append((pid, state))
            todo.append(pid)
    return out


def comm(pid):
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def status_field(pid, name):
    line = next(line for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                if line.startswith(f"{name}:"))
    return int(line.split()[1], 16)


def signal_bits(mask, names):
    return {name for name in names if mask >> (getattr(signal, f"SIG{name}") - 1) & 1}


def wait_for(probe, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Session:
    """A bot's tmux session, started by bot_session_spawn under MANAGER."""

    def __init__(self, scratch, pane, *, socket=None, python=sys.executable, os_name="", hold=None,
                 tmux_bin=None):
        self.socket = f"sr{random.randrange(10**6)}" if socket is None else socket
        tag = f"{self.socket}-{random.randrange(10**6)}"
        self.log, out, self.events_file = (scratch / f"{tag}.{kind}" for kind in ("signals", "out", "events"))
        self.env = constructed_env(TMUX_TMPDIR=scratch / "s", CLAUDLOBBY_ROOT=scratch / "root")
        starter = (f". {shlex.quote(str(LIB))} >/dev/null 2>&1\nset +e\n"
                   f"emit_fleet_event() {{ printf '%s %s\\n' \"$1\" \"$3\" >> {self.events_file}; }}\n"
                   f"_NATIVE_ADMISSION_PYTHON={shlex.quote(python)}\n"
                   + (f"_OS={os_name}\n" if os_name else "")
                   + (f"exec 8>>{shlex.quote(str(hold))}\n" if hold else "")
                   + (f"_TMUX_BIN={shlex.quote(tmux_bin)}\n" if tmux_bin else "")
                   + f"bot_session_spawn {shlex.quote(self.socket)} bot {shlex.quote(pane)}\nrc=$?\n"
                   f"printf '%s\\n%s\\n' \"$rc\" \"${{BOT_SUBREAPER_REPORT:-}}\" > {out}.tmp\n"
                   f"mv {out}.tmp {out}\n")
        self.manager = subprocess.Popen(
            [sys.executable, "-c", MANAGER, str(self.log), "bash", "-c", starter],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        wait_for(out.exists, "the starter's result")
        rc, self.report_text = (out.read_text().split("\n") + [""])[:2]
        self.rc = int(rc)
        self.report = dict(kv.split("=", 1) for kv in self.report_text.split() if "=" in kv)
        pids = {key: int(value) for key, value in self.report.items() if value.isdigit()}
        self.subreaper, self.server = pids.get("subreaper"), pids.get("server")

    def tmux(self, *args):
        socket = ["-L", self.socket] if self.socket else []  # "" is tmux's default socket
        return subprocess.run([TMUX, *socket, *args], env=self.env,
                              capture_output=True, text=True)

    def has_session(self):
        return self.tmux("has-session", "-t", "=bot").returncode == 0

    def pane(self):
        return int(self.tmux("list-panes", "-t", "bot", "-F", "#{pane_pid}").stdout.split()[0])

    def signals(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def events(self):
        return self.events_file.read_text().splitlines() if self.events_file.exists() else []

    def close(self):
        self.tmux("kill-server")
        for pid, _ in reversed(tree(self.manager.pid)):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        Path(f"{self.log}.stop").touch()
        try:
            os.kill(self.manager.pid, signal.SIGCHLD)  # wake it to see the stop file
            self.manager.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            self.manager.kill()
            self.manager.wait()


@pytest.fixture
def scratch(_isolate_claudlobby_root):
    # The shared test root is short on purpose: a tmux socket must fit sun_path.
    for name in ("s", "root"):
        (_isolate_claudlobby_root / name).mkdir(exist_ok=True)
    return _isolate_claudlobby_root


@pytest.fixture
def start(scratch):
    sessions = []

    def _start(body, **kwargs):
        pane = scratch / f"pane-{len(sessions)}.sh"
        pane.write_text(body)
        session = Session(scratch, f"bash {shlex.quote(str(pane))}", **kwargs)
        sessions.append(session)
        return session

    yield _start
    for session in sessions:
        session.close()


def test_an_orphan_of_the_session_reparents_to_the_bots_subreaper(scratch, start):
    record = scratch / "orphan"
    session = start(f"( sleep 3611 & echo $! > {record} )\nexec sleep 600\n")
    orphan = int(wait_for(lambda: record.exists() and record.read_text().strip(), "the orphan"))
    adopters = {session.manager.pid, session.subreaper} - {None}
    adopter = wait_for(lambda: parent(orphan) if parent(orphan) in adopters else None,
                       "the orphan's adoption")
    assert adopter == session.subreaper, (
        f"the orphan re-parented to {adopter}; the stand-in user manager is {session.manager.pid}")
    assert parent(session.server) == session.subreaper
    assert parent(session.subreaper) == session.manager.pid
    assert session.events() == [], "a healthy start records no event"


def test_the_outage_loop_leaves_the_user_manager_untouched(scratch, start):
    token = str(random.randrange(10**9, 10**10))
    assert OUTAGE_LOOP.count(RECORDED_PATTERN) == 2
    go, loop, targets, done = (scratch / name for name in ("go", "loop.sh", "targets", "done"))
    # The pane stands in for claude: one tool shell runs a background job whose
    # command line carries the pattern, a second runs the cleanup loop.
    session = start(f'setsid bash -c "sleep {token}; true" &\n'
                    f"while [ ! -e {go} ]; do sleep 0.05; done\n"
                    f"setsid bash {loop} > {loop}.out 2>&1 &\nwait\n")
    loop.write_text(KILL_SHIM.format(manager=session.manager.pid, log=targets)
                    + OUTAGE_LOOP.replace(RECORDED_PATTERN, token) + f"\ntouch {done}\n")
    go.touch()
    wait_for(done.exists, "the loop to finish", timeout=20)
    hit = targets.read_text().splitlines()
    assert not [line for line in hit if line.startswith("outside")], hit
    # Two matches, each killed with its parent: the tool shell and claude, then
    # the orphaned job and whatever adopted it.
    assert len(hit) == 4, hit
    assert int(hit[3]) == session.subreaper, (
        f"the loop's second parent was {hit[3]}; the stand-in user manager is {session.manager.pid}")
    # The stand-in blocks what it watches: a signal sent to it is either still
    # pending or already in its log.
    pending = status_field(session.manager.pid, "ShdPnd") | status_field(session.manager.pid, "SigPnd")
    assert session.signals() == [] and not signal_bits(pending, ("TERM",))


def test_the_subreaper_survives_what_a_stray_kill_sends(start):
    session = start("exec sleep 600\n")
    # Once it has re-executed, it is the long-lived process a stray kill reaches.
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's re-execution")
    assert signal_bits(status_field(session.subreaper, "SigIgn"), STRAY) == set(STRAY)
    for name in STRAY:
        os.kill(session.subreaper, getattr(signal, f"SIG{name}"))
    # The kernel drops a signal its target ignores when it is sent.
    assert live(session.subreaper)
    assert session.has_session()


def _dispositions(pid):
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    return [line for line in status if line.startswith(("SigBlk:", "SigIgn:"))]


def test_the_session_inherits_no_signal_disposition_from_it(start):
    kept = start("exec sleep 600\n")
    plain = start("exec sleep 600\n", os_name="Darwin")  # the client run directly
    assert kept.subreaper and plain.subreaper is None
    assert _dispositions(kept.pane()) == _dispositions(plain.pane())
    assert _dispositions(kept.server) == _dispositions(int(plain.tmux("display-message", "-p", "#{pid}").stdout))


def test_with_nothing_left_behind_it_leaves_with_the_tmux_server(start):
    session = start("exec sleep 600\n")
    assert session.subreaper, session.report_text
    session.tmux("kill-server")
    wait_for(lambda: not live(session.subreaper), "the subreaper to leave", timeout=5)


def test_it_reaps_and_waits_for_what_outlives_the_session(scratch, start):
    record = scratch / "survivor"
    session = start(f"setsid sleep 600 & echo $! > {record}\nexec sleep 600\n")
    survivor = int(wait_for(lambda: record.exists() and record.read_text().strip(), "the survivor"))
    assert session.subreaper, session.report_text
    session.tmux("kill-server")
    # The setsid'd sleep left the pane's terminal session, so the hangup that
    # ends the session misses it; it re-parents to the subreaper, which reaps
    # the rest of the session as it goes.
    wait_for(lambda: parent(survivor) == session.subreaper, "the survivor's adoption")
    wait_for(lambda: not [pid for pid, state in tree(session.subreaper) if state == "Z"],
             "the subreaper to reap what the session left")
    assert live(session.subreaper)
    os.kill(survivor, signal.SIGKILL)  # the test's own process
    wait_for(lambda: not live(session.subreaper), "the subreaper to leave after its last child")


def test_it_sleeps_while_it_waits(start):
    # The reap loop blocks in waitpid. One that polls leaves at the same
    # moments, so only its CPU time tells them apart.
    session = start("exec sleep 600\n")
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's re-execution")

    def cpu():
        fields = Path(f"/proc/{session.subreaper}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")  # utime + stime

    before = cpu()
    time.sleep(1)
    assert cpu() - before < 0.05, "the subreaper spends CPU while it has nothing to reap"


def test_it_holds_nothing_it_inherited(scratch, start):
    # The starter holds a file open on fd 8, as start-bot.sh holds its own; the
    # subreaper lets go of it before it reports. Read once it has re-executed:
    # for a few ms before that, the interpreter holds its own script open.
    session = start("exec sleep 600\n", hold=scratch / "held")
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's re-execution")
    assert sorted(os.listdir(f"/proc/{session.subreaper}/fd"), key=int) == ["0", "1", "2"]


def test_it_closes_what_it_inherited_under_an_unlimited_descriptor_limit(scratch):
    # SC_OPEN_MAX reads -1 under an unlimited soft limit; the subreaper must still
    # let go of every descriptor above its report pipe. Run in a child, so the
    # test closes nothing of its own.
    probe = textwrap.dedent(f"""
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("bot_subreaper", {str(SUBREAPER)!r})
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        report = os.dup(1)
        held = [os.open(os.devnull, os.O_RDONLY) for _ in range(4)]
        os.sysconf = lambda name: -1
        mod.close_above(report)
        def is_open(fd):
            try:
                os.fstat(fd)
                return True
            except OSError:
                return False
        print(json.dumps({{"report": report, "kept": [fd for fd in range(report + 1) if is_open(fd)],
                          "held": [fd for fd in held if is_open(fd)]}}))
    """)
    result = json.loads(subprocess.run([sys.executable, "-I", "-c", "import json\n" + probe],
                                       capture_output=True, text=True, check=True).stdout)
    assert result["held"] == [], result
    assert result["kept"] == list(range(result["report"] + 1)), result


def test_a_client_that_fails_reports_its_status_and_leaves_nothing(start):
    first = start("exec sleep 600\n")
    again = start("exec sleep 600\n", socket=first.socket)  # the same session name: tmux refuses it
    assert first.rc == 0 and again.rc != 0
    assert again.report.get("adopted") == "no", again.report_text
    assert [line.split()[0] for line in again.events()] == ["bot_subreaper_unavailable"]
    wait_for(lambda: not tree(again.manager.pid), "the failed start to leave nothing behind")


@pytest.mark.parametrize("override, why, recorded", [
    ({"os_name": "Darwin"}, "not used: Darwin has no child subreaper", []),
    ({"python": "/bin/false"}, "not used: the subreaper did not run the client",
     ["bot_subreaper_unavailable"]),
    # No interpreter, or one that cannot run: the report keeps its reason.
    ({"python": ""}, "not used: no private socket or release interpreter",
     ["bot_subreaper_unavailable"]),
    ({"python": "/nonexistent/python3"}, "not used: no private socket or release interpreter",
     ["bot_subreaper_unavailable"]),
    # No socket: the plain path on tmux's default socket, never `tmux -L ''`.
    ({"socket": ""}, "not used: no private socket or release interpreter",
     ["bot_subreaper_unavailable"]),
])
def test_without_a_subreaper_the_session_starts_as_before(start, override, why, recorded):
    session = start("exec sleep 600\n", **override)
    assert session.rc == 0 and session.has_session()
    assert session.report_text.startswith(why), session.report_text
    assert session.subreaper is None
    assert [line.split()[0] for line in session.events()] == recorded


def test_a_subreaper_that_dies_after_its_client_ran_never_starts_the_session_twice(scratch, start):
    # An interpreter that runs the client, then dies before any report.
    dies = scratch / "dies-after-the-client"
    dies.write_text('#!/bin/bash\nshift 4\n"$@" >/dev/null\nexit 1\n')
    dies.chmod(0o755)
    session = start("exec sleep 600\n", python=str(dies))
    assert session.rc == 0 and session.has_session()
    assert session.report_text.startswith("not used: the subreaper did not run the client")
    assert [line.split()[0] for line in session.events()] == ["bot_subreaper_unavailable"]


def test_an_interpreter_that_prints_something_else_is_not_a_report(scratch, start):
    # Output that is not the subreaper's report must not count as one: the
    # client never ran, so the session starts on the plain path.
    garbage = scratch / "prints-garbage"
    garbage.write_text("#!/bin/bash\necho garbage\nexit 0\n")
    garbage.chmod(0o755)
    session = start("exec sleep 600\n", python=str(garbage))
    assert session.rc == 0 and session.has_session()
    assert session.report_text.startswith("not used: the subreaper did not run the client")
    assert [line.split()[0] for line in session.events()] == ["bot_subreaper_unavailable"]


def test_a_tmux_that_cannot_run_fails_the_start(start):
    # The client's own status stands: a start whose tmux never ran is not a success.
    session = start("exec sleep 600\n", tmux_bin="/nonexistent/tmux")
    assert session.rc == 127, session.report_text
    assert session.report.get("adopted") == "no", session.report_text
    assert [line.split()[0] for line in session.events()] == ["bot_subreaper_unavailable"]


def test_a_session_added_to_a_server_outside_the_subreaper_is_not_adopted(scratch, start):
    # A server already running on the socket, started without the subreaper:
    # the client adds the session to it, and its parent is not the subreaper.
    socket = f"sr{random.randrange(10**6)}"
    env = constructed_env(TMUX_TMPDIR=scratch / "s", CLAUDLOBBY_ROOT=scratch / "root")
    subprocess.run([TMUX, "-L", socket, "new-session", "-d", "-s", "other", "sleep 600"],
                   env=env, check=True)
    session = start("exec sleep 600\n", socket=socket)
    assert session.has_session()
    assert session.report.get("adopted") == "no", session.report_text
    assert session.server and parent(session.server) != session.subreaper
    assert [line.split()[0] for line in session.events()] == ["bot_subreaper_unavailable"]


def test_it_names_itself_as_the_browser_reaper_expects(start):
    session = start("exec sleep 600\n")
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's name")
    name = re.search(r'^NAME = b"([^"]+)"', SUBREAPER.read_text(), re.M).group(1)
    parents = re.search(r"^DEFAULT_ORPHAN_PARENTS='([^']+)'", REAPER.read_text(), re.M).group(1)
    assert comm(session.subreaper) == name and re.fullmatch(parents, name)
    argv = Path(f"/proc/{session.subreaper}/cmdline").read_bytes().split(b"\0")
    assert b"reap" in argv
    assert not [arg for arg in argv if b"pane-" in arg or b"new-session" in arg], argv


def test_it_never_sends_a_signal():
    """No route to a signal at all: not a call by name, not one reached through
    getattr, an import alias or an assignment (each names the function as an
    attribute, an alias or a string), and libc only for prctl."""
    module = ast.parse(SUBREAPER.read_text())
    named = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.alias):
            named.update({node.name, node.asname or ""})
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            named.add(node.value)
    signallers = {"kill", "killpg", "raise_signal", "pidfd_send_signal", "pthread_kill",
                  "sigqueue", "tgkill", "tkill"}
    assert not named & signallers, named & signallers
    libc = [node.attr for node in ast.walk(module) if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Call) and getattr(node.value.func, "attr", "") == "CDLL"]
    assert libc == ["prctl"], libc


# A subreaper lost mid-session (#2184): what fleet-pulse.sh reads for each live
# session, and records as bot_subreaper_missing.

def lost(socket, env, os_name=""):
    """bot_subreaper_lost on one socket, as the pulse calls it."""
    script = (f". {shlex.quote(str(LIB))} >/dev/null 2>&1\nset +e\n"
              + (f"_OS={os_name}\n" if os_name else "")
              + 'bot_subreaper_lost "$1"\n')
    done = subprocess.run(["bash", "-c", script, "_", socket], env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_a_session_under_its_subreaper_reads_as_kept(start):
    session = start("exec sleep 600\n")
    assert session.report.get("adopted") == "yes", session.report_text
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's name")
    assert lost(session.socket, session.env) == ""


def test_a_session_whose_subreaper_died_reads_as_lost(start):
    session = start("exec sleep 600\n")
    wait_for(lambda: comm(session.subreaper) == "bot-subreaper", "the subreaper's name")
    os.kill(session.subreaper, signal.SIGKILL)  # the one signal it cannot ignore
    wait_for(lambda: parent(session.server) == session.manager.pid, "the server's re-adoption")
    found = json.loads(lost(session.socket, session.env))
    assert found == {"server": session.server, "parent": comm(session.manager.pid)}
    # Off Linux there is no subreaper to lose, so no verdict.
    assert lost(session.socket, session.env, os_name="Darwin") == ""


def test_a_session_started_without_its_subreaper_reads_as_lost(start):
    session = start("exec sleep 600\n", python="/bin/false")
    server = int(session.tmux("display-message", "-p", "#{pid}").stdout)
    assert parent(server) == session.manager.pid
    found = json.loads(lost(session.socket, session.env))
    assert found == {"server": server, "parent": comm(session.manager.pid)}


def test_with_no_server_on_the_socket_there_is_no_verdict(scratch):
    env = constructed_env(TMUX_TMPDIR=scratch / "s", CLAUDLOBBY_ROOT=scratch / "root")
    assert lost(f"sr{random.randrange(10**6)}", env) == ""


def test_the_pulse_records_a_live_session_that_lost_its_subreaper():
    src = (LIB.parent / "fleet-pulse.sh").read_text()
    check = src[src.index("# --- Check 2c"):src.index("# --- Check 3")]
    assert 'if [ "$_session_alive" -eq 1 ]; then' in check
    assert '_subreaper_lost=$(bot_subreaper_lost "$_bot_socket")' in check
    assert 'emit_fleet_event "bot_subreaper_missing" "pulse" "$_subreaper_lost" "$bot_dir" "$bot_id"' in check
