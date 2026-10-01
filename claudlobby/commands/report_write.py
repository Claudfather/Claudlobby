"""Public acknowledgement of the report prefix this generated viewer read."""

from __future__ import annotations

from dataclasses import asdict
import os
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (
        OperationContextError, OperationContextUnavailableError, bind_task_context,
        resolve_operation_scope,
    )
    from ..paths import InvalidPathSelector
    from ..plane.contracts import ContractViolation
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..report_operations import AckError, ack_reports
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from .message_write import _request_id

    release_id = None
    try:
        request_id = _request_id(args.request_id)
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no report read position")
        destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if (destination.paths.seed or origin is None or origin.bot_id is None
                or origin.fleet.name != destination.fleet.name):
            raise CommandFailure("conflict", "report ACK requires a generated viewer in its own fleet")
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID")
        if not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release):
            raise CommandFailure("release_mismatch", "generated caller lacks a bound release")
        with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            release_id = release.release_id
            selected = read_selection(destination.paths.root)
            if selected is None or selected["release_id"] != release_id:
                raise CommandFailure("release_mismatch", "report selection differs from this caller",
                                     release_id=release_id)
            ctx = bind_task_context(destination, origin=origin)
            result = ack_reports(ctx, selected, args.through, request_id=request_id)
        return CommandOutput(asdict(result), release_id=release_id,
                             lines=(f"Acknowledged {result.count} reports through ingest "
                                    f"{result.acked_through_seq} for {result.viewer}.",))
    except CommandFailure:
        raise
    except AckError as exc:
        raise CommandFailure(exc.code, str(exc),
                             data=asdict(exc.result) if exc.result is not None else {},
                             release_id=release_id) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from this caller",
                             hint=exc.hint, release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid report root or fleet selector",
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "report identity registry is unavailable",
                             release_id=release_id) from exc
    except (OperationContextError, BotNotFoundError, ActivationError, PlanError,
            ContractViolation) as exc:
        raise CommandFailure("conflict", "active report scope or recording contract is invalid",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "report recording or request state is unavailable",
                             release_id=release_id) from exc
