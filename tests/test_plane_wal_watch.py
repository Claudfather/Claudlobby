"""A reader holding a plane snapshot grows the WAL past the ceiling, and the
estate can now see it (#1905).

The failure, reproduced for real: a separate process holds a read snapshot, the
daemon's own writer keeps committing and running its checkpoint cadence, and the
WAL grows past the 4 MiB ceiling because no checkpoint can reset it. Before
#1905 nothing reported that: `plane doctor` stayed green. The test drives the
REAL pieces -- `PlaneWriter` and its cadence, a `mode=ro` reader in its own
process, and `plane doctor` as a subprocess -- and asserts the doctor names the
holder, then that the WAL comes back under the ceiling once the reader lets go.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from claudlobby.plane.db import db_path
from claudlobby.plane.wal import (WAL_CEILING_BYTES, shm_file, snapshot_holders, wal_size,
                                  writer_pids)
from claudlobby.plane.writer import PlaneWriter

_HOLD = (
    "import sqlite3, sys\n"
    "c = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)\n"
    "c.execute('BEGIN')\n"
    "c.execute('SELECT COUNT(*) FROM ingest_ledger').fetchone()\n"
    "print('held', flush=True)\n"
    "sys.stdin.readline()\n"
)


def _doctor(root: Path) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root), "plane", "doctor"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    wal = [line for line in r.stdout.splitlines() if "] wal" in line]
    assert len(wal) == 1, f"expected ONE wal rung, got {wal!r}\n{r.stdout}\n{r.stderr}"
    return r.returncode, wal[0]


def _batch(writer: PlaneWriter) -> None:
    """One committed batch through the daemon's writer, then its cadence.
    A 64 KiB blob dirties ~16 pages, so the WAL passes the ceiling in a few
    dozen batches rather than the ~90 real events would take."""
    conn = writer.connection()
    conn.execute("CREATE TABLE IF NOT EXISTS wal_watch_scratch (b BLOB)")
    conn.execute("INSERT INTO wal_watch_scratch VALUES (zeroblob(65536))")
    writer.after_batch()


def test_a_held_reader_is_named_while_the_wal_is_over_the_ceiling(tmp_path):
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    writer = PlaneWriter(root)
    writer.connection()  # migrate: a real plane db
    reader = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(db_path(root))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert reader.stdout.readline().strip() == "held"
        for _ in range(200):
            _batch(writer)
            if wal_size(root) > WAL_CEILING_BYTES:
                break
        # The mechanism, not just the symptom: the cadence DID run and was
        # refused by the reader, so a WAL over the ceiling here is the reader's.
        assert wal_size(root) > WAL_CEILING_BYTES, wal_size(root)
        assert writer.checkpoint_busy > 0

        rc, line = _doctor(root)
        assert rc == 1 and line.startswith("[ATTENTION] wal"), line
        assert "over the 4.0 MB ceiling" in line, line
        if Path("/proc/locks").exists():
            assert f"pid {reader.pid}" in line, line  # the holder, by name
        else:
            assert "cannot be named on this host" in line, line
    finally:
        reader.stdin.write("\n")
        reader.stdin.flush()
        reader.wait(timeout=30)

    # The reader let go: the next batch's cadence resets the WAL (#1693's
    # no-wait TRUNCATE succeeds with nothing in the way), and the rung clears.
    _batch(writer)
    assert wal_size(root) < WAL_CEILING_BYTES, wal_size(root)
    rc, line = _doctor(root)
    assert line.startswith("[ok] wal"), line
    writer.close()


def test_only_a_read_mark_holder_is_named(tmp_path):
    """The lock table carries four kinds of line for the plane's -shm, and only
    one of them is a snapshot: a READ lock inside bytes 123-127 (the five
    read-marks). The DMS byte (128) is held by every process with the db
    open, the writer's own WRITE locks are not reads, and a `->` line is a
    process QUEUED for a lock, which holds nothing. Naming any of those would
    send an operator after the wrong process."""
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    shm_file(root).write_bytes(b"")
    st = os.stat(shm_file(root))
    key = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    lock = root / "state" / "plane" / "ingest.sock.lock"
    lock.write_bytes(b"")
    lst = os.stat(lock)
    lock_key = "%02x:%02x:%d" % (os.major(lst.st_dev), os.minor(lst.st_dev), lst.st_ino)
    locks = tmp_path / "locks"
    locks.write_text("".join([
        f"1: POSIX  ADVISORY  READ 111 {key} 128 128\n",        # db open, no snapshot
        f"2: POSIX  ADVISORY  WRITE 222 {key} 120 121\n",       # the writer and its checkpoint
        f"3: POSIX  ADVISORY  READ 333 {key} 125 125\n",        # a snapshot: named
        f"3: -> POSIX  ADVISORY  WRITE 444 {key} 123 123\n",    # queued, holds nothing
        f"4: POSIX  ADVISORY  READ 555 fe:01:{st.st_ino + 1} 124 124\n",  # another file
        f"5: POSIX  ADVISORY  READ 333 {key} 124 124\n",        # one pid, one entry
        f"6: POSIX  ADVISORY  READ 666 {key} 123 123\n",        # the daemon, mid-batch
        f"7: FLOCK  ADVISORY  WRITE 666 {lock_key} 0 EOF\n",    # ...holding its lifetime flock
        f"7: -> FLOCK  ADVISORY  WRITE 777 {lock_key} 0 EOF\n",  # a second daemon, refused
    ]))
    proc = tmp_path / "proc"
    (proc / "333").mkdir(parents=True)
    (proc / "333" / "cmdline").write_bytes(b"python3\0lib/dispatch-overdue.py\0--all\0")
    (proc / "333" / "stat").write_text("333 (python3) S " + " ".join(["0"] * 18) + " 100 0 0\n")
    (proc / "uptime").write_text("50.00 1.00\n")
    # the daemon holds a read-mark for the length of each batch; its lifetime
    # flock is what tells it apart, and a queued second daemon holds nothing
    assert writer_pids(lock, locks=str(locks)) == {666}
    assert [h.pid for h in snapshot_holders(root, locks=str(locks), proc=str(proc),
                                            reads=1)] == [333, 666]
    got = snapshot_holders(root, locks=str(locks), proc=str(proc),
                           exclude=writer_pids(lock, locks=str(locks)))
    assert [h.pid for h in got] == [333]
    assert got[0].cmd == "python3 lib/dispatch-overdue.py --all"
    assert got[0].age_s is not None and got[0].age_s <= 50
    # no lock table at all (macOS): None, never an empty list that reads as
    # "nobody is holding it"
    assert snapshot_holders(root, locks=str(tmp_path / "absent"), proc=str(proc)) is None


def test_a_pid_seen_in_one_read_only_is_not_named(tmp_path, monkeypatch):
    """A statement or a batch running at the instant of one read is not the
    snapshot that grows the WAL; only a pid holding in EVERY read is named."""
    from claudlobby.plane import wal
    root = tmp_path / "root"
    (root / "state" / "plane").mkdir(parents=True)
    shm_file(root).write_bytes(b"")
    st = os.stat(shm_file(root))
    key = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    steady = ["1:", "POSIX", "ADVISORY", "READ", "333", key, "124", "124"]
    passing = ["2:", "POSIX", "ADVISORY", "READ", "888", key, "125", "125"]
    tables = iter([[steady, passing], [steady], [steady]])
    monkeypatch.setattr(wal, "_lock_table", lambda locks: next(tables))
    got = snapshot_holders(root, proc=str(tmp_path / "noproc"), reads=3, interval=0)
    assert [h.pid for h in got] == [333]


def test_the_host_card_verdict_is_the_apis():
    """The page renders `wal_state`; it never compares bytes itself."""
    from claudlobby.plane.view import _wal_state
    sample = lambda v: {"host.plane_wal_bytes": {"value": v, "occurred_at": "x"}}  # noqa: E731
    assert _wal_state(None) is None                      # the probe never recorded one
    assert _wal_state({}) is None
    assert _wal_state(sample(WAL_CEILING_BYTES)) == "ok"
    assert _wal_state(sample(WAL_CEILING_BYTES + 1)) == "over"
    assert _wal_state(sample(True)) is None              # not a size: no verdict
    assert _wal_state(sample("81.8 MB")) is None

