"""Public selected-fleet alert and notice."""

from ..command_result import CommandFailure, CommandOutput


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError
    from ..fleet_notification import (FleetNotificationError, FleetNotificationInputError,
                                      notify_fleet)
    from ..operation_context import OperationContextError
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError
    from ..runtime_admission import ReleaseMismatch

    if args.seed:
        raise CommandFailure("conflict", "fleet notification requires an active host")
    try:
        result = notify_fleet(root=args.root, fleet=args.fleet, level=args.level,
                              event=args.event, message=args.message)
    except FleetNotificationInputError as exc:
        raise CommandFailure("invalid_argument", str(exc)) from exc
    except FleetNotificationError as exc:
        raise CommandFailure("conflict", str(exc)) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid root or fleet selector") from exc
    except (ReleaseMismatch, ReleaseError) as exc:
        raise CommandFailure("release_mismatch", "fleet notification requires the selected release") from exc
    except (ActivationError, OperationContextError) as exc:
        raise CommandFailure("conflict", "selected fleet notification scope is unavailable") from exc
    data = result.data()
    reached = [name for name in ("manager", "telegram")
               if data[name]["status"] in {"submitted", "carrier_accepted", "suppressed"}]
    if result.recording != "committed":
        raise CommandFailure("recording_degraded", "fleet notification recording is unverified; "
                             "inspect channel outcomes before retrying",
                             data=data, release_id=result.release_id)
    if not reached:
        raise CommandFailure("notification_failed", "fleet notification has no confirmed channel submission; inspect outcomes before retrying",
                             data=data, release_id=result.release_id)
    return CommandOutput(data, release_id=result.release_id,
                         lines=(f"{result.fleet}: {result.level} recording committed; "
                                f"manager={result.manager.status}, telegram={result.telegram.status}.",))
