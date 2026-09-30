"""A bot can declare heavy scripts (#2039): `heavy_slot: {scripts: [...]}`.

A heavy tool started inside a script (a Python render script that drives a
browser) is invisible to the matcher, and the wrapper refuses a script by
design, so such a job had no slot path at all. A declared script takes the
slot. The composer writes each fleet's declared scripts, resolved to absolute
paths, under the install's `runtime/_host/heavy-slot/`, and the hook and the
wrapper read them from the install they belong to. Nothing on a command line
can declare one: not argv, not an environment variable.

Driven through a COPY of the two scripts in a temporary install root, since the
declared files are found from the script's own location and never from the
environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

SCRIPT = """#!/usr/bin/env python3
import os, pathlib, sys
pathlib.Path(os.environ["STUB_DIR"], "ran." + pathlib.Path(sys.argv[0]).name).touch()
"""


@pytest.fixture()
def se(tmp_path):
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    for name in ("heavy-slot.py", "heavy-slot-guard.sh"):
        shutil.copy2(REPO / "lib" / name, root / "lib" / name)
    bot = tmp_path / "bot"
    (bot / "tools" / "sub").mkdir(parents=True)
    for rel in ("render.py", "undeclared.py", "tools/job.py", "tools/sub/deep.py"):
        (bot / rel).write_text(SCRIPT)
        (bot / rel).chmod(0o755)
    declared = root / "runtime" / "_host" / "heavy-slot"
    declared.mkdir(parents=True)
    (declared / "testfleet.json").write_text(json.dumps({
        "v": 1, "fleet": "testfleet",
        "bots": {"alpha": [str(bot / "render.py"), str(bot / "tools" / "*.py")]}}))
    (declared / "testfleet.names").write_text("render.py\n*.py\n")
    env = constructed_env(
        PATH=os.environ["PATH"],
        HEAVY_SLOT_DIR=tmp_path / "state",
        HEAVY_SLOT_EVENTS_FILE=tmp_path / "events.jsonl",
        HEAVY_SLOT_BOOT_ID="this-boot",
        FLEET_NAME="testfleet",
        BOT_ID="alpha",
        STUB_DIR=tmp_path,
        PLANE_EMIT_DISABLED="1",
        TZ="UTC",
    )
    return type("SE", (), {"root": root, "bot": bot, "tmp": tmp_path, "env": env,
                           "wrapper": root / "lib" / "heavy-slot.py",
                           "guard": root / "lib" / "heavy-slot-guard.sh",
                           "declared": declared})


def _run(se, *argv, env=None):
    return subprocess.run([str(se.wrapper), "run", "--", *argv], cwd=se.bot,
                          env=env or se.env, capture_output=True, text=True, timeout=60)


def _ran(se, name):
    return (se.tmp / f"ran.{name}").exists()


def _events(se):
    f = se.tmp / "events.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


class TestTheWrapper:
    def test_a_declared_script_takes_the_slot(self, se):
        r = _run(se, "python3", "render.py")
        assert r.returncode == 0, r.stderr
        assert _ran(se, "render.py")
        assert [e["type"] for e in _events(se)] == ["heavy_slot_acquired", "heavy_slot_released"]

    def test_an_undeclared_script_is_still_refused(self, se):
        r = _run(se, "python3", "undeclared.py")
        assert r.returncode == 2 and "not a heavy job" in r.stderr
        assert not _ran(se, "undeclared.py")

    def test_the_environment_cannot_declare_a_script(self, se, tmp_path):
        other = tmp_path / "other-root" / "runtime" / "_host" / "heavy-slot"
        other.mkdir(parents=True)
        (other / "x.json").write_text(json.dumps(
            {"v": 1, "fleet": "x", "bots": {"alpha": [str(se.bot / "undeclared.py")]}}))
        r = _run(se, "python3", "undeclared.py",
                 env={**se.env, "CLAUDLOBBY_ROOT": str(tmp_path / "other-root")})
        assert r.returncode == 2 and not _ran(se, "undeclared.py")

    def test_a_glob_matches_within_one_directory(self, se):
        assert _run(se, "python3", "tools/job.py").returncode == 0 and _ran(se, "job.py")
        r = _run(se, "python3", "tools/sub/deep.py")
        assert r.returncode == 2 and not _ran(se, "deep.py")

    @pytest.mark.parametrize("argv", [["./render.py"], ["python3", "-u", "render.py", "--out", "x"]])
    def test_other_ways_to_run_it(self, se, argv):
        r = _run(se, *argv)
        assert r.returncode == 0, r.stderr
        assert _ran(se, "render.py")

    def test_python_dash_c_is_not_a_script(self, se):
        assert _run(se, "python3", "-c", "print(1)").returncode == 2


def _hook(se, command, env=None, cwd=None):
    payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": cwd or str(se.bot),
               "permission_mode": "auto", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": command},
               "tool_use_id": "toolu_x", "prompt_id": "p"}
    p = subprocess.run([BASH, str(se.guard)], input=json.dumps(payload), env=env or se.env,
                       capture_output=True, text=True, timeout=60)
    return p.stdout


class TestTheHook:
    def test_it_wraps_a_declared_script(self, se):
        out = _hook(se, "python3 render.py --pages 3")
        command = json.loads(out)["hookSpecificOutput"]["updatedInput"]["command"]
        assert "heavy-slot.py" in command and command.endswith("python3 render.py --pages 3")

    def test_it_leaves_an_undeclared_script_alone(self, se):
        assert _hook(se, "python3 undeclared.py") == ""

    def test_the_prefilter_starts_python_only_for_a_declared_name(self, se, tmp_path):
        shim = tmp_path / "shim"
        shim.mkdir()
        os.symlink("/bin/cat", shim / "cat")
        marker = tmp_path / "python-started"
        (shim / "python3").write_text(f"#!/bin/sh\n: > {marker}\ncat >/dev/null\n")
        (shim / "python3").chmod(0o755)
        env = {**se.env, "PATH": str(shim)}
        # a neutral cwd: the tmp path holds "pytest", which the heavy-word
        # prefilter would match on its own
        cwd = "/srv/app"
        _hook(se, "ls -la", env=env, cwd=cwd)
        assert not marker.exists()
        _hook(se, "python3 render.py", env=env, cwd=cwd)
        assert marker.exists()
        marker.unlink()
        for f in se.declared.iterdir():
            f.unlink()
        _hook(se, "python3 render.py", env=env, cwd=cwd)  # nothing declared on the host: no python
        assert not marker.exists()
