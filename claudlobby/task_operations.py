"""Durable task admission, routing, acceptance and withdrawal, no delivery.

The public boundary supplies resolved Context and existing frozen identities,
and owns runtime/release admission. Request lock precedes the per-task lock;
only emit_batch(require_commit=True) writes Plane facts. Receipts are proof
coordinates, never a second work-state store or a conditional replay queue.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
from types import MappingProxyType
from typing import Literal, Mapping, TYPE_CHECKING

from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS, mint_assignment_id, mint_event_id, mint_msg_id, mint_work_item_id
from .plane.queries import checkin_event_scope_sql, fleet_alias_range, fleet_range_params
from .plane.schema_state import PendingMigrationError, require_current_schema
from .report_payload import ReportLink, ReportPayload, encode_report_facts
from .request_facts import expected_fact, reconcile_facts
from .request_receipts import (MessageRouteBinding, ReceiptConflict, RequestIntent,
                               StagePlan, locked_request, semantic_digest)
from .task_queries import TaskNotFoundError, TaskQueryError, show_assignment, show_task
from .task_state import TASK_EMITTER, Task

if TYPE_CHECKING:
    from .context import Context


@dataclass(frozen=True)
class TaskActor:
    uid: str
    alias: str


@dataclass(frozen=True)
class TaskOperationContext:
    context: Context
    host_uid: str
    fleet_uid: str
    caller: TaskActor
    bots: Mapping[str, TaskActor]
    caller_fleet_uid: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "bots", MappingProxyType(dict(self.bots)))
        for value, kind in ((self.host_uid, "host"), (self.fleet_uid, "fleet"),
                            (self.caller.uid, "actor"), *((a.uid, "actor") for a in self.bots.values())):
            if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
                raise TaskQueryError("operation context requires canonical frozen identities")
        if any(not isinstance(actor.alias, str) or not actor.alias for actor in (self.caller, *self.bots.values())):
            raise TaskQueryError("operation context requires existing actor aliases")
        if self.caller.alias.startswith("bot:") and self.caller_fleet_uid is None:
            object.__setattr__(self, "caller_fleet_uid", self.fleet_uid)
        if self.caller_fleet_uid is not None and (not isinstance(self.caller_fleet_uid, str)
                or not re.fullmatch(ID_PATTERNS["fleet"], self.caller_fleet_uid)):
            raise TaskQueryError("operation context requires a canonical caller fleet")

    @property
    def root(self) -> Path:
        return self.context.paths.root


class TaskConflictError(TaskQueryError):
    code = "conflict"


class TaskOperationUnavailableError(TaskQueryError):
    code = "unavailable"


class TaskRecordingError(TaskQueryError):
    code = "unavailable"
    recording = "unknown"

    def __init__(self, request_id, reason, *, recording="unknown", task_id=None,
                 assignment_id=None, message_id=None, recipient_uid=None,
                 request_persisted=None):
        self.request_id = request_id
        self.recording = recording
        self.task_id = task_id
        self.assignment_id = assignment_id
        self.message_id = message_id
        self.recipient_uid = recipient_uid
        self.request_persisted = request_persisted
        prefix = "recording committed" if recording == "committed" else "recording not proved"
        super().__init__(f"{prefix}: {reason}; inspect request {request_id} before retrying")


@dataclass(frozen=True)
class TaskOperationResult:
    request_id: str
    task_id: str
    assignment_id: str | None
    task: Task  # current read, not a receipt-cached lifecycle state
    replayed: bool
    recording: Literal["committed"] = "committed"
    delivery: Literal["not_requested"] = "not_requested"
    notification: Literal["not_requested", "pending", "unknown", "received", "submitted", "failed"] = "not_requested"
    message_id: str | None = None
    recipient_uid: str | None = None


@contextmanager
def _locked_task(store, task_id):
    store.assert_locked()
    if not re.fullmatch(ID_PATTERNS["work_item"], task_id):
        raise TaskQueryError("task lock requires a canonical task ID")
    directory = store.path.parent.parent.parent  # existing request owner proved state/
    for part in ("task-locks", store.fleet_uid):
        directory = directory / part
        if directory.is_symlink():
            raise TaskConflictError("task lock directory is redirected")
        directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (task_id + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskConflictError("task lock must be an owned private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TaskConflictError("task is already being changed") from exc
        def check():
            store.assert_locked()
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise TaskConflictError("task lock was replaced")
        check()
        yield check
    finally:
        os.close(fd)


@contextmanager
def _reader(ctx):
    try:
        conn = connect_ro(db_file(ctx.root))
    except FileNotFoundError as exc:
        raise PendingMigrationError("task operations require an explicitly initialized Plane") from exc
    except (OSError, sqlite3.Error) as exc:
        raise TaskOperationUnavailableError("task operation Plane storage is unavailable") from exc
    with closing(conn):
        try:
            require_current_schema(conn)
            yield conn
        except sqlite3.Error as exc:
            raise TaskOperationUnavailableError("task operation Plane storage is unavailable") from exc


def _identities(ctx, conn, actors):
    # Supported registry writers never delete/rebind aliases. Require their
    # existing exact bindings, so ingest's lazy resolver cannot create one.
    if (ctx.root / "state/host-uid").read_text().strip() != ctx.host_uid:
        raise TaskConflictError("host identity differs from the operation context")
    expected = [("fleet", ctx.context.fleet.name, ctx.fleet_uid)]
    expected += [("actor", actor.alias, actor.uid) for actor in actors]
    for kind, alias, uid in expected:
        row = conn.execute("SELECT uid, parent_uid FROM identity_registry WHERE kind=? AND alias=?",
                           (kind, alias)).fetchone()
        parent = ctx.caller_fleet_uid if kind == "actor" and alias == ctx.caller.alias else ctx.fleet_uid
        if kind == "actor" and alias.startswith("bot:") and alias == ctx.caller.alias:
            origin = conn.execute("SELECT alias FROM identity_registry WHERE kind='fleet' AND uid=?",
                                  (parent,)).fetchone()
            if origin is None or not alias.startswith(f"bot:{origin[0]}/"):
                raise TaskConflictError("frozen caller origin is absent or changed")
        if (row is None or row[0] != uid or kind == "actor" and not alias.startswith("human:")
                and row[1] not in (None, parent)):
            raise TaskConflictError("frozen identity is absent, foreign or changed")


def _worker(ctx, bot_id):
    if bot_id not in ctx.context.fleet.bots or bot_id not in ctx.bots:
        raise TaskQueryError("worker is not a declared member of the selected fleet")
    return ctx.bots[bot_id]


def _own_fleet(ctx):
    if ctx.caller.alias.startswith("bot:") and ctx.caller_fleet_uid != ctx.fleet_uid:
        raise TaskConflictError("bot caller may change routing only within its origin fleet")


def _reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise TaskQueryError("a nonempty reason is required")


def _scope_links(ctx, conn, project_key, workstream_id, repo):
    projects = ctx.context.fleet.projects
    if project_key is not None:
        if project_key not in projects:
            raise TaskQueryError("project is not declared in the selected fleet")
        if repo is not None and projects[project_key].repos and repo not in projects[project_key].repos:
            raise TaskQueryError("repository does not belong to the declared project")
    if workstream_id is not None:
        row = conn.execute("SELECT fleet_uid, project_key FROM workstreams WHERE workstream_id=?",
                           (workstream_id,)).fetchone()
        if row is None or row[0] != ctx.fleet_uid:
            raise TaskQueryError("workstream does not belong to the selected fleet")
        if row[1] is not None and (row[1] not in projects or project_key is not None and row[1] != project_key):
            raise TaskQueryError("workstream project differs from the selected task project")


def _provenance(ctx, conn, alias):
    # The context module owns identity resolution; this claim never substitutes
    # for the authoritative resolved caller frozen in the request receipt.
    from .operation_context import resolve_task_provenance
    return resolve_task_provenance(ctx, alias, conn)


def _provenance_alias(ctx, alias):
    from .operation_context import canonical_task_provenance_alias
    return canonical_task_provenance_alias(ctx, alias)


def _checkin_decision(ctx, conn, checkin_id):
    if not isinstance(checkin_id, str) or not re.fullmatch(r"ck_[0-9a-f]{32}", checkin_id):
        raise TaskQueryError("check-in ID must have canonical ck_<32hex> form")
    # The existing decision reader admits only a bot of this fleet. Require
    # that same visible decision even when a malformed fact claims our UID.
    sql = ("SELECT 1 FROM events e WHERE e.kind='system' AND e.event='checkin_decision'"
           " AND e.source_ref=? AND " + checkin_event_scope_sql("e")
           + " AND " + fleet_alias_range("e.subject_alias") + " LIMIT 1")
    try:
        found = conn.execute(sql, (f"checkin:{checkin_id}", ctx.context.fleet.name,
                                   *fleet_range_params(ctx.context.fleet.name),
                                   *fleet_range_params(ctx.context.fleet.name))).fetchone()
    except sqlite3.Error as exc:
        raise TaskOperationUnavailableError("check-in decision registry is unavailable") from exc
    if found is None:
        raise TaskNotFoundError("check-in decision not found in the selected fleet")


def _existing(store, ctx, operation, semantic, recipient=None, *, fact_count=1, notification=False,
              route=None):
    receipt = store.load()
    if receipt is not None:
        intent = receipt.intent
        if ((intent.operation, intent.operation_version, intent.host_uid, intent.fleet_uid,
             intent.caller_uid, intent.recipient_uid, intent.semantic_sha256)
                != (operation, 1, ctx.host_uid, ctx.fleet_uid, ctx.caller.uid, recipient, semantic)
                or tuple(stage.kind for stage in intent.stages) != (
                    ("recording", "notification") if notification else ("recording",))
                or len(intent.stages[0].facts) != fact_count or intent.route != route
                or notification and (intent.message_id is None or intent.recipient_uid is None
                                     or intent.stages[1].facts)):
            raise ReceiptConflict("request UUID already has different semantics or identities")
    return receipt


def _replayed(store, receipt, conn):
    if receipt is None:
        return False
    proof = reconcile_facts(conn, receipt.intent.stages[0].facts)
    status = receipt.stages[0].status
    if proof.status == "committed":
        if status == "unknown":
            store.outcome(0, "committed")
        elif status != "committed":
            raise ReceiptConflict("facts exist without a compatible recorded attempt")
        return True
    if proof.status == "unknown":
        raise TaskRecordingError(receipt.request_id, proof.reason)
    if proof.status == "conflict" or status == "committed":
        raise ReceiptConflict("request fact proof conflicts with its durable receipt")
    if status == "unknown":
        store.outcome(0, "unrecorded")
    return False


def _raw(ctx, request_id, family, payload, receipt, *, fact_index=0):
    from .plane import PLANE_SCHEMA_VERSION
    return {"event_type": family, "payload": payload, "emitter": TASK_EMITTER,
            "fleet": ctx.context.fleet.name, "source_ref": f"request:{request_id}",
            "schema_version": PLANE_SCHEMA_VERSION,
            "event_id": receipt.intent.stages[0].facts[fact_index].event_id if receipt else mint_event_id()}


def _prepare(store, ctx, operation, semantic, raws, task_id, assignment_id=None, recipient=None, actors=(),
             *, message_id=None, notification=False, route=None):
    from .plane.emit_api import CONTENT_FIELDS, load_capture_config, validate_item
    modes = load_capture_config(ctx.root) if any(CONTENT_FIELDS.get(r["event_type"]) for r in raws) else {}
    parties = {actor.alias: actor.uid for actor in (ctx.caller, *actors)}
    facts = tuple(expected_fact(validate_item(raw, modes)[0], host_uid=ctx.host_uid,
                               fleet_uid=ctx.fleet_uid, parties=parties) for raw in raws)
    stages = (StagePlan("recording", facts),) + ((StagePlan("notification"),) if notification else ())
    return store.prepare(RequestIntent(operation, 1, ctx.host_uid, ctx.fleet_uid, ctx.caller.uid,
                         recipient, semantic, stages,
                         task_id=task_id, assignment_id=assignment_id, message_id=message_id,
                         route=route))


def _result(ctx, conn, receipt, replayed):
    notification = "not_requested"
    if len(receipt.stages) == 2:
        state = receipt.stages[1].status
        notification = "pending" if state == "prepared" else state
    return TaskOperationResult(receipt.request_id, receipt.intent.task_id, receipt.intent.assignment_id,
                               show_task(conn, receipt.intent.task_id, fleet_uid=ctx.fleet_uid), replayed,
                               notification=notification, message_id=receipt.intent.message_id,
                               recipient_uid=receipt.intent.recipient_uid)


def _commit(store, ctx, conn, receipt, raws, check_lock):
    from .plane.emit_api import emit_batch
    check_lock()
    store.begin_attempt()
    store.stage(0)  # durable unknown before SQLite; crashes never authorize blind replay
    try:
        emit_batch(ctx.root, list(raws), require_commit=True)
    except (sqlite3.Error, OSError) as exc:
        # Do not turn an exception into an unchanged result, even if it looks
        # like a connection failure. The next invocation reconciles exact IDs.
        raise TaskRecordingError(receipt.request_id, str(exc)) from exc
    proof = reconcile_facts(conn, receipt.intent.stages[0].facts)
    if proof.status == "conflict":
        raise ReceiptConflict("committed result differs from the frozen request facts")
    if proof.status != "committed":
        raise TaskRecordingError(receipt.request_id, proof.reason)
    try:
        receipt = store.outcome(0, "committed")
    except OSError as exc:
        # Exact Plane facts have already been proved. The final receipt
        # outcome write can fail after rename; disclose the lost persistence
        # guarantee and never attempt a notification from this invocation.
        intent = receipt.intent
        raise TaskRecordingError(receipt.request_id, "request outcome persistence failed",
                                 recording="committed", task_id=intent.task_id,
                                 assignment_id=intent.assignment_id,
                                 message_id=intent.message_id,
                                 recipient_uid=intent.recipient_uid,
                                 request_persisted=False) from exc
    return _result(ctx, conn, receipt, False)


def admit(ctx: TaskOperationContext, request_id: str, *, title: str, body: str | None = None,
          repo: str | None = None, project_key: str | None = None,
          workstream_id: str | None = None, by: str | None = None) -> TaskOperationResult:
    """Admit fleet-owned intake, deliberately creating no assignment or message."""
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(title=title, body=body, repo=repo, project_key=project_key,
                                    workstream_id=workstream_id, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.admit", semantic)
        tid = previous.intent.task_id if previous else mint_work_item_id()
        with _locked_task(store, tid) as check, _reader(ctx) as conn:
            if _replayed(store, previous, conn):
                return _result(ctx, conn, previous, True)
            provenance = _provenance(ctx, conn, by)
            _identities(ctx, conn, (ctx.caller, provenance))
            _scope_links(ctx, conn, project_key, workstream_id, repo)
            raw = _raw(ctx, request_id, "work_item", dict(work_item_id=tid, title=title, body=body,
                       created_by=provenance.alias, repo=repo, project_key=project_key,
                       workstream_id=workstream_id), previous)
            receipt = _prepare(store, ctx, "task.admit", semantic, (raw,), tid, actors=(provenance,))
            return _commit(store, ctx, conn, receipt, (raw,), check)


def assign(ctx: TaskOperationContext, request_id: str, task_id: str, *, bot_id: str,
           expected_by: str | None = None, checkin_id: str | None = None,
           by: str | None = None) -> TaskOperationResult:
    """Route one queued task, serialized with every other supported task mutation."""
    _own_fleet(ctx)
    worker = _worker(ctx, bot_id)
    by_alias = _provenance_alias(ctx, by)
    if checkin_id is not None and (not isinstance(checkin_id, str)
                                   or not re.fullmatch(r"ck_[0-9a-f]{32}", checkin_id)):
        raise TaskQueryError("check-in ID must have canonical ck_<32hex> form")
    semantic = semantic_digest(dict(task_id=task_id, bot_id=bot_id, expected_by=expected_by,
                                    checkin_id=checkin_id, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.assign", semantic, worker.uid,
                             fact_count=2 if checkin_id is not None else 1)
        with _reader(ctx) as conn:
            task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)  # canonical recovery errors
            with _locked_task(store, task.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                if task.state != "queued" or task.current_assignment is not None:
                    raise TaskConflictError("task is not queued and unassigned")
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, worker, provenance))
                _scope_links(ctx, conn, task.project_key, task.workstream_id, task.repo)
                if checkin_id is not None:
                    _checkin_decision(ctx, conn, checkin_id)
                aid = previous.intent.assignment_id if previous else mint_assignment_id()
                raw = _raw(ctx, request_id, "assignment", dict(assignment_id=aid, work_item_id=task_id,
                           assignee=worker.alias, assigned_by=provenance.alias, expected_by=expected_by), previous)
                raws = (raw,)
                if checkin_id is not None:
                    join = _raw(ctx, request_id, "system", {
                        "event": "checkin_dispatch", "subject_kind": "actor", "subject": ctx.caller.alias,
                        "data": {"checkin_id": checkin_id, "assignment_id": aid,
                                 "work_item_id": task_id, "task_id": None},
                    }, previous, fact_index=1)
                    raws = (raw, join)
                receipt = _prepare(store, ctx, "task.assign", semantic, raws, task_id, aid, worker.uid,
                                   (worker, provenance))
                return _commit(store, ctx, conn, receipt, raws, check)


def accept(ctx: TaskOperationContext, request_id: str, assignment_id: str) -> TaskOperationResult:
    """Accept the caller's exact current assignment; receipts/delivery are not acceptance."""
    semantic = semantic_digest(dict(assignment_id=assignment_id))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "assignment.accept", semantic)
        with _reader(ctx) as conn:
            first = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                view = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
                task = _current_assignee(ctx, view)
                if task.state != "assigned":
                    raise TaskConflictError("assignment is already accepted")
                _identities(ctx, conn, (ctx.caller,))
                raw = _raw(ctx, request_id, "task", dict(work_item_id=task.task_id,
                           assignment_id=assignment_id, event="accepted", actor=ctx.caller.alias), previous)
                receipt = _prepare(store, ctx, "assignment.accept", semantic, (raw,), task.task_id, assignment_id)
                return _commit(store, ctx, conn, receipt, (raw,), check)


