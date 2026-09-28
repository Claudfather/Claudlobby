"""One fleet-scoped workstream writer over the existing Plane registry reducer."""

from __future__ import annotations

from contextlib import closing
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


class WorkstreamError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class WorkstreamResult:
    request_id: str
    workstream_ids: tuple[str, ...]
    recording: str
    request_persisted: bool
    replayed: bool
    notification: str = "not_requested"


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
    from .plane.ingest import CONSTRUCT_TABLES

    ids = []
    with closing(connect_ro(db_file(ctx.root))) as conn:
        require_current_schema(conn)
        for fact in facts:
            table = CONSTRUCT_TABLES.get(fact.family, "events")
            if table == "events" and fact.family == "system":
                continue
            row = conn.execute(f"SELECT workstream_id FROM {table} WHERE event_id=? AND fleet_uid=?",
                               (fact.event_id, ctx.fleet_uid)).fetchone()
            if row is None:
                raise WorkstreamError("unavailable", "committed workstream identity is unavailable")
            ids.append(row[0])
    return tuple(ids)


def _event(ctx, request_id, ordinal, family, payload, now):
    from .plane import PLANE_SCHEMA_VERSION

    return {"event_id": derive_uid("ev", f"workstream:v1:{ctx.host_uid}:{ctx.fleet_uid}:"
                                          f"{request_id}:{ordinal}"),
            "event_type": family, "emitter": "workstream-operation",
            "schema_version": PLANE_SCHEMA_VERSION, "fleet": ctx.context.fleet.name,
            "source_ref": f"request:{request_id}", "occurred_at": now,
            "payload": payload}


def _plan(ctx, verb, args, registry, request_id, now, maximum, days):
    work = registry["workstreams"]
    archived = set(registry.get("archived", ()))
    if verb == "open":
        title = args["title"]
        explicit = args.get("id")
        if explicit:
            wid = explicit
            if wid in work or wid in archived:
                raise WorkstreamError("conflict", "workstream ID already exists or was archived")
        else:
            stem = _slug(title)
            wid, number = stem, 2
            while wid in work or wid in archived:
                wid, number = f"{stem}-{number}", number + 1
                if len(wid) > 131:
                    raise WorkstreamError("conflict", "no available workstream ID within the slug bound")
        if sum(row.get("status") == "active" for row in work.values()) >= maximum:
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
        payload.update(event="blocked", waiting_on=args["on"], note=args["note"])
    elif verb == "unblock":
        if status != "blocked":
            raise WorkstreamError("conflict", "only blocked workstreams can be unblocked")
        if sum(item.get("status") == "active" for item in work.values()) >= maximum:
            raise WorkstreamError("conflict", f"active workstreams are at the cap ({maximum})")
        payload.update(event="unblocked", note=args["note"])
    elif verb == "close":
        payload.update(event="closed", disposition=args.get("status", "done"))
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
                if proof.status != "committed":
                    raise WorkstreamError("unavailable" if proof.status == "unknown" else "conflict",
                                          "prior workstream attempt cannot be proved committed; inspect request")
                if previous.stages[0].status == "unknown":
                    store.outcome(0, "committed")
                elif previous.stages[0].status != "committed":
                    raise WorkstreamError("conflict", "workstream fact conflicts with retained request state")
                return WorkstreamResult(request_id, _ids_from_facts(ctx, facts), "committed", True, True)
            with registry_lock(lock_path):
                registry = _reader(ctx, days, or_empty=True)
                now = datetime.now(timezone.utc).isoformat()
                ids, raws = _plan(ctx, verb, args, registry, request_id, now, maximum, days)
                parties = {ctx.caller.alias: ctx.caller.uid,
                           **{actor.alias: actor.uid for actor in ctx.bots.values()}}
                modes = load_capture_config(ctx.root)
                facts = tuple(expected_fact(validate_item(raw, modes)[0], host_uid=ctx.host_uid,
                                            fleet_uid=ctx.fleet_uid, parties=parties)
                              for raw in raws)
                store.prepare(RequestIntent(f"workstream.{verb}", 1, ctx.host_uid, ctx.fleet_uid,
                                            ctx.caller.uid, None, semantic,
                                            (StagePlan("recording", facts),)))
                store.begin_attempt()
                store.stage(0)
                try:
                    outcome = emit_batch(ctx.root, raws, require_commit=True)
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
                return WorkstreamResult(request_id, ids, "committed", True, False)
    except WorkstreamError:
        raise
    except TimeoutError as exc:
        raise WorkstreamError("conflict", "workstream registry is busy") from exc
    except (ReceiptError, OSError, sqlite3.Error) as exc:
        raise WorkstreamError("unavailable", "workstream request or Plane storage is unavailable") from exc
