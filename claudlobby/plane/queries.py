"""The Lane C derivation queries — ONE definition (round-6 F7).

Parameter contract, fixed: WORKSTREAM_STATUS_SQL binds (now, cutoff) —
renewal horizons compare against NOW ("renewed UNTIL" means until: an
expired renewal protects nothing — round-6 counterexample), while activity
recency compares against CUTOFF = now − policy_window. Both latest-by-
ingest_seq: ledger order is authoritative, producer timestamps may arrive
out of order. ATTENTION_SQL binds (overdue_cutoff,).
"""

from __future__ import annotations

TERMINAL_TASK_EVENTS = (
    "completed", "failed", "cancelled", "returned_blocked",
    "superseded", "reassigned", "expired",
)
_TERMINAL = ",".join(f"'{e}'" for e in TERMINAL_TASK_EVENTS)

ATTENTION_SQL = (
    "SELECT a.assignment_id FROM assignments a"
    " WHERE NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"   AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL}))"
    " AND (NOT EXISTS (SELECT 1 FROM events e WHERE"
    "   e.kind='transmission' AND e.msg_id = a.dispatch_msg_id"
    "   AND e.event='recipient_acknowledged')"
    "  OR a.expected_by < ?)"
)

TASK_STATUS_SQL = (
    "SELECT a.assignment_id, COALESCE("
    " (SELECT t.event FROM events t WHERE t.kind='task'"
    f"  AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL})"
    "  ORDER BY t.ingest_seq LIMIT 1),"
    " (SELECT t.event FROM events t WHERE t.kind='task'"
    "  AND t.assignment_id = a.assignment_id"
    "  ORDER BY t.ingest_seq DESC LIMIT 1),"
    " 'open') AS status FROM assignments a"
)

WORKSTREAM_STATUS_SQL = (
    "SELECT w.workstream_id, CASE"
    " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
    "   AND c.workstream_id = w.workstream_id AND c.event='archived')"
    "   THEN 'archived'"
    " WHEN EXISTS (SELECT 1 FROM events c WHERE c.kind='workstream'"
    "   AND c.workstream_id = w.workstream_id AND c.event='closed')"
    "   THEN 'closed'"
    " WHEN (SELECT e.event FROM events e WHERE e.kind='workstream'"
    "   AND e.workstream_id = w.workstream_id"
    "   AND e.event IN ('blocked','unblocked')"
    "   ORDER BY e.ingest_seq DESC LIMIT 1) = 'blocked' THEN 'blocked'"
    " WHEN COALESCE((SELECT e.renewed_until FROM events e"
    "   WHERE e.kind='workstream' AND e.event='renewed'"
    "   AND e.workstream_id = w.workstream_id"
    "   ORDER BY e.ingest_seq DESC LIMIT 1), '') < ?"
    "  AND COALESCE((SELECT e.occurred_at FROM events e"
    "   WHERE e.kind='workstream'"
    "   AND e.workstream_id = w.workstream_id"
    "   ORDER BY e.ingest_seq DESC LIMIT 1), w.occurred_at) < ?"
    "   THEN 'stale'"
    " ELSE 'active' END AS status FROM workstreams w"
)

RECONCILIATION_SQL = (
    "SELECT COUNT(*) FROM events s WHERE s.kind='transmission'"
    " AND s.event='pane_submitted' AND NOT EXISTS"
    " (SELECT 1 FROM events a WHERE a.kind='transmission'"
    "  AND a.msg_id = s.msg_id AND a.event='recipient_acknowledged')"
)


import re as _re

def events_aliases(sql: str) -> frozenset[str]:
    """Every name the `events` table can appear under in this query —
    round-7: SQLite's EXPLAIN QUERY PLAN prints "SCAN e" for an aliased
    table, so a detector matching only "SCAN events" waves through the
    exact unindexed scan it exists to catch."""
    names = {"events"}
    for m in _re.finditer(r"\b(?:FROM|JOIN)\s+events\s+(?:AS\s+)?([A-Za-z_]\w*)",
                          sql, _re.I):
        alias = m.group(1)
        if alias.upper() not in {"WHERE", "ON", "GROUP", "ORDER", "LEFT",
                                  "JOIN", "AS", "SET"}:
            names.add(alias)
    return frozenset(names)


def is_bare_events_scan(plan_detail: str, aliases: frozenset[str]) -> bool:
    """True for an UNindexed full scan of events (under any alias). An
    index-assisted "SCAN x USING ... INDEX ..." is not bare."""
    tokens = plan_detail.split()
    if len(tokens) < 2 or tokens[0] != "SCAN":
        return False
    # SQLite < 3.36 prints "SCAN TABLE events [AS e]"; newer prints
    # "SCAN events" / "SCAN e" — post-review fix: the old form made the
    # detector silently pass on exactly the older-sqlite hosts (Pi class)
    # the bench gate targets.
    name = tokens[2] if tokens[1] == "TABLE" and len(tokens) >= 3 else tokens[1]
    return name in aliases and "USING" not in plan_detail
