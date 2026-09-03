"""The shadow-diff primitive (cutover chunk 3, #1444; J4 verification).

The instrument that decides the flip: per bot, per reader, a RECORDED
comparison of the legacy answer and the plane's answer, so the J4 count gate
is derived from the plane itself and never from a timer. The reader shadowed
here is the OPEN SET — ``dispatch-overdue.py``'s ``open_dispatches`` (the
list the ``--open`` mode prints and whose head ``--open-task`` resolves an
id-less report to, so the one a wrong answer would write into a ledger).

Structural invariants:

- the legacy answer comes from the INSTALL's ``lib/dispatch-overdue.py``
  module (``brief.load_dispatch_doors`` — the same seam brief uses), never a
  re-implementation of the join: two copies of "open" is the #1357 class;
- the comparator never writes a ledger and never routes the plane's answer
  into any door — ``report-back.sh`` is untouched by this chunk;
- a comparison against an unreachable ledger or db is REFUSED, not recorded
  as clean;
- every divergence carries a pre-declared CLASS, so the gate can tell a
  real disagreement from the three legitimate ones: ``skew`` (a row inside
  the emit grace — the dispatch door stamps the ledger BEFORE it emits, and a
  spooled emit lands later), ``legacy_supersedes_pre_cutover`` (a row the
  JSONL retired by ``--supersedes`` before chunk 1 wired supersession to the
  plane — plane-more-wrong but explained, and it drains as those rows
  rotate), and ``intentional`` (a task id declared before shadow started);
- the record is a ``system`` event per (bot, comparison) —
  ``shadow_parity_clean`` (notice) / ``shadow_parity_diverged`` (critical) —
  whose ``data`` carries both answers, both heads and each divergence's
  class, and whose event id is DERIVED from (bot, instant), so a replayed
  instant classifies duplicate rather than double-counting.

The gate (J4): per bot, 20 consecutive clean comparisons with at least one
open→closed TRANSITION observed between consecutive comparisons — a bot
whose open set never changed has not exercised the join. ``--replay-hours``
re-derives both answers at past instants (both readers are deadline-blind,
so the open set at instant T is dispatches ≤ T minus closures ≤ T) and
records them, so the gate does not start from zero the day shadow arms.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .ids import derive_uid
from .parity import DISPATCH, read_ledger, ts19
from .queries import OPEN_ASSIGNMENTS_AT_SQL

EVENT_CLEAN = "shadow_parity_clean"
EVENT_DIVERGED = "shadow_parity_diverged"
READER = "open"
DEFAULT_SKEW_S = 600
GATE_CLEAN_RUN = 20
GATE_TRANSITIONS = 1

CLASS_SKEW = "skew"
CLASS_LEGACY_SUPERSEDES = "legacy_supersedes_pre_cutover"
CLASS_INTENTIONAL = "intentional"
CLASS_DIVERGENCE = "divergence"
EXPLAINED = (CLASS_SKEW, CLASS_LEGACY_SUPERSEDES, CLASS_INTENTIONAL)

FAR_FUTURE = "9999-12-31T23:59:59+00:00"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _epoch_iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# --- the two answers -----------------------------------------------------------

@dataclass
class OpenRow:
    task_id: Optional[str]      # legacy id (None for a plane assignment without one)
    at: str                     # dispatched instant, ISO (ts19-comparable)
    ref: str                    # what identifies the row on its own side


def plane_open(conn: sqlite3.Connection, fleet: str, bot: str, *,
               at: str = FAR_FUTURE) -> list[OpenRow]:
    """The plane's open set for ``bot:<fleet>/<bot>`` as of instant *at*
    (default: now — everything landed). Oldest first, the legacy order."""
    alias = f"bot:{fleet}/{bot}"
    rows = conn.execute(OPEN_ASSIGNMENTS_AT_SQL, (alias, at, at)).fetchall()
    out: list[OpenRow] = []
    for occurred_at, source_ref, assignment_id in rows:
        task_id = None
        if source_ref and source_ref.startswith(f"{DISPATCH}:") \
                and not source_ref.startswith(f"{DISPATCH}:sha:"):
            task_id = source_ref[len(DISPATCH) + 1:]
        out.append(OpenRow(task_id, str(occurred_at or ""), assignment_id))
    return out


def legacy_open(doors, bot: str, dispatch_path: Path, report_path: Path, *,
                at: Optional[str] = None) -> list[OpenRow]:
    """The legacy open set through the INSTALL's matcher. With *at*, both
    ledgers are first cut to rows whose ``ts`` is ≤ the instant (temp files
    the matcher reads exactly as it reads the real ones), so the answer is
    what the matcher WOULD have said then."""
    dlog, rlog = str(dispatch_path), str(report_path)
    if at is not None:
        tmp = Path(tempfile.mkdtemp(prefix="plane-shadow-"))
        for name, src in (("dispatch-log.jsonl", dispatch_path), ("report-back.jsonl", report_path)):
            _, _, rows, _ = read_ledger(src)
            (tmp / name).write_text("".join(
                r.raw + "\n" for r in rows if ts19(r.ts) <= ts19(at)))
        dlog, rlog = str(tmp / "dispatch-log.jsonl"), str(tmp / "report-back.jsonl")
    return [OpenRow(str(tid), _epoch_iso(da), str(tid))
            for da, _exp, tid in doors.open_dispatches(bot, dlog, rlog)]


def superseded_ids(doors, bot: str, dispatch_path: Path) -> set[str]:
    """Task ids the JSONL retired by a same-bot ``--supersedes`` — the
    matcher's own rule, read from its own helper."""
    rows = doors._load_jsonl(str(dispatch_path))
    return {tid for b, tid in doors._superseded_ids(rows) if b == bot.lower()}


