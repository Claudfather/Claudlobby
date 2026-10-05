"""The signal guard (#1069): refuse a Bash command that signals a process the
caller did not start.

Every bot on a host runs as one user, so a pid read back from a process lookup
can belong to any bot, and an orphaned job's parent is the user manager that
runs them all. On 2026-10-05 a cleanup loop killed a pattern match and its
parent by pid and stopped every bot on a host for 15 hours (#2158). OUTAGE_LOOP
is that command with its names neutralised. It killed by pid, as #1069's
interim fix asked, so a guard keyed on the verb would have passed it: this one
keys on where each pid came from. Every form in REFUSED is refused; the forms
in ALLOWED (own children, job specs, pid files, liveness checks, and text that
only mentions a kill) pass untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.conftest import constructed_env, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "claudlobby/_runtime_scripts" / "signal-guard.sh"


def _run(
    command: str, env: dict | None = None, tool: str = "Bash", guard: Path = GUARD
):
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
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env or constructed_env(),
        timeout=60,
    )


def _decision(p: subprocess.CompletedProcess):
    if not p.stdout.strip():
        return None
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return out.get("permissionDecision"), out.get("permissionDecisionReason", "")


# The 2026-10-05 01:30:24Z command, structure verbatim, names neutralised.
OUTAGE_LOOP = (
    'ps -eo pid,ppid,args | grep -E "notify-when-idle.sh worker .*msg-stale" | grep -v grep'
    " | cut -c1-120; for p in $(ps -eo pid,args | awk '/notify-when-idle.sh worker .*msg-stale/"
    " && !/awk/ {print $1}'); do PP=$(ps -o ppid= -p $p | tr -d ' '); kill $p $PP 2>/dev/null"
    ' && echo "killed $p (+ outer $PP)"; done; sleep 1; ps -eo pid,args | grep -c "[m]sg-stale"'
)

REFUSED = [
    OUTAGE_LOOP,
    # by name or pattern: the #1069 instances, the 2026-10-03 one among them
    "pkill -f 'sleep 60'",
    "pkill -x -f 'sleep 60'",
    "pkill sleep",
    "/usr/bin/pkill -f watcher",
    "pk'ill' -f watcher",
    "killall node",
    "killall5 -9",
    "skill watcher",
    "fuser -k 8080/tcp",
    "fuser -km /mnt/data",
    "/bin/kill watcher",  # util-linux kill takes a name
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
    "pgrep watcher | while read p; do kill $p; done",
    'while read p; do kill "$p"; done < <(pgrep watcher)',
    "mapfile -t pids < <(pgrep watcher); kill ${pids[@]}",
    "kill -s $SIG $(pgrep watcher)",  # a signal it cannot read still sends
    # xargs fed by a lookup
    "pgrep -f watcher | xargs -r kill -9",
    "pgrep watcher | xargs -I{} kill {}",
    "ps aux | grep watcher | awk '{print $2}' | xargs kill",
    "lsof -ti :8080 | xargs kill",
    "pgrep watcher | xargs -n1 sh -c 'kill $0'",
    # every process, PID 1
    "kill -9 -1",
    "kill -- -1",
    "kill -s KILL -1",
    "kill 1",
    # where a command can stand
    "ls && pkill watcher",
    "(pkill watcher)",
    "echo done\npkill watcher",
    "if pgrep watcher >/dev/null; then pkill watcher; fi",
    "sudo kill $(pidof watcher)",
    "nohup kill $(pgrep watcher) &",
    "time -p kill $(pgrep watcher)",
    "timeout 5 kill $(pgrep watcher)",
    "bash -c 'kill $(pgrep watcher)'",
    "bash -lc 'pkill -f watcher'",
    "trap 'pkill -f watcher' EXIT; sleep 1",
    "eval 'pkill watcher'",
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
    "setsid job.sh & kill -- -$!",
    "kill -- -$$",
    "kill 0",  # the tool shell leads its own process group
    "kill $(jobs -p)",
    # a pid file
    "kill $(cat /tmp/job.pid)",
    'kill "$(cat /tmp/job.pid)"; sleep 1; ps aux | grep watcher',
    "for p in $(cat pids.txt); do kill $p; done",
    "cat job.pid | xargs kill",
    # sends nothing
    "kill -0 $pid",
    "kill -0 $(pgrep watcher) && echo alive",
    "kill -s 0 4242",
    "kill -l",
    "kill -l 15",
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
    verdict = _decision(_run(command))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)


@pytest.mark.parametrize("command", ALLOWED)
def test_what_the_caller_started_and_text_that_mentions_a_kill_are_allowed(command):
    assert _decision(_run(command)) is None, command


def test_the_outage_loop_is_refused_for_both_of_its_pids():
    verdict = _decision(_run(OUTAGE_LOOP))
    assert verdict is not None and verdict[0] == "deny"
    assert "`$p`" in verdict[1] and "`$PP`" in verdict[1], verdict[1]


def test_a_typed_pid_or_group_of_an_ancestor_is_refused():
    # This test process is an ancestor of the hook it starts, as a bot's claude,
    # its tmux server and the user manager are of a bot's hook.
    for command in (f"kill -9 {os.getpid()}", f"kill -- -{os.getpgid(0)}"):
        verdict = _decision(_run(command))
        assert verdict is not None and verdict[0] == "deny", (command, verdict)
        assert "ancestor" in verdict[1], verdict[1]


def test_a_typed_pid_that_is_not_an_ancestor_is_allowed():
    # A typed number shows no provenance; only its target can condemn it. This
    # is the stated bound: another bot's pid, typed, passes (#2158 backstops it).
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert _decision(_run(f"kill {child.pid}")) is None
    finally:
        child.kill()
        child.wait()


def test_the_refusal_names_the_safe_pattern_and_the_guardrail():
    verdict = _decision(_run("pkill -f watcher"))
    assert verdict is not None and verdict[0] == "deny"
    for needle in ("$!", "kill %1", "kill -0", "signal-only-what-you-started"):
        assert needle in verdict[1], (needle, verdict[1])


def test_another_tool_is_untouched():
    assert _decision(_run("pkill -f watcher", tool="Read")) is None


def test_a_malformed_payload_fails_open():
    p = subprocess.run(
        ["bash", str(GUARD)],
        input="Bash pkill {not json",
        capture_output=True,
        text=True,
        env=constructed_env(),
        timeout=60,
    )
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
        p = _run(
            "pkill -f CANARY_watcher_x7q2", env={**env, "PLANE_SOCKET": str(socket)}
        )
    assert _decision(p) is not None and _decision(p)[0] == "deny", p.stderr
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    refused = [r for r in rows if r["type"] == "signal_guard_refused"]
    assert len(refused) == 1, (rows, p.stderr)
    assert refused[0]["source"] == "signal-guard", refused[0]
    assert refused[0]["data"] == {"kinds": ["selector"]}, refused[0]
    assert "CANARY_watcher_x7q2" not in json.dumps(rows)
