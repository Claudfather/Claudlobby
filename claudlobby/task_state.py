"""Fleet-scoped, read-only Task state over the existing Plane storage codec.

Task.task_id is work_items.work_item_id; no identities or history are rewritten.
Only claudlobby.tasks.v1 changes assignment release into queued work. Historical
terminal events keep their old closure semantics. This reader confers no actor,
manager or worker-membership authority; mutation owners must also hold their
per-task lock, check capabilities/membership and require a resolved open Task.

The supplied connection owns its access mode and any existing transaction. An
otherwise idle connection gets one read snapshot. Reads accept historical task
schemas 1 through the current SQL version; ordinary command admission remains
responsible for its separate pending-migration gate. No migrations run here.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Literal

from .plane.queries import TERMINAL_TASK_EVENTS
from .runtime_versions import SQL_SCHEMA_VERSION
from .task_audit import _display_id

TASK_EMITTER = "claudlobby.tasks.v1"
WorkState = Literal["queued", "assigned", "active", "blocked", "completed", "failed", "cancelled"]
AssignmentState = Literal["assigned", "active", "blocked", "closed"]
_OPEN = {"queued", "assigned", "active", "blocked"}
_RELEASE = {"returned_blocked", "rejected", "expired", "cancelled", "superseded", "reassigned"}
_ACTIVITY = {"accepted": "active", "progress": "active", "resumed": "active", "blocked_waiting": "blocked"}
_BATCH = 400  # safely below SQLite's older 999-variable limit


class TaskStateError(ValueError):
    """The task schema or requested state cannot be interpreted safely."""


@dataclass(frozen=True)
class TaskIssue:
    code: str
    task_id: str
    assignment_id: str | None = None
    event_id: str | None = None
    blocking: bool = True


class UnresolvedTaskError(TaskStateError):
    def __init__(self, issues: tuple[TaskIssue, ...]):
        self.issues = issues
        super().__init__(", ".join(sorted({issue.code for issue in issues})))


@dataclass(frozen=True)
class Fact:
    ingest_seq: int
    event_id: str
    schema_version: str
    host_uid: str
    fleet_uid: str | None
    emitter: str
    origin: str
    source_ref: str | None
    occurred_at: str
    ingested_at: str


@dataclass(frozen=True)
class TaskEvent(Fact):
    task_id: str
    assignment_id: str | None
    event: str
    actor_uid: str | None
    detail: str | None  # preserve stored JSON, including historical unknown fields
    deadline: str | None
    successor_id: str | None


@dataclass(frozen=True)
class Assignment(Fact):
    assignment_id: str
    task_id: str
    assignee_uid: str
    assigned_by_uid: str
    expected_by: str | None
    dispatch_message_id: str | None
    state: AssignmentState
    terminal_event: TaskEvent | None
    history: tuple[TaskEvent, ...]

    @property
    def current(self) -> bool:
        return self.terminal_event is None


@dataclass(frozen=True)
class Task(Fact):
    task_id: str
    title: str
    body: str | None
    repo: str | None
    project_key: str | None
    workstream_id: str | None
    created_by_uid: str
    state: WorkState | None
    current_assignment: Assignment | None
    assignments: tuple[Assignment, ...]
    history: tuple[TaskEvent, ...]
    terminal_event: TaskEvent | None
    display_ids: tuple[str, ...]
    issues: tuple[TaskIssue, ...]

    @property
    def blockers(self) -> tuple[TaskIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def open(self) -> bool:
        return self.state in _OPEN

    def require_resolved(self) -> Task:
        """Admission guard, not permission or a promise that work is still open.

        A mutation owner must call this again under its task lock and separately
        reject terminal/stale state. Historical, nonblocking issues stay visible.
        """
        if self.blockers:
            raise UnresolvedTaskError(self.blockers)
        return self


@dataclass(frozen=True)
class TaskSnapshot:
    schema_version: int
    fleet_uid: str
    tasks: tuple[Task, ...]
    issues: tuple[TaskIssue, ...]

    def get(self, task_id: str) -> Task | None:
        """Canonical IDs only; display IDs never become mutation aliases."""
        return next((task for task in self.tasks if task.task_id == task_id), None)

    @property
    def blockers(self) -> tuple[TaskIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def _related(conn, table: str, column: str, ids, *, task_events=False):
    """Indexed batched lookups; identifiers are private constants, never input."""
    rows = []
    values = sorted(set(ids))
    for start in range(0, len(values), _BATCH):
        batch = values[start:start + _BATCH]
        predicate = "kind='task' AND " if task_events else ""
        # Without this, SQLite can choose idx_events_kind_seq and rescan all
        # task history for every batch. Both selected indexes exist since 0001.
        index = (" INDEXED BY " + ("idx_events_item" if column == "work_item_id"
                                   else "idx_events_assignment")) if task_events else ""
        rows.extend(_rows(conn, f"SELECT * FROM {table}{index} WHERE {predicate}{column}"
                          f" IN ({','.join('?' for _ in batch)})", batch))
    return rows


def _fact(row):
    return {name: row[name] for name in Fact.__dataclass_fields__}


def _event(row):
    return TaskEvent(**_fact(row), task_id=row["work_item_id"],
                     **{name: row[name] for name in ("assignment_id", "event", "actor_uid",
                                                   "detail", "deadline", "successor_id")})


def _known(emitter):
    # Historical producers were not versioned. Refuse an unfamiliar version
    # of the new owner, rather than pretending it is an old shell producer.
    return not emitter.startswith("claudlobby.tasks.") or emitter == TASK_EMITTER


def _assignment_terminal(event):
    return _known(event.emitter) and event.event in (
        _RELEASE | {"completed", "failed"} if event.emitter == TASK_EMITTER
        else TERMINAL_TASK_EVENTS)


def _work_terminal(event):
    if not _known(event.emitter):
        return False
    if event.emitter != TASK_EMITTER:
        return event.event in TERMINAL_TASK_EVENTS
    return event.event in {"completed", "failed"} or (
        event.event == "cancelled" and event.assignment_id is None)


def read_tasks(conn: sqlite3.Connection, *, fleet_uid: str) -> TaskSnapshot:
    """Read all work owned by exactly one stored fleet UID, in one snapshot.

    No assigner/name/source-prefix ownership inference. Fetch histories by
    indexed task/assignment ID batches, not a whole-plane scan per row. The
    present schema lacks a work_items fleet index, so its single fleet filter
    scans that construct table once. Orphan scoped links remain explicit issues.
    """
    if not fleet_uid:
        raise ValueError("fleet_uid is required")
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= SQL_SCHEMA_VERSION:
            raise TaskStateError(f"unsupported task schema: {version}")
        tasks = _rows(conn, "SELECT * FROM work_items WHERE fleet_uid=? ORDER BY ingest_seq",
                      (fleet_uid,))
        assignments = _related(conn, "assignments", "work_item_id", [r["work_item_id"] for r in tasks])
        events = _related(conn, "events", "work_item_id", [r["work_item_id"] for r in tasks], task_events=True)
        # Also find malformed events naming one of our assignments but another
        # task, so that a wrong-link terminal cannot silently reopen its parent.
        events += _related(conn, "events", "assignment_id", [r["assignment_id"] for r in assignments], task_events=True)
        events = sorted({r["event_id"]: r for r in events}.values(), key=lambda r: r["ingest_seq"])
        known_assignments = {r["assignment_id"]: r for r in assignments}
        missing = {r["assignment_id"] for r in events if r["assignment_id"]} - known_assignments.keys()
        known_assignments.update((r["assignment_id"], r) for r in
                                 _related(conn, "assignments", "assignment_id", missing))
        orphan_assignments = _rows(conn, "SELECT a.* FROM assignments a LEFT JOIN work_items w"
                                   " ON w.work_item_id=a.work_item_id WHERE a.fleet_uid=?"
                                   " AND w.work_item_id IS NULL", (fleet_uid,))
        orphan_events = _rows(conn, "SELECT e.* FROM events e LEFT JOIN work_items w"
                              " ON w.work_item_id=e.work_item_id WHERE e.kind='task'"
                              " AND e.fleet_uid=? AND w.work_item_id IS NULL", (fleet_uid,))
        orphan_history = _related(conn, "events", "assignment_id",
                                  [r["assignment_id"] for r in orphan_assignments], task_events=True)
        return _reduce(version, fleet_uid, tasks, assignments, known_assignments,
                       events, orphan_assignments, orphan_events, orphan_history)
    finally:
        if own_snapshot:
            conn.rollback()


def _reduce(version, fleet, rows, assignment_rows, known_assignments,
            event_rows, orphan_assignments, orphan_events, orphan_history):
    events = tuple(_event(row) for row in event_rows)
    by_task, by_assignment = defaultdict(list), defaultdict(list)
    for event in events:
        by_task[event.task_id].append(event)
        if event.assignment_id:
            by_assignment[event.assignment_id].append(event)
    assigned = defaultdict(list)
    for row in sorted(assignment_rows, key=lambda row: row["ingest_seq"]):
        assigned[row["work_item_id"]].append(row)
    terminals = {tid: next((event for event in history if _work_terminal(event)), None)
                 for tid, history in by_task.items()}
    closed_ids = {event.assignment_id for event in events if _assignment_terminal(event)}
    for row in assignment_rows:
        terminal = terminals.get(row["work_item_id"])
        if terminal and terminal.emitter == TASK_EMITTER and terminal.event == "cancelled" \
                and terminal.assignment_id is None and row["ingest_seq"] < terminal.ingest_seq:
            closed_ids.add(row["assignment_id"])

    def unresolved_link_is_active(event):
        active = event.assignment_id not in closed_ids if event.assignment_id else not terminals.get(event.task_id)
        target = known_assignments.get(event.assignment_id)
        if target and target["work_item_id"] != event.task_id:
            active |= not terminals.get(target["work_item_id"])
        return active

    tasks = []
    for row in rows:
        tid = row["work_item_id"]
        history = tuple(by_task[tid])
        terminal = terminals.get(tid)
        issues = []
        def issue(code, aid=None, eid=None, *, blocking=None):
            issues.append(TaskIssue(code, tid, aid, eid,
                                    terminal is None if blocking is None else blocking))
        if not _known(row["emitter"]):
            issue("unknown_task_producer", eid=row["event_id"], blocking=True)
        assignments = []
        for assignment in assigned[tid]:
            aid = assignment["assignment_id"]
            ahistory = tuple(by_assignment[aid])
            closed = next((event for event in ahistory if _assignment_terminal(event)), None)
            # A v1 work cancellation explicitly closes any assignment already
            # present. A later assignment cannot reopen that terminal work.
            if terminal and terminal.emitter == TASK_EMITTER and terminal.event == "cancelled" \
                    and terminal.assignment_id is None and assignment["ingest_seq"] < terminal.ingest_seq:
                if closed is None or terminal.ingest_seq < closed.ingest_seq:
                    closed = terminal
            if assignment["fleet_uid"] != fleet:
                issue("cross_fleet_assignment" if assignment["fleet_uid"] else "unscoped_assignment",
                      aid, blocking=closed is None)
            if not _known(assignment["emitter"]):
                issue("unknown_task_producer", aid, assignment["event_id"], blocking=True)
            state = "assigned"
            for event in ahistory:
                if event.task_id != tid:
                    issue("mismatched_task_event", aid, event.event_id,
                          blocking=unresolved_link_is_active(event))
                if _known(event.emitter) and event.event in _ACTIVITY:
                    state = _ACTIVITY[event.event]
            assignments.append(Assignment(**_fact(assignment), assignment_id=aid, task_id=tid,
                assignee_uid=assignment["assignee_uid"], assigned_by_uid=assignment["assigned_by_uid"],
                expected_by=assignment["expected_by"], dispatch_message_id=assignment["dispatch_msg_id"],
                state="closed" if closed else state, terminal_event=closed, history=ahistory))
        for event in {e.event_id: e for e in history + tuple(
                e for a in assignments for e in a.history)}.values():
            if not _known(event.emitter):
                issue("unknown_task_producer", event.assignment_id, event.event_id, blocking=True)
            if event.fleet_uid != fleet:
                issue("cross_fleet_task_event" if event.fleet_uid else "unscoped_task_event",
                      event.assignment_id, event.event_id)
            target = known_assignments.get(event.assignment_id)
            if event.assignment_id and target is None:
                issue("dangling_task_event", event.assignment_id, event.event_id,
                      blocking=unresolved_link_is_active(event))
            elif target and target["work_item_id"] != event.task_id:
                issue("mismatched_task_event", event.assignment_id, event.event_id,
                      blocking=unresolved_link_is_active(event))
            elif event.assignment_id is None and (event.event in _ACTIVITY or
                    event.emitter == TASK_EMITTER and event.event in _RELEASE - {"cancelled"}):
                issue("unlinked_assignment_event", eid=event.event_id)
        current = tuple(a for a in assignments if a.current)
        if len(current) > 1:
            issue("multiple_current_assignments", blocking=True)
        if terminal and current:
            for assignment in current:
                issue("current_assignment_on_terminal_task", assignment.assignment_id, blocking=True)
        state = (terminal.event if terminal and terminal.event in {"completed", "failed"}
                 else "cancelled" if terminal else current[0].state if len(current) == 1 else "queued")
        if not terminal and any(i.blocking for i in issues):
            state = None
        tasks.append(Task(**_fact(row), task_id=tid, title=row["title"], body=row["body"],
            repo=row["repo"], project_key=row["project_key"], workstream_id=row["workstream_id"],
            created_by_uid=row["created_by_uid"], state=state,
            current_assignment=current[0] if len(current) == 1 and not terminal and not any(i.blocking for i in issues) else None,
            assignments=tuple(assignments), history=history, terminal_event=terminal,
            display_ids=tuple(sorted({_display_id(r["source_ref"]) for r in [row, *assigned[tid]]} - {None})),
            issues=tuple(dict.fromkeys(issues))))

    # Preserve A0's disagreement with the old display-ID closure grouping. A
    # closed sibling must not make another canonical row safe by coincidence.
    closed_groups = {(a.assignee_uid, a.source_ref) for task in tasks for a in task.assignments
                     if a.terminal_event and a.terminal_event.emitter != TASK_EMITTER
                     and _display_id(a.source_ref)}
    displays = defaultdict(set)
    for task in tasks:
        if task.terminal_event is None:
            for display in task.display_ids:
                displays[display].add(task.task_id)
    resolved = []
    for task in tasks:
        extra = []
        for assignment in task.assignments:
            if assignment.current and (assignment.assignee_uid, assignment.source_ref) in closed_groups:
                extra.append(TaskIssue("legacy_display_closure_disagreement", task.task_id, assignment.assignment_id))
        if task.terminal_event is None and any(len(displays[d]) > 1 for d in task.display_ids):
            extra.append(TaskIssue("ambiguous_active_display_id", task.task_id))
        if extra:
            task = replace(task, issues=task.issues + tuple(extra), current_assignment=None,
                           state=task.state if task.terminal_event else None)
        resolved.append(task)
    orphan_facts = tuple(map(_event, orphan_history + orphan_events))
    orphan_terminal = {e.assignment_id for e in orphan_facts if _assignment_terminal(e)}
    orphan_closed_work = {e.task_id for e in orphan_facts if _work_terminal(e)}
    issues = [issue for task in resolved for issue in task.issues]
    issues += [TaskIssue("dangling_assignment", r["work_item_id"], r["assignment_id"],
                         blocking=r["assignment_id"] not in orphan_terminal) for r in orphan_assignments]
    issues += [TaskIssue("dangling_task_event", r["work_item_id"], r["assignment_id"], r["event_id"],
                         blocking=(r["assignment_id"] not in orphan_terminal if r["assignment_id"]
                                   else r["work_item_id"] not in orphan_closed_work)) for r in orphan_events]
    return TaskSnapshot(version, fleet, tuple(resolved), tuple(issues))
