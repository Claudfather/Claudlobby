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
from typing import Iterable, Literal

from .plane.queries import TERMINAL_TASK_EVENTS
from .runtime_versions import SQL_SCHEMA_VERSION

TASK_EMITTER = "claudlobby.tasks.v1"
WorkState = Literal["queued", "assigned", "active", "blocked", "completed", "failed", "cancelled"]
AssignmentState = Literal["assigned", "active", "blocked", "closed"]
_OPEN = {"queued", "assigned", "active", "blocked"}
_RELEASE = {"returned_blocked", "rejected", "expired", "cancelled", "superseded", "reassigned"}
_ACTIVITY = {"accepted": "active", "progress": "active", "resumed": "active", "blocked_waiting": "blocked"}
_BATCH = 400  # safely below SQLite's older 999-variable limit


def legacy_display_id(source_ref: str | None) -> str | None:
    """Decode historical display metadata, never ownership or a public alias."""
    if source_ref and source_ref.startswith("dispatch-log:"):
        value = source_ref.removeprefix("dispatch-log:")
        if value and not value.startswith("sha:"):
            return value
    return None


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


def _legacy_companion_ids(conn, source_refs) -> set[str]:
    """Construct IDs sharing a selected legacy display reference.

    These reads may scan construct tables on historical schemas, but never
    scan events. Only matching siblings need history for display-group parity.
    """
    found = set()
    values = sorted(set(source_refs))
    for start in range(0, len(values), _BATCH):
        batch = values[start:start + _BATCH]
        placeholders = ",".join("?" for _ in batch)
        for table in ("work_items", "assignments"):
            found.update(row[0] for row in conn.execute(
                f"SELECT work_item_id FROM {table} WHERE source_ref IN ({placeholders})", batch))
    return found


def _fact(row):
    return {name: row[name] for name in Fact.__dataclass_fields__}


def task_event_from_row(row):
    """Decode one stored task event without changing its identity/provenance."""
    return TaskEvent(**_fact(row), task_id=row["work_item_id"],
                     **{name: row[name] for name in ("assignment_id", "event", "actor_uid",
                                                   "detail", "deadline", "successor_id")})


def known_task_producer(emitter):
    """Recognize legacy producers and the supported version of the new owner."""
    # Historical producers were not versioned. Refuse an unfamiliar version
    # of the new owner, rather than pretending it is an old shell producer.
    return not emitter.startswith("claudlobby.tasks.") or emitter == TASK_EMITTER


def _assignment_terminal(event):
    return known_task_producer(event.emitter) and event.event in (
        _RELEASE | {"completed", "failed"} if event.emitter == TASK_EMITTER
        else TERMINAL_TASK_EVENTS)


def _work_terminal(event):
    if not known_task_producer(event.emitter):
        return False
    if event.emitter != TASK_EMITTER:
        return event.event in TERMINAL_TASK_EVENTS
    return event.event in {"completed", "failed"} or (
        event.event == "cancelled" and event.assignment_id is None)


@dataclass(frozen=True)
class TaskClosures:
    """First terminal facts, shared by the scoped reader and estate audit.

    IDs may name missing constructs: closure evidence must not hide broken
    links or accidentally reopen historical rows. Callers own linkage issues
    and unknown-producer refusal; unknown facts never establish closure.
    """
    tasks: dict[str, TaskEvent]
    assignments: dict[str, TaskEvent]


def task_closures(events, assignment_rows) -> TaskClosures:
    """Reduce closure once in ingest order, including v1 work cancellation."""
    tasks, assignments = {}, {}
    for event in sorted(events, key=lambda event: event.ingest_seq):
        if _work_terminal(event):
            tasks.setdefault(event.task_id, event)
        if event.assignment_id and _assignment_terminal(event):
            assignments.setdefault(event.assignment_id, event)
    for row in assignment_rows:
        terminal = tasks.get(row["work_item_id"])
        if terminal and terminal.emitter == TASK_EMITTER and terminal.event == "cancelled" \
                and terminal.assignment_id is None and row["ingest_seq"] < terminal.ingest_seq:
            closed = assignments.get(row["assignment_id"])
            if closed is None or terminal.ingest_seq < closed.ingest_seq:
                assignments[row["assignment_id"]] = terminal
    return TaskClosures(tasks, assignments)


