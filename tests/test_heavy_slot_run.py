"""The heavy-job slot itself (#1686): the wrapper that holds the host lock
while a heavy job runs, the record it leaves, and the one-line status door.

Driven through the real wrapper, executed by its path as the rewritten command
executes it (so the execute bit and the shebang are pinned too), with a stub
`pytest` that records that it ran and waits for a file. Every test gets its own
slot directory (HEAVY_SLOT_DIR) and writes its events to a file
(HEAVY_SLOT_EVENTS_FILE), so nothing here reaches the host's slots or plane.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env, load_lib_module, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "claudlobby/_runtime_scripts" / "heavy-slot.py"

# The stub job: records that it ran (and who, in STUB_LOG), traps TERM, sleeps
# or waits for a file when asked, exits with a chosen code.
STUB = """#!/bin/bash
trap 'touch "$STUB_DIR/term"; exit 143' TERM
touch "$STUB_DIR/ran.$$"
if [ -n "${STUB_LOG:-}" ]; then echo "${STUB_WHO:-?} start" >> "$STUB_LOG"; fi
if [ -n "${STUB_SLEEP:-}" ]; then sleep "$STUB_SLEEP"; fi
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


def _status(se, *args, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), "status", *args],
        env=_env(se, **extra),
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
    """The slot's record, read as the module's own _read reads it (#2125): an
    empty or half-written file is no record yet. _acquire creates the file
    empty and _write truncates it before writing, so a poll can land between."""
    try:
        rec = json.loads((se.state / f"slot-{slot}.lock").read_text() or "{}")
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


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


def _release(se, p: subprocess.Popen) -> int:
    """Let the held stub job end: its wrapper's exit code."""
    (se.tmp / "release").touch()
    return p.wait(15)


def _age(se, seconds: float, bots=(), release: bool = False) -> None:
    """Move the named bots' tickets, and with `release` the slot's last release,
    `seconds` into the past, as if that long had gone by with nobody calling."""
    queue = se.state / "queue.lock"
    q = json.loads(queue.read_text())
    for t in q["tickets"]:
        if t["bot"] in bots:
            t["since"] -= seconds
            t["seen"] -= seconds
    queue.write_text(json.dumps(q))
    if release:
        rec = _record(se)
        rec["released_epoch"] -= seconds
        (se.state / "slot-0.lock").write_text(json.dumps(rec))


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


# A driver that takes the slot again the moment it releases it: the wrapper's
# own cmd_run in a loop, in one process, so nothing but its own code runs
# between a release and the next take (#2124). A refused take is retried 20 ms
# later, faster than any waiter polls.
DRIVER = """
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("heavy_slot", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
stop, rounds = Path(sys.argv[2]), 0
while not stop.exists() and rounds < 40:
    if m.cmd_run(["pytest"]) == 75:
        time.sleep(0.02)
    else:
        rounds += 1
"""


def _drive(se, log: Path, stop: Path) -> subprocess.Popen:
    """The back-to-back driver, testfleet/driver, its jobs 0.6 s each."""
    with open(se.tmp / "driver.err", "w") as err:
        return subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(WRAPPER), str(stop)],
            env=_env(se, BOT_ID="driver", STUB_LOG=log, STUB_WHO="driver", STUB_SLEEP=0.6),
            stdout=subprocess.DEVNULL,
            stderr=err,
        )


