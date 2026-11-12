"""Public Plane ingest adapter; emit_api owns validation, commit, and spool policy."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from ..command_result import CommandFailure, CommandOutput
from ..context import resolve_paths
from ..plane.contracts import ContractViolation
from ..plane.emit_api import emit, emit_batch
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError
from ..plane.spool import SpoolWriteError


def _request(path: str):
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text()
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandFailure("invalid_argument", f"unreadable request: {exc}") from exc


def _object(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise CommandFailure("invalid_argument", f"{where} must be a JSON object")
    return value


def dispatch(args) -> CommandOutput:
    action = args.plane_action
    parsed = _request(args.file)
    if action == "emit":
        request = dict(_object(parsed, "request"))
        request["event_type"] = args.event_type
        requests = [request]
    else:
        requests = parsed.get("events") if isinstance(parsed, dict) else parsed
        if not isinstance(requests, list) or not requests:
            raise CommandFailure("invalid_argument", "batch requires a nonempty events array")
        requests = [_object(value, f"events[{i}]") for i, value in enumerate(requests)]

    try:
        root = resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed).root
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("invalid_argument", "invalid Plane root or fleet selector") from exc
    try:
        outcomes = ([emit(root, requests[0], require_commit=args.require_commit)]
                    if action == "emit" else
                    emit_batch(root, requests, require_commit=args.require_commit))
    except ContractViolation as exc:
        errors = getattr(exc, "errors", None)
        first = errors[0] if errors else str(exc)
        raise CommandFailure("invalid_argument", f"contract violation: {first}") from exc
    except SpoolWriteError as exc:
        raise CommandFailure("total_failure", f"TOTAL FAILURE: {exc}") from exc
    except PendingMigrationError as exc:
        raise CommandFailure("migration_required", f"REFUSED: {exc}") from exc
    except DowngradeError as exc:
        raise CommandFailure("downgrade", f"REFUSED: {exc}") from exc
    except (sqlite3.Error, OSError) as exc:
        # A require-commit failure may follow a lost acknowledgement. Never
        # promise that it did not commit or queue an automatic replay.
        raise CommandFailure("commit_unknown", "Plane commit outcome unknown; inspect event IDs before retrying") from exc

    data = {"outcomes": [{"event_id": o.event_id, "status": o.status,
                           "detail": o.detail} for o in outcomes]}
    if any(o.status == "spooled" for o in outcomes):
        raise CommandFailure(
            "spooled", "SPOOLED: durable on disk, pending Plane commit; do not resend",
            data=data)
    return CommandOutput(data, lines=tuple(o.event_id for o in outcomes))
