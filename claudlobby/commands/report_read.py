"""Read-only fleet report listing over the existing Plane report decoder."""

from __future__ import annotations

from ..command_result import selection_read_conflict

import base64
import binascii
from datetime import datetime, timezone
import json
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def _since(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(timezone.utc).isoformat()
    except (AttributeError, ValueError, OverflowError) as exc:
        raise CommandFailure("invalid_argument", "--since requires an RFC3339 instant with an offset") from exc


def _cursor(scope: dict, after: tuple[int, str]) -> str:
    payload = json.dumps({"v": 1, "scope": scope, "after": after},
                         sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _after(token: str | None, scope: dict, release_id: str) -> tuple[int, str] | None:
    if token is None:
        return None
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"v", "scope", "after"}
                or type(value["v"]) is not int or value["v"] != 1
                or value["scope"] != scope):
            raise ValueError
        key = value["after"]
        if (not isinstance(key, list) or len(key) != 2 or type(key[0]) is not int
                or key[0] < 1 or not isinstance(key[1], str) or not key[1]):
            raise ValueError
        return key[0], key[1]
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise CommandFailure("invalid_argument", "invalid report cursor or changed viewer, filters or read position",
                             release_id=release_id) from exc


def _line(row: dict) -> str:
    report = row["report"]
    summary = ((" ".join(row["summary"].split()) or "[summary unavailable]")
               if report["state"] in ("captured", "legacy")
               else f"[content {report['state']}]")
    return f"{row['message_id']}\t{row['author']}\t{row['status'] or 'unknown'}\t{summary}"


def _item(row: dict) -> dict:
    """Name the canonical public fields without changing the shared decoder."""
    canonical_task = row["work_item_id"] or None
    historical = row["task_id"] or None
    if historical == canonical_task:
        historical = None
    return {
        "message_id": row["plane_msg_id"],
        "author": row["bot"],
        "occurred_at": row["ts"],
        "ingest_seq": row["_seq"],
        "task_id": canonical_task,
        "historical_task_reference": historical,
        "assignment_id": row["assignment_id"] or None,
        "task_event": row["task_event"],
        "status": row["status"] or None,
        "summary": row["summary"],
        "report": row["report"],
        "evidence": {field: row[field] for field in (
            "pr_url", "pr_role", "artifact", "issues", "skill", "progress")},
    }


