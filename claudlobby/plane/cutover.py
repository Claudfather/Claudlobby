"""The cutover EPOCH — a reader's flip to the plane, recorded when it happens
(cutover chunk 5 of the F18 walk).

Pure logic; ``commands/plane.py`` wraps it. The flip is TWO facts: a
per-reader flag (``PLANE_READ_OPEN`` / ``PLANE_READ_OVERDUE``, the fleet
``.env`` tier → composed into ``bot.conf`` for the session doors and stamped on
the fleet-pulse unit) AND a ``cutover_declared`` record for (fleet, reader) —
the matcher serves the plane only when both hold, so a flag set ahead of the
declaration is disclosed, never a silent flip. ``plane cutover --reader R``
refuses unless the J4 gate is met for that reader on every declared bot,
records the declaration (the streaks at that instant, or the ``--force``
reason) anchored on the FLEET's identity, and prints the flag line. The doctor
reads the declaration back against the flag.

Twin: ``lib/plane-readers.py::declared`` (stdlib, the matcher's side).
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from .ids import derive_uid
from .shadow import GATE_CLEAN_RUN, GATE_TRANSITIONS, READERS, Streak

EVENT_DECLARED = "cutover_declared"
READ_FLAGS = {r: f"PLANE_READ_{r.upper()}" for r in READERS}

# The fleet is matched on the anchor COLUMN first (survives a truncated
# detail), the detail's fleet second (a declaration recorded before the fleet
# had a registry identity carries no anchor).
LATEST_DECLARED_SQL = (
    "SELECT json_extract(e.detail, '$.reader'), e.occurred_at,"
    " json_extract(e.detail, '$.forced') FROM events e"
    " WHERE e.kind = 'system' AND e.event = ? AND e.detail_truncated = 0"
    " AND (e.subject_alias = ? OR json_extract(e.detail, '$.fleet') = ?)"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC"
)
FLEET_UID_SQL = "SELECT uid FROM identity_registry WHERE alias = ? AND kind = 'fleet' LIMIT 1"


def fleet_alias(fleet: str) -> str:
    return f"fleet:{fleet}"


def fleet_uid(conn: sqlite3.Connection, fleet: str) -> Optional[str]:
    row = conn.execute(FLEET_UID_SQL, (fleet_alias(fleet),)).fetchone()
    return row[0] if row else None


def unmet(streaks: list[Streak]) -> list[Streak]:
    return [s for s in streaks if not s.gate_ok]


def declaration_event(fleet: str, reader: str, streaks: list[Streak], now: str, *,
                      forced: Optional[str] = None, subject_uid: Optional[str] = None) -> dict:
    """The system event recording the flip. Id derived from (fleet, reader,
    instant, reason): a re-run at the same instant is one fact, not two, while
    a CORRECTED reason at that instant is a new fact, never dropped as a
    duplicate. Anchored on the fleet's identity when it has one."""
    if reader not in READ_FLAGS:
        raise ValueError(f"unknown reader {reader!r}")
    return {
        "event_type": "system",
        "emitter": "plane-cutover",
        "fleet": fleet,
        "occurred_at": now,
        "event_id": derive_uid("ev", f"cutover:{fleet}:{reader}:{now}:{forced or ''}"),
        "payload": {
            "event": EVENT_DECLARED,
            **({"subject_kind": "fleet", "subject_uid": subject_uid,
                "subject_alias": fleet_alias(fleet)} if subject_uid else {}),
            "data": {
                "fleet": fleet,
                "reader": reader,
                "flag": READ_FLAGS[reader],
                "gate": {"clean_run": GATE_CLEAN_RUN, "transitions": GATE_TRANSITIONS},
                "gate_met": not unmet(streaks),
                "forced": forced,
                "streaks": [
                    {"bot": s.bot, "clean_run": s.clean_run,
                     "transitions": s.transitions, "gate_ok": s.gate_ok}
                    for s in streaks],
            },
        },
    }


def declared(conn: sqlite3.Connection, fleet: str) -> dict[str, tuple[str, Optional[str]]]:
    """reader → (instant, forced reason or None) for the LATEST declaration
    per reader on this fleet; a reader never declared is absent."""
    out: dict[str, tuple[str, Optional[str]]] = {}
    for reader, at, forced in conn.execute(LATEST_DECLARED_SQL,
                                           (EVENT_DECLARED, fleet_alias(fleet), fleet)):
        if reader in READ_FLAGS and reader not in out:
            out[reader] = (at, forced)
    return out


def flag_vs_declaration(flag_on: bool, declared_at: Optional[str]) -> tuple[bool, str]:
    """The doctor's verdict for one reader: consistent, or which half is missing."""
    if flag_on and declared_at is None:
        return False, ("flag set but NO declaration recorded — the matcher keeps serving the"
                       " JSONL; `plane cutover --reader` first")
    if not flag_on and declared_at is not None:
        return False, f"declared {declared_at} but the flag is off — the flip did not land"
    if flag_on:
        return True, f"flipped to the plane (declared {declared_at})"
    return True, "legacy (not declared, not flipped)"
