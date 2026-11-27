"""#3156: on a Skill call, bot-vitals.sh adds ``skill`` to the ``tool_call``
event's data: the name in the hook payload's ``tool_input.skill``, as given,
so the plane can count which library skills run. The call's ``args`` are never
read: they can carry text the user wrote.

Every case runs the REAL hook through the REAL fleet-event door into a scratch
plane (the rig of tests/test_plane_cutover_keepalive.py) and reads the row as
the ledger wrote it. The cost case counts the commands the hook runs, from
``bash -x``, as #906 counts them.
"""
from __future__ import annotations

import collections
import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from tests.plane_fixtures import _stdlib_readers
from tests.test_plane_keepalive_door import LIB, _replay_pending, _rig

FLEET = "kfleet"
SESSION = "s-3156"
# Text a Skill call's args can carry. It must reach no row and no file.
ARGS = "handoff --auto and a sentence the user typed 3156"
BASE_KEYS = {"tool", "event", "session"}


def _payload(tool, event="PreToolUse", **extra):
    return {"hook_event_name": event, "tool_name": tool, "session_id": SESSION, **extra}


def _run(root: Path, payload: dict, scratch_plane_env, *, trace=False):
    root.mkdir(parents=True, exist_ok=True)
    libdir, bot, env = _rig(root, scratch_plane_env=scratch_plane_env)
    (libdir / "bot-vitals.sh").symlink_to(LIB / "bot-vitals.sh")
    env = {**env, "BOT_DIR": str(bot), "BOT_ID": "b1", "FLEET_NAME": FLEET}
    argv = ["bash", str(libdir / "bot-vitals.sh")]
    if trace:
        # Each traced command names its file and function stack. PS4 is set in
        # a BASH_ENV file, not the environment: bash ignores an inherited PS4
        # when it runs as root. The stack reads with an empty default: under the
        # hook's set -u, bash 3.2 stops at an unset FUNCNAME at the top level.
        ps4 = root / "ps4.sh"
        ps4.write_text("PS4='+XTRACE ${BASH_SOURCE[0]##*/}|${FUNCNAME[*]:-}| '\n")
        env["BASH_ENV"] = str(ps4)
        argv = ["bash", "-x", str(libdir / "bot-vitals.sh")]
    # errors="replace": the rig sets no locale, so bash -x quotes the hook's
    # non-ASCII text byte by byte; the trace is read for its ASCII command words.
    r = subprocess.run(argv, input=json.dumps(payload), capture_output=True, encoding="utf-8",
                       errors="replace", timeout=180, env=env)
    assert r.returncode == 0, r.stderr
    assert (bot / "data" / ".last-tool-call").exists()          # the hook ran to its end
    return r


def _rows(root: Path) -> list[dict]:
    """The fleet's rows as the ledger wrote them, once the hook's staged
    emission is replayed and its one tool_call row is committed; none when no
    tool_call row is committed within the wait."""
    deadline = time.monotonic() + 30
    while True:
        _replay_pending(root)
        n = 0
        if db_path(root).exists():
            try:
                with connect(db_path(root)) as conn:
                    n = conn.execute("SELECT COUNT(*) FROM events WHERE event = 'tool_call'").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
        if n or time.monotonic() > deadline:
            break
        time.sleep(0.25)
    if not n:
        return []
    pr = _stdlib_readers()
    with connect(db_path(root)) as conn:
        return [pr.public(r) for r in pr.fleet_events(conn, FLEET)]


