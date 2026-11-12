"""The heavy-job slot records a job's SHAPE, never its text (#2037).

What the slot records reaches five places: the slot's lock file, the wrapper's
own events, the hook's events, events about other holders, and the refusal
another bot reads. Plane events are never pruned. So a record holds the tool
and the names of its flags, and nothing else: no value, no positional word, no
environment assignment. The wrapper sees argv after the shell expanded it, so a
secret passed as "$VAR" arrives as a bare word; the hook sees an unparsed
command as typed, heredoc bodies included. A shape keeps neither. The refusal
names a holder's bot, tool and start time.

Driven through the real wrapper and the real hook script, with the stub `pytest`
the other heavy-slot tests use. The secrets are FAKE, assembled from fragments.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py"
GUARD = REPO / "claudlobby/_runtime_scripts" / "heavy-slot-guard.sh"
BASH = shutil.which("bash")
SECRET = "Fk3" + "q9Zx" * 6  # FAKE

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
    (bin_dir / "pytest").write_text(STUB)
    (bin_dir / "pytest").chmod(0o755)
    env = constructed_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HEAVY_SLOT_DIR=tmp_path / "state",
        HEAVY_SLOT_EVENTS_FILE=tmp_path / "events.jsonl",
        HEAVY_SLOT_BOOT_ID="this-boot",
        FLEET_NAME="testfleet",
        BOT_ID="alpha",
        STUB_DIR=tmp_path,
        PLANE_EMIT_DISABLED="1",
        TZ="UTC",
    )
    yield type("SE", (), {"env": env, "tmp": tmp_path, "state": tmp_path / "state",
                          "events": tmp_path / "events.jsonl"})
    (tmp_path / "release").touch()


def _run(se, *argv, **extra):
    return subprocess.run([str(WRAPPER), "run", "--", *argv],
                          env={**se.env, **{k: str(v) for k, v in extra.items()}},
                          capture_output=True, text=True, timeout=60)


def _hold(se, *argv):
    """A holder running `argv` under the slot, waiting on `release`."""
    p = subprocess.Popen([str(WRAPPER), "run", "--", *argv],
                         env={**se.env, "STUB_WAIT": str(se.tmp / "release")},
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 15
    while not list(se.tmp.glob("ran.*")):
        assert time.monotonic() < deadline, "the holder never started"
        time.sleep(0.02)
    return p


def _hook(se, command):
    payload = {"session_id": "s", "transcript_path": "/dev/null", "cwd": str(se.tmp),
               "permission_mode": "auto", "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_input": {"command": command},
               "tool_use_id": "toolu_x", "prompt_id": "p"}
    p = subprocess.run([BASH, str(GUARD)], input=json.dumps(payload), env=se.env,
                       capture_output=True, text=True, timeout=60)
    return p.stdout


def _events_text(se) -> str:
    return se.events.read_text() if se.events.exists() else ""


def _lock_text(se) -> str:
    return "".join(p.read_text() for p in se.state.glob("slot-*.lock"))


# Each secret-bearing argv, and the shape the record keeps of it. The last three
# are what a secret passed as "$VAR" looks like once the shell expanded it: a
# bare word with nothing naming it.
FORMS = [
    (["env", "SOME_KEY=" + SECRET, "pytest", "-q"], "pytest -q"),
    (["pytest", "-q", "--token", SECRET], "pytest -q --token"),
    (["pytest", "-q", "--password=" + SECRET], "pytest -q --password"),
    (["pytest", "-q", "-H", "Authorization: Bearer " + SECRET], "pytest -q -H"),
    (["pytest", "-q", "--db", "postgres://app:" + SECRET + "@db.local/x"], "pytest -q --db"),
    (["pytest", SECRET], "pytest"),
    (["pytest", "-q", "--any-flag", SECRET], "pytest -q --any-flag"),
    (["pytest", "-k" + SECRET], "pytest -k"),
]


class TestASecretNeverSurvives:
    @pytest.mark.parametrize("argv,kept", FORMS)
    def test_the_record_and_the_wrappers_events(self, se, argv, kept):
        r = _run(se, *argv)
        assert r.returncode == 0, r.stderr
        for where, text in (("lock file", _lock_text(se)), ("events", _events_text(se))):
            assert SECRET not in text, where
            assert kept in text, where
        assert "pytest" in _events_text(se)  # the tool stays

    def test_the_refusal_names_bot_tool_and_start_never_the_command(self, se):
        holder = _hold(se, "env", "SOME_KEY=" + SECRET, "pytest", "-q")
        second = _run(se, "pytest", BOT_ID="beta")
        assert second.returncode == 75
        assert SECRET not in second.stderr and "SOME_KEY" not in second.stderr
        assert "testfleet/alpha" in second.stderr and "pytest" in second.stderr
        assert "since" in second.stderr
        refused = [json.loads(x) for x in _events_text(se).splitlines() if '"heavy_slot_refused"' in x]
        assert refused and SECRET not in json.dumps(refused)
        (se.tmp / "release").touch()
        holder.wait(15)

    def test_the_hooks_refusal_and_its_event(self, se):
        holder = _hold(se, "env", "SOME_KEY=" + SECRET, "pytest", "-q")
        out = _hook(se, "OTHER_TOKEN=" + SECRET + " npm ci")
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        assert SECRET not in reason and "SOME_KEY" not in reason
        assert "testfleet/alpha" in reason and "pytest" in reason
        text = _events_text(se)
        assert SECRET not in text and "OTHER_TOKEN" not in text
        (se.tmp / "release").touch()
        holder.wait(15)

    @pytest.mark.parametrize("command", [
        "eval 'API_KEY=" + SECRET + " npm ci'",
        "eval \"npm ci --registry " + SECRET + "\"",
        "eval 'npm ci' <<'EOF'\n" + SECRET + "\nEOF",
    ])
    def test_the_hooks_unparsed_event_keeps_a_shape(self, se, command):
        _hook(se, command)
        events = [json.loads(x) for x in _events_text(se).splitlines()]
        assert [e["type"] for e in events] == ["heavy_slot_unparsed"]
        assert SECRET not in json.dumps(events)
        assert events[0]["data"]["shape"].startswith("npm")

    def test_the_status_of_a_held_slot_shows_its_shape(self, se):
        holder = _hold(se, "pytest", "-q", "--token", SECRET)
        out = subprocess.run([str(WRAPPER), "status"], env=se.env, capture_output=True,
                             text=True, timeout=60).stdout
        assert "slot 0: HELD by testfleet/alpha" in out and "pytest -q --token" in out
        assert SECRET not in out
        (se.tmp / "release").touch()
        holder.wait(15)

    def test_a_record_written_before_redaction_is_not_reemitted_or_shown(self, se):
        """A lock file from before this change still holds the raw command:
        whatever reads it back redacts it again."""
        se.state.mkdir(parents=True)
        legacy = {"v": 1, "slot": 0, "slots": 1, "state": "held", "fleet": "testfleet",
                  "bot": "old", "command": "env OLD_KEY=" + SECRET + " pytest",
                  "cwd": "/x", "pid": 1, "host": "h", "boot_id": "earlier-boot",
                  "started_at": "2026-09-30T00:00:00Z", "started_epoch": 1790726400,
                  "released_at": None, "exit": None}
        (se.state / "slot-0.lock").write_text(json.dumps(legacy))
        for args in ([], ["--json"]):
            st = subprocess.run([str(WRAPPER), "status", *args], env=se.env,
                                capture_output=True, text=True, timeout=60)
            assert SECRET not in st.stdout, args
        _run(se, "pytest", "-q")  # takes the slot: the old hold is reported as unreleased
        text = _events_text(se)
        assert '"heavy_slot_unreleased"' in text and SECRET not in text


def _module():
    """The script as a module (its name has a dash)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("heavy_slot", WRAPPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAShapeStaysUseful:
    @pytest.mark.parametrize("argv,shape", [
        (["pytest", "-q", "tests/test_x.py", "-k", "name and not slow"], "pytest -q -k"),
        (["pytest", "--maxfail=2", "-x", "--tb=short"], "pytest --maxfail -x --tb"),
        (["npx", "playwright", "test", "--project=chromium"], None),
        (["uv", "run", "--with", "pytest==8.0", "pytest", "-q"], "pytest -q"),
        (["env", "-i", "PATH=/usr/bin", "nice", "-n", "5", "npm", "ci"], "npm ci"),
    ])
    def test_the_tool_and_its_flag_names(self, argv, shape):
        got = _module()._shape_argv(argv)
        if shape is None:  # the label is the classifier's own: pin only that the flag stays
            assert got.endswith(" --project") and "chromium" not in got
        else:
            assert got == shape

    def test_a_typed_command_keeps_its_tool_words_and_flags(self):
        assert _module()._shape_text("cd app && npm run build --prefix app") == "npm --prefix"

    def test_through_the_wrapper(self, se):
        r = _run(se, "pytest", "-q", "tests/test_x.py", "-k", "name and not slow")
        assert r.returncode == 0, r.stderr
        acquired = [json.loads(x) for x in _events_text(se).splitlines() if '"heavy_slot_acquired"' in x]
        assert acquired[0]["data"]["shape"] == "pytest -q -k"
        assert json.loads((se.state / "slot-0.lock").read_text())["tool"] == "pytest"
