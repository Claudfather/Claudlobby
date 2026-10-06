"""Public read-only PR review evidence under the selected host release."""

from __future__ import annotations

from ..command_result import selection_read_conflict

from contextlib import closing
import re
import sqlite3
import subprocess

from ..command_result import CommandFailure, CommandOutput


def _read(args) -> CommandOutput:
    from ..active_config import ActivationError
    from ..activation_identity import read_selected_identity_bindings
    from ..activation_state import read_selection
    from ..config_plan import PlanError
    from ..context import BotNotFoundError
    from ..operation_context import (
        OperationContextError, OperationContextUnavailableError, resolve_operation_scope,
    )
    from ..paths import InvalidPathSelector
    from ..plane.db import connect_ro, db_file
    from ..plane.migrations import DowngradeError
    from ..plane.registry_read import current_entities
    from ..plane.schema_state import PendingMigrationError, require_current_schema
    from ..releases import ReleaseError
    from .. import review_queries, review_rules

    release_id = None
    try:
        if args.seed:
            raise CommandFailure("conflict", "seed configuration has no review evidence")
        if (not isinstance(args.repo, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)):
            raise CommandFailure("invalid_argument", "repository must be OWNER/REPO")
        if args.pr is not None and (type(args.pr) is not int or args.pr <= 0):
            raise CommandFailure("invalid_argument", "--pr requires a positive PR number")
        if type(args.limit) is not int or not 1 <= args.limit <= 1000:
            raise CommandFailure("invalid_argument", "--limit must be from 1 to 1000")
        destination, _origin = resolve_operation_scope(root=args.root, fleet=args.fleet)
        if destination.paths.seed:
            raise CommandFailure("conflict", "seed configuration has no review evidence")
        selected = read_selection(destination.paths.root)
        if selected is None:
            raise CommandFailure("conflict", "active review selection is unavailable")
        release_id = selected["release_id"]
        bindings = read_selected_identity_bindings(destination.paths.root, destination.fleet.name,
                                                   package=destination.paths.package)
        if (bindings["manager"] != destination.fleet.manager
                or set(bindings["bots"]) != set(destination.fleet.bots)):
            raise CommandFailure("conflict", "active caller fleet bindings differ from frozen scope",
                                 release_id=release_id)

        try:
            payloads = review_queries.fetch_payloads(args.repo, pr=args.pr, limit=args.limit)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            raise CommandFailure("unavailable", "GitHub review data is unavailable or incomplete",
                                 retryable=True, release_id=release_id) from exc
        # A single read-only snapshot gives all-host report rows and the Plane
        # epoch one consistent meaning. An unavailable Plane is not an empty
        # set of reviewers or a clean review state.
        with closing(connect_ro(db_file(destination.paths.root))) as conn:
            conn.execute("BEGIN")
            require_current_schema(conn)
            fleets = [row for row in current_entities(conn, entity_type="fleet")
                      if row["entity_alias"] == destination.fleet.name]
            if (len(fleets) != 1 or fleets[0]["host_uid"] != bindings["host_uid"]
                    or fleets[0]["entity_uid"] != bindings["fleet_uid"]):
                raise CommandFailure("conflict", "Plane host differs from active review scope",
                                     release_id=release_id)
            data = review_queries.assess_payloads(conn, payloads, args.repo)
        if read_selection(destination.paths.root) != selected:
            raise selection_read_conflict('active review selection changed during read', release_id=release_id)
        data.update({"caller_fleet": destination.fleet.name,
                     "host_uid": bindings["host_uid"],
                     "merge_authorization": False})
        lines = [f"Review attribution scans every fleet in this host's Plane; caller fleet: "
                 f"{destination.fleet.name}.",
                 "This read does not authorize a merge or prove PR authorship.",
                 review_rules.render(data["prs"], canonical=True)]
        for pr in data["attribution_events"]:
            for event in pr["events"]:
                actor = event["actor"] or ", ".join(
                    candidate["actor"] for candidate in event["candidates"])
                # The method tells an exact URL join from a timed window, and a
                # window used because the verdict's URL could not be read says why (#1537).
                method = event["method"] + (f": {event['fallback_reason']}"
                                            if event["fallback_reason"] else "")
                lines.append(f"  #{pr['number']} {event['event_id'][0]} "
                             f"{event['ts']}: {event['verdict']} {actor or event['reason']}"
                             f" [{method}]")
        return CommandOutput(data, release_id=release_id, lines=tuple(lines))
    except CommandFailure:
        raise
    except OperationContextUnavailableError as exc:
        raise CommandFailure("unavailable", "review identity registry is unavailable",
                             retryable=True, release_id=release_id) from exc
    except OperationContextError as exc:
        raise CommandFailure("conflict", "generated review caller conflicts with active scope",
                             release_id=release_id) from exc
    except BotNotFoundError as exc:
        raise CommandFailure("conflict", "generated review caller is not in the active fleet",
                             release_id=release_id) from exc
    except InvalidPathSelector as exc:
        raise CommandFailure("invalid_argument", "invalid review root or fleet selector",
                             release_id=release_id) from exc
    except ActivationError as exc:
        message = str(exc)
        if "executing package differ" in message or "selected release differ" in message:
            raise CommandFailure("release_mismatch", "selected release differs from this CLI") from exc
        if "unavailable" in message:
            raise CommandFailure("unavailable", "active review scope is unavailable",
                                 retryable=True, release_id=release_id) from exc
        raise CommandFailure("conflict", "active review scope is incomplete",
                             release_id=release_id) from exc
    except ReleaseError as exc:
        raise CommandFailure("release_mismatch", "selected release is unavailable or mismatched") from exc
    except PlanError as exc:
        raise CommandFailure("conflict", "active review configuration is incomplete") from exc
    except (PendingMigrationError, DowngradeError) as exc:
        raise CommandFailure("unavailable", "Plane schema is not current for this CLI",
                             release_id=release_id) from exc
    except (OSError, sqlite3.Error) as exc:
        raise CommandFailure("unavailable", "Plane review evidence is unavailable",
                             retryable=True, release_id=release_id) from exc


def dispatch(args) -> CommandOutput:
    return _read(args)
