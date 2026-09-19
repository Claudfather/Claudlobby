# claudlobby/commands/checkins.py
"""`claudlobby checkins` — the check-in's read door (manager check-in spec §11):
the decision rows newest-first, plus `--summary` (chunk 3), which rolls the
window up into FACTS ONLY -- actions, ask rate, considered lengths,
unavailable frequencies, dispatch outcomes, grouped by project_key -- and
never a verdict; that judgment is an operator ruling for the whole series, not
this door's to make. `--limit`, and the `--since`/`--bot` window, are bound in
SQL (chunk 4, `checkin_rows_sql` in plane/queries.py) rather than scanned in
Python.

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
from ..plane.queries import (
    TASK_STATUS_SQL,
    checkin_dispatch_rows_sql,
    checkin_rows_sql,
    fleet_range_params,
)
from ._helpers import _resolve_paths, refuse_unreachable

# Raw TASK_STATUS_SQL status -> the bucket a reader acts on: these are
# reader-facing groupings of the plane's raw task statuses. `cancelled` /
# `superseded` / `reassigned` get their own `retired` bucket because a
# withdrawal or a re-dispatch is neither a completion nor a failure. Any
# arithmetic over these fields is a later, separate concern, and none lives
# in this door. `dispatch_failed` is NOT a task event (it is derived from
# transmissions): the send never landed, a failure to start. Anything
# unmapped reads `open` -- bounded by test_every_terminal_task_event_has_a_bucket.
_OUTCOME = {
    "completed": "completed",
    "returned_blocked": "blocked",
    "failed": "failed",
    "expired": "failed",
    "dispatch_failed": "failed",
    "cancelled": "retired",
    "superseded": "retired",
    "reassigned": "retired",
}
OUTCOMES = ("completed", "blocked", "failed", "retired", "open", "unjoined")


def _outcome_of(status: str | None) -> str:
    """None = the join row names an assignment the plane does not hold: absence
    inside a reachable source, reported as `unjoined`, never as `open`."""
    return _OUTCOME.get(status, "open") if status else "unjoined"


def _join_dispatches(conn, fleet: str, refs: list[str]) -> dict[str, list[dict]]:
    """checkin_id -> its dispatches, resolved to the plane's own status. TWO
    queries for the whole page, never one per row: the join rows for every id at
    once, then TASK_STATUS_SQL narrowed by `WHERE a.assignment_id IN (...)` --
    view.py's own pattern, the shipped constant APPENDED to and never copied."""
    ids = [r.split("checkin:", 1)[1] for r in refs if r and r.startswith("checkin:")]
    if not ids:
        return {}
    links = list(conn.execute(checkin_dispatch_rows_sql(len(ids)),
                              (*fleet_range_params(fleet), *ids)))
    asg = [r["assignment_id"] for r in links if r["assignment_id"]]
    status: dict[str, tuple] = {}
    if asg:
        ph = ",".join("?" * len(asg))
        status = {r["assignment_id"]: (r["status"], r["terminal_at"])
                  for r in conn.execute(
                      TASK_STATUS_SQL + f" WHERE a.assignment_id IN ({ph})", asg)}
    out: dict[str, list[dict]] = {}
    for r in links:
        st, at = status.get(r["assignment_id"], (None, None))
        out.setdefault(r["checkin_id"], []).append({
            "assignment_id": r["assignment_id"], "work_item_id": r["work_item_id"],
            "task_id": r["task_id"], "status": st, "outcome": _outcome_of(st),
            "terminal_at": at, "occurred_at": r["occurred_at"],
        })
    return out


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
        "checkin_ref": r["source_ref"], "dispatches": [],
    }


