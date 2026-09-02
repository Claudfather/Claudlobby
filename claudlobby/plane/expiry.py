"""Attention expiry — the queue's aging sweep (the operator's "are these
open tasks?" question, answered by policy rather than by a card that
lingers forever).

ATTENTION_SQL lists a non-terminal assignment for as long as it is overdue
(``expected_by`` in the past) — and NOTHING ever ends that: the vocabulary
has carried a terminal ``expired`` task event since Phase 1, but no door
emits it, so a dispatch nobody closed sits in the queue for weeks, burying
what needs the operator NOW under what needed them last Tuesday.

This sweep emits ``expired`` for assignments whose deadline passed more
than a policy horizon ago and that still have no terminal task event. Its
laws:

  - **A Lane-B FACT through normal ingest, never a table write.** Status
    is always derived (contract × events × clock × policy); the sweep adds
    the event that lets the derivation say "expired", it edits nothing.
  - **Idempotent by construction.** The query excludes anything already
    terminal, so a second run finds nothing — no dedupe bookkeeping.
  - **Deadline-only.** Trouble-class attention (a dispatch that never
    activated, no deadline passed) is a human's to judge; this door never
    expires it. And an assignment whose fleet cannot be attributed is
    SKIPPED and disclosed — never emitted under a fabricated fleet.
  - **Dormant and self-gated** (``PLANE_EXPIRE_ENABLED``), like the
    retention door: an event-emitting policy sweep must not arrive
    switched on via a root pull.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .queries import TERMINAL_TASK_EVENTS

DEFAULT_AFTER_DAYS = 7
_TERMINAL = ",".join(f"'{e}'" for e in TERMINAL_TASK_EVENTS)

EXPIRABLE_SQL = (
    "SELECT a.assignment_id, a.work_item_id, a.expected_by, f.alias AS fleet"
    " FROM assignments a"
    " LEFT JOIN identity_registry f ON f.uid = a.fleet_uid"
    " WHERE a.expected_by IS NOT NULL AND a.expected_by < ?"
    " AND NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"   AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL}))"
    " ORDER BY a.expected_by"
)


@dataclass
class ExpiryPlan:
    cutoff: str
    rows: list           # attributable assignments to expire
    unattributed: list   # assignment_ids with no fleet alias — skipped, disclosed


def expirable(conn, *, now: datetime | None = None,
              after_days: int = DEFAULT_AFTER_DAYS) -> ExpiryPlan:
    if now is None:
        now = datetime.now(timezone.utc)
    if after_days < 0:
        raise ValueError("expiry horizon cannot be negative")
    cutoff = (now - timedelta(days=after_days)).isoformat()
    seen: dict[str, dict] = {}
    for r in conn.execute(EXPIRABLE_SQL, (cutoff,)):
        d = dict(zip(("assignment_id", "work_item_id", "expected_by", "fleet"), r))
        seen[d["assignment_id"]] = d      # last row wins on a replayed id
    rows = [d for d in seen.values() if d["fleet"]]
    unattributed = [d["assignment_id"] for d in seen.values() if not d["fleet"]]
    return ExpiryPlan(cutoff=cutoff, rows=rows, unattributed=unattributed)


def expired_events(plan: ExpiryPlan, *, now: datetime | None = None,
                   after_days: int = DEFAULT_AFTER_DAYS) -> list[dict]:
    """The task events the sweep emits — the SAME wire shape every door
    uses (the 6b fixture's), so ingest/contracts are the only gate."""
    if now is None:
        now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    return [{
        "event_type": "task", "emitter": "attention-expiry",
        "fleet": r["fleet"], "occurred_at": stamp,
        "payload": {
            "work_item_id": r["work_item_id"],
            "assignment_id": r["assignment_id"],
            "event": "expired",
            "summary": (f"expired by the attention sweep: >{after_days}d past"
                        f" expected_by {r['expected_by']}"),
        }} for r in plan.rows]
