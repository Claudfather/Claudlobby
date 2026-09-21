"""The daemon's long-lived write connection (#1693 arm D).

**What it replaces.** `emit_batch` opened and closed the db per batch, and
SQLite runs a checkpoint-and-truncate when the LAST connection closes — so the
daemon, normally the only writer, paid a full truncate checkpoint on every
batch. otis measured essentially all of the daemon's service time in that
syscall.

**What it does instead.** One connection, held, opened `synchronous=FULL` so
each COMMIT fsyncs the WAL, plus an explicit TRUNCATE checkpoint on a cadence.
Durability moves from the close to the commit, which is why this is faster AND
a stronger promise: today a checkpoint failure on close is swallowed and still
reports `committed` (`emit_api.py`), so durability rode on a step whose failure
was invisible. Here it rides on the commit, and a checkpoint failure is a
WAL-SIZE event — loud, bounded, and not a data-loss hole.

**The failure mode this introduces, which per-batch connect is structurally
immune to.** A connection held for the process's life can go bad underneath
itself, and the dangerous half is silent: if the db FILE is replaced — an
operator restoring a backup, a migration moving it aside, a `rm` — the held
handle keeps writing SUCCESSFULLY into the now-unlinked inode. No error, no
exception, and every subsequent acknowledgment is a lie. Nothing detects that
by waiting for a failure, because there is no failure. So identity is CHECKED
rather than assumed: the (device, inode) the connection was opened on is
compared against the path before each batch, and a mismatch reconnects.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .db import connect, db_path
from .migrations import migrate

#: Explicit rather than passive. SQLite's auto-checkpoint is "it will try", and
#: a held reader defeats it silently; an explicit call returns a result we can
#: record.
#:
#: **The primary trigger is WAL BYTES, and that is a correction the canary
#: forced.** The cadence first proposed on #1693 was 200 batches, derived from
#: the plane's measured ~767 B/row. That reasoning was wrong by ~60x: a WAL
#: frame is a PAGE, and one small event dirties pages across the table, the
#: ledger and several indexes, so a batch costs ~45 KB of WAL rather than ~767
#: B (measured through the real daemon: 6 batches = 259,592 B). At 200 batches
#: the WAL would reach ~9 MB, over the 4 MB ceiling the same design proposed.
#:
#: Row size predicts db growth; it does not predict WAL growth. Triggering on
#: the actual file size needs no such prediction and stays correct when the
#: schema, the page size or the index set changes.
CHECKPOINT_WAL_BYTES = 1_048_576        # 1 MiB — ~22 batches at the measured rate
#: Backstops, not the primary trigger: a quiet daemon should still fold its WAL
#: back rather than leaving one for the next start to replay.
CHECKPOINT_EVERY_BATCHES = 500
CHECKPOINT_EVERY_SECONDS = 30.0


class PlaneWriter:
    """A held write connection with a checkpoint cadence and identity checks."""

    def __init__(self, root: Path, *, every_batches: int = CHECKPOINT_EVERY_BATCHES,
                 every_seconds: float = CHECKPOINT_EVERY_SECONDS,
                 wal_bytes: int = CHECKPOINT_WAL_BYTES) -> None:
        self.root = Path(root)
        self.every_batches = every_batches
        self.every_seconds = every_seconds
        self.wal_bytes = wal_bytes
        self._conn: sqlite3.Connection | None = None
        self._ident: tuple[int, int] | None = None
        self._since_checkpoint = 0
        self._last_checkpoint = 0.0
        #: Observability, read by the daemon's status surface and the canary.
        self.reconnects = 0
        self.checkpoints = 0
        self.checkpoint_busy = 0

    # --- identity -----------------------------------------------------------
    def _identity(self) -> tuple[int, int] | None:
        try:
            st = os.stat(db_path(self.root))
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def _is_stale(self) -> bool:
        """Has the file we hold open stopped being the file at our path?

        The reason this is a stat rather than a probe query: a replaced db does
        not make queries FAIL. The old inode is still perfectly writable while
        it is unlinked, so a SELECT succeeds, an INSERT succeeds, a commit
        fsyncs — into a file nothing can ever read. Only identity distinguishes
        that from health.
        """
        if self._conn is None:
            return True
        now = self._identity()
        return now is None or now != self._ident

    # --- lifecycle ----------------------------------------------------------
    def connection(self) -> sqlite3.Connection:
        """The live connection, reconnecting if it went bad underneath us."""
        if self._is_stale():
            self._reopen()
        return self._conn                                   # type: ignore[return-value]

    def _reopen(self) -> None:
        if self._conn is not None:
            self.reconnects += 1
            try:
                self._conn.close()
            except sqlite3.Error:
                pass                # the old handle may be unusable; that is why we are here
            self._conn = None
        conn = connect(db_path(self.root), synchronous="FULL")
        migrate(conn)               # DowngradeError propagates — the daemon exits 4 on it
        self._conn = conn
        self._ident = self._identity()
        self._since_checkpoint = 0
        self._last_checkpoint = time.monotonic()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self.checkpoint()   # leave the WAL small for the next start
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _wal_size(self) -> int:
        """Bytes of WAL right now. A stat, microseconds, once per batch.

        Deliberately the FILE rather than a frame count from SQLite: the
        ceiling this feeds is a disk-and-replay budget expressed in bytes, and
        the file is what that budget is about.
        """
        try:
            return os.path.getsize(str(db_path(self.root)) + "-wal")
        except OSError:
            return 0

    # --- checkpointing ------------------------------------------------------
    def checkpoint(self) -> tuple[int, int, int] | None:
        """TRUNCATE checkpoint. Returns SQLite's own (busy, log, checkpointed).

        The return value is recorded rather than discarded: `busy != 0` means a
        reader held a snapshot the checkpoint needed to pass, which is the one
        way the WAL grows without bound under this design. Under the old
        per-batch close that answer was unobservable, so the estate could not
        tell a checkpoint that worked from one that quietly did nothing.
        """
        if self._conn is None:
            return None
        row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self._since_checkpoint = 0
        self._last_checkpoint = time.monotonic()
        self.checkpoints += 1
        if row is not None and row[0]:
            self.checkpoint_busy += 1
        return tuple(row) if row is not None else None

    def after_batch(self) -> None:
        """Called once per committed batch; checkpoints on cadence."""
        self._since_checkpoint += 1
        due = (self._wal_size() >= self.wal_bytes
               or self._since_checkpoint >= self.every_batches
               or (time.monotonic() - self._last_checkpoint) >= self.every_seconds)
        if due:
            try:
                self.checkpoint()
            except sqlite3.Error:
                # A checkpoint failure is a WAL-SIZE event, never a durability
                # one: the commits it would have folded in are already fsync'd.
                # It must not fail the batch that has already been acknowledged.
                self._since_checkpoint = 0
                self._last_checkpoint = time.monotonic()
