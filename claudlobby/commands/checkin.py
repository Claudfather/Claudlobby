"""Public check-in reads, one committed decision, and offline selection checks."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


def _text_file(name: str, *, cap: int) -> str:
    path = Path(name).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file")
            raw = stream.read(cap + 1)
        if len(raw) > cap or b"\x00" in raw:
            raise ValueError("file exceeds its bound or is not text")
        return raw.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise CommandFailure("invalid_argument", "check-in input must be a readable bounded UTF-8 file") from exc


def _json_file(name: str, *, cap: int = 1_048_576) -> dict:
    def _constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        value = json.loads(_text_file(name, cap=cap), parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise CommandFailure("invalid_argument", "check-in input must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise CommandFailure("invalid_argument", "check-in input must be a JSON object")
    return value


def _request(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


def _offline(args) -> CommandOutput:
    from ..checkin_selection import DEFECT, INVALID, parse_focus_refs, verify_record

    if args.public_command == "checkin.selection.focus-refs":
        refs = parse_focus_refs(_text_file(args.file, cap=1_048_576))
        return CommandOutput({"refs": refs}, lines=tuple("#" + ref for ref in refs))
    record = _json_file(args.file)
    states = _json_file(args.issue_states) if args.issue_states else None
    try:
        verdict, findings = verify_record(record, states)
    except (TypeError, AttributeError, ValueError) as exc:
        raise CommandFailure("invalid_argument", "selection evidence has invalid structure") from exc
    data = {"verdict": verdict, "findings": findings,
            "unknown_count": sum(item.startswith("UNKNOWN") for item in findings)}
    if verdict in (DEFECT, INVALID):
        raise CommandFailure("selection_defect" if verdict == DEFECT else "invalid_argument",
                             "selection evidence is defective" if verdict == DEFECT else
                             "selection evidence is invalid", data=data)
    return CommandOutput(data, lines=(f"{verdict} (unknown={data['unknown_count']})", *findings))


def _scope(args):
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import read_selection
    from ..operation_context import resolve_operation_scope

    if args.seed:
        raise CommandFailure("conflict", "seed configuration has no active check-in selection")
    destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
    if destination.paths.seed:
        raise CommandFailure("conflict", "seed configuration has no active check-in selection")
    selected = read_selection(destination.paths.root)
    if selected is None:
        raise CommandFailure("conflict", "active check-in selection is unavailable")
    bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                               package=destination.paths.package)
    if (bindings["manager"] != destination.fleet.manager
            or set(bindings["bots"]) != set(destination.fleet.bots)
            or read_selection(destination.paths.root) != selected):
        raise CommandFailure("conflict", "active check-in scope changed or differs from frozen bindings")
    return destination, origin, selected, bindings


def _read(args) -> CommandOutput:
    from ..activation_state import read_selection
    from ..commands.checkins import _since, collect_checkins, summarize
    from ..plane.db import connect_ro, db_file
    from ..plane.schema_state import require_current_schema
    from ..paths import load_lib_module
    from ..workstreams import blocked_waits
    from datetime import datetime, timezone

    destination, _origin, selected, bindings = _scope(args)
    if args.public_command == "checkin.list":
        if args.bot is not None and args.bot not in destination.fleet.bots:
            raise CommandFailure("not_found", "bot is not in the active fleet")
        if args.limit is not None and args.limit <= 0:
            raise CommandFailure("invalid_argument", "--limit must be positive")
        if args.summary and (args.last or args.limit is not None):
            raise CommandFailure("invalid_argument", "--summary requires an untruncated time window")
        try:
            since = None if args.last else _since(args.since)
        except ValueError as exc:
            raise CommandFailure("invalid_argument", str(exc)) from exc
    else:
        if not isinstance(args.checkin_id, str) or not re.fullmatch(r"ck_[0-9a-f]{32}", args.checkin_id):
            raise CommandFailure("invalid_argument", "check-in ID must have canonical ck_<32hex> form")
    with closing(connect_ro(db_file(destination.paths.root))) as conn:
        conn.execute("BEGIN")
        require_current_schema(conn)
        fleet = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias=?",
                             (destination.fleet.name,)).fetchone()
        if fleet is None or fleet[0] != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "Plane fleet differs from active check-in scope")
        if args.public_command == "checkin.list":
            rows = collect_checkins(conn, destination.fleet.name, since=since, bot=args.bot,
                                    last=args.last, raised=args.raised, limit=args.limit)
        else:
            rows = collect_checkins(conn, destination.fleet.name, since=None, bot=None,
                                    last=False, checkin_id=args.checkin_id, limit=1)
        readers = load_lib_module(destination.paths.lib, "plane-readers.py")
        if readers is None or not hasattr(readers, "workstream_registry"):
            raise CommandFailure("unavailable", "Plane workstream reader is unavailable")
        waits = blocked_waits(readers.workstream_registry(
            conn, destination.fleet.name,
            lease_days=destination.fleet.workstreams.lease_days)["workstreams"],
            int(datetime.now(timezone.utc).timestamp()))
    if read_selection(destination.paths.root) != selected:
        raise CommandFailure("conflict", "active check-in selection changed during read")
    if args.public_command == "checkin.show":
        if not rows:
            raise CommandFailure("not_found", "check-in decision is not in the selected fleet")
        row = rows[0]
        return CommandOutput({"fleet": destination.fleet.name, "decision": row,
                              "current_workstream_waits": waits},
                             release_id=selected["release_id"],
                             lines=(f"{row['checkin_id']}\t{row['actor']}\t{row['action']}",))
    data = {"fleet": destination.fleet.name, "bot": args.bot,
            "since": None if args.last else since.isoformat(), "limit": args.limit,
            "items": rows, "current_workstream_waits": waits,
            "wait_observation": "declared workstream waits only; ordinary questions are not covered"}
    if args.summary:
        data["summary"] = summarize(rows)
    lines = tuple(f"{row['checkin_id'] or '-'}\t{row['actor']}\t{row['action'] or 'unknown'}"
                  for row in rows)
    if args.summary:
        totals = data["summary"]["totals"]
        lines = (f"{totals['checkins']} check-ins; {totals['raised']} raised; "
                 f"{totals['no_record']} unreadable records",)
    if waits:
        lines += tuple(f"workstream wait {item['id']} on={item['waiting_on'] or 'unknown'} "
                       f"age_seconds={item['age_seconds'] if item['age_seconds'] is not None else 'unknown'} "
                       f"note={item['note'] or ''}" for item in waits)
    return CommandOutput(data, release_id=selected["release_id"], lines=lines)


def _record(args) -> CommandOutput:
    from ..activation_state import read_selection
    from ..checkin_operations import record_checkin, validate_content
    from ..operation_context import resolve_task_mutation_context
    from ..plane.ids import derive_uid
    from ..runtime_admission import RuntimeIdentity, mutation_admission

    request_id = _request(args.request_id)
    decision = _json_file(args.file, cap=16_384)
    selection_file = _json_file(args.selection_file, cap=16_384) if args.selection_file else None
    destination, origin, selected, bindings = _scope(args)
    if origin is not None and (origin.fleet.name != destination.fleet.name
                               or origin.bot_id != destination.fleet.manager):
        raise CommandFailure("conflict", "generated check-in caller must be the selected fleet manager")
    checkin_id = derive_uid("ck", f"checkin:v1:{bindings['host_uid']}:{bindings['fleet_uid']}:{request_id}")
    validate_content(decision, selection_file, checkin_id=checkin_id)
    if args.dry_run:
        return CommandOutput({"validated": True, "checkin_id": None, "recording": "not_attempted"},
                             release_id=selected["release_id"],
                             lines=("Check-in input valid; nothing recorded.",))
    bound_release = None
    if origin is not None:
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
        if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
            raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
    with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                            expected_release=bound_release) as release:
        if release.release_id != selected["release_id"] or read_selection(destination.paths.root) != selected:
            raise CommandFailure("release_mismatch", "selected check-in release changed")
        ctx = resolve_task_mutation_context(root=destination.paths.root,
                                            fleet=destination.fleet.name,
                                            package=destination.paths.package)
        if ctx.host_uid != bindings["host_uid"] or ctx.fleet_uid != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "check-in mutation identity differs from active selection")
        result = record_checkin(ctx, selected, decision, selection_file, request_id=request_id)
    data = {"fleet": destination.fleet.name, **asdict(result)}
    return CommandOutput(data, release_id=release.release_id,
                         lines=(f"{result.checkin_id}\t{result.actor}\t"
                                f"recording={result.recording}\treplayed={result.replayed}",))


def dispatch(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..checkin_contract import ContractError
    from ..checkin_operations import CheckinError
    from ..checkin_selection import RecordError
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import OperationContextError, OperationContextUnavailableError
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..request_receipts import ReceiptError
    from ..runtime_admission import ReleaseMismatch
    from ..task_state import TaskStateError

    if args.public_command.startswith("checkin.selection."):
        return _offline(args)
    try:
        return _record(args) if args.public_command == "checkin.record" else _read(args)
    except CommandFailure:
        raise
    except ContractError as exc:
        raise CommandFailure("invalid_argument", "invalid check-in decision: " + "; ".join(exc.reasons[:4])) from exc
    except RecordError as exc:
        raise CommandFailure("invalid_argument", "invalid selection evidence") from exc
    except CheckinError as exc:
        raise CommandFailure(exc.code, str(exc),
                             data=asdict(exc.result) if exc.result is not None else {}) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid check-in root or fleet selector") from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "check-in identity registry is unavailable") from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError,
            TaskStateError) as exc:
        raise CommandFailure("conflict", "active check-in scope or history is incomplete") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except (PendingMigrationError, DowngradeError, ReceiptError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "check-in Plane or request state is unavailable") from exc
