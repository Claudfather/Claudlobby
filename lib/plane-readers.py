#!/usr/bin/env python3
"""plane-readers.py — the plane answering the LIST readers and the RESOLVER, stdlib
(cutover chunks 5 + 6a).

The matcher (``dispatch-overdue.py``) is a stdlib script every consumer shells,
so its plane source cannot import the package (the ``plane-lookup.py`` /
``plane-shadow-check.py`` precedent — both now import THIS module's ``connect``
so the read-only open, its schema probe and its transient retry live once).
This module is the stdlib twin of the package definitions — keep them in step
(the open SQL is pinned byte-identical):

- ``open_rows``     ↔ ``claudlobby.plane.queries.OPEN_ASSIGNMENTS_AT_SQL`` +
                      ``claudlobby.plane.shadow.plane_open``
- ``overdue_rows``  ↔ ``claudlobby.plane.shadow.plane_overdue`` (deadline
                      passed, the expiry cap, the bot's own ``progress`` inside
                      the grace — the watchdog's rules, mirrored; id-less rows
                      are KEPT, as that reader keeps them)
- ``declared``      ↔ ``claudlobby.plane.cutover.declared``
- ``answering_idless`` / ``head`` ↔ ``dispatch-overdue._answering_an_idless_dispatch``
                      + ``open_task_id`` (the resolver, chunk 6a: while the bot's
                      NEWEST assignment is an id-less dispatch nothing has
                      answered, resolve nothing — the next terminal report
                      answers THAT, never the oldest id'd row, #1418). The
                      report door closes id-less assignments on the bot's next
                      terminal report (``plane-lookup.py --open-idless``), which
                      is what makes the guard answerable from the plane.

Read-only (``mode=ro`` + ``query_only``). A missing or unopenable db raises
``PlaneUnreachable`` — the caller refuses, it never falls back to the JSONL:
rollback is the flag, not a silent fallback.
"""

from __future__ import annotations

import os
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional


class PlaneUnreachable(RuntimeError):
    pass


def db_file(root: str) -> str:
    return os.path.join(root, "state", "plane", "plane.db")


OPEN_RETRY_S = 0.25


def _open(path: str, readonly_uri: bool) -> sqlite3.Connection:
    conn = (sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) if readonly_uri
            else sqlite3.connect(path, timeout=2.0))
    conn.execute("PRAGMA query_only = 1")
    conn.execute("SELECT 1 FROM identity_registry LIMIT 0")   # the schema is there
    return conn


def connect(root: str, *, retries: int = 1) -> sqlite3.Connection:
    """A connection that only READS, whose first read has succeeded.

    The `mode=ro` URI open is tried first. On a WAL database whose writer
    has closed (no `-wal`/`-shm` beside it), the SQLite bundled with the
    system python3 the bash doors run answers "unable to open database
    file" for a read-only URI — it cannot create the shared-memory file
    from a read-only handle. That was the Mini's ~20s "transient" after a
    daemon restart, and it is deterministic under /usr/bin/python3 3.9. So
    a CANTOPEN falls back to a normal connection held read-only by
    `PRAGMA query_only` (SQLite may create the WAL side files; the pragma
    refuses every write). Anything else is retried once after a short
    pause, then raised: refuse, never answer empty."""
    path = db_file(root)
    if not os.path.isfile(path):
        raise PlaneUnreachable(f"no plane db at {path}")
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return _open(path, readonly_uri=True)
        except sqlite3.OperationalError as exc:
            last = exc
            if "unable to open" in str(exc):
                try:
                    return _open(path, readonly_uri=False)
                except sqlite3.Error as exc2:
                    last = exc2
        except sqlite3.Error as exc:
            last = exc
        if attempt < retries:
            time.sleep(OPEN_RETRY_S)
    raise PlaneUnreachable(f"plane db unreadable: {last}") from last


