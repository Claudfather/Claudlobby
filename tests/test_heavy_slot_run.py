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
import functools
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import constructed_env

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
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75  # ticket 1
        assert _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma").returncode == 75
        (se.tmp / "release").touch()
        assert p.wait(15) == 0
        r = _run(se, "pytest", BOT_ID="beta")
        assert r.returncode == 75
        assert "another caller's turn: ticket 2, otherfleet/gamma" in r.stderr
        assert _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma").returncode == 0
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0

    def test_within_one_fleet_a_free_slot_goes_to_the_oldest_ticket(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75  # ticket 1
        assert _run(se, "pytest", BOT_ID="gamma").returncode == 75  # ticket 2
        (se.tmp / "release").touch()
        assert p.wait(15) == 0
        r = _run(se, "pytest", BOT_ID="gamma")
        assert r.returncode == 75 and "ticket 1, testfleet/beta" in r.stderr
        assert "you hold ticket 2, place 2 of 2" in r.stderr
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0
        assert _run(se, "pytest", BOT_ID="gamma").returncode == 0

    def test_nothing_expires_while_every_slot_is_held(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        time.sleep(4)  # silent past the limit below, but the slot was held all along
        (se.tmp / "release").touch()
        assert p.wait(15) == 0
        r = _run(se, "pytest", HEAVY_SLOT_TICKET_IDLE_S=3)  # alpha's next take, at once
        assert r.returncode == 75 and "ticket 1, testfleet/beta" in r.stderr
        assert _run(se, "pytest", BOT_ID="beta", HEAVY_SLOT_TICKET_IDLE_S=3).returncode == 0

    def test_a_silent_waiter_is_dropped_once_a_slot_has_been_free_that_long(self, se):
        p = _hold(se)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 75
        (se.tmp / "release").touch()
        assert p.wait(15) == 0
        time.sleep(1.5)
        r = _run(se, "pytest", HEAVY_SLOT_TICKET_IDLE_S=1)  # alpha's next take
        assert r.returncode == 0, r.stderr
        (ev,) = [e for e in _events(se) if e["type"] == "heavy_slot_ticket_dropped"]
        assert (ev["data"]["ticket"]["bot"], ev["data"]["idle_s"]) == ("beta", 1)
        assert ev["data"]["silent_s"] >= 1

    def test_status_lists_the_queue_with_the_limit_beside_each_wait(self, se):
        p = _hold(se)
        _run(se, "pytest", BOT_ID="beta")
        _run(se, "pytest", FLEET_NAME="otherfleet", BOT_ID="gamma")
        out = _status(se).stdout
        # served next: the other fleet's ticket, since the holder's fleet took the slot last
        assert "queue: 2 waiting, in the order they are served" in out
        assert "queue 1: ticket 2, otherfleet/gamma (pytest), waiting since" in out
        assert "queue 2: ticket 1, testfleet/beta (pytest), waiting since" in out
        assert out.count("dropped after 3 min silent once a slot is free") == 2
        assert out.count("— next") == 1
        st = json.loads(_status(se, "--json").stdout)
        assert st["ticket_idle_s"] == 180 and st["queue_off"] is False
        assert [(q["place"], q["ticket"], q["fleet"], q["bot"], q["idle_s"])
                for q in st["queue"]] == [(1, 2, "otherfleet", "gamma", 180),
                                          (2, 1, "testfleet", "beta", 180)]
        # the limit shown is the one the reader's environment sets
        other = subprocess.run([str(WRAPPER), "status"], env=_env(se, HEAVY_SLOT_TICKET_IDLE_S=90),
                               capture_output=True, text=True, timeout=60).stdout
        assert "dropped after 90 s silent once a slot is free" in other
        (se.tmp / "release").touch()
        p.wait(15)

    def test_a_refusal_and_the_take_it_waited_for_carry_one_ticket(self, se):
        p = _hold(se)
        _run(se, "pytest", BOT_ID="beta")
        (se.tmp / "release").touch()
        p.wait(15)
        assert _run(se, "pytest", BOT_ID="beta").returncode == 0
        refused = [e["data"] for e in _events(se) if e["type"] == "heavy_slot_refused"]
        acquired = [e["data"] for e in _events(se) if e["type"] == "heavy_slot_acquired"]
        assert [(r["ticket"]["n"], r["ticket"]["bot"], r["ticket"]["place"]) for r in refused] == [
            (1, "beta", 1)]
        assert "ticket" not in acquired[0]  # alpha took a free slot nobody waited for
        assert acquired[1]["ticket"]["n"] == 1 and acquired[1]["ticket"]["waited_s"] >= 0

    def test_the_off_switch_decides_on_the_slot_alone(self, se):
        se.state.mkdir(parents=True)
        (se.state / "no-queue").touch()
        p = _hold(se)
        r = _run(se, "pytest", BOT_ID="beta")
        assert r.returncode == 75 and "ticket" not in r.stderr
        (se.tmp / "release").touch()
        p.wait(15)
        assert _run(se, "pytest").returncode == 0  # alpha again: no turn is held for beta
        assert not (se.state / "queue.json").exists()
        assert "QUEUE OFF" in _status(se).stdout

    def test_a_queue_lock_nobody_releases_falls_back_to_the_slot_alone(self, se):
        # A stopped process holding the queue's lock must not stop every heavy job.
        se.state.mkdir(parents=True)
        fd = os.open(se.state / "queue.lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            t = time.monotonic()
            r = _run(se, "pytest")
            assert r.returncode == 0, r.stderr
            assert time.monotonic() - t >= 2  # QUEUE_LOCK_WAIT_S, then on without it
        finally:
            os.close(fd)
        assert not (se.state / "queue.json").exists()

    def test_a_damaged_queue_file_is_an_empty_queue(self, se):
        se.state.mkdir(parents=True)
        (se.state / "queue.json").write_text('{"next": "x", "tickets": [')
        assert _run(se, "pytest").returncode == 0


@pytest.fixture(scope="module")
def hs():
    """The script as a module (its name has a dash)."""
    spec = importlib.util.spec_from_file_location("heavy_slot", WRAPPER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


T0 = 1_790_000_000.0  # any epoch: the decision reads differences only


def _held(at: float) -> list:
    return [(0, True, {"state": "held", "fleet": "testfleet", "bot": "driver",
                       "started_epoch": at})]


def _free(at: float) -> list:
    return [(0, False, {"state": "released", "fleet": "testfleet", "bot": "driver",
                        "started_epoch": at - 30, "released_epoch": at})]


class TestTheQueuesDecision:
    """take_turn and _may_take, the decision both the hook and the wrapper make,
    at the real constants: the clock is an argument here, so a 60 s retry costs
    no 60 s of test time."""

    @staticmethod
    def _call(hs, monkeypatch, q, fleet, bot, slots, now):
        """One call by fleet/bot at `now`, written back as the wrapper writes it:
        (whether it may take a slot, the bots whose tickets it dropped)."""
        monkeypatch.setenv("FLEET_NAME", fleet)
        monkeypatch.setenv("BOT_ID", bot)
        order, dropped, mine, _ = hs.take_turn(q, slots, "pytest", now, hs.ticket_idle_s())
        may = hs._may_take(mine, order, slots)
        q["tickets"] = [t for t in order if not (may and t["n"] == mine["n"])]
        return may, [t["bot"] for t, _ in dropped]

    @pytest.mark.parametrize("fleet", ["otherfleet", "testfleet"],
                             ids=["another-fleet", "same-fleet"])
    def test_a_waiter_retrying_every_60_s_keeps_its_place_while_a_driver_waits(
            self, hs, monkeypatch, fleet):
        monkeypatch.delenv("HEAVY_SLOT_TICKET_IDLE_S", raising=False)
        q = {"v": 1, "next": 1, "tickets": []}
        call = functools.partial(self._call, hs, monkeypatch, q)
        # the driver holds the slot: the waiter's poll is refused and takes ticket 1
        assert call(fleet, "waiter", _held(T0 - 5), T0) == (False, [])
        # the driver's job ends at T0+1; it tries again at once, then every 20 ms
        for dt in (1.0, 1.02, 30, 59.98):
            assert call("testfleet", "driver", _free(T0 + 1), T0 + dt) == (False, [])
        # the waiter's next poll, 60 s after its last, takes the slot; the driver is next
        assert call(fleet, "waiter", _free(T0 + 1), T0 + 60) == (True, [])
        assert [(t["bot"], t["n"]) for t in q["tickets"]] == [("driver", 2)]

    def test_a_ticket_is_kept_180_s_after_a_slot_comes_free_and_no_longer(self, hs, monkeypatch):
        monkeypatch.delenv("HEAVY_SLOT_TICKET_IDLE_S", raising=False)
        assert hs.TICKET_IDLE_S == 180
        q = {"v": 1, "next": 1, "tickets": []}
        call = functools.partial(self._call, hs, monkeypatch, q)
        assert call("otherfleet", "waiter", _held(T0 - 5), T0) == (False, [])
        # however long the slot stays held, the ticket stands
        assert call("testfleet", "driver", _held(T0 - 5), T0 + 3600) == (False, [])
        # the slot comes free at T0+3600, and the waiter stays silent from then on
        assert call("testfleet", "driver", _free(T0 + 3600), T0 + 3600 + 180) == (False, [])
        assert call("testfleet", "driver", _free(T0 + 3600), T0 + 3600 + 181) == (True, ["waiter"])
