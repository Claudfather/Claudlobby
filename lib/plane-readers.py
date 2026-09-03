#!/usr/bin/env python3
"""plane-readers.py — the plane answering the two LIST readers, stdlib (cutover chunk 5).

The matcher (``dispatch-overdue.py``) is a stdlib script every consumer shells,
so its plane source cannot import the package (the ``plane-lookup.py`` /
``plane-shadow-check.py`` precedent). This module is the stdlib twin of the
package definitions — keep them in step (the SQL is pinned byte-identical):

- ``open_rows``    ↔ ``claudlobby.plane.queries.OPEN_ASSIGNMENTS_AT_SQL`` +
                     ``claudlobby.plane.shadow.plane_open``
- ``overdue_rows`` ↔ ``claudlobby.plane.shadow.plane_overdue`` (deadline
                     passed, the expiry cap, the bot's own ``progress`` inside
                     the grace — the watchdog's rules, mirrored; id-less rows
                     are KEPT, as that reader keeps them)
- ``declared``     ↔ ``claudlobby.plane.cutover.declared``

Read-only (``mode=ro`` + ``query_only``). A missing or unopenable db raises
``PlaneUnreachable`` — the caller refuses, it never falls back to the JSONL:
rollback is the flag, not a silent fallback.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional


class PlaneUnreachable(RuntimeError):
    pass


def db_file(root: str) -> str:
    return os.path.join(root, "state", "plane", "plane.db")


OPEN_RETRY_S = 0.25


def connect(root: str, *, retries: int = 1) -> sqlite3.Connection:
    """A read-only connection whose FIRST read has succeeded. Retried once
    after a short pause: on the live Mini a `mode=ro` open answered
    "unable to open database file" for ~20s while the ingest daemon
    held the WAL, then cleared — a transient the reader must not turn
    into a page. A persistent failure still raises: refuse, never
    answer empty."""
    path = db_file(root)
    if not os.path.isfile(path):
        raise PlaneUnreachable(f"no plane db at {path}")
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("PRAGMA query_only = 1")
            conn.execute("SELECT 1 FROM identity_registry LIMIT 0")   # the schema is there
            return conn
        except sqlite3.Error as exc:
            last = exc
            if attempt < retries:
                time.sleep(OPEN_RETRY_S)
    raise PlaneUnreachable(f"plane db unreadable: {last}") from last


# Twin of queries.OPEN_ASSIGNMENTS_AT_SQL — BYTE-IDENTICAL (pinned by test), so
# the flipped reader and the shadow's plane side can never drift apart.
OPEN_SQL = (
    "SELECT a.occurred_at, a.source_ref, a.assignment_id, a.expected_by FROM assignments a WHERE"
    " a.assignee_uid = ? AND (? IS NULL OR a.occurred_at <= ?)  AND NOT EXISTS (SELECT 1 FROM ev"
    "ents t WHERE t.kind='task'    AND t.event IN ('completed','failed','cancelled','returned_bl"
    "ocked','superseded','reassigned','expired') AND (? IS NULL OR t.occurred_at <= ?)    AND (t."
    "assignment_id = a.assignment_id      OR (a.source_ref LIKE 'dispatch-log:%' AND a.source_re"
    "f NOT LIKE 'dispatch-log:sha:%'          AND t.assignment_id IN (SELECT s.assignment_id FRO"
    "M assignments s            WHERE s.assignee_uid = a.assignee_uid AND s.source_ref = a.sourc"
    "e_ref              AND (? IS NULL OR s.occurred_at <= ?))))) ORDER BY a.occurred_at, a.inge"
    "st_seq"
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


def open_rows(conn: sqlite3.Connection, fleet: str, bot: str, at: Optional[str] = None,
              *, entry: Optional[dict] = None, idd_only: bool = True
              ) -> list[tuple[int, Optional[int], Optional[str]]]:
    """The legacy ``open_dispatches`` tuple shape — (dispatched_at, expected_by,
    task_id), oldest first — from the plane. ``idd_only`` drops rows with no
    legacy task id (``sha:`` refs, or none): the open LIST is id'd rows only;
    the overdue reader keeps them (task_id None → the legacy ``-``)."""
    entry = entry if entry is not None else bot_entry(conn, fleet, bot)
    out: list[tuple[int, Optional[int], Optional[str]]] = []
    for uid in (entry or {}).get("uids", []):
        for occurred_at, source_ref, _asg, expected_by in conn.execute(
                OPEN_SQL, (uid, at, at, at, at, at, at)):
            tid = None
            if source_ref and source_ref.startswith(DISPATCH) \
                    and not source_ref.startswith(DISPATCH + "sha:"):
                tid = source_ref[len(DISPATCH):]
            if idd_only and tid is None:
                continue
            da = _epoch(occurred_at)
            if da is None:
                continue
            out.append((da, _epoch(expected_by), tid))
    out.sort(key=lambda t: t[0])
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


def declared(conn: sqlite3.Connection, fleet: str, reader: str) -> Optional[str]:
    """The instant of the LATEST ``cutover_declared`` for (fleet, reader), or
    None: a flag nobody declared is not a flip."""
    row = conn.execute(DECLARED_SQL, ("cutover_declared", reader, f"fleet:{fleet}", fleet)).fetchone()
    return row[0] if row else None
