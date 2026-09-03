#!/usr/bin/env python3
"""plane-readers.py — the plane answering the two LIST readers, stdlib (cutover chunk 5).

The matcher (``dispatch-overdue.py``) is a stdlib script every consumer shells,
so its plane source cannot import the package (the ``plane-lookup.py`` /
``plane-shadow-check.py`` precedent). This module is the stdlib twin of two
package definitions — keep them in step:

- ``open_rows``   ↔ ``claudlobby.plane.queries.OPEN_ASSIGNMENTS_AT_SQL`` +
                    ``claudlobby.plane.shadow.plane_open``
- ``overdue_rows`` ↔ ``claudlobby.plane.shadow.plane_overdue`` (deadline
                    passed, the #460 expiry cap, the bot's own ``progress``
                    inside the grace — the watchdog's rules, mirrored)

Read-only (``mode=ro`` + ``query_only``); a missing db raises
``PlaneUnreachable`` — the caller refuses, it never falls back to the JSONL:
rollback is the flag, not a silent fallback.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

class PlaneUnreachable(RuntimeError):
    pass


def db_file(root: str) -> str:
    return os.path.join(root, "state", "plane", "plane.db")


def connect(root: str) -> sqlite3.Connection:
    path = db_file(root)
    if not os.path.isfile(path):
        raise PlaneUnreachable(f"no plane db at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = 1")
        return conn
    except sqlite3.Error as exc:
        raise PlaneUnreachable(f"plane db unreadable: {exc}") from exc


# Twin of queries.OPEN_ASSIGNMENTS_AT_SQL — BYTE-IDENTICAL (pinned by test), so
# the flipped reader and the shadow's plane side can never drift apart.
OPEN_SQL = (
    'SELECT a.occurred_at, a.source_ref, a.assignment_id, a.expected_by FROM assignments a WHER'
    'E a.assignee_uid = ? AND (? IS NULL OR a.occurred_at <= ?)  AND NOT EXISTS (SELECT 1 FROM '
    "events t WHERE t.kind='task'    AND t.event IN ('completed','failed','cancelled','returned"
    "_blocked','superseded','reassigned','expired') AND (? IS NULL OR t.occurred_at <= ?)    AN"
    "D (t.assignment_id = a.assignment_id      OR (a.source_ref LIKE 'dispatch-log:%' AND a.sou"
    "rce_ref NOT LIKE 'dispatch-log:sha:%'          AND t.assignment_id IN (SELECT s.assignment"
    '_id FROM assignments s            WHERE s.assignee_uid = a.assignee_uid AND s.source_ref ='
    ' a.source_ref              AND (? IS NULL OR s.occurred_at <= ?))))) ORDER BY a.occurred_a'
    't, a.ingest_seq'
)
UIDS_SQL = "SELECT uid FROM identity_registry WHERE lower(alias) = lower(?)"
ACTOR_SQL = "SELECT uid FROM identity_registry WHERE alias = ? AND kind = 'actor' LIMIT 1"
LAST_PROGRESS_SQL = (
    "SELECT MAX(e.occurred_at) FROM events e WHERE e.kind = 'task' AND e.event = 'progress'"
    " AND e.actor_uid = ? AND e.occurred_at <= ?"
)
ROSTER_SQL = "SELECT DISTINCT alias FROM identity_registry WHERE kind = 'actor' AND alias LIKE ?"


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


def open_rows(conn: sqlite3.Connection, fleet: str, bot: str,
              at: str | None = None) -> list[tuple[int, int | None, str]]:
    """The legacy ``open_dispatches`` tuple shape — (dispatched_at,
    expected_by, task_id), oldest first — from the plane. Assignments with no
    legacy task id (none exists for a task row) are skipped: the legacy list
    is id'd rows only."""
    out: list[tuple[int, int | None, str]] = []
    for (uid,) in conn.execute(UIDS_SQL, (f"bot:{fleet}/{bot}",)).fetchall():
        for occurred_at, source_ref, _asg, expected_by in conn.execute(
                OPEN_SQL, (uid, at, at, at, at, at, at)):
            if not source_ref or not source_ref.startswith("dispatch-log:") \
                    or source_ref.startswith("dispatch-log:sha:"):
                continue
            da = _epoch(occurred_at)
            if da is None:
                continue
            out.append((da, _epoch(expected_by), source_ref[len("dispatch-log:"):]))
    out.sort(key=lambda t: t[0])
    return out


def overdue_rows(conn: sqlite3.Connection, fleet: str, bot: str, *, now: int,
                 max_age: int, progress_grace: int) -> list[tuple[int, int, int, str]]:
    """The legacy ``--all`` row shape — (dispatched_at, expected_by, elapsed,
    task_id) — from the plane, the watchdog's rules mirrored."""
    at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    rows = open_rows(conn, fleet, bot, at)
    last_progress = None
    if progress_grace > 0:
        hit = conn.execute(ACTOR_SQL, (f"bot:{fleet}/{bot}",)).fetchone()
        if hit:
            row = conn.execute(LAST_PROGRESS_SQL, (hit[0], at)).fetchone()
            last_progress = _epoch(row[0]) if row and row[0] else None
    out: list[tuple[int, int, int, str]] = []
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


def roster(conn: sqlite3.Connection, fleet: str) -> list[str]:
    """Every bot the plane knows for the fleet (actor aliases)."""
    prefix = f"bot:{fleet}/"
    return sorted(r[0][len(prefix):] for r in conn.execute(ROSTER_SQL, (prefix + "%",))
                  if r[0].startswith(prefix))