# ONE list of the terminal task events, feeding every SQL below — the
# package's TERMINAL_TASK_EVENTS pattern; OPEN_SQL assembled from it stays
# BYTE-IDENTICAL to queries.OPEN_ASSIGNMENTS_AT_SQL (pinned by test).
TERMINAL = ('completed', 'failed', 'cancelled', 'returned_blocked', 'superseded', 'reassigned', 'expired')
_TERMINAL = "(" + ",".join(f"'{e}'" for e in TERMINAL) + ")"
OPEN_SQL = (
    'SELECT a.occurred_at, a.source_ref, a.assignment_id, a.expected_by FROM assignments a WHER'
    'E a.assignee_uid = ? AND (? IS NULL OR a.occurred_at <= ?)  AND NOT EXISTS (SELECT 1 FROM '
    "events t WHERE t.kind='task'    AND t.event IN "
    + _TERMINAL +
    ' AND (? IS NULL OR t.occurred_at <= ?)    AND (t.assignment_id = a.assignment_id      OR ('
    "a.source_ref LIKE 'dispatch-log:%' AND a.source_ref NOT LIKE 'dispatch-log:sha:%'         "
    ' AND t.assignment_id IN (SELECT s.assignment_id FROM assignments s            WHERE s.assi'
    'gnee_uid = a.assignee_uid AND s.source_ref = a.source_ref              AND (? IS NULL OR s'
    '.occurred_at <= ?))))) ORDER BY a.occurred_at, a.ingest_seq'
)
ROSTER_SQL = "SELECT alias, uid, kind FROM identity_registry WHERE alias LIKE ?"
LAST_PROGRESS_SQL = (
    "SELECT MAX(e.occurred_at) FROM events e WHERE e.kind = 'task' AND e.event = 'progress'"
    " AND e.actor_uid = ? AND e.occurred_at <= ?"
)
# Twin of cutover.LATEST_DECLARED_SQL: the fleet is matched on the anchor
# COLUMN first (survives a truncated detail), the detail's fleet second.
DECLARED_SQL = (
    "SELECT e.occurred_at FROM events e WHERE e.kind = 'system' AND e.event = ?"
    " AND e.detail_truncated = 0 AND json_extract(e.detail, '$.reader') = ?"
    " AND (e.subject_alias = ? OR json_extract(e.detail, '$.fleet') = ?)"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC LIMIT 1"
)
DISPATCH = "dispatch-log:"
IDLESS = DISPATCH + "sha:"
# The bot's newest assignment across its uids, as of an instant (one query, the
# same tie-break the open list uses: occurred_at, then ingest order).
_NEWEST_SQL = (
    "SELECT a.occurred_at, a.source_ref, a.assignment_id, a.work_item_id FROM assignments a"
    " WHERE a.assignee_uid IN (%s) AND (? IS NULL OR a.occurred_at <= ?)"
    " ORDER BY a.occurred_at DESC, a.ingest_seq DESC LIMIT 1"
)
ASSIGNMENT_TERMINAL_SQL = (
    "SELECT 1 FROM events e WHERE e.kind = 'task' AND e.event IN " + _TERMINAL +
    " AND e.assignment_id = ? AND (? IS NULL OR e.occurred_at <= ?) LIMIT 1"
)
WORK_ITEM_SQL = "SELECT work_item_id FROM assignments WHERE assignment_id = ?"