# --- the diff ------------------------------------------------------------------

@dataclass
class Divergence:
    task_id: Optional[str]
    side: str        # "legacy_only" | "plane_only"
    cls: str
    ref: str


@dataclass
class ShadowDiff:
    fleet: str
    bot: str
    at: str
    legacy_ids: list[str]
    plane_ids: list[Optional[str]]
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def head_legacy(self) -> Optional[str]:
        return self.legacy_ids[0] if self.legacy_ids else None

    @property
    def head_plane(self) -> Optional[str]:
        return self.plane_ids[0] if self.plane_ids else None

    @property
    def head_agrees(self) -> bool:
        return self.head_legacy == self.head_plane

    @property
    def unexplained(self) -> list[Divergence]:
        return [d for d in self.divergences if d.cls == CLASS_DIVERGENCE]

    @property
    def clean(self) -> bool:
        """Clean = no UNEXPLAINED divergence AND the heads agree. An explained
        divergence (skew, a pre-cutover supersession, a declared id) is
        disclosed but does not break the streak; a head disagreement always
        does, because the head is the answer that gets written."""
        return not self.unexplained and self.head_agrees

    def classes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.divergences:
            out[d.cls] = out.get(d.cls, 0) + 1
        return out


def diff(fleet: str, bot: str, legacy: list[OpenRow], plane: list[OpenRow], *,
         now: datetime, skew_s: int = DEFAULT_SKEW_S,
         superseded: Optional[set[str]] = None,
         intentional: Optional[set[str]] = None) -> ShadowDiff:
    """Pure: the two answers in, the classified disagreement out. Ids are
    compared as MULTISETS (a redispatched task id is two open rows on both
    sides) and the heads as the two lists' first elements."""
    superseded = superseded or set()
    intentional = intentional or set()
    grace = ts19(_iso(now - timedelta(seconds=skew_s)))
    out = ShadowDiff(fleet, bot, _iso(now),
                     [r.task_id or "" for r in legacy],
                     [r.task_id for r in plane])
    plane_pool = [r for r in plane]
    for row in legacy:
        match = next((p for p in plane_pool if p.task_id == row.task_id), None)
        if match is not None:
            plane_pool.remove(match)
            continue
        cls = CLASS_INTENTIONAL if row.task_id in intentional \
            else CLASS_SKEW if ts19(row.at) >= grace \
            else CLASS_DIVERGENCE
        out.divergences.append(Divergence(row.task_id, "legacy_only", cls, row.ref))
    for row in plane_pool:
        cls = CLASS_INTENTIONAL if row.task_id in intentional \
            else CLASS_LEGACY_SUPERSEDES if row.task_id in superseded \
            else CLASS_SKEW if ts19(row.at) >= grace \
            else CLASS_DIVERGENCE
        out.divergences.append(Divergence(row.task_id, "plane_only", cls, row.ref))
    return out


# --- the record ----------------------------------------------------------------

ACTOR_UID_SQL = "SELECT uid FROM identity_registry WHERE alias = ? AND kind = 'actor' LIMIT 1"


def actor_uid(conn: sqlite3.Connection, alias: str) -> Optional[str]:
    """The bot's actor uid when the plane knows it — the subject anchor.
    A bot with no plane history has none; the record then carries the
    alias alone (the contract wants kind and uid together or neither)."""
    row = conn.execute(ACTOR_UID_SQL, (alias,)).fetchone()
    return row[0] if row else None


