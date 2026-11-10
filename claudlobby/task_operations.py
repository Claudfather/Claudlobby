"""Durable task admission, routing, acceptance and withdrawal, no delivery.

The public boundary supplies resolved Context and existing frozen identities,
and owns runtime/release admission. Request lock precedes the per-task lock;
only emit_batch(require_commit=True) writes Plane facts. Receipts are proof
coordinates, never a second work-state store or a conditional replay queue.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from types import MappingProxyType
from typing import Literal, Mapping, TYPE_CHECKING

from .plane.db import connect_ro, db_file
from .message_payload import MessageBody
from .plane.ids import ID_PATTERNS, mint_assignment_id, mint_event_id, mint_msg_id, mint_work_item_id
from .plane.queries import checkin_event_scope_sql, fleet_alias_range, fleet_range_params
from .plane.schema_state import PendingMigrationError, require_current_schema
from .report_payload import ReportLink, ReportPayload, encode_report_facts
from .request_facts import expected_fact, reconcile_facts
from .request_receipts import (MessageRouteBinding, ReceiptConflict, RequestIntent,
                               StagePlan, locked_request, same_native_route, semantic_digest)
from .task_queries import TaskNotFoundError, TaskQueryError, show_assignment, show_task
from .task_state import TASK_EMITTER, Task, read_tasks, workstream_task_ids
from .workstream_operations import task_workstream_id

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


class WorkstreamClosedError(TaskConflictError):
    """Admit refused to join a workstream that has ended; nothing was recorded."""


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


@contextmanager
def _read_snapshot(conn, admit_read=None):
    """Optional trusted source admission covers the same snapshot as its read.

    End it before emit_batch: recording and its fresh proof use different
    snapshots. Ordinary local operations keep their existing read behavior.
    """
    if admit_read is None:
        yield
        return
    conn.execute("BEGIN")
    try:
        admit_read(conn)
        yield
    finally:
        conn.rollback()


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


def _reason(reason, *, field="reason"):
    if not isinstance(reason, str) or not reason.strip():
        raise TaskQueryError(f"a nonempty {field} is required")


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


_ADMIT_ELSEWHERE = "admit the task without --workstream, or into an open workstream"
_MOVE_ELSEWHERE = "move the task into an open workstream"


def _require_open_workstream(ctx, conn, workstream_id, *, elsewhere=_ADMIT_ELSEWHERE):
    """Refuse a join to a workstream that is archived or closed.

    Closed is a recorded close (``workstream close``) or a derived one: the
    workstream holds a task, or a move took one out of it, and every task it
    holds has ended (the task reducer's first terminal fact). Its tasks are
    the ones after moves (``task_state.workstream_task_ids``). Admit and a
    move run this before their request and again as their batch's
    precondition, under the plane's write lock, so a close that lands in
    between refuses the join instead of racing it.
    """
    ended = {row[0] for row in conn.execute(
        "SELECT DISTINCT event FROM events WHERE kind='workstream' AND workstream_id=?"
        " AND fleet_uid=? AND event IN ('closed', 'archived')", (workstream_id, ctx.fleet_uid))}
    if ended:
        raise WorkstreamClosedError(
            f"workstream is {'archived' if 'archived' in ended else 'closed'}; {elsewhere}")
    task_ids, moved_out = workstream_task_ids(conn, fleet_uid=ctx.fleet_uid,
                                              workstream_id=workstream_id)
    if (task_ids or moved_out) and all(task.terminal_event is not None for task in (
            read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=task_ids).tasks if task_ids else ())):
        why = ("every task in it has ended" if not moved_out
               else "every task in it has ended or moved out" if task_ids
               else "a move took its last task out of it")
        raise WorkstreamClosedError(f"workstream is closed: {why}; {elsewhere}")


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
    # Current human decisions retain the selected fleet UID and actual human
    # subject; historical null-fleet decisions require a fleet bot alias.
    sql = ("SELECT 1 FROM events e WHERE e.kind='system' AND e.event='checkin_decision'"
           " AND e.source_ref=? AND " + checkin_event_scope_sql("e")
           + " AND (" + fleet_alias_range("e.subject_alias")
           + " OR (e.fleet_uid IS NOT NULL AND e.subject_alias LIKE 'human:%')) LIMIT 1")
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
                or len(intent.stages[0].facts) != fact_count
                or not same_native_route(intent.route, route)
                or notification and (intent.message_id is None or intent.recipient_uid is None
                                     or intent.stages[1].facts)):
            raise ReceiptConflict("request UUID already has different semantics or identities")
    return receipt


OPEN_ENDED = "none"  # explicit open-ended routing; omission takes the fleet default
_DEFAULT_DISPATCH_DEADLINE_S = 86_400  # mirrors composer.DEFAULT_DISPATCH_DEADLINE_S


def _deadline(ctx, expected_by, previous):
    """Return (payload deadline, receipt-frozen default) for assign/reassign.

    The semantic digest keeps the caller's intent (omitted, none or explicit);
    an omitted deadline is resolved once and retries reuse the frozen value.
    """
    if expected_by == OPEN_ENDED:
        return None, None
    if expected_by is not None:
        return expected_by, None
    if previous is not None:  # never today's clock or config
        return previous.intent.expected_by, previous.intent.expected_by
    # Fleet-owned routing uses the implicit manager's resolved observability,
    # the same composed default whether a human or a local bot submits it.
    fleet = ctx.context.fleet
    seconds = fleet.bots[fleet.manager].observability.dispatch_deadline
    seconds = _DEFAULT_DISPATCH_DEADLINE_S if seconds is None else seconds
    if type(seconds) is not int or seconds < 0:
        raise TaskQueryError("observability.dispatch_deadline must be a nonnegative integer of seconds")
    if seconds == 0:
        return None, None
    frozen = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    return frozen, frozen


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
             *, message_id=None, notification=False, route=None, expected_by=None):
    from .plane.emit_api import CONTENT_FIELDS, load_capture_config, validate_item
    modes = load_capture_config(ctx.root) if any(CONTENT_FIELDS.get(r["event_type"]) for r in raws) else {}
    parties = {actor.alias: actor.uid for actor in (ctx.caller, *actors)}
    facts = tuple(expected_fact(validate_item(raw, modes)[0], host_uid=ctx.host_uid,
                               fleet_uid=ctx.fleet_uid, parties=parties) for raw in raws)
    stages = (StagePlan("recording", facts),) + ((StagePlan("notification"),) if notification else ())
    return store.prepare(RequestIntent(operation, 1, ctx.host_uid, ctx.fleet_uid, ctx.caller.uid,
                         recipient, semantic, stages,
                         task_id=task_id, assignment_id=assignment_id, message_id=message_id,
                         route=route, expected_by=expected_by))


def _result(ctx, conn, receipt, replayed):
    notification = "not_requested"
    if len(receipt.stages) == 2:
        state = receipt.stages[1].status
        notification = "pending" if state == "prepared" else state
    return TaskOperationResult(receipt.request_id, receipt.intent.task_id, receipt.intent.assignment_id,
                               show_task(conn, receipt.intent.task_id, fleet_uid=ctx.fleet_uid), replayed,
                               notification=notification, message_id=receipt.intent.message_id,
                               recipient_uid=receipt.intent.recipient_uid)


def _commit(store, ctx, conn, receipt, raws, check_lock, *, admit_read=None, precondition=None):
    from .plane.emit_api import emit_batch
    check_lock()
    store.begin_attempt()
    store.stage(0)  # durable unknown before SQLite; crashes never authorize blind replay
    try:
        # Only a join and a move pass a precondition; every other batch keeps its call.
        emit_batch(ctx.root, list(raws), require_commit=True,
                   **({} if precondition is None else {"precondition": precondition}))
    except WorkstreamClosedError:
        # Raised only by the precondition, after BEGIN IMMEDIATE and before any
        # row: the rolled-back batch is definitely absent. A failed outcome
        # write leaves it unknown, which the next attempt reconciles.
        try:
            store.outcome(0, "unrecorded")
        except OSError:
            pass
        raise
    except (sqlite3.Error, OSError) as exc:
        # Do not turn an exception into an unchanged result, even if it looks
        # like a connection failure. The next invocation reconciles exact IDs.
        raise TaskRecordingError(receipt.request_id, str(exc)) from exc
    with _read_snapshot(conn, admit_read):
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
    """Admit fleet-owned intake, deliberately creating no assignment or message.

    Every new task belongs to a workstream. Without ``workstream_id`` the
    task's own batch also records a one-to-one workstream (its ID derives
    from the task's, its title is the task's, the fleet's manager owns it and
    the caller opened it), so both land or neither does. With one, the task
    joins it, and admit refuses a workstream that is archived or closed.
    """
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(title=title, body=body, repo=repo, project_key=project_key,
                                    workstream_id=workstream_id, by=by_alias))
    own = workstream_id is None
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.admit", semantic, fact_count=2 if own else 1)
        tid = previous.intent.task_id if previous else mint_work_item_id()
        with _locked_task(store, tid) as check, _reader(ctx) as conn:
            if _replayed(store, previous, conn):
                return _result(ctx, conn, previous, True)
            provenance = _provenance(ctx, conn, by)
            actors = (provenance,)
            raws = ()
            precondition = None
            if own:
                manager = ctx.bots.get(ctx.context.fleet.manager)
                if manager is None:
                    raise TaskQueryError("the fleet's manager is not a declared bot of the selected fleet")
                actors += (manager,)
                workstream_id = task_workstream_id(tid)
                raws += (_raw(ctx, request_id, "workstream", dict(
                    workstream_id=workstream_id, title=title, owner=manager.alias,
                    opened_by=ctx.caller.alias, project_key=project_key), previous),)
            _identities(ctx, conn, (ctx.caller, *actors))
            _scope_links(ctx, conn, project_key, None if own else workstream_id, repo)
            if not own:
                _require_open_workstream(ctx, conn, workstream_id)
                joined = workstream_id
                precondition = lambda write: _require_open_workstream(ctx, write, joined)  # noqa: E731
            raws += (_raw(ctx, request_id, "work_item", dict(work_item_id=tid, title=title, body=body,
                     created_by=provenance.alias, repo=repo, project_key=project_key,
                     workstream_id=workstream_id), previous, fact_index=len(raws)),)
            receipt = _prepare(store, ctx, "task.admit", semantic, raws, tid, actors=actors)
            return _commit(store, ctx, conn, receipt, raws, check, precondition=precondition)


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
        due, frozen = _deadline(ctx, expected_by, previous)
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
                           assignee=worker.alias, assigned_by=provenance.alias, expected_by=due), previous)
                raws = (raw,)
                if checkin_id is not None:
                    join = _raw(ctx, request_id, "system", {
                        "event": "checkin_dispatch", "subject_kind": "actor", "subject": ctx.caller.alias,
                        "data": {"checkin_id": checkin_id, "assignment_id": aid,
                                 "work_item_id": task_id, "task_id": None},
                    }, previous, fact_index=1)
                    raws = (raw, join)
                receipt = _prepare(store, ctx, "task.assign", semantic, raws, task_id, aid, worker.uid,
                                   (worker, provenance), expected_by=frozen)
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
    view.task.require_resolved()
    if (view.assignment.assignee_uid != ctx.caller.uid
            or not any(_worker(ctx, bot) == ctx.caller for bot in ctx.bots if bot in ctx.context.fleet.bots)):
        raise TaskConflictError("only the declared current assignee can act")
    return _current_assignment(view)


def _current_assignment(view):
    """The viewed assignment's task, refusing unless it is still the task's
    open current assignment."""
    task = view.task.require_resolved()
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
                                   notification=True,
                                   route=previous.intent.route if previous else route)
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
                raw = _raw(ctx, request_id, "task",
                           _withdrawal(task_id, actor=provenance.alias, reason=reason), previous)
                receipt = _prepare(store, ctx, "task.withdraw", semantic, (raw,), task_id, aid,
                                   actors=(provenance,))
                return _commit(store, ctx, conn, receipt, (raw,), check)


def _withdrawal(task_id, *, actor, reason):
    """The withdraw row's payload, a task-family row under ``TASK_EMITTER``.

    It is the reducer's work-level cancellation: it names no assignment, so it
    also closes every assignment before it, where an assignment-only cancel
    queues the work again. ``task withdraw`` records one, and a workstream's
    close records one for each queued task it withdraws
    (``workstream_operations.py``).
    """
    return dict(work_item_id=task_id, event="cancelled", actor=actor, reason=reason)


def move(ctx: TaskOperationContext, request_id: str, task_id: str, *, workstream_id: str,
         reason: str, by: str | None = None) -> TaskOperationResult:
    """Move open fleet work into another open workstream of its fleet (#1053).

    One system row, ``task_moved`` (``_moved``), names the task, the workstream
    it left, the one it joined, who moved it and why. Every read takes a
    task's workstream from its newest move (``task_state.read_moves``). Only
    the fleet's manager or a local person moves a task, and only an open one.
    The target must be in the task's fleet and project, and open: no recorded
    close, no derived one, not archived. The batch's precondition checks that
    again under the plane's write lock, so a close that lands in between
    refuses the move. A workstream whose last open task moves out reads
    closed (the registry derives it).
    """
    from .plane.registries import cap_for

    if not _manager_or_local_person(ctx):
        raise TaskConflictError("moving a task requires the current manager or a local person")
    _reason(reason)
    if len(reason.encode("utf-8")) > cap_for("task", "reason"):
        raise TaskQueryError(f"a reason of at most {cap_for('task', 'reason')} bytes is required")
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(task_id=task_id, workstream_id=workstream_id, reason=reason,
                                    by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.move", semantic)
        with _reader(ctx) as conn:
            first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                if not task.open:
                    raise TaskConflictError("terminal task cannot be moved")
                if task.workstream_id == workstream_id:
                    raise TaskConflictError("task is already in that workstream")
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, provenance))
                _scope_links(ctx, conn, task.project_key, workstream_id, task.repo)
                _require_open_workstream(ctx, conn, workstream_id, elsewhere=_MOVE_ELSEWHERE)
                raw = _raw(ctx, request_id, "system",
                           _moved(ctx, task, workstream_id, actor=provenance.alias, reason=reason),
                           previous)
                receipt = _prepare(store, ctx, "task.move", semantic, (raw,), task_id,
                                   actors=(provenance,))
                return _commit(store, ctx, conn, receipt, (raw,), check,
                               precondition=lambda write: _require_open_workstream(
                                   ctx, write, workstream_id, elsewhere=_MOVE_ELSEWHERE))


def _moved(ctx, task, joined, *, actor, reason):
    """The move row's payload: a system row whose subject is the task's fleet.

    A system row has no task or actor column, so the task, both workstreams,
    the mover and the reason ride its ``data``. Its subject is the fleet, the
    one identity every task verb already holds, so the read seeks the fleet's
    own subject rows (``task_state.read_moves``).
    """
    return {"event": "task_moved", "subject_kind": "fleet", "subject_uid": ctx.fleet_uid,
            "subject_alias": ctx.context.fleet.name,
            "data": {"task_id": task.task_id, "from_workstream_id": task.workstream_id,
                     "to_workstream_id": joined, "actor": actor, "reason": reason}}


def escalate(ctx: TaskOperationContext, request_id: str, task_id: str, *, question: str,
             by: str | None = None) -> TaskOperationResult:
    """Ask for human guidance on open fleet work without changing its state."""
    _own_fleet(ctx)
    _reason(question, field="question")
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(task_id=task_id, question=question, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.escalate", semantic)
        with _reader(ctx) as conn:
            first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                if not task.open:
                    raise TaskConflictError("terminal task cannot be escalated")
                aid = task.current_assignment.assignment_id if task.current_assignment else None
                if previous and previous.intent.assignment_id != aid:
                    raise TaskConflictError("escalation's original assignment changed")
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, provenance))
                raw = _raw(ctx, request_id, "task", dict(work_item_id=task_id,
                           assignment_id=aid, event="escalated", actor=provenance.alias,
                           by=provenance.alias, question=question), previous)
                receipt = _prepare(store, ctx, "task.escalate", semantic, (raw,), task_id, aid,
                                   actors=(provenance,))
                return _commit(store, ctx, conn, receipt, (raw,), check)


def _manager_or_local_person(ctx):
    """The fleet's current manager, calling from its own fleet, or a person at
    this host: the workstream door's predicate (workstream_operations.py),
    checked in the door rather than left to the composed grant alone."""
    manager = ctx.bots.get(ctx.context.fleet.manager)
    return ((manager is not None and ctx.caller == manager and ctx.caller_fleet_uid == ctx.fleet_uid)
            or ctx.caller.alias.startswith("human:") and ctx.caller_fleet_uid is None)


def reschedule(ctx: TaskOperationContext, request_id: str, assignment_id: str, *, expected_by: str,
               reason: str, by: str | None = None) -> TaskOperationResult:
    """Move an open assignment's deadline (#2469): one `deadline_changed` fact on
    the SAME assignment, with the new deadline (`none` = open-ended), who moved
    it and why. Every reader takes the newest such fact as the deadline; the
    assignment row keeps the deadline its dispatch promised. Records only."""
    if not _manager_or_local_person(ctx):
        raise TaskConflictError("rescheduling an assignment requires the current manager or a local person")
    _reason(reason)
    by_alias = _provenance_alias(ctx, by)
    semantic = semantic_digest(dict(assignment_id=assignment_id, expected_by=expected_by,
                                    reason=reason, by=by_alias))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "assignment.reschedule", semantic)
        with _reader(ctx) as conn:
            first = show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task.task_id) as check:
                if _replayed(store, previous, conn):
                    return _result(ctx, conn, previous, True)
                task = _current_assignment(show_assignment(conn, assignment_id, fleet_uid=ctx.fleet_uid))
                provenance = _provenance(ctx, conn, by)
                _identities(ctx, conn, (ctx.caller, provenance))
                raw = _raw(ctx, request_id, "task", dict(work_item_id=task.task_id,
                           assignment_id=assignment_id, event="deadline_changed",
                           actor=provenance.alias, by=provenance.alias, reason=reason,
                           deadline=None if expected_by == OPEN_ENDED else expected_by), previous)
                receipt = _prepare(store, ctx, "assignment.reschedule", semantic, (raw,), task.task_id,
                                   assignment_id, actors=(provenance,))
                return _commit(store, ctx, conn, receipt, (raw,), check)


def nudge_body(task_id: str, assignment_id: str | None, by: str, reason: str) -> str:
    """Keep authored text inside one JSON value in the recorded and native ask."""
    return json.dumps({"kind": "task_nudge", "task_id": task_id,
                       "assignment_id": assignment_id, "by": by, "reason": reason},
                      ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# Omission preserves CLI semantics; explicit None requires still-queued work.
UNSPECIFIED_ASSIGNMENT = object()


def nudge_semantic_digest(task_id: str, *, reason: str, by: str,
                          expected_assignment_id=UNSPECIFIED_ASSIGNMENT) -> str:
    """Pure canonical intent digest; omission and explicit null stay distinct."""
    semantic_fields = dict(task_id=task_id, reason=reason, by=by)
    if expected_assignment_id is not UNSPECIFIED_ASSIGNMENT:
        if expected_assignment_id is not None and (not isinstance(expected_assignment_id, str)
                or not re.fullmatch(ID_PATTERNS["assignment"], expected_assignment_id)):
            raise TaskQueryError("expected assignment must be a canonical assignment ID or null")
        semantic_fields["expected_assignment_id"] = expected_assignment_id
    return semantic_digest(semantic_fields)


def nudge(ctx: TaskOperationContext, request_id: str, task_id: str, *, reason: str,
          route: MessageRouteBinding, by: str | None = None,
          expected_assignment_id=UNSPECIFIED_ASSIGNMENT, admit_read=None) -> TaskOperationResult:
    """Commit one work-level nudge and manager ask before native notification."""
    _own_fleet(ctx)
    _reason(reason)
    by_alias = _provenance_alias(ctx, by)
    manager = _worker(ctx, ctx.context.fleet.manager)
    if (not isinstance(route, MessageRouteBinding) or route.caller_alias != ctx.caller.alias
            or route.caller_fleet_uid != ctx.caller_fleet_uid
            or route.peer_fleet_uid != ctx.fleet_uid
            or route.recipient_alias != manager.alias or route.manager_alias != manager.alias
            or route.manager_uid != manager.uid):
        raise ReceiptConflict("nudge route differs from the frozen caller or fleet manager")
    semantic = nudge_semantic_digest(task_id, reason=reason, by=by_alias,
                                     expected_assignment_id=expected_assignment_id)
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.nudge", semantic, manager.uid,
                             fact_count=2, notification=True, route=route)
        with _reader(ctx) as conn:
            with _read_snapshot(conn, admit_read):
                first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                with _read_snapshot(conn, admit_read):
                    if _replayed(store, previous, conn):
                        return _result(ctx, conn, previous, True)
                    task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                    if not task.open:
                        raise TaskConflictError("terminal task cannot be nudged")
                    assignment_id = (task.current_assignment.assignment_id
                                     if task.current_assignment else None)
                    if expected_assignment_id is not UNSPECIFIED_ASSIGNMENT and expected_assignment_id != assignment_id:
                        raise TaskConflictError("task assignment changed since the nudge was selected")
                    if previous and previous.intent.assignment_id != assignment_id:
                        raise TaskConflictError("nudge's original assignment changed")
                    provenance = _provenance(ctx, conn, by)
                    _identities(ctx, conn, (ctx.caller, provenance, manager))
                    message_id = previous.intent.message_id if previous else mint_msg_id()
                    task_raw = _raw(ctx, request_id, "task", dict(
                        work_item_id=task_id, assignment_id=assignment_id, event="nudged",
                        actor=ctx.caller.alias, by=provenance.alias, reason=reason), previous)
                    body = nudge_body(task_id, assignment_id, provenance.alias, reason)
                    ask_raw = _raw(ctx, request_id, "communication", dict(
                        msg_id=message_id, sender=ctx.caller.alias, recipient=manager.alias,
                        recipient_raw=ctx.context.fleet.manager, message_class="task_request",
                        command_type="query", work_item_id=task_id, body=body,
                        **({"assignment_id": assignment_id} if assignment_id else {})),
                        previous, fact_index=1)
                    raws = (task_raw, ask_raw)
                    receipt = _prepare(store, ctx, "task.nudge", semantic, raws, task_id,
                                       assignment_id, manager.uid, (provenance, manager),
                                       message_id=message_id, notification=True,
                                       route=previous.intent.route if previous else route)
                return _commit(store, ctx, conn, receipt, raws, check, admit_read=admit_read)


def feedback_semantic_digest(task_id: str, *, body: MessageBody,
                             expected_assignment_id: str | None) -> str:
    """A task comment pins authored bytes and an explicit assignment or null."""
    if not isinstance(task_id, str) or not re.fullmatch(ID_PATTERNS["work_item"], task_id):
        raise TaskQueryError("feedback requires a canonical task ID")
    if not isinstance(body, MessageBody):
        raise TaskQueryError("feedback requires a validated message body")
    if expected_assignment_id is not None and (not isinstance(expected_assignment_id, str)
            or not re.fullmatch(ID_PATTERNS["assignment"], expected_assignment_id)):
        raise TaskQueryError("expected assignment must be a canonical assignment ID or null")
    return semantic_digest(dict(task_id=task_id, body=body.text,
                                expected_assignment_id=expected_assignment_id, kind="task_feedback"))


def feedback(ctx: TaskOperationContext, request_id: str, task_id: str, *, body: MessageBody,
             expected_assignment_id: str | None, route: MessageRouteBinding,
             admit_read=None) -> TaskOperationResult:
    """Record one human comment on resolved work, including terminal work.

    Existing requests are reconciliation-only. They cannot repeat recording or
    become a notification retry, even when no original fact can be proved.
    """
    if ctx.caller_fleet_uid is not None or not re.fullmatch(r"human:[^\s:/]+", ctx.caller.alias):
        raise TaskConflictError("task feedback requires an existing local human actor")
    manager = _worker(ctx, ctx.context.fleet.manager)
    if (not isinstance(route, MessageRouteBinding) or route.caller_alias != ctx.caller.alias
            or route.caller_fleet_uid is not None or route.peer_fleet_uid != ctx.fleet_uid
            or route.recipient_alias != manager.alias or route.manager_alias != manager.alias
            or route.manager_uid != manager.uid):
        raise ReceiptConflict("feedback route differs from the frozen human or fleet manager")
    semantic = feedback_semantic_digest(task_id, body=body,
                                        expected_assignment_id=expected_assignment_id)
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        previous = _existing(store, ctx, "task.feedback", semantic, manager.uid,
                             notification=True, route=route)
        with _reader(ctx) as conn:
            with _read_snapshot(conn, admit_read):
                first = show_task(conn, task_id, fleet_uid=ctx.fleet_uid)
            with _locked_task(store, first.task_id) as check:
                with _read_snapshot(conn, admit_read):
                    if _replayed(store, previous, conn):
                        return _result(ctx, conn, previous, True)
                    if previous is not None:
                        raise TaskRecordingError(request_id,
                            "retained feedback is inspection-only; recording cannot be repeated",
                            task_id=previous.intent.task_id, assignment_id=previous.intent.assignment_id,
                            message_id=previous.intent.message_id, recipient_uid=manager.uid,
                            request_persisted=True)
                    task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                    assignment_id = task.current_assignment.assignment_id if task.current_assignment else None
                    if expected_assignment_id != assignment_id:
                        raise TaskConflictError("task assignment changed since feedback was selected")
                    _identities(ctx, conn, (ctx.caller, manager))
                    message_id = mint_msg_id()
                    raw = _raw(ctx, request_id, "communication", dict(
                        msg_id=message_id, sender=ctx.caller.alias, recipient=manager.alias,
                        recipient_raw=ctx.context.fleet.manager, message_class="chat",
                        work_item_id=task_id, assignment_id=assignment_id, body=body.text), None)
                    receipt = _prepare(store, ctx, "task.feedback", semantic, (raw,), task_id,
                        assignment_id, manager.uid, (manager,), message_id=message_id,
                        notification=True, route=route)
                return _commit(store, ctx, conn, receipt, (raw,), check, admit_read=admit_read)


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
        due, frozen = _deadline(ctx, expected_by, previous)
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
                         assignee=worker.alias, assigned_by=provenance.alias, expected_by=due),
                         previous, fact_index=1),
                )
                # The immutable closure projection freezes both predecessor
                # and successor. A retry against a replacement cannot prepare
                # a different closure under this UUID, even when no fact landed.
                receipt = _prepare(store, ctx, "task.reassign", semantic, raws,
                                   task_id, successor, worker.uid, (worker, provenance), expected_by=frozen)
                return _commit(store, ctx, conn, receipt, raws, check)
