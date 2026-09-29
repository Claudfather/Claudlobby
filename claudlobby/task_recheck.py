"""Fleet-owned, bounded manager attention over the canonical Task reducer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

from .plane.ids import mint_msg_id
from .request_facts import reconcile_facts
from .request_queries import RequestQueryError, read_request
from .request_receipts import MessageRouteBinding, ReceiptConflict, locked_request, semantic_digest
from .task_defaults import DEFAULT_MAX_AGE_H, DEFAULT_REPEAT_H
from .task_operations import (TaskConflictError, TaskOperationContext, TaskOperationUnavailableError,
                              TaskRecordingError, _existing, _identities, _prepare, _provenance,
                              _provenance_alias, _raw, _reader, _replayed, _worker, _own_fleet)
from .task_queries import task_escalations_from_snapshot
from .task_state import Task, TaskIssue, read_tasks


MAX_ROWS = 8
REF_PREFIX = "task-recheck:"


class RecheckUnresolved(TaskOperationUnavailableError):
    def __init__(self, issues: tuple[TaskIssue, ...]):
        self.issues = issues
        codes = ", ".join(sorted({issue.code for issue in issues if issue.blocking}))
        super().__init__(f"unresolved fleet Task history prevents recheck: {codes}")


@dataclass(frozen=True)
class RecheckSelection:
    rows: tuple[Task, ...]
    held: int
    uncertain: int
    waiting: int
    overflow: int
    issues: tuple[TaskIssue, ...]
    uncertain_request_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecheckResult:
    request_id: str
    selection: RecheckSelection
    task_ids: tuple[str, ...]
    message_id: str | None
    recipient_uid: str | None
    body: str | None
    replayed: bool

    @property
    def task_id(self) -> None:
        return None

    @property
    def assignment_id(self) -> None:
        return None


def _instant(value: str | None) -> datetime | None:
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return at if at and at.tzinfo else None
    except ValueError:
        return None


def _link(task: Task) -> str:
    return task.current_assignment.assignment_id if task.current_assignment else "queued"


def _ref(task: Task, request_id: str) -> str:
    return f"{REF_PREFIX}{task.task_id}:{_link(task)}:{request_id}"


def _latest_asks(conn: sqlite3.Connection, fleet_uid: str,
                 tasks: list[Task]) -> dict[tuple[str, str | None], tuple[str, str]]:
    """One bounded Plane lookup per task-ID chunk, never one query per task."""
    latest = {}
    ids = [task.task_id for task in tasks]
    for start in range(0, len(ids), 300):
        chunk = ids[start:start + 300]
        slots = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT work_item_id, assignment_id, source_ref, occurred_at FROM ("
            "SELECT work_item_id, assignment_id, source_ref, occurred_at,"
            " ROW_NUMBER() OVER (PARTITION BY work_item_id, assignment_id"
            " ORDER BY ingest_seq DESC) AS rank FROM communications"
            " WHERE fleet_uid=? AND emitter=? AND source_ref LIKE 'task-recheck:%'"
            f" AND work_item_id IN ({slots})) WHERE rank=1",
            (fleet_uid, "claudlobby.tasks.v1", *chunk),
        ).fetchall()
        latest.update(((row[0], row[1]), (row[2], row[3])) for row in rows)
    return latest


def _due(task: Task, now: datetime, max_age_s: float) -> bool:
    assignment = task.current_assignment
    deadline = _instant(assignment.expected_by) if assignment else None
    if deadline is not None and deadline <= now:
        return True
    started = _instant(assignment.occurred_at if assignment else task.occurred_at)
    return started is not None and (now - started).total_seconds() > max_age_s


def select(conn: sqlite3.Connection, *, root, fleet_uid: str, now: datetime,
           max_age_h: float = DEFAULT_MAX_AGE_H,
           repeat_h: float = DEFAULT_REPEAT_H) -> RecheckSelection:
    """One reducer snapshot; committed asks debounce by task and current link."""
    if max_age_h < 0 or repeat_h < 0:
        raise ValueError("recheck windows must be nonnegative")
    snapshot = read_tasks(conn, fleet_uid=fleet_uid)
    if any(issue.blocking for issue in snapshot.issues):
        raise RecheckUnresolved(snapshot.issues)
    escalated = {row.task_id for row in task_escalations_from_snapshot(snapshot).items}
    due = [task for task in sorted(snapshot.tasks, key=lambda item: (item.ingest_seq, item.task_id))
           if task.open and _due(task, now, max_age_h * 3600)]
    asks = _latest_asks(conn, fleet_uid, due)
    receipts = {}
    eligible, held, uncertain, waiting = [], 0, 0, 0
    uncertain_requests = set()
    for task in due:
        if task.task_id in escalated:
            waiting += 1
            continue
        prefix = f"{REF_PREFIX}{task.task_id}:{_link(task)}:"
        ask = asks.get((task.task_id, task.current_assignment.assignment_id
                        if task.current_assignment else None))
        request_id = ask[0][len(prefix):] if ask and ask[0].startswith(prefix) else None
        if request_id is not None:
            if request_id not in receipts:
                try:
                    prior = read_request(root, fleet_uid, request_id)
                    if (prior.operation != "task.recheck" or not prior.transmissions
                            or not prior.stages or prior.stages[0].proof is None
                            or prior.stages[0].proof.status != "committed"):
                        receipts[request_id] = "unknown"
                    else:
                        observation = prior.transmissions[-1].transport
                        receipts[request_id] = observation.status if observation else "unknown"
                except RequestQueryError:
                    receipts[request_id] = "unknown"
            status = receipts[request_id]
            if status == "unknown":
                uncertain += 1
                uncertain_requests.add(request_id)
                continue
            if status != "failed":
                at = _instant(ask[1])
                if at is None:
                    uncertain += 1
                    uncertain_requests.add(request_id)
                    continue
                if (now - at).total_seconds() < repeat_h * 3600:
                    held += 1
                    continue
        eligible.append(task)
    return RecheckSelection(tuple(eligible[:MAX_ROWS]), held, uncertain, waiting,
                            max(0, len(eligible) - MAX_ROWS), snapshot.issues,
                            tuple(sorted(uncertain_requests)))


def _clip(value: str, length: int = 80) -> str:
    one = " ".join((value or "").split())
    return one if len(one) <= length else one[:length].rstrip() + "…"


def digest(selection: RecheckSelection, *, fleet: str, manager: str,
           max_age_h: float, by: str) -> str:
    lines = []
    for task in selection.rows:
        assignment = task.current_assignment
        detail = f"assignment {assignment.assignment_id}, worker {assignment.assignee_uid}" if assignment else "queued, unassigned"
        deadline = f", deadline {assignment.expected_by}" if assignment and assignment.expected_by else ""
        lines.append(f"task {task.task_id} ({_clip(task.title)}; {detail}{deadline})")
    note = f"; {selection.overflow} more due" if selection.overflow else ""
    waiting = f"; {selection.waiting} escalated and waiting on the human" if selection.waiting else ""
    return (f"[Claudlobby task recheck] {fleet}: {len(lines)} open task(s) past deadline or "
            f"older than {max_age_h:g}h for current manager {manager}; requested by {by}. "
            + "; ".join(lines) + note + waiting + ". For each, use its canonical task ID "
            "with claudlobby task show TASK_ID, then record a decision or progress. "
            "An untouched task returns on a later sweep.")


def _frozen_task_ids(conn: sqlite3.Connection, receipt) -> tuple[str, ...]:
    ids = [fact.event_id for fact in receipt.intent.stages[0].facts
           if fact.family == "communication"]
    if not ids:
        return ()
    slots = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT work_item_id FROM communications WHERE fleet_uid=? "
                        f"AND emitter=? AND event_id IN ({slots}) ORDER BY ingest_seq",
                        (receipt.intent.fleet_uid, "claudlobby.tasks.v1", *ids)).fetchall()
    if len(rows) != len(ids):
        raise ReceiptConflict("frozen recheck rows differ from recorded asks")
    return tuple(row[0] for row in rows)


def recheck(ctx: TaskOperationContext, request_id: str, *, route, max_age_h: float,
            repeat_h: float, by: str | None = None) -> RecheckResult:
    """Commit all row asks before one native digest; a replay never resends."""
    from .plane.emit_api import emit_batch
    from .report_operations import AckError, _viewer_lock

    if (not isinstance(max_age_h, (float, int)) or not isinstance(repeat_h, (float, int))
            or max_age_h < 0 or repeat_h < 0):
        raise ValueError("recheck windows must be nonnegative")
    _own_fleet(ctx)
    by_alias = _provenance_alias(ctx, by)
    manager = _worker(ctx, ctx.context.fleet.manager)
    if (not isinstance(route, MessageRouteBinding) or route.caller_alias != ctx.caller.alias
            or route.caller_fleet_uid != ctx.caller_fleet_uid
            or route.recipient_alias != manager.alias or route.manager_alias != manager.alias
            or route.manager_uid != manager.uid or route.peer_fleet_uid != ctx.fleet_uid):
        raise ReceiptConflict("recheck route differs from the current fleet manager")
    semantic = semantic_digest({"max_age_h": float(max_age_h), "repeat_h": float(repeat_h),
                                "by": by_alias})
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = store.load()
        if previous is not None:
            has_message = previous.intent.message_id is not None
            previous = _existing(store, ctx, "task.recheck", semantic,
                                 manager.uid if has_message else None,
                                 fact_count=len(previous.intent.stages[0].facts),
                                 notification=has_message,
                                 route=route if has_message else None)
        try:
            with _viewer_lock(ctx.root, ctx.fleet_uid, ctx.fleet_uid,
                              namespace="task-recheck-locks"):
                with _reader(ctx) as conn:
                    if previous is not None:
                        if not _replayed(store, previous, conn):
                            raise TaskRecordingError(request_id, "previous recheck is unrecorded")
                        selection = RecheckSelection((), 0, 0, 0, 0, ())
                        return RecheckResult(request_id, selection,
                                             _frozen_task_ids(conn, previous), previous.intent.message_id,
                                             previous.intent.recipient_uid, None, True)
                    conn.execute("BEGIN")
                    selection = select(conn, root=ctx.root, fleet_uid=ctx.fleet_uid,
                                       now=datetime.now(timezone.utc), max_age_h=max_age_h,
                                       repeat_h=repeat_h)
                    conn.rollback()  # Proof reads after emit must see the newly committed facts.
                    if not selection.rows:
                        provenance = _provenance(ctx, conn, by)
                        _identities(ctx, conn, (ctx.caller, provenance))
                        raw = _raw(ctx, request_id, "system", {
                            "event": "task_recheck_noop", "subject_kind": "actor",
                            "subject_uid": provenance.uid, "subject_alias": provenance.alias}, None)
                        receipt = _prepare(store, ctx, "task.recheck", semantic, (raw,), None,
                                           actors=(provenance,))
                        store.begin_attempt()
                        store.stage(0)
                        try:
                            emit_batch(ctx.root, [raw], require_commit=True)
                        except (OSError, sqlite3.Error) as exc:
                            raise TaskRecordingError(request_id, str(exc)) from exc
                        proof = reconcile_facts(conn, receipt.intent.stages[0].facts)
                        if proof.status != "committed":
                            raise TaskRecordingError(request_id, proof.reason)
                        try:
                            store.outcome(0, "committed")
                        except OSError as exc:
                            raise TaskRecordingError(request_id, "no-op receipt outcome persistence failed",
                                                     recording="committed", request_persisted=False) from exc
                        return RecheckResult(request_id, selection, (), None, None, None, False)
                    provenance = _provenance(ctx, conn, by)
                    _identities(ctx, conn, (ctx.caller, provenance, manager))
                    body = digest(selection, fleet=ctx.context.fleet.name,
                                  manager=ctx.context.fleet.manager, max_age_h=max_age_h,
                                  by=provenance.alias)
                    primary_id = mint_msg_id()
                    raws = []
                    for index, task in enumerate(selection.rows):
                        link = task.current_assignment.assignment_id if task.current_assignment else None
                        raw = _raw(ctx, request_id, "communication", dict(
                            msg_id=primary_id if index == 0 else mint_msg_id(),
                            sender=ctx.caller.alias, recipient=manager.alias,
                            recipient_raw=ctx.context.fleet.manager,
                            message_class="task_request", command_type="query",
                            work_item_id=task.task_id,
                            body=body if index == 0 else f"Bookkeeping ask in digest {primary_id}: {task.task_id}",
                            **({"assignment_id": link} if link else {})), None,
                            fact_index=index)
                        raw["source_ref"] = _ref(task, request_id)
                        raws.append(raw)
                    raws.append(_raw(ctx, request_id, "system", {
                        "event": "task_recheck", "subject_kind": "actor",
                        "subject_uid": provenance.uid, "subject_alias": provenance.alias},
                        None, fact_index=len(raws)))
                    receipt = _prepare(store, ctx, "task.recheck", semantic, tuple(raws),
                                       None, recipient=manager.uid, actors=(provenance, manager),
                                       message_id=primary_id, notification=True, route=route)
                    store.begin_attempt()
                    store.stage(0)
                    try:
                        emit_batch(ctx.root, raws, require_commit=True)
                    except (OSError, sqlite3.Error) as exc:
                        raise TaskRecordingError(request_id, str(exc)) from exc
                    proof = reconcile_facts(conn, receipt.intent.stages[0].facts)
                    if proof.status != "committed":
                        raise TaskRecordingError(request_id, proof.reason)
                    try:
                        store.outcome(0, "committed")
                    except OSError as exc:
                        raise TaskRecordingError(request_id, "request outcome persistence failed",
                                                 recording="committed", message_id=primary_id,
                                                 recipient_uid=manager.uid,
                                                 request_persisted=False) from exc
                    return RecheckResult(request_id, selection,
                                         tuple(task.task_id for task in selection.rows), primary_id,
                                         manager.uid, body, False)
        except AckError as exc:
            raise TaskConflictError("another fleet recheck is selecting tasks") from exc