def read_tasks(conn: sqlite3.Connection, *, fleet_uid: str,
               task_ids: Iterable[str] | None = None) -> TaskSnapshot:
    """Read one fleet's work, or an explicit task-ID projection, in one snapshot.

    No assigner/name/source-prefix ownership inference. Fetch histories by
    indexed task/assignment ID batches, not a whole-plane scan per row. The
    present schema lacks a work_items fleet index, so its single fleet filter
    scans that construct table once only for whole-fleet reads. An explicit
    subset (including empty) uses work-item IDs plus only legacy display
    siblings needed for faithful selected-task state. Issues in the returned
    projection name selected tasks; only whole-fleet reads audit unrelated
    orphan history.
    """
    if not fleet_uid:
        raise ValueError("fleet_uid is required")
    if isinstance(task_ids, (str, bytes)):
        raise ValueError("task_ids must be an iterable of task IDs")
    selected = None if task_ids is None else tuple(task_ids)
    if selected is not None and any(not isinstance(task_id, str) or not task_id
                                    for task_id in selected):
        raise ValueError("task_ids must contain nonempty task IDs")
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not 1 <= version <= SQL_SCHEMA_VERSION:
            raise TaskStateError(f"unsupported task schema: {version}")
        if selected is None:
            tasks = _rows(conn, "SELECT * FROM work_items WHERE fleet_uid=? ORDER BY ingest_seq",
                          (fleet_uid,))
        else:
            tasks = sorted((row for row in _related(conn, "work_items", "work_item_id", selected)
                            if row["fleet_uid"] == fleet_uid), key=lambda row: row["ingest_seq"])
        requested_ids = {row["work_item_id"] for row in tasks}
        assignments = _related(conn, "assignments", "work_item_id", [r["work_item_id"] for r in tasks])
        if selected is not None and tasks:
            refs = {row["source_ref"] for row in (*tasks, *assignments)
                    if legacy_display_id(row["source_ref"])}
            companions = _legacy_companion_ids(conn, refs) - requested_ids
            siblings = [row for row in _related(conn, "work_items", "work_item_id", companions)
                        if row["fleet_uid"] == fleet_uid]
            if siblings:
                tasks = sorted([*tasks, *siblings], key=lambda row: row["ingest_seq"])
                assignments = _related(conn, "assignments", "work_item_id",
                                       [row["work_item_id"] for row in tasks])
        events = _related(conn, "events", "work_item_id", [r["work_item_id"] for r in tasks], task_events=True)
        # Also find malformed events naming one of our assignments but another
        # task, so that a wrong-link terminal cannot silently reopen its parent.
        events += _related(conn, "events", "assignment_id", [r["assignment_id"] for r in assignments], task_events=True)
        events = sorted({r["event_id"]: r for r in events}.values(), key=lambda r: r["ingest_seq"])
        known_assignments = {r["assignment_id"]: r for r in assignments}
        missing = {r["assignment_id"] for r in events if r["assignment_id"]} - known_assignments.keys()
        known_assignments.update((r["assignment_id"], r) for r in
                                 _related(conn, "assignments", "assignment_id", missing))
        if selected is not None:
            # A wrong-link event can name an assignment on another same-fleet
            # task. Its terminal event decides whether that link still blocks.
            loaded_ids = {row["work_item_id"] for row in tasks}
            target_ids = {row["work_item_id"] for row in known_assignments.values()} - loaded_ids
            target_ids = {row["work_item_id"] for row in
                          _related(conn, "work_items", "work_item_id", target_ids)
                          if row["fleet_uid"] == fleet_uid}
            # A v1 task-level cancellation closes the target's assignments
            # through their construct rows, even when that task is off-page.
            # Only wrong-link targets are needed; known_assignments already
            # fetched those exact rows by indexed assignment ID.
            assignments.extend(row for row in known_assignments.values()
                               if row["work_item_id"] in target_ids)
            events += _related(conn, "events", "work_item_id", target_ids, task_events=True)
            events = sorted({r["event_id"]: r for r in events}.values(),
                            key=lambda row: row["ingest_seq"])
        if selected is None:
            orphan_assignments = _rows(conn, "SELECT a.* FROM assignments a LEFT JOIN work_items w"
                                       " ON w.work_item_id=a.work_item_id WHERE a.fleet_uid=?"
                                       " AND w.work_item_id IS NULL", (fleet_uid,))
            orphan_events = _rows(conn, "SELECT e.* FROM events e LEFT JOIN work_items w"
                                  " ON w.work_item_id=e.work_item_id WHERE e.kind='task'"
                                  " AND e.fleet_uid=? AND w.work_item_id IS NULL", (fleet_uid,))
            orphan_history = _related(conn, "events", "assignment_id",
                                      [r["assignment_id"] for r in orphan_assignments], task_events=True)
        else:
            orphan_assignments = orphan_events = orphan_history = []
        result = _reduce(version, fleet_uid, tasks, assignments, known_assignments,
                         events, orphan_assignments, orphan_events, orphan_history)
        if selected is None:
            return result
        return replace(result,
                       tasks=tuple(task for task in result.tasks if task.task_id in requested_ids),
                       issues=tuple(issue for issue in result.issues if issue.task_id in requested_ids))
    finally:
        if own_snapshot:
            conn.rollback()


