"""Public read-only task and assignment commands over the canonical query owner."""

from __future__ import annotations

from ..command_result import selection_read_conflict

from contextlib import closing
from dataclasses import asdict
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def _read(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..activation_state import read_selection
    from ..activation_identity import read_selected_identity_bindings
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (
        OperationContextError, OperationContextUnavailableError, resolve_operation_scope,
    )
    from ..paths import InvalidPathSelector
    from ..plane.db import connect_ro, db_file
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError, require_current_schema
    from ..releases import ReleaseError
    from ..task_queries import TaskQueryError, list_tasks, show_assignment, show_task
    from ..task_state import TaskStateError

    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no task registry")
        destination, _origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no task registry")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active task selection is unavailable")
        bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                                   package=destination.paths.package)
        if read_selection(destination.paths.root) != selected:
            raise selection_read_conflict('active task selection changed during read')
        if (bindings["manager"] != destination.fleet.manager
                or set(bindings["bots"]) != set(destination.fleet.bots)):
            raise CommandFailure("conflict", "active task identity bindings differ from frozen fleet")
        bot_uid = None
        if args.public_command == "task.list" and args.bot is not None:
            if args.bot not in destination.fleet.bots:
                raise CommandFailure("not_found", "bot is not in the active fleet",
                                     hint="inspect claudlobby bot list")
            bot_uid = bindings["bots"][args.bot]

        # connect_ro cannot initialize an absent DB, and query_only prevents
        # writes. Keep schema admission and the reducer within one snapshot.
        with closing(connect_ro(db_file(destination.paths.root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            if args.public_command == "task.list":
                page = list_tasks(conn, fleet_uid=bindings["fleet_uid"], state=args.state,
                                  bot_uid=bot_uid, limit=args.limit, cursor=args.cursor)
                data = {"fleet": destination.fleet.name, "manager": bindings["manager"],
                        "bot": args.bot, "items": [asdict(item) for item in page.items],
                        "next_cursor": page.next_cursor, "issues": [asdict(issue) for issue in page.issues]}
                lines = tuple(f"{item.task_id}\t{item.state or 'unresolved'}\t{item.title}"
                              for item in page.items)
                if page.next_cursor:
                    lines += (f"next_cursor: {page.next_cursor}",)
            elif args.public_command == "task.show":
                task = show_task(conn, args.task_id, fleet_uid=bindings["fleet_uid"])
                data = {"fleet": destination.fleet.name, "manager": bindings["manager"],
                        "task": asdict(task)}
                lines = (f"{task.task_id}\t{task.state or 'unresolved'}\t{task.title}",)
            else:
                view = show_assignment(conn, args.assignment_id, fleet_uid=bindings["fleet_uid"])
                data = {"fleet": destination.fleet.name, "manager": bindings["manager"],
                        "assignment": asdict(view.assignment), "task": asdict(view.task)}
                lines = (f"{view.assignment.assignment_id}\t{view.assignment.state}\t"
                         f"{view.task.task_id}",)
        return CommandOutput(data, release_id=selected["release_id"], lines=lines)
    except CommandFailure:
        raise
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "task identity registry is unavailable", retryable=True) from exc
    except OperationContextError as exc:
        raise CommandFailure("conflict", "generated task caller context conflicts with active scope") from exc
    except BotNotFoundError as exc:
        raise CommandFailure("conflict", "generated bot origin is not in the active fleet") from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid task root or fleet selector") from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI") from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active task scope is unavailable", retryable=True) from exc
        raise CommandFailure("conflict", "active task scope or identity binding is incomplete",
                             hint=f"{message}; inspect claudlobby host releases") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active task configuration is incomplete") from exc
    except (PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "Plane schema is not current for this CLI") from exc
    except (OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "Plane task storage is unavailable", retryable=True) from exc
    except TaskQueryError as exc:
        message = {"invalid_argument": "invalid task list filter or cursor",
                   "not_found": "canonical task or assignment not found",
                   "wrong_reference": "reference is not a canonical ID for this command",
                   "ambiguous_reference": "reference maps to multiple canonical records"}.get(
                       exc.code, "invalid task query")
        raise CommandFailure(exc.code, message, hint=exc.hint) from exc
    except TaskStateError as exc:
        raise CommandFailure("unavailable", "recorded task state cannot be read safely") from exc
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid generated task selector") from exc


def dispatch(args) -> CommandOutput:
    return _read(args)
