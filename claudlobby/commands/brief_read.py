"""Read the selected fleet's schema-2 brief through the shared public result."""

from __future__ import annotations

from ..command_result import selection_read_conflict

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import shlex
import sqlite3

from ..command_result import CommandFailure, CommandOutput


@contextmanager
def _viewer_knobs(resolved, *, caller_bot: str | None, viewer: str):
    """Supply only the brief knobs; a different viewer cannot inherit caller knobs."""
    keys = ("WORKSTREAM_LEASE_DAYS", "DISPATCH_PROGRESS_GRACE_S",
            "DISPATCH_OVERDUE_MAX_AGE_S")
    missing = object()
    previous = {key: os.environ.get(key, missing) for key in keys}
    try:
        for key in keys:
            if caller_bot == viewer and key in os.environ:
                continue  # The generated viewer's live bot.conf already supplied it.
            if key in resolved:
                os.environ[key] = resolved[key].value
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def dispatch(args) -> CommandOutput:
    if args.boot and args.json:
        raise CommandFailure("invalid_argument", "--boot and --json cannot be combined")
    if args.boot and args.usage_since is not None:
        raise CommandFailure("invalid_argument", "--usage-since is for the full brief, not the boot payload")
    if args.seed:
        raise CommandFailure("conflict", "seed configuration has no operational brief")
    usage_window = None
    if args.usage_since is not None:
        from .usage_read import _window

        usage_window = _window(args.usage_since)

    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import ActivationError, read_selection
    from ..brief import (BriefIdentityMismatch, boot_provenance, build_brief,
                         format_boot_brief, format_brief)
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..env_tiers import ResolverUnavailable, resolve as resolve_env_tiers
    from ..operation_context import (OperationContextError, OperationContextUnavailableError,
                                     resolve_operation_scope)
    from ..paths import InvalidPathSelector
    from ..releases import ReleaseError

    release_id = None
    releases = "inspect claudlobby host releases"
    try:
        context, origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if context.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no operational brief")
        root = shlex.quote(str(context.paths.root))
        restage = (f"run claudlobby --root {root} --fleet {shlex.quote(context.fleet.name)} "
                   "config plan, then host activate the reviewed plan")
        selection = read_selection(context.paths.root)
        if selection is None:
            raise CommandFailure("conflict", "active brief selection is unavailable",
                                 hint=f"inspect claudlobby --root {root} host releases; pass this "
                                      f"host's --root, or {restage}")
        release_id = selection["release_id"]
        bindings = read_selected_identity_bindings(context.paths.root, context.fleet.name,
                                                   package=context.paths.package)
        if (bindings["manager"] != context.fleet.manager
                or set(bindings["bots"]) != set(context.fleet.bots)):
            raise CommandFailure("conflict", "active brief identities differ from frozen fleet",
                                 hint=f"fleet bots or manager changed after activation; {restage}",
                                 release_id=release_id)
        if origin is not None and (origin.fleet.name != context.fleet.name
                                   or origin.bot_id not in context.fleet.bots):
            raise CommandFailure("conflict", "generated brief caller differs from selected fleet",
                                 hint=f"caller is bot:{origin.fleet.name}/{origin.bot_id}; "
                                      "omit --fleet to read your own fleet's brief",
                                 release_id=release_id)
        if args.bot is not None:
            if args.bot not in context.fleet.bots:
                raise CommandFailure("not_found", "brief viewer is not in the active fleet",
                                     release_id=release_id)
            viewer, source = args.bot, "explicit"
        elif origin is not None:
            viewer, source = origin.bot_id, "generated"
        else:
            viewer, source = bindings["manager"], "manager_default"

        # The shell's env-tier resolver owns precedence. A generated caller's
        # own bot.conf remains authoritative only when reading its own view.
        resolved = resolve_env_tiers(context.paths, bot_name=viewer,
                                     fleet_name=context.fleet.name)
        now = int(datetime.now(timezone.utc).timestamp())
        selected_identity = (bindings["fleet_uid"], bindings["bots"][viewer])
        with _viewer_knobs(resolved, caller_bot=origin.bot_id if origin else None,
                           viewer=viewer):
            brief = build_brief(context.fleet, context.paths, viewer, now,
                                selected_identity=selected_identity)
            if usage_window is not None:
                from ..transcript_usage import collect_bot_usage, current_context

                since, until = usage_window
                brief["usage"] = collect_bot_usage(context.paths, context.fleet,
                                                   viewer, since, until)
                # #2206: the same reader's live context, the newest main-chain call.
                brief["context"] = current_context(context.paths, context.fleet, viewer)
            lines = ((format_boot_brief(brief, boot_provenance(
                context.paths, now, fleet_name=context.fleet.name, viewer=viewer,
                selected_identity=selected_identity)).rstrip("\n"),)
                     if args.boot else (f"Brief viewer: bot:{context.fleet.name}/{viewer} ({source})",
                                        format_brief(brief).rstrip("\n")))
        if read_selection(context.paths.root) != selection:
            raise selection_read_conflict('active brief selection changed during read', release_id=release_id)
        return CommandOutput({"brief": brief, "viewer_selection": source},
                             release_id=release_id, lines=lines)
    except CommandFailure:
        raise
    except BriefIdentityMismatch as exc:
        raise CommandFailure("conflict", "Plane brief identities differ from selected activation",
                             hint=releases, release_id=release_id) from exc
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "brief identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except (OperationContextError, BotNotFoundError) as exc:
        # Authored scope causes name selectors and identities, not config values.
        raise CommandFailure("conflict", "generated brief context conflicts with active fleet",
                             hint=f"{exc}; compare CLAUDLOBBY_ROOT, FLEET_NAME and BOT_ID with "
                                  f"the active fleet ({releases}); a bot added since activation "
                                  "needs config plan, then host activate",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid brief root or fleet selector",
                             hint=str(exc), release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI",
                                 hint=f"{message}; {releases} and run the selected release's CLI",
                                 release_id=release_id) from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active brief scope is unavailable",
                                 hint=f"{message}; {releases}",
                                 release_id=release_id) from exc
        raise CommandFailure("conflict", "active brief scope or identities are incomplete",
                             hint=f"{message}; {releases}",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected brief release is unavailable or mismatched",
                             hint=releases, release_id=release_id) from exc
    except PlanError as exc:
        # e.g. "select --fleet from the active fleets" on a multi-fleet host.
        raise CommandFailure("conflict", "active brief configuration is incomplete",
                             hint=f"{exc}; {releases}", release_id=release_id) from exc
    except ResolverUnavailable as exc:
        raise CommandFailure("unavailable", "brief runtime environment tiers are unavailable",
                             release_id=release_id) from exc
    except (OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "brief storage is unavailable",
                             release_id=release_id) from exc
    except ValueError as exc:
        # Includes a present-but-empty generated selector; config values stay private.
        raise CommandFailure("invalid_argument", "invalid generated brief selector",
                             hint="generated FLEET_NAME, CLAUDLOBBY_FLEET and BOT_ID must be single "
                                  "non-empty names; pass explicit --root and --fleet",
                             release_id=release_id) from exc
