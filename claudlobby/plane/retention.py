"""Metric-sample retention (chunk 3a; spec §F20/§10 mutation surface).

The one aggressive-retention lane: raw ``metric_samples`` are the 30-day
incident-join window, and past it they age out. The ruling's hard edges,
enforced here:

  - **Family-scoped.** Retention DELETEs ``metric_samples`` and NOTHING
    else. It is the only DELETE the plane performs, and deletion for
    retention is not mutation of history (spec §10).
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
    return RetentionResult(cutoff=cutoff, candidates=candidates,
                           deleted=cur.rowcount, dry_run=False)
