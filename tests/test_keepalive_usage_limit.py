"""keepalive's LIMIT verdict and its one resume per limit (#996).

Every test drives the REAL keepalive.sh tick (real lib-common, real
usage-limit.py) against a stateful tmux stub that serves live Claude Code
frames (tests/fixtures/pane-states/usage-limit-*.txt, captured from 2.1.292),
records every key it is sent, and moves the frame the way Claude Code does: a
typed prompt shows in the input box, an Enter submits it, and an Enter on the
usage-limit menu closes it. The reset time in each frame is rewritten relative
to now, so "before the reset" and "after the reset" are the real clock's.

The failures each test names are the ones #996 ruled out: another menu gets no
keys, usage credits are never chosen (the wait option is chosen by its label,
never by position), the reset time decides when, and one action per limit.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.fixtures.native_admission import admit_watchdog_fixture

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"
FIXTURES = REPO / "tests/fixtures/pane-states"
DOOR_FILES = (
    "keepalive.sh",
    "lib-common.sh",
    "supervisor.sh",
    "plane-emit.sh",
    "plane-socket-client.py",
    "usage-limit.py",
)
PROMPT_START = "Your usage limit has reset. Continue the task"

STUB = r'''#!/usr/bin/env python3
"""A tmux that serves one pane from STUB_DIR and records what it is sent."""
import os, sys
d = os.environ["STUB_DIR"]
args = sys.argv[1:]
if args[:1] == ["-L"]:
    args = args[2:]
def rd(n, default=""):
    try:
        return open(os.path.join(d, n), encoding="utf-8").read()
    except OSError:
        return default
def wr(n, s):
    open(os.path.join(d, n), "w", encoding="utf-8").write(s)
def log(s):
    open(os.path.join(d, "keys.log"), "a", encoding="utf-8").write(s + "\n")
cmd = args[0] if args else ""
if cmd == "has-session":
    sys.exit(0)
if cmd == "capture-pane":
    frame, typed = rd("frame"), rd("typed")
    if typed:
        lines = frame.split("\n")
        for i, l in enumerate(lines):
            if l.rstrip() in ("❯", "❯ ") or l == "❯ ":
                chunks = [typed[k:k + 76] for k in range(0, len(typed), 76)]
                lines[i:i + 1] = ["❯ " + chunks[0]] + ["  " + c for c in chunks[1:]]
                break
        frame = "\n".join(lines)
    sys.stdout.write(frame)
    sys.exit(0)
if cmd == "send-keys":
    rest = args[1:]
    if rest[:1] == ["-t"]:
        rest = rest[2:]
    if rest[:1] == ["-l"]:
        rest = rest[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        text = rest[0] if rest else ""
        log("TEXT " + text)
        wr("typed", rd("typed") + text)
        sys.exit(0)
    for key in rest:
        log("KEY " + key)
        if key == "Enter":
            if "What do you want to do?" in rd("frame"):
                wr("frame", rd("on_menu_enter", rd("frame")))
            elif rd("typed"):
                wr("frame", rd("on_submit", rd("frame")))
                wr("typed", "")
    sys.exit(0)
sys.exit(0)
'''


def _clock(epoch: int) -> str:
    """How Claude Code prints a reset minute within 24 hours, in UTC."""
    t = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return t.strftime("%-I:%M%p").lower() + " (UTC)"


def _frame(name: str, reset_text: str) -> str:
    text = (FIXTURES / f"usage-limit-{name}.txt").read_text()
    for old in ("10:36am (America/New_York)", "10:45am (America/New_York)", "10:45am"):
        text = text.replace(
            old, reset_text if "(" in old else reset_text.split(" (")[0]
        )
    return text


class Rig:
    def __init__(self, tmp_path: Path, env_extra: dict, armed: bool):
        self.tmp = tmp_path
        self.lib = tmp_path / "lib"
        self.lib.mkdir()
        for name in DOOR_FILES:
            (self.lib / name).symlink_to(LIB / name)
        admit_watchdog_fixture(self.lib)
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        stub = tmp_path / "tmux"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.bot = tmp_path / "bots" / "b1"
        (self.bot / "data").mkdir(parents=True)
        conf = 'BOT_NAME="b1"\nFLEET_NAME="kfleet"\nBOT_SERVICE="com.k.b1"\nKEEPALIVE_LIMIT_RESUME_GRACE_S=0\n'
        if armed:
            conf += "KEEPALIVE_LIMIT_RESUME_ENABLED=1\n"
        (self.bot / "bot.conf").write_text(conf)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name in ("systemctl", "launchctl"):
            native = bindir / name
            native.write_text("#!/bin/bash\nexit 0\n")
            native.chmod(0o755)
        (tmp_path / "state" / "pane-send").mkdir(parents=True)
        self.env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "TMUX_BIN": str(stub),
            "STUB_DIR": str(self.stub_dir),
            "CLAUDLOBBY_ROOT": str(tmp_path),
            "PLANE_EMIT_DISABLED": "1",
            "PANE_SEND_SETTLE_S": "0",
            "PANE_SEND_CHUNK_SETTLE_S": "0",
            "KEEPALIVE_LIMIT_MENU_SETTLE_S": "0",
            **env_extra,
        }

    def show(self, frame: str, *, on_submit: str = "", on_menu_enter: str = ""):
        (self.stub_dir / "frame").write_text(frame)
        (self.stub_dir / "typed").write_text("")
        (self.stub_dir / "on_submit").write_text(on_submit)
        (self.stub_dir / "on_menu_enter").write_text(on_menu_enter)

    def hook_record(self, hit_epoch: int, line: str = "You've hit your session limit"):
        (self.bot / "data" / ".usage-limit").write_text(f"{hit_epoch}\n{line}\n")

    def tick(self) -> subprocess.CompletedProcess:
        r = subprocess.run(
            ["bash", str(self.lib / "keepalive.sh"), str(self.bot)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )
        assert r.returncode == 0, r.stderr
        return r

    def keys(self) -> list[str]:
        p = self.stub_dir / "keys.log"
        return p.read_text().splitlines() if p.exists() else []

    def log(self) -> str:
        p = self.bot / "keepalive.log"
        return p.read_text() if p.exists() else ""

    def data(self, name: str) -> Path:
        return self.bot / "data" / name


@pytest.fixture
def rig(tmp_path):
    return lambda armed=True, **env: Rig(tmp_path, env, armed)


def _past_reset():
    """A reset 10 minutes gone, seen first 15 minutes ago."""
    now = int(time.time())
    return now - 900, _clock(now - 600)


def _future_reset():
    now = int(time.time())
    return now - 60, _clock(now + 3600)


def _typed(keys: list[str]) -> str:
    return "".join(k[5:] for k in keys if k.startswith("TEXT "))


# --- the held frame is named, never IDLE --------------------------------------------


def test_a_held_frame_reads_limit_with_its_reset_not_idle(rig):
    r = rig(armed=False)
    hit, reset = _future_reset()
    r.show(_frame("held", reset))
    r.hook_record(hit)
    r.tick()
    assert f"LIMIT — held by a usage limit (session limit), resets {reset}" in r.log()
    assert not r.data(".idle").exists()
    first, reset_epoch, screen, _native = (
        r.data(".limit").read_text().split("\n")[0].split()
    )
    assert screen == "limit" and int(reset_epoch) > time.time()
    assert r.keys() == []


def test_a_limit_line_with_no_hook_record_is_not_a_limit(rig):
    """A bot that quotes a limit line writes no StopFailure record: IDLE as before."""
    r = rig()
    _hit, reset = _past_reset()
    r.show(_frame("held", reset))
    r.tick()
    assert "IDLE — at prompt" in r.log() and "LIMIT" not in r.log()
    assert r.data(".idle").exists() and not r.data(".limit").exists()
    assert r.keys() == []


def test_a_tool_call_after_the_hook_record_ends_the_limit(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(_frame("held", reset))
    r.hook_record(hit)
    marker = r.data(".last-tool-call")
    marker.touch()
    stamp = time.time() - 400  # older than the busy window, newer than the record
    os.utime(marker, (stamp, stamp))
    os.utime(r.data(".usage-limit"), (stamp - 100, stamp - 100))
    r.tick()
    assert "LIMIT" not in r.log() and r.keys() == []


# --- when: the reset decides ---------------------------------------------------------


def test_no_keys_before_the_reset(rig):
    r = rig()
    hit, reset = _future_reset()
    r.show(_frame("held", reset))
    r.hook_record(hit)
    r.tick()
    assert "resume due" in r.log()
    assert r.keys() == [] and not r.data(".limit-resumed").exists()


def test_resume_off_sends_no_keys_after_the_reset(rig):
    r = rig(armed=False)
    hit, reset = _past_reset()
    r.show(_frame("held", reset))
    r.hook_record(hit)
    r.tick()
    assert "resume OFF here (KEEPALIVE_LIMIT_RESUME_ENABLED=1" in r.log()
    assert r.keys() == []


def test_after_the_reset_one_resume_prompt_is_submitted(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(_frame("held", reset), on_submit=_frame("resumed", reset))
    r.hook_record(hit)
    r.tick()
    keys = r.keys()
    assert _typed(keys).startswith(PROMPT_START), keys
    assert keys[-1] == "KEY Enter" and keys.count("KEY Enter") == 1, keys
    assert "outcome submitted" in r.log() and "menu none" in r.log()
    done_reset = r.data(".limit-resumed").read_text().split()[0]
    assert done_reset == r.data(".limit").read_text().split()[1]


# --- one action per limit -------------------------------------------------------------


def test_one_action_per_limit(rig):
    """Resumed once, then held again by the same reset (it was not over after
    all): no second prompt, however many ticks see it."""
    r = rig()
    hit, reset = _past_reset()
    r.show(_frame("held", reset), on_submit=_frame("resumed", reset))
    r.hook_record(hit)
    r.tick()
    sent = r.keys()
    assert _typed(sent).startswith(PROMPT_START)
    r.show(_frame("held", reset), on_submit=_frame("resumed", reset))
    r.hook_record(int(time.time()) - 300)
    r.tick()
    r.tick()
    assert r.keys() == sent
    assert "already resumed once for this reset, so no more keys" in r.log()


def test_a_new_limit_gets_its_own_action(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(_frame("held", reset), on_submit=_frame("resumed", reset))
    r.hook_record(hit)
    r.tick()
    first = len(r.keys())
    now = int(time.time())
    later = _clock(now - 120)
    r.show(_frame("held", later), on_submit=_frame("resumed", later))
    r.hook_record(now - 200)
    r.tick()
    assert _typed(r.keys()[first:]).startswith(PROMPT_START)


# --- the menu: the wait option by its label, never credits, nothing else --------------


def test_the_menu_gets_one_enter_on_the_wait_label_then_the_prompt(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(
        _frame("menu", reset),
        on_menu_enter=_frame("menu-closed", reset),
        on_submit=_frame("resumed", reset),
    )
    r.hook_record(hit)
    r.tick()
    keys = r.keys()
    assert keys[0] == "KEY Enter", keys  # the menu's wait option
    assert _typed(keys).startswith(PROMPT_START)
    assert keys.count("KEY Enter") == 2, keys  # the option, then the prompt
    assert "menu confirmed, outcome submitted" in r.log()


def _menu(reset: str, options: list[str]) -> str:
    lines = _frame("menu", reset).split("\n")
    start = next(i for i, l in enumerate(lines) if "1. Stop and wait" in l)
    end = next(i for i, l in enumerate(lines) if "Enter to confirm" in l) - 1
    return "\n".join(lines[:start] + options + lines[end:])


def test_credits_are_never_chosen(rig):
    """A server flag puts the credits options first, where the pointer starts.
    Enter there would choose credits, so no key at all."""
    r = rig()
    hit, reset = _past_reset()
    r.show(
        _menu(
            reset,
            [
                "   ❯ 1. Switch to usage credits",
                "     2. Stop and wait for limit to reset",
                "     3. Upgrade your plan",
            ],
        )
    )
    r.hook_record(hit)
    r.tick()
    r.tick()
    assert r.keys() == []
    assert 'pointer is not on "Stop and wait for limit to reset"; no keys' in r.log()
    assert not r.data(".limit-resumed").exists()


def test_another_menu_gets_no_keys(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(
        _frame("menu", reset).replace(
            "What do you want to do?", "Do you want to proceed?"
        )
    )
    r.hook_record(hit)
    r.tick()
    assert r.keys() == []
    assert "a dialog other than the usage-limit menu holds the screen" in r.log()


def test_a_menu_without_the_exact_wait_label_gets_no_keys(rig):
    r = rig()
    hit, reset = _past_reset()
    r.show(_menu(reset, ["   ❯ 1. Stop", "     2. Switch to usage credits"]))
    r.hook_record(hit)
    r.tick()
    assert r.keys() == []


def test_a_box_holding_text_gets_no_keys(rig):
    """keepalive reads a held box as HELD first, so LIMIT never types into it."""
    r = rig()
    hit, reset = _past_reset()
    r.show(_frame("held", reset))
    (r.stub_dir / "typed").write_text("a half-typed message")
    r.hook_record(hit)
    r.tick()
    assert r.keys() == []
    assert "HELD" in r.log() and "LIMIT" not in r.log()


# --- the record on the plane ------------------------------------------------------------


def test_the_resume_is_a_plane_event_and_the_tick_a_limit_sample(
    tmp_path, scratch_plane_env
):
    from tests.conftest import read_fleet_events
    from tests.test_plane_keepalive_door import _replay_pending, _samples

    r = Rig(tmp_path, {}, armed=True)
    (tmp_path / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    r.env.update(scratch_plane_env(tmp_path, initialize=True))
    hit, reset = _past_reset()
    r.show(_frame("held", reset), on_submit=_frame("resumed", reset))
    r.hook_record(hit)
    r.tick()
    deadline = time.monotonic() + 20
    rows: list[dict] = []
    while time.monotonic() < deadline:
        # The heartbeat emit is backgrounded, so a batch can be staged after
        # the replay: replay and read again until a read sees no staged batch.
        _replay_pending(tmp_path)
        try:
            text = read_fleet_events(tmp_path, allow_absent=True)
        except AssertionError:
            time.sleep(0.2)
            continue
        rows = [json.loads(l) for l in text.splitlines()]
        if any(row.get("type") == "keepalive_limit_resume" for row in rows):
            break
        time.sleep(0.2)
    event = next(row for row in rows if row.get("type") == "keepalive_limit_resume")
    assert event["source"] == "keepalive" and event["bot"] == "b1"
    assert event["data"]["outcome"] == "submitted" and event["data"]["menu"] == "none"
    assert event["data"]["reset"] == reset and event["data"]["limit"] == "session limit"
    samples = []
    while time.monotonic() < deadline and not samples:
        samples = [s for s in _samples(tmp_path) if s["metric"] == "bot.heartbeat"]
        time.sleep(0.2)
    assert json.loads(samples[0]["value"])["state"] == "LIMIT"
