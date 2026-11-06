"""Public acknowledgement of the report prefix this generated viewer read."""

from __future__ import annotations

from dataclasses import asdict
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..active_config import runtime_read_stamp
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
    from ..runtime_admission import (RuntimeIdentity, mutation_admission,
                                     runtime_release_id, runtime_route_matches)
    from .message_write import _request_id, _bound_release

    release_id = None
    provenance = {}
    try:
        request_id = _request_id(args.request_id)
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no report read position")
        destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if (destination.paths.seed or origin is None or origin.bot_id is None
                or origin.fleet.name != destination.fleet.name):
            raise CommandFailure("conflict", "report ACK requires a generated viewer in its own fleet")
        bound_release = _bound_release(destination.paths.package, generated=True)
        with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            selected = runtime_read_stamp(destination.paths.root, package=destination.paths.package)
            release_id = runtime_release_id(release)
            provenance = selected.provenance if selected is not None else {}
            if selected is None or not runtime_route_matches(release, selected, package=destination.paths.package):
                raise CommandFailure("release_mismatch", "report selection differs from this caller",
                                     release_id=release_id)
            ctx = bind_task_context(destination, origin=origin)
            result = ack_reports(ctx, selected, args.through, request_id=request_id)
        return CommandOutput({**asdict(result), **selected.provenance}, release_id=release_id,
                             lines=(f"Acknowledged {result.count} reports through ingest "
                                    f"{result.acked_through_seq} for {result.viewer}.",))
    except CommandFailure:
        raise
    except AckError as exc:
        raise CommandFailure(exc.code, str(exc),
                             data={**(asdict(exc.result) if exc.result is not None else {}), **provenance},
                             release_id=release_id) from exc
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
