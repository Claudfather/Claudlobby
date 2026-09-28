"""Selected bot automation status and narrow durable state mutations."""

from __future__ import annotations

import os
import re
from uuid import UUID

from ..command_result import CommandFailure, CommandOutput


def _request(value: str) -> str:
    try:
        if str(UUID(value)) == value:
            return value
    except (TypeError, ValueError, AttributeError):
        pass
    raise CommandFailure("invalid_argument", "--request-id requires a canonical UUID")


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..automation_state import AutomationStateError, mutate, pause_input, record_input, status
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     _local_operator_alias)
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch, RuntimeIdentity, mutation_admission
    from .checkin import _scope

    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no bot automation state")
        action = args.automation_action
        request_id = _request(args.request_id) if action != "status" else None
        destination, origin, selected, _bindings = _scope(args)
        bot = args.bot_id
        if bot not in destination.fleet.bots:
            raise CommandFailure("not_found", "bot is not in the selected fleet")
        config = destination.fleet.bots[bot].autonomous_runner
        if origin is not None and origin.fleet.name != destination.fleet.name:
            raise CommandFailure("conflict", "generated automation caller belongs to another fleet")
        if action == "status":
            result = status(destination.paths.root, destination.fleet.name, bot,
                            configured=config is not None)
            if read_selection(destination.paths.root) != selected:
                raise CommandFailure("conflict", "active automation selection changed during read")
            return CommandOutput(result, release_id=selected["release_id"],
                                 lines=(f"{bot}: eligible={result['eligible']}; "
                                        f"reason={result['ineligible_reason'] or '-'}; "
                                        f"runs={result['runs_recorded']}",))
        if config is None:
            raise CommandFailure("conflict", "bot has no autonomous runner configuration")
        if origin is not None and (origin.bot_id != bot and
                                   (action == "record" or origin.bot_id != destination.fleet.manager)):
            raise CommandFailure("conflict", "generated automation caller cannot update this bot")
        payload = (pause_input(args.reason) if action == "pause" else
                   record_input(args.outcome, args.pr, args.issue, config.target_repo)
                   if action == "record" else {})
        actor = (f"bot:{destination.fleet.name}/{origin.bot_id}" if origin is not None
                 else _local_operator_alias())
        bound_release = os.environ.get("CLAUDLOBBY_RELEASE_ID") if origin is not None else None
        if origin is not None and (not bound_release or not re.fullmatch(r"r-[0-9a-f]{64}", bound_release)):
            raise CommandFailure("release_mismatch", "generated automation caller lacks a bound release")
        with mutation_admission(destination.paths.root, identity=RuntimeIdentity.current(),
                                expected_release=bound_release) as release:
            if release.release_id != selected["release_id"] or read_selection(destination.paths.root) != selected:
                raise CommandFailure("release_mismatch", "selected automation release changed")
            result = mutate(destination.paths.root, destination.fleet.name, bot, action,
                            actor=actor, request_id=request_id, payload=payload,
                            target_repo=config.target_repo)
        return CommandOutput({"fleet": destination.fleet.name, "bot": bot, **result,
                              "request_persisted": True}, release_id=selected["release_id"],
                             lines=(f"{bot}: {action}; recording={result['recording']}; "
                                    f"eligible={result['state']['eligible']}",))
    except CommandFailure:
        raise
    except AutomationStateError as exc:
        raise CommandFailure(exc.code, str(exc)) from exc
    except ReleaseMismatch as exc:
        raise CommandFailure("release_mismatch", "selected release differs from automation caller",
                             hint=exc.hint) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid automation root or fleet selector") from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "automation identity registry is unavailable") from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError) as exc:
        raise CommandFailure("conflict", "active automation scope is incomplete") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected automation release is unavailable") from exc
