# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11),
minimal form: the decision rows, newest first. `--summary`, `--limit` and the
outcome join land in chunk 3.

Two connections, on purpose: `brief.plane_session` is THE reachability door for
the package (no db / no fleet / a plane that has never seen the fleet all refuse
with a note -- unreachable is not empty), but its connection yields TUPLES
(plane-readers.py:53-58; status.py:218). The rows are read through
`plane.db.open_ro`, which sets sqlite3.Row -- the `commands/task.py` pattern. The
session is a context manager; it is probed and closed here on purpose (nothing is
read through it), not entered.
Usage errors (no fleet, an unparseable --since) are rc 2 -- dispatch-overdue.py's
convention for a read door (2 = malformed call, 3 = cannot answer); report-back
says 1 for the same case and task-act.sh's write ladder puts usage at 1 too.
One convention for the plane's READ doors wins over matching either sibling."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from ..plane.db import open_ro
from ..plane.queries import CHECKIN_ROWS_SQL, fleet_range_params
from ._helpers import _resolve_paths, refuse_unreachable


def _since(text: str) -> datetime:
    """The `--since` grammar the read doors share: 24h, 7d, 30m, or an ISO
    instant (`cmd_report_back` applies the same rule inline, core.py)."""
    raw = (text or "").strip()
    now = datetime.now(timezone.utc)
    try:
        if raw.endswith("h"):
            return now - timedelta(hours=int(raw[:-1]))
        if raw.endswith("d"):
            return now - timedelta(days=int(raw[:-1]))
        if raw.endswith("m"):
            return now - timedelta(minutes=int(raw[:-1]))
        got = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"cannot parse --since '{raw}' (use e.g. 24h, 7d, 30m, or ISO date)") from None


def _bot_of(alias: str | None) -> str:
    return alias.rsplit("/", 1)[-1] if alias else ""


def _row(r) -> dict:
    truncated = bool(r["detail_truncated"])
    detail = r["detail"]
    rec = json.loads(detail) if detail and not truncated else None    # a data-less row parses to nothing, never raises
    if not isinstance(rec, dict):
        rec = None
    g = rec or {}
    raise_ = g.get("raise") if isinstance(g.get("raise"), dict) else {}
    seen = g.get("inputs_seen") if isinstance(g.get("inputs_seen"), dict) else {}
    return {
        "checkin_id": g.get("checkin_id"), "prev_checkin_id": g.get("prev_checkin_id"),
        "bot": _bot_of(r["subject_alias"]), "occurred_at": r["occurred_at"],
        "action": g.get("action"), "project_key": g.get("project_key"),
        "raise": {"decided": bool(raise_.get("decided")), "reason": raise_.get("reason")},
        "rationale": g.get("rationale"),
        "considered": list(seen.get("considered") or []), "unavailable": list(seen.get("unavailable") or []),
        "truncated": truncated, "record": rec,
    }


def collect_checkins(conn, fleet: str, *, since: datetime | None, bot: str | None,
                     last: bool, raised: bool = False) -> list[dict]:
    """Newest first by occurred_at. `since` None means no window (--last);
    `raised` keeps only rows whose raise.decided is true (the ask count)."""
    out: list[dict] = []
    for r in conn.execute(CHECKIN_ROWS_SQL, fleet_range_params(fleet)):
        if bot and _bot_of(r["subject_alias"]) != bot:
            continue
        if since is not None and datetime.fromisoformat(r["occurred_at"]) < since:
            continue
        row = _row(r)
        if raised and not row["raise"]["decided"]:
            continue
        out.append(row)
        if last:
            break
    return out


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import TEXT_ROW_LIMIT, plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    try:
        since = None if args.last else _since(args.since)
    except ValueError as exc:
        print(f"checkins: {exc}", file=sys.stderr)
        return 2
    plane, note = plane_session(paths, fleet)
    if plane is None:
        return refuse_unreachable("checkins", note)
    plane.close()
    conn, reason = open_ro(paths.root)
    if conn is None:
        return refuse_unreachable("checkins", reason or "plane db unreadable")
    try:
        rows = collect_checkins(conn, fleet, since=since, bot=args.bot, last=args.last,
                                raised=getattr(args, "raised", False))
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"schema": 1, "fleet": fleet,
                          "since": since.isoformat() if since else None,
                          "checkins": rows}, indent=2))
        return 0
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + \
        (" (newest only)" if args.last else f", last {args.since}") + \
        (" (asks only)" if getattr(args, "raised", False) else "")
    if not rows:
        print(f"no check-ins — {scope}" + ("" if getattr(args, "last", False) else " (--last reads the newest row with no window)"))
        return 0
    print(f"check-ins — {scope}: {len(rows)}")
    for r in rows[:TEXT_ROW_LIMIT]:
        if r["truncated"]:
            print(f"  {r['occurred_at']}  {r['bot']}  (record over the size cap — truncated at ingest)")
            continue
        if r["record"] is None:
            print(f"  {r['occurred_at']}  {r['bot']}  (row carries no record)")
            continue
        raised = " · raised" if r["raise"]["decided"] else ""
        proj = f" [{r['project_key']}]" if r["project_key"] else ""
        print(f"  {r['occurred_at']}  {r['bot']}  {r['action']}{proj}{raised}  {r['checkin_id']}")
        print(f"      {r['rationale']}")
        print(f"      surfacing: {r['raise']['reason']}")
        if r["considered"]:
            print("      passed over: " + " · ".join(r["considered"]))
        if r["unavailable"]:
            print("      unavailable: " + ", ".join(r["unavailable"]))
    if len(rows) > TEXT_ROW_LIMIT:
        # silent truncation reads as exhaustive coverage (brief.py's rows() rule)
        print(f"  ... showing the newest {TEXT_ROW_LIMIT} of {len(rows)} — full list in --json")
    return 0