def _keys(value) -> set[str]:
    """Every key at any depth of a decoded JSON value."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def _files_holding(root: Path, text: str) -> list[str]:
    """The files under the plane root whose bytes hold ``text``."""
    needle = text.encode()
    return [str(f) for f in (root / "state").rglob("*") if f.is_file() and needle in f.read_bytes()]


@pytest.mark.parametrize("event, name", [
    ("PreToolUse", "claudna:session"),
    ("PostToolUse", "claudna:session"),
    ("PreToolUse", "fleet-ops"),
    ("PreToolUse", "apps/web:deploy"),
])
def test_a_skill_call_records_the_skill_name_and_never_its_args(tmp_path, event, name, *, scratch_plane_env):
    _run(tmp_path, _payload("Skill", event, tool_input={"skill": name, "args": ARGS}), scratch_plane_env)
    rows = _rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert (row["bot"], row["type"], row["source"]) == ("b1", "tool_call", "vitals")
    assert row["data"] == {"tool": "Skill", "event": event, "session": SESSION, "skill": name}
    assert "args" not in _keys(row)
    assert _files_holding(tmp_path, ARGS) == []                  # not in the plane, the stage or a spool


@pytest.mark.parametrize("payload", [
    _payload("Bash", tool_input={"command": "ls", "skill": "claudna:session"}),
    _payload("Read", "PostToolUse", tool_input={"file_path": "/tmp/x", "skill": "fleet-ops"}),
    _payload("mcp__github__get_issue", tool_input={"skill": "claudna:session", "args": ARGS}),
    _payload("skill", tool_input={"skill": "claudna:session", "args": ARGS}),
    _payload("Read"),
], ids=["bash", "read-post", "mcp", "lowercase-skill", "no-input"])
def test_any_other_tool_records_no_skill_key(tmp_path, payload, *, scratch_plane_env):
    _run(tmp_path, payload, scratch_plane_env)
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert set(rows[0]["data"]) == BASE_KEYS
    assert rows[0]["data"]["tool"] == payload["tool_name"]
    assert _files_holding(tmp_path, ARGS) == []


@pytest.mark.parametrize("extra", [
    {},
    {"tool_input": {}},
    {"tool_input": {"args": ARGS}},
    {"tool_input": {"skill": None}},
    {"tool_input": {"skill": ""}},
    {"tool_input": {"skill": 7}},
    {"tool_input": {"skill": ["claudna:session"]}},
    {"tool_input": "claudna:session"},
    {"tool_input": ["claudna:session"]},
], ids=["no-input", "empty-input", "args-only", "null", "empty-name", "number", "list", "input-string", "input-list"])
def test_a_skill_call_with_no_skill_name_records_the_event_without_one(tmp_path, extra, *, scratch_plane_env):
    _run(tmp_path, _payload("Skill", **extra), scratch_plane_env)
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["data"] == {"tool": "Skill", "event": "PreToolUse", "session": SESSION}
    assert _files_holding(tmp_path, ARGS) == []


RIG_PATH = "/usr/bin:/bin"          # the rig's PATH past its stub directory, which holds no command the hook runs
TRACED = re.compile(r"^\++XTRACE ([^|]*)\|([^|]*)\| (\S+)")
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^]]*\])?\+?=")


def _commands(stderr: str) -> collections.Counter:
    """Each command the hook ran, by file, function stack and command word.
    The door's wait for its shim (plane_emit_bounded) polls the child's exit,
    so its count follows the child's run time, not the hook's work: it is left
    out, as its child is (a process the trace does not follow)."""
    seen = collections.Counter()
    for line in stderr.splitlines():
        m = TRACED.match(line)
        if not m:
            continue
        source, stack, word = m.groups()
        if "plane_emit_bounded" in stack.split():
            continue
        a = ASSIGNMENT.match(word)
        seen[(source, stack, a.group(1) + "=" if a else word)] += 1
    return seen


def _hook_execs(counts: collections.Counter) -> collections.Counter:
    """The programs the hook's own lines start, by name: #906's exec inventory of
    bot-vitals.sh. Builtins, keywords, assignments and functions start none."""
    builtins = set(subprocess.run(["bash", "-c", "compgen -b; compgen -k"], capture_output=True,
                                  text=True, check=True).stdout.split())
    execs = collections.Counter()
    for (source, _, word), n in counts.items():
        if source == "bot-vitals.sh" and not word.endswith("=") and word not in builtins \
                and shutil.which(word, path=RIG_PATH):
            execs[word] += n
    return execs


def test_a_skill_call_costs_the_commands_any_tool_call_costs(tmp_path, *, scratch_plane_env):
    """#906 counts the hook's cost as the commands it runs per call (its exec
    inventory, read with bash -x). A Skill call runs exactly the commands a call
    to any other tool runs, the one python3 included: the name is read inside
    the python3 the hook already starts. The hook's own lines start the same
    four programs on every call; a change that adds one changes this inventory."""
    skill_root = tmp_path / "skill"
    read_root = tmp_path / "read"
    skill = _commands(_run(skill_root, _payload("Skill", tool_input={"skill": "claudna:session", "args": ARGS}),
                           scratch_plane_env, trace=True).stderr)
    read = _commands(_run(read_root, _payload("Read", tool_input={"file_path": "/tmp/x"}),
                          scratch_plane_env, trace=True).stderr)
    # The trace is read, not empty: the hook's own lines and the door's are both in it.
    assert sum(n for (src, _, _), n in skill.items() if src == "bot-vitals.sh") > 5
    assert any("emit_fleet_event" in stack.split() for (_, stack, _) in skill)
    for counts in (skill, read):
        assert _hook_execs(counts) == {"dirname": 1, "cat": 1, "python3": 1, "touch": 1}
    assert skill == read, (sorted((skill - read).items()), sorted((read - skill).items()))
    # Both calls were recorded, the Skill one with its name.
    assert _rows(skill_root)[0]["data"]["skill"] == "claudna:session"
    assert "skill" not in _rows(read_root)[0]["data"]
