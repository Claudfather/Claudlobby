"""The long-lived write connection (#1693 arm D), and the failure mode it adds.

Per-batch connect+close is structurally immune to a stale handle: it never has
one. Holding a connection for the process's life introduces exactly one new way
to be wrong, and its dangerous half is SILENT — if the db file is replaced, the
held handle keeps writing successfully into the now-unlinked inode. No
exception, no failed query, every acknowledgment a lie. Nothing that waits for
an error can detect it, because there is no error.

So these tests are mostly about that: that identity is checked rather than
health, and that the checkpoint cadence can never turn an acknowledged batch
into a failure.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from claudlobby.plane.db import db_path
from claudlobby.plane.writer import PlaneWriter


def _rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0]


class TestDurabilityRidesOnTheCommit:
    def test_the_held_connection_is_synchronous_FULL(self, tmp_path):
        """The whole design rests on this pragma. NORMAL would mean the commit
        does not fsync the WAL, and with no per-batch close there would be
        nothing else to make an acknowledged row durable."""
        w = PlaneWriter(tmp_path)
        assert w.connection().execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        w.close()

    def test_the_same_connection_is_reused_across_batches(self, tmp_path):
        """If it reconnected per batch we would be paying the old cost with
        new code."""
        w = PlaneWriter(tmp_path)
        first = w.connection()
        assert w.connection() is first
        assert w.reconnects == 0
        w.close()


class TestTheNewFailureMode:
    """A held connection that goes bad underneath itself."""

    def test_a_REPLACED_db_is_detected_although_nothing_errors(self, tmp_path):
        """THE test for the failure mode this design introduces.

        The old handle stays perfectly writable after the file is replaced —
        that is the danger. This asserts the writer notices anyway, which it
        can only do by checking IDENTITY rather than health.
        """
        w = PlaneWriter(tmp_path)
        conn = w.connection()
        db = db_path(tmp_path)

        # Positive control: the stale handle really does still work, so the
        # detection cannot be crediting an error that happened anyway.
        os.replace(db, tmp_path / "moved-aside.db")
        conn.execute("SELECT 1").fetchone()          # no exception: still writable

        assert w._is_stale(), "a replaced db must be seen as stale"
        fresh = w.connection()
        assert fresh is not conn
        assert w.reconnects == 1
        w.close()

    def test_a_DELETED_db_is_detected(self, tmp_path):
        w = PlaneWriter(tmp_path)
        conn = w.connection()
        os.unlink(db_path(tmp_path))
        assert w._is_stale()
        assert w.connection() is not conn
        assert w.reconnects == 1
        w.close()

    def test_an_untouched_db_does_NOT_reconnect(self, tmp_path):
        """The other direction, and it matters as much: a detector that
        reconnects constantly would reintroduce the per-batch cost it exists to
        remove, and nothing else in these tests would notice."""
        w = PlaneWriter(tmp_path)
        first = w.connection()
        for _ in range(20):
            assert w.connection() is first
        assert w.reconnects == 0
        w.close()

    def test_detection_is_identity_not_a_probe_query(self, tmp_path):
        """Pinning the mechanism, because the obvious alternative is wrong.

        A probe query (`SELECT 1`) reports HEALTH, and a replaced db is
        perfectly healthy — it just is not the database anyone can read. Only
        (dev, inode) separates those.
        """
        w = PlaneWriter(tmp_path)
        w.connection()
        st = os.stat(db_path(tmp_path))
        assert w._ident == (st.st_dev, st.st_ino)
        w.close()


class TestTheCheckpointCadence:
    def test_it_fires_on_WAL_SIZE_which_is_the_primary_trigger(self, tmp_path):
        """The correction the canary forced.

        The first cadence was 200 batches, derived from the plane's ~767 B/row.
        That predicts DB growth and not WAL growth: a WAL frame is a page, and
        one small event dirties pages across the table, the ledger and several
        indexes — ~45 KB per batch measured through the real daemon. At 200
        batches the WAL reaches ~9 MB, over the 4 MB ceiling the same design
        proposed. Triggering on the file size needs no such prediction.
        """
        w = PlaneWriter(tmp_path, wal_bytes=1, every_batches=10**6, every_seconds=9999)
        w.connection()
        w.connection().execute(
            "CREATE TABLE IF NOT EXISTS _probe(x)")            # make the WAL non-empty
        w.connection().execute("INSERT INTO _probe VALUES(1)")
        before = w.checkpoints
        w.after_batch()
        assert w.checkpoints == before + 1, "a WAL over the threshold must checkpoint"

    def test_a_tiny_wal_does_not_trigger_on_size_alone(self, tmp_path):
        """The other direction: an enormous threshold must leave the size
        trigger inert, or the backstops below are never what fires and the
        test above proves nothing about which trigger ran."""
        w = PlaneWriter(tmp_path, wal_bytes=10**12, every_batches=10**6,
                        every_seconds=9999)
        w.connection()
        before = w.checkpoints
        for _ in range(50):
            w.after_batch()
        assert w.checkpoints == before

    def test_it_fires_on_the_batch_count(self, tmp_path):
        w = PlaneWriter(tmp_path, every_batches=5, every_seconds=9999, wal_bytes=10**12)
        w.connection()
        before = w.checkpoints
        for _ in range(5):
            w.after_batch()
        assert w.checkpoints == before + 1
        w.close()

    def test_it_fires_on_the_clock(self, tmp_path):
        w = PlaneWriter(tmp_path, every_batches=10**6, every_seconds=0.05, wal_bytes=10**12)
        w.connection()
        before = w.checkpoints
        time.sleep(0.08)
        w.after_batch()
        assert w.checkpoints == before + 1
        w.close()

    def test_a_checkpoint_failure_does_NOT_fail_the_batch(self, tmp_path, monkeypatch):
        """The batch is already committed and already fsync'd when this runs.
        A checkpoint error is a WAL-SIZE event; raising here would turn a
        durable, acknowledged write into a reported failure."""
        w = PlaneWriter(tmp_path, every_batches=1, every_seconds=9999, wal_bytes=10**12)
        w.connection()
        monkeypatch.setattr(
            w, "checkpoint",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
        w.after_batch()                     # must not raise
        w.close()

    def test_a_busy_checkpoint_is_COUNTED_not_swallowed(self, tmp_path):
        """A reader holding a snapshot blocks truncation — the one way the WAL
        grows without bound under this design. Under the old per-batch close
        that answer was unobservable; here it is recorded."""
        w = PlaneWriter(tmp_path)
        w.connection()
        ro = sqlite3.connect(f"file:{db_path(tmp_path)}?mode=ro", uri=True)
        ro.execute("BEGIN")
        ro.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()
        row = w.checkpoint()
        assert row is not None and len(row) == 3, row
        # busy is row[0]; whether it is set depends on timing, so assert the
        # ACCOUNTING follows the answer rather than asserting the answer.
        assert w.checkpoint_busy == (1 if row[0] else 0)
        ro.close()
        w.close()


class TestTheConnectionIsOpenedLATE:
    """The ordering regression, pinned.

    The first build passed a ready-made connection: `emit_batch(root, events,
    conn=writer.connection())`. That opens the db in the ARGUMENT LIST — before
    `emit_batch` is entered, and so before the capture-config load and the
    validation that precede the connect inside it.

    It broke a root whose `state/` is a regular file: `db_path()`'s mkdir
    raises NotADirectoryError, and evaluating it early turned a typed
    `contract_violation` (the capture policy is unreadable, which is what fails
    FIRST there) into an unrouted traceback — a daemon dying where the old code
    disclosed and served. A factory is called exactly where `connect` used to
    be, so the order of failures is unchanged.
    """

    def test_the_factory_is_NOT_called_when_validation_refuses(self, tmp_path):
        from claudlobby.plane.contracts import ContractViolation
        from claudlobby.plane.emit_api import emit_batch

        called = []

        def factory():
            called.append(1)
            raise AssertionError("the db was opened before the batch was validated")

        with pytest.raises(ContractViolation):
            emit_batch(tmp_path, [{"event_type": "task", "emitter": "t", "fleet": "f",
                                   "payload": {"nonsense": True}}],
                       conn_factory=factory)
        assert called == [], "validation must refuse before anything opens the db"

    def test_the_factory_IS_called_for_a_valid_batch(self, tmp_path):
        """The positive control: a test that only ever asserts the factory was
        NOT called would pass against a build that never calls it at all."""
        from claudlobby.plane.emit_api import emit_batch

        w = PlaneWriter(tmp_path)
        calls = []

        def factory():
            calls.append(1)
            return w.connection()

        emit_batch(tmp_path, [{"event_type": "system", "emitter": "t", "fleet": "f",
                               "payload": {"event": "canary_probe",
                                           "subject_kind": "actor",
                                           "subject": "bot:f/c", "data": {"n": 1}}}],
                   conn_factory=factory)
        assert calls == [1]
        w.close()