def collect_checkins(conn, fleet: str, *, since: datetime | None, bot: str | None,
                     last: bool, raised: bool = False, limit: int | None = None) -> list[dict]:
    """Newest first by occurred_at. `since` None means no window (--last);
    `raised` keeps only rows whose raise.decided is true (the ask count).

    The bot filter and the since floor are bound in SQL (checkin_rows_sql,
    PR 3 chunk 4) rather than scanned here in Python -- the params list below
    is gated on the SAME three booleans passed to checkin_rows_sql, in the
    SAME order, so the SQL shape and the bind list cannot drift apart.
    `limit` is pushed into SQL only when `raised` is False: with `raised`
    True the SQL read is unbounded and the limit is applied in Python AFTER
    the raise filter, or `--raised --limit N` would return N rows of which
    only some raised, not N raised rows. `out` is clamped to `limit` in
    Python either way, so the two paths cannot disagree."""
    push_limit = limit is not None and not raised
    sql = checkin_rows_sql(since=since is not None, bot=bool(bot), limit=push_limit)
    params: list = list(fleet_range_params(fleet))
    if bot:
        # exact and case-sensitive -- fleet_alias_range's own rule, and
        # byte-equivalent to PR 1's `_bot_of(...) != bot` given the fleet
        # range above already holds
        params.append(f"bot:{fleet}/{bot}")
    if since is not None:
        params.append(since.isoformat())
    if push_limit:
        params.append(limit)
    out: list[dict] = []
    for r in conn.execute(sql, params):
        row = _row(r)
        if raised and not row["raise"]["decided"]:
            continue
        out.append(row)
        if last:
            break
    if limit is not None:
        out = out[:limit]
    joined = _join_dispatches(conn, fleet, [x["checkin_ref"] for x in out])
    for row in out:
        row["dispatches"] = joined.get((row["checkin_ref"] or "").split("checkin:", 1)[-1], [])
    return out


def _block(rows: list[dict]) -> dict:
    """One rollup block -- the shape `summarize` uses for both `totals` and
    every `project_key` group, through this single definition, so a group can
    never carry a field the totals lack (test_the_group_blocks_have_the_same_
    shape_as_totals). FACTS only: a count, a distribution -- no threshold, no
    verdict, ever."""
    checkins = len(rows)
    actions = {"dispatch": 0, "ask": 0, "nothing": 0}
    no_record = 0
    raised = 0
    considered_lengths: list[int] = []
    unavailable: dict[str, int] = {}
    dispatches = 0
    dispatch_outcomes = {k: 0 for k in OUTCOMES}
    dispatch_statuses: dict[str, int] = {}
    for r in rows:
        if r.get("action") in actions:
            actions[r["action"]] += 1
        if r.get("record") is None:
            no_record += 1
        else:
            # considered lengths are measured over rows WITH a parsed record
            # only -- a window of truncated rows must not read as a window of
            # empty `considered` lists (no_record says how many were excluded)
            considered_lengths.append(len(r.get("considered") or []))
        if r.get("raise", {}).get("decided"):
            raised += 1
        for tok in r.get("unavailable") or []:
            unavailable[tok] = unavailable.get(tok, 0) + 1
        row_dispatches = r.get("dispatches") or []
        dispatches += len(row_dispatches)
        for d in row_dispatches:
            dispatch_outcomes[d["outcome"]] += 1
            if d.get("status"):
                dispatch_statuses[d["status"]] = dispatch_statuses.get(d["status"], 0) + 1
        if r.get("action") == "dispatch" and not row_dispatches:
            # the one place this door counts something the row list does not
            # contain: a dispatch decision with no join row at all -- a quiet
            # ask/nothing row must add nothing here (the negative test)
            dispatch_outcomes["unjoined"] += 1
    return {
        "checkins": checkins,
        "actions": actions,
        "raised": raised,
        "ask_rate": round(raised / checkins, 3) if checkins else 0.0,
        "no_record": no_record,
        "considered": {
            "rows": len(considered_lengths),
            "empty": sum(1 for n in considered_lengths if n == 0),
            "min": min(considered_lengths) if considered_lengths else None,
            "max": max(considered_lengths) if considered_lengths else None,
            "mean": round(sum(considered_lengths) / len(considered_lengths), 3)
                if considered_lengths else None,
        },
        "unavailable": unavailable,
        "dispatches": dispatches,
        "dispatch_outcomes": dispatch_outcomes,
        "dispatch_statuses": dispatch_statuses,
    }


def summarize(rows: list[dict]) -> dict:
    """The window rolled up: one `_block` over every row, and one per
    project_key group through the same helper. A PURE function of the row
    dicts `collect_checkins` returns -- no db, no clock, no I/O -- so it is
    unit-testable with hand-built rows and cannot silently acquire a second
    source. `projects` is a LIST sorted by key with the null group last: a
    JSON object cannot hold a null key, and mapping it to e.g. "-" would
    collide with a project legitimately named that."""
    groups: dict[str | None, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get("project_key"), []).append(r)
    projects = [{"project_key": k, **_block(groups[k])} for k in sorted(k for k in groups if k is not None)]
    if None in groups:
        projects.append({"project_key": None, **_block(groups[None])})
    return {"totals": _block(rows), "projects": projects}


