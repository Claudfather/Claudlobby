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

# --- Registry lane, the READ half (chunk B; spec §9 lines 143-146) --------
#
# THE F11 VALIDATION HALF IS ENFORCED HERE AND ONLY HERE: a tombstone is
# honored only when a scan_completed declaration with the SAME scan_id and
# complete=true exists — the join is by scan_id, never by time. The emitter
# enforces prevention (incomplete enumerations never tombstone); this CTE is
# the reader's half, shared by all three registry queries so the two halves
# cannot drift apart per-query. Snapshots are never completion-gated: the
# hash gate self-heals state, the completion event exists to make ABSENCE
# trustworthy. `complete` is stored as canonical JSON true/false, which
# json_extract yields as 1/0 (pinned).
_F11_COMPLETION_JOIN = (
    "EXISTS (SELECT 1 FROM events d WHERE d.kind='declaration'"
    " AND d.event='scan_completed'"
    " AND json_extract(d.detail,'$.scan_id') = rs.scan_id"
    " AND json_extract(d.detail,'$.complete') = 1)"
)

_REG_EFFECTIVE = (
    "effective AS ("
    " SELECT rs.*,"
    "  (rs.tombstone = 0 OR " + _F11_COMPLETION_JOIN + ") AS f11_valid"
    " FROM registry_snapshots rs)"
)

# Current state per SCD partition: the latest F11-valid row wins; when that
# row is a (valid) tombstone the entity is absent from current. An INVALID
# tombstone is not merely demoted — it is excluded, so the prior snapshot
# remains current. Ordering is (occurred_at, ingest_seq), the spec's ONE
# SCD ordering (line 145: first-hand snapshots carry null observed_at, and
# ingest_seq breaks producer-timestamp ties) — current is simply its tail.
REG_CURRENT_SQL = (
    "WITH " + _REG_EFFECTIVE + ", latest AS ("
    " SELECT e.*, ROW_NUMBER() OVER ("
    "   PARTITION BY e.host_uid, e.entity_type, e.entity_uid"
    "   ORDER BY e.occurred_at DESC, e.ingest_seq DESC) AS rn"
    " FROM effective e WHERE e.f11_valid = 1)"
    " SELECT host_uid, entity_type, entity_uid, entity_alias, payload,"
    " payload_hash, cause, scan_id, vault_rev, occurred_at, ingest_seq"
    " FROM latest WHERE rn = 1 AND tombstone = 0"
    " ORDER BY entity_type, entity_alias"
)

# Point form of REG_CURRENT for WRITE-SIDE suppression decisions (chunk-B
# gauntlet, probed SEV-1): "is this entity effectively deleted?" must mean
# exactly what the READER means by absent. The write side used to key its
# tombstone dedup on a tombstone row merely EXISTING — so one crashed
# scan's invalid tombstone suppressed every later valid deletion, a
# permanent false PRESENT in the exact case F11 exists for. A second
# spelling of "current" here would re-open that split.
REG_ENTITY_IS_CURRENT_SQL = (
    "SELECT 1 FROM (" + REG_CURRENT_SQL + ")"
    " WHERE host_uid = ? AND entity_type = ? AND entity_uid = ? LIMIT 1"
)

# SCD2 windows: each F11-valid row opens at its occurred_at and closes at
# the partition's next row (NULL = still open). Tombstone rows appear as
# window-openers of the deleted period — the reader renders them, never
# filters them, or deletion vanishes from history.
REG_HISTORY_SQL = (
    "WITH " + _REG_EFFECTIVE +
    " SELECT e.host_uid, e.entity_type, e.entity_uid, e.entity_alias,"
    " e.tombstone, e.payload, e.payload_hash, e.cause, e.scan_id,"
    " e.occurred_at AS valid_from,"
    " LEAD(e.occurred_at) OVER ("
    "   PARTITION BY e.host_uid, e.entity_type, e.entity_uid"
    "   ORDER BY e.occurred_at, e.ingest_seq) AS valid_to,"
    " e.ingest_seq"
    " FROM effective e WHERE e.f11_valid = 1"
    " ORDER BY e.entity_type, e.entity_alias, e.occurred_at, e.ingest_seq"
)

# Consecutive rows in a partition ARE the diff view (spec line 143). This
# pairs each row with its predecessor; the FIELD-level diff is computed by
# registry_read.diff_fields at read time — payloads are nested JSON and a
# json_each diff in SQL would re-implement canonicalization badly.
# First-in-partition rows are INCLUDED (both prev columns null) and render
# as first_observed — spec's own derivation name, and a new entity is
# prime drift signal (chunk-B gauntlet: the old WHERE silently dropped
# exactly those rows from --changes).
REG_CHANGES_SQL = (
    "WITH " + _REG_EFFECTIVE + ", ordered AS ("
    " SELECT e.*,"
    "  LAG(e.payload) OVER w AS prev_payload,"
    "  LAG(e.payload_hash) OVER w AS prev_hash,"
    "  LAG(e.tombstone) OVER w AS prev_tombstone"
    " FROM effective e WHERE e.f11_valid = 1"
    " WINDOW w AS (PARTITION BY e.host_uid, e.entity_type, e.entity_uid"
    "   ORDER BY e.occurred_at, e.ingest_seq))"
    " SELECT entity_type, entity_alias, entity_uid, tombstone, payload,"
    " prev_payload, prev_tombstone, cause, scan_id,"
    " occurred_at, ingest_seq"
    " FROM ordered"
    " ORDER BY ingest_seq DESC"
)

# Trust: tombstones the F11 join does NOT validate. Nonzero means a scan
# died between its tombstones and its completion (or emitted incomplete) —
# the reader is already ignoring them; doctor surfaces that they exist.
REG_INVALID_TOMBSTONES_SQL = (
    "SELECT entity_type, entity_alias, scan_id, occurred_at, ingest_seq"
    " FROM registry_snapshots rs WHERE rs.tombstone = 1"
    " AND NOT " + _F11_COMPLETION_JOIN +
    " ORDER BY ingest_seq DESC"
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
