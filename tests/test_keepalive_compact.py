"""keepalive types /compact for a pending compaction request (#1053, build PR 4).

Every test drives the REAL keepalive.sh tick (the real lib-common.sh,
compact-guard.py and compaction-slot.py) against a stateful tmux stub. The stub
serves frames captured from a real Claude Code 2.1.319 pane, 80x24 under the
composed display settings (tests/fixtures/pane-states/compact-*.txt), records
every key it is sent, and moves the frame when an Enter submits typed text.
Plane rows are caught by a stand-in for emit_fleet_event, so each row's data is
read back and parsed as JSON.

The plan's ten tests (shared plan, "The PRs", item 4) are marked by number.
Each fails on main, whose keepalive acts on no request: the tests that expect
no keys also read the executor's own record of why.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fixtures.native_admission import admit_watchdog_fixture

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"
FIXTURES = REPO / "tests/fixtures/pane-states"
DOOR_FILES = (
    "keepalive.sh",
    "supervisor.sh",
    "plane-emit.sh",
    "plane-socket-client.py",
    "plane-readers.py",
    "usage-limit.py",
    "compact-guard.py",
    "compaction-slot.py",
)

STUB = r'''#!/usr/bin/env python3
"""A tmux that serves one pane from STUB_DIR and records what it is sent.
A file `queue` lists frame files, one per line: each capture takes the next one
as the frame, and the last stays."""
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
    queue = [q for q in rd("queue").split("\n") if q]
    if queue:
        wr("frame", rd(queue[0]))
        wr("queue", "\n".join(queue[1:]))
    frame, typed = rd("frame"), rd("typed")
    if typed:
        lines = frame.split("\n")
        for i, l in enumerate(lines):
            if l.rstrip() in ("❯", "❯ "):
                lines[i] = "❯ " + typed
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
        # A file `fault` reading "fail TEXT" fails that keystroke; "drop TEXT"
        # takes it and never shows it in the box.
        mode, _, target = rd("fault").strip().partition(" ")
        if text == target and mode == "fail":
            sys.exit(1)
        if text != target or mode != "drop":
            wr("typed", rd("typed") + text)
        sys.exit(0)
    for key in rest:
        log("KEY " + key)
        if key == "Enter" and rd("typed"):
            wr("frame", rd("on_submit", rd("frame")))
            wr("typed", "")
    sys.exit(0)
sys.exit(0)
'''

# A stand-in for emit_fleet_event: every row the tick records, as
# "type<TAB>source<TAB>data" lines, so a test reads each row's data back.
RECORDER = r'''. "{real}"
emit_fleet_event() {{
    local data="${{3:-}}"
    [ -n "$data" ] || data='{{}}'
    printf '%s\t%s\t%s\n' "$1" "$2" "$data" >> "$COMPACT_TEST_EVENTS"
    return 0
}}
'''

_DEFAULT = object()
ENGINE = '#!/bin/bash\n[ "$1" = version ] || exit 2\n{body}\n'


def _frame(name: str) -> str:
    return (FIXTURES / f"compact-{name}.txt").read_text(encoding="utf-8")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Rig:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.lib = tmp_path / "lib"
        self.lib.mkdir()
        for name in DOOR_FILES:
            (self.lib / name).symlink_to(LIB / name)
        (self.lib / "lib-common.sh").write_text(RECORDER.format(real=LIB / "lib-common.sh"))
        admit_watchdog_fixture(self.lib)
        self.stub_dir = tmp_path / "stub"
        self.stub_dir.mkdir()
        stub = tmp_path / "tmux"
        stub.write_text(STUB)
        stub.chmod(0o755)
        self.bot = tmp_path / "bots" / "b1"
        self.data = self.bot / "data"
        self.data.mkdir(parents=True)
        self.account = tmp_path / "account"
        self.conf = {"CLAUDRON_SESSION_LOOP": "0", "CLAUDNA_PRECOMPACT_REFLECT": "0"}
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        for name in ("systemctl", "launchctl"):
            native = self.bindir / name
            native.write_text("#!/bin/bash\nexit 0\n")
            native.chmod(0o755)
        (tmp_path / "state" / "pane-send").mkdir(parents=True)
        self.proc = tmp_path / "proc"
        self.proc.mkdir()
        self.load(1.0)
        self.deliveries = tmp_path / "deliveries.json"
        self.deliveries.write_text("[]")
        self.events = tmp_path / "events.tsv"
        self.env = {
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "TMUX_BIN": str(stub),
            "STUB_DIR": str(self.stub_dir),
            "CLAUDLOBBY_ROOT": str(tmp_path),
            "PLANE_EMIT_DISABLED": "1",
            "PANE_SEND_SETTLE_S": "0",
            "PANE_SEND_CHUNK_SETTLE_S": "0",
            "COMPACT_TEST_EVENTS": str(self.events),
            "COMPACT_GUARD_DELIVERIES": str(self.deliveries),
            "COMPACTION_SLOT_PROC": str(self.proc),
            "COMPACTION_SLOT_BOOT_ID": "boot-test",
        }
        self.now = int(time.time())
        self.transcripts = self.account / "projects" / re.sub(
            r"[^A-Za-z0-9]", "-", os.path.realpath(self.bot))
        self.transcripts.mkdir(parents=True)
        self.session = self.transcripts / "s1.jsonl"
        self._rows([
            {"type": "user", "entrypoint": "cli", "timestamp": _iso(self.now - 9000), "message": {}},
            {"type": "assistant", "timestamp": _iso(self.now - 600), "message": {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 450000,
                "cache_creation_input_tokens": 0, "output_tokens": 5}}},
        ])
        self.markers(turn_ended=self.now - 30, turn_input=self.now - 90, tool=self.now - 300)
        self.show("idle-before-turn")

    # --- the bot -------------------------------------------------------------
    def write_conf(self) -> None:
        lines = ['BOT_NAME="b1"', 'FLEET_NAME="kfleet"', 'BOT_SERVICE="com.k.b1"',
                 f'export CLAUDE_CONFIG_DIR="{self.account}"']
        lines += [f"export {k}={v}" for k, v in self.conf.items() if v is not None]
        (self.bot / "bot.conf").write_text("\n".join(lines) + "\n")

    def request(self, *, ago: int = 60, by: str = "bot:kfleet/b1", request_id: str = "req-1",
                epoch=_DEFAULT) -> None:
        epoch = self.now - ago if epoch is _DEFAULT else epoch
        record = {"by": by, "context": {"at": None, "reason": None, "tokens": 450010},
                  "request_id": request_id,
                  "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - ago)),
                  "requested_epoch": epoch}
        (self.data / ".compact-request").write_text(json.dumps(record, sort_keys=True) + "\n")

    def markers(self, *, turn_ended=None, turn_input=None, tool=None) -> None:
        for name, at in ((".turn-ended", turn_ended), (".turn-input", turn_input),
                         (".last-tool-call", tool)):
            if at is not None:
                path = self.data / name
                path.touch()
                os.utime(path, (at, at))

    def _rows(self, rows) -> None:
        with open(self.session, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def boundary(self, at: float, pre: int = 450010, post: int = 21000) -> None:
        self._rows([{"type": "system", "subtype": "compact_boundary", "timestamp": _iso(at),
                     "uuid": "u-1", "sessionId": "s1",
                     "compactMetadata": {"trigger": "manual", "preTokens": pre,
                                         "postTokens": post}}])

    def load(self, load1: float) -> None:
        (self.proc / "loadavg").write_text(f"{load1:.2f} 1.00 1.00 1/100 42\n")

    def engine(self, body: str) -> None:
        """A stand-in Claudron on PATH, and a composed PreCompact hook naming it."""
        exe = self.bindir / "claudron"
        exe.write_text(ENGINE.format(body=body))
        exe.chmod(0o755)
        settings = self.bot / ".claude" / "settings.local.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"hooks": {"PreCompact": [{"matcher": "", "hooks": [
            {"type": "command", "command": "claudron --vault /v hook pre-compact"}]}]}}))

    # --- the pane ------------------------------------------------------------
    def show(self, name: str, *, on_submit: str = "compacting", text: str | None = None) -> None:
        (self.stub_dir / "frame").write_text(_frame(name) if text is None else text)
        (self.stub_dir / "typed").write_text("")
        (self.stub_dir / "on_submit").write_text(_frame(on_submit))
        (self.stub_dir / "queue").write_text("")

    def queue(self, *texts: str) -> None:
        names = []
        for i, text in enumerate(texts):
            (self.stub_dir / f"q{i}").write_text(text)
            names.append(str(self.stub_dir / f"q{i}"))
        (self.stub_dir / "queue").write_text("\n".join(names))

    # --- one tick ------------------------------------------------------------
    def tick(self) -> subprocess.CompletedProcess:
        self.write_conf()
        r = subprocess.run(["bash", str(self.lib / "keepalive.sh"), str(self.bot)],
                           capture_output=True, text=True, env=self.env, timeout=120)
        assert r.returncode == 0, r.stderr
        return r

    def keys(self) -> list[str]:
        p = self.stub_dir / "keys.log"
        return p.read_text().splitlines() if p.exists() else []

    def compact_log(self) -> list[str]:
        p = self.bot / "keepalive.log"
        return [l for l in (p.read_text().splitlines() if p.exists() else []) if " COMPACT " in l]

    def rows(self, kind: str | None = None) -> list[dict]:
        out = []
        if self.events.exists():
            for line in self.events.read_text().splitlines():
                event, _source, data = line.split("\t", 2)
                if kind is None or event == kind:
                    out.append({"type": event, "data": json.loads(data)})
        return out

    def slot(self) -> dict:
        r = subprocess.run(["python3", "-S", "-E", str(LIB / "compaction-slot.py"), "status", "--json"],
                           capture_output=True, text=True, env=self.env, timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def pending(self) -> bool:
        return (self.data / ".compact-request").exists()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _sent(keys: list[str]) -> int:
    return sum(1 for k in keys if k == "TEXT /compact")


def _waits(rig: Rig, why: str) -> bool:
    return any(f"waits: {why}" in line for line in rig.compact_log())


# --- the one send, and its record -------------------------------------------------

def test_an_idle_empty_prompt_gets_one_compact_and_its_row(rig):
    rig.request()
    rig.tick()
    assert rig.keys() == ["TEXT /compact", "KEY Enter"]
    assert not rig.pending()
    [row] = rig.rows("compaction_sent")
    data = row["data"]
    assert data["request_id"] == "req-1" and data["by"] == "bot:kfleet/b1"
    assert data["guard"]["readings"]["deliveries"] == {"read": "ok", "since_request": []}
    assert data["guard"]["readings"]["precompact"]["refuses"] is False
    slot = rig.slot()
    assert slot["state"] == "held" and slot["lease"]["bot"] == "b1"
    assert data["lease"] == slot["lease"]["lease"]
    assert (rig.data / ".compact-sent").read_text().split("\t")[2] == "req-1"


def test_no_request_no_keys_and_no_record(rig):
    rig.tick()
    assert rig.keys() == [] and rig.rows() == [] and rig.compact_log() == []


# --- 1: a running turn whose activity line is above keepalive's ten lines ---------

def _running_turn_with_task_list() -> str:
    """The real running-turn frame with four task items drawn under its activity
    line and the footer's "esc to interrupt" left off, as Claude Code 2.1.285
    drew it in most running turns (lib-common.sh, _BUSY_PATTERN_BASE). 2.1.319
    gives no task list to the models a bot runs, so this one is built."""
    lines = _frame("running-turn").split("\n")
    at = next(i for i, l in enumerate(lines) if l.startswith("● Generating"))
    tasks = ["  ⎿  ☐ Read the plan", "     ☐ Write the guard", "     ☐ Run the tests",
             "     ☐ Report", ""]
    lines[at + 1:at + 1] = tasks
    text = "\n".join(lines).replace(" · esc to interrupt", "")
    return "\n".join(text.split("\n")[-24:])


def test_1_a_running_turn_with_a_task_list_gets_no_keys(rig):
    frame = _running_turn_with_task_list()
    # The case: keepalive's own read of the last ten lines calls it IDLE.
    r = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; eval "$(sed -n "/^classify_pane()/,/^}}/p" "{LIB}/keepalive.sh")"; classify_pane "$(printf "%s" "$1" | tail -10)"', "_", frame],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, timeout=30)
    assert r.stdout.strip() == "IDLE", r.stdout + r.stderr
    rig.request()
    rig.show("idle-before-turn", text=frame)
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    assert _waits(rig, "screen busy")


@pytest.mark.parametrize("name", ["running-turn", "running-four-subagents", "at-stop"])
def test_1_a_running_turn_from_the_real_pane_gets_no_keys(rig, name):
    rig.request()
    rig.show(name)
    rig.tick()
    assert rig.keys() == [] and rig.pending() and _waits(rig, "screen busy")


# --- 2: a held box and a menu ---------------------------------------------------

@pytest.mark.parametrize("name,why", [("held-box", "held"), ("question-menu", "menu"),
                                      ("typed", "held")])
def test_2_a_held_box_or_a_menu_gets_no_keys(rig, name, why):
    rig.request()
    rig.show(name)
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    assert _waits(rig, f"screen {why}")


# --- 3: an input or a tool call newer than the turn's end -----------------------

@pytest.mark.parametrize("markers,why", [
    ({"turn_input": "+5"}, "input_after_turn_end"),
    ({"tool": "+5"}, "tool_call_after_turn_end"),
    ({"turn_ended": "-3"}, "turn_ended_"),
])
def test_3_markers_that_do_not_say_idle_get_no_keys(rig, markers, why):
    rig.request()
    base = rig.now - 30
    rig.markers(turn_ended=base)
    if "turn_input" in markers:
        rig.markers(turn_input=base + 5)
    if "tool" in markers:
        rig.markers(tool=base + 5)
    if "turn_ended" in markers:
        rig.markers(turn_ended=int(time.time()) - 3)
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    assert _waits(rig, why), rig.compact_log()


def test_3_no_turn_end_marker_gets_no_keys(rig):
    rig.request()
    (rig.data / ".turn-ended").unlink()
    rig.tick()
    assert rig.keys() == [] and rig.pending() and _waits(rig, "no_turn_end")


# --- 4: a frame that changes between the two reads ------------------------------

@pytest.mark.parametrize("first,second", [
    ("delivery-a-idle", "delivery-b-typing"),    # a delivery starts typing
    ("idle-before-turn", "turn-end-b-idle"),     # both idle, but not the same frame
])
def test_4_a_frame_that_changes_under_the_lock_gets_no_keys(rig, first, second):
    rig.request()
    rig.show(first)
    # keepalive's own classification reads the pane first, then the executor's
    # tick read, then the read under the lock.
    rig.queue(_frame(first), _frame(first), _frame(second))
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "frame_changed" and row["data"]["request"] == "kept"
    assert rig.slot()["state"] == "free"   # the slot it took is given back


# --- 5: a delivery since the request --------------------------------------------

def test_5_a_delivery_after_the_request_drops_it_with_its_row(rig):
    rig.request(ago=60)
    rig.deliveries.write_text(json.dumps([
        {"msg_id": "msg_before", "occurred_at": _iso(rig.now - 120)},
        {"msg_id": "msg_after", "occurred_at": _iso(rig.now - 20)}]))
    rig.tick()
    assert rig.keys() == [] and not rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "delivered_since_request"
    assert row["data"]["request"] == "dropped"
    assert [d["msg_id"] for d in row["data"]["readings"]["deliveries"]["since_request"]] == ["msg_after"]


def test_5_a_delivery_before_the_request_does_not_drop_it(rig):
    rig.request(ago=60)
    rig.deliveries.write_text(json.dumps([{"msg_id": "m0", "occurred_at": _iso(rig.now - 120)}]))
    rig.tick()
    assert _sent(rig.keys()) == 1 and rig.rows("compaction_skipped") == []


def test_5_a_plane_it_cannot_read_keeps_the_request_and_sends_nothing(rig):
    del rig.env["COMPACT_GUARD_DELIVERIES"]   # and no plane under the root
    rig.request()
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "plane_unread" and row["data"]["request"] == "kept"


# --- 6: the host slot -----------------------------------------------------------

def test_6_load1_at_40_gets_no_keys_and_keeps_the_request(rig):
    rig.request()
    rig.load(40.0)
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "slot_load" and row["data"]["slot"]["load1"] == 40.0


def test_6_load1_under_40_sends(rig):
    rig.request()
    rig.load(39.9)
    rig.tick()
    assert _sent(rig.keys()) == 1


def test_6_another_bots_live_lease_gets_no_keys_and_keeps_the_request(rig):
    r = subprocess.run(["python3", "-S", "-E", str(LIB / "compaction-slot.py"), "take",
                        "--fleet", "kfleet", "--bot", "other", "--json"],
                       capture_output=True, text=True, env=rig.env, timeout=30)
    assert r.returncode == 0, r.stderr
    rig.request()
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "slot_held" and row["data"]["slot"]["lease"]["bot"] == "other"
    rig.tick()   # once per reason per request
    assert len(rig.rows("compaction_skipped")) == 1


def test_6_the_operators_stop_gets_no_keys_and_keeps_the_request_until_it_goes(rig):
    stop = rig.tmp / "state" / "compaction-slot" / "disabled"
    stop.parent.mkdir(parents=True)
    stop.touch()
    rig.request()
    rig.tick()
    rig.tick()   # once per reason per request
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "slot_disabled" and row["data"]["request"] == "kept"
    assert row["data"]["slot"]["stop"]["path"] == str(stop)
    assert os.listdir(stop.parent) == ["disabled"]   # a refused take writes nothing
    stop.unlink()
    rig.tick()
    assert rig.keys() == ["TEXT /compact", "KEY Enter"] and not rig.pending()
    [sent] = rig.rows("compaction_sent")
    assert sent["data"]["request_id"] == "req-1"


# --- 7: a request 2 hours old ---------------------------------------------------

def test_7_a_request_over_2_hours_old_expires_with_a_row(rig):
    rig.request(ago=7200)
    os.utime(rig.data / ".compact-request")  # a fresh file time does not renew it
    rig.tick()
    assert rig.keys() == [] and not rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "request_expired" and row["data"]["request"] == "dropped"


def test_7_a_request_just_under_2_hours_old_still_acts(rig):
    rig.request(ago=7100)
    rig.tick()
    assert _sent(rig.keys()) == 1


@pytest.mark.parametrize("epoch", ["1794100000", None, 1.5, True])
def test_7_a_request_whose_epoch_is_not_an_integer_expires(rig, epoch):
    rig.request(epoch=epoch)
    rig.tick()
    assert rig.keys() == [] and not rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "request_unreadable"


# --- 8: one send, and no second while it waits for its proof ---------------------

def test_8_one_send_one_row_and_no_second_send_while_the_proof_is_awaited(rig):
    rig.request(request_id="req-1")
    rig.tick()
    assert _sent(rig.keys()) == 1
    # The pane reads idle again and a second request is pending, but no boundary
    # row has come: the send is still in flight, so nothing more is typed.
    rig.show("compacted-idle")
    rig.request(request_id="req-2")
    rig.markers(turn_ended=int(time.time()) - 30)
    rig.tick()
    assert _sent(rig.keys()) == 1
    assert len(rig.rows("compaction_sent")) == 1
    assert any("waiting for its boundary row" in line for line in rig.compact_log())


def test_8_a_compacting_frame_gets_no_keys(rig):
    rig.request()
    rig.show("compacting")
    rig.tick()
    assert rig.keys() == [] and _waits(rig, "screen busy")


# --- 9: the proof -----------------------------------------------------------------

def test_9_a_boundary_row_after_the_send_proves_it_with_its_sizes(rig):
    rig.request()
    rig.tick()
    assert _sent(rig.keys()) == 1
    rig.boundary(time.time() + 1, pre=452000, post=20500)
    rig.tick()
    [row] = rig.rows("compaction_proven")
    assert row["data"]["pre_tokens"] == 452000 and row["data"]["post_tokens"] == 20500
    assert row["data"]["trigger"] == "manual" and row["data"]["request_id"] == "req-1"
    assert not (rig.data / ".compact-sent").exists()
    assert rig.slot()["state"] == "free"


def test_9_a_boundary_row_older_than_the_send_proves_nothing(rig):
    rig.request()
    rig.tick()
    rig.boundary(time.time() - 3600)
    rig.tick()
    assert rig.rows("compaction_proven") == [] and (rig.data / ".compact-sent").exists()


def test_9_no_boundary_in_10_minutes_is_not_proven_and_frees_the_slot(rig):
    rig.request()
    rig.tick()
    sent = rig.data / ".compact-sent"
    fields = sent.read_text().split("\t")
    fields[0] = str(int(time.time()) - 601)
    sent.write_text("\t".join(fields))
    rig.tick()
    [row] = rig.rows("compaction_not_proven")
    assert row["data"]["request_id"] == "req-1" and row["data"]["waited_s"] >= 600
    assert not sent.exists() and rig.slot()["state"] == "free"
    assert _sent(rig.keys()) == 1   # nothing resends


# --- 10: a composed PreCompact hook that refuses the first compaction ------------

def test_10_a_loop_bot_on_an_engine_under_0_10_0_gets_no_keys_and_one_row(rig):
    rig.conf["CLAUDRON_SESSION_LOOP"] = "1"
    rig.engine("echo claudron 0.6.1")
    rig.request()
    rig.tick()
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    data = row["data"]
    assert data["reason"] == "precompact_hook_refuses" and data["hook"] == "claudron"
    assert data["readings"]["precompact"]["engine"]["version"] == "0.6.1"


def test_10_a_loop_bot_on_engine_0_10_0_sends(rig):
    rig.conf["CLAUDRON_SESSION_LOOP"] = "1"
    rig.engine("echo claudron 0.10.0")
    rig.request()
    rig.tick()
    assert _sent(rig.keys()) == 1
    [row] = rig.rows("compaction_sent")
    assert row["data"]["guard"]["readings"]["precompact"]["engine"]["version"] == "0.10.0"


@pytest.mark.parametrize("case,reason,hook", [
    ("unreadable_version", "precompact_hook_refuses", "claudron"),
    ("claudna_armed", "precompact_hook_refuses", "claudna"),
    ("no_loop_line", "precompact_hooks_unknown", None),
])
def test_10_an_unknown_or_refusing_hook_gets_no_keys(rig, case, reason, hook):
    if case == "unreadable_version":
        rig.conf["CLAUDRON_SESSION_LOOP"] = "1"
        rig.engine("exit 1")
    elif case == "claudna_armed":
        rig.conf["CLAUDNA_PRECOMPACT_REFLECT"] = "1"
    else:
        rig.conf["CLAUDRON_SESSION_LOOP"] = None
    rig.request()
    rig.tick()
    assert rig.keys() == [] and rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == reason and row["data"]["hook"] == hook


# --- the request's other guards ---------------------------------------------------

def test_a_compaction_since_the_request_drops_it(rig):
    rig.request(ago=60)
    rig.boundary(rig.now - 30)
    rig.tick()
    assert rig.keys() == [] and not rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "compacted_since_request"


def test_a_close_request_under_the_floor_is_dropped_and_one_over_it_sends(rig):
    rig.request(by="assignment:asg_1")
    rig.env["COMPACT_AT_CLOSE_TOKENS"] = "500000"   # the context reads 450010
    rig.tick()
    assert rig.keys() == [] and not rig.pending()
    [row] = rig.rows("compaction_skipped")
    assert row["data"]["reason"] == "below_floor" and row["data"]["readings"]["context"]["tokens"] == 450010
    rig.request(by="assignment:asg_2", request_id="req-2")
    rig.env["COMPACT_AT_CLOSE_TOKENS"] = "450010"
    rig.tick()
    assert _sent(rig.keys()) == 1


def test_a_usage_limit_tick_sends_nothing(rig):
    rig.request()
    hit = int(time.time()) - 60
    (rig.data / ".usage-limit").write_text(f"{hit}\nYou've hit your session limit\n")
    os.utime(rig.data / ".last-tool-call", (hit - 600, hit - 600))
    rig.show("idle-before-turn", text=(FIXTURES / "usage-limit-held.txt").read_text())
    rig.tick()
    assert rig.keys() == [] and rig.compact_log() == [] and rig.pending()
    (rig.data / ".usage-limit").unlink()   # the same pane with no limit held sends
    rig.show("idle-before-turn")
    rig.tick()
    assert _sent(rig.keys()) == 1


@pytest.mark.parametrize("fault", [
    None,                     # the control: both commands go through
    "fail /reload-skills",    # its keystroke fails
    "drop /reload-skills",    # the box never shows it, so its Enter is withheld
    "fail /reload-plugins",   # the first command's keystroke fails
])
def test_a_tick_that_tries_a_reload_sends_no_compact_whatever_the_reload_returns(rig, fault):
    """An idle tick types a pending fleet reload. A tick that tries it types no
    /compact, whether or not its commands go through: one that did not submit can
    sit in the box, or arrive there, under the /compact."""
    rig.request()
    (rig.data / ".reload-pending").touch()
    rig.show("idle-before-turn", on_submit="idle-before-turn")
    (rig.stub_dir / "fault").write_text(fault or "")
    rig.tick()
    keys = rig.keys()
    if fault is None:
        assert keys == ["TEXT /reload-plugins", "KEY Enter", "TEXT /reload-skills", "KEY Enter"]
    assert keys[:1] == ["TEXT /reload-plugins"] and _sent(keys) == 0, keys
    assert rig.compact_log() == [] and rig.rows("compaction_sent") == [] and rig.pending()
    assert (rig.data / ".reload-pending").exists() is (fault is not None)
    (rig.stub_dir / "fault").write_text("")   # the fault clears
    if fault is not None:
        rig.tick()                             # the reload goes through, and no /compact
        assert _sent(rig.keys()) == 0 and not (rig.data / ".reload-pending").exists()
    rig.tick()                                 # the next idle tick sends it
    assert _sent(rig.keys()) == 1 and not rig.pending()


# --- the interpreter keepalive runs the guard with ------------------------------------

def test_the_guards_package_modules_load_under_the_hosts_system_python():
    """keepalive runs compact-guard.py as `python3 -S -E`, the host's system Python:
    3.9 on macOS (Apple's /usr/bin/python3), where an `X | None` annotation evaluated
    when its function is defined raises TypeError. Each module the guard loads defers
    its annotations, so none is evaluated at import (a run on macOS with
    transcript_usage.py lacking this read every tick as a failed guard)."""
    import ast
    for rel in ("claudlobby/transcript_usage.py", "claudlobby/compact_request.py",
                "claudlobby/_runtime_scripts/compact-guard.py"):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
        assert any(a.name == "annotations" for n in futures for a in n.names), rel
    src = (LIB / "compact-guard.py").read_text(encoding="utf-8")
    assert set(re.findall(r'_package_module\("([a-z_]+)"\)', src)) == {"compact_request", "transcript_usage"}


# --- the whole-screen test on every captured frame --------------------------------

READY = {"idle-before-turn", "turn-end-b-idle", "delivery-a-idle", "compacted-idle"}
VETO = {
    "running-turn": "busy", "running-four-subagents": "busy", "at-stop": "busy",
    "at-pre-tool-use": "busy", "queued-during-turn": "busy", "at-queued-user-prompt-submit": "busy",
    "turn-end-a-busy": "busy", "compacting": "busy", "compacting-queued": "busy",
    "at-post-compact": "busy", "question-menu": "menu", "held-box": "held", "typed": "held",
    "at-user-prompt-submit": "held", "delivery-b-typing": "held", "at-pre-compact": "held",
    "compacting-typed-text": "held",
}


@pytest.mark.parametrize("ambient", ["C", "C.UTF-8"])
@pytest.mark.parametrize("name", sorted(READY | set(VETO)))
def test_the_whole_screen_test_reads_each_real_frame_under_any_ambient_locale(name, ambient):
    assert len(READY | set(VETO)) == 21
    r = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; if pane_compact_ready "$1"; then echo " rc=0"; else echo " rc=$?"; fi', "_",
                        _frame(name)], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "LC_ALL": ambient})
    want = "rc=0" if name in READY else f"{VETO[name]} rc=1"
    assert r.stdout.strip() == want, (name, r.stdout, r.stderr)
