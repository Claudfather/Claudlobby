"""Public Plane spool and retention operations; kernel modules own the writes."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import os
import re
import sqlite3

from ..command_result import CommandFailure, CommandOutput
from ..activation_state import ActivationError
from ..context import resolve_paths
from ..plane.db import connect, connect_ro, db_file
from ..plane.ids import ensure_host_uid
from ..plane.migrations import DowngradeError
from ..plane.schema_state import PendingMigrationError, preflight_schema, require_current_schema
from ..plane.spool import drain, quarantine_dir, quarantine_entry, spool_dir, spool_entries
from ..plane.queue_paths import scan_spool, spool_path
from ..runtime_admission import RuntimeIdentity, mutation_admission


_SPOOL_NAME = re.compile(r"ev_[0-9a-f]{32}\.json")
# What else the quarantine holds (#2165): a staged batch or orphan stage that
# replay refused keeps its stage name, `<time_ns>-<lead event>[.batch.<pid>].json`.
# `inspect` reads these too; `quarantine` (a move) still takes spool names only.
_QUARANTINED_NAME = re.compile(r"(?:\d+-)?ev_[0-9a-f]{32}(?:\.batch\.\d+)?\.json")

QUARANTINE_LIST_LIMIT = 50
QUARANTINE_LIST_MAX = 500
_REASON_READ_BYTES = 4096
_REASON_SHOWN = 300           # as the trust panel cuts a reason


def _root(args):
    try:
        return resolve_paths(root=args.root, fleet=args.fleet, seed=args.seed).root
    except (OSError, ValueError, RuntimeError) as exc:
        raise CommandFailure("invalid_argument", "invalid Plane root or fleet selector") from exc


def _schema_failure(exc):
    if isinstance(exc, PendingMigrationError):
        return CommandFailure("migration_required", f"REFUSED: {exc}")
    return CommandFailure("downgrade", f"REFUSED: {exc}")


def _iso_ns(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quarantine_cursor(scope: dict, after: tuple[int, str]) -> str:
    raw = json.dumps({"v": 1, "scope": scope, "after": list(after)},
                     sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _quarantine_after(token: str | None, scope: dict) -> tuple[int, str] | None:
    if token is None:
        return None
    try:
        if not 1 <= len(token) <= 4096:
            raise ValueError
        value = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
        if (not isinstance(value, dict) or set(value) != {"v", "scope", "after"}
                or value["v"] != 1 or value["scope"] != scope):
            raise ValueError
        key = value["after"]
        if not isinstance(key, list) or len(key) != 2 or type(key[0]) is not int or not isinstance(key[1], str):
            raise ValueError
        return key[0], key[1]
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise CommandFailure("invalid_argument", "invalid quarantine cursor, or one from another root") from exc


def _reason(entry) -> tuple[str | None, str | None, bool]:
    """(the reason's first line, when it was written, readable). The sidecar
    `quarantine_entry` writes first, at the quarantine; absent is no reason."""
    sidecar = entry.with_name(entry.name + ".reason")
    try:
        fd = os.open(sidecar, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None, True
    except OSError:
        return None, None, False
    try:
        written = os.fstat(fd).st_mtime_ns
        raw = os.read(fd, _REASON_READ_BYTES)
    except OSError:
        return None, None, False
    finally:
        os.close(fd)
    lines = raw.decode("utf-8", "replace").strip().splitlines()
    return (lines[0].strip()[:_REASON_SHOWN] if lines else ""), _iso_ns(written), True


def _list_quarantined(root, args) -> CommandOutput:
    """Every quarantined entry, newest written first (#2165). Read-only: it lists
    and stats the quarantine through scan_spool and reads each reason sidecar,
    never the plane database or daemon. A quarantine that cannot be enumerated
    is unavailable, never an empty list; an entry gone between the listing and
    its stat is counted as vanished, not dropped silently."""
    limit = QUARANTINE_LIST_LIMIT if getattr(args, "limit", None) is None else args.limit
    if not 1 <= limit <= QUARANTINE_LIST_MAX:
        raise CommandFailure("invalid_argument", f"--limit must be 1-{QUARANTINE_LIST_MAX}")
    scan = scan_spool(root)
    if scan.quarantine_state == "unreadable":
        raise CommandFailure("unavailable", "Plane quarantine cannot be enumerated (a gap, not an empty list)")
    if not spool_path(root).exists() and not db_file(root).exists():
        raise CommandFailure("unavailable", "Plane storage is absent at this root")
    scope = {"root": str(root), "list": "quarantine"}
    after = _quarantine_after(getattr(args, "cursor", None), scope)
    rows, vanished = [], 0
    for path in scan.quarantined:
        try:
            st = path.lstat()
        except FileNotFoundError:
            vanished += 1
            continue
        except OSError as exc:
            raise CommandFailure("unavailable", "a quarantined entry cannot be read") from exc
        rows.append((st.st_mtime_ns, path.name, st.st_size, path))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if after is not None:
        rows = [row for row in rows if (row[0], row[1]) < after]
    page = rows[:limit]
    items, unreadable = [], 0
    for mtime_ns, name, size, path in page:
        reason, quarantined_at, readable = _reason(path)
        unreadable += not readable
        items.append({"name": name, "written_at": _iso_ns(mtime_ns), "quarantined_at": quarantined_at,
                      "size": size, "empty": size == 0, "reason": reason})
    next_cursor = _quarantine_cursor(scope, (page[-1][0], page[-1][1])) if len(rows) > limit else None
    coverage = {"state": "ok", "total": len(scan.quarantined), "returned": len(items),
                "vanished": vanished, "reasons_unreadable": unreadable}
    lines = tuple(f"{item['written_at']}  {'EMPTY' if item['empty'] else str(item['size']) + ' B':>9}"
                  f"  {item['name']}  {item['reason'] or '(no reason recorded)'}" for item in items)
    lines = lines or ("No quarantined entries.",)
    if next_cursor:
        lines += (f"next_cursor: {next_cursor}",)
    lines += (f"coverage: {len(items)} of {len(scan.quarantined)} listed, {vanished} vanished,"
              f" {unreadable} reason(s) unreadable",)
    return CommandOutput({"items": items, "next_cursor": next_cursor, "coverage": coverage}, lines=lines)


def spool(args) -> CommandOutput:
    root = _root(args)
    action = args.spool_action
    if action == "list" and getattr(args, "quarantined", False):
        return _list_quarantined(root, args)
    if getattr(args, "limit", None) is not None or getattr(args, "cursor", None) is not None:
        raise CommandFailure("invalid_argument", "--limit and --cursor page the quarantine: use list --quarantined")
    if action == "list":
        scan = scan_spool(root)
        if scan.spool_state == "unreadable" or scan.quarantine_state == "unreadable":
            raise CommandFailure("unavailable", "Plane spool cannot be enumerated")
        if not spool_path(root).exists() and not db_file(root).exists():
            raise CommandFailure("unavailable", "Plane storage is absent at this root")
        try:
            entries = spool_entries(root)
        except OSError as exc:
            raise CommandFailure("unavailable", "Plane spool cannot be read") from exc
        summary = [{"name": entry["_file"], "event_ids": entry.get("event_ids"),
                    "attempts": entry.get("attempts"), "spooled_at": entry.get("spooled_at")}
                   for entry in entries]
        return CommandOutput({"entries": summary}, lines=tuple(
            f"{entry['_file']}  events={entry.get('event_ids')}  attempts={entry.get('attempts')}"
            for entry in entries))
    if action in {"inspect", "quarantine"}:
        name = args.name or ""
        if not (_SPOOL_NAME.fullmatch(name)
                or action == "inspect" and _QUARANTINED_NAME.fullmatch(name)):
            raise CommandFailure("invalid_argument", "invalid spool entry name")
        if action == "inspect":
            scan = scan_spool(root)
            if scan.spool_state == "unreadable" or scan.quarantine_state == "unreadable":
                raise CommandFailure("unavailable", "Plane spool cannot be enumerated")
            if not spool_path(root).exists() and not db_file(root).exists():
                raise CommandFailure("unavailable", "Plane storage is absent at this root")
            source = spool_path(root) / name
            if not source.exists():
                source = spool_path(root) / "quarantine" / name
            if not source.exists():
                raise CommandFailure("not_found", f"no such spool entry: {name}")
            try:
                raw = source.read_bytes()
                # An empty entry is a stage that died before writing (#2164):
                # nothing to parse, which is the answer, not a read failure.
                entry = json.loads(raw) if raw else None
                reason_file = source.with_name(source.name + ".reason")
                reason = reason_file.read_text().strip() if reason_file.exists() else None
            except (OSError, json.JSONDecodeError) as exc:
                raise CommandFailure("unavailable", "spool entry is unreadable") from exc
            return CommandOutput({"name": name, "entry": entry, "empty": not raw, "quarantine_reason": reason},
                                 lines=tuple(filter(None, (f"quarantined: {reason}" if reason else "",
                                                     json.dumps(entry, indent=2, sort_keys=True, default=str)))))
        try:
            with mutation_admission(root, identity=RuntimeIdentity.current(),
                                    expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
                source = spool_dir(root) / name
                if not source.exists():
                    raise CommandFailure("not_found", f"no such spool entry: {name}")
                try:
                    quarantine_entry(root, source, "operator")
                except OSError as exc:
                    # The reason sidecar may already have been written. The
                    # move is unproved; inspect before any explicit retry.
                    raise CommandFailure("commit_unknown", "quarantine outcome unknown; inspect the entry before retrying",
                                         data={"name": name, "quarantined": "unknown"},
                                         release_id=release.release_id) from exc
                return CommandOutput({"name": name, "quarantined": True},
                                     release_id=release.release_id, lines=(f"quarantined {name}",))
        except ActivationError as exc:
            raise CommandFailure("release_mismatch", f"spool quarantine refused: {exc}") from exc
    if action != "retry":
        raise CommandFailure("invalid_argument", "unknown spool action")
    try:
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            preflight_schema(root)
            conn = connect(db_file(root))
            try:
                require_current_schema(conn)
                host = ensure_host_uid(root / "state")
                report = drain(root, conn, host)
            finally:
                conn.close()
    except ActivationError as exc:
        raise CommandFailure("release_mismatch", f"spool retry refused: {exc}") from exc
    except (PendingMigrationError, DowngradeError) as exc:
        raise _schema_failure(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        # Drain commits per entry. A prior entry may already be recorded.
        raise CommandFailure("commit_unknown", "spool drain outcome unknown; inspect spool and Plane before retrying",
                             data={"drain": "unknown"}) from exc
    data = {"ingested": report.ingested, "duplicates": report.duplicates,
            "quarantined": report.quarantined, "remaining": report.remaining,
            "attempted": report.attempted, "refused": list(report.refused),
            "limited": report.limited}
    line = (f"ingested={report.ingested} duplicates={report.duplicates}"
            f" quarantined={report.quarantined} remaining={report.remaining}")
    if report.quarantined or report.refused:
        raise CommandFailure("conflict", "spool drain quarantined or refused entries; inspect them",
                             data=data)
    if report.remaining or report.limited:
        raise CommandFailure("spooled", "spool entries remain pending; inspect before retrying", data=data)
    return CommandOutput(data, release_id=release.release_id, lines=(line,))


def prune(args) -> CommandOutput:
    from ..plane.retention import DEFAULT_RETENTION_DAYS

    root = _root(args)
    days = args.days if args.days is not None else DEFAULT_RETENTION_DAYS
    if days < 0 or days > 36500:
        raise CommandFailure("invalid_argument", "retention days must be between 0 and 36500")
    if args.dry_run:
        return _prune_under_scope(root, args, days, None)
    try:
        with mutation_admission(root, identity=RuntimeIdentity.current(),
                                expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
            return _prune_under_scope(root, args, days, release.release_id)
    except ActivationError as exc:
        raise CommandFailure("release_mismatch", f"prune refused: {exc}") from exc


def _prune_under_scope(root, args, days: int, release_id: str | None) -> CommandOutput:
    from ..plane.retention import (PRUNABLE_SYSTEM_EVENTS, prune_metric_samples,
                                   prune_system_events)

    path = db_file(root)
    if not path.exists():
        return CommandOutput({"dry_run": args.dry_run, "days": days, "db": "absent",
                              "metric_samples": 0, "system_events": None},
                             release_id=release_id,
                             lines=(f"prune: no plane db at {path} — nothing to age out",))
    system_on = os.environ.get("PLANE_PRUNE_SYSTEM_EVENTS_ENABLED", "").strip() == "1"
    try:
        preflight_schema(root)
        conn = connect_ro(path) if args.dry_run else connect(path)
        try:
            require_current_schema(conn)
            if not args.dry_run:
                # Both retention lanes and their watermarks form one verdict.
                conn.execute("BEGIN IMMEDIATE")
            try:
                res = prune_metric_samples(conn, days=days, dry_run=args.dry_run)
                system_count = None
                if system_on:
                    if args.dry_run:
                        marks = ",".join("?" for _ in PRUNABLE_SYSTEM_EVENTS)
                        system_count = conn.execute(
                            f"SELECT COUNT(*) FROM events WHERE kind='system'"
                            f" AND event IN ({marks}) AND ingested_at < ?",
                            (*sorted(PRUNABLE_SYSTEM_EVENTS), res.cutoff)).fetchone()[0]
                    else:
                        system_count = prune_system_events(conn, days=days)
                if not args.dry_run:
                    conn.commit()
            except Exception:
                if not args.dry_run:
                    conn.rollback()
                raise
        finally:
            conn.close()
    except (PendingMigrationError, DowngradeError) as exc:
        raise _schema_failure(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise CommandFailure("commit_unknown" if not args.dry_run else "unavailable",
                             "prune outcome unknown; inspect Plane before retrying" if not args.dry_run
                             else "Plane retention scan unavailable") from exc
    count = res.candidates if args.dry_run else res.deleted
    verb = "would delete" if args.dry_run else "deleted"
    lines = [f"metric_samples: {verb} {count} rows older than {days}d (cutoff {res.cutoff})"]
    if system_count is None:
        lines.append("system events: lane OFF — arm with PLANE_PRUNE_SYSTEM_EVENTS_ENABLED=1"
                     " in this host's .env (it deletes data; see claudlobby host doctor --switches)")
    else:
        lines.append(f"system events: {verb} {system_count} row(s) older than {days}d,"
                     f" of {sorted(PRUNABLE_SYSTEM_EVENTS)} — every other event type is kept")
    return CommandOutput({"dry_run": args.dry_run, "days": days, "cutoff": res.cutoff,
                          "metric_samples": count, "system_events": system_count,
                          "system_events_enabled": system_on},
                         release_id=release_id, lines=tuple(lines))
