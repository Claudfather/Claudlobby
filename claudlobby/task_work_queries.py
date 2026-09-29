"""Read-only bot work projection of the canonical fleet Task snapshot."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .task_state import TaskIssue, read_tasks


@dataclass(frozen=True)
class CurrentWork:
    task_id: str
    assignment_id: str
    title: str
    state: str


@dataclass(frozen=True)
class BotWork:
    assignments: tuple[CurrentWork, ...] = ()
    last_completed: str | None = None
    unavailable: str = ""

    @property
    def current_task(self) -> str | None:
        return self.assignments[0].title if self.assignments and not self.unavailable else None

    @property
    def blocked(self) -> bool:
        return (not self.unavailable and bool(self.assignments)
                and all(a.state == "blocked" for a in self.assignments))


@dataclass(frozen=True)
class FleetWork:
    bots: dict[str, BotWork]
    issues: tuple[TaskIssue, ...]


def read_fleet_work(conn: sqlite3.Connection, *, fleet_uid: str, fleet: str,
                    bot_names: list[str]) -> FleetWork:
    """Reduce once; resolve roster aliases without minting identities or owners."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        snapshot = read_tasks(conn, fleet_uid=fleet_uid)
        aliases = conn.execute(
            "SELECT uid, alias FROM identity_registry WHERE kind='actor'",
        ).fetchall()
    finally:
        if own_snapshot:
            conn.rollback()
    uids: dict[str, set[str]] = {name.lower(): set() for name in bot_names}
    prefix = f"bot:{fleet}/"
    for uid, alias in aliases:
        if alias.startswith(prefix):
            name = alias[len(prefix):].lower()
            if name in uids:
                uids[name].add(uid)
    current: dict[str, list[CurrentWork]] = {name: [] for name in bot_names}
    completed: dict[str, list[tuple[int, str, str]]] = {name: [] for name in bot_names}
    for task in snapshot.tasks:
        if task.blockers:
            continue
        assignment = task.current_assignment
        if task.open and assignment is not None:
            for name in bot_names:
                if assignment.assignee_uid in uids[name.lower()]:
                    current[name].append(CurrentWork(task.task_id, assignment.assignment_id,
                                                     task.title, assignment.state))
        elif task.state == "completed" and task.terminal_event is not None:
            for historical in task.assignments:
                if historical.assignment_id != task.terminal_event.assignment_id:
                    continue
                for name in bot_names:
                    if historical.assignee_uid in uids[name.lower()]:
                        completed[name].append((task.terminal_event.ingest_seq,
                                                task.task_id, task.title))
    bots = {}
    for name in bot_names:
        ordered = tuple(sorted(current[name], key=lambda a: (a.task_id, a.assignment_id)))
        recent = max(completed[name]) if completed[name] else None
        identity_count = len(uids[name.lower()])
        unavailable = (
            f"no actor identity for bot:{fleet}/{name}" if identity_count == 0 else
            f"multiple actor identities for bot:{fleet}/{name}" if identity_count > 1 else
            "Task snapshot has unresolved blocking issues" if snapshot.blockers else ""
        )
        bots[name] = BotWork(ordered, recent[2] if recent and not unavailable else None,
                             unavailable)
    return FleetWork(bots, snapshot.issues)
