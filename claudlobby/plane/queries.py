"""The Lane C derivation queries — ONE definition (round-6 F7).

Parameter contract, fixed: WORKSTREAM_STATUS_SQL binds (now, cutoff) —
renewal horizons compare against NOW ("renewed UNTIL" means until: an
expired renewal protects nothing — round-6 counterexample), while activity
recency compares against CUTOFF = now − policy_window. Both latest-by-
ingest_seq: ledger order is authoritative, producer timestamps may arrive
out of order. ATTENTION_SQL and ATTENTION_ARMS_SQL bind ONE value per
`?`-bearing arm, in ARM ORDER, through `attention_params` /
`attention_arms_params` (chunk M-A, #1481) — the arms-with-columns form binds
the same tuple TWICE, the columns then the filter. Positional binds were
hand-written at five call sites while there was exactly one `?`; a second
arm made a mis-ordered pair a silently WRONG answer rather than an error, so
the tuple is derived from one `now` in one place.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TERMINAL_TASK_EVENTS = (
    "completed", "failed", "cancelled", "returned_blocked",
    "superseded", "reassigned", "expired",
)
_TERMINAL = ",".join(f"'{e}'" for e in TERMINAL_TASK_EVENTS)

# ONE definition of "this assignment is still open" — ATTENTION_SQL and the
# attention-expiry sweep must agree on it, or a card can sit in the queue
# that the sweep considers closed (or vice versa). `a` is the assignments
# alias in the enclosing query.
NON_TERMINAL_CLAUSE = (
    " NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"   AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL}))"
)

# The plane's OPEN SET for one assignee AS OF an instant (cutover chunk 3, the
# shadow primitive). Deliberately NOT NON_TERMINAL_CLAUSE: that closes per
# ASSIGNMENT (attention and expiry's question), while the legacy list reader
# being shadowed closes per (bot, TASK ID) — a redispatched task id with one
# terminal report closes every row that carried it — so this closes an
# assignment when a terminal task event by the instant names it OR a sibling
# assignment of the same assignee with the same dispatch-log source_ref.
# Params: (assignee_uid, at, at, at, at, at, at) — the assignee resolved to a
# uid by the caller on the small registry (case-insensitively), so the read
# hits idx_assignments_assignee; `at` None = unbounded (everything landed).
# --- the fleet axis of an alias, in SQL (U) -----------------------------------
# `bot:<fleet>/<name>`: an actor's fleet is the segment between `bot:` and the
# first `/`. As a predicate it is a RANGE over the alias index — case-sensitive
# like the equality arms the room queries bind (`fleet_uid`, `recipient_fleet`),
# so one string means one fleet on every arm (a LIKE arm is ASCII-case-
# insensitive and let `Eng` count `eng`'s bots while the room kept them apart),
# seekable, and free of LIKE metacharacters (a fleet named `en_` cannot absorb
# `eng`). Bind the fleet TWICE (`fleet_range_params`). Python-side, the same
# rule is inventory.fleet_of.
def fleet_alias_range(col: str = "alias") -> str:
    return f"({col} >= 'bot:' || ? || '/' AND {col} < 'bot:' || ? || '0')"


def fleet_range_params(fleet: str) -> tuple[str, str]:
    return (fleet, fleet)


# `_`-prefixed aliases are scope SENTINELS (the `_host` fleet the host probe
# emits under — a host job has no real fleet), never participants: the one
# spelling every rail, roster and fleet-list read applies.
def not_sentinel_sql(col: str = "alias") -> str:
    return f"{col} NOT LIKE '\\_%' ESCAPE '\\'"


# --- a fleet's reports, and its read position (chunk K, #1467) -----------------
# THE definition of "a fleet's reports": report-class communications on the
# fleet's ROOM AXIS — sent by the fleet (fleet_uid) or addressed to it
# (recipient_fleet: a worker on another fleet reporting to this fleet's manager
# is this fleet's report). brief's unacked list, `claudlobby report-back` and
# the overview card all read this text (lib/plane-readers.py carries the
# byte-identical copy, pinned): a second population let the card count a report
# the manager's brief never showed, which no ack could clear. Binds
# (fleet_uid, fleet_alias, since, since, since_seq, since_seq).
FLEET_REPORTS_SQL = (
    "SELECT c.occurred_at, c.msg_id, c.sender_uid, c.sender_alias, c.body, c.source_ref,"
    " c.ingest_seq"
    " FROM communications c"
    " WHERE c.message_class = 'report' AND (c.fleet_uid = ? OR c.recipient_fleet = ?)"
    " AND (? IS NULL OR c.occurred_at >= ?) AND (? IS NULL OR c.ingest_seq > ?)"
    " ORDER BY c.occurred_at, c.ingest_seq"
)

# The newest READABLE `reports_acked` event among a set of uids (subject OR
# actor — an ack lands on the viewer's own actor; the uids span every alias
# variant the bot minted, the R2a rule). Decoded by SQLite: a row whose detail
# carries no integer cursor is not an ack and is skipped — never a reset to
# "never acked" that erases an older valid one (adversarial lens). Format with
# ph = the uid placeholders, bind the uids twice.
NEWEST_ACK_SQL = (
    "SELECT json_extract(e.detail, '$.acked_through_seq') AS seq,"
    " json_extract(e.detail, '$.acked_through_ts') AS ts,"
    " json_extract(e.detail, '$.count') AS count,"
    " e.occurred_at AS acked_at, e.subject_alias AS acked_by, e.ingest_seq AS landed_seq"
    " FROM events e"
    " WHERE e.kind = 'system' AND e.event = 'reports_acked'"
    " AND (e.subject_uid IN ({ph}) OR e.actor_uid IN ({ph}))"
    " AND json_type(e.detail, '$.acked_through_seq') = 'integer'"
    " ORDER BY e.ingest_seq DESC LIMIT 1"
)

OPEN_ASSIGNMENTS_AT_SQL = (
    "SELECT a.occurred_at, a.source_ref, a.assignment_id, a.expected_by"
    " FROM assignments a"
    " WHERE a.assignee_uid = ? AND (? IS NULL OR a.occurred_at <= ?)"
    "  AND NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"    AND t.event IN ({_TERMINAL}) AND (? IS NULL OR t.occurred_at <= ?)"
    "    AND (t.assignment_id = a.assignment_id"
    "      OR (a.source_ref LIKE 'dispatch-log:%' AND a.source_ref NOT LIKE 'dispatch-log:sha:%'"
    "          AND t.assignment_id IN (SELECT s.assignment_id FROM assignments s"
    "            WHERE s.assignee_uid = a.assignee_uid AND s.source_ref = a.source_ref"
    "              AND (? IS NULL OR s.occurred_at <= ?)))))"
    " ORDER BY a.occurred_at, a.ingest_seq"
)

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

# The ARMS of attention, one definition each (chunk L fold, #1479). Attention
# = non-terminal AND (evidence of dispatch trouble OR overdue). Trouble is
# EVIDENCE-BASED: transmission rows exist for the dispatch, yet none reached
# activation — a send that FAILED or one that sits queued. An assignment with
# NO transmission rows at all is a producer gap (or a pre-doors import), which
# is silence, not alarm (§6b #2).
#
# The two trouble arms partition that one predicate on whether a `failed`
# transmission exists, because the operator's remedy differs: a failed send is
# a carrier fact to re-send, a never-activated one may be a bot that is down.
# Their union is byte-equivalent to the old single arm (a `failed` row IS a
# transmission row), so ATTENTION_SQL's population is unchanged.
_TX_EXISTS = ("EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
              " AND e.msg_id = a.dispatch_msg_id)")
_TX_ACTIVATED = ("EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
                 " AND e.msg_id = a.dispatch_msg_id"
                 f" AND e.event IN ({_TX_ACTIVATION}))")
_TX_FAILED = ("EXISTS (SELECT 1 FROM events e WHERE e.kind='transmission'"
              " AND e.msg_id = a.dispatch_msg_id AND e.event='failed')")

# --- the HUMAN arms (chunk M-A, #1481) ----------------------------------------
# Two acts a person takes on ONE task, both already just task events: a
# manager's `escalated` (it asks the human a question, and is NON-terminal —
# the task stays open while the human decides) and the operator's `nudged`
# (it asks the manager to act). Each holds only while it is the assignment's
# NEWEST task event, so a later `progress`, a re-dispatch's `superseded`, a
# withdrawal's `cancelled` or any terminal event clears it with no second
# door and no state to reconcile. "Newest" is TASK_STATUS_SQL's own rule —
# latest by ingest_seq.
#
# WHICH tokens that window IGNORES is the whole correctness of the escalation
# arm, and the M-A fold (F1) found it one token short. `supplied_id_not_open`
# was already excluded as a JOIN anomaly rather than a lifecycle state (#1372
# review F11); `nudged` has to be excluded from the ESCALATION's window for a
# different reason, and the reason is what makes it a rule instead of a patch:
# **a nudge is an ASK, not an ANSWER.** It asks the manager to act on the row
# and changes nothing about the row itself, so it cannot be what ends the
# manager's own raise. Left in, the sequence escalate → nudge silently
# extinguished the escalation — off the card, off `--escalated`, off the
# operator's queue, permanently, because nothing ever re-raises it (measured
# on a throwaway plane, both surfaces). The ruling enumerates what DOES clear
# an escalation — progress, a terminal report, a withdrawal, a supersede —
# and a nudge is not among them.
#
# The nudge's OWN arm needs the opposite window (a nudge is exactly what it
# looks for), so there are two, and they differ by that one token.
NEWEST_TASK_IGNORED = ("supplied_id_not_open",)
ESCALATION_IGNORED = NEWEST_TASK_IGNORED + ("nudged",)


def _newest_task(col: str, ignore: tuple = NEWEST_TASK_IGNORED) -> str:
    skip = ",".join(f"'{e}'" for e in ignore)
    return (f"(SELECT {col} FROM events n WHERE n.kind='task'"
            "  AND n.assignment_id = a.assignment_id"
            f"  AND n.event NOT IN ({skip})"
            "  ORDER BY n.ingest_seq DESC LIMIT 1)")


_NEWEST_EVENT = _newest_task("n.event")
_NEWEST_AT = _newest_task("n.occurred_at")
_NEWEST_BY = _newest_task("json_extract(n.detail, '$.by')")
_NEWEST_ANSWER = _newest_task("n.event", ESCALATION_IGNORED)
_NEWEST_ANSWER_AT = _newest_task("n.occurred_at", ESCALATION_IGNORED)

# A nudge is an ASK, and a manager is owed a moment to act on it, so a nudge
# is not attention until it has gone unanswered past this. Named because the
# card's own wording quotes it ("nudged 40m ago by chris, no act yet"), and
# because a bare literal inside the SQL string is unquotable there.
NUDGE_GRACE_S = 30 * 60


def _epoch(expr: str) -> str:
    """An instant compared as an INSTANT, not as text (the M-A fold, F7).

    The ledger stores `occurred_at` / `expected_by` as the emitter's own
    isoformat, offset and all (`ingest` writes `.isoformat()` of an aware
    datetime), so `-04:00` and `+00:00` rows sit side by side in one column.
    A lexical `<` then compares the wall-clock DIGITS: a nudge stamped ten
    minutes ago at `-04:00` sorts before a `+00:00` cutoff four hours older
    and read as past the grace — reproduced, and it would have paged an
    operator about a nudge nobody had ignored. `strftime('%s', …)` is the
    same lesson `plane-readers.since_form` teaches on the read side (normalise
    before comparing), applied where the STORED form cannot be normalised
    after the fact. SQLite parses the offset suffix and fractional seconds
    (probed); an unparseable instant yields NULL, so an arm fails OFF rather
    than firing on garbage — which ingest's AwareDatetime makes unreachable
    anyway."""
    return f"CAST(strftime('%s', {expr}) AS INTEGER)"


# (name, predicate, since_expr, window) — the ONE list both surfaces below are
# built from, so a new arm cannot reach the queue without also reaching the
# column the card renders its reason from, the column it dates that reason by,
# and (for a human arm) the columns carrying what the human said (the fold's
# rule, F12: a new arm forces a new column, the date included — the date used
# to live in a second list plus an if/elif ladder in the view, which is
# exactly the shape that lets an arm ship undated).
#
# `window` is the newest-task-event window the arm and its facts are read in.
# It is per-arm rather than global because F1 made the two human arms
# genuinely disagree: the escalation's window must SKIP a later nudge (a nudge
# does not answer a raise) while the nudge's window must not (a nudge is what
# it looks for). A global window gets one of the two wrong, and dating or
# quoting an arm through the OTHER arm's window is the same defect one step
# on — an escalation dated by the nudge that followed it, quoting the nudge's
# words as the question.
#
# ORDER IS THE OPERATOR'S PRIORITY (a human waiting on an answer outranks a
# carrier fault, which outranks a clock) and also the `?` order of
# ATTENTION_ARMS_SQL's columns — bind through `attention_params`, never by
# hand.
ATTENTION_ARMS = (
    ("escalated", f"({_NEWEST_ANSWER} = 'escalated')",
     _NEWEST_ANSWER_AT, ESCALATION_IGNORED),
    ("send_failed", f"({_TX_FAILED} AND NOT {_TX_ACTIVATED})",
     "a.occurred_at", None),
    ("never_activated",
     f"({_TX_EXISTS} AND NOT {_TX_ACTIVATED} AND NOT {_TX_FAILED})",
     "a.occurred_at", None),
    ("nudged",
     f"({_NEWEST_EVENT} = 'nudged' AND {_epoch(_NEWEST_AT)} < {_epoch('?')})",
     _NEWEST_AT, NEWEST_TASK_IGNORED),
    ("overdue", f"({_epoch('a.expected_by')} < {_epoch('?')})",
     "a.expected_by", None),
)

# The arms a PERSON raised, and therefore the only arms that carry the fact
# columns below (the fold's F10). The old single `arm_*` set read the row's
# newest task event whether or not that event was what the LEADING arm was
# about, so an OVERDUE row carrying a still-in-grace nudge reported
# `attention_by: chris` under a reason line that never mentions a person — a
# fact attached to the wrong claim. The nudge is not lost by narrowing this:
# it rides `NUDGE_STATE_SQL` below, where it is true.
HUMAN_ARMS = ("escalated", "nudged")

# WHAT the human said, off the detail of the arm's OWN newest event: the
# escalation's question is the whole point of its card ("needs you: <q>"), and
# `by` / `reason` name the person and their words. One list of detail KEYS,
# expanded per human arm into `<arm>_<key>`, so the columns and the arm they
# belong to cannot drift apart and no consumer re-derives them (the chunk-L
# fold's F2). None carries a `?`, so the bind contract stays the arms'; each
# is NULL where that event has no such detail — including where a
# metadata-mode capture dropped the prose, which the card renders as "not
# recorded" rather than inventing one.
ATTENTION_ARM_FACTS = ("question", "by", "reason")
_ARM_FACT_COLS = tuple(
    (f"{name}_{key}",
     _newest_task(f"json_extract(n.detail, '$.{key}')", window))
    for name, _sql, _at, window in ATTENTION_ARMS if name in HUMAN_ARMS
    for key in ATTENTION_ARM_FACTS
)
_ARM_FILTER = "(" + " OR ".join(sql for _, sql, _at, _w in ATTENTION_ARMS) + ")"


def nudge_cutoff(now: str) -> str:
    """The instant a nudge must PREDATE to be an unanswered one — `now` less
    the grace. Compared as an instant (`_epoch`), so its offset is its own."""
    return (datetime.fromisoformat(now.replace("Z", "+00:00"))
            - timedelta(seconds=NUDGE_GRACE_S)).isoformat()


def attention_params(now: str) -> tuple:
    """ATTENTION_SQL's binds: ONE value per `?`-bearing arm, in ARM ORDER.
    Both derive from a single `now`, so the queue can never read the nudge
    grace off one clock and the deadline off another."""
    return (nudge_cutoff(now), now)


def attention_arms_params(now: str) -> tuple:
    """ATTENTION_ARMS_SQL's binds: the arm COLUMNS, then the same arms again
    inside the filter."""
    return attention_params(now) * 2


_NON_TERMINAL_A = (
    "NOT EXISTS (SELECT 1 FROM events t WHERE t.kind='task'"
    f"   AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL}))")

ATTENTION_SQL = (
    "SELECT a.assignment_id FROM assignments a"
    f" WHERE {_NON_TERMINAL_A}"
    f" AND {_ARM_FILTER}"
)

# The same rows, each stamped with WHICH arms hold and the facts those arms
# are about — so the card can say "send failed 12h ago", "overdue 2h" or
# "needs you: <question>" instead of a bare flag, and no consumer re-derives
# an arm in Python beside the query that selected it (the fold's F2:
# `_fetch_tasks` had its own second derivation and `_fetch_overview` a
# third). Binds `attention_arms_params(now)`: the arm COLUMNS, then the same
# arms inside the filter.
#
# Each arm contributes TWO columns — `<name>` (does it hold) and `<name>_at`
# (the instant that arm is about) — so `since` is a lookup, never a ladder.
ATTENTION_ARMS_SQL = (
    "SELECT a.assignment_id,"
    + ",".join(f" {sql} AS {name}, {at} AS {name}_at"
               for name, sql, at, _w in ATTENTION_ARMS)
    + ","
    + ",".join(f" {sql} AS {name}" for name, sql in _ARM_FACT_COLS)
    + " FROM assignments a"
    f" WHERE {_NON_TERMINAL_A}"
    f" AND {_ARM_FILTER}"
)

# The row's OUTSTANDING NUDGE, whether or not the grace has passed (the fold's
# F11). The arm above only fires past the grace, but the operator asking "did
# anyone poke this?" is owed the answer in the first thirty minutes too — that
# is the window in which the manager is still expected to act, and the one the
# card can most usefully show. Same window as the arm (a nudge stays
# outstanding only while it is the newest task event, so any later act ends
# it), minus the clock. Selected for the rows the board already has, so the
# caller appends its own ` AND a.assignment_id IN (…)`.
NUDGE_STATE_SQL = (
    "SELECT a.assignment_id,"
    f" {_NEWEST_AT} AS nudged_at,"
    f" {_NEWEST_BY} AS nudged_by"
    " FROM assignments a"
    f" WHERE {_NEWEST_EVENT} = 'nudged'"
)

# --- resolving ONE task a human names (chunk M-A fold, F6) --------------------
# "The open assignments carrying this task id" existed three times — the
# stdlib reader's `TASK_OPEN_SQL`, `commands/task.py`'s `OPEN_BY_TASK_SQL`,
# and the shape both refusal ladders reason about. Three spellings of one
# question is how a door starts refusing an id another door acts on. Here it
# is once; `lib/plane-readers.py` carries the byte-identical stdlib twin, the
# `OPEN_ASSIGNMENTS_AT_SQL` pattern, pinned by test.
#
# There is deliberately NO fleet or assignee scope in the SQL. An operator may
# nudge or withdraw ANY task they can name — they are not a member of a fleet
# — and the sender's roster does not hold a cross-fleet worker (44.6% of
# dispatch traffic). Ambiguity is answered by naming the assignment
# (`--assignment`, the fold's F5), not by silently narrowing the search to the
# caller's own fleet and acting on whatever survives.
#
# Columns are the union the two callers need, so one row serves the CLI's
# re-check message (title, age, assignee, the task's own manager) and the bash
# door's refusal listing (dispatch_msg_id, assignee, fleet). Newest first:
# the ORDER only decides how candidates are LISTED, never which one is acted
# on — a single match is the only thing either door acts on.
_OPEN_ROW_SELECT = (
    "SELECT a.work_item_id, a.assignment_id, a.dispatch_msg_id, a.occurred_at,"
    " w.title, i.alias AS assignee, m.alias AS assigned_by, f.alias AS fleet"
    " FROM assignments a"
    " LEFT JOIN work_items w ON w.work_item_id = a.work_item_id"
    " LEFT JOIN identity_registry i ON i.uid = a.assignee_uid"
    " LEFT JOIN identity_registry m ON m.uid = a.assigned_by_uid"
    " LEFT JOIN identity_registry f ON f.uid = a.fleet_uid"
)
OPEN_BY_TASK_REF_SQL = (
    _OPEN_ROW_SELECT + " WHERE a.source_ref = ? AND" + NON_TERMINAL_CLAUSE
    + " ORDER BY a.ingest_seq DESC")

# `--assignment <asg_id>` — the remedy the ambiguity refusal names — is NOT a
# second query. It NARROWS this result, in the caller, so the row it acts on
# is provably one of the rows carrying the task id the caller named; a lookup
# by assignment alone would happily act on a row belonging to another task
# while the act stamped the named task's `source_ref` as its provenance.

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

# THE first terminal task event of an assignment, as a correlated scalar
# subquery over one COLUMN of that row (chunk L fold, #1479). Both the status
# and the instant are read through this ONE fragment, so they can never name
# different rows: the selection is byte-identical and `ingest_seq` is unique
# per event (one ledger row per ingested envelope), which makes the ORDER BY a
# total order. The fold's finding: `terminal_at` was a separate
# MIN(occurred_at), so an assignment with two terminal events (a `completed`
# ingested first, a `superseded` with an EARLIER occurred_at ingested second)
# showed one event's name over the other's instant. SQLite has no LATERAL, so
# one subquery cannot yield two columns here; a window-function CTE could, at
# the cost of a whole-table pass over `events` that the per-assignment
# partial index (migration 0002) exists to avoid — the §14 read gate.
def _first_terminal(col: str) -> str:
    return (f"(SELECT {col} FROM events t WHERE t.kind='task'"
            f"  AND t.assignment_id = a.assignment_id AND t.event IN ({_TERMINAL})"
            "  ORDER BY t.ingest_seq LIMIT 1)")


# Columns: (assignment_id, status, terminal_at). `terminal_at` is NULL for a
# live assignment and is the instant of the SAME row `status` names when the
# task has ended — the card reads "completed 1m ago" rather than a deadline it
# no longer has.
TASK_STATUS_SQL = (
    "SELECT a.assignment_id, COALESCE("
    f" {_first_terminal('t.event')},"
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
    " END) AS status,"
    f" {_first_terminal('t.occurred_at')} AS terminal_at"
    " FROM assignments a"
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

# WRITE-SIDE forms of REG_CURRENT (chunk-B gauntlet, probed SEV-1 + r3):
# every write-side suppression decision must ask what the READER would
# answer — "current" spelled a second way is how the two sides disagreed
# into a permanent false PRESENT (existence-keyed tombstone dedup) and a
# permanently-stale payload (the hash gate keyed on ledger-latest while
# the reader is the occurred_at winner; a stale-clock backfill then
# suppressed every honest rescan). The POINT form returns the current
# row's payload_hash: row-is-None = effectively deleted; the hash is what
# the ingest gate compares against. The KEYS form is the emitter's bulk
# read for tombstone eligibility. Both live HERE so zero Lane-C SQL is
# assembled anywhere else.
REG_CURRENT_POINT_SQL = (
    "SELECT payload_hash FROM (" + REG_CURRENT_SQL + ")"
    " WHERE host_uid = ? AND entity_type = ? AND entity_uid = ? LIMIT 1"
)

REG_CURRENT_KEYS_SQL = (
    "SELECT entity_type, entity_uid FROM (" + REG_CURRENT_SQL + ")"
    " WHERE host_uid = ?"
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

# --- Presence: the recorded half of the derivation (chunk 2) --------------
#
# The LATEST bot.heartbeat sample per instance, by ingest_seq (ledger order
# is authoritative — producer timestamps may arrive skewed, the estate's
# RTC-less-Pi class). Joined to identity_registry so the row carries the
# alias the sampler keys on (bot:<fleet>/<name>), and to the ingest_ledger
# for the sample's ingested_at — the freshness clock the derivation reads
# to type a quiet recording as STALE rather than trusting an old verdict.
# Presence itself is NEVER a table (spec §9b: an in-memory derivation over
# the latest samples plus a live poll); this query is only its recorded
# input.
LATEST_HEARTBEAT_SQL = (
    "WITH latest AS ("
    " SELECT subject_uid, value, ingest_seq, occurred_at,"
    "  ROW_NUMBER() OVER (PARTITION BY subject_uid"
    "    ORDER BY ingest_seq DESC) AS rn"
    " FROM metric_samples WHERE metric='bot.heartbeat')"
    " SELECT i.alias AS alias, l.value AS value, g.ingested_at AS ingested_at,"
    "  l.occurred_at AS occurred_at"
    " FROM latest l"
    " JOIN identity_registry i ON i.uid = l.subject_uid"
    " JOIN ingest_ledger g ON g.ingest_seq = l.ingest_seq"
    " WHERE l.rn = 1"
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