def _epoch(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def roster(conn: sqlite3.Connection, fleet: str) -> dict[str, dict]:
    """bot (lower-cased key, the legacy bot key) → {"uids": [...], "actor": uid|None}
    from ONE registry scan: every identity whose alias is ``bot:<fleet>/<name>``.
    Empty = the plane holds no bot of this fleet at all."""
    prefix = f"bot:{fleet}/"
    out: dict[str, dict] = {}
    for alias, uid, kind in conn.execute(ROSTER_SQL, (prefix + "%",)):
        if not alias.startswith(prefix):
            continue
        entry = out.setdefault(alias[len(prefix):].lower(), {"uids": [], "actor": None})
        entry["uids"].append(uid)
        if kind == "actor" and entry["actor"] is None:
            entry["actor"] = uid
    return out


def bot_entry(conn: sqlite3.Connection, fleet: str, bot: str) -> Optional[dict]:
    """One bot's registry entry (case-insensitive alias, like the legacy bot key)."""
    return roster(conn, fleet).get(bot.lower())


def open_assignments(conn: sqlite3.Connection, fleet: str, bot: str, at: Optional[str] = None,
                     *, entry: Optional[dict] = None) -> list[tuple]:
    """The bot's OPEN assignments as of *at* (None = everything landed), oldest
    first: (occurred_at, source_ref, assignment_id, expected_by)."""
    entry = entry if entry is not None else bot_entry(conn, fleet, bot)
    out: list[tuple] = []
    for uid in (entry or {}).get("uids", []):
        out.extend(tuple(r) for r in conn.execute(OPEN_SQL, (uid, at, at, at, at, at, at)))
    out.sort(key=lambda r: (r[0] or ""))
    return out


def _task_id(source_ref: Optional[str]) -> Optional[str]:
    if source_ref and source_ref.startswith(DISPATCH) and not source_ref.startswith(IDLESS):
        return source_ref[len(DISPATCH):]
    return None


def open_rows(conn: sqlite3.Connection, fleet: str, bot: str, at: Optional[str] = None,
              *, entry: Optional[dict] = None, idd_only: bool = True
              ) -> list[tuple[int, Optional[int], Optional[str]]]:
    """The legacy ``open_dispatches`` tuple shape — (dispatched_at, expected_by,
    task_id), oldest first — from the plane. ``idd_only`` drops rows with no
    legacy task id (``sha:`` refs, or none): the open LIST is id'd rows only;
    the overdue reader keeps them (task_id None → the legacy ``-``)."""
    out: list[tuple[int, Optional[int], Optional[str]]] = []
    for occurred_at, source_ref, _asg, expected_by in open_assignments(conn, fleet, bot, at, entry=entry):
        tid = _task_id(source_ref)
        if idd_only and tid is None:
            continue
        da = _epoch(occurred_at)
        if da is None:
            continue
        out.append((da, _epoch(expected_by), tid))
    return out


def open_idless_assignments(conn: sqlite3.Connection, fleet: str, bot: str,
                            at: Optional[str] = None, *, entry: Optional[dict] = None
                            ) -> list[tuple[str, str]]:
    """(work_item_id, assignment_id) for every OPEN id-less dispatch of the bot
    (a ``sha:``-keyed assignment), oldest first — what the report door closes
    on the bot's next terminal report, as the legacy ledger closes id-less
    rows by any later terminal report."""
    out: list[tuple[str, str]] = []
    for _at, source_ref, asg, _exp in open_assignments(conn, fleet, bot, at, entry=entry):
        if source_ref and source_ref.startswith(IDLESS):
            wi = conn.execute(WORK_ITEM_SQL, (asg,)).fetchone()
            out.append((wi[0] if wi else "", asg))
    return out


def overdue_rows(conn: sqlite3.Connection, fleet: str, bot: str, *, now: int, max_age: int,
                 progress_grace: int, entry: Optional[dict] = None
                 ) -> list[tuple[int, int, int, Optional[str]]]:
    """The legacy ``--all`` row shape — (dispatched_at, expected_by, elapsed,
    task_id) — from the plane, the watchdog's rules mirrored; task_id None
    for an id-less row (the caller prints ``-``)."""
    entry = entry if entry is not None else bot_entry(conn, fleet, bot)
    at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    rows = open_rows(conn, fleet, bot, at, entry=entry, idd_only=False)
    last_progress = None
    actor = (entry or {}).get("actor")
    if progress_grace > 0 and actor:
        row = conn.execute(LAST_PROGRESS_SQL, (actor, at)).fetchone()
        last_progress = _epoch(row[0]) if row and row[0] else None
    out: list[tuple[int, int, int, Optional[str]]] = []
    for da, exp, tid in rows:
        if exp is None or now <= exp:
            continue
        if max_age > 0 and (now - da) > max_age:
            continue
        if last_progress is not None and da < last_progress <= now \
                and (now - last_progress) <= progress_grace:
            continue
        out.append((da, exp, now - exp, tid))
    return out


def answering_idless(conn: sqlite3.Connection, fleet: str, bot: str, at: Optional[str] = None,
                     *, entry: Optional[dict] = None) -> bool:
    """True while the bot's NEWEST assignment (as of *at*) is an id-less
    dispatch nothing has answered: a ``sha:``-keyed assignment with no
    terminal task event of its own. The report door lands that event on the
    bot's next terminal report (any status the legacy ledger calls
    terminal), so the guard releases exactly when the legacy one does."""
    entry = entry if entry is not None else bot_entry(conn, fleet, bot)
    uids = (entry or {}).get("uids", [])
    if not uids:
        return False
    row = conn.execute(_NEWEST_SQL % ",".join("?" * len(uids)), (*uids, at, at)).fetchone()
    if row is None or not (row[1] or "").startswith(IDLESS):
        return False
    return conn.execute(ASSIGNMENT_TERMINAL_SQL, (row[2], at, at)).fetchone() is None


def head(conn: sqlite3.Connection, fleet: str, bot: str, at: Optional[str] = None,
         *, entry: Optional[dict] = None) -> Optional[str]:
    """The resolver's answer from the plane: the oldest open id'd dispatch,
    or None — including None while an id-less dispatch is unanswered."""
    entry = entry if entry is not None else bot_entry(conn, fleet, bot)
    if entry is None or answering_idless(conn, fleet, bot, at, entry=entry):
        return None
    rows = open_rows(conn, fleet, bot, at, entry=entry, idd_only=True)
    return rows[0][2] if rows else None


# The idle-worker check (chunk 7a, the last reader to get a plane path): per
# bot, the NEWEST report by the bot — of ANY status, exactly as the legacy
# rule reads the newest report row and only then asks whether it is terminal
# (a later `progress` means the worker is busy, not idle) — against the NEWEST
# assignment to it. Purely temporal; it never asks whether work is open. The
# report door lands every report as a `report` communication and, when it
# resolved an assignment, a task event; the communication is the newest-report
# fact (a report on nothing still counts, as its ledger row does), the task
# event beside it carries the status.
# Every per-bot query spans ALL of the bot's uids (a case-variant alias mints a
# second actor; the roster collapses them, so must the reads — the adversarial
# lens found a report under the second uid vanishing from the idle check).
_NEWEST_REPORT_SQL = (
    "SELECT c.occurred_at, c.msg_id FROM communications c"
    " WHERE c.message_class = 'report' AND c.sender_uid IN (%s) AND (? IS NULL OR c.occurred_at <= ?)"
    " ORDER BY c.occurred_at DESC, c.ingest_seq DESC LIMIT 1"
)
_REPORT_TASK_EVENT_SQL = (
    "SELECT e.event, a.source_ref FROM events e"
    " LEFT JOIN assignments a ON a.assignment_id = e.assignment_id"
    " WHERE e.kind = 'task' AND e.source_ref = ? AND e.actor_uid IN (%s)"
    " ORDER BY e.ingest_seq DESC LIMIT 1"
)
# a report that resolved nothing carries its status as a report_status system
# event on the bot, under the same report-back:<msg> ref (chunk 7a)
_REPORT_STATUS_SQL = (
    "SELECT json_extract(e.detail, '$.status') FROM events e"
    " WHERE e.kind = 'system' AND e.event = 'report_status' AND e.source_ref = ? AND e.subject_uid IN (%s)"
    " ORDER BY e.ingest_seq DESC LIMIT 1"
)
LEGACY_TO_EVENT = {"completed": "completed", "failed": "failed", "blocked": "returned_blocked"}
REPORT_STATUS_EVENTS = ("completed", "failed", "returned_blocked", "progress")
NEWEST_ASSIGNMENT_SQL = (
    "SELECT a.occurred_at FROM assignments a WHERE a.assignee_uid IN (%s)"
    " AND (? IS NULL OR a.occurred_at <= ?) ORDER BY a.occurred_at DESC, a.ingest_seq DESC LIMIT 1"
)
LEGACY_STATUS = {"completed": "completed", "failed": "failed", "returned_blocked": "blocked"}


def unassigned_rows(conn: sqlite3.Connection, fleet: str, *, now: int, idle_threshold: int = 0,
                    at: Optional[str] = None) -> dict[str, tuple[int, int, str, str]]:
    """The legacy ``unassigned_all`` shape — {bot: (reported_at, idle_seconds,
    task_id, status)} — from the plane: workers whose newest report is
    terminal and were never re-tasked afterwards (the #1024 mirror)."""
    out: dict[str, tuple[int, int, str, str]] = {}
    for bot, entry in roster(conn, fleet).items():
        uids = entry["uids"]
        if not uids:
            continue
        marks = ",".join("?" * len(uids))
        rep = conn.execute(_NEWEST_REPORT_SQL % marks, (*uids, at, at)).fetchone()
        if rep is None:
            continue
        rts = _epoch(rep[0])
        if rts is None:
            continue
        ref = f"report-back:{rep[1]}"
        ev = conn.execute(_REPORT_TASK_EVENT_SQL % marks, (ref, *uids)).fetchone()
        status = ev[0] if ev else None
        if status is None:
            marker = conn.execute(_REPORT_STATUS_SQL % marks, (ref, *uids)).fetchone()
            status = LEGACY_TO_EVENT.get(marker[0]) if marker and marker[0] else None
        if status not in ("completed", "failed", "returned_blocked"):
            continue                                   # the newest report is not terminal (progress, or a bare note)
        asg = conn.execute(NEWEST_ASSIGNMENT_SQL % marks, (*uids, at, at)).fetchone()
        last_d = _epoch(asg[0]) if asg and asg[0] else None
        if last_d is not None and last_d > rts:
            continue                                   # re-tasked after reporting: the loop is intact
        idle = now - rts
        if idle < 0 or idle < idle_threshold:
            continue
        out[bot] = (rts, idle, (_task_id(ev[1]) if ev else None) or "-", LEGACY_STATUS.get(status, status))
    return out


# The bot-events ledger from the plane (Phase B): every system event the
# fleet's `emit_fleet_event` door landed — selected by PROVENANCE (the
# source_ref prefix the door stamps, never an event-name list, so the plane's
# own machinery and a report door's marker can never leak in) and rendered
# back as the legacy row {ts, bot, type, source, data} so every reader keeps
# its row contract. The door stamps occurred_at in UTC (`+00:00` once stored),
# so `since` — normalised to that form by since_form — compares lexically on
# the indexed column. The filters ride in the SQL: brief asks for one bot's day.
FLEET_UID_SQL = "SELECT uid FROM identity_registry WHERE kind = 'fleet' AND alias = ? LIMIT 1"
FLEET_EVENTS_PREFIX = "fleet-events:"
FLEET_EVENTS_SQL = (
    "SELECT e.occurred_at, e.event, e.severity, e.subject_kind, e.subject_alias,"
    " e.detail, e.detail_truncated FROM events e"
    " WHERE e.kind = 'system' AND e.fleet_uid = ? AND e.source_ref LIKE ?"
    " AND (? IS NULL OR e.occurred_at >= ?)"
    " AND (? IS NULL OR e.event = ?)"
    " AND (? IS NULL OR lower(e.subject_alias) = lower(?))"
    " ORDER BY e.occurred_at, e.ingest_seq"
)
# fleet-pulse's escalation, answered for EVERY critical type in one read (a
# sweep used to spawn this once per bot per type): which bots carry which
# critical fleet event strictly after the window start — the legacy grep's
# own compare — and when the latest landed. Critical = the severity the
# registry stamped at ingest, one definition.
ESCALATION_SQL = (
    "SELECT e.subject_alias, e.event, MAX(e.occurred_at) FROM events e"
    " WHERE e.kind = 'system' AND e.fleet_uid = ? AND e.source_ref LIKE ?"
    " AND e.severity = 'critical' AND e.subject_kind = 'actor' AND e.occurred_at > ?"
    " AND (? IS NULL OR e.event = ?)"
    " GROUP BY e.subject_alias, e.event"
)


def since_form(since: Optional[str]) -> Optional[str]:
    """A window start in the form the door STORES occurred_at in — UTC,
    isoformat (`Z` lands as `+00:00`) — so the lexical compare in the SQL is
    an instant compare: an aware instant is converted, a NAIVE one is the
    host's local clock (fleet-pulse's `date +%Y-%m-%dT%H:%M` window) and is
    converted from it. A string that is not an instant is refused, never
    compared as text."""
    if not since:
        return None
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"since must be an ISO instant, not {since!r}") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).isoformat()


