"""Presence — the Lane C LIVE derivation (chunk 2; spec §9b, #1361).

Presence is what the harvest audit named the founding gap: five hours of a
stopped estate read as busy from every existing signal. It is derived, at
read time, from TWO independent inputs — never stored (spec §9b: "presence
is a derivation over the latest samples plus a live poll, deliberately
never a table"):

  - the RECORDED half: the latest ``bot.heartbeat`` sample keepalive wrote
    (BUSY/IDLE/UNKNOWN + marker_age_s), with its ingest freshness clock;
  - the LIVE half: the view sampler's in-memory liveness poll (up / down /
    sampling) — is-the-session-alive-right-now.

The two answer DIFFERENT questions, and the derivation keeps them in their
lanes. Liveness is the LIVE poll's to decide: a dead session is ``down``
even if a heartbeat from a minute ago said IDLE (the recorded verdict is
stale the instant the session dies, and #1361 is precisely the failure of
trusting a recorded signal past its truth). Activity — working vs idle —
is the RECORDED half's, but only while FRESH: a quiet recording past the
staleness horizon is typed ``stale`` (the recorded half went dark; do not
render its last guess as current). No-evidence is never evidence-of-down
(source_state #1216): a pane the sampler has not reached is ``sampling``,
a bot with no heartbeat yet is ``unknown``.

This module is a PURE function of (heartbeats, live panes, now) — no db,
no tmux, no FastAPI — so the whole verdict table is unit-testable, and the
one definition of "what presence means" lives here rather than smeared
across the endpoint and the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

# 3 keepalive ticks: a recording quiet this long has stopped being current.
# Derived from keepalive's own active window (KEEPALIVE_ACTIVE_WINDOW_S,
# default 180s ≈ 3 cycles) rather than a twin literal — if that cadence is
# ever tuned, presence must not silently mis-type staleness (the coupling
# class the keepalive-door fold flagged for its 120s constant). The
# endpoint passes the resolved value; the module default matches the
# keepalive default so a pure call is still honest.
STALE_AFTER_S = 180.0

# The presence vocabulary, closed:
#   working  — live-up AND a fresh heartbeat says BUSY
#   idle     — live-up AND a fresh heartbeat says IDLE
#   down     — the live poll says the session is dead (wins over any record)
#   stale    — live-up (or unknown) but the recorded half went quiet
#   unknown  — live-up but the newest heartbeat is UNKNOWN, or none exists
#   sampling — the live poll has not reached this pane yet (no verdict)
PRESENCE_STATES = ("working", "idle", "down", "stale", "unknown", "sampling")


@dataclass
class Presence:
    alias: str
    presence: str
    marker_age_s: int | None = None      # from the heartbeat, when fresh
    heartbeat_age_s: float | None = None  # how old the recorded half is
    live: str = "sampling"                # the raw live-poll status


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def derive_presence(heartbeats, live_panes, *, now,
                    stale_after_s: float = STALE_AFTER_S) -> list[Presence]:
    """Join the recorded and live halves into one verdict per bot.

    heartbeats: rows with .alias / ["alias"], a JSON ``value`` ({state,
      marker_age_s}), and an ``ingested_at`` ISO string (LATEST_HEARTBEAT_SQL).
    live_panes: the sampler snapshot's ``panes`` — dicts with fleet, bot,
      status (up|down|sampling).
    now: a timezone-aware datetime (the caller's clock — injected, never
      read here, so the function stays pure and testable).

    The union of both key sets is covered: a bot the sampler sees but that
    has never recorded is ``sampling``/``unknown``; a bot that recorded but
    the sampler has not discovered is judged on the record alone (its live
    status defaults to ``sampling`` — no-evidence, not down).
    """
    def _get(row, key):
        return row[key]     # the query is dict-wrapped upstream; one shape

    live_by_alias = {
        f"bot:{p['fleet']}/{p['bot']}": p.get("status", "sampling")
        for p in live_panes
    }
    hb_by_alias = {}
    for r in heartbeats:
        alias = _get(r, "alias")
        raw = _get(r, "value")
        # The MetricSample.value contract is `object` (number|bool|str|
        # object), so a bot.heartbeat with a SCALAR or list value commits
        # fine — from a non-keepalive emitter, a legacy import, or a manual
        # emit. A reader that assumed a dict 500'd the WHOLE panel on one
        # poison row (probed: worse than the mislabel #1361 kills). A value
        # that cannot be read as a state-bearing object IS an unknown-state
        # heartbeat: parse defensively, never trust, never crash.
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            val = None
        if not isinstance(val, dict):
            val = {}
        ingested = _parse_iso(_get(r, "ingested_at"))
        age = (now - ingested).total_seconds() if ingested else None
        hb_by_alias[alias] = (val, age)

    out = []
    for alias in sorted(set(live_by_alias) | set(hb_by_alias)):
        live = live_by_alias.get(alias, "sampling")
        val, age = hb_by_alias.get(alias, (None, None))
        state = (val or {}).get("state")
        marker = (val or {}).get("marker_age_s")
        fresh = age is not None and age <= stale_after_s

        # Liveness is the LIVE poll's decision and it wins outright.
        if live == "down":
            presence = "down"
        elif not hb_by_alias.get(alias):
            # up or sampling, but nothing recorded — no-evidence, not down.
            presence = "unknown" if live == "up" else "sampling"
        elif not fresh:
            # the recorded half went quiet: do not render its last guess.
            presence = "stale"
        elif state == "BUSY":
            presence = "working"
        elif state == "IDLE":
            presence = "idle"
        else:                       # UNKNOWN heartbeat, or an unmapped state
            presence = "unknown"

        out.append(Presence(
            alias=alias, presence=presence,
            # the marker describes an ACTIVE session's tool-call recency, so
            # it rides only the verdicts that surface a fresh recorded
            # activity — never `down` (liveness overrode the record) or
            # `stale` (the marker is as quiet as the heartbeat carrying it)
            marker_age_s=(marker if presence in ("working", "idle")
                          else None),
            heartbeat_age_s=round(age, 1) if age is not None else None,
            live=live))
    return out


def presence_counts(rows) -> dict:
    """Header rollup: one count per presence state, all keys always present
    (a zero is a fact, not an absence — the reader must not have to guess
    whether ``down: 0`` means none-down or not-computed)."""
    counts = {s: 0 for s in PRESENCE_STATES}
    for r in rows:
        counts[r.presence] = counts.get(r.presence, 0) + 1
    return counts
