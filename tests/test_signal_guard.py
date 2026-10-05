"""The signal guard (#1069): refuse a Bash command that signals a process the
caller did not start.

Every bot on a host runs as one user, so a pid read back from a process lookup
can belong to any bot, and an orphaned job's parent is the user manager that
runs them all. On 2026-10-05 a cleanup loop killed a pattern match and its
parent by pid and stopped every bot on a host for 15 hours (#2158). OUTAGE_LOOP
is that command with its names neutralised. It killed by pid, as #1069's
interim fix asked, so a guard keyed on the verb would have passed it: this one
keys on where each pid came from, and allows only the caller's own handles.

The REFUSED and ALLOWED tables run through the decider in-process; the tests
after them run the hook itself, for its prefilter, its deny, its fail-open
paths and its plane record.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.conftest import constructed_env, read_fleet_events
from tests.test_credential_echo_guard import _decision
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "claudlobby/_runtime_scripts" / "signal-guard.sh"


def _decider():
    spec = importlib.util.spec_from_file_location(
        "signal_decide", GUARD.with_name("signal-decide.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


D = _decider()


def _run(
    command: str,
    env: dict | None = None,
    tool: str = "Bash",
    guard: Path = GUARD,
    raw: str | None = None,
) -> subprocess.CompletedProcess:
    # The keys of a live PreToolUse payload (as in test_credential_echo_guard), identifiers faked.
    payload = {
        "session_id": "s",
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
        "permission_mode": "auto",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
        "tool_use_id": "toolu_x",
        "prompt_id": "p",
    }
    return subprocess.run(
        ["bash", str(guard)],
        input=json.dumps(payload) if raw is None else raw,
        capture_output=True,
        text=True,
        env=env or constructed_env(),
        timeout=60,
    )


def _refusal(command: str, **kw) -> str:
    """Run the hook and return its reason, asserting that it refused."""
    verdict = _decision(_run(command, **kw))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)
    return verdict[1]


# The 2026-10-05 01:30:24Z command, structure verbatim, names neutralised.
OUTAGE_LOOP = (
    'ps -eo pid,ppid,args | grep -E "notify-when-idle.sh worker .*msg-stale" | grep -v grep'
    " | cut -c1-120; for p in $(ps -eo pid,args | awk '/notify-when-idle.sh worker .*msg-stale/"
    " && !/awk/ {print $1}'); do PP=$(ps -o ppid= -p $p | tr -d ' '); kill $p $PP 2>/dev/null"
    ' && echo "killed $p (+ outer $PP)"; done; sleep 1; ps -eo pid,args | grep -c "[m]sg-stale"'
)

REFUSED = [
    # by name or pattern: the #1069 instances, the 2026-10-03 one among them
    "pkill -f 'sleep 60'",
    "pkill -x -f 'sleep 60'",
    "pkill sleep",
    "/usr/bin/pkill -f watcher",
    "pk'ill' -f watcher",
    "p$'\\x6b'ill -f watcher",  # a name spelled in an ANSI-C escape
    "killall node",
    "killall5 -9",
    "skill watcher",
    "fuser -k 8080/tcp",
    "fuser -km /mnt/data",
    "/bin/kill watcher",  # util-linux kill takes a name
    "find /proc -maxdepth 1 -name '[0-9]*' -exec kill {} +",
    # a pid read back from a lookup or $PPID, however it arrives
    "kill $PPID",
    'kill -9 "${PPID}"',
    "pp=$PPID; kill $pp",
    "kill $(pgrep -f watcher)",
    "kill `pgrep watcher`",
    "kill $(ps -o ppid= -p 4242)",
    "kill $(awk '{print $4}' /proc/4242/stat)",
    "kill -9 $(lsof -t -i :8080)",
    "pid=$(pgrep -f watcher); kill $pid",
    "p=$(pidof watcher) && kill -TERM $p",
    "export P=$(pgrep watcher); kill $P",
    "a=$(pgrep watcher); b=$a; kill $b",
    "pids=($(pgrep watcher)); kill ${pids[@]}",
    "for p in $(pgrep watcher); do kill $p; done",
    "for d in /proc/[0-9]*; do grep -q watcher $d/cmdline && kill ${d#/proc/}; done",
    "pgrep watcher | while read p; do kill $p; done",
    'while read p; do kill "$p"; done < <(pgrep watcher)',
    "mapfile -t pids < <(pgrep watcher); kill ${pids[@]}",
    "cat <<'EOF' | bash | while read p; do kill $p; done\npgrep watcher\nEOF",
    "kill -s $SIG $(pgrep watcher)",  # a signal it cannot read still sends
    # a pid from a source the guard does not know as the caller's own
    "kill $(busybox pgrep watcher)",
    "kill $(cat /sys/fs/cgroup/app/cgroup.procs)",
    "kill $(jq -r .pid state.json)",  # by design: read a pid file with cat
    "kill $SSH_AGENT_PID",  # each Bash call starts a fresh shell: not set here
    # xargs fed by a lookup
    "pgrep -f watcher | xargs -r kill -9",
    "pgrep watcher | xargs -I{} kill {}",
    "pgrep watcher | xargs --replace kill {}",
    "pgrep watcher | xargs -rI {} kill {}",
    "ps aux | grep watcher | awk '{print $2}' | xargs kill",
    "lsof -ti :8080 | xargs kill",
    "pgrep watcher | xargs -n1 sh -c 'kill $0'",
    "xargs kill < /proc/4242/task/4242/children",
    # every process, PID 1, through a variable too
    "kill -9 -1",
    "kill -- -1",
    "kill -s KILL -1",
    "s=-1; kill -9 $s",
    "kill 1",
    # where a command can stand
    "ls && pkill watcher",
    "(pkill watcher)",
    "echo done\npkill watcher",
    "if pgrep watcher >/dev/null; then pkill watcher; fi",
    "sudo kill $(pidof watcher)",
    "sudo -nu root kill $(pgrep watcher)",
    "nohup kill $(pgrep watcher) &",
    "time -p kill $(pgrep watcher)",
    "timeout 5 kill $(pgrep watcher)",
    "watch -n 5 'pkill -f watcher'",
    "su -c 'pkill -f watcher'",
    "flock /tmp/lock -c 'pkill -f watcher'",
    "bash -c 'kill $(pgrep watcher)'",
    "bash -lc 'pkill -f watcher'",
    "bash -o pipefail -c 'pkill -f watcher'",
    "bash -c $'pkill -f watcher'",
    "trap 'pkill -f watcher' EXIT; sleep 1",
    "eval 'pkill watcher'",
    "eval $'kill $(pgrep watcher)'",
    'echo "$(pkill -f watcher)"',
    'echo "$(case x in x) pkill -f watcher;; esac)"',  # a case pattern's parenthesis
    "bash <<'EOF'\npkill -f watcher\nEOF",  # a heredoc fed to a shell is commands
    "cat <<'EOF' | bash\npkill -f watcher\nEOF",
    "cat <<EOF\n$(pkill -f watcher)\nEOF",  # an unquoted heredoc runs its substitutions
]

ALLOWED = [
    # what the caller started
    "sleep 100 & kill $!",
    "sleep 100 & pid=$!; sleep 1; kill $pid",
    "sleep 100 & pid=$!; ps -p $pid >/dev/null && kill $pid",  # a lookup that feeds no kill
    "sleep 100 & kill %1",
    "sleep 100 & kill %%",
    "sleep 100 & sig=1; kill -$sig $!",  # -$sig is a signal, not group 1
    "sleep 100 & echo $! | xargs kill",
    "setsid job.sh & kill -- -$!",
    "kill -- -$$",
    "kill 0",  # the tool shell leads its own process group
    "kill $(jobs -p)",
    # a pid file
    "kill $(cat /tmp/job.pid)",
    "kill $(< job.pid)",
    "kill $(cat job.pid | tr -d ' ')",
    'kill -TERM -- -"$(cat job.pgid)"',
    'kill "$(cat /tmp/job.pid)"; sleep 1; ps aux | grep watcher',
    "for p in $(cat pids.txt); do kill $p; done",
    "while read p; do kill $p; done < pids.txt",
    "cat job.pid | xargs kill",
    "cat job.pid | xargs -I PID kill PID",  # a named placeholder is not a process name
    # sends nothing, or runs nothing
    "kill -0 $pid",
    "kill -0 $(pgrep watcher) && echo alive",
    "kill -s 0 4242",
    "kill -l",
    "kill -l 15",
    "command -v pkill",
    "command -V killall",
    "ps aux | grep watcher; pgrep -a watcher",
    "until ! pgrep -f '[b]in/pytest' >/dev/null; do sleep 30; done",
    "timeout 5 sleep 10",
    # text that only mentions a kill
    "grep -rn 'pkill -f' lib/",
    "git commit -m 'refuse pkill -f and kill $PPID'",
    "echo 'kill $(pgrep watcher)' > fixture.txt",
    "cat > notes.md <<'EOF'\nNever run pkill -f or kill $(pgrep x).\nEOF",
    "gh pr comment 1 --body \"$(cat <<'EOF'\nIt's about pkill -f (and kill $PPID\nEOF\n)\"",
    "ls ~/.claude/skills",
]


@pytest.mark.parametrize("command", REFUSED)
def test_a_signal_to_a_process_the_caller_did_not_start_is_refused(command):
    assert D.decide(command), command


@pytest.mark.parametrize("command", ALLOWED)
def test_what_the_caller_started_and_text_that_mentions_a_kill_are_allowed(command):
    assert D.decide(command) == [], command


def test_a_typed_pid_of_an_ancestor_is_refused_however_it_is_held():
    # This test process is an ancestor of the decider's own process, as a bot's
    # claude, its tmux server and the user manager are of a bot's hook.
    me, group = os.getpid(), os.getpgid(0)
    for command in (
        f"kill -9 {me}",
        f"kill -- -{group}",
        f"PP={me}; kill $PP",
        f"for p in 4242 {me}; do kill $p; done",
    ):
        found = D.decide(command)
        assert [kind for kind, _ in found] == ["ancestor"], (command, found)


def test_a_typed_pid_that_is_not_an_ancestor_is_allowed():
    # A typed number shows no provenance; only its target can condemn it. This
    # is the stated bound: another bot's pid, typed, passes (#2158 backstops it).
    child = subprocess.Popen(["sleep", "30"])
    try:
        for command in (f"kill {child.pid}", f"PP={child.pid}; kill $PP"):
            assert D.decide(command) == [], command
    finally:
        child.kill()
        child.wait()


def test_the_hook_refuses_the_outage_loop_for_both_of_its_pids():
    reason = _refusal(OUTAGE_LOOP)
    assert "`$p`" in reason and "`$PP`" in reason, reason


@pytest.mark.parametrize("command", ["pk'ill' -f watcher", "p$'\\x6b'ill -f watcher"])
def test_the_hook_prefilter_sees_a_name_split_by_quoting_or_spelled_in_escapes(command):
    _refusal(command)


def test_the_hook_refuses_a_typed_pid_of_its_own_ancestor():
    assert "ancestor" in _refusal(f"kill -9 {os.getpid()}")


@pytest.mark.parametrize("command", ["sleep 100 & kill $!", "ls ~/.claude/skills"])
def test_the_hook_allows_what_the_caller_started_and_a_path_that_names_skills(command):
    assert _decision(_run(command)) is None, command


def test_the_refusal_names_the_safe_pattern_and_the_guardrail():
    reason = _refusal("pkill -f watcher")
    for needle in ("$!", "kill %1", "kill -0", "signal-only-what-you-started"):
        assert needle in reason, (needle, reason)


def test_another_tool_is_untouched():
    assert _decision(_run("pkill -f watcher", tool="Read")) is None


def test_a_malformed_payload_fails_open():
    p = _run("", raw="Bash pkill {not json")
    assert p.returncode == 0 and _decision(p) is None


def test_a_missing_decider_fails_open(tmp_path):
    lone = tmp_path / "signal-guard.sh"
    shutil.copy2(GUARD, lone)
    p = _run("pkill -f watcher", guard=lone)
    assert p.returncode == 0 and _decision(p) is None, p.stderr


def test_the_guard_event_is_registered_as_a_notice():
    # Unregistered, it would land on the plane with no severity.
    assert SYSTEM_EVENT_SEVERITY.get("signal_guard_refused") == "notice"


def test_the_guard_is_composed_into_every_bot_on_bash():
    hooks = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())[
        "defaults"
    ]["hooks"]
    entries = [
        e for e in hooks["PreToolUse"] if e["command"].endswith("/signal-guard.sh")
    ]
    assert entries == [
        {"command": "$CLAUDLOBBY_NATIVE_DIR/signal-guard.sh", "matcher": "Bash"}
    ]


def test_a_refusal_is_recorded_with_its_kinds_and_never_the_command(
    tmp_path, scratch_plane_env
):
    root = tmp_path / "root"
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    env = constructed_env(
        HOME=tmp_path / "home",
        FLEET_NAME="testfleet",
        BOT_ID="tbot",
        BOT_DIR=bot,
        **scratch_plane_env(root, initialize=True),
    )
    with _serving(root, scratch_plane_env) as socket:
        _refusal(
            "pkill -f CANARY_watcher_x7q2", env={**env, "PLANE_SOCKET": str(socket)}
        )
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    refused = [r for r in rows if r["type"] == "signal_guard_refused"]
    assert len(refused) == 1, rows
    assert refused[0]["source"] == "signal-guard", refused[0]
    assert refused[0]["data"] == {"kinds": ["selector"]}, refused[0]
    assert "CANARY_watcher_x7q2" not in json.dumps(rows)
