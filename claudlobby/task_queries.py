"""Scoped task reads over the single task-state reducer; no aliases or writes.

The caller supplies a read-only connection and an already resolved fleet/bot
UID. It also resolves the fleet's current manager when presenting results;
historical assigners never become task owners here. Cursors continue immutable
admission order across fresh read snapshots, not a frozen lifecycle snapshot.
They are filter-bound continuation data, not authorization credentials.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, NoReturn
from uuid import UUID

from .plane.queries import (ASSIGNMENT_DELIVERIES_SQL, DELIVERY_ATTEMPTS_SQL, DELIVERY_MESSAGE_SQL,
                            DELIVERY_STATUS_SQL, ESCALATION_IGNORED, EVENT_OCCURRED_AT_SQL)
from .reference_hints import ReferenceCandidate, ReferenceHint
from .task_state import Assignment, Task, TaskIssue, TaskSnapshot, legacy_display_id, read_tasks

ListState = Literal["open", "queued", "assigned", "active", "blocked", "completed", "failed", "cancelled", "all"]
_STATES = {"open", "queued", "assigned", "active", "blocked", "completed", "failed", "cancelled", "all"}
_HINT_LIMIT = 20


class TaskQueryError(ValueError):
    code = "invalid_argument"
    retryable = False

    def __init__(self, message: str, *, hint: ReferenceHint | None = None):
        self.hint = hint
        super().__init__(message)


class TaskNotFoundError(TaskQueryError):
    code = "not_found"


class WrongTaskReferenceError(TaskQueryError):
    code = "wrong_reference"


class AmbiguousTaskReferenceError(TaskQueryError):
    code = "ambiguous_reference"


class InvalidTaskCursorError(TaskQueryError):
    pass


@dataclass(frozen=True)
class TaskPage:
    schema_version: int
    fleet_uid: str
    items: tuple[Task, ...]
    next_cursor: str | None
    # Includes unresolved/orphan history even when it cannot match a state
    # filter. A default-open result must not silently certify a healthy fleet.
    issues: tuple[TaskIssue, ...]


@dataclass(frozen=True)
class AssignmentView:
    assignment: Assignment
    task: Task


@dataclass(frozen=True)
class DeliveryAttempt:
    attempt_no: int | None  # the request's attempt number, as `assignment deliver` prints it
    state: str              # the recorded transmission: pane_submitted, failed, unknown, ...
    occurred_at: str


@dataclass(frozen=True)
class AssignmentDelivery:
    """What delivered an assignment, read from the rows its delivery left (#2379).

    Nothing is recorded for this read. `assignment deliver` commits a
    communication linked to the task and the assignment before it sends, each
    send records a transmission of that message, and the receiver's prompt
    hook records a `received` row. `message_id` is the board's delivery message
    (queries._DELIVERY_MSG): the legacy dispatch message where the assignment
    row carries one, else the newest delivery. `message_ids` lists every
    delivery message, oldest first, so a re-delivery shows beside the delivery
    it followed. `attempts` counts the sender's recorded attempts at
    `message_id`; `last_attempt` is the newest. The receipt is the receiver's
    newest committed `received` row, judged by DELIVERY_STATUS_SQL as
    `message receipt` judges it. A receipt still queued for ingest reads
    `not_recorded`: this read does not open the queues, `message receipt` does.
    """
    message_id: str
    message_ids: tuple[str, ...]
    request_id: str | None   # the `assignment deliver` request, for `request show`
    recorded_at: str | None  # when the delivery's communication was recorded
    attempts: int
    last_attempt: DeliveryAttempt | None
    receipt_observation: Literal["received", "not_recorded"]
    integrity_verdict: Literal["delivered", "truncated", "altered", "unconfirmed", "unknown"]
    received_at: str | None


@dataclass(frozen=True)
class TaskEscalation:
    task_id: str
    # Recorded link on the raise, which stays None for an intake raise even
    # when the work is later assigned.
    assignment_id: str | None
    event_id: str
    actor_uid: str | None
    occurred_at: str
    by: str | None
    question: str | None


@dataclass(frozen=True)
class TaskEscalations:
    schema_version: int
    fleet_uid: str
    items: tuple[TaskEscalation, ...]
    issues: tuple[TaskIssue, ...]


def _scope(fleet_uid: str, bot_uid: str | None = None) -> None:
    if not isinstance(fleet_uid, str) or not fleet_uid:
        raise TaskQueryError("fleet_uid is required")
    if bot_uid is not None and (not isinstance(bot_uid, str) or not bot_uid):
        raise TaskQueryError("bot_uid must be a resolved nonempty UID")


def _key(task: Task) -> tuple[int, str]:
    return task.ingest_seq, task.task_id


def _cursor(scope: dict, after: tuple[int, str]) -> str:
    raw = json.dumps({"v": 1, **scope, "after": after}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _after(cursor: str | None, scope: dict) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4),
                                             altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"v", "after", *scope}:
            raise ValueError
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError
        if any(type(payload[key]) is not type(value) or payload[key] != value
               for key, value in scope.items()):
            raise ValueError
        key = payload["after"]
        if (not isinstance(key, list) or len(key) != 2 or type(key[0]) is not int
                or key[0] < 1 or not isinstance(key[1], str) or not key[1]):
            raise ValueError
        return key[0], key[1]
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise InvalidTaskCursorError("invalid cursor or cursor does not match task list scope/filters") from exc


def _matches_bot(task: Task, bot_uid: str) -> bool:
    # Returned work belongs to fleet intake, not its previous worker. Closed
    # work remains discoverable by historical assignee without picking a
    # newest assignment; unresolved current links remain visible under all.
    if task.state == "queued":
        return False
    return any(assignment.fleet_uid == task.fleet_uid and assignment.assignee_uid == bot_uid
               and (assignment.current or task.terminal_event is not None)
               for assignment in task.assignments)


def list_tasks(conn: sqlite3.Connection, *, fleet_uid: str, state: ListState = "open",
               bot_uid: str | None = None, limit: int = 100,
               cursor: str | None = None) -> TaskPage:
    """Oldest admitted work first, then canonical ID; default is open intake.

    Unknown states appear with ``state='all'`` and through canonical show;
    scoped issues are always returned. Pagination never writes a read position.
    Lifecycle/filter changes between pages can change membership; already
    passed admission keys cannot reappear. Newer admissions may appear later.
    """
    _scope(fleet_uid, bot_uid)
    if not isinstance(state, str) or state not in _STATES:
        raise TaskQueryError("unknown task list state")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise TaskQueryError("limit must be between 1 and 1000")
    scope = {"fleet_uid": fleet_uid, "state": state, "bot_uid": bot_uid, "limit": limit}
    after = _after(cursor, scope)
    snapshot = read_tasks(conn, fleet_uid=fleet_uid)
    matches = sorted((task for task in snapshot.tasks
                      if (state == "all" or (task.open if state == "open" else task.state == state))
                      and (bot_uid is None or _matches_bot(task, bot_uid))
                      and (after is None or _key(task) > after)), key=_key)
    items = tuple(matches[:limit])
    token = _cursor(scope, _key(items[-1])) if len(matches) > limit else None
    return TaskPage(snapshot.schema_version, fleet_uid, items, token, snapshot.issues)


def list_task_escalations(conn: sqlite3.Connection, *, fleet_uid: str) -> TaskEscalations:
    """Current fleet-owned raises, oldest event first, with unresolved issues.

    Assignment creation alone does not answer a work-level raise. A later
    event on that assignment remains an answer after it closes, but its own
    raise and any post-closure stale event cannot hold current attention.
    """
    _scope(fleet_uid)
    return task_escalations_from_snapshot(read_tasks(conn, fleet_uid=fleet_uid))


def task_escalations_from_snapshot(snapshot: TaskSnapshot) -> TaskEscalations:
    """Project current raises from a caller's already selected task snapshot."""
    rows = []
    for task in snapshot.tasks:
        if not task.open or task.blockers:
            continue
        assignment_id = task.current_assignment.assignment_id if task.current_assignment else None
        relevant = (event for event in task.history
                    if event.assignment_id in (None, assignment_id)
                    and event.event not in ESCALATION_IGNORED)
        newest = max(relevant, key=lambda event: event.ingest_seq, default=None)
        if newest is None or newest.event != "escalated":
            continue
        if newest.assignment_id is None and any(
            event.assignment_id == assignment.assignment_id
            and newest.ingest_seq < event.ingest_seq <= assignment.terminal_event.ingest_seq
            and event.event not in (*ESCALATION_IGNORED, "escalated")
            for assignment in task.assignments if assignment.terminal_event
            for event in assignment.history
        ):
            continue
        try:
            detail = json.loads(newest.detail) if newest.detail else None
        except (ValueError, TypeError):
            detail = None
        detail = detail if isinstance(detail, dict) else {}
        by = detail.get("by")
        question = detail.get("question")
        rows.append((newest.ingest_seq, TaskEscalation(
            task.task_id, newest.assignment_id, newest.event_id, newest.actor_uid,
            newest.occurred_at, by if isinstance(by, str) else None,
            question if isinstance(question, str) else None)))
    items = tuple(row for _, row in sorted(rows, key=lambda pair: (pair[0], pair[1].task_id)))
    return TaskEscalations(snapshot.schema_version, snapshot.fleet_uid, items, snapshot.issues)


def _reference_candidates(snapshot: TaskSnapshot, reference: str, kind: str):
    """A0-style historical mapping within this snapshot, never a global scan.

    Keep closed assignments and task-plus-assignment display metadata. A
    returned task has its own unassigned intake candidate as well as history;
    ambiguity is never resolved by preferring a current or latest assignment.
    Foreign/unscoped assignment identities are not recovery destinations.
    """
    candidates = []
    for task in snapshot.tasks:
        task_display = legacy_display_id(task.source_ref)
        scoped_assignments = tuple(a for a in task.assignments if a.fleet_uid == snapshot.fleet_uid)
        for assignment in scoped_assignments:
            if reference in (task.task_id, assignment.assignment_id,
                             task_display, legacy_display_id(assignment.source_ref)):
                command = (("task", "show", task.task_id) if kind == "task"
                           else ("assignment", "show", assignment.assignment_id))
                candidates.append(ReferenceCandidate(task.task_id, assignment.assignment_id, command))
        if (not scoped_assignments or task.state == "queued") and reference in (task.task_id, task_display):
            candidates.append(ReferenceCandidate(task.task_id, None, ("task", "show", task.task_id)))
    return sorted(set(candidates), key=lambda row: (row.task_id, row.assignment_id or ""))


def _not_canonical(snapshot: TaskSnapshot, reference: str, kind: str) -> NoReturn:
    candidates = _reference_candidates(snapshot, reference, kind)
    if not candidates:
        raise TaskNotFoundError(f"{kind} not found: {reference}", hint=ReferenceHint(
            (), 0, ("task", "list", "--state", "all")))
    hint = ReferenceHint(tuple(candidates[:_HINT_LIMIT]), len(candidates),
                         candidates[0].command if len(candidates) == 1 else None)
    if len(candidates) > 1:
        raise AmbiguousTaskReferenceError(f"ambiguous reference: {reference}; select a canonical {kind} ID",
                                          hint=hint)
    raise WrongTaskReferenceError(f"wrong reference: {reference}; inspect the scoped canonical object", hint=hint)


def _lookup_snapshot(conn: sqlite3.Connection, fleet_uid: str, reference: str) -> TaskSnapshot:
    _scope(fleet_uid)
    if not isinstance(reference, str) or not reference:
        raise TaskQueryError("a canonical ID is required")
    return read_tasks(conn, fleet_uid=fleet_uid)


def show_task(conn: sqlite3.Connection, task_id: str, *, fleet_uid: str) -> Task:
    """Canonical task IDs only; unresolved history remains explicitly visible."""
    snapshot = _lookup_snapshot(conn, fleet_uid, task_id)
    task = snapshot.get(task_id)
    if task is None:
        _not_canonical(snapshot, task_id, "task")
    return task


def show_assignment(conn: sqlite3.Connection, assignment_id: str, *, fleet_uid: str) -> AssignmentView:
    """Read a canonical assignment and its parent, including closed history."""
    snapshot = _lookup_snapshot(conn, fleet_uid, assignment_id)
    for task in snapshot.tasks:
        for assignment in task.assignments:
            if assignment.assignment_id == assignment_id and assignment.fleet_uid == fleet_uid:
                return AssignmentView(assignment, task)
    _not_canonical(snapshot, assignment_id, "assignment")


#: Every statement assignment_delivery runs, for the read-cost pin.
ASSIGNMENT_DELIVERY_STATEMENTS = {
    "deliveries": ASSIGNMENT_DELIVERIES_SQL, "message": DELIVERY_MESSAGE_SQL,
    "attempts": DELIVERY_ATTEMPTS_SQL, "receipt": DELIVERY_STATUS_SQL.format(ph="?"),
    "received_at": EVENT_OCCURRED_AT_SQL,
}


def _request_id(source_ref: str | None) -> str | None:
    """The canonical request UUID a `request:<uuid>` source_ref names, else None."""
    if isinstance(source_ref, str) and source_ref.startswith("request:"):
        value = source_ref[len("request:"):]
        try:
            if str(UUID(value)) == value:
                return value
        except ValueError:
            pass
    return None


def assignment_delivery(conn: sqlite3.Connection, assignment: Assignment) -> AssignmentDelivery | None:
    """The assignment's delivery, or None when no delivery is recorded.

    Each statement seeks one assignment's, message's or event's rows by index
    (ASSIGNMENT_DELIVERY_STATEMENTS), so the read does not grow with the
    fleet's history. The caller's read snapshot keeps them consistent.
    """
    rows = conn.execute(ASSIGNMENT_DELIVERIES_SQL, (assignment.assignment_id,)).fetchall()
    recorded = {row[0]: (row[1], row[2]) for row in rows}
    message_ids = [row[0] for row in rows]
    legacy = assignment.dispatch_message_id  # the row's dispatch_msg_id, as the reducer read it
    if legacy is not None and legacy not in recorded:
        message_ids.insert(0, legacy)
    if not message_ids:
        return None
    message_id = legacy or message_ids[-1]
    row = recorded.get(message_id) or conn.execute(
        DELIVERY_MESSAGE_SQL, (message_id, assignment.host_uid)).fetchone()
    recorded_at, source_ref = row if row else (None, None)
    attempts = conn.execute(DELIVERY_ATTEMPTS_SQL, (
        message_id, assignment.host_uid, assignment.fleet_uid)).fetchall()
    proof = conn.execute(DELIVERY_STATUS_SQL.format(ph="?"), (message_id,)).fetchone()
    received = proof[4] if proof else None  # the receiver's newest `received` row
    return AssignmentDelivery(
        message_id, tuple(message_ids), _request_id(source_ref), recorded_at,
        len({attempt[0] for attempt in attempts}),
        DeliveryAttempt(*attempts[-1]) if attempts else None,
        "received" if received is not None else "not_recorded",
        # The verdict as message_queries.receipt reads the same row.
        (proof[3] if proof else None) or ("unconfirmed" if received is not None else "unknown"),
        conn.execute(EVENT_OCCURRED_AT_SQL, (received,)).fetchone()[0] if received is not None else None)