def fleet_uid(conn: sqlite3.Connection, fleet: str) -> str:
    """The fleet's identity uid. Every fleet-scoped row minted it at its first
    ingest, so a fleet the plane holds no identity for is a plane that never
    saw the fleet — a wrong root, refused rather than read as 'nothing recorded'."""
    row = conn.execute(FLEET_UID_SQL, (fleet,)).fetchone()
    if row is None:
        raise PlaneUnreachable(f"no identity for fleet {fleet!r} in the plane"
                               " — a wrong root is not 'nothing recorded'")
    return row[0]


def legacy_event_row(occurred_at, event, severity, subject_kind, subject_alias,
                     detail, truncated, fleet) -> dict:
    """One plane system event as the legacy ledger row it stands for. The
    private keys carry what the row never had: the registry-stamped severity,
    and whether the detail was truncated (then data is {} and disclosed)."""
    data = {}
    if detail and not truncated:
        try:
            data = json.loads(detail)
        except ValueError:
            data = {}
    prefix = f"bot:{fleet}/"
    bot = "fleet" if subject_kind == "fleet" else (subject_alias or "?").removeprefix(prefix)
    return {"ts": (data.get("legacy_ts") or (occurred_at or "").replace("+00:00", "Z")),
            "bot": bot, "type": event, "source": data.get("source") or "plane",
            "data": data.get("data") if isinstance(data.get("data"), dict) else {},
            "_severity": severity, "_truncated": bool(truncated)}


