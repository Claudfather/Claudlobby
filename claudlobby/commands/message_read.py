"""Public message observations over the existing read-only query owner."""

from __future__ import annotations

from dataclasses import asdict
import sqlite3

from ..command_result import CommandFailure, CommandOutput


def _receipt_data(observation) -> dict:
    return {"message_id": observation.message_id, "root": observation.root,
            "sender": asdict(observation.sender) if observation.sender else None,
            "destination": asdict(observation.destination) if observation.destination else None,
            "receipt_observation": observation.receipt_observation,
            "integrity_verdict": observation.integrity_verdict}


def _read(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..message_queries import MessageQueryError, receipt, show_message, wait_for_reply
    from ..operation_context import (
        OperationContextError, OperationContextUnavailableError, _MissingHumanIdentity,
        _local_operator_alias, bind_task_context, resolve_operation_scope,
    )
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no operational message history")
        destination, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no operational message history")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active message selection is unavailable")
        release_id = selected["release_id"]
        caller_alias = None if origin is not None else _local_operator_alias()
        ctx = bind_task_context(destination, origin=origin, operator_alias=caller_alias)
        failure = None

        if args.public_command == "message.show":
            message = show_message(ctx, args.message_id)
            data = {"message": asdict(message)}
            header = (f"{message.message_id}\t{message.sender.alias}\t"
                      f"{message.destination.alias if message.destination else '-'}\t{message.content}")
            if message.content == "withheld":
                lines = (header, "[content withheld]")
            elif message.content == "partial":
                lines = (header, message.body or "", "[content partial]")
            else:
                lines = (header, message.body if message.body is not None else "[content unavailable]")
        elif args.public_command == "message.receipt":
            observation = receipt(ctx, args.message_id, destination=args.destination, wait=args.wait)
            data = _receipt_data(observation)
            if observation.code is not None:
                failure = (observation.code, observation.reason or "message receipt is incomplete")
            lines = (f"{observation.message_id}\t{observation.receipt_observation}\t"
                     f"{observation.integrity_verdict}",)
        else:
            observation = wait_for_reply(ctx, args.message_id, timeout=args.timeout)
            data = {"message_id": observation.message_id,
                    "reply": asdict(observation.reply) if observation.reply else None}
            if observation.code is not None:
                failure = (observation.code, observation.reason or "direct reply is unavailable")
            lines = ((f"{observation.message_id}\t{observation.reply.message_id}",)
                     if observation.reply else ())
        if read_selection(destination.paths.root) != selected:
            raise CommandFailure("conflict", "active message selection changed during read",
                                 release_id=release_id)
        if failure is not None:
            raise CommandFailure(*failure, data=data, release_id=release_id)
        return CommandOutput(data, release_id=release_id, lines=lines)
    except CommandFailure:
        raise
    except _MissingHumanIdentity as exc:
        raise CommandFailure("unavailable", "local operator identity is not recorded; message reads do not register it",
                             release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "message identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except OperationContextError as exc:
        raise CommandFailure("conflict", "generated message caller context conflicts with active scope",
                             release_id=release_id) from exc
    except MessageQueryError as exc:
        raise CommandFailure(exc.code, str(exc), retryable=exc.retryable,
                             release_id=release_id) from exc
    except BotNotFoundError as exc:
        raise CommandFailure("conflict", "generated bot origin is not in the active fleet",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid message root or fleet selector",
                             release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI") from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active message scope is unavailable",
                                 retryable=True, release_id=release_id) from exc
        raise CommandFailure("conflict", "active message scope is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active message configuration is incomplete",
                             release_id=release_id) from exc
    except (PendingMigrationError, DowngradeError, OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "Plane message storage is unavailable",
                             retryable=True, release_id=release_id) from exc
    except ValueError as exc:
        raise CommandFailure("invalid_argument", "invalid generated message selector",
                             release_id=release_id) from exc


def dispatch(args) -> CommandOutput:
    return _read(args)
