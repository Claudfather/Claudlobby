"""#1053 PR 3: one compaction at a time on the host (compaction-slot.py).

Driven through the script by its path, as the build's executor is to run it, so
its execute bit and shebang are pinned too. Every test has its own slot directory
(COMPACTION_SLOT_DIR), a stand-in /proc for the load (COMPACTION_SLOT_PROC), a
boot ID and a clock (COMPACTION_SLOT_NOW), so nothing here reaches the host's
slot. Nothing in the framework takes the slot yet.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import constructed_env

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "claudlobby/_runtime_scripts" / "compaction-slot.py"
NOW = 1_800_000_000.0


@pytest.fixture()
def slot(tmp_path):
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc/loadavg").write_text("1.00 1.00 1.00 1/100 42\n")
    return tmp_path


def _env(slot, *, now=NOW, boot="boot-a"):
    return constructed_env(COMPACTION_SLOT_DIR=slot / "state", COMPACTION_SLOT_PROC=slot / "proc",
                           COMPACTION_SLOT_BOOT_ID=boot, COMPACTION_SLOT_NOW=repr(now))


def _run(slot, *argv, now=NOW, boot="boot-a", load=None):
    if load is not None:
        (slot / "proc/loadavg").write_text(f"{load} 1.00 1.00 1/100 42\n")
    done = subprocess.run([str(SCRIPT), *argv, "--json"], env=_env(slot, now=now, boot=boot),
                          cwd=slot, capture_output=True, text=True, timeout=30)
    assert done.stderr == "", done.stderr
    return done.returncode, json.loads(done.stdout)


def _take(slot, bot, **kw):
    return _run(slot, "take", "--fleet", "fleet-a", "--bot", bot, **kw)


def _release(slot, bot, lease, **kw):
    return _run(slot, "release", "--fleet", "fleet-a", "--bot", bot, "--lease", lease, **kw)


def _status(slot, **kw):
    rc, out = _run(slot, "status", **kw)
    assert rc == 0
    return out


def test_a_second_take_is_refused_while_a_lease_lives_and_granted_once_released(slot):
    rc, first = _take(slot, "alpha")
    assert (rc, first["outcome"]) == (0, "granted")
    lease = first["lease"]
    assert (lease["fleet"], lease["bot"], lease["taken"], lease["ends"]) == ("fleet-a", "alpha", NOW, NOW + 600)
    # Another bot, and the holder itself: its own compaction is still in flight.
    for bot in ("beta", "alpha"):
        rc, refused = _take(slot, bot, now=NOW + 1)
        assert (rc, refused["outcome"], refused["reason"]) == (75, "refused", "held")
        assert refused["lease"]["lease"] == lease["lease"]
    assert _status(slot, now=NOW + 1)["lease"]["lease"] == lease["lease"]
    rc, released = _release(slot, "alpha", lease["lease"], now=NOW + 2)
    assert (rc, released["outcome"]) == (0, "released")
    assert _status(slot, now=NOW + 2)["last"]["ended"] == "released"
    rc, second = _take(slot, "beta", now=NOW + 3)
    assert (rc, second["outcome"], second["lease"]["bot"]) == (0, "granted", "beta")


def test_a_lease_whose_holder_died_ends_at_its_ten_minutes(slot):
    # The process that took the lease has exited: a lease outlives its holder.
    rc, first = _take(slot, "alpha")
    assert rc == 0
    rc, refused = _take(slot, "beta", now=NOW + 599.9)
    assert (rc, refused["reason"]) == (75, "held")
    assert _status(slot, now=NOW + 600)["last"]["ended"] == "expired"
    rc, granted = _take(slot, "beta", now=NOW + 600)
    assert (rc, granted["outcome"]) == (0, "granted")


def test_a_host_reset_ends_a_live_lease(slot):
    _take(slot, "alpha", boot="boot-a")
    status = _status(slot, now=NOW + 1, boot="boot-b")
    assert (status["state"], status["last"]["ended"]) == ("free", "reset")
    rc, granted = _take(slot, "beta", now=NOW + 1, boot="boot-b")
    assert (rc, granted["outcome"]) == (0, "granted")


def test_load1_at_40_refuses_and_39_9_grants(slot):
    rc, refused = _take(slot, "alpha", load="40.00")
    assert (rc, refused["reason"], refused["load1"], refused["load_line"]) == (75, "load", 40.0, 40.0)
    assert not (slot / "state/lease.json").exists()  # a refusal writes nothing
    rc, granted = _take(slot, "alpha", load="39.90")
    assert (rc, granted["outcome"], granted["lease"]["load1"]) == (0, "granted", 39.9)


def test_a_load_that_cannot_be_read_refuses(slot):
    for text in ("\n", "busy 1 1 1/1 1\n"):
        (slot / "proc/loadavg").write_text(text)
        rc, refused = _take(slot, "alpha")
        assert (rc, refused["reason"]) == (75, "load_unreadable")
        assert _status(slot)["load1"] is None
    assert not (slot / "state/lease.json").exists()


def test_two_racing_takes_grant_one(slot):
    """Six takers start while the test holds the slot's lock, so they meet at it."""
    (slot / "state").mkdir()
    fd = os.open(slot / "state/lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        takers = [subprocess.Popen([str(SCRIPT), "take", "--fleet", "fleet-a", "--bot", f"bot{i}",
                                    "--json"], env=_env(slot), cwd=slot, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True) for i in range(6)]
        time.sleep(0.3)
    finally:
        os.close(fd)
    results = []
    for taker in takers:
        out, err = taker.communicate(timeout=30)
        assert err == "", err
        results.append((taker.returncode, json.loads(out)))
    granted = [out for rc, out in results if rc == 0]
    assert len(granted) == 1, results
    assert sorted(out["reason"] for rc, out in results if rc != 0) == ["held"] * 5
    assert _status(slot)["lease"]["bot"] == granted[0]["lease"]["bot"]


