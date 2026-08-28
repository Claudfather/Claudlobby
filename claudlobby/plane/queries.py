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

# §6b #1/#2 (PR-B): activation derives from CARRIER-APPROPRIATE evidence —
# submission-class rows (pane_submitted / carrier_accepted) occupy the
# activation rung, because submission is the strongest fact the tmux carrier
# can ever yield; a real recipient_acknowledged row TIGHTENS where a door
# observed one, and is never inferred. carrier_queued is NOT activation
# (accepted-but-parked behind a busy turn). And a missing producer must fail
# toward EMPTY, never toward everything: the old attention predicate
# (NOT EXISTS ack) inverted to all-alarm with zero producers.
#
# ONE definition (gauntlet round; the TERMINAL_TASK_EVENTS pattern above):
# ATTENTION_SQL and TASK_STATUS_SQL must mean the SAME thing by "activated" —
# two byte-identical constants under two names is how a promoted/demoted
# token (this PR moved carrier_queued and recipient_acknowledged) splits
# the attention view from the status view silently.
ACTIVATION_TX_EVENTS = (
    "pane_submitted", "carrier_accepted", "recipient_acknowledged",
)
_TX_ACTIVATION = ",".join(f"'{e}'" for e in ACTIVATION_TX_EVENTS)

ATTENTION_SQL = (
    # Attention = non-terminal AND (evidence of dispatch trouble OR overdue).
    # Trouble is EVIDENCE-BASED: transmission rows exist for the dispatch, yet
    # none reached activation — a send that failed or sits queued. An
    # assignment with NO transmission rows at all is a producer gap (or a
    # pre-doors import), which is silence, not alarm (§6b #2).
    "SELECT a.assignment_id FROM assignments a"
    " WHERE NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"   AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL}))"
    " AND ((EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
    "        AND e.msg_id = a.dispatch_msg_id)"
    "   AND NOT EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
    "        AND e.msg_id = a.dispatch_msg_id"
    f"        AND e.event IN ({_TX_ACTIVATION})))"
    "  OR a.expected_by < ?)"
)

# Attempt-status ladder below the task-event tiers (§8 + §6b #1): activation
# derives through assignments.dispatch_msg_id -> transmission rows, from
# carrier-appropriate evidence. SUBMISSION-CLASS rows (pane_submitted /
# carrier_accepted) occupy the 'open' rung — submission is the strongest fact
# tmux can yield; a real recipient_acknowledged row lands on the same rung as
# the tightening case. carrier_queued and unresolved attempts (send_attempted/
# unknown/duplicate_suppressed with no 'failed' verdict on that attempt_no)
# read pending_unacknowledged — a retry in flight after a failed first attempt
# must not report dispatch_failed, while an attempt whose own verdict IS
# 'failed' must.
_TX_OPEN = _TX_ACTIVATION   # the SAME activation set — never a second copy
_TX_UNRESOLVED = "'send_attempted','carrier_queued','unknown','duplicate_suppressed'"

TASK_STATUS_SQL = (
    "SELECT a.assignment_id, COALESCE("
    " (SELECT t.event FROM events t WHERE t.kind='task'"
    f"  AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL})"
    "  ORDER BY t.ingest_seq LIMIT 1),"
    # supplied_id_not_open is a JOIN anomaly, not a lifecycle state (#1372
    # review F11) — it must never surface as an assignment's visible status.
    " (SELECT t.event FROM events t WHERE t.kind='task'"
    "  AND t.assignment_id = a.assignment_id"
    "  AND t.event <> 'supplied_id_not_open'"
    "  ORDER BY t.ingest_seq DESC LIMIT 1),"
    " CASE"
    "  WHEN a.dispatch_msg_id IS NULL THEN 'created_not_sent'"
    "  WHEN EXISTS (SELECT 1 FROM events x WHERE x.kind='transmission'"
    "    AND x.msg_id = a.dispatch_msg_id"
    f"    AND x.event IN ({_TX_OPEN})) THEN 'open'"
    "  WHEN EXISTS (SELECT 1 FROM events x WHERE x.kind='transmission'"
    "    AND x.msg_id = a.dispatch_msg_id"
    f"    AND x.event IN ({_TX_UNRESOLVED})"
    "    AND NOT EXISTS (SELECT 1 FROM events y WHERE y.kind='transmission'"
    "      AND y.msg_id = x.msg_id AND y.attempt_no = x.attempt_no"
    "      AND y.event='failed')) THEN 'pending_unacknowledged'"
    "  WHEN EXISTS (SELECT 1 FROM events x WHERE x.kind='transmission'"
    "    AND x.msg_id = a.dispatch_msg_id"
    "    AND x.event='failed') THEN 'dispatch_failed'"
    "  ELSE 'created_not_sent'"
    " END) AS status FROM assignments a"
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
