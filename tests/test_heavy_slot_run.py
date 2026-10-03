"""The heavy-job slot itself (#1686): the wrapper that holds the host lock
while a heavy job runs, the record it leaves, and the one-line status door.

Driven through the real wrapper, executed by its path as the rewritten command
executes it (so the execute bit and the shebang are pinned too), with a stub
`pytest` that records that it ran and waits for a file. Every test gets its own
slot directory (HEAVY_SLOT_DIR) and writes its events to a file
(HEAVY_SLOT_EVENTS_FILE), so nothing here reaches the host's slots or plane.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py"

# The stub job: records that it ran, traps TERM, waits for a file when asked,
# exits with a chosen code.
STUB = """#!/bin/bash
trap 'touch "$STUB_DIR/term"; exit 143' TERM
touch "$STUB_DIR/ran.$$"
if [ -n "${STUB_WAIT:-}" ]; then
  while [ ! -e "$STUB_WAIT" ]; do sleep 0.05; done
fi
exit "${STUB_RC:-0}"
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
        HEAVY_SLOT_BOOT_ID="this-boot",
        FLEET_NAME="testfleet",
        BOT_ID="alpha",
        STUB_DIR=tmp_path,
        PLANE_EMIT_DISABLED="1",
        TZ="UTC",
    )
    ns = SimpleNamespace(
        env=env,
        tmp=tmp_path,
        state=tmp_path / "state",
        events=tmp_path / "events.jsonl",
    )
    yield ns
    # never leave a stub waiting behind a test
    (tmp_path / "release").touch()
    (tmp_path / "never").touch()


def _env(se, **extra) -> dict:
    return {**se.env, **{k: str(v) for k, v in extra.items()}}


