"""Utilization over the plane — busy/idle % per bot from the recorded
``bot.heartbeat`` samples (Phase-6 surface). ONE definition of the math:
the legacy rollup's ``_compute_busy_pct`` (``claudlobby.utilization``,
which reads keepalive JSONL) is reused verbatim over the plane's series —
busy % = BUSY seconds / (BUSY + IDLE seconds), downtime gaps and UNKNOWN
excluded — so the two surfaces can never disagree on what "busy" means.
Durations come from consecutive samples' ``occurred_at`` (the emitter's
tick instant); a series shorter than two samples reports 0.0, never a
fabricated number.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..utilization import _compute_busy_pct

HEARTBEAT_SERIES_SQL = (
    "SELECT i.alias AS alias, m.occurred_at AS occurred_at, m.value AS value"
    " FROM metric_samples m JOIN identity_registry i ON i.uid = m.subject_uid"
    " WHERE m.metric = 'bot.heartbeat' AND m.occurred_at >= ?"
    " ORDER BY i.alias, m.ingest_seq"
)


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def bot_utilization(conn, *, now: datetime | None = None,
                    fleet: str | None = None, days: int = 7) -> list[dict]:
    if now is None:
        now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    series: dict[str, list] = {}
    for alias, occ, raw in conn.execute(HEARTBEAT_SERIES_SQL, (since,)):
        if fleet is not None and not alias.startswith(f"bot:{fleet}/"):
            continue
        ts = _parse(occ)
        try:
            state = (json.loads(raw) if isinstance(raw, str) else raw or {}).get("state")
        except (ValueError, AttributeError):
            state = None
        if ts is None or not isinstance(state, str):
            continue                      # an unreadable sample is skipped, not counted
        series.setdefault(alias, []).append((ts, state))
    out = []
    for alias in sorted(series):
        entries = series[alias]
        last_busy = max((ts for ts, st in entries if st == "BUSY"), default=None)
        out.append({
            "alias": alias, "short": alias.rsplit("/", 1)[-1],
            "samples": len(entries),
            "busy_pct_24h": round(_compute_busy_pct(entries, timedelta(days=1), now), 1),
            "busy_pct_7d": round(_compute_busy_pct(entries, timedelta(days=7), now), 1),
            "last_state": entries[-1][1],
            "idle_since": (last_busy.isoformat() if last_busy and entries[-1][1] == "IDLE"
                           else None),
        })
    return out
