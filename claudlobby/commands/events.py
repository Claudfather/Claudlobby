"""claudlobby events — the fleet's events, from the plane and nothing else.

Every fleet event lands on the plane as a system event anchored on the bot's
actor (or the fleet, or the host) with a ``fleet-events:`` provenance and a
detail carrying ``{source, legacy_ts, data}`` (F18 R1: the plane is the only
recorder). The stdlib readers the bash doors ship (``lib/plane-readers.py``)
render each one back as the row the retired ledgers used to hold, so the
table and the ``--json`` rows are the shapes they were — ONE rendering,
shared with ``plane-lookup.py --events`` and fleet-pulse. ``--critical`` is
the severity the registry stamped at ingest (``SYSTEM_EVENT_SEVERITY``), one
definition; ``CRITICAL_TYPES`` below is the file-era vocabulary, kept so the
registry is pinned to agree with it.

There is no file to read (F18 closure, R2b): R1 removed every writer, and a
reader that could still open one would read nothing at best and the archive
at worst. There is no flag and no declaration either — the plane is the only
source. An unreachable plane REFUSES (rc 3, the remedy on stderr): "No events
found." from an instrument that could not be reached is a claim about the
estate drawn from nothing, the #1216 class. A reachable plane holding no
event for the fleet prints that line honestly, at rc 0.
"""

from __future__ import annotations

import json
import sqlite3
import sys

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
}


def _readers(paths):
    """The stdlib readers, loaded from the INSTALL's lib/ (the door and the
    bash readers must render one row the same way), or None."""
    from ..brief import load_lib_module
    return load_lib_module(paths, "plane-readers.py")


def plane_events_conn(paths):
    """(conn, note): an OPEN read-only plane connection, or ``(None, why)``
    when the plane cannot answer — no fleet named, no db, an unopenable or
    schema-less db, or a fleet the plane holds no identity for (a wrong root
    is not "nothing recorded"). No flag, no declaration (F18 closure, R2b):
    the plane is the only source. The caller closes."""
    from ..brief import resolve_fleet_name
    from ..plane.db import open_ro
    fleet = resolve_fleet_name(paths)          # root mode names its fleet in fleet.yaml alone
    if not fleet:
        return None, ("no fleet is named (--fleet <name>, or a fleet.yaml naming one) — the"
                      " plane's events are per fleet")
    conn, why = open_ro(paths.root)
    if conn is None:
        return None, why
    pr = _readers(paths)
    if pr is None:
        conn.close()
        return None, f"lib/plane-readers.py is not readable under {paths.lib}"
    try:
        pr.fleet_uid(conn, fleet)          # the identity probe: schema AND fleet, one read
    except (pr.PlaneUnreachable, sqlite3.Error) as exc:
        conn.close()
        return None, str(exc)
    return conn, None


def collect_plane_events(conn, paths, *, bot=None, event_type=None, source=None,
                         critical_only=False, since=None) -> list[dict]:
    """The fleet's events from the plane as legacy rows — the same filters and
    row shape the retired files had, through the stdlib readers the bash
    doors ship (one row rendering; critical = the severity the registry
    stamped at ingest, one definition). A plane that cannot answer (no
    identity for the fleet, a db error) raises RuntimeError — the caller's
    refusal."""
    from ..brief import resolve_fleet_name
    pr = _readers(paths)
    if pr is None:
        raise RuntimeError(f"lib/plane-readers.py is not readable under {paths.lib}")
    try:
        rows = pr.fleet_events(conn, resolve_fleet_name(paths), since=since, bot=bot, event_type=event_type)
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


def cmd_events(args) -> int:
    """CLI entry point for ``claudlobby events``."""
    from ._helpers import _resolve_paths

    paths = _resolve_paths(args)
    conn, note = plane_events_conn(paths)
    if conn is None:
        # rc 3, the unreachable-is-not-empty code every plane reader uses:
        # nothing is printed on stdout, so a caller cannot read the refusal
        # as a quiet fleet.
        print(f"claudlobby events: UNREACHABLE — {note}; restore the plane db"
              f" (state/plane/plane.db) under {paths.root} or name the right root — no"
              " event is served rather than a wrong count", file=sys.stderr)
        return 3
    try:
        events = collect_plane_events(conn, paths, bot=args.bot, event_type=args.type,
                                      source=args.source, critical_only=args.critical,
                                      since=getattr(args, "since", None))
    except RuntimeError as exc:
        print(f"claudlobby events: UNREACHABLE — {exc}", file=sys.stderr)
        return 3
    finally:
        conn.close()

    if args.json:
        for ev in events:
            print(json.dumps(ev, separators=(",", ":")))
    else:
        if args.tail:
            events = events[-args.tail :]
        print(format_event_table(events))
    return 0
