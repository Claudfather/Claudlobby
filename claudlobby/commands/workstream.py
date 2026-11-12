"""Selected-fleet workstream reads and committed coordination verbs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput
from ..request_ids import canonical_request_id


def _text(value, name, *, cap=4096):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode("utf-8")) > cap):
        raise CommandFailure("invalid_argument", f"{name} requires bounded nonempty text")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"ws-[a-z0-9][a-z0-9-]{0,127}", value):
        raise CommandFailure("invalid_argument", "workstream ID must use ws- followed by a slug")
    return value


def _inputs(args, destination):
    verb = args.workstream_action
    data = {}
    if verb == "open":
        data["title"] = _text(args.title, "TITLE")
        data["id"] = _id(args.id) if args.id else None
        data["project"] = args.project
        if data["project"] is not None and not re.fullmatch(r"[a-z][a-z0-9-]*", data["project"]):
            raise CommandFailure("invalid_argument", "--project requires a project key")
        data["owner"] = args.owner
        if data["owner"] is not None and data["owner"] not in destination.fleet.bots:
            raise CommandFailure("not_found", "--owner is not a selected-fleet bot")
        data["next"] = _text(args.next, "--next") if args.next is not None else None
    elif verb != "prune":
        data["id"] = _id(args.id)
        if verb == "progress":
            data["next"] = _text(args.next, "--next") if args.next is not None else None
        if verb in ("renew", "block", "unblock"):
            data["note"] = _text(args.note, "--note")
        if verb == "block":
            actor = _text(args.on, "--on", cap=256)
            if not re.fullmatch(r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)", actor):
                raise CommandFailure("invalid_argument", "--on requires a canonical human or bot actor alias")
            data["on"] = actor
        if verb == "close":
            data["status"] = args.status
    return data


def _scope(args):
    from .checkin import _scope as selected_scope

    return selected_scope(args)


def _session_read(destination, bindings, read):
    """*read* on one read-only snapshot of the selected fleet's plane: the
    reads, the close's dry run and the backfill's preview. It registers no
    identity and opens no request."""
    from ..brief import plane_session
    from ..plane.schema_state import require_current_schema

    session, note = plane_session(destination.paths, destination.fleet.name)
    if session is None:
        raise CommandFailure("unavailable", note or "Plane workstream registry is unavailable")
    with session:
        session.conn.execute("BEGIN")
        require_current_schema(session.conn)
        if session.pr.fleet_uid(session.conn, destination.fleet.name) != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "Plane fleet differs from selected workstream scope")
        return read(session)


def _registry(session, destination):
    return session.pr.workstream_registry(session.conn, destination.fleet.name,
                                          lease_days=destination.fleet.workstreams.lease_days)


def _read(args, destination, selected, bindings):
    from ..workstreams import format_list, format_show

    registry = _session_read(destination, bindings, lambda session: _registry(session, destination))
    selected.check(destination.paths.root, package=destination.paths.package,
                   message='active workstream selection changed during read')
    entries = registry["workstreams"]
    if args.workstream_action == "show":
        row = entries.get(_id(args.id))
        if row is None:
            raise CommandFailure("not_found", "workstream is not live in selected fleet")
        return CommandOutput({"fleet": destination.fleet.name, "workstream": row, **selected.provenance},
                             release_id=selected.release_id, lines=(format_show(row),))
    return CommandOutput({"fleet": destination.fleet.name, "workstreams": entries,
                          "updated": registry["updated"], **selected.provenance}, release_id=selected.release_id,
                         lines=(format_list(entries),))


@contextmanager
def _admitted(destination, origin, selected, bindings):
    """The selected release's mutation admission, then the caller's operation
    context: one path for a recorded verb and the backfill's apply."""
    from ..active_config import runtime_read_stamp
    from ..operation_context import resolve_task_mutation_context
    from ..runtime_admission import (RuntimeIdentity, mutation_admission, runtime_release_id,
                                     runtime_route_matches)
    from .message_write import _bound_release

    bound_release = _bound_release(destination.paths.package, generated=origin is not None)
    with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                            expected_release=bound_release) as release:
        if (not runtime_route_matches(release, selected, package=destination.paths.package)
                or runtime_read_stamp(destination.paths.root, package=destination.paths.package) != selected):
            raise CommandFailure("release_mismatch", "selected workstream release changed",
                                 data=selected.provenance,
                                 hint=("inspect the current configuration and claudlobby host update status"
                                       if selected.kind == "git" else None))
        ctx = resolve_task_mutation_context(root=destination.paths.root,
                                            fleet=destination.fleet.name,
                                            package=destination.paths.package)
        if ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "workstream identity differs from active selection")
        selected.check(destination.paths.root, package=destination.paths.package)
        yield ctx, runtime_release_id(release)


