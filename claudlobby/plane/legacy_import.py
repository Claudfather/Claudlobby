"""The parity-gap importer (cutover chunk 2, #1444; F18 selective import).

What it is for: the plane becomes the record only when every legacy row a
reader could still consult is in it. Measured on the Mini (2026-09-02) the
gap was 25 pre-go-live dispatches and 0 lost emits, and the ledgers rotate
at seven days — so this is a small, recurring repair tool, never
archaeology. It plans from ``parity.compare`` (one join, one set of
causes) and lands rows through NORMAL ingest with ``origin=legacy`` and an
``import_batch``, so an imported row is distinguishable from a live one
forever, and a re-run classifies ``duplicate`` (ids are content-hashed).

Every rule below is from the J3 evaluation and was verified in code:

- a dispatch imports as FOUR events — work_item, assignment, communication
  and a ``pane_submitted`` tmux transmission — or ``TASK_STATUS_SQL``
  renders it ``created_not_sent`` (its ELSE branch fires whenever no
  transmission exists for ``dispatch_msg_id``);
- a report imports as its communication plus its task event
  (``completed`` / ``failed`` / ``returned_blocked`` / ``progress``), with the
  ``supplied_id_not_open`` anomaly alongside when the row recorded one;
- **fleet attribution is by the fleet's own report ledger and nothing
  else**: the dispatch log is host-global with no fleet column, bots move
  fleets and names collide across them (#526), so a dispatch row belongs to
  fleet F only if F's ``report-back.jsonl`` holds a report for its task id.
  Anything else is skipped and DISCLOSED, never guessed — which also means
  query rows (no task id by design) are never imported;
- a report whose dispatch row is in neither the plane nor this plan has no
  work item to hang on (``TaskEvent.work_item_id`` is required) and is an
  orphan: skipped and disclosed;
- ids: a stamped row keeps its own ``plane_*`` ids (the JSONL's references
  to it stay valid); an unstamped row gets ``sha256(ledger + raw line +
  role)`` — content, never position, because rotation rewrites lines;
- the manager's alias resolves through the identity registry when exactly
  one fleet has a bot of that name; otherwise it is assumed to be F and the
  assumption is COUNTED in the plan.

The epoch declaration is deliberately NOT here: it lands with the flip
that consumes it. Dry-run is the default; ``apply_import`` writes.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .parity import (
    DISPATCH, REPORT, LedgerParity, LegacyRow, compare, content_key, read_ledger,
)

EMITTER = "plane-import"
STATUS_EVENT = {
    "completed": "completed",
    "failed": "failed",
    "blocked": "returned_blocked",
    "progress": "progress",
}
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_UNTITLED = "(untitled legacy dispatch)"


def _h(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def _iso(epoch) -> Optional[str]:
    """The ledger stores ``expected_by`` / ``dispatched_at`` as epoch seconds
    (or null); the contract wants an aware ISO instant."""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@dataclass
class ImportPlan:
    fleet: str
    batch: str
    dispatch: LedgerParity
    report: LedgerParity
    events: list[dict] = field(default_factory=list)
    dispatches: int = 0
    reports: int = 0
    unattributed: list[str] = field(default_factory=list)   # no report in this fleet's ledger
    orphan_reports: list[str] = field(default_factory=list) # no dispatch row anywhere
    unknown_status: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    assumed_manager_fleet: int = 0

    @property
    def reachable(self) -> bool:
        return self.dispatch.reachable and self.report.reachable


def manager_alias(conn: sqlite3.Connection, fleet: str, name: str) -> tuple[str, bool]:
    """(alias, assumed). Registry-resolved when exactly ONE fleet has a bot
    of that name; otherwise assumed to be *fleet*, and the caller counts it."""
    like = "bot:%/" + (name.replace("\\", "\\\\").replace("%", "\\%")
                       .replace("_", "\\_"))
    fleets = {
        r[0][len("bot:"):].rsplit("/", 1)[0]
        for r in conn.execute(
            "SELECT DISTINCT alias FROM identity_registry"
            " WHERE alias LIKE ? ESCAPE '\\'", (like,))}
    if len(fleets) == 1:
        return f"bot:{fleets.pop()}/{name}", False
    return f"bot:{fleet}/{name}", True


def _envelope(event_type: str, ref: str, fleet: str, ts: str, batch: str,
              event_key: str, payload: dict) -> dict:
    return {
        "event_type": event_type,
        "emitter": EMITTER,
        "source_ref": ref,
        "fleet": fleet,
        "occurred_at": ts,
        "origin": "legacy",
        "import_batch": batch,
        "event_id": f"ev_{event_key}",
        "payload": payload,
    }


def dispatch_events(row: LegacyRow, fleet: str, manager: str,
                    batch: str) -> tuple[dict, list[dict]]:
    """The four events one legacy dispatch becomes. Returns the ids the
    row's reports will link to, and the events in ingest order."""
    r = row.row
    task_id = str(r["task_id"])
    bot = str(r["bot"])
    wi = r.get("plane_work_item_id") or f"wi_{_h(DISPATCH, row.raw, 'wi')}"
    asg = r.get("plane_assignment_id") or f"asg_{_h(DISPATCH, row.raw, 'asg')}"
    msg = r.get("plane_msg_id") or f"msg_{_h(DISPATCH, row.raw, 'msg')}"
    ref = f"{DISPATCH}:{task_id}"
    ts = row.ts
    assignee = f"bot:{fleet}/{bot}"
    text = str(r.get("task") or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = (lines[0][:200] if lines else "") or _UNTITLED
    wi_p: dict = {"work_item_id": wi, "title": title, "created_by": manager}
    if r.get("workstream"):
        wi_p["workstream_id"] = str(r["workstream"])
    asg_p: dict = {"assignment_id": asg, "work_item_id": wi, "assignee": assignee,
                   "assigned_by": manager, "dispatch_msg_id": msg}
    expected = _iso(r.get("expected_by"))
    if expected:
        asg_p["expected_by"] = expected
    comm_p: dict = {"msg_id": msg, "sender": manager, "recipient": assignee,
                    "recipient_raw": bot, "message_class": "task_request",
                    "command_type": "task", "work_item_id": wi,
                    "assignment_id": asg}
    if text:
        comm_p["body"] = text
    tx_p = {"msg_id": msg, "attempt_no": 1, "carrier": "tmux",
            "destination": bot, "state": "pane_submitted"}
    events = [
        _envelope("work_item", ref, fleet, ts, batch, _h(DISPATCH, row.raw, "ev:wi"), wi_p),
        _envelope("assignment", ref, fleet, ts, batch, _h(DISPATCH, row.raw, "ev:asg"), asg_p),
        _envelope("communication", ref, fleet, ts, batch, _h(DISPATCH, row.raw, "ev:comm"), comm_p),
        _envelope("transmission", ref, fleet, ts, batch, _h(DISPATCH, row.raw, "ev:tx"), tx_p),
    ]
    return {"wi": wi, "asg": asg, "msg": msg}, events


def report_events(row: LegacyRow, fleet: str, wi: str, asg: str,
                  recipient: Optional[str], batch: str) -> Optional[list[dict]]:
    """A report's communication + task event (+ the anomaly it recorded).
    None when the row's status has no plane vocabulary — disclosed, never
    coerced."""
    r = row.row
    event = STATUS_EVENT.get(str(r.get("status") or ""))
    if not event:
        return None
    bot = str(r["bot"])
    actor = f"bot:{fleet}/{bot}"
    stamped = r.get("plane_msg_id")
    msg = stamped or f"msg_{_h(REPORT, row.raw, 'msg')}"
    ref = f"{REPORT}:{stamped}" if stamped else f"{REPORT}:sha:{content_key(row.raw)}"
    ts = row.ts
    comm_p: dict = {"msg_id": msg, "sender": actor, "message_class": "report",
                    "work_item_id": wi, "assignment_id": asg}
    if recipient:
        comm_p["recipient"] = recipient
    if r.get("summary"):
        comm_p["body"] = str(r["summary"])
    task_p: dict = {"work_item_id": wi, "assignment_id": asg, "event": event,
                    "actor": actor}
    if r.get("summary"):
        task_p["summary"] = str(r["summary"])
    if r.get("pr_url"):
        task_p["pr_url"] = str(r["pr_url"])
    try:
        progress = int(r.get("progress"))
        if 0 <= progress <= 100:
            task_p["progress"] = progress
    except (TypeError, ValueError):
        pass
    events = [
        _envelope("communication", ref, fleet, ts, batch, _h(REPORT, row.raw, "ev:comm"), comm_p),
        _envelope("task", ref, fleet, ts, batch, _h(REPORT, row.raw, "ev:task"), task_p),
    ]
    if r.get("task_anomaly") == "supplied-id-not-open":
        events.append(_envelope(
            "task", ref, fleet, ts, batch, _h(REPORT, row.raw, "ev:anomaly"),
            {"work_item_id": wi, "assignment_id": asg, "event": "supplied_id_not_open",
             "actor": actor}))
    return events


def plan_import(conn: sqlite3.Connection, *, fleet: str, dispatch_path: Path,
                report_path: Path, now: datetime,
                since: Optional[str] = None) -> ImportPlan:
    """Derive the batch for *fleet* from parity. Pure: reads only."""
    dp = compare(conn, DISPATCH, dispatch_path, since=since)
    rp = compare(conn, REPORT, report_path, since=since)
    batch = f"imp_{_h(fleet, now.isoformat())[:16]}"
    plan = ImportPlan(fleet, batch, dp, rp)
    if not plan.reachable:
        return plan
    # The attribution evidence: every task id this fleet's ledger reported,
    # over the WHOLE ledger (a report may sit below --since).
    _, _, all_reports, _ = read_ledger(report_path)
    reported = {str(x.row["task_id"]) for x in all_reports if x.row.get("task_id")}
    ids_by_task: dict[str, dict] = {}
    manager_by_task: dict[str, str] = {}
    for m in dp.missing:
        r = m.row.row
        task_id = r.get("task_id")
        if not task_id or str(task_id) not in reported:
            plan.unattributed.append(m.key)
            continue
        manager, bot = r.get("manager"), r.get("bot")
        if not (manager and bot and _NAME.match(str(manager)) and _NAME.match(str(bot))):
            plan.malformed.append(m.key)
            continue
        alias, assumed = manager_alias(conn, fleet, str(manager))
        plan.assumed_manager_fleet += int(assumed)
        ids, events = dispatch_events(m.row, fleet, alias, batch)
        ids_by_task[str(task_id)] = ids
        manager_by_task[str(task_id)] = alias
        plan.events.extend(events)
        plan.dispatches += 1
    for m in rp.missing:
        r = m.row.row
        task_id = str(r.get("task_id") or "")
        bot = r.get("bot")
        if not (bot and _NAME.match(str(bot))):
            plan.malformed.append(m.key)
            continue
        ids = ids_by_task.get(task_id)
        if ids is None and task_id:
            hit = conn.execute(
                "SELECT work_item_id, assignment_id FROM assignments"
                " WHERE source_ref = ? ORDER BY ingest_seq DESC LIMIT 1",
                (f"{DISPATCH}:{task_id}",)).fetchone()
            if hit:
                ids = {"wi": hit[0], "asg": hit[1]}
        if ids is None:
            plan.orphan_reports.append(m.key)
            continue
        events = report_events(m.row, fleet, ids["wi"], ids["asg"],
                               manager_by_task.get(task_id), batch)
        if events is None:
            plan.unknown_status.append(m.key)
            continue
        plan.events.extend(events)
        plan.reports += 1
    return plan


def apply_import(root: Path, plan: ImportPlan) -> dict[str, int]:
    """Land the plan through normal ingest. Returns outcome counts —
    ``duplicate`` on a re-run is the idempotency proof, not a failure."""
    from .emit_api import emit_batch
    counts = {"committed": 0, "duplicate": 0, "spooled": 0}
    if plan.events:
        for outcome in emit_batch(root, plan.events):
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts
