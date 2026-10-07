"""Read-only message proof against actual SQL, with no transports or emitters."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import shutil
from types import SimpleNamespace
import sqlite3
import time

import pytest

from claudlobby import message_queries as q
from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files, migrate
from claudlobby.task_operations import TaskActor, TaskOperationContext
from tests.ingest_listener import DAEMON_ANSWER, listening_socket, short_socket_dir


HOST = "host_" + "0" * 32
FLEETS = {name: "fleet_" + char * 32 for name, char in (("a", "a"), ("b", "b"), ("c", "c"))}
ACTORS = {alias: TaskActor("actor_" + char * 32, alias) for alias, char in (
    ("bot:a/manager", "1"), ("bot:a/worker", "2"), ("bot:b/worker", "3"),
    ("bot:c/worker", "4"), ("human:operator", "5"), ("system:task-recheck", "6"))}


def mid(number):
    return f"msg_{number:032x}"


@pytest.fixture
def estate(tmp_path):
    path = db_file(tmp_path)
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    for alias, uid in FLEETS.items():
        conn.execute("INSERT INTO identity_registry VALUES (?, 'fleet', ?, NULL, 0, 't', 't')", (uid, alias))
    for actor in ACTORS.values():
        conn.execute("INSERT INTO identity_registry VALUES (?, 'actor', ?, NULL, 0, 't', 't')", (actor.uid, actor.alias))
    context = SimpleNamespace(paths=SimpleNamespace(root=tmp_path))
    ctx = TaskOperationContext(context, HOST, FLEETS["a"], ACTORS["bot:a/manager"], {})
    yield ctx, conn
    conn.close()


def insert(conn, table, **fields):
    event_id = f"ev_{conn.total_changes:032x}"
    seq = conn.execute("INSERT INTO ingest_ledger(event_id, family, ingested_at) VALUES (?, ?, 't')",
                       (event_id, table)).lastrowid
    row = dict(ingest_seq=seq, event_id=event_id, schema_version="1.0.0", host_uid=HOST,
               fleet_uid=FLEETS["a"], occurred_at="2026-09-28T00:00:00Z", ingested_at="t",
               emitter="test", origin="live")
    row.update(fields)
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def communication(conn, number=1, *, sender="bot:a/manager", recipient="bot:b/worker", **fields):
    row = dict(msg_id=mid(number), sender_uid=ACTORS[sender].uid, sender_alias=sender,
               recipient_uid=ACTORS[recipient].uid, recipient_alias=recipient,
               recipient_raw=recipient, message_class="chat", body="private body",
               body_bytes=12, privacy="full")
    row.update(fields)
    insert(conn, "communications", **row)
    return mid(number)


def transmission(conn, event, *, number=1, fleet="b", destination="bot:b/worker", proof="abc", size=12):
    prefix = "received" if event == "received" else "wire"
    insert(conn, "events", kind="transmission", event=event, msg_id=mid(number), carrier="tmux", attempt_no=1,
           fleet_uid=FLEETS.get(fleet), detail=json.dumps({"destination": destination,
           prefix + "_sha256": "sha256:" + proof * 22, prefix + "_bytes": size,
           "sender": "bot:c/forged-marker-sender"}))


class Clock:
    def __init__(self, callback=None):
        self.now = 0
        self.callback = callback

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.now += delay
        if self.callback:
            callback, self.callback = self.callback, None
            callback()


def test_global_id_then_participation_operator_scope_and_capture(estate):
    ctx, conn = estate
    ident = communication(conn)
    receiver = replace(ctx, caller=ACTORS["bot:b/worker"], caller_fleet_uid=FLEETS["b"])
    shown = q.show_message(receiver, ident)
    assert shown.sender == q.MessageIdentity(ACTORS["bot:a/manager"].uid, "bot:a/manager", FLEETS["a"])
    assert shown.destination.alias == "bot:b/worker" and shown.body == "private body"
    assert shown.content == "captured"
    stranger = replace(ctx, caller=ACTORS["bot:a/worker"])
    with pytest.raises(q.MessageNotFoundError):
        q.show_message(stranger, ident)
    operator = replace(ctx, caller=ACTORS["human:operator"], caller_fleet_uid=None)
    assert q.show_message(operator, ident).message_id == ident
    with pytest.raises(q.MessageNotFoundError):
        q.show_message(replace(operator, fleet_uid=FLEETS["c"]), ident)
    with pytest.raises(q.MessageQueryError, match="canonical"):
        q.show_message(ctx, "display-id")
    communication(conn, 2, privacy="metadata")
    redacted = q.show_message(ctx, mid(2))
    assert redacted.body is None and redacted.content == "withheld"
    communication(conn, 3, privacy="preview", truncated=1, body="private")
    assert q.show_message(ctx, mid(3)).content == "partial"


def test_framework_sender_is_verified_from_registry_with_recipient_scope(estate):
    ctx, conn = estate
    ident = communication(conn, sender="system:task-recheck", recipient="bot:a/manager",
                          message_class="task_request")
    transmission(conn, "pane_submitted", fleet="a", destination="bot:a/manager")
    transmission(conn, "received", fleet="a", destination="bot:a/manager")
    verified = q.receipt(ctx, ident, destination="a/manager")
    assert verified.exit_code == 0 and verified.integrity_verdict == "delivered"
    assert verified.sender == q.MessageIdentity(ACTORS["system:task-recheck"].uid,
                                                "system:task-recheck", None)
    with pytest.raises(q.MessageNotFoundError):
        q.receipt(replace(ctx, caller=ACTORS["bot:a/worker"]), ident)
    conn.execute("UPDATE communications SET sender_alias='system:forged' WHERE msg_id=?", (ident,))
    assert q.receipt(ctx, ident).receipt_observation == "unavailable"


def test_wrong_fleet_bare_receipt_cannot_corroborate_same_name_or_replace_valid_proof(estate):
    ctx, conn = estate
    stdlib_reader = runpy.run_path(str(Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/plane-readers.py"))
    assert stdlib_reader["DELIVERY_SQL"] == q.DELIVERY_STATUS_SQL
    assert stdlib_reader["RECEIPT_HISTORY_SQL"] == q.RECEIPT_HISTORY_SQL
    ident = communication(conn, recipient_raw="worker")  # historical carrier spelling
    transmission(conn, "pane_submitted", fleet="a", destination="worker")
    transmission(conn, "received", fleet="a", destination="worker")
    refused = q.receipt(ctx, ident, destination="b/worker")
    assert (refused.receipt_observation, refused.integrity_verdict, refused.exit_code) == ("no_history", "unconfirmed", 9)
    for wrong in ("a/worker", "bot:a/worker", "worker"):
        with pytest.raises(q.MessageQueryError, match="does not match"):
            q.receipt(ctx, ident, destination=wrong)
    transmission(conn, "received", fleet="b", destination="worker")
    transmission(conn, "received", fleet="a", destination="bot:b/worker", proof="def")
    final = q.receipt(ctx, ident, destination="bot:b/worker")
    assert (final.receipt_observation, final.integrity_verdict, final.exit_code) == ("received", "delivered", 0)
    assert final.sender.alias == "bot:a/manager"  # never the marker's sender
    transmission(conn, "received", destination="bot:b/worker", proof="def", size=3)
    assert q.receipt(ctx, ident).integrity_verdict == "truncated"
    transmission(conn, "received", destination="b/worker", proof="def", size=12)
    mismatch = q.receipt(ctx, ident)
    assert (mismatch.integrity_verdict, mismatch.exit_code, mismatch.code) == ("altered", 10, "receipt_mismatch")


def test_receipt_waits_for_late_sender_proof_in_fresh_snapshot(estate, monkeypatch):
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "received")
    transmission(conn, "pane_submitted", fleet="c")  # unrelated fleet cannot prove sender bytes
    incomplete = q.receipt(ctx, ident)
    assert (incomplete.receipt_observation, incomplete.integrity_verdict, incomplete.exit_code) == ("received", "unconfirmed", 8)
    clock = Clock(lambda: transmission(conn, "pane_submitted", fleet="a"))
    monkeypatch.setattr(q, "time", clock)
    final = q.receipt(ctx, ident, wait=1)
    assert final.exit_code == 0 and final.integrity_verdict == "delivered" and clock.now == 0.25
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_no_history_is_immediate_and_observable_missing_times_out_without_writes(estate, monkeypatch):
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "received", fleet=None, destination="worker")  # unscoped cannot prove a fleet
    clock = Clock()
    monkeypatch.setattr(q, "time", clock)
    none = q.receipt(ctx, ident, wait=60)
    assert none.exit_code == 9 and clock.now == 0
    assert none.destination.alias == "bot:b/worker" and str(ctx.root) in none.reason
    assert "may" in none.reason
    transmission(conn, "received", number=2)  # destination observability, not this message
    changes = conn.total_changes
    missing = q.receipt(ctx, ident, wait=1)
    assert (missing.receipt_observation, missing.integrity_verdict, missing.exit_code) == ("missing", "unknown", 8)
    assert clock.now == 1 and conn.total_changes == changes
    assert not list(ctx.root.rglob("*.jsonl")) and not (ctx.root / "state/requests").exists()


def received_event(number=1):
    """The receiver hook's event, as plane-socket-client.py finalizes it."""
    return {"event_type": "transmission", "emitter": "dispatch-in-hook", "fleet": "b",
            "event_id": "ev_" + "e" * 32, "occurred_at": "2026-09-28T00:00:00Z",
            "payload": {"msg_id": mid(number), "attempt_no": 1, "carrier": "tmux",
                        "destination": "worker", "state": "received"}}


def staged_receipt(root, number=1, name="1-ev_staged.batch"):
    """The receiver hook's batch as plane-emit.sh stages it while ingest is down."""
    staged = root / "state/plane/staged"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / name).write_text(json.dumps({"events": [received_event(number)]}) + "\n")
    return staged / name


@contextmanager
def ingest_daemon(monkeypatch, *, answer=DAEMON_ANSWER):
    """Where the receipt check probes the ingest daemon, a real listener on a
    short socket: the daemon's own answer, or with answer=None a listener that
    never answers, as a daemon slower than the check's 0.5 s probe (#2086)."""
    from claudlobby.plane import daemon
    directory = short_socket_dir("rq-")
    try:
        with listening_socket(directory / "s", answer=answer) as path, \
                monkeypatch.context() as scoped:
            scoped.setattr(daemon, "socket_path", lambda _root: path)
            yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def stage(staged, name, events, written):
    """A stage file as a stager leaves it (empty when events is None), last
    written at `written` (epoch seconds)."""
    path = staged / name
    path.write_text("" if events is None else json.dumps({"events": events}) + "\n")
    os.utime(path, (written, written))
    return path


#: An hour before the fixture's messages are recorded (2026-09-28T00:00:00Z).
BEFORE_THE_MESSAGE = datetime(2026, 9, 27, 23, 0, tzinfo=timezone.utc).timestamp()


def test_queued_receiver_proof_is_unavailable_never_missing_and_committed_proof_wins(estate, monkeypatch):
    from claudlobby.plane.spool import spool_write
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "pane_submitted", fleet="a")
    transmission(conn, "received", number=2)  # destination history exists
    clock = Clock()
    monkeypatch.setattr(q, "time", clock)
    staged = staged_receipt(ctx.root, number=3, name="1-ev_unrelated.batch").parent
    with ingest_daemon(monkeypatch):
        assert q.receipt(ctx, ident, wait=1).receipt_observation == "missing"  # unrelated work is no gate
        batch = staged_receipt(ctx.root)
        before = sorted(path.name for path in staged.iterdir())
        pending = q.receipt(ctx, ident, wait=1)
        assert (pending.receipt_observation, pending.exit_code, pending.code) == ("unavailable", 6, "unavailable")
        assert pending.integrity_verdict != "delivered" and "staged (pending" in pending.reason
        assert sorted(path.name for path in staged.iterdir()) == before  # readers never drain
        batch.unlink()
        # The daemon's own spool envelope, written by its real owner.
        spooled = spool_write(ctx.root, [received_event()], "database is locked")
        assert q.pending_transmission_proof(ctx.root, ident) == "pending"
        spooled.unlink()
        torn = staged / "2-ev_torn.batch"
        torn.write_text('{"events": [{"event_type": "transmission", "payload": {"msg_id": "msg_')
        assert q.pending_transmission_proof(ctx.root, ident) == "unavailable"
        torn.unlink()
        fifo = staged / "3-ev_fifo.batch"
        os.mkfifo(fifo)  # Must be refused before any blocking open or read.
        assert q.pending_transmission_proof(ctx.root, ident) == "unavailable"
        fifo.unlink()
        assert q.pending_transmission_proof(ctx.root, ident) == "absent"
    # Same staged handshake, only unrelated work queued, no live ingest daemon.
    assert q.pending_transmission_proof(ctx.root, ident, probe_timeout=0.1) == "unavailable"
    down = q.receipt(ctx, ident, wait=0)
    assert (down.receipt_observation, down.exit_code, down.code) == ("unavailable", 6, "unavailable")
    assert "ingest is down" in down.reason
    (ctx.root / "state/plane/spool").rmdir()
    (ctx.root / "state/plane/spool").write_text("not a directory")
    unreadable = q.receipt(ctx, ident, wait=0)
    assert (unreadable.receipt_observation, unreadable.exit_code) == ("unavailable", 6)
    transmission(conn, "received")  # committed receiver proof beside unavailable queues
    final = q.receipt(ctx, ident)
    assert (final.receipt_observation, final.integrity_verdict, final.exit_code) == ("received", "delivered", 0)


def test_orphaned_stages_written_before_the_message_neither_hold_its_proof_nor_fill_the_bound(
        estate, monkeypatch):
    """#2086, path 1. Stagers reaped before their rename leave hidden temp
    stages, and the daemon replays one only once it is an hour old. Past 256
    queued entries, every receipt check whose proof was not yet committed read
    "Plane ingest is down" for that hour, with ingest up. A file last written
    before the message existed cannot hold proof of its transmission, so it is
    neither read nor counted. One written since may hold it: it is still read,
    and still counts toward the bound, which the reason now names."""
    from claudlobby.message_operations import _retry_has_no_submission_proof
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "pane_submitted", fleet="a")
    transmission(conn, "received", number=2)  # destination history exists
    staged = ctx.root / "state/plane/staged"
    staged.mkdir(parents=True)
    for n in range(300):
        stage(staged, f".{n}-ev_{n:032x}.batch.{4_000_000 + n}.tmp", [received_event(3)],
              BEFORE_THE_MESSAGE)
    with ingest_daemon(monkeypatch):
        quiet = q.receipt(ctx, ident, wait=0)
        assert (quiet.receipt_observation, quiet.exit_code, quiet.code) == ("missing", 8, "timeout"), \
            quiet.reason
        # The retry gate has no recorded instant: every entry counts, so a resend stays refused.
        assert q.pending_transmission_proof(ctx.root, ident) == "unavailable"
        assert _retry_has_no_submission_proof(ctx.root, None, ident, None) is False
        late = stage(staged, f".9-ev_{'a' * 32}.batch.{os.getpid()}.tmp", [received_event()],
                     time.time())
        pending = q.receipt(ctx, ident, wait=0)
        assert pending.receipt_observation == "unavailable" and "staged (pending" in pending.reason
        late.unlink()
        for n in range(257):
            stage(staged, f"{n}-ev_{n:032x}.batch", [received_event(3)], time.time())
        bound = q.receipt(ctx, ident, wait=0)
    assert (bound.receipt_observation, bound.exit_code, bound.code) == ("unavailable", 6, "unavailable")
    assert bound.reason == ("257 queued Plane entries that may hold this message's proof are more than"
                            " the 256 a receipt check reads; receipt absence is unproven")


