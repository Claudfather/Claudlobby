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
- the plane answer mirrors the legacy reader's SEMANTICS, deliberately: the
  legacy join closes by (bot, task id), so a redispatched task id with one
  terminal report closes every row that carried it — the plane's per-
  assignment truth is richer, but the reader being replaced is the list
  reader, and a permanent "divergence" on every redispatch would grade the
  plane against a question it was not asked;
- the comparator never writes a ledger and never routes the plane's answer
  into any door — ``report-back.sh`` is untouched by this chunk;
- a comparison against an unreachable ledger or db is REFUSED, not recorded
  as clean;
- every divergence carries a pre-declared CLASS, so the gate can tell a
  real disagreement from the three explained ones: ``skew`` (a row inside
  the emit grace — the dispatch door stamps the ledger BEFORE it emits, and a
  spooled emit lands later), ``legacy_supersedes_pre_cutover`` (a row the
  JSONL retired by ``--supersedes`` before chunk 1 wired supersession to the
  plane — plane-more-wrong but explained, and it drains as those rows
  rotate) and ``intentional`` (a task id declared before shadow started);
  anything else is ``divergence``;
- the record is a ``system`` event per (bot, comparison) —
  ``shadow_parity_clean`` (notice) / ``shadow_parity_diverged`` (critical) —
  whose ``data`` carries both answers, both heads and each divergence's
  class, and whose event id is DERIVED from (bot, instant), so a replayed
  instant classifies duplicate rather than double-counting; replayed
  instants are the top-of-hour marks, never ``now`` minus N hours, so two
  invocations replay the SAME instants (measured: microseconds in the
  instant made every re-run mint fresh comparisons from zero new evidence).

The gate (J4, the LIST modes): per bot, 20 consecutive clean comparisons
with at least one open→closed TRANSITION observed between two consecutive
CLEAN comparisons — a bot whose open set never changed has not exercised
the join. **This is not the ``--open-task`` bar**: the head of the list is
the answer a report gets written against, and J4 gives it its own gate —
200 real resolutions with zero divergences of any class — which chunk 6
builds from these same records (``data.head_agrees``); nothing here
licenses flipping the resolver. ``--replay-hours`` re-derives both answers
at past instants (both readers are deadline-blind, so the open set at
instant T is dispatches ≤ T minus closures ≤ T) and records them, so the
gate does not start from zero the day shadow arms.

