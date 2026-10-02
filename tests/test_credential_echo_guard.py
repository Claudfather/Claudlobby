"""The credential-echo guard (#2090): refuse a CLI form that prints an
env-held credential into the transcript, unless the variable is removed in
the same command.

`neonctl --help` printed a real NEON_API_KEY into a session transcript: the
CLI shows the variable as the default of `--api-key`. The matrix below is the
evidence table itself (fixtures/credential_echo/registry-rows.json, the
canary probe kit's table A as data): every row that echoes is refused as
written, every `unset` row passes once its variables are removed in the same
command, and the controls, which never echo, pass untouched.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.conftest import constructed_env, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "claudlobby/_runtime_scripts" / "credential-echo-guard.sh"
TABLE = json.loads((REPO / "tests/fixtures/credential_echo/registry-rows.json").read_text())


def _run(command: str, env: dict | None = None, tool: str = "Bash") -> subprocess.CompletedProcess:
    # The keys of a live PreToolUse payload (as in test_heavy_slot_guard), identifiers faked.
    payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": "/tmp",
               "permission_mode": "auto", "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command}, "tool_use_id": "toolu_x", "prompt_id": "p"}
    return subprocess.run(["bash", str(GUARD)], input=json.dumps(payload), capture_output=True,
                          text=True, env=env or constructed_env(), timeout=60)


def _decision(p: subprocess.CompletedProcess):
    if not p.stdout.strip():
        return None
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return out.get("permissionDecision"), out.get("permissionDecisionReason", "")


def _as_written(row: dict) -> str:
    assignments = " ".join(f"{k}={shlex.quote(v)}" for k, v in row.get("env", {}).items())
    command = shlex.join(row["argv"])
    return f"{assignments} {command}" if assignments else command


GUARDED = [r for r in TABLE["rows"] if r["action"] != "optional"]
UNSET = [r for r in TABLE["rows"] if r["action"] == "unset"]
UNTOUCHED = TABLE["controls"] + [r for r in TABLE["rows"] if r["action"] == "optional"]


@pytest.mark.parametrize("row", GUARDED, ids=lambda r: r["id"])
def test_every_echoing_row_is_refused_as_written(row):
    verdict = _decision(_run(_as_written(row)))
    assert verdict is not None and verdict[0] == "deny", (row["id"], verdict)


@pytest.mark.parametrize("neutralise", ["env -u", "env -i", "empty assignment", "unset first"])
@pytest.mark.parametrize("row", UNSET, ids=lambda r: r["id"])
def test_an_unset_row_passes_once_its_variables_are_removed(row, neutralise):
    command, names = shlex.join(row["argv"]), row["vars"]
    if neutralise == "env -u":
        command = "env " + " ".join(f"-u {n}" for n in names) + " " + command
    elif neutralise == "env -i":
        command = 'env -i PATH="$PATH" HOME="$HOME" ' + command
    elif neutralise == "empty assignment":
        command = " ".join(f"{n}=" for n in names) + " " + command
    else:
        command = "unset " + " ".join(names) + "; " + command
    assert _decision(_run(command)) is None, command


@pytest.mark.parametrize("row", UNTOUCHED, ids=lambda r: r["id"])
def test_a_form_that_does_not_echo_is_untouched(row):
    assert _decision(_run(_as_written(row))) is None, row["id"]


REFUSED = [
    "ls && neonctl --help",
    "cd /tmp;neonctl --help",
    "(neonctl --help)",
    "timeout 10 neonctl --help",
    "/usr/local/bin/neonctl --help",
    "ls\nneonctl --help",
    "ls && \\\nneonctl --help",
    "bash -c 'neonctl --help'",
    "env -u NEON_API_KEY true && neonctl --help",  # env scopes the other command
    "unset NEON_API_KEY | neonctl --help",  # a pipeline element is its own subshell
    "NEON_API_KEY=CANARY_x7q2 neonctl --help",  # set, not removed
    "neonctl",
    "neon projects",
    "neonctl -o json",
    "neonctl --bogus projects list",
    "env -u NEON_API_KEY DEBUG='*' neonctl me",  # the trace prints the stored login too
    "DEBUG=neonctl* neon projects list",
    "python3 -m pip config list",
    "pip3 config debug",
    "env -u GH_TOKEN gh auth token",  # falls back to the stored login's token
    "gh auth token | cat",
    "gh auth token 2>/dev/null",
    "gh auth token > /dev/stdout",
    "gh auth token >&2",
    "echo $(gh auth token)",
    "cat <<EOF\nhi\nEOF\nneonctl --help",  # a command after a heredoc is judged
]

ALLOWED = [
    'echo "neonctl --help"',
    "# neonctl --help",
    "grep -n neonctl notes.md",
    "neonctl projects list",
    "neonctl -o json projects list",
    "neon branches list --project-id p-1",
    "unset NEON_API_KEY && neonctl --help",
    "env --unset=NEON_API_KEY neonctl --help",
    "env -u NEON_API_KEY bash -c 'neonctl --help'",
    "NEON_API_KEY= neon projects --help",
    "gh auth token > token.txt",
    "gh auth token >> token.txt",
    "gh auth token &> token.txt",
    "git status",
    "git commit -F - <<'EOF'\nmentions neonctl --help here\nEOF",  # a heredoc body is data
]


@pytest.mark.parametrize("command", REFUSED)
def test_the_command_forms_around_a_row_are_refused(command):
    verdict = _decision(_run(command))
    assert verdict is not None and verdict[0] == "deny", (command, verdict)


@pytest.mark.parametrize("command", ALLOWED)
def test_the_command_forms_around_a_row_are_allowed(command):
    assert _decision(_run(command)) is None, command


def test_the_refusal_names_the_safe_form_and_never_a_value():
    verdict = _decision(_run("NEON_API_KEY=CANARY_x7q2_rs01 neonctl --help"))
    assert verdict is not None and verdict[0] == "deny"
    assert "env -u NEON_API_KEY" in verdict[1]
    assert "CANARY_x7q2_rs01" not in verdict[1]


def test_another_tool_is_untouched():
    assert _decision(_run("neonctl --help", tool="Read")) is None


def test_a_malformed_payload_fails_open():
    p = subprocess.run(["bash", str(GUARD)], input="Bash neonctl {not json", capture_output=True,
                       text=True, env=constructed_env(), timeout=60)
    assert p.returncode == 0 and _decision(p) is None


def test_a_refusal_is_recorded_with_its_row_and_never_the_command(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    bot = root / "runtime" / "bots" / "tbot"
    bot.mkdir(parents=True)
    env = constructed_env(HOME=tmp_path / "home", FLEET_NAME="testfleet", BOT_ID="tbot",
                          BOT_DIR=bot, **scratch_plane_env(root, initialize=True))
    with _serving(root, scratch_plane_env) as socket:
        p = _run("NEON_API_KEY=CANARY_x7q2_ev01 neonctl --help",
                 env={**env, "PLANE_SOCKET": str(socket)})
    assert _decision(p) is not None and _decision(p)[0] == "deny", p.stderr
    rows = [json.loads(line) for line in read_fleet_events(root).splitlines()]
    refused = [r for r in rows if r["type"] == "credential_echo_refused"]
    assert len(refused) == 1, (rows, p.stderr)
    assert refused[0]["source"] == "credential-echo-guard", refused[0]
    assert refused[0]["data"] == {"row": "neon-help", "cli": "neonctl"}, refused[0]
    assert "CANARY_x7q2_ev01" not in json.dumps(rows)
