"""Read-only message proof against actual SQL, with no transports or emitters."""

from dataclasses import replace
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import sqlite3

import pytest

from claudlobby import message_queries as q
from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import migrate
from claudlobby.task_operations import TaskActor, TaskOperationContext


HOST = "host_" + "0" * 32
FLEETS = {name: "fleet_" + char * 32 for name, char in (("a", "a"), ("b", "b"), ("c", "c"))}
ACTORS = {alias: TaskActor("actor_" + char * 32, alias) for alias, char in (
    ("bot:a/manager", "1"), ("bot:a/worker", "2"), ("bot:b/worker", "3"),
    ("bot:c/worker", "4"), ("human:operator", "5"))}


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


def test_wrong_fleet_bare_receipt_cannot_corroborate_same_name_or_replace_valid_proof(estate):
    ctx, conn = estate
    stdlib_reader = runpy.run_path(str(Path(__file__).resolve().parents[1] / "lib/plane-readers.py"))
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
