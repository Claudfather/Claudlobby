"""Public read-only view of one retained request and its independent fact proof."""

from __future__ import annotations

from ..command_result import selection_read_conflict

from dataclasses import asdict

from ..command_result import CommandFailure, CommandOutput


def _lines(view) -> tuple[str, ...]:
    lines = [f"{view.request_id}\t{view.operation}/v{view.operation_version}\t"
             f"observed_attempt={view.observed_attempt}"]
    for index, stage in enumerate(view.stages, 1):
        proof = stage.proof.status if stage.proof is not None else "not_applicable"
        lines.append(f"stage {index} {stage.kind}\trecorded={stage.recorded_status}\t"
                     f"recording_proof={proof}")
    for attempt in view.transmissions:
        transport = attempt.transport.status if attempt.transport is not None else "unobserved"
        proof = attempt.proof.status if attempt.proof is not None else "not_available"
        lines.append(f"transmission {attempt.attempt_no}\ttransport={transport}\t"
                     f"recorded={attempt.recorded_status}\trecording_proof={proof}")
    return tuple(lines)


def dispatch(args) -> CommandOutput:
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     resolve_operation_scope)
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..request_queries import RequestQueryError, read_request

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no retained request history")
        destination, _origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no retained request history")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active request selection is unavailable")
        release_id = selected["release_id"]
        bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                                   package=destination.paths.package)
        if (bindings["manager"] != destination.fleet.manager
                or set(bindings["bots"]) != set(destination.fleet.bots)):
            raise CommandFailure("conflict", "active request identity bindings differ from frozen fleet",
                                 release_id=release_id)
        view = read_request(destination.paths.root, bindings["fleet_uid"], args.request_id)
        if view.host_uid != bindings["host_uid"] or view.fleet_uid != bindings["fleet_uid"]:
            raise CommandFailure("conflict", "retained request belongs to another active host or fleet",
                                 release_id=release_id)
        if read_selection(destination.paths.root) != selected:
            raise selection_read_conflict('active request selection changed during read', release_id=release_id)
        return CommandOutput({"fleet": destination.fleet.name, "request": asdict(view)},
                             release_id=release_id, lines=_lines(view))
    except CommandFailure:
        raise
    except RequestQueryError as exc:
        raise CommandFailure(exc.code, str(exc), hint=getattr(exc, "hint", None),
                             retryable=exc.code == "unavailable", release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "request scope is unavailable",
                             retryable=True, release_id=release_id) from exc
    except OperationContextError as exc:
        raise CommandFailure("conflict", "generated request caller conflicts with active scope",
                             release_id=release_id) from exc
    except BotNotFoundError as exc:
        raise CommandFailure("conflict", "generated bot origin is not in the active fleet",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid request root or fleet selector",
                             release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI",
                                 release_id=release_id) from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active request scope is unavailable",
                                 retryable=True, release_id=release_id) from exc
        raise CommandFailure("conflict", "active request scope or identity binding is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched",
                             release_id=release_id) from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active request configuration is incomplete",
                             release_id=release_id) from exc
    except (OSError, ValueError) as exc:
        raise CommandFailure("unavailable", "retained request scope cannot be read",
                             retryable=True, release_id=release_id) from exc
