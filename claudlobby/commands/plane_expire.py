"""Selected host attention-expiry sweep over the canonical Plane producer."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import os
import sqlite3

from ..command_result import CommandFailure, CommandOutput
from ..context import resolve_paths
from ..plane.db import connect_ro, db_file
from ..plane.emit_api import emit_batch
from ..plane.expiry import (DEFAULT_AFTER_DAYS, ExpiryChanged, ExpiryPlan,
                            expirable, expired_events, require_expirable)
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError, require_current_schema
from ..runtime_admission import RuntimeIdentity, mutation_admission


def dispatch(args) -> CommandOutput:
    from ..activation_state import ActivationError, read_selection
    from ..config_plan import PlanError, read_plan

    if args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "plane expire is a host-wide selected-fleet sweep")
    days = args.after_days if args.after_days is not None else DEFAULT_AFTER_DAYS
    if days < 0:
        raise CommandFailure("invalid_argument", "expiry horizon cannot be negative")
    try:
        root = resolve_paths(root=args.root).root
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            selected = read_selection(root)
            if selected is None:
                raise ActivationError("active selection is unavailable")
            plan = read_plan(root, selected["plan_id"])
            if (plan.release_id != release.release_id
                    or plan.release_seal != release.seal_sha256 or not plan.fleets):
                raise PlanError("active plan differs from the admitted release")
            stamp = datetime.now(timezone.utc)
            path = db_file(root)
            if not path.exists():
                return CommandOutput({"dry_run": args.dry_run, "expired": 0,
                                      "fleets": list(plan.fleets), "recording": "not_needed"},
                                     release_id=release.release_id,
                                     lines=("attention: no Plane db — nothing to sweep",))
            with closing(connect_ro(path)) as conn:
                require_current_schema(conn)
                found = expirable(conn, now=stamp, after_days=days)
            fleets = set(plan.fleets)
            rows = [row for row in found.rows if row["fleet"] in fleets]
            outside = len(found.rows) - len(rows)
            scope = ExpiryPlan(found.cutoff, rows, found.unattributed)
            data = {"dry_run": args.dry_run, "candidates": len(rows),
                    "fleets": list(plan.fleets), "skipped_unselected": outside,
                    "skipped_unattributed": len(found.unattributed),
                    "cutoff": found.cutoff, "recording": "not_requested" if args.dry_run else "not_needed"}
            if args.dry_run:
                return CommandOutput(data, release_id=release.release_id,
                                     lines=(f"attention: would expire {len(rows)} assignment(s) overdue >{days}d",
                                            f"attention: skipped {outside} unselected and {len(found.unattributed)} unattributed assignment(s)"))
            if not rows:
                data["expired"] = 0
                return CommandOutput(data, release_id=release.release_id,
                                     lines=("attention: expired 0 assignment(s)",
                                            f"attention: skipped {outside} unselected and {len(found.unattributed)} unattributed assignment(s)"))
            events = expired_events(scope, now=stamp, after_days=days)
            try:
                outcomes = emit_batch(
                    root, events, require_commit=True,
                    precondition=lambda conn: require_expirable(
                        conn, scope, now=stamp, after_days=days))
            except ExpiryChanged as exc:
                raise CommandFailure("conflict", "expiry selection changed before commit; no expiry recorded",
                                     release_id=release.release_id) from exc
            except (sqlite3.Error, OSError) as exc:
                raise CommandFailure("commit_unknown", "expiry commit outcome unknown; inspect event IDs before retrying",
                                     data={**data, "event_ids": [event["event_id"] for event in events],
                                           "recording": "unknown"},
                                     release_id=release.release_id) from exc
            if any(outcome.status == "spooled" for outcome in outcomes):
                raise CommandFailure("spooled", "expiry events are pending Plane commit; no assignment is proved expired",
                                     data={**data, "event_ids": [event["event_id"] for event in events],
                                           "recording": "pending"},
                                     release_id=release.release_id)
            if len(outcomes) != len(events) or any(
                    outcome.status not in {"committed", "duplicate"} for outcome in outcomes):
                raise CommandFailure("commit_unknown", "expiry recording not proved; inspect event IDs before retrying",
                                     data={**data, "event_ids": [event["event_id"] for event in events],
                                           "recording": "unknown"},
                                     release_id=release.release_id)
            data["recording"] = "committed"
            data["expired"] = len(rows)
            data["event_ids"] = [outcome.event_id for outcome in outcomes]
            return CommandOutput(data, release_id=release.release_id,
                                 lines=(f"attention: expired {len(rows)} assignment(s) overdue >{days}d",
                                        f"attention: skipped {outside} unselected and {len(found.unattributed)} unattributed assignment(s)"))
    except (ActivationError, PlanError) as exc:
        raise CommandFailure("release_mismatch", f"expiry refused: {exc}") from exc
    except PendingMigrationError as exc:
        raise CommandFailure("migration_required", f"expiry refused: {exc}") from exc
    except DowngradeError as exc:
        raise CommandFailure("downgrade", f"expiry refused: {exc}") from exc
