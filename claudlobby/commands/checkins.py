# claudlobby/commands/checkins.py
"""Shared check-in row projection and factual summary for the public reader."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..plane.queries import (
    checkin_dispatch_rows_sql,
    checkin_rows_sql,
    fleet_range_params,
)
from ..task_state import TaskStateError, read_tasks

# Assignment terminal events, never transmission verdicts, decide outcomes.
# An unresolved canonical link is unknown rather than a healthy open task.
_OUTCOME = {
    "completed": "completed",
    "returned_blocked": "blocked",
    "failed": "failed",
    "expired": "failed",
    "rejected": "retired",
    "cancelled": "retired",
    "superseded": "retired",
    "reassigned": "retired",
}
OUTCOMES = ("completed", "blocked", "failed", "retired", "open", "unknown", "unjoined")


def _join_dispatches(conn, fleet: str, refs: list[str]) -> dict[str, list[dict]]:
    """Resolve one page's recorded links through one canonical task projection.

    The caller owns the read transaction so the links and reducer share it.
    """
    if not conn.in_transaction:
        raise TaskStateError("check-in join requires a read snapshot")
    ids = [r.split("checkin:", 1)[1] for r in refs if r and r.startswith("checkin:")]
    if not ids:
        return {}
    links = list(conn.execute(checkin_dispatch_rows_sql(len(ids)),
                              (fleet, *fleet_range_params(fleet), *ids)))
    if not links:
        return {}
    fleet_row = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias=?",
                             (fleet,)).fetchone()
    if fleet_row is None:
        raise TaskStateError("selected fleet identity is unavailable")
    fleet_uid = fleet_row[0]
    snapshot = read_tasks(conn, fleet_uid=fleet_uid,
                          task_ids=[r["work_item_id"] for r in links
                                    if isinstance(r["work_item_id"], str) and r["work_item_id"]])
    tasks = {task.task_id: task for task in snapshot.tasks}
    assignments = {assignment.assignment_id: assignment
                   for task in snapshot.tasks for assignment in task.assignments}
    out: dict[str, list[dict]] = {}
    for r in links:
        task = tasks.get(r["work_item_id"])
        assignment = assignments.get(r["assignment_id"])
        problem = ("missing_task" if task is None else
                   "missing_assignment" if assignment is None else
                   "mismatched_assignment_task" if assignment.task_id != task.task_id or
                   assignment.fleet_uid != fleet_uid else
                   "unresolved_task" if task.blockers or task.state is None else None)
        terminal = assignment.terminal_event if assignment and problem is None else None
        outcome = ("unjoined" if problem in {"missing_task", "missing_assignment",
                                            "mismatched_assignment_task"} else
                   "unknown" if problem or (terminal and terminal.event not in _OUTCOME) else
                   _OUTCOME[terminal.event] if terminal else "open")
        out.setdefault(r["checkin_id"], []).append({
            "task_id": r["work_item_id"], "assignment_id": r["assignment_id"],
            "historical_task_reference": r["task_id"],
            "task_state": task.state if problem is None else None,
            "assignment_state": assignment.state if problem is None else None,
            "terminal_event": terminal.event if terminal else None,
            "outcome": outcome, "link_issue": problem,
            "delivery": "not_assessed",
            "terminal_at": terminal.occurred_at if terminal else None,
            "occurred_at": r["occurred_at"],
        })
    return out


def _since(text: str) -> datetime:
    """Check-in `--since`: 24h, 7d, 30m, or an ISO instant.

    Fleet report listing has its own RFC3339-with-offset contract.
    """
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
    return alias.rsplit("/", 1)[-1] if alias and alias.startswith("bot:") else ""


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
        "actor": r["subject_alias"], "bot": _bot_of(r["subject_alias"]),
        "occurred_at": r["occurred_at"],
        "action": g.get("action"), "project_key": g.get("project_key"),
        "raise": {"decided": bool(raise_.get("decided")), "reason": raise_.get("reason")},
        "rationale": g.get("rationale"),
        "considered": list(seen.get("considered") or []), "unavailable": list(seen.get("unavailable") or []),
        "truncated": truncated, "record": rec,
        "checkin_ref": r["source_ref"], "dispatches": [],
    }


def collect_checkins(conn, fleet: str, *, since: datetime | None, bot: str | None,
                     last: bool, raised: bool = False, limit: int | None = None,
                     checkin_id: str | None = None) -> list[dict]:
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
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        push_limit = limit is not None and not raised
        sql = checkin_rows_sql(since=since is not None, bot=bool(bot), limit=push_limit,
                               checkin_id=checkin_id is not None)
        params: list = [fleet, *fleet_range_params(fleet), *fleet_range_params(fleet)]
        if checkin_id is not None:
            params.append(f"checkin:{checkin_id}")
        if bot:
            # exact and case-sensitive -- fleet_alias_range's own rule.
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
    finally:
        if own_snapshot:
            conn.rollback()


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
    assignment_states: dict[str, int] = {}
    terminal_events: dict[str, int] = {}
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
            if d.get("assignment_state"):
                state = d["assignment_state"]
                assignment_states[state] = assignment_states.get(state, 0) + 1
            if d.get("terminal_event"):
                event = d["terminal_event"]
                terminal_events[event] = terminal_events.get(event, 0) + 1
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
        "assignment_states": assignment_states,
        "terminal_events": terminal_events,
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