def public(row: dict) -> dict:
    """The legacy row shape, private keys stripped — one definition for the
    CLI, the package and the tests."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def fleet_events(conn: sqlite3.Connection, fleet: str, *, since: Optional[str] = None,
                 bot: Optional[str] = None, event_type: Optional[str] = None) -> list[dict]:
    """The fleet's events as legacy rows, oldest first (`--critical` and
    `--source` are the reader's own vocabulary, filtered on the rows)."""
    uid = fleet_uid(conn, fleet)
    alias = f"bot:{fleet}/{bot}" if bot and bot != "fleet" else None
    since = since_form(since)
    rows = [legacy_event_row(*row, fleet) for row in conn.execute(
        FLEET_EVENTS_SQL, (uid, FLEET_EVENTS_PREFIX + "%", since, since,
                           event_type, event_type, alias, alias))]
    if bot == "fleet":
        rows = [r for r in rows if r["bot"] == "fleet"]
    return rows


def escalation(conn: sqlite3.Connection, fleet: str, window_start: Optional[str], *,
               event_type: Optional[str] = None) -> dict[tuple[str, str], str]:
    """{(bot, type): latest_instant} for every bot carrying a CRITICAL fleet
    event strictly after *window_start* — fleet-pulse's escalation question,
    every type at once (or one, with *event_type*)."""
    uid = fleet_uid(conn, fleet)
    prefix = f"bot:{fleet}/"
    return {(alias.removeprefix(prefix), ev): at
            for alias, ev, at in conn.execute(
                ESCALATION_SQL, (uid, FLEET_EVENTS_PREFIX + "%", since_form(window_start) or "",
                                 event_type, event_type))
            if alias and alias.startswith(prefix)}