def _read(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..activation_identity import read_selected_identity_bindings
    from ..brief import plane_session
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (
        OperationContextError, OperationContextUnavailableError, bind_task_context,
        resolve_operation_scope,
    )
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError, require_current_schema
    from ..report_cursors import CursorError, decode_cursor, issue_cursor, validate_prefix
    from ..releases import ReleaseError

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no report history")
        if type(args.limit) is not int or not 1 <= args.limit <= 1000:
            raise CommandFailure("invalid_argument", "--limit must be from 1 to 1000")
        if args.bot is not None and (not args.bot or args.bot != args.bot.strip()):
            raise CommandFailure("invalid_argument", "--bot requires one exact author")
        if args.status is not None and (not args.status or args.status != args.status.strip()):
            raise CommandFailure("invalid_argument", "--status requires one exact status")
        since = _since(args.since)
        destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no report history")
        if origin is not None and (origin.bot_id is None or origin.fleet.name != destination.fleet.name):
            raise CommandFailure("conflict", "generated report viewer differs from the selected fleet")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active report selection is unavailable")
        release_id = selected["release_id"]
        bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                                   package=destination.paths.package)
        if (bindings["manager"] != destination.fleet.manager
                or set(bindings["bots"]) != set(destination.fleet.bots)):
            raise CommandFailure("conflict", "active report identity bindings differ from frozen fleet",
                                 release_id=release_id)
        viewer = viewer_uid = None
        if origin is not None:
            ctx = bind_task_context(destination, origin=origin)
            viewer, viewer_uid = ctx.caller.alias, ctx.caller.uid
            if (ctx.caller_fleet_uid != bindings["fleet_uid"]
                    or ctx.fleet_uid != bindings["fleet_uid"]
                    or viewer_uid != bindings["bots"].get(origin.bot_id)):
                raise CommandFailure("conflict", "report viewer differs from the selected fleet",
                                     release_id=release_id)
        elif args.unacknowledged:
            raise CommandFailure("conflict", "unacknowledged reports require a generated viewer; ordinary reads have no read position",
                                 release_id=release_id)
        session, _note = plane_session(destination.paths, destination.fleet.name)
        if session is None:
            raise CommandFailure("unavailable", "Plane report storage is unavailable; inspect host status",
                                 retryable=True, release_id=release_id)
        with session:
            session.conn.execute("BEGIN")
            require_current_schema(session.conn)
            if session.pr.fleet_uid(session.conn, destination.fleet.name) != bindings["fleet_uid"]:
                raise CommandFailure("conflict", "report fleet identity differs from the active binding",
                                     release_id=release_id)
            ack = None
            if origin is not None:
                entry = session.pr.roster(session.conn, destination.fleet.name).get(origin.bot_id.lower())
                if entry is None or viewer_uid not in entry["uids"]:
                    raise CommandFailure("unavailable", "report viewer identity is unavailable",
                                         release_id=release_id)
                ack = session.pr.newest_ack(session.conn, entry["uids"])
            prior_seq = ack["seq"] if ack else None
            prior_ack_seq = ack["landed_seq"] if ack else None
            eligible = (origin is not None and args.unacknowledged and args.bot is None
                        and args.status is None and since is None)
            cursor_identity = {"host_uid": bindings["host_uid"],
                               "fleet_uid": bindings["fleet_uid"], "viewer_uid": viewer_uid,
                               "release_id": release_id,
                               "activation_id": selected["activation_id"],
                               "plan_id": selected["plan_id"]} if eligible else None
            scope = {"fleet_uid": bindings["fleet_uid"], "viewer_uid": viewer_uid,
                     "release_id": release_id, "author": args.bot, "status": args.status,
                     "since": since, "unacknowledged": args.unacknowledged,
                     "prior_seq": prior_seq, "prior_ack_event": prior_ack_seq}
            after = None if eligible else _after(args.cursor, scope, release_id)
            rows = session.pr.report_rows(session.conn, destination.fleet.name,
                                          bot=args.bot, status=args.status, since=since)
            if args.unacknowledged:
                rows = session.pr.unacked_rows(rows, prior_seq)
            # The shared reader's historical display order is event time. A
            # report may be backdated, so page only after ingest ordering.
            rows.sort(key=lambda row: (row["_seq"], row["plane_msg_id"]))
            if eligible:
                served = []
                if args.cursor is not None:
                    prefix = decode_cursor(destination.paths.root, args.cursor,
                                           identity=cursor_identity)
                    if prefix.prior_seq != prior_seq or prefix.prior_ack_seq != prior_ack_seq:
                        raise CommandFailure("conflict", "report read position advanced after this page",
                                             release_id=release_id)
                    served = validate_prefix(prefix, rows)
                remaining = rows[len(served):]
                page = remaining[:args.limit]
                ack_cursor = (issue_cursor(destination.paths.root, cursor_identity,
                                           prior_seq=prior_seq, prior_ack_seq=prior_ack_seq,
                                           rows=[*served, *page]) if page else args.cursor)
                next_cursor = ack_cursor if len(remaining) > args.limit else None
            else:
                if after is not None:
                    rows = [row for row in rows if (row["_seq"], row["plane_msg_id"]) > after]
                page = rows[:args.limit]
                next_cursor = (_cursor(scope, (page[-1]["_seq"], page[-1]["plane_msg_id"]))
                               if len(rows) > args.limit else None)
                ack_cursor = None
            items = [_item(row) for row in page]
            lines = tuple(_line(item) for item in items)
        if read_selection(destination.paths.root) != selected:
            raise selection_read_conflict('active report selection changed during read', release_id=release_id)
        return CommandOutput({"fleet": destination.fleet.name, "viewer": viewer,
                              "viewer_uid": viewer_uid,
                              "items": items, "next_cursor": next_cursor,
                              "ack_cursor": ack_cursor, "ack_available": ack_cursor is not None},
                             release_id=release_id, lines=lines)
    except CommandFailure:
        raise
    except CursorError as exc:
        code = "unavailable" if exc.unavailable else "invalid_argument"
        raise CommandFailure(code, str(exc), retryable=exc.unavailable,
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "report identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except OperationContextError as exc:
        raise CommandFailure("conflict", "generated report viewer conflicts with active scope",
                             release_id=release_id) from exc
    except BotNotFoundError as exc:
        raise CommandFailure("conflict", "generated report viewer is not in the active fleet",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid report root or fleet selector",
                             release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI") from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active report scope is unavailable",
                                 retryable=True, release_id=release_id) from exc
        raise CommandFailure("conflict", "active report scope is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active report configuration is incomplete",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "Plane report storage is unavailable",
                             retryable=True, release_id=release_id) from exc
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid generated report selector",
                             release_id=release_id) from exc


def dispatch(args) -> CommandOutput:
    return _read(args)