def _close_preview(destination, bindings, wid):
    """What ``workstream close`` would do now, read and not recorded: the
    refusal it would raise, or the queued tasks it would withdraw (#1053)."""
    from ..workstream_operations import close_refusal, open_tasks

    def read(session):
        row = _registry(session, destination)["workstreams"].get(wid)
        tasks = (open_tasks(session.conn, fleet_uid=bindings["fleet_uid"], workstream_id=wid)
                 if row is not None else ())
        return close_refusal(wid, row, tasks), sorted(task.task_id for task in tasks)

    refusal, task_ids = _session_read(destination, bindings, read)
    if refusal is not None:
        return {"refusal": {"code": refusal[0], "message": refusal[1]}, "withdraw_task_ids": []}
    return {"refusal": None, "withdraw_task_ids": task_ids}


def _close_preview_line(wid, preview):
    refusal = preview["refusal"]
    if refusal is not None:
        return (f"close {wid} would be refused ({refusal['code']}): {refusal['message']}; "
                "no request or fact recorded.")
    return (f"close {wid} would withdraw: {', '.join(preview['withdraw_task_ids']) or 'no task'}; "
            "no request or fact recorded.")


def _write(args, destination, origin, selected, bindings):
    from ..workstream_operations import apply

    request_id = canonical_request_id(args.request_id)
    data = _inputs(args, destination)
    if args.dry_run:
        out = {"validated": True, "recording": "not_attempted",
               "workstream_ids": [], "scope_state": "not_checked", **selected.provenance}
        lines = ("Workstream input valid; no request or fact recorded.",)
        if args.workstream_action == "close":
            # What a close does depends on its workstream's tasks, so its dry run reads them.
            out.update(scope_state="read", close=_close_preview(destination, bindings, data["id"]))
            selected.check(destination.paths.root, package=destination.paths.package,
                           message="active workstream selection changed during read")
            lines = (_close_preview_line(data["id"], out["close"]),)
        return CommandOutput(out, release_id=selected.release_id, lines=lines)
    with _admitted(destination, origin, selected, bindings) as (ctx, release_id):
        result = apply(ctx, args.workstream_action, data, request_id=request_id)
    return CommandOutput({"fleet": destination.fleet.name, **asdict(result), **selected.provenance},
                         release_id=release_id, lines=write_lines(args.workstream_action, result))


