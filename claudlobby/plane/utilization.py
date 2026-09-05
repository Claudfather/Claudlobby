"""Utilization over the plane — busy/idle % per bot from the recorded
``bot.heartbeat`` samples (Phase-6 surface). ONE definition of the math:
the legacy rollup's ``_compute_busy_pct`` (``claudlobby.utilization``,
which reads keepalive JSONL) is reused verbatim over the plane's series —
busy % = BUSY seconds / (BUSY + IDLE seconds), downtime gaps and UNKNOWN
excluded — so the two surfaces can never disagree on what "busy" means.
Durations come from consecutive samples' ``occurred_at`` (the emitter's
tick instant) in TIME order — never ingest order, which a re-ingested
spool can invert (probed: 300% busy). A sample stamped in the future
(RTC skew) is dropped as a clock error, not data (probed: -50% busy). A
downtime gap credits at most 10 minutes of the preceding state — the
legacy definition's pinned choice, inherited rather than re-decided. A
single sample's state extends to `now` (the legacy semantic), so a bot
with one fresh BUSY sample reads 100% and one IDLE sample 0% — one
minute of evidence, honestly one minute of evidence; `samples` is on the
row so a reader can weigh it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..utilization import compute_busy_pct, find_state_transition

# Fleet scope is applied IN SQL (a LIKE on the alias) — the lens measured
# 730 ms at 21 bots × 7 d when every fleet's rows were fetched and parsed
# before Python discarded the other fleets'. Measured trade (round 2): a
# scan with a LIKE, not a seek (no alias index) — ~1.4× faster for a
# scoped read on a multi-fleet host, ~15% SLOWER for fleet=None ("all")
# where nothing is discarded. The scoped read is the common one.
HEARTBEAT_SERIES_SQL = (
    "SELECT i.alias AS alias, m.occurred_at AS occurred_at, m.value AS value"
    " FROM metric_samples m JOIN identity_registry i ON i.uid = m.subject_uid"
    " WHERE m.metric = 'bot.heartbeat' AND m.occurred_at >= ?"
    " AND (? IS NULL OR i.alias LIKE ? ESCAPE '\\')"
    " ORDER BY i.alias, m.occurred_at, m.ingest_seq"
)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def heartbeat_series(conn, *, now: datetime | None = None,
                     fleet: str | None = None, days: int = 7) -> dict[str, list[tuple[datetime, str]]]:
    """``{alias: [(instant, BUSY|IDLE|UNKNOWN), ...]}`` in TIME order — the
    recorded (instant, verdict) pairs EVERY keepalive-derived read consumes
    (this surface, ``claudlobby.utilization``, ``claudlobby status``; F18
    closure R2b: the keepalive.log those once parsed is gone). ONE reader,
    so no two surfaces can disagree about what was recorded."""
    if now is None:
        now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    like = None if fleet is None else "bot:" + fleet.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    series: dict[str, list] = {}
    for alias, occ, raw in conn.execute(HEARTBEAT_SERIES_SQL, (since, like, like)):
        ts = _parse(occ)
        try:
            state = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("state")
        except (ValueError, AttributeError):
            state = None
        if ts is None or not isinstance(state, str):
            continue                      # an unreadable sample is skipped, not counted
        if ts > now + timedelta(seconds=60):
            continue                      # a future stamp is a clock error, not data
        series.setdefault(alias, []).append((ts, state))
    return series


def bot_utilization(conn, *, now: datetime | None = None,
                    fleet: str | None = None, days: int = 7) -> list[dict]:
    if now is None:
        now = datetime.now(timezone.utc)
    series = heartbeat_series(conn, now=now, fleet=fleet, days=days)
    out = []
    for alias in sorted(series):
        entries = series[alias]
        # idle_since = the FIRST IDLE of the current idle run — the legacy
        # definition (find_state_transition), not "last BUSY": one definition
        idle_since = find_state_transition(entries, "IDLE") if entries[-1][1] == "IDLE" else None
        out.append({
            "alias": alias, "short": alias.rsplit("/", 1)[-1],
            "samples": len(entries),
            "busy_pct_24h": round(compute_busy_pct(entries, timedelta(days=1), now), 1),
            "busy_pct_7d": round(compute_busy_pct(entries, timedelta(days=7), now), 1),
            "last_state": entries[-1][1],
            "idle_since": idle_since.isoformat() if idle_since else None,
        })
    return out