def _current_assignee(ctx, view):
    task = view.task.require_resolved()
    if (view.assignment.assignee_uid != ctx.caller.uid
            or not any(_worker(ctx, bot) == ctx.caller for bot in ctx.bots if bot in ctx.context.fleet.bots)):
        raise TaskConflictError("only the declared current assignee can act")
    if not task.open or task.current_assignment != view.assignment:
        raise TaskConflictError("assignment is stale or closed")
    return task


def _assignment_report(ctx, request_id, assignment_id, report, *, verb, status, transition,
                       text_field, route=None):
    _own_fleet(ctx)
    if not isinstance(report, ReportPayload) or report.status != status or getattr(report, text_field) is None:
        raise TaskQueryError(f"assignment {verb} requires a {status} report with {text_field}")
    if route is not None and (not isinstance(route, MessageRouteBinding)
            or route.caller_alias != ctx.caller.alias
            or route.caller_fleet_uid != ctx.caller_fleet_uid
            or route.peer_fleet_uid != ctx.fleet_uid
            or route.recipient_alias != route.manager_alias):
        raise ReceiptConflict("report route differs from the frozen caller or selected fleet")
    semantic = semantic_digest(dict(assignment_id=assignment_id, report=report.to_body()))
    operation = "assignment." + verb
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        frozen = store.load()
        if route is not None and frozen is not None and frozen.intent.recipient_uid != route.manager_uid:
            raise ReceiptConflict("report route differs from the frozen recipient")
        # Do not consult today's manager for an already-recorded operation.
        # Its immutable fact projections prove the originally addressed UID.
        previous = _existing(store, ctx, operation, semantic,
                             frozen.intent.recipient_uid if frozen else None,
                             fact_count=2, notification=True, route=route)
        with _reader(ctx) as conn:
            first = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = _current_assignee(ctx, show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid))
                manager = _worker(ctx, ctx.context.fleet.manager)
                if previous and previous.intent.recipient_uid != manager.uid:
                    raise ReceiptConflict("report manager differs from the frozen recipient")
                if route is not None and (route.manager_uid != manager.uid
                                          or route.manager_alias != manager.alias):
                    raise ReceiptConflict("report route differs from the selected manager")
                _identities(ctx, conn, (ctx.caller, manager))
                message_id = previous.intent.message_id if previous else mint_msg_id()
                event_ids = (tuple(fact.event_id for fact in previous.intent.stages[0].facts)
                             if previous else (mint_event_id(), mint_event_id()))
                raws = encode_report_facts(report, fleet=ctx.context.fleet.name, sender=ctx.caller.alias,
                        recipient=manager.alias, msg_id=message_id, event_ids=event_ids,
                        occurred_at=datetime.now(timezone.utc).isoformat(),
                        link=ReportLink(task.task_id, assignment_id, transition))
                receipt = _prepare(store, ctx, operation, semantic, raws, task.task_id, assignment_id,
                                   manager.uid, (manager,), message_id=message_id,
                                   notification=True, route=route)
                # Only the recording stage is attempted. The notification
                # stays prepared for the later messaging owner, never sent here.
                return _commit(store, ctx, conn, receipt, raws, check)