# Twin of cutover.LATEST_RETIRED_SQL, with the door: a retirement COVERS a
# door when its recorded flags name it — a record from before a door existed
# retires nothing it never named. The fleet is matched on the anchor column
# (the registry mints the fleet identity under its BARE name) first, the
# detail's fleet second.
RETIRED_SQL = (
    "SELECT e.occurred_at FROM events e WHERE e.kind = 'system' AND e.event = ?"
    " AND e.detail_truncated = 0 AND (e.subject_alias = ? OR json_extract(e.detail, '$.fleet') = ?)"
    " AND (? IS NULL OR json_extract(e.detail, '$.flags.' || ?) IS NOT NULL)"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC LIMIT 1"
)


def retired(conn: sqlite3.Connection, fleet: str, door: Optional[str] = None) -> Optional[str]:
    """The instant the fleet's legacy writes were retired (``legacy_write_retired``)
    — the LATEST record naming *door* (dispatch / report / events) when one is
    given — or None: the doors skip their ledger append only when this is
    recorded."""
    row = conn.execute(RETIRED_SQL, ("legacy_write_retired", fleet, fleet, door, door)).fetchone()
    return row[0] if row else None


def declared(conn: sqlite3.Connection, fleet: str, reader: str) -> Optional[str]:
    """The instant of the LATEST ``cutover_declared`` for (fleet, reader), or
    None: a flag nobody declared is not a flip."""
    row = conn.execute(DECLARED_SQL, ("cutover_declared", reader, fleet, fleet)).fetchone()
    return row[0] if row else None