def test_a_take_waits_for_the_lock_and_refuses_past_its_wait(slot):
    (slot / "state").mkdir()
    fd = os.open(slot / "state/lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        rc, refused = _take(slot, "alpha")
        waited = time.monotonic() - started
    finally:
        os.close(fd)
    assert (rc, refused["reason"]) == (75, "lock_busy")
    assert waited >= 2.0
    assert not (slot / "state/lease.json").exists()


def test_a_late_release_never_ends_a_later_lease(slot):
    _, first = _take(slot, "alpha")
    _, again = _take(slot, "alpha", now=NOW + 600)  # the same bot, a new lease
    rc, late = _release(slot, "alpha", first["lease"]["lease"], now=NOW + 601)
    assert (rc, late["outcome"], late["reason"]) == (0, "not_held", "not_this_lease")
    assert _status(slot, now=NOW + 601)["lease"]["lease"] == again["lease"]["lease"]
    _release(slot, "alpha", again["lease"]["lease"], now=NOW + 601)
    _, second = _take(slot, "beta", now=NOW + 602)
    assert second["outcome"] == "granted"
    rc, late = _release(slot, "alpha", first["lease"]["lease"], now=NOW + 603)
    assert (rc, late["outcome"], late["reason"]) == (0, "not_held", "not_this_lease")
    rc, wrong = _release(slot, "alpha", second["lease"]["lease"], now=NOW + 603)
    assert (rc, wrong["outcome"]) == (0, "not_held")
    status = _status(slot, now=NOW + 603)
    assert (status["state"], status["lease"]["bot"]) == ("held", "beta")
    # Released after its end, a lease is not held either.
    rc, gone = _release(slot, "beta", second["lease"]["lease"], now=NOW + 1202)
    assert (rc, gone["outcome"], gone["reason"]) == (0, "not_held", "expired")


def test_a_record_that_cannot_be_read_holds_nothing(slot):
    (slot / "state").mkdir()
    for text in ("not json\n", "[]\n", '{"lease": "x"}\n'):
        (slot / "state/lease.json").write_text(text)
        status = _status(slot)
        assert status["state"] == "free" and status["record"].startswith("unreadable"), status
    rc, granted = _take(slot, "alpha")
    assert rc == 0 and _status(slot)["lease"]["lease"] == granted["lease"]["lease"]


def test_status_reads_free_with_the_load_beside_the_line(slot):
    status = _status(slot, load="3.25")
    assert status == {"state": "free", "lease": None, "last": None, "load1": 3.25,
                      "load_line": 40.0}
    assert not (slot / "state").exists()  # a status read makes nothing


def test_the_slot_lives_in_the_data_root_and_needs_one(slot):
    no_root = subprocess.run([str(SCRIPT), "status"], env=constructed_env(), cwd=slot,
                             capture_output=True, text=True, timeout=30)
    assert no_root.returncode == 2 and "CLAUDLOBBY_ROOT" in no_root.stderr
    root = slot / "root"
    took = subprocess.run([str(SCRIPT), "take", "--fleet", "fleet-a", "--bot", "alpha"],
                          env=constructed_env(CLAUDLOBBY_ROOT=root, COMPACTION_SLOT_PROC=slot / "proc"),
                          cwd=slot, capture_output=True, text=True, timeout=30)
    assert took.returncode == 0 and took.stdout.startswith("granted: lease "), took.stderr
    record = json.loads((root / "state/compaction-slot/lease.json").read_text())
    assert (record["fleet"], record["bot"]) == ("fleet-a", "alpha")
