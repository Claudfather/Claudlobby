"""Public admission, assignment and acceptance over the durable task owner."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import re
import sqlite3
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


def _reference(value: str, field: str) -> str:
    # The scoped task reader identifies wrong-kind and historical references
    # and returns a concrete canonical remedy; this command never acts on one.
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CommandFailure("invalid_argument", f"{field} requires a task or assignment reference")
    return value


def _request_id(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CommandFailure("invalid_argument", f"{field} requires nonempty text")
    return value


def _optional(value: str | None, field: str, pattern: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not re.fullmatch(pattern, value)):
        raise CommandFailure("invalid_argument", f"invalid {field}")
    return value


def _body(path: str | None) -> str | None:
    if path is None:
        return None
    from ..plane.registries import FIELD_POLICY

    file = Path(path).expanduser()
    try:
        if not file.is_file():
            raise OSError("not a regular file")
        cap = FIELD_POLICY[("work_item", "body")]["cap"]
        with file.open("rb") as stream:
            raw = stream.read(cap + 1)
        if len(raw) > cap:
            raise CommandFailure("invalid_argument", "task body exceeds the allowed byte limit")
        if b"\x00" in raw:
            raise CommandFailure("invalid_argument", "task body must be text without null bytes")
        return raw.decode("utf-8")
    except (UnicodeError, ValueError) as exc:
        raise CommandFailure("invalid_argument", "task body must be UTF-8 text") from exc
    except OSError as exc:
        raise CommandFailure("invalid_argument", "task body file is unavailable") from exc


def _deadline(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        raise CommandFailure("invalid_argument", "--expected-by requires RFC3339 with a timezone")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "--expected-by is not a valid instant") from exc
    return value


def _inputs(args) -> dict:
    _request_id(args.request_id)
    if args.public_command == "task.admit":
        return {"title": _text(args.title, "--title"), "body": _body(args.body_file),
                "repo": _optional(args.repo, "--repo", r"[^/\s]+/[^/\s]+"),
                "project_key": _optional(args.project, "--project", r"[a-z][a-z0-9-]*"),
                "workstream_id": _optional(args.workstream, "--workstream", r"\S+"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    if args.public_command == "task.assign":
        return {"task_id": _reference(args.task_id, "TASK_ID"),
                "bot_id": _optional(args.bot, "--bot", r"[A-Za-z0-9_-]+"),
                "expected_by": _deadline(args.expected_by),
                "checkin_id": _optional(args.checkin, "--checkin", r"ck_[0-9a-f]{32}"),
                "by": _optional(args.by, "--by", r"(?:human:[^\s:/]+|bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+)")}
    return {"assignment_id": _reference(args.assignment_id, "ASSIGNMENT_ID")}


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     resolve_operation_scope, resolve_task_mutation_context)
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptBusy, ReceiptConflict, ReceiptError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from ..task_operations import TaskRecordingError, admit, assign, accept
    from ..task_queries import TaskQueryError
    from ..task_state import TaskStateError

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no task mutations")
        values = _inputs(args)
        selected, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if selected.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no task mutations")
        bound_release = None
        if origin is not None:
            bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
            if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
                raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
        root = selected.paths.root
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            ctx = resolve_task_mutation_context(root=root, fleet=selected.fleet.name,
                                                package=selected.paths.package)
            if args.public_command == "task.admit":
                result = admit(ctx, args.request_id, **values)
            elif args.public_command == "task.assign":
                result = assign(ctx, args.request_id, values.pop("task_id"), **values)
            else:
                result = accept(ctx, args.request_id, values["assignment_id"])
        outcome = "unchanged" if result.replayed else "committed"
        recording = "unchanged" if result.replayed else result.recording
        data = {"fleet": selected.fleet.name, "task_id": result.task_id,
                "assignment_id": result.assignment_id, "state": result.task.state,
                "outcome": outcome, "recording": recording,
                "delivery": result.delivery, "notification": result.notification,
                "replayed": result.replayed}
        lines = (f"{result.task_id}\t{result.assignment_id or '-'}\t{result.task.state}\t"
                 f"recording={recording}\tdelivery={result.delivery}\t"
                 f"notification={result.notification}",)
        return CommandOutput(data, release_id=release_id, lines=lines)
    except CommandFailure:
        raise
    except TaskRecordingError as exc:
        raise CommandFailure("unavailable", "task recording is unconfirmed; inspect the request before retrying",
                             data={"outcome": "unknown", "recording": "unknown"},
                             release_id=release_id) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint) from exc
    except (OperationContextUnavailableError, PendingMigrationError, DowngradeError,
            sqlite3.Error, OSError) as exc:
        raise CommandFailure("unavailable", "task identity or storage is unavailable",
                             retryable=True, release_id=release_id) from exc
    except (ReceiptConflict, ReceiptBusy) as exc:
        raise CommandFailure("conflict", "task request conflicts with recorded history or another caller",
                             release_id=release_id) from exc
    except ReceiptError as exc:
        raise CommandFailure("invalid_argument", "task request receipt is invalid",
                             release_id=release_id) from exc
    except TaskQueryError as exc:
        raise CommandFailure(exc.code, str(exc), hint=exc.hint,
                             retryable=exc.retryable, release_id=release_id) from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError,
            TaskStateError) as exc:
        raise CommandFailure("conflict", "active task scope or state is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid task root or fleet selector") from exc