def _run(se, *argv, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), "run", "--", *argv],
        env=_env(se, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _start(se, *argv, **extra) -> subprocess.Popen:
    return subprocess.Popen(
        [str(WRAPPER), "run", "--", *argv],
        env=_env(se, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _status(se, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), "status", *args],
        env=se.env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _wait_for(pred, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _ran(where: Path) -> list[Path]:
    return sorted(where.glob("ran.*"))


def _record(se, slot: int = 0) -> dict:
    return json.loads((se.state / f"slot-{slot}.lock").read_text())


def _events(se) -> list[dict]:
    if not se.events.exists():
        return []
    return [json.loads(line) for line in se.events.read_text().splitlines() if line]


def _hold(se, **extra) -> subprocess.Popen:
    """A holder: the stub job, waiting on `release`, already running."""
    stub_dir = Path(extra.pop("STUB_DIR", se.tmp))
    p = _start(
        se, "pytest", "-q", STUB_WAIT=se.tmp / "release", STUB_DIR=stub_dir, **extra
    )
    _wait_for(lambda: _ran(stub_dir))
    return p


class TestTheSlot:
    def test_the_holder_is_recorded_while_the_job_runs(self, se):
        p = _hold(se)
        rec = _record(se)
        assert rec["state"] == "held"
        assert (rec["fleet"], rec["bot"]) == ("testfleet", "alpha")
        assert rec["shape"] == "pytest -q" and "command" not in rec
        assert rec["pid"] == p.pid and rec["slot"] == 0
        assert rec["boot_id"] == "this-boot" and rec["started_at"].endswith("Z")
        (se.tmp / "release").touch()
        assert p.wait(15) == 0

    def test_the_release_is_recorded_with_the_exit_code(self, se):
        assert _run(se, "pytest", STUB_RC=3).returncode == 3
        rec = _record(se)
        assert rec["state"] == "released" and rec["exit"] == 3
        assert rec["released_at"].endswith("Z") and rec["duration_s"] >= 0

    def test_a_second_job_is_refused_naming_the_holder(self, se):
        p = _hold(se)
        second = se.tmp / "second"
        second.mkdir()
        r = _run(se, "pytest", BOT_ID="beta", STUB_DIR=second)
        assert r.returncode == 75
        assert "NOT RUN" in r.stderr and "(1 of 1)" in r.stderr
        assert "testfleet/alpha" in r.stderr and "pytest" in r.stderr
        assert "pytest -q" not in r.stderr  # the refusal names the tool, never the command
        assert _ran(second) == []  # the refused job never started
        (se.tmp / "release").touch()
        p.wait(15)

    def test_a_killed_holder_frees_the_slot_at_once(self, se):
        # The kernel drops the lock with its holder, so nothing can wedge the
        # slot; the job it orphans keeps running WITHOUT the slot, which shows
        # the lock belongs to the wrapper, never to the job's own processes.
        p = _start(se, "pytest", STUB_WAIT=se.tmp / "never")
        _wait_for(lambda: _ran(se.tmp))
        p.kill()
        p.wait(15)
        other = se.tmp / "other"
        other.mkdir()
        r = _run(se, "pytest", STUB_DIR=other)
        assert r.returncode == 0 and _ran(other)
        (ev,) = [e for e in _events(se) if e["type"] == "heavy_slot_unreleased"]
        assert ev["data"]["across_reset"] is False
        assert ev["data"]["previous"]["pid"] == p.pid

    def test_an_unreleased_hold_from_an_earlier_boot_is_reset_evidence(self, se):
        # #1644: what was running when the host reset. The record survives the
        # reset on disk; the next holder reports it before overwriting it.
        se.state.mkdir(parents=True)
        (se.state / "slot-0.lock").write_text(
            json.dumps(
                {
                    "v": 1,
                    "slot": 0,
                    "state": "held",
                    "fleet": "f",
                    "bot": "b",
                    "command": "npm ci",
                    "pid": 999999,
                    "boot_id": "an-earlier-boot",
                    "started_at": "2026-09-29T14:30:00Z",
                }
            )
        )
        assert _run(se, "pytest").returncode == 0
        (ev,) = [e for e in _events(se) if e["type"] == "heavy_slot_unreleased"]
        assert ev["data"]["across_reset"] is True
        assert ev["data"]["previous"]["shape"] == "npm ci"
        assert _record(se)["state"] == "released"

    def test_two_slots_admit_two_holders_and_refuse_a_third(self, se):
        se.state.mkdir(parents=True)
        (se.state / "slots").write_text("2\n")
        a, b = se.tmp / "a", se.tmp / "b"
        a.mkdir()
        b.mkdir()
        p1 = _hold(se, STUB_DIR=a)
        p2 = _hold(se, STUB_DIR=b, BOT_ID="beta")
        r = _run(se, "pytest", BOT_ID="gamma")
        assert r.returncode == 75 and "(2 of 2)" in r.stderr
        assert "testfleet/alpha" in r.stderr and "testfleet/beta" in r.stderr
        (se.tmp / "release").touch()
        p1.wait(15)
        p2.wait(15)

    def test_a_signal_reaches_the_job_and_the_release_is_still_recorded(self, se):
        p = _start(se, "pytest", STUB_WAIT=se.tmp / "never")
        _wait_for(lambda: _ran(se.tmp))
        p.send_signal(signal.SIGTERM)
        rc = p.wait(15)
        assert (se.tmp / "term").exists()
        assert rc in (143, -signal.SIGTERM)
        assert _record(se)["state"] == "released"

    def test_a_signal_before_the_job_starts_is_still_forwarded_and_released(self, se):
        # The wrapper holds the slot from the moment it writes the record, so a
        # TERM from then on must reach the job and leave a release. Found at
        # load 18: with the handler installed after the job started, a TERM in
        # between killed the wrapper outright. The seam holds that window open.
        p = _start(se, "pytest", STUB_WAIT=se.tmp / "never", HEAVY_SLOT_START_DELAY_S=3)
        _wait_for(lambda: (se.state / "slot-0.lock").exists()
                  and _record(se).get("state") == "held")
        p.send_signal(signal.SIGTERM)
        rc = p.wait(15)
        assert rc in (143, -signal.SIGTERM)
        rec = _record(se)
        assert rec["state"] == "released" and rec["exit"] == 143

    def test_a_command_that_is_not_found_is_released_with_127(self, se):
        r = _run(se, str(se.tmp / "missing" / "pytest"))
        assert r.returncode == 127
        rec = _record(se)
        assert rec["state"] == "released" and rec["exit"] == 127

    def test_the_wrapper_runs_only_heavy_jobs(self, se):
        r = _run(se, "ls")
        assert r.returncode == 2 and "not a heavy job" in r.stderr
        assert not (se.state / "slot-0.lock").exists()


class TestTheRecord:
    def test_a_poll_between_the_lock_and_its_record_reads_no_record_yet(self, se):
        """_acquire creates the lock file empty and _write truncates it before
        writing, so a poll can read it in between (#2125). CI hit that once, in
        the held-slot poll (run 37104705081: JSONDecodeError). The tests read it
        as the module's own _read does, as no record yet, and poll on."""
        lock = se.state / "slot-0.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("")  # created by _acquire, not yet written
        assert _record(se) == {}
        lock.write_text('{"state": "he')  # caught mid-write
        assert _record(se) == {}
        lock.write_text("")
        reads = []

        def poll():
            reads.append(_record(se))
            if len(reads) == 2:  # the wrapper's write lands
                lock.write_text(json.dumps({"state": "held"}) + "\n")
            return reads[-1].get("state") == "held"

        _wait_for(poll)
        assert reads == [{}, {}, {"state": "held"}]

    def test_acquire_and_release_are_events(self, se):
        _run(se, "pytest", "-q")
        acquired, released = _events(se)
        assert acquired["type"] == "heavy_slot_acquired"
        assert (
            acquired["data"]["shape"] == "pytest -q" and acquired["data"]["slot"] == 0
        )
        assert released["type"] == "heavy_slot_released"
        assert released["data"]["exit"] == 0

    def test_a_refusal_is_an_event_naming_the_holders(self, se):
        p = _hold(se)
        _run(se, "pytest", BOT_ID="beta")
        (ev,) = [e for e in _events(se) if e["type"] == "heavy_slot_refused"]
        assert ev["data"]["shape"] == "pytest"
        assert [(h["fleet"], h["bot"]) for h in ev["data"]["holders"]] == [
            ("testfleet", "alpha")
        ]
        (se.tmp / "release").touch()
        p.wait(15)


class TestStatus:
    def test_a_slot_never_used(self, se):
        r = _status(se)
        assert r.returncode == 0 and "slot 0: free — never used" in r.stdout

    def test_a_held_slot_names_its_holder(self, se):
        p = _hold(se)
        out = _status(se).stdout
        assert "slot 0: HELD by testfleet/alpha" in out and "pytest -q" in out
        (se.tmp / "release").touch()
        p.wait(15)

    def test_a_released_slot_names_the_last_holder(self, se):
        _run(se, "pytest")
        out = _status(se).stdout
        assert "slot 0: free — last: testfleet/alpha" in out and "exit 0" in out

    def test_a_hold_that_spans_a_reset_says_so(self, se):
        se.state.mkdir(parents=True)
        (se.state / "slot-0.lock").write_text(
            json.dumps(
                {
                    "state": "held",
                    "fleet": "f",
                    "bot": "b",
                    "command": "npm ci",
                    "boot_id": "an-earlier-boot",
                    "started_at": "2026-09-29T14:30:00Z",
                }
            )
        )
        out = _status(se).stdout
        assert "NEVER RELEASED" in out and "host reset" in out

    def test_json(self, se):
        _run(se, "pytest")
        (slot,) = json.loads(_status(se, "--json").stdout)["slots"]
        assert slot["slot"] == 0 and slot["held"] is False
        assert slot["record"]["state"] == "released"

    def test_the_off_file_is_shown(self, se):
        se.state.mkdir(parents=True)
        (se.state / "disabled").touch()
        assert "DISABLED" in _status(se).stdout
