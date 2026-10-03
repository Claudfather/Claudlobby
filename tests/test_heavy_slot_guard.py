"""The PreToolUse hook for the heavy-job slot (#1686), through the real script.

The hook is composed only for a bot that opted in (`heavy_slot: true`). For
such a bot it reads each Bash tool call and either leaves it alone (no output),
puts the slot wrapper in front of each heavy command (`updatedInput`, every
other tool_input field kept), or, when every slot is taken, refuses the call
before anything runs, naming the holder. It fails open: a hook that cannot
decide lets the call through.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "claudlobby/_runtime_scripts" / "heavy-slot-guard.sh"
WRAPPER = REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py"
W = shlex.quote(str(WRAPPER.resolve())) + " run --"
BASH = shutil.which("bash")

STUB = """#!/bin/bash
touch "$STUB_DIR/ran.$$"
if [ -n "${STUB_WAIT:-}" ]; then
  while [ ! -e "$STUB_WAIT" ]; do sleep 0.05; done
fi
exit 0
"""


@pytest.fixture()
def se(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pytest"
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = constructed_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HEAVY_SLOT_DIR=tmp_path / "state",
        HEAVY_SLOT_EVENTS_FILE=tmp_path / "events.jsonl",
        FLEET_NAME="testfleet",
        BOT_ID="alpha",
        STUB_DIR=tmp_path,
        PLANE_EMIT_DISABLED="1",
        TZ="UTC",
    )
    yield SimpleNamespace(env=env, tmp=tmp_path, state=tmp_path / "state")
    (tmp_path / "release").touch()


def _hook(se, command: str, tool: str = "Bash", env: dict | None = None, **extra):
    # The keys of a live PreToolUse payload (claude 2.1.281, captured on #1686),
    # identifiers faked.
    payload = {
        "session_id": "s",
        "transcript_path": "/dev/null",
        "cwd": str(se.tmp),
        "permission_mode": "auto",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command, **extra},
        "tool_use_id": "toolu_x",
        "prompt_id": "p",
    }
    p = subprocess.run(
        [BASH, str(GUARD)],
        input=json.dumps(payload),
        env=env or se.env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


def _events(se) -> list[dict]:
    f = se.tmp / "events.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def _hold(se) -> subprocess.Popen:
    """testfleet/alpha holding the slot: its stub job started, waiting on `release`."""
    holder = subprocess.Popen(
        [str(WRAPPER), "run", "--", "pytest", "-q"],
        env={**se.env, "STUB_WAIT": str(se.tmp / "release")},
    )
    deadline = time.monotonic() + 15
    while not list(se.tmp.glob("ran.*")):
        assert time.monotonic() < deadline, "the holder never started"
        time.sleep(0.02)
    return holder


def _rewritten(out: str) -> str:
    """The command a let-through decision runs."""
    return json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]


def test_a_heavy_command_gets_the_wrapper_and_keeps_the_rest_of_its_input(se):
    rc, out, _ = _hook(
        se,
        "cd x && npm ci",
        description="install",
        timeout=600000,
        run_in_background=True,
    )
    assert rc == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    # No permission decision: the rewritten call still goes through the
    # permission layer exactly as any other call does.
    assert "permissionDecision" not in decision
    assert decision["updatedInput"] == {
        "command": f"cd x && {W} npm ci",
        "description": "install",
        "timeout": 600000,
        "run_in_background": True,
    }


@pytest.mark.parametrize(
    "command,extra",
    [
        ("ls -la", {}),
        ("grep -rn pytest .", {}),
        ("ls", {"description": "before the pytest run"}),
        ("pytest tests/test_x.py", {}),
    ],
)
def test_anything_else_passes_untouched(se, command, extra):
    assert _hook(se, command, **extra)[:2] == (0, "")


def test_another_tool_passes_untouched(se):
    assert _hook(se, "npm ci", tool="Read")[:2] == (0, "")


def test_a_taken_slot_refuses_the_call_before_anything_runs(se):
    holder = _hold(se)
    rc, out, _ = _hook(se, "cd app && npm ci")
    assert rc == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "NOT RUN" in reason and "(1 of 1)" in reason
    assert "testfleet/alpha" in reason and "pytest" in reason
    assert "pytest -q" not in reason  # the refusal names the tool, never the command
    refused = [e for e in _events(se) if e["type"] == "heavy_slot_refused"]
    assert len(refused) == 1 and refused[0]["data"]["where"] == "hook"
    (se.tmp / "release").touch()
    holder.wait(15)


def test_the_off_file_lets_everything_through(se):
    se.state.mkdir(parents=True)
    (se.state / "disabled").touch()
    assert _hook(se, "npm ci")[:2] == (0, "")


def test_an_unparsed_construct_passes_untouched_and_is_counted(se):
    assert _hook(se, "eval 'npm ci'")[:2] == (0, "")
    assert [e["type"] for e in _events(se)] == ["heavy_slot_unparsed"]


def test_without_python_the_hook_fails_open(se, tmp_path):
    # `cat` stays reachable, so the hook reads its payload and gets as far as
    # looking for python3: an empty PATH would pass this test by never
    # reading the payload at all.
    only_cat = tmp_path / "only-cat"
    only_cat.mkdir()
    (only_cat / "cat").symlink_to(shutil.which("cat"))
    rc, out, _ = _hook(se, "npm ci", env={**se.env, "PATH": str(only_cat)})
    assert (rc, out) == (0, "")


def test_the_rewritten_command_runs_the_job_under_the_slot(se):
    _, out, _ = _hook(se, "pytest -q")
    p = subprocess.run(
        [BASH, "-c", _rewritten(out)], env=se.env, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0 and list(se.tmp.glob("ran.*"))
    record = json.loads((se.state / "slot-0.lock").read_text())
    assert record["shape"] == "pytest -q" and record["state"] == "released"


def test_the_slot_lives_under_the_data_root_and_the_wrapper_is_the_native_code(se):
    # A sealed release is code only: the wrapper the hook inserts is this
    # native file, and the slot it takes is host state under CLAUDLOBBY_ROOT.
    root = se.tmp / "data-root"
    env = {k: v for k, v in se.env.items() if k != "HEAVY_SLOT_DIR"}
    env["CLAUDLOBBY_ROOT"] = str(root)
    _, out, _ = _hook(se, "pytest -q", env=env)
    command = _rewritten(out)
    assert command == f"{W} pytest -q"
    p = subprocess.run(
        [BASH, "-c", command], env=env, capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0, p.stderr
    record = json.loads((root / "state" / "heavy-slot" / "slot-0.lock").read_text())
    assert record["shape"] == "pytest -q" and record["state"] == "released"
    assert not (WRAPPER.parent.parent / "state" / "heavy-slot").exists()


def test_without_a_data_root_the_hook_fails_open_and_writes_no_slot(se):
    # No HEAVY_SLOT_DIR and no absolute CLAUDLOBBY_ROOT: never fall back to a
    # slot beside the code; let the call through untouched.
    env = {k: v for k, v in se.env.items()
           if k not in ("HEAVY_SLOT_DIR", "CLAUDLOBBY_ROOT")}
    assert _hook(se, "npm ci", env=env)[:2] == (0, "")
    # A relative root is no root either (status only reads, so nothing lands).
    p = subprocess.run([str(WRAPPER), "status"],
                       env={**env, "CLAUDLOBBY_ROOT": "relative/root"},
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "CLAUDLOBBY_ROOT" in p.stderr
    assert not (WRAPPER.parent.parent / "state" / "heavy-slot").exists()


def test_a_free_slot_that_is_another_callers_turn_refuses_the_call(se):
    # #2124: beta was refused while the slot was held, so the free slot is its
    # turn. The hook refuses alpha, naming that turn, and queues alpha behind
    # it; once beta has had its turn the hook lets alpha through.
    holder = _hold(se)
    beta = {**se.env, "BOT_ID": "beta"}

    def run_beta():
        return subprocess.run([str(WRAPPER), "run", "--", "pytest"], env=beta,
                              capture_output=True, text=True, timeout=60).returncode

    assert run_beta() == 75
    (se.tmp / "release").touch()
    holder.wait(15)
    rc, out, _ = _hook(se, "pytest -q")
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "another caller's turn" in reason and "testfleet/beta, waiting since" in reason
    assert "place 2 of 2" in reason
    (ev,) = [e["data"] for e in _events(se) if e["type"] == "heavy_slot_refused"
             and e["data"]["where"] == "hook"]
    assert (ev["ticket"]["bot"], ev["ticket"]["place"], ev["ticket"]["tool"]) == ("alpha", 2, "pytest")
    assert run_beta() == 0
    _, out, _ = _hook(se, "pytest -q")
    p = subprocess.run([BASH, "-c", _rewritten(out)], env=se.env, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, p.stderr
