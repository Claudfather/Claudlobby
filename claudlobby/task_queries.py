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

from .task_state import Assignment, Task, TaskIssue, TaskSnapshot, legacy_display_id, read_tasks

ListState = Literal["open", "queued", "assigned", "active", "blocked", "completed", "failed", "cancelled", "all"]
_STATES = {"open", "queued", "assigned", "active", "blocked", "completed", "failed", "cancelled", "all"}
_HINT_LIMIT = 20


@dataclass(frozen=True)
class ReferenceCandidate:
    task_id: str
    assignment_id: str | None
    # Supported argv relative to the caller's SAME resolved root/fleet context.
    command: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceHint:
    candidates: tuple[ReferenceCandidate, ...]
    total_matches: int
    next_command: tuple[str, ...] | None


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
