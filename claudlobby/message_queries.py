"""Scoped message observations. No identity creation, migration, send or retry.

The public boundary supplies the same frozen caller context as task operations.
Each poll owns a short read-only snapshot so later sender/receiver proof can
become visible. Transmission integrity comes only from DELIVERY_STATUS_SQL.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import re
import sqlite3
import time
from typing import Literal, TYPE_CHECKING

from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS
from .plane.migrations import DowngradeError
from .plane.queries import DELIVERY_STATUS_SQL, RECEIPT_HISTORY_SQL
from .plane.schema_state import PendingMigrationError, require_current_schema

if TYPE_CHECKING:
    from .task_operations import TaskOperationContext


class MessageQueryError(ValueError):
    code = "invalid_argument"
    exit_code = 2
    retryable = False


class MessageNotFoundError(MessageQueryError):
    code = "not_found"
    exit_code = 3


class MessageUnavailableError(MessageQueryError):
    code = "unavailable"
    exit_code = 6


@dataclass(frozen=True)
class MessageIdentity:
    uid: str
    alias: str
    fleet_uid: str | None


@dataclass(frozen=True)
class Message:
    message_id: str
    ingest_seq: int
    occurred_at: str
    sender: MessageIdentity
    destination: MessageIdentity | None
    message_class: str
    reply_to_message_id: str | None
    task_id: str | None
    assignment_id: str | None
    body: str | None
    body_bytes: int
    body_sha256: str | None
    privacy: str
    truncated: bool
    content: Literal["captured", "partial", "withheld"]


@dataclass(frozen=True)
class ReceiptObservation:
    message_id: str
    root: str
    sender: MessageIdentity | None
    destination: MessageIdentity | None
    receipt_observation: Literal["received", "missing", "no_history", "unavailable"]
    integrity_verdict: Literal["delivered", "truncated", "altered", "unconfirmed", "unknown"]
    exit_code: int
    code: str | None
    reason: str | None


@dataclass(frozen=True)
class ReplyObservation:
    message_id: str
    reply: Message | None
    exit_code: int
    code: str | None
    reason: str | None


def _message_id(value):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS["msg"], value):
        raise MessageQueryError("message ID must be canonical (msg_<32 lowercase hex digits>)")


def _seconds(value, minimum, name):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= 60):
        raise MessageQueryError(f"{name} must be between {minimum} and 60 seconds")


@contextmanager
def _snapshot(ctx, *, deadline=None):
    try:
        # A locked/unavailable path is not evidence that a receipt is missing.
        conn = connect_ro(db_file(ctx.root), timeout=0)
        try:
            if deadline is not None:
                # Allow one immediate observation at --wait 0/the deadline,
                # but do not let an unexpectedly large scan make an unbounded
                # wait. Interrupted reads are unavailable, never missing.
                query_deadline = max(deadline, time.monotonic() + 0.25)
                conn.set_progress_handler(lambda: time.monotonic() >= query_deadline, 1000)
            conn.execute("BEGIN")
            require_current_schema(conn)
            yield conn
        finally:
            conn.close()
    except (OSError, sqlite3.Error, PendingMigrationError, DowngradeError) as exc:
        raise MessageUnavailableError("existing Plane message history is unavailable") from exc


def _identity(conn, uid, alias):
    row = conn.execute("SELECT alias FROM identity_registry WHERE kind='actor' AND uid=?", (uid,)).fetchone()
    if row is None or row[0] != alias:
        raise MessageUnavailableError("recorded message identity is unavailable or inconsistent")
    fleet_uid = None
    bot = re.fullmatch(r"bot:([^\s:/]+)/([^\s:/]+)", alias)
    if bot:
        fleet = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias=?",
                             (bot[1],)).fetchone()
        if fleet is None:
            raise MessageUnavailableError("recorded destination fleet identity is unavailable")
        fleet_uid = fleet[0]
    elif not re.fullmatch(r"human:[^\s:/]+", alias):
        raise MessageUnavailableError("recorded message party has no supported canonical identity")
    return MessageIdentity(uid, alias, fleet_uid)


def _show(conn, ctx, message_id):
    row = conn.execute("SELECT * FROM communications WHERE msg_id=?", (message_id,)).fetchone()
    missing = MessageNotFoundError(f"message not found: {message_id}")
    if row is None or row["host_uid"] != ctx.host_uid:
        raise missing
    bot_caller = ctx.caller.alias.startswith("bot:")
    operator = ctx.caller.alias.startswith("human:") and ctx.caller_fleet_uid is None
    if not operator and (not bot_caller or ctx.caller.uid not in (row["sender_uid"], row["recipient_uid"])):
        raise missing
    caller = _identity(conn, ctx.caller.uid, ctx.caller.alias)
    if bot_caller and caller.fleet_uid != ctx.caller_fleet_uid:
        raise MessageUnavailableError("frozen caller fleet disagrees with recorded identity")
    sender = _identity(conn, row["sender_uid"], row["sender_alias"])
    destination = (_identity(conn, row["recipient_uid"], row["recipient_alias"])
                   if row["recipient_uid"] else None)
    if operator and ctx.fleet_uid not in (row["fleet_uid"], sender.fleet_uid,
                                          destination.fleet_uid if destination else None):
        raise missing
    body = row["body"] if row["privacy"] != "metadata" else None
    content = ("withheld" if body is None else "partial"
               if row["privacy"] == "preview" or row["truncated"] else "captured")
    return Message(message_id, row["ingest_seq"], row["occurred_at"], sender, destination,
                   row["message_class"], row["reply_to_msg_id"], row["work_item_id"],
                   row["assignment_id"], body, row["body_bytes"], row["body_sha256"],
                   row["privacy"], bool(row["truncated"]), content)


def show_message(ctx: TaskOperationContext, message_id: str) -> Message:
    """Resolve the global ID, then enforce participation or explicit operator scope."""
    _message_id(message_id)
    with _snapshot(ctx) as conn:
        return _show(conn, ctx, message_id)


def _destination(conn, ctx, message, supplied):
    target = message.destination
    if target is None or target.fleet_uid is None:
        raise MessageQueryError("receipt requires a recorded canonical bot destination")
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied or re.search(r"\s", supplied):
            raise MessageQueryError("destination must name the recorded bot")
        if supplied.startswith("bot:"):
            alias = supplied
        elif "/" in supplied:
            alias = "bot:" + supplied
        else:
            selected = conn.execute("SELECT alias FROM identity_registry WHERE kind='fleet' AND uid=?",
                                    (ctx.fleet_uid,)).fetchone()
            if selected is None:
                raise MessageUnavailableError("selected fleet identity is unavailable")
            alias = f"bot:{selected[0]}/{supplied}"
        if alias != target.alias:
            raise MessageQueryError("destination does not match the recorded recipient")
    return target


def receipt(ctx: TaskOperationContext, message_id: str, *, destination: str | None = None,
            wait: float = 0) -> ReceiptObservation:
    """Wait for final byte integrity, never just a receipt and never a send.

    No-history is immediate. Qualified destinations retain their fleet; a bare
    destination resolves within the already selected fleet, including on reads.
    """
    _message_id(message_id)
    _seconds(wait, 0, "wait")
    deadline = time.monotonic() + wait
    message = None
    while True:
        try:
            with _snapshot(ctx, deadline=deadline) as conn:
                observed = _show(conn, ctx, message_id)
                _destination(conn, ctx, observed, destination)
                if message is not None and observed != message:
                    raise MessageUnavailableError("recorded message changed during receipt observation")
                message = observed
                proof = conn.execute(DELIVERY_STATUS_SQL.format(ph="?"), (message_id,)).fetchone()
                received = proof["received_ingest_seq"] is not None
                history = received or bool(conn.execute(RECEIPT_HISTORY_SQL, (message_id,)).fetchone()[0])
        except MessageUnavailableError as exc:
            return ReceiptObservation(message_id, str(ctx.root), message.sender if message else None,
                message.destination if message else None, "unavailable", "unknown", 6, "unavailable", str(exc))
        observation = "received" if received else "missing" if history else "no_history"
        verdict = proof["delivery"] or ("unconfirmed" if received else "unknown")
        if verdict in ("delivered", "truncated", "altered"):
            code = None if verdict == "delivered" else "receipt_mismatch"
            reason = None if code is None else f"receipt {message_id} is {verdict}; delivery not verified"
            exit_code = 0 if code is None else 10
        elif observation == "no_history":
            code, exit_code = "receipt_unobservable", 9
            reason = (f"no receipt history for {message.destination.alias} under {ctx.root}; "
                      "hook may be inactive or lookup context may not reach it")
        elif time.monotonic() >= deadline:
            code, exit_code = "timeout", 8
            reason = f"final receipt integrity not observed within {wait}s"
        else:
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            continue
        return ReceiptObservation(message_id, str(ctx.root), message.sender, message.destination,
                                  observation, verdict, exit_code, code, reason)


def wait_for_reply(ctx: TaskOperationContext, message_id: str, *, timeout: float) -> ReplyObservation:
    """Earliest ingest-ordered direct reply from the recorded recipient only."""
    _message_id(message_id)
    _seconds(timeout, 1, "timeout")
    deadline = time.monotonic() + timeout
    parent = None
    while True:
        try:
            with _snapshot(ctx, deadline=deadline) as conn:
                observed = _show(conn, ctx, message_id)
                if parent is not None and observed != parent:
                    raise MessageUnavailableError("recorded parent changed during reply observation")
                parent = observed
                if parent.destination is None:
                    raise MessageQueryError("reply wait requires a recorded recipient identity")
                row = conn.execute(
                    "SELECT msg_id FROM communications WHERE reply_to_msg_id=?"
                    " AND sender_uid=? AND recipient_uid=? AND host_uid=?"
                    " ORDER BY ingest_seq, msg_id LIMIT 1",
                    (message_id, parent.destination.uid, parent.sender.uid, ctx.host_uid)).fetchone()
                if row:
                    return ReplyObservation(message_id, _show(conn, ctx, row[0]), 0, None, None)
        except MessageUnavailableError as exc:
            return ReplyObservation(message_id, None, 6, "unavailable", str(exc))
        if time.monotonic() >= deadline:
            return ReplyObservation(message_id, None, 8, "timeout", f"direct reply not observed within {timeout}s")
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
