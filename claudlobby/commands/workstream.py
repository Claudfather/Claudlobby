"""Selected-fleet workstream reads and committed coordination verbs."""

from __future__ import annotations

from dataclasses import asdict
import os
import re
import sqlite3
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


def _text(value, name, *, cap=4096):
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value.encode("utf-8")) > cap):
        raise CommandFailure("invalid_argument", f"{name} requires bounded nonempty text")
    return value


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"ws-[a-z0-9][a-z0-9-]{0,127}", value):
        raise CommandFailure("invalid_argument", "workstream ID must use ws- followed by a slug")
    return value


def _request(value):
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


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


def _read(args, destination, selected, bindings):
    from ..brief import plane_session
    from ..plane.schema_state import require_current_schema
    from ..workstreams import format_list, format_show

    session, note = plane_session(destination.paths, destination.fleet.name)
    if session is None:
        raise CommandFailure("unavailable", note or "Plane workstream registry is unavailable")
    with session:
        session.conn.execute("BEGIN")
        require_current_schema(session.conn)
        if session.pr.fleet_uid(session.conn, destination.fleet.name) != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "Plane fleet differs from selected workstream scope")
        registry = session.pr.workstream_registry(
            session.conn, destination.fleet.name,
            lease_days=destination.fleet.workstreams.lease_days)
    from ..activation_state import read_selection

    if read_selection(destination.paths.root) != selected:
        raise CommandFailure("conflict", "active workstream selection changed during read")
    entries = registry["workstreams"]
    if args.workstream_action == "show":
        row = entries.get(_id(args.id))
        if row is None:
            raise CommandFailure("not_found", "workstream is not live in selected fleet")
        return CommandOutput({"fleet": destination.fleet.name, "workstream": row},
                             release_id=selected["release_id"], lines=(format_show(row),))
    return CommandOutput({"fleet": destination.fleet.name, "workstreams": entries,
                          "updated": registry["updated"]}, release_id=selected["release_id"],
                         lines=(format_list(entries),))


def _write(args, destination, origin, selected, bindings):
    from ..activation_state import read_selection
    from ..operation_context import resolve_task_mutation_context
    from ..runtime_admission import RuntimeIdentity, mutation_admission
    from ..workstream_operations import apply

    request_id = _request(args.request_id)
    data = _inputs(args, destination)
    if args.dry_run:
        return CommandOutput({"validated": True, "recording": "not_attempted",
                              "workstream_ids": [], "scope_state": "not_checked"},
                             release_id=selected["release_id"],
                             lines=("Workstream input valid; no request or fact recorded.",))
    bound_release = None
    if origin is not None:
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
        if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
            raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
    with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                            expected_release=bound_release) as release:
        if release.release_id != selected["release_id"] or read_selection(destination.paths.root) != selected:
            raise CommandFailure("release_mismatch", "selected workstream release changed")
        ctx = resolve_task_mutation_context(root=destination.paths.root,
                                            fleet=destination.fleet.name,
                                            package=destination.paths.package)
        if ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "workstream identity differs from active selection")
        result = apply(ctx, args.workstream_action, data, request_id=request_id)
    return CommandOutput({"fleet": destination.fleet.name, **asdict(result)},
                         release_id=release.release_id,
                         lines=(f"{args.workstream_action}: {', '.join(result.workstream_ids) or '(none)'}; "
                                f"recording={result.recording}; replayed={result.replayed}",))


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
    from ..runtime_admission import ReleaseMismatch
    from ..workstream_operations import WorkstreamError

    try:
        destination, origin, selected, bindings = _scope(args)
        if origin is not None and args.workstream_action not in ("list", "show"):
            if origin.fleet.name != destination.fleet.name or origin.bot_id != destination.fleet.manager:
                raise CommandFailure("conflict", "generated workstream writer must be the selected manager")
        if args.workstream_action in ("list", "show"):
            return _read(args, destination, selected, bindings)
        return _write(args, destination, origin, selected, bindings)
    except CommandFailure:
        raise
    except WorkstreamError as exc:
        raise CommandFailure(exc.code, str(exc)) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from caller", hint=exc.hint) from exc
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
