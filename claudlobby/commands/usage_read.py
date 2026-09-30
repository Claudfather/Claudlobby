"""On-demand selected-fleet transcript usage; no quota inference."""

from __future__ import annotations

from ..command_result import selection_read_conflict

from datetime import datetime, timedelta, timezone
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput


_DURATION = re.compile(r"[1-9][0-9]{0,3}[mhd]\Z")


def _window(raw: str) -> tuple[datetime, datetime]:
    from .checkins import _since

    if not isinstance(raw, str) or not _DURATION.fullmatch(raw):
        raise CommandFailure("invalid_argument", "--since requires a positive duration such as 24h or 7d")
    end = datetime.now(timezone.utc)
    start = _since(raw)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise CommandFailure("invalid_argument", "--since must be within the past seven days")
    return start, end


def dispatch(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..activation_state import read_selection
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import OperationContextError, OperationContextUnavailableError
    from ..paths import InvalidPathSelector
    from ..plane.migrations import DowngradeError
    from ..plane.schema_state import PendingMigrationError
    from ..releases import ReleaseError
    from ..transcript_usage import collect_bot_usage, collect_fleet_usage
    from .checkin import _scope

    try:
        start, end = _window(args.since)
        destination, _origin, selected, _bindings = _scope(args)
        if args.public_command == "bot.usage":
            if args.bot_id not in destination.fleet.bots:
                raise CommandFailure("not_found", "bot is not declared in the selected fleet")
            row = collect_bot_usage(destination.paths, destination.fleet, args.bot_id, start, end)
            data = {"fleet": destination.fleet.name, **row}
            lines = (f"{args.bot_id} ({args.since}, {row['window']['since']} to "
                     f"{row['window']['until']}): {row['usage']['input_tokens']} input, "
                     f"{row['usage']['output_tokens']} output, "
                     f"{row['usage']['cache_creation_input_tokens']} cache-write, "
                     f"{row['usage']['cache_read_input_tokens']} cache-read tokens; "
                     f"turns={row['usage']['turns']}, "
                     f"models={','.join(row['usage']['models']) or '-'}; "
                     f"coverage={row['coverage']['status']}; "
                     f"files={row['coverage']['files_read']}, "
                     f"skipped>={row['coverage']['files_skipped_at_least']}; "
                     f"issues={','.join(row['coverage']['issues']) or '-'}; "
                     f"scan limits={row['scan_limits']}; "
                     f"shared account peers={','.join(row['shared_account_with_selected_fleet_bots']) or '-'}; "
                     f"quota=unavailable.",)
        else:
            data = collect_fleet_usage(destination.paths, destination.fleet, start, end)
            lines = (f"{destination.fleet.name} ({args.since}, {data['window']['since']} to "
                     f"{data['window']['until']}): {data['usage']['input_tokens']} input, "
                     f"{data['usage']['output_tokens']} output, "
                     f"{data['usage']['cache_creation_input_tokens']} cache-write, "
                     f"{data['usage']['cache_read_input_tokens']} cache-read tokens; "
                     f"turns={data['usage']['turns']}, "
                     f"models={','.join(data['usage']['models']) or '-'}; "
                     f"coverage={data['coverage']['status']} "
                     f"({data['coverage']['bots_observed']}/{data['coverage']['bots_total']} bots); "
                     f"quota=unavailable.",
                     *(f"{row['bot']}: coverage={row['coverage']['status']}; "
                       f"files={row['coverage']['files_read']}, "
                       f"skipped>={row['coverage']['files_skipped_at_least']}; "
                       f"issues={','.join(row['coverage']['issues']) or '-'}; "
                       f"scan limits={row['scan_limits']}; "
                       f"shared account peers={','.join(row['shared_account_with_selected_fleet_bots']) or '-'}"
                       for row in data['bots']))
        if read_selection(destination.paths.root) != selected:
            raise selection_read_conflict('active usage selection changed during read')
        if data["coverage"]["status"] == "unavailable":
            # Unreadable is not empty: no zero totals when nothing was observed.
            raise CommandFailure(
                "unavailable", "no transcript usage could be observed for the selected scope",
                data={**{key: value for key, value in data.items()
                         if key not in ("main", "sidechain")}, "usage": None},
                release_id=selected["release_id"])
        return CommandOutput(data, release_id=selected["release_id"], lines=lines)
    except CommandFailure:
        raise
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid usage root or fleet selector") from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "usage identity registry is unavailable") from exc
    except (ActivationError, PlanError, OperationContextError, BotNotFoundError) as exc:
        raise CommandFailure("conflict", "active usage scope is incomplete") from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected usage release is unavailable") from exc
    except (OSError, sqlite3.Error, PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "usage identity or transcript source is unavailable") from exc
