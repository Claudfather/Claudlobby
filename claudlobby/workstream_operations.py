"""One fleet-scoped workstream writer over the existing Plane registry reducer."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from uuid import UUID

from .plane.db import connect_ro, db_file
from .plane.ids import derive_uid
from .plane.schema_state import require_current_schema
from .request_facts import expected_fact, reconcile_facts
from .request_receipts import (ReceiptError, RequestIntent, StagePlan, locked_request,
                               semantic_digest)


#: A task's own workstream, recorded by ``task admit`` in the task's own batch:
#: ``ws_`` and the task ID's 32 hex digits, so a replay names the same one.
#: ``workstream open`` mints ``ws-<slug>`` and refuses this form for ``--id``,
#: and the active cap does not count it: it is one per task, not one per plan.
TASK_WORKSTREAM_ID = re.compile(r"ws_[0-9a-f]{32}")


def task_workstream_id(task_id: str) -> str:
    """The ID of the workstream ``task admit`` opens for ``task_id``."""
    if not re.fullmatch(r"wi_[0-9a-f]{32}", task_id):
        raise ValueError("a task's own workstream needs a canonical task ID")
    return "ws_" + task_id[3:]


def _active(work) -> int:
    """Active workstreams under the cap: those opened with ``workstream open``."""
    return sum(row.get("status") == "active" and not TASK_WORKSTREAM_ID.fullmatch(wid)
               for wid, row in work.items())


class WorkstreamError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class WorkstreamTasksChanged(WorkstreamError):
    """The close's precondition refused: under the plane's write lock, the
    workstream's open tasks are no longer the queued ones its read planned to
    withdraw. Raised before any row in the batch is written."""

    def __init__(self, message: str):
        super().__init__("conflict", message)


@dataclass(frozen=True)
class FiledTask:
    """A task the workstream backfill filed into its own workstream."""
    task_id: str
    #: The workstream it left: None when it had none, else the closed or
    #: archived one it was in.
    from_workstream_id: str | None
    workstream_id: str


@dataclass(frozen=True)
class WorkstreamResult:
    request_id: str
    workstream_ids: tuple[str, ...]
    recording: str
    request_persisted: bool
    replayed: bool
    notification: str = "not_requested"
    #: The queued tasks a close withdrew in its own batch, in task-ID order.
    withdrawn_task_ids: tuple[str, ...] = ()
    #: The tasks a backfill filed in its own batch, in task-ID order.
    filed_tasks: tuple[FiledTask, ...] = ()


def _configuration(ctx):
    config = ctx.context.fleet.workstreams
    maximum, days = config.max_active, config.lease_days
    if any(type(value) is not int or value < 1 for value in (maximum, days)):
        raise WorkstreamError("conflict", "workstream cap or lease must be a positive integer")
    return maximum, days


def _reader(ctx, days: int, *, or_empty: bool):
    from .paths import load_lib_module

    pr = load_lib_module(ctx.context.paths.lib, "plane-readers.py")
    if pr is None or not hasattr(pr, "workstream_registry"):
        raise WorkstreamError("unavailable", "installed Plane workstream reader is unavailable")
    try:
        with closing(connect_ro(db_file(ctx.root))) as conn:
            require_current_schema(conn)
            row = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias=?",
                               (ctx.context.fleet.name,)).fetchone()
            if row is None or row[0] != ctx.fleet_uid:
                raise WorkstreamError("conflict", "Plane fleet differs from the selected workstream fleet")
            return pr.workstream_registry(conn, ctx.context.fleet.name, lease_days=days,
                                          or_empty=or_empty)
    except (OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "Plane workstream registry is unavailable") from exc


def _proof(ctx, facts):
    try:
        with closing(connect_ro(db_file(ctx.root))) as conn:
            require_current_schema(conn)
            return reconcile_facts(conn, facts)
    except (OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "workstream recording proof is unavailable") from exc


def _slug(title):
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not value:
        raise WorkstreamError("invalid_argument", "title has no usable workstream slug")
    if len(value) > 128:
        raise WorkstreamError("invalid_argument", "title slug is too long; supply a short --id")
    return "ws-" + value


def _ids_from_facts(ctx, facts):
    """The committed workstream IDs, the tasks a close withdrew and the tasks
    a backfill filed, of a replayed request."""
    from .plane.ingest import CONSTRUCT_TABLES
    from .task_state import TASK_MOVED

    ids, withdrawn, filed = [], [], []
    with closing(connect_ro(db_file(ctx.root))) as conn:
        require_current_schema(conn)
        for fact in facts:
            table = CONSTRUCT_TABLES.get(fact.family, "events")
            if table == "events" and fact.family == "system":
                row = conn.execute("SELECT event, detail, detail_truncated FROM events"
                                   " WHERE event_id=? AND fleet_uid=?",
                                   (fact.event_id, ctx.fleet_uid)).fetchone()
                if row is None or row[0] == TASK_MOVED and (row[2] or not row[1]):
                    raise WorkstreamError("unavailable", "committed workstream identity is unavailable")
                if row[0] == TASK_MOVED:
                    data = json.loads(row[1])
                    filed.append(FiledTask(data["task_id"], data["from_workstream_id"],
                                           data["to_workstream_id"]))
                continue
            column, found = (("work_item_id", withdrawn) if fact.family == "task"
                             else ("workstream_id", ids))
            row = conn.execute(f"SELECT {column} FROM {table} WHERE event_id=? AND fleet_uid=?",
                               (fact.event_id, ctx.fleet_uid)).fetchone()
            if row is None:
                raise WorkstreamError("unavailable", "committed workstream identity is unavailable")
            found.append(row[0])
    return tuple(ids), tuple(withdrawn), tuple(filed)


def open_tasks(conn, *, fleet_uid, workstream_id):
    """The workstream's tasks that have not ended, with the task reducer's
    state: the facts the registry's ``open_task_ids`` and admit's join check
    read. Its tasks are the ones after moves (#1053), so a task moved in is
    the close's to refuse or withdraw, and one moved out is not. A task the
    reducer cannot resolve has no terminal fact, so it is open, with state
    None."""
    from .task_state import TaskStateError, read_tasks, workstream_task_ids

    try:
        ids, _moved_out = workstream_task_ids(conn, fleet_uid=fleet_uid, workstream_id=workstream_id)
        if not ids:
            return ()
        snapshot = read_tasks(conn, fleet_uid=fleet_uid, task_ids=ids)
    except TaskStateError as exc:
        raise WorkstreamError("unavailable", f"the workstream's task state is unreadable: {exc}") from exc
    return tuple(task for task in snapshot.tasks if task.terminal_event is None)


def _open_tasks(ctx, conn, wid):
    return open_tasks(conn, fleet_uid=ctx.fleet_uid, workstream_id=wid)


def _close_read(ctx, wid):
    try:
        with closing(connect_ro(db_file(ctx.root))) as conn:
            require_current_schema(conn)
            return _open_tasks(ctx, conn, wid)
    except (OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "the workstream's tasks are unavailable") from exc


def _not_queued(task):
    """How the close's refusal names a task it cannot withdraw."""
    if task.state is None:
        codes = ", ".join(sorted({issue.code for issue in task.blockers})) or "unresolved"
        return f"{task.task_id} (the task reducer cannot resolve it: {codes})"
    return f"{task.task_id} ({task.state}, a live assignment)"


_NOT_LIVE = "workstream is not live in the selected fleet"
_TERMINAL = "terminal workstream cannot be changed except by prune"


def close_refusal(wid, row, tasks):
    """The refusal a close of *wid* raises, as ``(code, message)``, or None.

    *row* is its registry entry (None when it is not live) and *tasks* its
    open tasks (``open_tasks``). A task with a live assignment is the
    manager's to end first, and so is one the reducer cannot resolve, which
    cannot be told queued from assigned (#1053). ``workstream close
    --dry-run`` reports this same refusal, or the tasks the close withdraws.
    """
    if row is None:
        return "not_found", _NOT_LIVE
    if row.get("status") in ("done", "abandoned"):
        return "conflict", _TERMINAL
    held = [task for task in tasks if task.state != "queued"]
    if held:
        return "conflict", (f"workstream {wid} holds tasks the close cannot withdraw: "
                            f"{'; '.join(_not_queued(task) for task in held)}; "
                            "end each first, then close it")
    return None


def _recheck_close(ctx, wid, withdrawn, write):
    """The close's batch precondition, read under the plane's write lock: the
    workstream's open tasks are still exactly the queued ones the close
    withdraws. A task assigned, ended or joined since the read refuses the
    close, and nothing in its batch is written."""
    now_open = _open_tasks(ctx, write, wid)
    changed = sorted({task.task_id for task in now_open if task.state != "queued"}
                     | ({task.task_id for task in now_open} ^ set(withdrawn)))
    if changed:
        raise WorkstreamTasksChanged(
            f"workstream {wid}'s tasks changed after the close read them: {', '.join(changed)}; "
            "nothing was recorded; read workstream show and close it again with a new --request-id")


def _lock_tasks(stack, store, task_ids, *, act, again):
    """Hold each task's lock, the one every task verb takes, through the
    write that changes it, and return each lock's check.

    A task verb checks its task's state under that lock but outside the
    plane's write lock, and writes with no precondition. Without the lock, a
    close that committed between such a check and its write would leave that
    verb's row, an assignment say, on a withdrawn task, and a backfill would
    file a task a move was taking elsewhere. The precondition catches only the
    other order, a row that lands before the batch. The caller runs the checks
    just before its write (``_check_locks``), as a task verb runs its own.

    The locks are taken in sorted task-ID order, so two callers that lock
    overlapping sets take them in one order and neither can hold a lock the
    other is waiting for. (A task lock never waits: a held one refuses.)
    """
    from .task_operations import _locked_task
    from .task_queries import TaskQueryError

    checks = []
    for task_id in sorted(task_ids):
        try:
            checks.append(stack.enter_context(_locked_task(store, task_id)))
        except TaskQueryError as exc:
            raise WorkstreamError("conflict", f"task {task_id} cannot be {act} now: {exc}; "
                                  f"nothing was recorded; {again}") from exc
    return checks


def _check_locks(checks):
    """Run each held task lock's check just before the write, as
    ``task_operations._commit`` runs its one: a lock file replaced while it
    was held no longer keeps a task verb out, so the write is refused."""
    from .task_queries import TaskQueryError

    try:
        for check in checks:
            check()
    except TaskQueryError as exc:
        raise WorkstreamError("conflict", f"{exc}; nothing was recorded; try again") from exc


def _event(ctx, request_id, ordinal, family, payload, now, *, emitter="workstream-operation"):
    from .plane import PLANE_SCHEMA_VERSION

    return {"event_id": derive_uid("ev", f"workstream:v1:{ctx.host_uid}:{ctx.fleet_uid}:"
                                          f"{request_id}:{ordinal}"),
            "event_type": family, "emitter": emitter,
            "schema_version": PLANE_SCHEMA_VERSION, "fleet": ctx.context.fleet.name,
            "source_ref": f"request:{request_id}", "occurred_at": now,
            "payload": payload}


def _plan(ctx, verb, args, registry, request_id, now, maximum, days, open_tasks=()):
    work = registry["workstreams"]
    archived = set(registry.get("archived", ()))
    if verb == "open":
        title = args["title"]
        explicit = args.get("id")
        if explicit:
            wid = explicit
            if TASK_WORKSTREAM_ID.fullmatch(wid):
                raise WorkstreamError("invalid_argument", "--id ws_<32 hex digits> is the form of a "
                                      "task's own workstream, which task admit opens; choose another ID")
            if wid in work or wid in archived:
                raise WorkstreamError("conflict", "workstream ID already exists or was archived")
        else:
            stem = _slug(title)
            wid, number = stem, 2
            while wid in work or wid in archived:
                wid, number = f"{stem}-{number}", number + 1
                if len(wid) > 131:
                    raise WorkstreamError("conflict", "no available workstream ID within the slug bound")
        if _active(work) >= maximum:
            raise WorkstreamError("conflict", f"active workstreams are at the cap ({maximum})")
        owner = args.get("owner")
        payload = {"workstream_id": wid, "title": title,
                   "opened_by": ctx.caller.alias,
                   **({"owner": ctx.bots[owner].alias} if owner else {}),
                   **({"goal": args["next"]} if args.get("next") else {}),
                   **({"project_key": args["project"]} if args.get("project") else {})}
        return (wid,), [_event(ctx, request_id, 0, "workstream", payload, now)]
    if verb == "prune":
        ids = tuple(sorted(wid for wid, row in work.items()
                           if row.get("status") in ("done", "abandoned")))
        if ids:
            return ids, [_event(ctx, request_id, index, "workstream_event",
                                {"workstream_id": wid, "event": "archived",
                                 "actor": ctx.caller.alias}, now)
                         for index, wid in enumerate(ids)]
        # Freeze an empty sweep, so reusing its UUID cannot prune later work.
        return (), [_event(ctx, request_id, 0, "system",
                           {"event": "workstream_prune_noop", "subject_kind": "actor",
                            "subject": ctx.caller.alias}, now)]
    wid = args["id"]
    row = work.get(wid)
    if row is None:
        raise WorkstreamError("not_found", _NOT_LIVE)
    status = row.get("status")
    if status in ("done", "abandoned"):
        raise WorkstreamError("conflict", _TERMINAL)
    payload = {"workstream_id": wid, "actor": ctx.caller.alias}
    if verb == "progress":
        payload.update(event="progressed")
        if args.get("next"):
            payload["next_step"] = args["next"]
    elif verb == "renew":
        expiry = datetime.fromisoformat(now).astimezone(timezone.utc) + timedelta(days=days)
        payload.update(event="renewed", renewed_until=expiry.isoformat(), note=args["note"])
    elif verb == "block":
        if status == "blocked":
            raise WorkstreamError("conflict", "workstream is already blocked; unblock before declaring a new wait")
        payload.update(event="blocked", waiting_on=args["on"], note=args["note"])
    elif verb == "unblock":
        if status != "blocked":
            raise WorkstreamError("conflict", "only blocked workstreams can be unblocked")
        if _active(work) >= maximum:
            raise WorkstreamError("conflict", f"active workstreams are at the cap ({maximum})")
        payload.update(event="unblocked", note=args["note"])
    elif verb == "close":
        # The queued tasks are withdrawn here, in the close's own batch, so a
        # closed workstream never holds an open task; any other open task
        # refuses the close (``close_refusal``).
        refusal = close_refusal(wid, row, open_tasks)
        if refusal is not None:
            raise WorkstreamError(*refusal)
        from .task_operations import _withdrawal
        from .task_state import TASK_EMITTER

        disposition = args.get("status", "done")
        payload.update(event="closed", disposition=disposition)
        reason = f"withdrawn by the close of workstream {wid} ({disposition})"
        return (wid,), [_event(ctx, request_id, 0, "workstream_event", payload, now)] + [
            _event(ctx, request_id, ordinal, "task",
                   _withdrawal(task_id, actor=ctx.caller.alias, reason=reason, workstream_id=wid), now,
                   emitter=TASK_EMITTER)
            for ordinal, task_id in enumerate(sorted(task.task_id for task in open_tasks), 1)]
    else:
        raise WorkstreamError("invalid_argument", "unknown workstream verb")
    return (wid,), [_event(ctx, request_id, 0, "workstream_event", payload, now)]


#: The ``task_moved`` reason the backfill records for each task it files.
BACKFILL_REASON = "filed into its own workstream by the one-time workstream backfill"


@dataclass(frozen=True)
class BackfillTask:
    """An open task the backfill files. Its own workstream's row takes the
    task's title and project, as ``task admit``'s row does."""
    task_id: str
    title: str
    project_key: str | None
    from_workstream_id: str | None
    workstream_id: str


@dataclass(frozen=True)
class BackfillPlan:
    #: The tasks the backfill files, in task-ID order.
    tasks: tuple[BackfillTask, ...]
    #: Open tasks it leaves where they are, each with why, in task-ID order.
    left: tuple[tuple[str, str], ...]


def _workstream_exists(conn, workstream_id):
    """Whether any row names the workstream. A workstream ID is unique across
    the plane, not only within one fleet (the ``workstreams`` table's UNIQUE
    column), so this read is plane-wide."""
    return conn.execute(
        "SELECT 1 FROM workstreams WHERE workstream_id=? UNION ALL"
        " SELECT 1 FROM events WHERE kind='workstream' AND workstream_id=? LIMIT 1",
        (workstream_id, workstream_id)).fetchone() is not None


def backfill_plan(conn, *, fleet_uid):
    """The open tasks that have no open workstream, which the backfill files (#1053).

    A task admitted before every task had a workstream has none, and one can
    sit in a workstream closed or archived while it was open, which a close
    once allowed. Each such task goes into its own workstream, the one ``task
    admit`` now opens for every new task. A task in an open workstream stays
    there, and an ended task is never filed. A task the reducer cannot
    resolve is left and named, and so is one whose own workstream already
    exists (a move can take it there). Whether a workstream is closed is
    ``task_operations.workstream_closed``, the predicate admit's join and a
    move read. *conn* should hold one read snapshot.
    """
    from .task_operations import workstream_closed
    from .task_state import read_moves, read_tasks

    moves = read_moves(conn, fleet_uid=fleet_uid)
    snapshot = read_tasks(conn, fleet_uid=fleet_uid, moves=moves)
    closed, tasks, left = {}, [], []
    for task in sorted(snapshot.tasks, key=lambda task: task.task_id):
        if task.terminal_event is not None:
            continue
        if task.blockers or not task.open:
            codes = ", ".join(sorted({issue.code for issue in task.blockers})) or "unresolved"
            left.append((task.task_id, f"the task reducer cannot resolve it: {codes}"))
            continue
        here = task.workstream_id
        if here is not None:
            if here not in closed:
                closed[here] = workstream_closed(conn, fleet_uid=fleet_uid, workstream_id=here,
                                                 moves=moves)
            if closed[here] is None:
                continue
        try:
            own = task_workstream_id(task.task_id)
        except ValueError:
            left.append((task.task_id, "it has no canonical task ID"))
            continue
        if _workstream_exists(conn, own):
            left.append((task.task_id, f"its own workstream {own} already exists"))
            continue
        tasks.append(BackfillTask(task.task_id, task.title, task.project_key, here, own))
    return BackfillPlan(tuple(tasks), tuple(left))


def _backfill_read(ctx):
    from .task_state import TaskStateError

    try:
        with closing(connect_ro(db_file(ctx.root))) as conn:
            require_current_schema(conn)
            conn.execute("BEGIN")
            return backfill_plan(conn, fleet_uid=ctx.fleet_uid)
    except TaskStateError as exc:
        raise WorkstreamError("unavailable", f"the fleet's task state is unreadable: {exc}") from exc
    except (OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "the fleet's tasks are unavailable") from exc


def _recheck_backfill(ctx, planned, write):
    """The backfill's batch precondition, read under the plane's write lock:
    each task it files is still open, readable and where its read found it,
    and its own workstream still does not exist. Any change refuses the whole
    batch, and nothing in it is written."""
    from .task_state import TaskStateError, read_tasks

    try:
        now = read_tasks(write, fleet_uid=ctx.fleet_uid, task_ids=[item.task_id for item in planned])
    except TaskStateError as exc:
        raise WorkstreamError("unavailable", f"the fleet's task state is unreadable: {exc}") from exc
    changed = []
    for item in planned:
        task = now.get(item.task_id)
        if (task is None or task.terminal_event is not None or task.blockers or not task.open
                or task.workstream_id != item.from_workstream_id
                or _workstream_exists(write, item.workstream_id)):
            changed.append(item.task_id)
    if changed:
        raise WorkstreamTasksChanged(
            f"tasks changed after the backfill read them: {', '.join(changed)}; nothing was "
            "recorded; preview again, then apply with a new --request-id")


def _backfill_raws(ctx, planned, request_id, now):
    """Two rows per task, in one batch. First its own workstream: the row
    ``task admit`` records for a new task, with the task's title and project,
    the fleet's manager as owner and the caller as opener. Then a move into
    it: the row ``task move`` records (``task_operations._moved``). Each row
    is a few hundred bytes, far under ingest's cut (``_require_whole_row``)."""
    from .task_operations import _moved
    from .task_state import TASK_EMITTER

    manager = ctx.bots.get(ctx.context.fleet.manager)
    if manager is None:
        raise WorkstreamError("conflict", "the fleet's manager is not a declared bot of the selected fleet")
    raws = []
    for item in planned:
        raws.append(_event(ctx, request_id, len(raws), "workstream", dict(
            workstream_id=item.workstream_id, title=item.title, owner=manager.alias,
            opened_by=ctx.caller.alias, project_key=item.project_key), now, emitter=TASK_EMITTER))
        raws.append(_event(ctx, request_id, len(raws), "system", _moved(
            ctx, item.task_id, item.from_workstream_id, item.workstream_id,
            actor=ctx.caller.alias, reason=BACKFILL_REASON), now, emitter=TASK_EMITTER))
    return raws


def apply(ctx, verb: str, args: dict, *, request_id: str) -> WorkstreamResult:
    """Freeze and commit one verb while holding request then fleet registry lock."""
    from .plane.emit_api import emit_batch, load_capture_config, validate_item
    from .plane.workstream_import import registry_lock

    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkstreamError("invalid_argument", "--request-id requires a canonical UUID") from exc
    manager = ctx.bots.get(ctx.context.fleet.manager)
    if not ((manager is not None and ctx.caller == manager and ctx.caller_fleet_uid == ctx.fleet_uid)
            or ctx.caller.alias.startswith("human:") and ctx.caller_fleet_uid is None):
        raise WorkstreamError("conflict", "workstream mutation requires the current manager or local human")
    maximum, days = _configuration(ctx)
    semantic = semantic_digest({"verb": verb, "args": args, "caller": ctx.caller.alias})
    lock_path = ctx.context.paths.fleet_state / "workstreams.lock"
    try:
        with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
            with registry_lock(lock_path):
                previous = store.load()
                if previous is not None:
                    intent = previous.intent
                    if (intent.operation != f"workstream.{verb}" or intent.operation_version != 1
                            or intent.host_uid != ctx.host_uid or intent.fleet_uid != ctx.fleet_uid
                            or intent.caller_uid != ctx.caller.uid or intent.semantic_sha256 != semantic
                            or intent.recipient_uid is not None or intent.route is not None
                            or len(intent.stages) != 1 or intent.stages[0].kind != "recording"):
                        raise WorkstreamError("conflict", "request UUID has different workstream semantics")
                    facts = intent.stages[0].facts
                    proof = _proof(ctx, facts)
                    if proof.status == "committed":
                        if previous.stages[0].status == "unknown":
                            store.outcome(0, "committed")
                        elif previous.stages[0].status != "committed":
                            raise WorkstreamError("conflict", "workstream fact conflicts with retained request state")
                        ids, withdrawn, filed = _ids_from_facts(ctx, facts)
                        return WorkstreamResult(request_id, ids, "committed", True, True,
                                                withdrawn_task_ids=withdrawn, filed_tasks=filed)
                    if proof.status == "unknown":
                        raise WorkstreamError("unavailable", "prior workstream recording proof unavailable; inspect request")
                    if proof.status == "conflict" or previous.stages[0].status == "committed":
                        raise WorkstreamError("conflict", "workstream fact conflicts with retained request state")
                    if previous.stages[0].status == "unknown":
                        store.outcome(0, "unrecorded")
                registry = _reader(ctx, days, or_empty=True)
                now = datetime.now(timezone.utc).isoformat()
                open_tasks = _close_read(ctx, args["id"]) if verb == "close" else ()
                planned = _backfill_read(ctx).tasks if verb == "backfill" else ()
                if verb == "backfill":
                    if not planned and previous is None:
                        # Nothing to file: no request is prepared and nothing is
                        # recorded, so a backfill run again after it is a no-op.
                        return WorkstreamResult(request_id, (), "not_attempted", False, False)
                    ids = tuple(item.workstream_id for item in planned)
                    raws = _backfill_raws(ctx, planned, request_id, now)
                else:
                    ids, raws = _plan(ctx, verb, args, registry, request_id, now, maximum, days,
                                      open_tasks=open_tasks)
                withdrawn = tuple(raw["payload"]["work_item_id"] for raw in raws
                                  if raw["event_type"] == "task")
                filed = tuple(FiledTask(item.task_id, item.from_workstream_id, item.workstream_id)
                              for item in planned)
                parties = {ctx.caller.alias: ctx.caller.uid,
                           **{actor.alias: actor.uid for actor in ctx.bots.values()}}
                modes = load_capture_config(ctx.root)
                facts = tuple(expected_fact(validate_item(raw, modes)[0], host_uid=ctx.host_uid,
                                            fleet_uid=ctx.fleet_uid, parties=parties)
                              for raw in raws)
                if previous is not None and facts != previous.intent.stages[0].facts:
                    raise WorkstreamError(
                        "conflict", "unrecorded workstream request no longer matches current state; "
                        "inspect workstream and request, then use a new --request-id for a new intent")
                with ExitStack() as locks:
                    batch, checks = {}, []
                    if verb == "close":
                        checks = _lock_tasks(locks, store, withdrawn, act="withdrawn",
                                             again="close again once it is free")
                        wid = args["id"]
                        batch["precondition"] = lambda write: _recheck_close(ctx, wid, withdrawn, write)  # noqa: E731
                    elif verb == "backfill":
                        checks = _lock_tasks(locks, store, [item.task_id for item in planned],
                                             act="filed", again="apply again once it is free")
                        batch["precondition"] = lambda write: _recheck_backfill(ctx, planned, write)  # noqa: E731
                    store.prepare(RequestIntent(f"workstream.{verb}", 1, ctx.host_uid, ctx.fleet_uid,
                                                ctx.caller.uid, None, semantic,
                                                (StagePlan("recording", facts),)))
                    _check_locks(checks)
                    store.begin_attempt()
                    store.stage(0)
                    try:
                        outcome = emit_batch(ctx.root, raws, require_commit=True, **batch)
                    except WorkstreamError:
                        # Raised only by a precondition (the close's or the backfill's),
                        # after BEGIN IMMEDIATE and before any row: the batch is definitely
                        # absent. A failed outcome write leaves it unknown, which the next
                        # attempt reconciles.
                        try:
                            store.outcome(0, "unrecorded")
                        except OSError:
                            pass
                        raise
                    except (OSError, sqlite3.Error) as exc:
                        raise WorkstreamError("unavailable", "workstream recording unconfirmed; inspect request") from exc
                    if len(outcome) != len(raws) or any(item.event_id != raw["event_id"] for item, raw in zip(outcome, raws)):
                        raise WorkstreamError("unavailable", "workstream recording result unconfirmed; inspect request")
                    proof = _proof(ctx, facts)
                    if proof.status != "committed":
                        raise WorkstreamError("unavailable", "workstream exact recording proof unavailable; inspect request")
                    try:
                        store.outcome(0, "committed")
                    except OSError as exc:
                        raise WorkstreamError("unavailable", "workstream committed but request outcome persistence failed") from exc
                    return WorkstreamResult(request_id, ids, "committed", True, False,
                                            withdrawn_task_ids=withdrawn, filed_tasks=filed)
    except WorkstreamError:
        raise
    except TimeoutError as exc:
        raise WorkstreamError("conflict", "workstream registry is busy") from exc
    except (ReceiptError, OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "workstream request or Plane storage is unavailable") from exc