def progress(ctx: TaskOperationContext, request_id: str, assignment_id: str,
             report: ReportPayload, *, route: MessageRouteBinding | None = None) -> TaskOperationResult:
    return _assignment_report(ctx, request_id, assignment_id, report, verb="progress",
                              status="progress", transition="progress", text_field="summary", route=route)


def block(ctx: TaskOperationContext, request_id: str, assignment_id: str,
          report: ReportPayload, *, route: MessageRouteBinding | None = None) -> TaskOperationResult:
    return _assignment_report(ctx, request_id, assignment_id, report, verb="block",
                              status="blocked", transition="blocked_waiting", text_field="reason", route=route)


def return_assignment(ctx: TaskOperationContext, request_id: str, assignment_id: str,
                      report: ReportPayload, *, route: MessageRouteBinding | None = None) -> TaskOperationResult:
    return _assignment_report(ctx, request_id, assignment_id, report, verb="return",
                              status="blocked", transition="returned_blocked", text_field="reason", route=route)


def complete(ctx: TaskOperationContext, request_id: str, assignment_id: str,
             report: ReportPayload, *, route: MessageRouteBinding | None = None) -> TaskOperationResult:
    return _assignment_report(ctx, request_id, assignment_id, report, verb="complete",
                              status="completed", transition="completed", text_field="summary", route=route)


