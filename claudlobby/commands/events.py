"""Fleet event reads from the Plane, including the canonical public event door.

Every fleet event lands on the plane as a system event anchored on the bot's
actor (or the fleet, or the host) with a ``fleet-events:`` provenance and a
detail carrying ``{source, legacy_ts, data}`` (F18 R1: the plane is the only
recorder). The stdlib readers the bash doors ship (``lib/plane-readers.py``)
render each one back as the row the retired ledgers used to hold. Private
legacy rows keep that shape through one renderer shared with
``plane-lookup.py --events`` and fleet-pulse. ``--critical`` is
the severity the registry stamped at ingest (``SYSTEM_EVENT_SEVERITY``), one
definition; ``CRITICAL_TYPES`` below is the file-era vocabulary, kept so the
registry is pinned to agree with it.

There is no file to read (F18 closure, R2b): R1 removed every writer, and a
reader that could still open one would read nothing at best and the archive
at worst. There is no flag and no declaration either — the plane is the only
source. An unreachable plane returns a schema-1 unavailable result: "No events
found." from an instrument that could not be reached is a claim about the
estate drawn from nothing, the #1216 class. A reachable plane holding no
event for the fleet prints that line honestly.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3

from ..command_result import CommandFailure, CommandOutput

# Critical event types — fleet health problems that need attention. The
# file-era hand list; the plane path filters on the registry's severity, and
# tests pin that every name here is registered critical.
CRITICAL_TYPES = {
    "session_missing",
    "service_down",
    "activity_stuck",
    "script_error",
    "overdue_dispatch",
    "bridge_down",
    "reload_failed",
    "restart_failed",
    "rc_timeout",
    "crash_loop",
}


def plane_events_conn(paths):
    """(conn, note): an OPEN read-only plane connection, or ``(None, why)``
    when the plane cannot answer — ``brief.plane_session``'s rule (no fleet
    named, no db, an unopenable or schema-less db, a fleet the plane holds no
    bot of; no flag, no declaration — F18 closure, R2b). The caller closes."""
    from ..brief import plane_session
    plane, note = plane_session(paths)
    if plane is None:
        return None, note
    return plane.conn, None


def collect_plane_events(conn, paths, *, fleet=None, pr=None, bot=None, event_type=None,
                         source=None, critical_only=False, since=None) -> list[dict]:
    """The fleet's events from the plane as legacy rows — the same filters and
    row shape the retired files had, through the stdlib readers the bash
    doors ship (one row rendering; critical = the severity the registry
    stamped at ingest, one definition). A plane that cannot answer (no
    identity for the fleet, a db error) raises RuntimeError — the caller's
    refusal."""
    from ..brief import resolve_fleet_name
    from ..paths import load_lib_module
    pr = pr or load_lib_module(paths.lib, "plane-readers.py")
    if pr is None:
        raise RuntimeError(f"lib/plane-readers.py is not readable under {paths.lib}")
    try:
        rows = pr.fleet_events(conn, fleet or resolve_fleet_name(paths), since=since, bot=bot,
                               event_type=event_type)
    except (pr.PlaneUnreachable, sqlite3.Error, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    return [pr.public(r) for r in rows
            if (not source or r.get("source") == source)
            and (not critical_only or r.get("_severity") == "critical")]


def format_event_table(events: list[dict]) -> str:
    """Format events as a human-readable table."""
    if not events:
        return "No events found."

    lines = []
    header = f"{'TIME':<22} {'BOT':<10} {'TYPE':<20} {'SOURCE':<10} {'DETAIL'}"
    lines.append(header)
    lines.append("-" * len(header))

    for ev in events:
        ts = ev.get("ts", "?")
        if len(ts) > 19:
            ts = ts[:19]
        bot_name = ev.get("bot", "?")
        ev_type = ev.get("type", "?")
        ev_source = ev.get("source", "?")
        data = ev.get("data", {})

        detail_parts = []
        for k, v in data.items():
            # actor/reason carry a teardown's who and why. Without them the row
            # falls back to raw JSON and is truncated mid-field, so the one
            # question a teardown record exists to answer goes unanswered.
            if k in (
                "session",
                "unit",
                "tool",
                "script",
                "state",
                "detail",
                "event",
                "actor",
                "reason",
            ):
                detail_parts.append(f"{k}={v}")
        detail = ", ".join(detail_parts) or json.dumps(data, separators=(",", ":"))
        if len(detail) > 60:
            detail = detail[:57] + "..."

        lines.append(f"{ts:<22} {bot_name:<10} {ev_type:<20} {ev_source:<10} {detail}")

    return "\n".join(lines)


def _events_coverage_line(conn, paths, since) -> str:
    """#1658. `events` takes `--since` and rendered the answer as if the window
    were covered; on a plane younger than the window that is an artifact of the
    record's age. See `plane-readers.coverage_line` for why this states rather
    than refuses -- refusal here is already reserved for an UNREACHABLE plane
    (rc 3 above), and under-covered is not unreachable."""
    try:
        from ..paths import load_lib_module
        pr = load_lib_module(paths.lib, "plane-readers.py")
        window_s = _window_seconds(since)
        first, last, rows = pr.coverage(conn, None)
        return pr.coverage_line(first, last, rows, window_s)
    except AttributeError:
        return ("coverage: unknown — the readers installed at this root predate"
                " the coverage derivation (#1658)")
    except Exception as exc:                       # pragma: no cover - defensive
        return f"coverage: unknown — {exc}"


def _window_seconds(since) -> float | None:
    """`--since` as seconds, or None when the caller named no window (then the
    answer spans whatever exists and there is nothing to fall short of)."""
    if not since:
        return None
    raw = str(since).strip()
    mult = {"h": 3600.0, "d": 86400.0, "m": 60.0}.get(raw[-1:])
    if mult is not None:
        try:
            return float(raw[:-1]) * mult
        except ValueError:
            return None
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - t).total_seconds(), 0.0)
    except ValueError:
        return None


def _cursor(scope: dict, after: tuple[str, int], since_instant: str | None) -> str:
    raw = json.dumps({"v": 1, "scope": scope, "after": after, "since_instant": since_instant},
                     sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _after(token: str | None, scope: dict) -> tuple[tuple[str, int], str | None] | None:
    if token is None:
        return None
    try:
        if not 1 <= len(token) <= 4096:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"v", "scope", "after", "since_instant"}
                or type(value["v"]) is not int or value["v"] != 1
                or value["scope"] != scope):
            raise ValueError
        key = value["after"]
        if (not isinstance(key, list) or len(key) != 2 or not isinstance(key[0], str)
                or type(key[1]) is not int or key[1] < 1
                or value["since_instant"] is not None
                and not isinstance(value["since_instant"], str)):
            raise ValueError
        return (key[0], key[1]), value["since_instant"]
    except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise CommandFailure("invalid_argument", "invalid event cursor or changed fleet or filters") from exc


def _item(row: dict, fleet: str) -> dict:
    return {"event_id": row["_event_id"], "fleet": fleet, "occurred_at": row["ts"],
            "bot": row["bot"], "type": row["type"], "source": row["source"],
            "severity": row["_severity"], "data": row["data"],
            "detail_truncated": row["_truncated"]}


def dispatch(args) -> CommandOutput:
    """Canonical ``event list/show`` over the existing Plane renderer."""
    from ._helpers import _resolve_paths
    from ..brief import resolve_fleet_name
    from .checkins import _since

    if args.seed:
        raise CommandFailure("conflict", "seed configuration has no fleet event history")
    if args.event_action == "list" and not 1 <= args.limit <= 1000:
        raise CommandFailure("invalid_argument", "--limit must be from 1 to 1000")
    typed = getattr(args, "since", None)
    since = None
    if typed:
        try:
            since = _since(typed).isoformat()
        except ValueError as exc:
            raise CommandFailure("invalid_argument", str(exc)) from exc

    paths = _resolve_paths(args)
    fleet = resolve_fleet_name(paths)
    conn, note = plane_events_conn(paths)
    if conn is None:
        raise CommandFailure("unavailable", f"Plane events are unreachable: {note}", retryable=True)
    try:
        from ..paths import load_lib_module
        pr = load_lib_module(paths.lib, "plane-readers.py")
        if pr is None:
            raise RuntimeError("selected Plane reader is unavailable")
        if args.event_action == "show":
            rows = pr.fleet_events(conn, fleet, event_id=args.event_id)
            if not rows:
                raise CommandFailure("not_found", "event ID is not recorded in the selected fleet")
            item = _item(rows[0], fleet)
            return CommandOutput({"event": item}, lines=(f"{item['event_id']}\t{item['type']}\t{item['bot']}\t{item['occurred_at']}",))
        scope = {"root": str(paths.root), "fleet": fleet, "bot": args.bot,
                 "type": args.type, "source": args.source, "critical": args.critical,
                 "since": typed}
        cursor = _after(args.cursor, scope)
        after = cursor[0] if cursor else None
        if cursor:
            since = cursor[1]
        rows = pr.fleet_events(conn, fleet, since=since, bot=args.bot, event_type=args.type)
        rows = [row for row in rows if (not args.source or row["source"] == args.source)
                and (not args.critical or row["_severity"] == "critical")]
        rows.reverse()  # newest event first, with ingest sequence breaking timestamp ties
        if after is not None:
            rows = [row for row in rows if (row["_occurred_at"], row["_ingest_seq"]) < after]
        page = rows[:args.limit]
        next_cursor = (_cursor(scope, (page[-1]["_occurred_at"], page[-1]["_ingest_seq"]), since)
                       if len(rows) > args.limit else None)
        coverage = _events_coverage_line(conn, paths, typed)
        items = [_item(row, fleet) for row in page]
        lines = tuple(f"{item['event_id']}\t{item['occurred_at']}\t{item['bot']}\t{item['type']}\t{item['source']}"
                      for item in items) or ("No events found.",)
        if next_cursor:
            lines += (f"next_cursor: {next_cursor}",)
        lines += (coverage,)
        return CommandOutput({"items": items, "next_cursor": next_cursor, "coverage": coverage}, lines=lines)
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        raise CommandFailure("unavailable", f"Plane events are unreachable: {exc}", retryable=True) from exc
    finally:
        conn.close()
