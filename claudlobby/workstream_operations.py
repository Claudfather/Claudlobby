"""One fleet-scoped workstream writer over the existing Plane registry reducer."""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
class WorkstreamResult:
    request_id: str
    workstream_ids: tuple[str, ...]
    recording: str
    request_persisted: bool
    replayed: bool
    notification: str = "not_requested"
    #: The queued tasks a close withdrew in its own batch, in task-ID order.
    withdrawn_task_ids: tuple[str, ...] = ()


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
    """The committed workstream IDs, and the tasks a close withdrew, of a
    replayed request."""
    from .plane.ingest import CONSTRUCT_TABLES

    ids, withdrawn = [], []
    with closing(connect_ro(db_file(ctx.root))) as conn:
        require_current_schema(conn)
        for fact in facts:
            table = CONSTRUCT_TABLES.get(fact.family, "events")
            if table == "events" and fact.family == "system":
                continue
            column, found = (("work_item_id", withdrawn) if fact.family == "task"
                             else ("workstream_id", ids))
            row = conn.execute(f"SELECT {column} FROM {table} WHERE event_id=? AND fleet_uid=?",
                               (fact.event_id, ctx.fleet_uid)).fetchone()
            if row is None:
                raise WorkstreamError("unavailable", "committed workstream identity is unavailable")
            found.append(row[0])
    return tuple(ids), tuple(withdrawn)


def _open_tasks(ctx, conn, wid):
    """The workstream's tasks that have not ended, with the task reducer's
    state: the facts the registry's ``open_task_ids`` and admit's join check
    read. A task the reducer cannot resolve has no terminal fact, so it is
    open, with state None."""
    from .task_state import TaskStateError, read_tasks

    ids = [row[0] for row in conn.execute(
        "SELECT work_item_id FROM work_items WHERE workstream_id=? AND fleet_uid=?",
        (wid, ctx.fleet_uid))]
    if not ids:
        return ()
    try:
        snapshot = read_tasks(conn, fleet_uid=ctx.fleet_uid, task_ids=ids)
    except TaskStateError as exc:
        raise WorkstreamError("unavailable", f"the workstream's task state is unreadable: {exc}") from exc
    return tuple(task for task in snapshot.tasks if task.terminal_event is None)


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


def _lock_queued(stack, store, withdrawn):
    """Hold each queued task's lock, the one every task verb takes, through
    the close's write.

    A task verb checks its task's state under that lock but outside the
    plane's write lock, and writes with no precondition. Without the lock, a
    close that committed between such a check and its write would leave that
    verb's row, an assignment say, on a withdrawn task. The precondition
    catches only the other order, a row that lands before the close.

    The locks are taken in sorted task-ID order, so two callers that lock
    overlapping sets take them in one order and neither can hold a lock the
    other is waiting for. (A task lock never waits: a held one refuses.)
    """
    from .task_operations import _locked_task
    from .task_queries import TaskQueryError

    for task_id in sorted(withdrawn):
        try:
            stack.enter_context(_locked_task(store, task_id))
        except TaskQueryError as exc:
            raise WorkstreamError("conflict", f"task {task_id} cannot be withdrawn now: {exc}; "
                                  "nothing was recorded; close again once it is free") from exc


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
        raise WorkstreamError("not_found", "workstream is not live in the selected fleet")
    status = row.get("status")
    if status in ("done", "abandoned"):
        raise WorkstreamError("conflict", "terminal workstream cannot be changed except by prune")
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
        # A task with a live assignment is the manager's to end first, and so
        # is one the reducer cannot resolve, which cannot be told queued from
        # assigned (#1053). The queued ones are withdrawn here, in the close's
        # own batch, so a closed workstream never holds an open task.
        held = [task for task in open_tasks if task.state != "queued"]
        if held:
            raise WorkstreamError(
                "conflict", f"workstream {wid} holds tasks the close cannot withdraw: "
                f"{'; '.join(_not_queued(task) for task in held)}; end each first, then close it")
        from .task_operations import _withdrawal
        from .task_state import TASK_EMITTER

        disposition = args.get("status", "done")
        payload.update(event="closed", disposition=disposition)
        reason = f"withdrawn by the close of workstream {wid} ({disposition})"
        return (wid,), [_event(ctx, request_id, 0, "workstream_event", payload, now)] + [
            _event(ctx, request_id, ordinal, "task",
                   _withdrawal(task_id, actor=ctx.caller.alias, reason=reason), now,
                   emitter=TASK_EMITTER)
            for ordinal, task_id in enumerate(sorted(task.task_id for task in open_tasks), 1)]
    else:
        raise WorkstreamError("invalid_argument", "unknown workstream verb")
    return (wid,), [_event(ctx, request_id, 0, "workstream_event", payload, now)]


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
                        ids, withdrawn = _ids_from_facts(ctx, facts)
                        return WorkstreamResult(request_id, ids, "committed", True, True,
                                                withdrawn_task_ids=withdrawn)
                    if proof.status == "unknown":
                        raise WorkstreamError("unavailable", "prior workstream recording proof unavailable; inspect request")
                    if proof.status == "conflict" or previous.stages[0].status == "committed":
                        raise WorkstreamError("conflict", "workstream fact conflicts with retained request state")
                    if previous.stages[0].status == "unknown":
                        store.outcome(0, "unrecorded")
                registry = _reader(ctx, days, or_empty=True)
                now = datetime.now(timezone.utc).isoformat()
                open_tasks = _close_read(ctx, args["id"]) if verb == "close" else ()
                ids, raws = _plan(ctx, verb, args, registry, request_id, now, maximum, days,
                                  open_tasks=open_tasks)
                withdrawn = tuple(raw["payload"]["work_item_id"] for raw in raws
                                  if raw["event_type"] == "task")
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
                    batch = {}
                    if verb == "close":
                        _lock_queued(locks, store, withdrawn)
                        wid = args["id"]
                        batch["precondition"] = lambda write: _recheck_close(ctx, wid, withdrawn, write)  # noqa: E731
                    store.prepare(RequestIntent(f"workstream.{verb}", 1, ctx.host_uid, ctx.fleet_uid,
                                                ctx.caller.uid, None, semantic,
                                                (StagePlan("recording", facts),)))
                    store.begin_attempt()
                    store.stage(0)
                    try:
                        outcome = emit_batch(ctx.root, raws, require_commit=True, **batch)
                    except WorkstreamError:
                        # Raised only by the close's precondition, after BEGIN IMMEDIATE
                        # and before any row: the batch is definitely absent. A failed
                        # outcome write leaves it unknown, which the next attempt reconciles.
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
                                            withdrawn_task_ids=withdrawn)
    except WorkstreamError:
        raise
    except TimeoutError as exc:
        raise WorkstreamError("conflict", "workstream registry is busy") from exc
    except (ReceiptError, OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "workstream request or Plane storage is unavailable") from exc
