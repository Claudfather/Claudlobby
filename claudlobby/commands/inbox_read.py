"""Read-only fleet inbox: open work, a selected viewer's reports, and attention."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import ActivationError, read_selection
    from ..brief import ALERT_WINDOW_H, _alerts_section, plane_session
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..inbox_queries import InboxCursorError, project_inbox
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     resolve_operation_scope)
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError, require_current_schema
    from ..releases import ReleaseError
    from ..task_queries import TaskQueryError
    from ..task_state import TaskStateError
    from .report_read import _item as report_item

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no fleet inbox")
        if type(args.limit) is not int or not 1 <= args.limit <= 1000:
            raise CommandFailure("invalid_argument", "--limit must be from 1 to 1000")
        destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no fleet inbox")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active inbox selection is unavailable")
        release_id = selected["release_id"]
        bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                                   package=destination.paths.package)
        if (bindings["manager"] != destination.fleet.manager
                or set(bindings["bots"]) != set(destination.fleet.bots)):
            raise CommandFailure("conflict", "active inbox identity bindings differ from frozen fleet",
                                 release_id=release_id)
        if origin is not None and (origin.fleet.name != destination.fleet.name
                                   or origin.bot_id not in destination.fleet.bots):
            raise CommandFailure("conflict", "generated inbox viewer differs from the selected fleet",
                                 release_id=release_id)
        if args.bot is not None:
            if args.bot not in destination.fleet.bots:
                raise CommandFailure("not_found", "viewer bot is not in the active fleet",
                                     release_id=release_id)
            viewer_bot, selected_by = args.bot, "explicit"
        elif origin is not None:
            viewer_bot, selected_by = origin.bot_id, "generated"
        else:
            viewer_bot, selected_by = bindings["manager"], "manager_default"
        viewer_uid = bindings["bots"][viewer_bot]
        session, _note = plane_session(destination.paths, destination.fleet.name)
        if session is None:
            raise CommandFailure("unavailable", "Plane inbox storage or its shared reader is unavailable",
                                 retryable=True, release_id=release_id)
        with session:
            session.conn.execute("BEGIN")
            require_current_schema(session.conn)
            if session.pr.fleet_uid(session.conn, destination.fleet.name) != bindings["fleet_uid"]:
                raise CommandFailure("conflict", "inbox fleet identity differs from active binding",
                                     release_id=release_id)
            entry = session.pr.roster(session.conn, destination.fleet.name).get(viewer_bot.lower())
            if entry is None or viewer_uid not in entry["uids"]:
                raise CommandFailure("unavailable", "selected viewer history is unavailable",
                                     release_id=release_id)
            if not all(hasattr(session.pr, method) for method in (
                    "report_rows", "unacked_rows", "newest_ack")):
                raise CommandFailure("unavailable", "installed Plane inbox reader is incomplete",
                                     release_id=release_id)
            scope = {"host_uid": bindings["host_uid"], "fleet_uid": bindings["fleet_uid"],
                     "viewer_uid": viewer_uid, "release_id": release_id,
                     "activation_id": selected["activation_id"], "plan_id": selected["plan_id"],
                     "limit": args.limit}
            page = project_inbox(session.conn, session.pr, fleet=destination.fleet.name,
                                 fleet_uid=bindings["fleet_uid"], viewer_uids=entry["uids"],
                                 scope=scope, limit=args.limit, cursor=args.cursor)
            from ..workstreams import blocked_waits
            waits = blocked_waits(session.pr.workstream_registry(
                session.conn, destination.fleet.name,
                lease_days=destination.fleet.workstreams.lease_days)["workstreams"],
                int(datetime.now(timezone.utc).timestamp()))
            degraded = []
            alerts = _alerts_section(destination.paths, viewer_bot,
                                     int(datetime.now(timezone.utc).timestamp()),
                                     degraded, plane=session)
            alert_omitted = any(item.field == "alerts" and item.mode == "omitted"
                                for item in degraded)
            alert_labeled = any(item.field == "alerts" and item.mode == "labeled"
                                for item in degraded)
        if read_selection(destination.paths.root) != selected:
            raise CommandFailure("conflict", "active inbox selection changed during read",
                                 release_id=release_id)
        reports = [report_item(row) for row in page.reports]
        viewer = f"bot:{destination.fleet.name}/{viewer_bot}"
        data = {"fleet": destination.fleet.name, "manager": bindings["manager"],
                "viewer": viewer, "viewer_uid": viewer_uid, "viewer_selection": selected_by,
                "work": {"items": [asdict(task) for task in page.work],
                         "issues": [asdict(issue) for issue in page.issues]},
                "reports": {"items": reports, "ack_cursor": None,
                            "read_position": {"acked_through_seq": page.prior_seq,
                                              "ack_event_seq": page.prior_ack_seq}},
                "attention": {"recorded_escalations": list(page.recorded_escalations),
                              "blocked_workstream_waits": waits,
                              "escalation_observation": "currently_open_task_escalations_including_queued_work",
                              "alerts": None if alert_omitted else alerts,
                              "alert_window_hours": ALERT_WINDOW_H,
                              "alert_read_position": None,
                              "alerts_observation": ("unavailable" if alert_omitted else
                                                     "recent_recorded_incomplete" if alert_labeled else
                                                     "recent_recorded"),
                              "degraded": [asdict(item) for item in degraded]},
                "next_cursor": page.next_cursor}
        lines = (f"Fleet {destination.fleet.name}; report/alert view for {viewer} ({selected_by})",
                 f"Open work on this page: {len(page.work)}; unseen reports on this page: {len(reports)}; "
                 f"currently open task escalations (including queued work): {len(page.recorded_escalations)}; "
                 f"critical alerts in last {ALERT_WINDOW_H}h: "
                 f"{'unavailable' if alert_omitted else len(alerts)}")
        lines += tuple(f"task {task.task_id}\t{task.state or 'unresolved'}\t{task.title}"
                       for task in page.work)
        if page.issues:
            blocking = sum(issue.blocking for issue in page.issues)
            lines += (f"Work history caveat: {len(page.issues)} recorded issue(s), "
                      f"{blocking} blocking; this page's open-work count is not a fleet all-clear.",)
            lines += tuple(
                f"work issue {issue.code}\ttask={issue.task_id}"
                f"{f' assignment={issue.assignment_id}' if issue.assignment_id else ''}"
                f"{f' event={issue.event_id}' if issue.event_id else ''}"
                f"\t{'blocking' if issue.blocking else 'nonblocking'}"
                for issue in page.issues)
        lines += tuple(f"report {row['message_id']}\t{row['author']}\t{row['status'] or 'unknown'}"
                       for row in reports)
        lines += tuple(
            f"task escalation {row['task_id']}\tassignment={row['assignment_id'] or '-'}"
            f"\tevent={row['event_id']}\tquestion={row['question'] or ''}"
            for row in page.recorded_escalations)
        lines += tuple(f"workstream wait {row['id']}\ton={row['waiting_on'] or 'unknown'}"
                       f"\tage_seconds={row['age_seconds'] if row['age_seconds'] is not None else 'unknown'}"
                       f"\tnote={row['note'] or ''}" for row in waits)
        lines += tuple(f"inbox {item.field} {item.mode}: {item.reason}" for item in degraded)
        if page.next_cursor:
            lines += (f"next_cursor: {page.next_cursor}",)
        return CommandOutput(data, release_id=release_id, lines=lines)
    except CommandFailure:
        raise
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "inbox identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except (OperationContextError, BotNotFoundError) as exc:
        raise CommandFailure("conflict", "generated inbox context conflicts with active scope",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid inbox root or fleet selector",
                             release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI") from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active inbox scope is unavailable",
                                 retryable=True, release_id=release_id) from exc
        raise CommandFailure("conflict", "active inbox scope is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active inbox configuration is incomplete",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "Plane schema is not current for this CLI",
                             release_id=release_id) from exc
    except (OSError, sqlite3.Error, RuntimeError, TaskStateError) as exc:
        raise CommandFailure("unavailable", "recorded fleet inbox cannot be read safely",
                             retryable=True, release_id=release_id) from exc
    except (InboxCursorError, TaskQueryError) as exc:
        raise CommandFailure("invalid_argument", "invalid inbox limit or cursor",
                             release_id=release_id) from exc
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid generated inbox selector",
                             release_id=release_id) from exc
