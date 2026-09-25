"""Metric-sample retention (chunk 3a; spec §F20/§10 mutation surface).

The one aggressive-retention lane: raw ``metric_samples`` are the 30-day
incident-join window, and past it they age out. The ruling's hard edges,
enforced here:

  - **Family-scoped, and there are now TWO lanes (#1659).** This one
    DELETEs ``metric_samples``. The second DELETEs a NAMED, ALLOWLISTED
    set of ``system`` events and nothing else — see
    :data:`PRUNABLE_SYSTEM_EVENTS`. Deletion for retention is not
    mutation of history (spec §10).

    **The "and NOTHING else" this docstring used to claim was load-bearing
    for code outside this module**, which is why the second lane is an
    allowlist rather than an age sweep. ``lib/selfstart-snapshot.sh``
    reads ``fleet_rescue`` receipts from the plane and its boot gate fails
    CLOSED on an UNREACHABLE read (exit 7, "a receipt gate that fails OPEN
    is the one failure this measurement must never have") — but a receipt
    that was PRUNED is not unreachable, it is absent, and absent is read as
    a certain no-receipt. A second deletion lane could therefore silently
    credit a rescued boot as a self-start without touching that gate at
    all. ``boot-capture`` records land as system events for the same stated
    reason.
  - **The ledger is NEVER touched.** ``ingest_ledger`` is the ordering
    authority AND the event_id dedupe horizon — its rows outlive every
    family row, so the dedupe window is the ledger's lifetime. A
    retention pass that deleted a ledger row would shrink that horizon
    and let a replayed old event re-ingest as new.
  - **Aged by ``ingested_at``, the ledger's landing clock — never
    ``occurred_at``.** occurred_at is the carrier's instant and skews
    freely on the RTC-less Pi; ingested_at is when the row entered the db,
    which is FORWARD-MOVING during normal operation. So a backfilled
    sample with an ancient occurred_at is kept 30 days from ingestion
    (correct: it is in the join window from when we learned it), and the
    conservative direction holds — because the failure mode of
    over-deleting is silent data loss. The one exposure (gauntlet-probed):
    ingested_at is `now()`, NOT strictly monotonic, so a >30-day BACKWARD
    clock step at boot (this host's stale-RTC class) can stamp a fresh row
    with an old ingested_at that a later corrected-clock prune deletes. A
    routine minutes-scale skew is safe (deletes 0), only a gross error
    bites, and a lost heartbeat re-emits next tick — so the sample lane
    tolerates it; a durable family would need a monotonic cutoff.

This module is the pure logic; the daemon does NOT run it (INGEST ONLY by
scope tripwire) — a separate CLI door (``claudlobby plane prune``) invoked
by a composed timer owns it, writing through its own connection (WAL lets
it delete while the daemon ingests). No VACUUM: it would lock the db
against the live daemon, and SQLite reuses freed pages, so the file
plateaus rather than growing — disclosed, not reclaimed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DEFAULT_RETENTION_DAYS = 30


@dataclass
class RetentionResult:
    cutoff: str            # ISO — rows with ingested_at < this age out
    candidates: int        # metric_samples older than the cutoff
    deleted: int           # rows actually removed (0 on dry-run)
    dry_run: bool


#: THE ONLY system event types this lane may delete (#1659).
#:
#: **An allowlist, and the direction IS the safety property.** Forgetting to
#: list a type here means it is KEPT; a denylist of protected types would mean
#: a forgotten type is DELETED. The first failure costs disk, the second costs
#: a boot-integrity gate — see the module docstring on `fleet_rescue`. So this
#: set names what may go, and the complement is everything else, proved by
#: construction rather than by enumerating what was checked.
#:
#: **Both entries are emit-only: nothing reads them back from the plane.**
#: Verified rather than assumed — no query under `claudlobby/` selects either
#: (`tool_call`'s only other consumers are the severity registry, CLI help
#: text, and `fleet-pulse.sh`, which reads the `data/.last-tool-call` FILE's
#: mtime, not a plane row). That is the discriminator between a sample-like
#: event and one that IS the record: whether a door reads it.
#:
#: **And they are 98% of the volume**, measured on the one complete day this
#: host's post-cutover record holds (2026-09-21): 12,963 `tool_call` and 2,163
#: `wip_uncommitted` out of 15,422 system events that day. Keeping the set
#: minimal therefore costs ~2% of the win and removes most of the risk.
PRUNABLE_SYSTEM_EVENTS = frozenset({
    "tool_call",        # one per guarded hook invocation (lib/bot-vitals.sh)
    "wip_uncommitted",  # one per dirty repo per fleet-pulse tick
})


def prune_system_events(conn, *, now=None, days: int = DEFAULT_RETENTION_DAYS,
                        events: frozenset[str] | None = None) -> int:
    """Age out the allowlisted system events. Returns rows deleted.

    Same two hard edges as the sample lane and for the same reasons: the
    ``ingest_ledger`` is NEVER touched (it is the dedupe horizon, and shrinking
    it would let a replayed old event re-ingest as new), and rows age by
    ``ingested_at`` rather than ``occurred_at``.

    Because the ledger is untouched, this reclaims the event rows and not their
    ledger rows — so it bounds the family that grows fastest without changing
    the dedupe window. That is a deliberate half-measure: the alternative
    trades a disk bound for a correctness hazard.
    """
    allow = PRUNABLE_SYSTEM_EVENTS if events is None else frozenset(events)
    if not allow:
        return 0
    unknown = allow - PRUNABLE_SYSTEM_EVENTS
    if unknown:
        # A caller cannot widen the lane by passing a set: the allowlist is the
        # contract, not a default. Refusing is the only answer that keeps the
        # complement proof true for every call site.
        raise ValueError(
            f"not allowlisted for retention: {sorted(unknown)} — add it to "
            "PRUNABLE_SYSTEM_EVENTS with its justification, or keep it"
        )
    at = now or datetime.now(timezone.utc)
    cutoff = _cutoff_iso(at, days)
    marks = ",".join("?" for _ in allow)
    cur = conn.execute(
        f"DELETE FROM events WHERE kind = 'system' AND event IN ({marks})"
        " AND ingested_at < ?",
        (*sorted(allow), cutoff),
    )
    deleted = cur.rowcount or 0
    # RECORD THE PRUNE so the duplicate verifier can tell a pruned row from a
    # corrupted one (#1659 review). Without this, replaying a pruned event
    # reaches `_verify_duplicates` with a ledger row and no family row and is
    # refused as integrity damage -- taking every other event in that batch
    # with it.
    #
    # A WATERMARK, not a row per pruned event: the latter would store as much
    # as the prune deleted. The cost is a real and bounded loss of coverage,
    # stated rather than hidden: for a `system` row older than the watermark,
    # a genuinely absent family row now classifies as pruned rather than
    # corrupt. That is strictly tighter than teaching the verifier to accept
    # any absent family row for a prunable type, which would hold forever and
    # for new rows too; here it holds only behind a cutoff this lane actually
    # ran, and only for the family it ran on.
    #
    # `MAX` so a re-run with a shorter window cannot walk the watermark
    # backwards and re-expose rows it already explained.
    if deleted:
        conn.execute(
            "INSERT INTO prune_watermarks (family, pruned_before, pruned_at)"
            " VALUES ('system', ?, ?)"
            " ON CONFLICT(family) DO UPDATE SET"
            "   pruned_before = MAX(pruned_before, excluded.pruned_before),"
            "   pruned_at = excluded.pruned_at",
            (cutoff, at.isoformat()),
        )
    return deleted


def _cutoff_iso(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


def prune_metric_samples(conn, *, now=None, days: int = DEFAULT_RETENTION_DAYS,
                         dry_run: bool = False) -> RetentionResult:
    """Age out metric_samples older than ``days`` by ingested_at.

    conn: a WRITE connection to the plane db. ``now`` is injected (defaults
    to real UTC) so the whole thing is deterministic under test. Returns
    the count acted on; a dry run reports candidates without deleting.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if days < 0:
        raise ValueError("retention days cannot be negative")
    cutoff = _cutoff_iso(now, days)

    candidates = conn.execute(
        "SELECT COUNT(*) FROM metric_samples WHERE ingested_at < ?",
        (cutoff,)).fetchone()[0]
    if dry_run or candidates == 0:
        return RetentionResult(cutoff=cutoff, candidates=candidates,
                               deleted=0, dry_run=dry_run)

    # The DELETE names metric_samples explicitly and by nothing else — the
    # ledger (and every other family) is untouched by construction.
    cur = conn.execute(
        "DELETE FROM metric_samples WHERE ingested_at < ?", (cutoff,))
    deleted = cur.rowcount

    # Record the prune: the watermark comment in prune_system_events applies (#1751).
    if deleted:
        conn.execute(
            "INSERT INTO prune_watermarks (family, pruned_before, pruned_at)"
            " VALUES ('metric_sample', ?, ?)"
            " ON CONFLICT(family) DO UPDATE SET"
            "   pruned_before = MAX(pruned_before, excluded.pruned_before),"
            "   pruned_at = excluded.pruned_at",
            (cutoff, now.isoformat()),
        )
    return RetentionResult(cutoff=cutoff, candidates=candidates,
                           deleted=deleted, dry_run=False)
