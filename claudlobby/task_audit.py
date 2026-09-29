"""Read-only A0 preflight for historical and supported versioned Plane tasks.

This is a migration inventory, not the new task-state reducer or a mutation
lookup. It preserves legacy terminal semantics and reports disagreements with
the old display-ID reader instead of choosing which history to rewrite.
Recompute the audit at quiescence; previews are not durable routing authority.
Only stored task fleet IDs scope reference previews. Assigner, worker names,
and source references never establish ownership.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from .plane.db import connect_ro, db_file
from .runtime_versions import SQL_SCHEMA_VERSION
from .task_state import (
    TASK_EMITTER,
    event_scope_codes,
    known_task_producer,
    legacy_display_id,
    task_closures,
    task_event_from_row,
)


class TaskAuditError(ValueError):
    """The database cannot be interpreted by this historical audit."""


@dataclass(frozen=True)
class TaskReference:
    task_id: str
    assignment_id: str | None
    fleet_uid: str | None
    assignment_fleet_uid: str | None
    display_ids: tuple[str, ...]
    active: bool
    blockers: tuple[str, ...] = ()

    @property
    def resumable(self) -> bool:
        """Linkage is unambiguous; runtime membership still needs validation."""
        return self.active and not self.blockers


@dataclass(frozen=True)
class AuditIssue:
    code: str
    task_ids: tuple[str, ...]
    assignment_ids: tuple[str, ...]
    blocking: bool
    reference: str | None = None


@dataclass(frozen=True)
class ReferencePreview:
    candidates: tuple[TaskReference, ...]
    total_matches: int

    @property
    def status(self) -> str:
        return ("not_found" if not self.total_matches else
                "unique" if self.total_matches == 1 else "ambiguous")

    @property
    def mapping(self) -> TaskReference | None:
        """A unique identity hint, possibly closed or explicitly blocked."""
        return self.candidates[0] if self.total_matches == 1 else None


@dataclass(frozen=True)
class TaskAudit:
    schema_version: int
    counts: dict[str, int]
    references: tuple[TaskReference, ...]
    issues: tuple[AuditIssue, ...]

    @property
    def blockers(self) -> tuple[AuditIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    def preview(self, reference: str, *, fleet_uid: str, active_only: bool = False,
                limit: int = 20) -> ReferencePreview:
        """Resolve a canonical ID or historical display ID within ONE fleet.

        No global fallback, newest-row preference, or legacy mutation alias.
        Cross-fleet assignment rows remain visible under their task's stored
        fleet, with both fleet IDs and a blocker. Null fleet rows cannot be
        discovered by this scoped door. ``active_only`` is an explicit
        quiescence filter; the default retains closed historical candidates.
        """
        if not fleet_uid or not 1 <= limit <= 100:
            raise ValueError("a fleet_uid and limit between 1 and 100 are required")
        matches = tuple(row for row in self.references
                        if row.fleet_uid == fleet_uid
                        and (not active_only or row.active)
                        and (reference in (row.task_id, row.assignment_id)
                             or reference in row.display_ids))
        return ReferencePreview(matches[:limit], len(matches))


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    cursor = conn.execute(sql)
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor]


def audit_root(root: Path) -> TaskAudit:
    """Open only an existing Plane database, read it, and close our connection."""
    conn = connect_ro(db_file(root))
    try:
        return audit_tasks(conn)
    finally:
        conn.close()


def audit_tasks(conn: sqlite3.Connection) -> TaskAudit:
    """Audit a supplied connection without writes, migrations, or committing it.

    An existing transaction belongs to the caller. Otherwise a read transaction
    holds one snapshot across all queries, and is rolled back on return. A0
    accepts the actual historical schema range, including an empty version-0
    database. The task-state owner supplies producer and closure semantics;
    unfamiliar future producers are refused. Three bulk row queries retain
    the whole estate, including unscoped tasks and orphan assignments/events;
    one identity lookup checks whether a legacy worker belongs to its task fleet.
    """
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SQL_SCHEMA_VERSION:
            raise TaskAuditError(f"unsupported historical task schema: {version}")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0 and not tables:
            return _audit(version, [], [], [])
        if version == 0 or not {"work_items", "assignments", "events"} <= tables:
            raise TaskAuditError("historical task tables are missing or unversioned")
        tasks = _rows(conn, "SELECT work_item_id, fleet_uid, source_ref, emitter"
                      " FROM work_items ORDER BY ingest_seq")
        assignments = _rows(conn, "SELECT ingest_seq, assignment_id, work_item_id, fleet_uid,"
                            " assignee_uid, source_ref, emitter FROM assignments ORDER BY ingest_seq")
        events = _rows(conn, "SELECT * FROM events WHERE kind='task' ORDER BY ingest_seq")
        if any(not known_task_producer(r["emitter"])
               for r in tasks + assignments + events):
            raise TaskAuditError("unsupported future task producer")
        identities = conn.execute(
            "SELECT kind, uid, alias FROM identity_registry WHERE kind IN ('actor', 'fleet')")
        actors, fleets = {}, {}
        for kind, uid, alias in identities:
            (actors if kind == "actor" else fleets)[uid] = alias
        return _audit(version, tasks, assignments, events, actors, fleets)
    finally:
        if own_snapshot:
            conn.rollback()


def _audit(version: int, tasks: list[dict], assignments: list[dict],
           events: list[dict], actors=None, fleets=None) -> TaskAudit:
    work = {r["work_item_id"]: r for r in tasks}
    assigned = {r["assignment_id"]: r for r in assignments}
    by_task: dict[str, list[dict]] = defaultdict(list)
    closures = task_closures(map(task_event_from_row, events), assignments, tasks)
    terminal_assignments, terminal_tasks = closures.assignments, closures.tasks
    issues: list[AuditIssue] = []
    actors, fleets = actors or {}, fleets or {}

    # The semantic owner preserves closure even for malformed historical
    # links; disclose those links rather than silently reopening their rows.
    for event in events:
        tid, aid = event["work_item_id"], event["assignment_id"]
        task = work.get(tid)
        if task is not None and task["fleet_uid"]:
            fact = task_event_from_row(event)
            for code in event_scope_codes(fact, task["fleet_uid"]):
                issues.append(AuditIssue(code, (tid,), (aid,) if aid else (),
                                         tid not in terminal_tasks, event["event_id"]))
        if tid not in work or (aid and aid not in assigned) or (
                aid in assigned and assigned[aid]["work_item_id"] != tid):
            active = aid not in terminal_assignments if aid else tid not in terminal_tasks
            if aid in assigned and assigned[aid]["work_item_id"] != tid:
                # A terminal event on the wrong task closes the old assignment
                # reader but cannot establish closure of its actual parent.
                active |= assigned[aid]["work_item_id"] not in terminal_tasks
            issues.append(AuditIssue("unresolved_task_event", (tid,),
                                     (aid,) if aid else (), active, event["event_id"]))

    closed_display_groups = {
        (row["assignee_uid"], row["source_ref"])
        for row in assignments
        if row["assignment_id"] in terminal_assignments
        and terminal_assignments[row["assignment_id"]].emitter != TASK_EMITTER
        and legacy_display_id(row["source_ref"])
    }
    references: list[TaskReference] = []
    for row in assignments:
        tid, aid = row["work_item_id"], row["assignment_id"]
        by_task[tid].append(row)
        task = work.get(tid)
        active = aid not in terminal_assignments
        codes = []
        if task is None:
            codes.append("dangling_assignment")
        elif not task["fleet_uid"]:
            codes.append("unscoped_task_link")
        if not row["fleet_uid"]:
            codes.append("unscoped_assignment")
        elif task and task["fleet_uid"] and task["fleet_uid"] != row["fleet_uid"]:
            codes.append("cross_fleet_assignment")
        if active and tid in terminal_tasks:
            codes.append("current_assignment_on_terminal_task")
        if active and (row["assignee_uid"], row["source_ref"]) in closed_display_groups:
            codes.append("legacy_display_closure_disagreement")
        # Legacy cross-fleet dispatches were legal, but the canonical worker
        # verbs are fleet-owned. Block cutover with an exact repair target.
        assignee = actors.get(row["assignee_uid"])
        owner_alias = fleets.get(task["fleet_uid"]) if task else None
        if (active and assignee and assignee.startswith("bot:") and owner_alias
                and not assignee.startswith(f"bot:{owner_alias}/")):
            codes.append("foreign_fleet_assignee")
        for code in codes:
            issues.append(AuditIssue(code, (tid,), (aid,), active))
        if task is not None:
            displays = {legacy_display_id(r["source_ref"]) for r in (task, row)} - {None}
            references.append(TaskReference(tid, aid, task["fleet_uid"], row["fleet_uid"],
                                            tuple(sorted(displays)), active, tuple(codes)))

    queued_after_release = set()
    for tid, task in work.items():
        active = tid not in terminal_tasks
        if not task["fleet_uid"]:
            issues.append(AuditIssue("unscoped_task", (tid,), (), active))
        current = tuple(sorted(r["assignment_id"] for r in by_task[tid]
                               if r["assignment_id"] not in terminal_assignments))
        if len(current) > 1:
            issues.append(AuditIssue("multiple_current_assignments", (tid,), current, True))
        if active and not current and any(
                (closed := terminal_assignments.get(row["assignment_id"]))
                and (closed.emitter == TASK_EMITTER or task["emitter"] == TASK_EMITTER)
                and closed.task_id == tid
                for row in by_task[tid]):
            queued_after_release.add(tid)
        if not by_task[tid] or tid in queued_after_release:
            display = legacy_display_id(task["source_ref"])
            references.append(TaskReference(tid, None, task["fleet_uid"], None,
                                            (display,) if display else (), active,
                                            () if task["fleet_uid"] else ("unscoped_task",)))

    active_displays: dict[tuple[str | None, str], list[TaskReference]] = defaultdict(list)
    for row in references:
        if row.active:
            for display in row.display_ids:
                active_displays[row.fleet_uid, display].append(row)
    for (_, display), rows in sorted(active_displays.items(), key=lambda pair: str(pair[0])):
        if len(rows) > 1:
            issues.append(AuditIssue("ambiguous_active_display_id",
                                     tuple(sorted({r.task_id for r in rows})),
                                     tuple(sorted(r.assignment_id for r in rows if r.assignment_id)),
                                     True, display))

    # Carry all task/link exceptions into previews. A unique ID may be useful
    # for an operator's repair artifact without being safe to resume.
    task_blockers: dict[str, set[str]] = defaultdict(set)
    assignment_blockers: dict[str, set[str]] = defaultdict(set)
    for issue in issues:
        if issue.blocking:
            for tid in issue.task_ids:
                task_blockers[tid].add(issue.code)
            for aid in issue.assignment_ids:
                assignment_blockers[aid].add(issue.code)
    references = [replace(row, blockers=tuple(sorted(
        task_blockers[row.task_id] | assignment_blockers.get(row.assignment_id, set()))))
        for row in references]
    totals = Counter(issue.code for issue in issues)
    counts = {
        "tasks": len(tasks),
        "scoped_tasks": sum(bool(r["fleet_uid"]) for r in tasks),
        "unscoped_tasks": sum(not r["fleet_uid"] for r in tasks),
        "active_tasks": sum(tid not in terminal_tasks for tid in work),
        "closed_tasks": sum(tid in terminal_tasks for tid in work),
        "unassigned_tasks": sum(not by_task[tid] or tid in queued_after_release for tid in work),
        "assignments": len(assignments),
        "current_assignments": sum(aid not in terminal_assignments for aid in assigned),
        "closed_assignments": sum(aid in terminal_assignments for aid in assigned),
        "dangling_assignments": totals["dangling_assignment"],
        "cross_fleet_assignments": totals["cross_fleet_assignment"],
        "unscoped_assignments": totals["unscoped_assignment"],
        "multiple_current_tasks": totals["multiple_current_assignments"],
        "ambiguous_active_display_ids": totals["ambiguous_active_display_id"],
        "blocking_issues": sum(issue.blocking for issue in issues),
        "historical_issues": sum(not issue.blocking for issue in issues),
    }
    return TaskAudit(version, counts,
                     tuple(sorted(references, key=lambda r: (r.task_id, r.assignment_id or ""))),
                     tuple(issues))