def _backfill(args, destination, origin, selected, bindings):
    """``migration workstream-backfill`` (#1053): a preview by default, and
    ``--apply`` records it (``workstream_operations.backfill_plan``)."""
    from ..task_state import TaskStateError
    from ..workstream_operations import apply, backfill_plan

    if not args.apply:
        if args.request_id is not None:
            raise CommandFailure("invalid_argument", "--request-id is for --apply; a preview records nothing")

        def read(session):
            try:
                return backfill_plan(session.conn, fleet_uid=bindings["fleet_uid"])
            except TaskStateError as exc:
                raise CommandFailure("unavailable", f"the fleet's task state is unreadable: {exc}") from exc

        plan = _session_read(destination, bindings, read)
        selected.check(destination.paths.root, package=destination.paths.package,
                       message="active workstream selection changed during read")
        return CommandOutput({"fleet": destination.fleet.name, "mode": "preview",
                              "recording": "not_attempted",
                              "tasks": [asdict(item) for item in plan.tasks],
                              "left": [{"task_id": task_id, "reason": why} for task_id, why in plan.left],
                              **selected.provenance},
                             release_id=selected.release_id, lines=backfill_preview_lines(plan))
    if args.request_id is None:
        raise CommandFailure("invalid_argument", "--apply requires --request-id UUID")
    request_id = canonical_request_id(args.request_id)
    with _admitted(destination, origin, selected, bindings) as (ctx, release_id):
        result = apply(ctx, "backfill", {}, request_id=request_id)
    return CommandOutput({"fleet": destination.fleet.name, "mode": "apply", **asdict(result),
                          **selected.provenance},
                         release_id=release_id, lines=write_lines("backfill", result))


def _filed_lines(pairs, verb) -> tuple[str, ...]:
    """One line for the tasks that had no workstream, then one for each
    closed workstream that tasks leave."""
    groups = {}
    for task_id, left in pairs:
        groups.setdefault(left, []).append(task_id)
    lines = ()
    if None in groups:
        lines += (f"{verb} (no workstream): {', '.join(groups.pop(None))}",)
    for left in sorted(groups):
        lines += (f"{verb} out of closed {left}: {', '.join(groups[left])}",)
    return lines


def backfill_preview_lines(plan) -> tuple[str, ...]:
    """The preview's text: every task the apply would file, by where it is
    now, and every open task it would leave, with why."""
    return ((f"backfill preview: {len(plan.tasks)} task(s) to file into their own workstreams; "
             "no request or fact recorded",)
            + _filed_lines([(item.task_id, item.from_workstream_id) for item in plan.tasks], "would file")
            + tuple(f"left alone: {task_id}: {why}" for task_id, why in plan.left))


def write_lines(verb, result) -> tuple[str, ...]:
    """The text view of a recorded verb. A close that withdrew queued tasks
    names them on a line of their own, and a backfill names every task it
    filed: a person reading the text must see every task it changed, not
    only the workstreams."""
    if verb == "backfill":
        if result.recording == "not_attempted":
            return ("backfill: no open task needs a workstream; no request or fact recorded",)
        return ((f"backfill: {len(result.filed_tasks)} task(s) filed into their own workstreams; "
                 f"recording={result.recording}; replayed={result.replayed}",)
                + _filed_lines([(task.task_id, task.from_workstream_id) for task in result.filed_tasks],
                               "filed"))
    lines = (f"{verb}: {', '.join(result.workstream_ids) or '(none)'}; "
             f"recording={result.recording}; replayed={result.replayed}",)
    if result.withdrawn_task_ids:
        lines += (f"withdrawn by the close: {', '.join(result.withdrawn_task_ids)}",)
    return lines


def dispatch(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import OperationContextError, OperationContextUnavailableError
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptError
    from ..workstream_operations import WorkstreamError

    try:
        destination, origin, selected, bindings = _scope(args)
        if origin is not None and args.workstream_action not in ("list", "show"):
            if origin.fleet.name != destination.fleet.name or origin.bot_id != destination.fleet.manager:
                raise CommandFailure("conflict", "generated workstream writer must be the selected manager")
        if args.workstream_action in ("list", "show"):
            return _read(args, destination, selected, bindings)
        if args.workstream_action == "backfill":
            return _backfill(args, destination, origin, selected, bindings)
        return _write(args, destination, origin, selected, bindings)
    except CommandFailure:
        raise
    except WorkstreamError as exc:
        raise CommandFailure(exc.code, str(exc)) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid workstream root or fleet selector") from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "workstream identity registry is unavailable") from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError) as exc:
        raise CommandFailure("conflict", "active workstream scope is incomplete") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable") from exc
    except (PendingMigrationError, DowngradeError, ReceiptError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "workstream Plane or request state is unavailable") from exc
