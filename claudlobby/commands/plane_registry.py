"""Public registry read adapter over the Plane projection owner."""

from __future__ import annotations

from contextlib import closing
import sqlite3
import sys

import yaml

from ..command_result import CommandFailure, CommandOutput
from ..context import load_context, resolve_paths
from ..plane import registry_read as rr
from ..plane.db import connect_ro, db_file
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError, require_current_schema


_PLAN_HINT = ("Review the authored change with claudlobby config plan, then activate"
              " its reviewed plan with claudlobby host activate.")


def _key_pairs(keys):
    return [{"entity_type": entity_type, "entity_alias": alias}
            for entity_type, alias in keys]


def _verify(conn, paths) -> CommandOutput:
    from ..plane.registry_emit import _vault_rev, assemble_entities

    try:
        context = load_context(paths)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise CommandFailure("unavailable", "authored fleet configuration is unavailable",
                             hint=_PLAN_HINT) from exc
    fleet = context.fleet
    uid_file = paths.root / "state" / "host-uid"
    try:
        host_uid = uid_file.read_text().strip()
    except FileNotFoundError:
        host_uid = ""
    except OSError as exc:
        raise CommandFailure("unavailable", "recorded host identity is unreadable") from exc
    if not host_uid:
        raise CommandFailure("unavailable", "no recorded host identity; no registry scan can be verified")

    assembled, complete = assemble_entities(paths, fleet, _vault_rev(paths))
    report = rr.verify_current(conn, assembled, fleet=fleet.name, host_uid=host_uid)
    invalid = rr.invalid_tombstones(conn)
    data = {"mode": "verify", "comparison": "authored_config_vs_recorded_plane_projection",
            "proves_activation_or_runtime": False, "fleet": fleet.name,
            "host_uid": host_uid, "enumeration_complete": complete,
            "checked": report.checked, "drifted": _key_pairs(report.drifted),
            "missing_from_db": _key_pairs(report.missing_from_db),
            "missing_from_estate": _key_pairs(report.missing_from_estate),
            "trust": {"invalid_tombstones": len(invalid)}}
    if not complete or invalid or not report.ok:
        reasons = []
        if not complete:
            reasons.append("authored enumeration incomplete; comparison is partial")
        if invalid:
            reasons.append(f"{len(invalid)} unvalidated tombstone(s)")
        if not report.ok:
            reasons.append("recorded projection differs from authored configuration")
        raise CommandFailure("conflict", "registry verify: " + "; ".join(reasons),
                             data=data, hint=_PLAN_HINT)
    return CommandOutput(data, lines=(f"checked {report.checked} entities",
                                      "authored configuration matches the recorded Plane projection",
                                      "this comparison does not prove activation or runtime state"))


def dispatch(args) -> CommandOutput:
    try:
        paths = resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed)
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("invalid_argument", "invalid Plane root or fleet selector") from exc
    path = db_file(paths.root)
    if not path.exists():
        raise CommandFailure("unavailable", f"registry has no Plane database at {path}",
                             hint="Initialize the selected Plane through host setup and activation.")
    try:
        with closing(connect_ro(path)) as conn:
            require_current_schema(conn)
            if args.verify:
                return _verify(conn, paths)
            if args.history:
                rows = rr.entity_history(conn, args.history)
                if not rows:
                    raise CommandFailure("not_found", f"no registry history for {args.history!r}")
                lines = tuple(
                    f"{row['valid_from']} -> {row['valid_to'] or 'now'}  "
                    f"{'TOMBSTONE' if row['tombstone'] else (row['payload_hash'] or '')[:12]}"
                    f"  cause={row['cause']} scan={row['scan_id']}" for row in rows)
                return CommandOutput({"mode": "history", "identifier": args.history, "rows": rows},
                                     lines=lines)
            if args.changes is not None:
                changes = rr.recent_changes(conn, limit=args.changes)
                lines = []
                for change in changes:
                    lines.append(f"{change['occurred_at']}  {change['entity_type']}"
                                 f" {change['entity_alias']}  {change['change']}")
                    lines.extend(f"    {field}: {old!r} -> {new!r}"
                                 for field, (old, new) in sorted(change["fields"].items()))
                return CommandOutput({"mode": "changes", "changes": changes},
                                     lines=tuple(lines or ["no registry changes recorded yet"]))
            if args.show:
                rows = [row for row in rr.current_entities(conn)
                        if row["entity_alias"] == args.show or row["entity_uid"] == args.show]
                if not rows:
                    raise CommandFailure("not_found", f"{args.show!r} is not in the current registry",
                                         hint="Inspect its history with claudlobby plane registry --history ALIAS.")
                import json
                return CommandOutput({"mode": "show", "identifier": args.show, "rows": rows},
                                     lines=tuple(json.dumps(row, indent=2, ensure_ascii=False)
                                                 for row in rows))
            rows = rr.current_entities(conn, entity_type=args.type, fleet=args.scope_fleet)
            invalid = rr.invalid_tombstones(conn)
            warning = (f"[trust] {len(invalid)} tombstone(s) NOT honored — no complete"
                       " same-scan_id scan_completed (run plane doctor)" if invalid else None)
            if warning and not args.json:
                print(warning, file=sys.stderr)
            lines = tuple(f"{row['entity_type']:13} {row['entity_alias']:44}"
                          f" {(row['payload_hash'] or '')[:12]}  {row['occurred_at']}"
                          for row in rows)
            if not lines:
                lines = ("registry is empty for this filter (no completed scan, or nothing matches)",)
            return CommandOutput({"mode": "list", "filters": {"type": args.type,
                                  "scope": args.scope_fleet}, "rows": rows,
                                  "trust": {"invalid_tombstones": len(invalid),
                                            "warning": warning}}, lines=lines)
    except PendingMigrationError as exc:
        raise CommandFailure("migration_required", f"registry refused: {exc}") from exc
    except DowngradeError as exc:
        raise CommandFailure("downgrade", f"registry refused: {exc}") from exc
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise CommandFailure("unavailable", "registry projection is unreadable") from exc
