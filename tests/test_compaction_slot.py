"""#1053 PR 3: one compaction at a time on the host (compaction-slot.py), and the
operator's stop: a `disabled` entry in the slot's directory refuses every take.

Driven through the script by its path, so its execute bit and shebang are pinned
too. Every test has its own slot directory (COMPACTION_SLOT_DIR), a stand-in /proc
for the load (COMPACTION_SLOT_PROC), a boot ID and a clock (COMPACTION_SLOT_NOW),
so nothing here reaches the host's slot. keepalive's compaction executor
(keepalive.sh, compact_tick) takes the slot; tests/test_keepalive_compact.py
drives it there.
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
                      "load_line": 40.0, "disabled": False}
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


# --- the operator's stop: anything named `disabled` in the slot's directory -----------

def _place(path: Path, kind: str) -> None:
    if kind == "empty file":
        path.write_text("")
    elif kind == "file with text":
        path.write_text("stopped while the host is looked at\n")
    elif kind == "directory":
        path.mkdir()
    elif kind == "dangling link":
        path.symlink_to("nowhere")
    else:  # a FIFO, which a read would hang on: the stop is never opened
        os.mkfifo(path)


@pytest.mark.parametrize("kind", ["empty file", "file with text", "directory", "dangling link",
                                  "fifo"])
def test_the_stop_refuses_every_take_writing_nothing_until_it_is_removed(slot, kind):
    state = slot / "state"
    state.mkdir()
    stop = state / "disabled"
    _place(stop, kind)
    for bot in ("alpha", "beta"):
        rc, refused = _take(slot, bot)
        assert (rc, refused["outcome"], refused["reason"]) == (75, "refused", "disabled")
        assert refused["stop"]["path"] == str(stop) and refused["stop"]["since"]
    assert sorted(os.listdir(state)) == ["disabled"]  # no lock, no lease: nothing written
    status = _status(slot)
    assert (status["state"], status["disabled"], status["stop"]["path"]) == ("free", True, str(stop))
    if kind == "directory":
        stop.rmdir()
    else:
        stop.unlink()
    status = _status(slot)
    assert status["disabled"] is False and "stop" not in status
    rc, granted = _take(slot, "alpha")
    assert (rc, granted["outcome"]) == (0, "granted")


def test_a_stop_placed_while_a_take_waits_for_the_lock_refuses_that_take(slot):
    """The take starts with no stop in place, meets the slot's lock, and decides after
    the stop is placed: it reads the stop again under the lock."""
    state = slot / "state"
    state.mkdir()
    fd = os.open(state / "lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        taker = subprocess.Popen([str(SCRIPT), "take", "--fleet", "fleet-a", "--bot", "alpha",
                                  "--json"], env=_env(slot), cwd=slot, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)  # past its first read of the stop, inside its 2 s wait for the lock
        (state / "disabled").touch()
    finally:
        os.close(fd)
    out, err = taker.communicate(timeout=30)
    assert err == "", err
    assert (taker.returncode, json.loads(out)["reason"]) == (75, "disabled")
    assert not (state / "lease.json").exists()


def test_a_live_lease_outlives_the_stop_and_its_release_still_works(slot):
    rc, first = _take(slot, "alpha")
    assert rc == 0
    (slot / "state/disabled").touch()
    rc, refused = _take(slot, "beta", now=NOW + 1)
    assert (rc, refused["reason"]) == (75, "disabled")  # the stop, before the holder
    status = _status(slot, now=NOW + 1)
    assert (status["state"], status["lease"]["bot"], status["disabled"]) == ("held", "alpha", True)
    rc, released = _release(slot, "alpha", first["lease"]["lease"], now=NOW + 2)
    assert (rc, released["outcome"]) == (0, "released")
    status = _status(slot, now=NOW + 2)
    assert (status["state"], status["last"]["ended"], status["disabled"]) == ("free", "released", True)
    assert (slot / "state/disabled").exists()  # a release leaves the stop in place
    read = subprocess.run([str(SCRIPT), "status"], env=_env(slot, now=NOW + 2), cwd=slot,
                          capture_output=True, text=True, timeout=30)
    assert read.returncode == 0, read.stderr
    assert f"; stopped by {slot / 'state/disabled'} since " in read.stdout
    assert "every take is refused" in read.stdout
    took = subprocess.run([str(SCRIPT), "take", "--fleet", "fleet-a", "--bot", "beta"],
                          env=_env(slot, now=NOW + 3), cwd=slot, capture_output=True, text=True,
                          timeout=30)
    assert took.returncode == 75 and took.stdout.startswith("refused: stopped by "), took.stdout


def test_a_stop_that_cannot_be_looked_at_grants_nothing(slot):
    loop = slot / "loop"
    loop.symlink_to("loop")  # every path through it is a link loop: lstat fails ELOOP
    env = constructed_env(COMPACTION_SLOT_DIR=loop, COMPACTION_SLOT_PROC=slot / "proc",
                          COMPACTION_SLOT_BOOT_ID="boot-a", COMPACTION_SLOT_NOW=repr(NOW))
    took = subprocess.run([str(SCRIPT), "take", "--fleet", "fleet-a", "--bot", "alpha", "--json"],
                          env=env, cwd=slot, capture_output=True, text=True, timeout=30)
    assert (took.returncode, took.stdout) == (2, "")
    assert f"the stop {loop / 'disabled'} cannot be looked at" in took.stderr
    read = subprocess.run([str(SCRIPT), "status", "--json"], env=env, cwd=slot,
                          capture_output=True, text=True, timeout=30)
    assert read.returncode == 0, read.stderr
    status = json.loads(read.stdout)
    assert status["disabled"] is None and status["stop"]["error"], status