Chunk 4 adds the second reader and the two consumers. ``reader=overdue``
shadows the WATCHDOG's question (``dispatch-overdue.py --all``) rule for
rule — deadline passed, the #460 expiry cap, the bot's own ``progress``
inside the grace — with the cap and the grace resolved the way the
watchdog resolves them (``DISPATCH_OVERDUE_MAX_AGE_S``,
``DISPATCH_PROGRESS_GRACE_S``); the legacy side is read with no bots dir,
so nothing is split off as an orphan — the split is a host-local
``.spawn`` fact the plane cannot see (J1: orphans stay hybrid), and
``--unassigned`` has no plane counterpart yet: neither is shadowed. A
legacy row whose deadline is not an int is dropped by the watchdog and
kept by the plane — ``legacy_malformed_deadline``, explained, never
paging. Records and streaks are keyed by ``data.reader``. ``plane shadow
--check`` (and its stdlib twin ``lib/plane-shadow-check.py``, the one the
fleet's watchdog runs) answers the fleet-pulse bridge: a (bot, reader)
whose LATEST recorded comparison diverged; ``brief`` carries the streaks.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .ids import derive_uid
from .parity import DISPATCH, epoch_iso, read_ledger, ts19
from .queries import OPEN_ASSIGNMENTS_AT_SQL

EVENT_CLEAN = "shadow_parity_clean"
EVENT_DIVERGED = "shadow_parity_diverged"
READER_OPEN = "open"          # dispatch-overdue.py --open / --open-task: the deadline-blind list
READER_OVERDUE = "overdue"    # dispatch-overdue.py --all: the watchdog's input (overdue ∪ orphans)
READERS = (READER_OPEN, READER_OVERDUE)
GATE_CLEAN_RUN = 20
GATE_TRANSITIONS = 1
# The resolver (dispatch-overdue.py --open-task) is the open list's HEAD, and
# every open record already carries head_legacy / head_plane / head_agrees —
# so it is a STREAK MODE over the open reader's records (chunk 6a), never a
# third comparison. Its bar is its own: a stale head is a false completion
# (#1418), so 200 agreeing heads with at least one head CHANGE.
READER_OPEN_TASK = "open_task"
GATED = READERS + (READER_OPEN_TASK,)
GATE_HEAD_CLEAN_RUN = 200
# The bar per reader, ONE registry (a Streak reads its bar from its reader,
# never from settable state): the list readers' 20, the resolver's 200.
BAR_BY_READER = {READER_OPEN: GATE_CLEAN_RUN, READER_OVERDUE: GATE_CLEAN_RUN,
                 READER_OPEN_TASK: GATE_HEAD_CLEAN_RUN}
# The resolver's tail: only records with a NON-EMPTY resolver answer count
# toward its run (None == None proves nothing), so an idle bot's records are
# skipped, not counted — the tail must reach past them to find 200 real ones.
HEAD_TAIL_LIMIT = 1000
DEFAULT_SKEW_S = 600
TAIL_LIMIT = 200        # records the streak reads per bot: the gate needs the tail, never the history
LIST_CAP = 60           # ids kept per side in a record (oldest first); counts carry the rest
DIVERGENCE_CAP = 40     # divergences listed in a record; the count carries the rest

CLASS_SKEW = "skew"
CLASS_LEGACY_SUPERSEDES = "legacy_supersedes_pre_cutover"
CLASS_LEGACY_MALFORMED = "legacy_malformed_deadline"   # the ledger row's deadline is not an int
CLASS_INTENTIONAL = "intentional"
CLASS_DIVERGENCE = "divergence"

ACTOR_UIDS_SQL = "SELECT uid FROM identity_registry WHERE lower(alias) = lower(?)"


def dt_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def replay_instants(now: datetime, hours: int) -> list[datetime]:
    """The last *hours* top-of-hour marks before *now*, oldest first — the
    same instants on every invocation inside an hour, so a re-run replays
    duplicates rather than minting fresh comparisons."""
    top = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return [top - timedelta(hours=h) for h in range(hours - 1, -1, -1)]


# --- the two answers -----------------------------------------------------------

@dataclass
class OpenRow:
    task_id: Optional[str]          # legacy id (None for a plane assignment without one)
    at: str                         # dispatched instant, ISO (ts19-comparable)
    ref: Optional[str] = None       # the plane's own id when there is no task id
    expected_by: Optional[str] = None

    @property
    def label(self) -> str:
        return self.task_id or self.ref or "?"


def plane_open(conn: sqlite3.Connection, fleet: str, bot: str, *,
               at: Optional[str] = None) -> list[OpenRow]:
    """The plane's open set for ``bot:<fleet>/<bot>`` as of instant *at*
    (None = everything landed). Oldest first, the legacy order. The alias is
    resolved on the small registry first (case-insensitively, like the
    legacy bot key), then the assignments are read by uid — the indexed
    column — rather than through an unindexable ``lower()`` join."""
    uids = [r[0] for r in conn.execute(ACTOR_UIDS_SQL, (f"bot:{fleet}/{bot}",))]
    out: list[OpenRow] = []
    for uid in uids:
        for occurred_at, source_ref, assignment_id, expected_by in conn.execute(
                OPEN_ASSIGNMENTS_AT_SQL, (uid, at, at, at, at, at, at)):
            task_id = None
            if source_ref and source_ref.startswith(f"{DISPATCH}:") \
                    and not source_ref.startswith(f"{DISPATCH}:sha:"):
                task_id = source_ref[len(DISPATCH) + 1:]
            out.append(OpenRow(task_id, str(occurred_at or ""), assignment_id,
                               str(expected_by) if expected_by else None))
    out.sort(key=lambda r: r.at)
    return out


@contextmanager
def ledgers_at(dispatch_path: Path, report_path: Path,
               at: Optional[str]) -> Iterator[tuple[str, str]]:
    """The two ledger paths the legacy matcher should read for instant *at*:
    the real files when *at* is None, otherwise copies cut to rows whose
    ``ts`` is ≤ the instant, in a temp dir that lives for the block. Cut
    ONCE per instant and reused across every bot — the ledgers are
    bot-invariant, so cutting per (bot, instant) multiplied the parse, and
    the first version never removed its temp dirs at all."""
    if at is None:
        yield str(dispatch_path), str(report_path)
        return
    with tempfile.TemporaryDirectory(prefix="plane-shadow-") as tmp:
        for name, src in (("dispatch-log.jsonl", dispatch_path),
                          ("report-back.jsonl", report_path)):
            _, _, rows, _ = read_ledger(src)
            (Path(tmp) / name).write_text("".join(
                r.raw + "\n" for r in rows if ts19(r.ts) <= ts19(at)))
        yield str(Path(tmp) / "dispatch-log.jsonl"), str(Path(tmp) / "report-back.jsonl")


def legacy_open(doors, bot: str, dispatch_log: str, report_ledger: str) -> list[OpenRow]:
    """The legacy open set through the INSTALL's matcher, on the paths
    ``ledgers_at`` handed out."""
    return [OpenRow(str(tid), epoch_iso(da) or "")
            for da, _exp, tid in doors.open_dispatches(bot, dispatch_log, report_ledger)]


LAST_PROGRESS_SQL = (
    "SELECT MAX(e.occurred_at) FROM events e WHERE e.kind = 'task' AND e.event = 'progress'"
    " AND e.actor_uid = ? AND e.occurred_at <= ?"
)


def plane_overdue(conn: sqlite3.Connection, fleet: str, bot: str, *, now: datetime,
                  max_age_s: int, progress_grace_s: int,
                  rows: Optional[list[OpenRow]] = None,
                  uid: Optional[str] = None) -> list[OpenRow]:
    """The plane's answer to the WATCHDOG's question, mirroring the legacy
    ``_classify_all`` rule for rule: open (by task id) as of *now*, deadline
    passed, not older than *max_age_s* (the #460 expiry cap), and not shielded
    by the bot's own ``progress`` report inside *progress_grace_s* (the legacy
    grace keys on the BOT's last progress, not the row's). The orphan split is
    host-local (a `.spawn` file) and invisible to the plane, so the legacy
    side of this comparison is overdue ∪ orphans (J1: orphans stay hybrid)."""
    at = dt_iso(now)
    if rows is None:                             # the open reader's rows, when a caller has them
        rows = plane_open(conn, fleet, bot, at=at)
    if uid is None:
        uid = actor_uid(conn, f"bot:{fleet}/{bot}")
    last_progress = None
    if uid and progress_grace_s > 0:
        row = conn.execute(LAST_PROGRESS_SQL, (uid, at)).fetchone()
        last_progress = row[0] if row and row[0] else None
    out: list[OpenRow] = []
    for r in rows:
        if not r.expected_by or ts19(r.expected_by) >= ts19(at):
            continue
        try:
            dispatched = datetime.fromisoformat(r.at)
        except ValueError:
            continue
        if max_age_s > 0 and (now - dispatched).total_seconds() > max_age_s:
            continue
        if last_progress and ts19(r.at) < ts19(last_progress) <= ts19(at) \
                and (now - datetime.fromisoformat(last_progress)).total_seconds() <= progress_grace_s:
            continue
        out.append(r)
    return out


def malformed_deadlines(doors, bot: str, dispatch_log: str) -> set[str]:
    """Task ids of the bot's ledger rows the watchdog DROPS for a deadline or
    dispatch instant that is not an int (``_classify_all``'s first gate) —
    the plane keeps its own copy of the row, so the overdue comparison
    explains the resulting plane-only row rather than paging on it."""
    out: set[str] = set()
    for d in doors._load_jsonl(dispatch_log):
        if str(d.get("bot", "")).lower() != bot.lower() or not d.get("task_id"):
            continue
        if not isinstance(d.get("expected_by"), int) or not isinstance(d.get("dispatched_at"), int):
            out.add(str(d["task_id"]))
    return out


def legacy_overdue_all(doors, dispatch_log: str, report_ledger: str, *,
                       now: datetime, max_age_s: int) -> dict[str, list[OpenRow]]:
    """Every bot's legacy overdue set in ONE classification (the matcher
    reads both ledgers once and answers for all bots), keyed by the
    matcher's lower-cased bot key — the shape a per-instant loop wants."""
    overdue, _never_split = doors._classify_all(dispatch_log, report_ledger,
                                                int(now.timestamp()), max_age_s, None)
    return {b: [OpenRow(None if tid == "-" else str(tid), epoch_iso(da) or "")
                for da, _exp, _late, tid in sorted(rows, key=lambda t: t[0])]
            for b, rows in overdue.items()}


def legacy_overdue(doors, bot: str, dispatch_log: str, report_ledger: str, *,
                   now: datetime, max_age_s: int) -> list[OpenRow]:
    """The legacy watchdog answer through the INSTALL's matcher, with NO bots
    dir: the orphan split is a host-local `.spawn` fact the plane cannot see
    (J1: orphans stay hybrid), and without a bots dir the matcher never
    splits one off — every past-deadline row is in its overdue set, which
    is the union the comparison wants. The progress grace is the matcher's
    own (DISPATCH_PROGRESS_GRACE_S or its default)."""
    return legacy_overdue_all(doors, dispatch_log, report_ledger,
                              now=now, max_age_s=max_age_s).get(bot.lower(), [])


def superseded_by_bot(doors, dispatch_path: Path) -> dict[str, set[str]]:
    """Task ids the JSONL retired by a same-bot ``--supersedes``, per bot
    (lower-cased key, the matcher's own) — the matcher's own rule, read from
    its own helper, ONCE for the whole ledger."""
    out: dict[str, set[str]] = {}
    for b, tid in doors._superseded_ids(doors._load_jsonl(str(dispatch_path))):
        out.setdefault(b, set()).add(tid)
    return out


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
    reader: str = READER_OPEN
    # The RESOLVER's answers on both sides (chunk 6a): the legacy open_task_id
    # (with its id-less guard) and the plane's head — recorded on every open
    # record so the resolver's gate grades the guard, not just the list's head.
    resolver_legacy: Optional[str] = None
    resolver_plane: Optional[str] = None

    @property
    def resolver_agrees(self) -> bool:
        return self.resolver_legacy == self.resolver_plane

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
        return not self.unexplained and self.head_agrees and self.resolver_agrees

    def classes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.divergences:
            out[d.cls] = out.get(d.cls, 0) + 1
        return out


def _classify(row: OpenRow, side: str, grace: str, superseded: set[str],
              intentional: set[str], malformed: set[str] = frozenset()) -> str:
    """ONE classifier for both sides — a class added to one loop and
    forgotten in the other is the #1357 shape."""
    if row.task_id in intentional:
        return CLASS_INTENTIONAL
    if side == "plane_only" and row.task_id in superseded:
        return CLASS_LEGACY_SUPERSEDES
    if side == "plane_only" and row.task_id in malformed:
        return CLASS_LEGACY_MALFORMED
    if ts19(row.at) >= grace:
        return CLASS_SKEW
    return CLASS_DIVERGENCE


def diff(fleet: str, bot: str, legacy: list[OpenRow], plane: list[OpenRow], *,
         now: datetime, skew_s: int = DEFAULT_SKEW_S,
         superseded: Optional[set[str]] = None,
         intentional: Optional[set[str]] = None,
         reader: str = READER_OPEN,
         malformed: Optional[set[str]] = None,
         resolver_legacy: Optional[str] = None,
         resolver_plane: Optional[str] = None) -> ShadowDiff:
    """Pure: the two answers in, the classified disagreement out. Ids are
    compared as MULTISETS (a redispatched task id is two open rows on both
    sides) and the heads as the two lists' first elements."""
    superseded = superseded or set()
    intentional = intentional or set()
    malformed = malformed or set()
    grace = ts19(dt_iso(now - timedelta(seconds=skew_s)))
    out = ShadowDiff(fleet, bot, dt_iso(now),
                     [r.task_id or "" for r in legacy],
                     [r.task_id for r in plane], reader=reader,
                     resolver_legacy=resolver_legacy, resolver_plane=resolver_plane)
    plane_pool = list(plane)
    for row in legacy:
        match = next((p for p in plane_pool if p.task_id == row.task_id), None)
        if match is not None:
            plane_pool.remove(match)
            continue
        out.divergences.append(Divergence(
            row.task_id, "legacy_only",
            _classify(row, "legacy_only", grace, superseded, intentional, malformed), row.label))
    for row in plane_pool:
        out.divergences.append(Divergence(
            row.task_id, "plane_only",
            _classify(row, "plane_only", grace, superseded, intentional, malformed), row.label))
    return out


# --- the record ----------------------------------------------------------------

def actor_uid(conn: sqlite3.Connection, alias: str) -> Optional[str]:
    """The bot's actor uid when the plane knows it — the subject anchor.
    A bot with no plane history has none; the record then carries no
    subject (the contract wants kind, uid and alias together or not at all)
    and is keyed by ``data.bot`` alone."""
    row = conn.execute(
        "SELECT uid FROM identity_registry WHERE alias = ? AND kind = 'actor' LIMIT 1",
        (alias,)).fetchone()
    return row[0] if row else None


def shadow_event(d: ShadowDiff, *, subject_uid: Optional[str] = None) -> dict:
    """One system event per comparison; id derived from (bot, instant).
    Every list is CAPPED so the record stays far under the diagnostic cap:
    a record that truncated would lose its JSON and with it the bot key —
    and the bigger the divergence, the likelier the truncation, which is
    adverse selection against the evidence the gate exists to see."""
    alias = f"bot:{d.fleet}/{d.bot}"
    data = {
        "reader": d.reader,
        "bot": alias,
        "at": d.at,
        "legacy_n": len(d.legacy_ids),
        "plane_n": len(d.plane_ids),
        "legacy": d.legacy_ids[:LIST_CAP],
        "plane": d.plane_ids[:LIST_CAP],
        "head_legacy": d.head_legacy,
        "head_plane": d.head_plane,
        "head_agrees": d.head_agrees,
        "resolver_legacy": d.resolver_legacy,
        "resolver_plane": d.resolver_plane,
        "resolver_agrees": d.resolver_agrees,
        "divergences_n": len(d.divergences),
        "divergences": [
            {"task_id": x.task_id, "side": x.side, "class": x.cls, "ref": x.ref}
            for x in d.divergences[:DIVERGENCE_CAP]],
        "classes": d.classes(),
    }
    return {
        "event_type": "system",
        "emitter": "plane-shadow",
        "fleet": d.fleet,
        "occurred_at": d.at,
        "event_id": derive_uid("ev", f"shadow:{d.reader}:{alias}:{d.at}"),
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
    reader: str = READER_OPEN
    comparisons: int = 0
    clean_run: int = 0            # consecutive clean comparisons at the tail
    transitions: int = 0          # open→closed transitions seen inside the clean run
    last_diverged_at: Optional[str] = None
    last_at: Optional[str] = None

    @property
    def clean_bar(self) -> int:
        return BAR_BY_READER.get(self.reader, GATE_CLEAN_RUN)

    @property
    def gate_ok(self) -> bool:
        return self.clean_run >= self.clean_bar and self.transitions >= GATE_TRANSITIONS

    @property
    def latest_diverged(self) -> bool:
        """The newest record ended the run at once: the bridge's question,
        derived from the same tail the gate reads rather than a second query."""
        return self.comparisons > 0 and self.clean_run == 0 and self.last_diverged_at is not None

    def line(self) -> str:
        mark = "met" if self.gate_ok else "NOT met"
        return (f"{self.bot} [{self.reader}]: clean_run={self.clean_run}/{self.clean_bar}"
                f" transitions={self.transitions}/{GATE_TRANSITIONS}"
                f" comparisons={self.comparisons}"
                f" last_diverged={self.last_diverged_at or '-'} -> {mark}")


# A record is keyed by its data.bot; a record that truncated (which the caps
# above make unreachable, but which the gate must survive) has no JSON left,
# so it is keyed by its subject alias instead — a truncated DIVERGENCE must
# still end a run, never vanish.
# json_extract RAISES on malformed JSON (a truncated detail is a raw prefix),
# so it is only evaluated for an intact record; CASE short-circuits.
_KEY = ("COALESCE(CASE WHEN e.detail_truncated = 0 THEN json_extract(e.detail, '$.bot') END,"
        " e.subject_alias)")
# A record's reader; a truncated record has none and counts for EVERY reader
# (conservative: a truncated divergence ends every run, never none).
_READER = ("(e.detail_truncated = 1 OR"
           f" COALESCE(json_extract(e.detail, '$.reader'), '{READER_OPEN}') = ?)")
TAIL_SQL = (
    "SELECT e.event, e.occurred_at, e.detail, e.detail_truncated FROM events e"
    f" WHERE e.kind = 'system' AND e.event IN (?, ?) AND {_KEY} = ? AND {_READER}"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC LIMIT ?"
)
COUNT_SQL = (
    "SELECT COUNT(*) FROM events e"
    f" WHERE e.kind = 'system' AND e.event IN (?, ?) AND {_KEY} = ? AND {_READER}"
)
SHADOWED_BOTS_SQL = (
    f"SELECT DISTINCT {_KEY} FROM events e"
    f" WHERE e.kind = 'system' AND e.event IN (?, ?) AND {_KEY} LIKE ?"
)


def streak(conn: sqlite3.Connection, fleet: str, bot: str,
           reader: str = READER_OPEN) -> Streak:
    """Derived from the recorded comparisons, newest first, reading only
    the tail (``TAIL_LIMIT``) — the gate is about the run at the end, never
    the history. A transition is an id present in one clean comparison's
    open set and absent from the next clean one — the join being exercised,
    not merely re-read; a divergence ends the run, truncated or not."""
    s = Streak(bot, reader)
    s.comparisons, tail, s.last_at = _tail(conn, fleet, bot, reader, TAIL_LIMIT)
    run: list[Optional[list]] = []            # the tail's clean comparisons, newest first
    for event, at, detail, truncated in tail:
        if event != EVENT_CLEAN:
            s.last_diverged_at = at
            break
        open_set: Optional[list] = None       # unknown when the record lost its JSON
        if not truncated and detail:
            try:
                open_set = list(json.loads(detail).get("legacy") or [])
            except (json.JSONDecodeError, AttributeError, TypeError):
                open_set = None
        run.append(open_set)
    s.clean_run = len(run)
    for newer, older in zip(run, run[1:]):    # an id that left the set between two known sets
        if newer is not None and older is not None and any(i not in newer for i in older):
            s.transitions += 1
    return s


def _tail(conn: sqlite3.Connection, fleet: str, bot: str, reader: str, limit: int
          ) -> tuple[int, list, Optional[str]]:
    """The shared preamble of both streaks: (comparisons, the tail newest
    first, the newest instant) for one (bot, reader)."""
    alias = f"bot:{fleet}/{bot}"
    comparisons = conn.execute(COUNT_SQL, (EVENT_CLEAN, EVENT_DIVERGED, alias, reader)).fetchone()[0]
    tail = conn.execute(TAIL_SQL, (EVENT_CLEAN, EVENT_DIVERGED, alias, reader, limit)).fetchall()
    return comparisons, tail, (tail[0][1] if tail else None)


def head_streak(conn: sqlite3.Connection, fleet: str, bot: str) -> Streak:
    """The resolver's streak, read off the OPEN reader's records (chunk 6a):
    newest first, a record counts toward the run when its RESOLVER answers
    agree and are non-empty (None == None proves nothing: an idle record is
    skipped, neither counted nor breaking); a diverged record, a resolver
    disagreement, a record without the resolver fields (pre-6a) or a
    truncated one ends the run; a transition is the resolver's answer
    CHANGING between two counted records. Bar: GATE_HEAD_CLEAN_RUN."""
    s = Streak(bot, READER_OPEN_TASK)
    s.comparisons, tail, s.last_at = _tail(conn, fleet, bot, READER_OPEN, HEAD_TAIL_LIMIT)
    answers: list[str] = []
    for event, at, detail, truncated in tail:
        data = None
        if event == EVENT_CLEAN and not truncated and detail:
            try:
                data = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                data = None
        if not isinstance(data, dict) or "resolver_agrees" not in data \
                or data.get("resolver_agrees") is not True:
            s.last_diverged_at = at
            break
        answer = data.get("resolver_legacy")
        if answer is None:
            continue                                   # idle on both sides: proves nothing
        answers.append(answer)
    s.clean_run = len(answers)
    s.transitions = sum(1 for newer, older in zip(answers, answers[1:]) if newer != older)
    return s


def shadowed_bots(conn: sqlite3.Connection, fleet: str) -> list[str]:
    prefix = f"bot:{fleet}/"
    return sorted(
        r[0][len(prefix):] for r in conn.execute(
            SHADOWED_BOTS_SQL, (EVENT_CLEAN, EVENT_DIVERGED, f"{prefix}%"))
        if r[0] and r[0].startswith(prefix))


def record(root: Path, events: list[dict]) -> dict[str, int]:
    """Land comparisons through normal ingest, ONE instant per batch: the
    ingest refuses a MIXED batch (some rows duplicate, some new — "mixed
    state") as a matter of atomicity, and a replay is exactly that shape —
    the hour marks duplicate while the live comparison is new. A batch that
    still collides (a bot added to the roster since the marks were first
    recorded) falls back to one event per batch. Returns outcome counts."""
    from .emit_api import emit_batch
    counts = {"committed": 0, "duplicate": 0, "spooled": 0}
    by_instant: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for ev in events:
        if ev["event_id"] in seen:          # a live instant that IS an hour mark: one fact
            continue
        seen.add(ev["event_id"])
        by_instant.setdefault(ev["occurred_at"], []).append(ev)
    for batch in by_instant.values():
        try:
            outcomes = emit_batch(root, batch)
        except RuntimeError as exc:                      # the mixed-state refusal
            if "duplicate-classification refused" not in str(exc):
                raise
            outcomes = [o for ev in batch for o in emit_batch(root, [ev])]
        for o in outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
    return counts


def gate_summary(conn: sqlite3.Connection, fleet: str,
                 roster: Optional[list[str]] = None,
                 readers: tuple[str, ...] = READERS) -> list[Streak]:
    """One streak per (bot, reader) — every bot on *roster* (the declared
    fleet) plus any recorded one. A declared bot with NO comparison recorded
    is short, named as such, never absent: an absence read as clean is the
    ``source_state`` class."""
    bots = sorted(set(roster or []) | set(shadowed_bots(conn, fleet)))
    return [head_streak(conn, fleet, b) if r == READER_OPEN_TASK else streak(conn, fleet, b, r)
            for b in bots for r in readers]


def latest_diverged(conn: sqlite3.Connection, fleet: str, roster: list[str],
                    readers: tuple[str, ...] = READERS) -> list[tuple[str, str, str]]:
    """(bot, reader, at) for every (bot, reader) whose LATEST recorded
    comparison diverged — the fleet-pulse bridge's question, read off the
    same streaks the gate uses. A diverged record means an unexplained
    divergence OR a head disagreement; explained-only divergences with
    agreeing heads record as clean and never page."""
    return [(st.bot, st.reader, st.last_diverged_at or "")
            for st in gate_summary(conn, fleet, roster, readers) if st.latest_diverged]