LIST_CAP = 40   # ids kept per side in the record; counts carry the rest


def shadow_event(d: ShadowDiff, *, subject_uid: Optional[str] = None) -> dict:
    """One system event per comparison; id derived from (bot, instant). The
    record is keyed by ``data.bot`` (the streak reads it with json_extract),
    and ANCHORED on the bot's actor uid when the plane knows it — the
    contract allows a subject alias only with its kind+uid pair, and a bot
    with no plane history has none. Id lists are capped (oldest first) so the
    diagnostic stays under its cap and never truncates into non-JSON."""
    alias = f"bot:{d.fleet}/{d.bot}"
    data = {
        "reader": READER,
        "bot": alias,
        "at": d.at,
        "legacy_n": len(d.legacy_ids),
        "plane_n": len(d.plane_ids),
        "legacy": d.legacy_ids[:LIST_CAP],
        "plane": d.plane_ids[:LIST_CAP],
        "head_legacy": d.head_legacy,
        "head_plane": d.head_plane,
        "head_agrees": d.head_agrees,
        "divergences": [
            {"task_id": x.task_id, "side": x.side, "class": x.cls, "ref": x.ref}
            for x in d.divergences],
        "classes": d.classes(),
    }
    return {
        "event_type": "system",
        "emitter": "plane-shadow",
        "fleet": d.fleet,
        "occurred_at": d.at,
        "event_id": derive_uid("ev", f"shadow:{READER}:{alias}:{d.at}"),
        "payload": {
            "event": EVENT_CLEAN if d.clean else EVENT_DIVERGED,
            **({"subject_kind": "actor", "subject_uid": subject_uid, "subject_alias": alias}
               if subject_uid else {}),
            "data": data,
        },
    }


# --- the gate ------------------------------------------------------------------

@dataclass
class Streak:
    bot: str
    comparisons: int = 0
    clean_run: int = 0            # consecutive clean comparisons at the tail
    transitions: int = 0          # open→closed transitions seen inside the clean run
    last_diverged_at: Optional[str] = None
    last_at: Optional[str] = None

    @property
    def gate_ok(self) -> bool:
        return self.clean_run >= GATE_CLEAN_RUN and self.transitions >= GATE_TRANSITIONS


STREAK_SQL = (
    "SELECT e.event, e.occurred_at, e.detail, e.detail_truncated FROM events e"
    " WHERE e.kind = 'system' AND e.event IN (?, ?)"
    "  AND e.detail_truncated = 0 AND json_extract(e.detail, '$.bot') = ?"
    " ORDER BY e.occurred_at, e.ingest_seq"
)

SHADOWED_BOTS_SQL = (
    "SELECT DISTINCT json_extract(e.detail, '$.bot') FROM events e"
    " WHERE e.kind = 'system' AND e.event IN (?, ?) AND e.detail_truncated = 0"
    "  AND json_extract(e.detail, '$.bot') LIKE ?"
)


def streak(conn: sqlite3.Connection, fleet: str, bot: str) -> Streak:
    """Derived from the recorded comparisons in time order: the clean run
    at the tail, and how many transitions that run contains. A transition is
    an id present in one clean comparison's open set and absent from the next
    — the join being exercised, not merely re-read."""
    alias = f"bot:{fleet}/{bot}"
    s = Streak(bot)
    prev_open: Optional[list] = None
    for event, at, detail, truncated in conn.execute(
            STREAK_SQL, (EVENT_CLEAN, EVENT_DIVERGED, alias)):
        s.comparisons += 1
        s.last_at = at
        if event != EVENT_CLEAN:
            s.clean_run, s.transitions, prev_open = 0, 0, None
            s.last_diverged_at = at
            continue
        s.clean_run += 1
        open_now: Optional[list] = None
        if detail and not truncated:
            try:
                open_now = list(json.loads(detail).get("legacy") or [])
            except (json.JSONDecodeError, AttributeError):
                open_now = None
        if prev_open is not None and open_now is not None \
                and any(i not in open_now for i in prev_open):
            s.transitions += 1
        prev_open = open_now
    return s


def shadowed_bots(conn: sqlite3.Connection, fleet: str) -> list[str]:
    return sorted(
        r[0][len(f"bot:{fleet}/"):] for r in conn.execute(
            SHADOWED_BOTS_SQL, (EVENT_CLEAN, EVENT_DIVERGED, f"bot:{fleet}/%"))
        if r[0] and r[0].startswith(f"bot:{fleet}/"))
