"""The plane's WAL: how big it is, whether that is over the ceiling, and which
process is holding it open (#1905).

**Why the WAL can grow at all.** The daemon folds the WAL back into the db on a
cadence (`writer.py`), and a TRUNCATE checkpoint can only reset the file when no
reader holds a snapshot it needs to pass. Let a reader keep one open and the
WAL grows for as long as it does: the #1693 canary grew it to 81.8 MB in 60 s
behind one held `mode=ro` connection. The daemon cannot end another process's
read transaction, so there is no cap to enforce here. What there can be is a
number an operator sees, a ceiling it is judged against, and the name of the
process to go and look at.

**The holder is named from the kernel, not from the plane.** SQLite's unix VFS
takes its WAL locks as fcntl byte-range locks on ``plane.db-shm``: bytes
123-127 are the five read-marks, and a READ lock on one of them is an open read
transaction. On Linux ``/proc/locks`` lists them with the owning pid, so the
holder is readable without opening the db, and without trusting the thing that
is misbehaving to report itself. macOS has no ``/proc/locks``; there the holder
is not determinable and callers say so rather than print an empty list.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from .db import db_file

#: The ceiling accepted on #1693 (issuecomment-5768778429). The cadence folds
#: the WAL back at 1 MiB (`writer.CHECKPOINT_WAL_BYTES`), so a WAL past 4 MiB
#: means the last few checkpoints could not reset it: a reader has held a
#: snapshot across them, or the daemon is not checkpointing at all.
WAL_CEILING_BYTES = 4 * 1024 * 1024

#: SQLite's unix VFS: UNIX_SHM_BASE = (22 + SQLITE_SHM_NLOCK) * 4 = 120, and
#: WAL_READ_LOCK(i) = 3 + i for the five read-marks i = 0..4.
_READ_MARKS = range(123, 128)


def wal_file(root: Path) -> Path:
    return Path(str(db_file(root)) + "-wal")


def shm_file(root: Path) -> Path:
    return Path(str(db_file(root)) + "-shm")


def wal_size(root: Path) -> int:
    """Bytes of WAL right now. 0 when the file is absent: SQLite deletes the
    WAL when the last connection closes cleanly, so "no file" beside an
    existing db is an empty WAL. Any other failure to stat raises OSError,
    because a size the caller could not read is not a size of zero."""
    try:
        return wal_file(root).stat().st_size
    except FileNotFoundError:
        return 0


@dataclass(frozen=True)
class Holder:
    pid: int
    cmd: str  # the command line, or "" when the process is gone
    age_s: int | None  # how long the PROCESS has run: an upper bound on the hold


def _lock_table(locks: str) -> list[list[str]] | None:
    try:
        with open(locks) as f:
            return [line.split() for line in f]
    except OSError:
        return None


def _file_key(path: Path) -> str:
    """How /proc/locks names a file: hex major:minor, then the inode."""
    st = os.stat(path)
    return "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)


def writer_pids(lock_file: Path, *, locks: str = "/proc/locks") -> set[int]:
    """The plane daemon: whoever holds its lifetime flock (`daemon.lock_path`).
    Its own transactions take a read-mark for as long as they run, so under
    traffic it is often mid-batch at the instant anyone looks, and it would be
    named as a holder every time. It is not the snapshot this is about."""
    table = _lock_table(locks)
    try:
        key = _file_key(lock_file)
    except OSError:
        return set()
    return {int(p[4]) for p in table or []
            if len(p) >= 8 and p[1] == "FLOCK" and p[5] == key and p[4].isdigit()}


def _read_mark_pids(table: list[list[str]], key: str) -> set[int]:
    pids = set()
    for parts in table:
        # "<n>: POSIX ADVISORY READ <pid> <maj:min:ino> <start> <end>"; a "->"
        # line is a process QUEUED for a lock, which holds nothing
        if len(parts) < 8 or parts[1] == "->" or parts[5] != key or parts[3] != "READ":
            continue
        try:
            pid, lo = int(parts[4]), int(parts[6])
            hi = int(parts[7]) if parts[7] != "EOF" else 1 << 62
        except ValueError:
            continue
        if lo <= _READ_MARKS[-1] and hi >= _READ_MARKS[0]:
            pids.add(pid)
    return pids


def snapshot_holders(root: Path, *, locks: str = "/proc/locks", proc: str = "/proc",
                     exclude: set[int] | frozenset[int] = frozenset(), reads: int = 5,
                     interval: float = 0.05) -> list[Holder] | None:
    """Processes holding a read snapshot on the plane, or None where the
    platform cannot say (no ``/proc/locks``, or no ``-shm`` file to match).

    Held means held in EVERY one of `reads` reads of the lock table, `interval`
    apart: a statement or a batch that happens to be running at one instant is
    not what grows the WAL, and naming it sends an operator the wrong way.
    `exclude` is for the plane's own writer (`writer_pids`). The lock table
    does not say how long a lock has been held, so ``age_s`` is the process's
    age: a bound, never a measurement of the hold."""
    try:
        key = _file_key(shm_file(root))
    except OSError:
        return None
    held: set[int] | None = None
    for i in range(max(1, reads)):
        table = _lock_table(locks)
        if table is None:
            return None
        now = _read_mark_pids(table, key)
        held = now if held is None else held & now
        if not held:
            break
        if i + 1 < reads:
            time.sleep(interval)
    return [_describe(pid, proc) for pid in sorted(held or ())
            if pid not in exclude and pid != os.getpid()]


def _describe(pid: int, proc: str) -> Holder:
    try:
        with open(f"{proc}/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return Holder(pid, "", None)
    age = None
    try:
        with open(f"{proc}/{pid}/stat") as f:
            start_ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open(f"{proc}/uptime") as f:
            uptime = float(f.read().split()[0])
        age = max(0, int(uptime - start_ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, ValueError, IndexError):
        pass
    return Holder(pid, cmd, age)


def holders_detail(holders: list[Holder] | None, *, limit: int = 3) -> str:
    """The sentence for a WAL over the ceiling: which process to go and look
    at, or why none can be named. One phrasing, so every surface agrees."""
    if holders is None:
        return ("a reader is holding a snapshot the checkpoint needs, or the"
                " daemon is not checkpointing; the holder cannot be named on"
                " this host (that needs /proc/locks and the plane's -shm file)")
    if not holders:
        return ("no process holds a snapshot now, so the daemon's next"
                " checkpoint should reset it; if it stays over, the checkpoint"
                " is not running (see the daemon rung)")
    named = "; ".join(
        f"pid {h.pid}" + (f", up {h.age_s}s" if h.age_s is not None else "")
        + f": {h.cmd[:120] if h.cmd else '(exited)'}" for h in holders[:limit])
    more = f" (+{len(holders) - limit} more)" if len(holders) > limit else ""
    return (f"holding a snapshot now: {named}{more}. The daemon's next"
            " checkpoint resets it once that reader lets go")


def human_bytes(n: int) -> str:
    for unit, size in (("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"