def test_an_empty_orphan_written_before_the_message_is_no_torn_entry_for_it(estate, monkeypatch):
    """#2086, at any queue depth. A stager reaped between creating its temp file
    and writing it leaves the file empty (#2164) until replay at the hour, and an
    empty entry reads as torn: every uncommitted receipt check said "Plane ingest
    is down", with one file queued. Written before the message, it cannot hold
    the proof. Written since, it may be a write in progress, so the check still
    cannot tell, and names the entry."""
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "received", number=2)
    staged = ctx.root / "state/plane/staged"
    staged.mkdir(parents=True)
    stage(staged, f".1-ev_{'1' * 32}.batch.4000001.tmp", None, BEFORE_THE_MESSAGE)
    with ingest_daemon(monkeypatch):
        quiet = q.receipt(ctx, ident, wait=0)
        assert (quiet.receipt_observation, quiet.exit_code) == ("missing", 8), quiet.reason
        name = f".2-ev_{'2' * 32}.batch.{os.getpid()}.tmp"
        stage(staged, name, None, time.time())
        torn = q.receipt(ctx, ident, wait=0)
    assert (torn.receipt_observation, torn.exit_code, torn.code) == ("unavailable", 6, "unavailable")
    assert torn.reason == (f"queued Plane entry {name} is not a whole batch (a write in progress,"
                           " or one cut short); receipt absence is unproven")


def test_a_listening_daemon_slower_than_the_probe_leaves_absence_unproven_not_ingest_down(
        estate, monkeypatch):
    """#2086, path 2, below the bound. Under #1693's load the daemon answered in
    1-3 s, so the check's 0.5 s probe missed a serving daemon and the receipt
    read "Plane ingest is down". A daemon that has not answered may still hold
    unstaged proof, so absence stays unproven, but the reason says what the
    probe saw. Only a socket nothing listens on reads as ingest down."""
    ctx, conn = estate
    ident = communication(conn)
    transmission(conn, "received", number=2)
    (ctx.root / "state/plane/staged").mkdir(parents=True)  # the staged handshake: a daemon must answer
    with ingest_daemon(monkeypatch, answer=None):
        slow = q.receipt(ctx, ident, wait=0)
        assert q.pending_transmission_proof(ctx.root, ident) == "unavailable"  # a resend stays refused
    assert (slow.receipt_observation, slow.exit_code, slow.code) == ("unavailable", 6, "unavailable")
    assert slow.reason == ("the Plane daemon is listening but did not answer within 0.5s, so it is"
                           " slow or stuck (a stopped daemon refuses the connection); receipt"
                           " absence is unproven")
    down = q.receipt(ctx, ident, wait=0)  # nothing listens on the root's own socket
    assert down.reason == ("Plane ingest is down: nothing listens on its socket; receipt absence"
                           " is unproven")


def test_reply_wait_ignores_wrong_peer_and_descendants_then_returns_first_direct_reply(estate, monkeypatch):
    ctx, conn = estate
    ident = communication(conn)
    communication(conn, 2, sender="bot:a/worker", recipient="bot:a/manager", reply_to_msg_id=ident)
    communication(conn, 3, sender="bot:b/worker", recipient="bot:a/manager", reply_to_msg_id=mid(2))
    communication(conn, 4, sender="bot:b/worker", recipient="bot:c/worker", reply_to_msg_id=ident)
    clock = Clock()
    monkeypatch.setattr(q, "time", clock)
    assert q.wait_for_reply(ctx, ident, timeout=1).exit_code == 8 and clock.now == 1

    def replies():
        communication(conn, 5, sender="bot:b/worker", recipient="bot:a/manager", reply_to_msg_id=ident,
                      occurred_at="2026-09-29T00:00:00Z")
        communication(conn, 6, sender="bot:b/worker", recipient="bot:a/manager", reply_to_msg_id=ident,
                      occurred_at="2026-09-27T00:00:00Z")
    clock.callback = replies
    reply = q.wait_for_reply(ctx, ident, timeout=1)
    assert reply.exit_code == 0 and reply.reply.message_id == mid(5) and clock.now == 1.25