def fail(ctx: TaskOperationContext, request_id: str, assignment_id: str,
         report: ReportPayload, *, route: MessageRouteBinding | None = None) -> TaskOperationResult:
    return _assignment_report(ctx, request_id, assignment_id, report, verb="fail",
                              status="failed", transition="failed", text_field="reason", route=route)


def withdraw(ctx: TaskOperationContext, request_id: str, task_id: str, *, reason: str,
             by: str | None = None) -> TaskOperationResult:
    """Cancel open work, including its current assignment; queued work is valid."""
    _own_fleet(ctx)
    _reason(reason)
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(task_id=task_id, reason=reason, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.withdraw", semantic)
        with _reader(ctx) as conn:
            first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                if not task.open:
                    raise TaskConflictError("terminal task cannot be withdrawn")
                aid = task.current_assignment.assignment_id if task.current_assignment else None
                if previous and previous.intent.assignment_id != aid:
                    raise TaskConflictError("withdrawal's original assignment changed")
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, provenance))
                # The reducer's work-level cancellation also closes every
                # assignment preceding it; an assignment-only cancel queues work.
                raw = _raw(ctx, request_id, "task", dict(work_item_id=task_id,
                           event="cancelled", actor=provenance.alias, reason=reason), previous)
                receipt = _prepare(store, ctx, "task.withdraw", semantic, (raw,), task_id, aid,
                                   actors=(provenance,))
                return _commit(store, ctx, conn, receipt, (raw,), check)