def _format_block(block: dict) -> list[str]:
    """The text rendering of one `_block` -- shared by the totals line and
    every project group so the two can never drift apart in shape."""
    c = block["considered"]
    return [
        f"    checkins: {block['checkins']}",
        f"    actions: dispatch={block['actions']['dispatch']} ask={block['actions']['ask']}"
        f" nothing={block['actions']['nothing']}",
        f"    raised: {block['raised']} (ask_rate {block['ask_rate']})",
        f"    no_record: {block['no_record']}",
        f"    considered: rows={c['rows']} empty={c['empty']} min={c['min']} max={c['max']} mean={c['mean']}",
        "    unavailable: " + (", ".join(f"{k}={v}" for k, v in block["unavailable"].items()) or "none"),
        f"    dispatches: {block['dispatches']}",
        "    dispatch_outcomes: " + " ".join(f"{k}={v}" for k, v in block["dispatch_outcomes"].items())
        + " (unjoined also counts dispatch decisions that joined nothing, so this can sum to more than dispatches)",
        "    dispatch_statuses: " + (", ".join(f"{k}={v}" for k, v in block["dispatch_statuses"].items()) or "none"),
    ]


def cmd_checkins(args) -> int:
    paths = _resolve_paths(args)
    from ..brief import TEXT_ROW_LIMIT, plane_session, resolve_fleet_name

    fleet = getattr(args, "checkins_fleet", None) or resolve_fleet_name(paths)
    if not fleet:
        print("checkins: no fleet is named (--fleet <name>, or a fleet.yaml naming one)"
              " — the plane's rows are per fleet", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        # 0 is not silently "no rows" -- an operator's explicit bound must
        # name at least one row or it is a typo, not a request
        print(f"checkins: --limit must be a positive integer (got {args.limit})", file=sys.stderr)
        return 2
    if args.summary:
        # both checked before the plane is opened -- a malformed call (rc 2)
        # never needs a db connection to be recognized as malformed
        if args.last:
            print("checkins: --summary and --last are exclusive — a summary of one row states"
                  " a window it did not read", file=sys.stderr)
            return 2
        if args.limit is not None:
            print("checkins: --summary and --limit are exclusive — a summary over a truncated"
                  " slice states a window it did not read", file=sys.stderr)
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
                                raised=getattr(args, "raised", False), limit=args.limit)
    finally:
        conn.close()
    scope = f"fleet {fleet}" + (f", bot {args.bot}" if args.bot else "") + \
        (" (newest only)" if args.last else f", last {args.since}") + \
        (" (asks only)" if getattr(args, "raised", False) else "") + \
        (f", limit {args.limit}" if args.limit is not None else "")

    if args.summary:
        summary = summarize(rows)
        if args.json:
            print(json.dumps({"schema": 1, "fleet": fleet,
                              "since": since.isoformat() if since else None,
                              "scope": scope, **summary}, indent=2))
            return 0
        print(f"check-ins --summary — {scope}: {summary['totals']['checkins']}")
        for line in _format_block(summary["totals"]):
            print(line)
        projects = summary["projects"]
        for p in projects[:TEXT_ROW_LIMIT]:
            key = p["project_key"] if p["project_key"] is not None else "(none)"
            print(f"  [{key}]")
            for line in _format_block(p):
                print(line)
        if len(projects) > TEXT_ROW_LIMIT:
            # same disclosure rule as the row listing below: silent truncation
            # reads as exhaustive coverage (brief.py's rows() rule)
            print(f"  ... showing {TEXT_ROW_LIMIT} of {len(projects)} project groups — full list in --json")
        return 0

    if args.json:
        # scope/limit disclosed here too -- the same Global Constraint
        # --summary's --json envelope already honors: --limit is an
        # operator's explicit bound, applied to BOTH surfaces and stated in
        # the scope line, so a --json consumer must be able to tell "there
        # were exactly N rows" from "there were more, cut to N" without
        # falling back to the text listing
        print(json.dumps({"schema": 1, "fleet": fleet,
                          "since": since.isoformat() if since else None,
                          "scope": scope, "limit": args.limit,
                          "checkins": rows}, indent=2))
        return 0
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
        for d in r["dispatches"]:
            tid = d["task_id"] or "id-less"
            when = f" ({d['terminal_at']})" if d["terminal_at"] else ""
            print(f"      → {tid}  {d['outcome']} [{d['status'] or 'no assignment row'}]{when}"
                  f"  {d['assignment_id']}")
        if not r["dispatches"] and r["action"] == "dispatch":
            print("      → no dispatch joined to this decision")
    if len(rows) > TEXT_ROW_LIMIT:
        # silent truncation reads as exhaustive coverage (brief.py's rows() rule)
        print(f"  ... showing the newest {TEXT_ROW_LIMIT} of {len(rows)} — full list in --json")
    return 0