def test_v12_direct_reply_index_upgrade_preserves_history_and_order(tmp_path):
    path = db_file(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        for number, sql in _migration_files():
            if number <= 12:
                conn.executescript(sql)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_intents_reply'").fetchone() is None
        conn.execute("BEGIN IMMEDIATE")
        for alias, uid in FLEETS.items():
            conn.execute("INSERT INTO identity_registry VALUES (?, 'fleet', ?, NULL, 0, 't', 't')",
                         (uid, alias))
        for actor in ACTORS.values():
            conn.execute("INSERT INTO identity_registry VALUES (?, 'actor', ?, NULL, 0, 't', 't')",
                         (actor.uid, actor.alias))
        parent = communication(conn)
        for number in range(2, 4098):
            communication(conn, number, sender="bot:b/worker", recipient="bot:a/manager")
        first = communication(conn, 4098, sender="bot:b/worker", recipient="bot:a/manager",
                              reply_to_msg_id=parent, occurred_at="2026-09-29T00:00:00Z")
        communication(conn, 4099, sender="bot:b/worker", recipient="bot:a/manager",
                      reply_to_msg_id=parent, occurred_at="2026-09-27T00:00:00Z")
        conn.execute("COMMIT")
        rows = [tuple(row) for row in conn.execute(
            "SELECT ingest_seq, event_id, msg_id, reply_to_msg_id FROM communications ORDER BY ingest_seq")]
        ledger = [tuple(row) for row in conn.execute(
            "SELECT ingest_seq, event_id FROM ingest_ledger ORDER BY ingest_seq")]
        cursor = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='ingest_ledger'").fetchone()[0]
        assert migrate(conn) == SCHEMA_USER_VERSION
        assert migrate(conn) == SCHEMA_USER_VERSION  # repeated explicit apply is a no-op
        assert [tuple(row) for row in conn.execute(
            "SELECT ingest_seq, event_id, msg_id, reply_to_msg_id FROM communications ORDER BY ingest_seq")] == rows
        assert [tuple(row) for row in conn.execute(
            "SELECT ingest_seq, event_id FROM ingest_ledger ORDER BY ingest_seq")] == ledger
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='ingest_ledger'").fetchone()[0] == cursor
        params = (parent, ACTORS["bot:b/worker"].uid, ACTORS["bot:a/manager"].uid, HOST)
        plan = conn.execute("EXPLAIN QUERY PLAN " + q.DIRECT_REPLY_SQL, params).fetchall()
        assert any("idx_intents_reply" in row[3] for row in plan)
        context = SimpleNamespace(paths=SimpleNamespace(root=tmp_path))
        ctx = TaskOperationContext(context, HOST, FLEETS["a"], ACTORS["bot:a/manager"], {})
        reply = q.wait_for_reply(ctx, parent, timeout=1)
        assert reply.exit_code == 0 and reply.reply.message_id == first


def test_absent_pending_storage_and_invalid_waits_never_initialize(estate, tmp_path):
    ctx, conn = estate
    missing_root = tmp_path / "missing"
    context = SimpleNamespace(paths=SimpleNamespace(root=missing_root))
    missing = replace(ctx, context=context)
    result = q.receipt(missing, mid(1))
    assert (result.receipt_observation, result.integrity_verdict, result.exit_code) == ("unavailable", "unknown", 6)
    assert not missing_root.exists()
    assert q.wait_for_reply(missing, mid(1), timeout=1).exit_code == 6
    with pytest.raises(q.MessageUnavailableError):
        q.show_message(missing, mid(1))
    for call in (lambda: q.receipt(ctx, mid(1), wait=61),
                 lambda: q.receipt(ctx, mid(1), wait=float("nan")),
                 lambda: q.wait_for_reply(ctx, mid(1), timeout=0)):
        with pytest.raises(q.MessageQueryError):
            call()
    conn.execute("PRAGMA user_version=0")
    assert q.receipt(ctx, mid(1)).exit_code == 6
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