def _reduce(version, fleet, rows, assignment_rows, known_assignments,
            event_rows, orphan_assignments, orphan_events, orphan_history):
    events = tuple(task_event_from_row(row) for row in event_rows)
    by_task, by_assignment = defaultdict(list), defaultdict(list)
    for event in events:
        by_task[event.task_id].append(event)
        if event.assignment_id:
            by_assignment[event.assignment_id].append(event)
    assigned = defaultdict(list)
    for row in sorted(assignment_rows, key=lambda row: row["ingest_seq"]):
        assigned[row["work_item_id"]].append(row)
    closures = task_closures(events, assignment_rows)
    terminals, closed_ids = closures.tasks, closures.assignments

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
        if not known_task_producer(row["emitter"]):
            issue("unknown_task_producer", eid=row["event_id"], blocking=True)
        assignments = []
        for assignment in assigned[tid]:
            aid = assignment["assignment_id"]
            ahistory = tuple(by_assignment[aid])
            closed = closures.assignments.get(aid)
            if assignment["fleet_uid"] != fleet:
                issue("cross_fleet_assignment" if assignment["fleet_uid"] else "unscoped_assignment",
                      aid, blocking=closed is None)
            if not known_task_producer(assignment["emitter"]):
                issue("unknown_task_producer", aid, assignment["event_id"], blocking=True)
            state = "assigned"
            for event in ahistory:
                if event.task_id != tid:
                    issue("mismatched_task_event", aid, event.event_id,
                          blocking=unresolved_link_is_active(event))
                if known_task_producer(event.emitter) and event.event in _ACTIVITY:
                    state = _ACTIVITY[event.event]
            assignments.append(Assignment(**_fact(assignment), assignment_id=aid, task_id=tid,
                assignee_uid=assignment["assignee_uid"], assigned_by_uid=assignment["assigned_by_uid"],
                expected_by=assignment["expected_by"], dispatch_message_id=assignment["dispatch_msg_id"],
                state="closed" if closed else state, terminal_event=closed, history=ahistory))
        for event in {e.event_id: e for e in history + tuple(
                e for a in assignments for e in a.history)}.values():
            if not known_task_producer(event.emitter):
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
            display_ids=tuple(sorted({legacy_display_id(r["source_ref"]) for r in [row, *assigned[tid]]} - {None})),
            issues=tuple(dict.fromkeys(issues))))

    # Preserve A0's disagreement with the old display-ID closure grouping. A
    # closed sibling must not make another canonical row safe by coincidence.
    closed_groups = {(a.assignee_uid, a.source_ref) for task in tasks for a in task.assignments
                     if a.terminal_event and a.terminal_event.emitter != TASK_EMITTER
                     and legacy_display_id(a.source_ref)}
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
    orphan_facts = tuple(map(task_event_from_row, orphan_history + orphan_events))
    orphan_closures = task_closures(orphan_facts, orphan_assignments)
    orphan_terminal, orphan_closed_work = orphan_closures.assignments, orphan_closures.tasks
    issues = [issue for task in resolved for issue in task.issues]
    issues += [TaskIssue("dangling_assignment", r["work_item_id"], r["assignment_id"],
                         blocking=r["assignment_id"] not in orphan_terminal) for r in orphan_assignments]
    issues += [TaskIssue("dangling_task_event", r["work_item_id"], r["assignment_id"], r["event_id"],
                         blocking=(r["assignment_id"] not in orphan_terminal if r["assignment_id"]
                                   else r["work_item_id"] not in orphan_closed_work)) for r in orphan_events]
    return TaskSnapshot(version, fleet, tuple(resolved), tuple(issues))