class TestTheQueue:
    @pytest.mark.parametrize("fleet", ["otherfleet", "testfleet"],
                             ids=["another-fleet", "same-fleet"])
    def test_a_back_to_back_driver_no_longer_starves_a_polling_waiter(self, se, fleet):
        # The slot had no queue: whoever called flock first after a release won
        # it, so a driver that takes it again at once beat every waiter that
        # polls on a timer, of its own fleet or another (#2124). Now a refused
        # call takes a ticket, and the driver's next take waits behind it.
        log, stop = se.tmp / "starts.log", se.tmp / "stop"
        driver = _drive(se, log, stop)
        try:
            _wait_for(lambda: log.exists() and "driver start" in log.read_text())
            served, deadline = False, time.monotonic() + 10
            while time.monotonic() < deadline:
                r = _run(se, "pytest", FLEET_NAME=fleet, BOT_ID="waiter",
                         STUB_LOG=log, STUB_WHO="waiter")
                if r.returncode == 0:
                    served = True
                    break
                assert r.returncode == 75, r.stderr
                with log.open("a") as fh:
                    fh.write("waiter refused\n")
                time.sleep(0.25)
        finally:
            stop.touch()
            driver.wait(30)
        lines = log.read_text().splitlines()
        assert served, f"the waiter was never served while the driver ran: {lines}"
        if "waiter refused" in lines:
            # served at the first release after its first refusal: the driver
            # finishes at most the job it was running then
            waited = lines[lines.index("waiter refused"):lines.index("waiter start")]
            assert waited.count("driver start") <= 1, lines

    def test_the_next_turn_goes_to_another_fleet_before_an_older_ticket(self, se):
        p = _hold(se)  # testfleet/alpha
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        assert _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma").returncode == 75
        assert _release(se, p) == 0
        r = _run(se, "pytest", BOT_ID="beta")
        assert r.returncode == 75 and "another caller's turn" in r.stderr
        assert "otherfleet/gamma, waiting since" in r.stderr and "place 2 of 2" in r.stderr
        assert _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma").returncode == 0
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0

    def test_within_one_fleet_a_free_slot_goes_to_the_oldest_ticket(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        assert _run(se, "pytest", BOT_ID="gamma").returncode == 75
        assert _release(se, p) == 0
        r = _run(se, "pytest", BOT_ID="gamma")
        assert r.returncode == 75 and "another caller's turn" in r.stderr
        assert "testfleet/beta, waiting since" in r.stderr and "place 2 of 2" in r.stderr
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0
        assert _run(se, "pytest", BOT_ID="gamma").returncode == 0

    def test_nothing_expires_while_every_slot_is_held(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        _age(se, 3600, bots=("beta",))  # an hour silent, with the slot held throughout
        assert _release(se, p) == 0
        r = _run(se, "pytest")  # alpha's next take, at once
        assert r.returncode == 75 and "testfleet/beta, waiting since" in r.stderr
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0

    def test_a_silent_waiter_is_dropped_once_a_slot_has_been_free_that_long(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        assert _release(se, p) == 0
        _age(se, 61, bots=("beta",), release=True)  # the slot free, and beta silent, 61 s
        r = _run(se, "pytest", HEAVY_SLOT_TICKET_IDLE_S=60)  # alpha's next take
        assert r.returncode == 0, r.stderr
        (ev,) = [e["data"] for e in _events(se) if e["type"] == "heavy_slot_ticket_dropped"]
        assert (ev["ticket"]["bot"], ev["ticket"]["idle_s"]) == ("beta", 60)
        assert ev["silent_s"] >= 61

    def test_status_lists_the_queue_with_the_limit_beside_each_wait(self, se):
        p = _hold(se)
        _run(se, "pytest", BOT_ID="beta")
        _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma")
        lines = _status(se).stdout.splitlines()
        # served next: the other fleet's ticket, since the holder's fleet took the slot last
        assert "queue: 2 waiting, in the order they are served" in lines
        first, second = [x for x in lines if x.startswith("queue ")]
        assert first.startswith("queue 1: ticket ") and "otherfleet/gamma (pytest)" in first
        assert first.endswith("dropped after 3 min silent once a slot is free — next")
        assert second.startswith("queue 2: ticket ") and "testfleet/beta (pytest)" in second
        assert second.endswith("dropped after 3 min silent once a slot is free")
        st = json.loads(_status(se, "--json").stdout)
        assert (st["ticket_idle_s"], st["queue_off"], st["queue_lock_held"]) == (180, False, False)
        assert [(q["place"], q["fleet"], q["bot"], q["idle_s"]) for q in st["queue"]] == [
            (1, "otherfleet", "gamma", 180), (2, "testfleet", "beta", 180)]
        assert all(q["waited_s"] >= 0 for q in st["queue"])
        # the limit shown is the one the reader's environment sets
        other = _status(se, HEAVY_SLOT_TICKET_IDLE_S=90).stdout
        assert "dropped after 90 s silent once a slot is free" in other
        _release(se, p)

    def test_a_refusal_and_the_take_it_waited_for_carry_one_ticket(self, se):
        p = _hold(se)
        _run(se, "pytest", BOT_ID="beta")
        _release(se, p)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0
        (refused,) = [e["data"]["ticket"] for e in _events(se) if e["type"] == "heavy_slot_refused"]
        takes = [e["data"] for e in _events(se) if e["type"] == "heavy_slot_acquired"]
        assert (refused["bot"], refused["place"], refused["queued"]) == ("beta", 1, 1)
        assert "ticket" not in takes[0]  # alpha took a free slot nobody waited for
        assert takes[1]["ticket"]["n"] == refused["n"] and takes[1]["ticket"]["waited_s"] >= 0

    def test_the_off_switch_decides_on_the_slot_alone(self, se):
        se.state.mkdir(parents=True)
        (se.state / "no-queue").touch()
        p = _hold(se)
        r = _run(se, "pytest", BOT_ID="beta")
        assert r.returncode == 75 and "ticket" not in r.stderr
        _release(se, p)
        assert _run(se, "pytest").returncode == 0  # alpha again: no turn is held for beta
        assert not (se.state / "queue.lock").exists()
        assert "QUEUE OFF" in _status(se).stdout

    def test_a_queue_lock_nobody_releases_falls_back_to_the_slots_alone(self, se, hs, monkeypatch):
        # A stopped process holding the queue's lock must not stop every heavy
        # job: the decision goes on without the queue, and says why.
        se.state.mkdir(parents=True)
        fd = os.open(se.state / "queue.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        monkeypatch.setattr(hs, "QUEUE_LOCK_WAIT_S", 0.05)
        try:
            got, _slots, mine, order, why = hs._decide(se.state, 1, "pytest", take=False)
        finally:
            os.close(fd)
        assert (got, mine, order) == (True, None, [])
        assert why == "queue.lock was not had within 0.05 s"

    def test_a_damaged_queue_file_is_an_empty_queue(self, se):
        se.state.mkdir(parents=True)
        (se.state / "queue.lock").write_text('{"next": "x", "tickets": [')
        assert _run(se, "pytest").returncode == 0


@pytest.fixture(scope="module")
def hs():
    return load_lib_module("heavy-slot")


T0 = 1_790_000_000.0  # any epoch: the decision reads differences only


def _held(at: float) -> list:
    return [(0, True, {"state": "held", "fleet": "testfleet", "bot": "driver",
                       "started_epoch": at})]


def _free(at: float) -> list:
    return [(0, False, {"state": "released", "fleet": "testfleet", "bot": "driver",
                        "started_epoch": at - 30, "released_epoch": at})]


class TestTheQueuesDecision:
    """take_turn and settle, the decision and the write-back both doors make, at
    the real constants: the clock is an argument here, so a 60 s retry costs no
    60 s of test time."""

    @staticmethod
    def _call(hs, q, me, slots, now):
        """One wrapper call by `me` at `now`: (whether it may take a slot, the
        bots whose tickets it dropped)."""
        may, mine, queued, order, dropped = hs.take_turn(q, slots, me, "pytest", now,
                                                         hs.TICKET_IDLE_S)
        hs.settle(q, order, mine, queued, may, True)
        return may, [t["bot"] for t, _ in dropped]

    @pytest.mark.parametrize("fleet", ["otherfleet", "testfleet"],
                             ids=["another-fleet", "same-fleet"])
    def test_a_waiter_retrying_every_60_s_keeps_its_place_while_a_driver_waits(self, hs, fleet):
        q = {"v": 1, "next": 1, "tickets": []}
        waiter, driver = (fleet, "waiter"), ("testfleet", "driver")
        # the driver holds the slot: the waiter's poll is refused and takes a ticket
        assert self._call(hs, q, waiter, _held(T0 - 5), T0) == (False, [])
        # the driver's job ends at T0+1; it tries again at once, then every 20 ms
        for dt in (1.0, 1.02, 30, 59.98):
            assert self._call(hs, q, driver, _free(T0 + 1), T0 + dt) == (False, [])
        # the waiter's next poll, 60 s after its last, takes the slot; the driver is next
        assert self._call(hs, q, waiter, _free(T0 + 1), T0 + 60) == (True, [])
        assert [t["bot"] for t in q["tickets"]] == ["driver"]

    def test_a_ticket_is_kept_180_s_after_a_slot_comes_free_and_no_longer(self, hs):
        assert hs.TICKET_IDLE_S == 180
        q = {"v": 1, "next": 1, "tickets": []}
        waiter, driver = ("otherfleet", "waiter"), ("testfleet", "driver")
        assert self._call(hs, q, waiter, _held(T0 - 5), T0) == (False, [])
        # however long the slot stays held, the ticket stands
        assert self._call(hs, q, driver, _held(T0 - 5), T0 + 3600) == (False, [])
        # the slot comes free at T0+3600, and the waiter stays silent from then on
        assert self._call(hs, q, driver, _free(T0 + 3600), T0 + 3780) == (False, [])
        assert self._call(hs, q, driver, _free(T0 + 3600), T0 + 3781) == (True, ["waiter"])

    def test_a_take_with_nobody_waiting_issues_no_ticket(self, hs):
        q = {"v": 1, "next": 1, "tickets": []}
        assert self._call(hs, q, ("testfleet", "alpha"), _free(T0), T0 + 1) == (True, [])
        assert q == {"v": 1, "next": 1, "tickets": []}  # so the wrapper writes nothing


class TestOnThePlane:
    def test_tickets_reach_the_plane_through_the_real_door(self, se, tmp_path, scratch_plane_env):
        """The other tests read the events file seam; this drives the wrapper's
        real door, emit_fleet_event, into a served scratch plane (#2124): a
        refusal and the take it waited for carry one ticket, and a dropped ticket
        lands as a registered notice."""
        root = tmp_path / "root"
        bot = root / "runtime" / "bots" / "tbot"
        bot.mkdir(parents=True)
        env = {k: v for k, v in se.env.items() if k != "HEAVY_SLOT_EVENTS_FILE"}
        # a cold emit is reaped at its 10 s bound under load; this test is about
        # what lands, not how fast
        env.update(BOT_DIR=str(bot), FLEET_EVENT_EMIT_TIMEOUT_S="60",
                   **scratch_plane_env(root, initialize=True))

        def run(bot_id: str) -> int:
            return subprocess.run([str(WRAPPER), "run", "--", "pytest"],
                                  env={**env, "BOT_ID": bot_id},
                                  capture_output=True, text=True, timeout=60).returncode

        def heavy_rows() -> list:
            try:
                rows = [json.loads(x) for x in read_fleet_events(root).splitlines()]
            except AssertionError:  # a detached emit not committed yet
                return []
            return [r for r in rows if r["source"] == "heavy-slot"]

        with _serving(root, scratch_plane_env) as sock:
            env["PLANE_SOCKET"] = str(sock)
            holder = subprocess.Popen([str(WRAPPER), "run", "--", "pytest"],
                                      env={**env, "BOT_ID": "alpha",
                                           "STUB_WAIT": str(se.tmp / "release")})
            _wait_for(lambda: _ran(se.tmp))
            assert (run("beta"), run("gamma")) == (75, 75)
            assert _release(se, holder) == 0
            assert run("beta") == 0
            _age(se, 3600, bots=("gamma",), release=True)  # gamma silent an hour, the slot free
            assert run("alpha") == 0
            # nine events: three takes and their releases, two refusals, one drop
            deadline = time.monotonic() + 30
            while len(rows := heavy_rows()) < 9:
                assert time.monotonic() < deadline, rows
                time.sleep(0.1)
        refused = {r["data"]["ticket"]["bot"]: r["data"]["ticket"] for r in rows
                   if r["type"] == "heavy_slot_refused"}
        assert {b: t["place"] for b, t in refused.items()} == {"beta": 1, "gamma": 2}
        takes = [r["data"] for r in rows if r["type"] == "heavy_slot_acquired"]
        assert len(takes) == 3
        assert [t["ticket"]["n"] for t in takes if "ticket" in t] == [refused["beta"]["n"]]
        (dropped,) = [r["data"] for r in rows if r["type"] == "heavy_slot_ticket_dropped"]
        assert (dropped["ticket"]["n"], dropped["ticket"]["idle_s"]) == (refused["gamma"]["n"], 180)