def reassign(ctx: TaskOperationContext, request_id: str, task_id: str, *, bot_id: str,
             reason: str, expected_by: str | None = None,
             by: str | None = None) -> TaskOperationResult:
    """Close the exact current assignment and admit its successor atomically."""
    _own_fleet(ctx)
    _reason(reason)
    worker = _worker(ctx, bot_id)
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(task_id=task_id, bot_id=bot_id, reason=reason,
                                    expected_by=expected_by, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.reassign", semantic, worker.uid, fact_count=2)
        with _reader(ctx) as conn:
            first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                if not task.open or task.current_assignment is None:
                    raise TaskConflictError("reassignment requires an open task with a current assignment")
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, worker, provenance))
                _scope_links(ctx, conn, task.project_key, task.workstream_id, task.repo)
                successor = previous.intent.assignment_id if previous else mint_assignment_id()
                raws = (
                    _raw(ctx, request_id, "task", dict(work_item_id=task_id,
                         assignment_id=task.current_assignment.assignment_id, event="reassigned",
                         successor_id=successor, actor=provenance.alias, reason=reason), previous),
                    _raw(ctx, request_id, "assignment", dict(assignment_id=successor, work_item_id=task_id,
                         assignee=worker.alias, assigned_by=provenance.alias, expected_by=expected_by),
                         previous, fact_index=1),
                )
                # The immutable closure projection freezes both predecessor
                # and successor. A retry against a replacement cannot prepare
                # a different closure under this UUID, even when no fact landed.
                receipt = _prepare(store, ctx, "task.reassign", semantic, raws,
                                   task_id, successor, worker.uid, (worker, provenance))
                return _commit(store, ctx, conn, receipt, raws, check)
