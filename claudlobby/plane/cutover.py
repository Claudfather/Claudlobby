"""The cutover EPOCH — a reader's flip to the plane, recorded when it happens
(cutover chunk 5 of the F18 walk).

Pure logic; ``commands/plane.py`` wraps it. The flip itself is a per-reader
flag in the fleet ``.env`` tier (``PLANE_READ_OPEN`` / ``PLANE_READ_OVERDUE``)
that the matcher and brief read; this module makes the flip a FACT the ledger
carries: ``plane cutover --reader R`` refuses unless the J4 gate is met for
that reader on every declared bot, records a ``cutover_declared`` system event
(the streaks at that instant, or the ``--force`` reason), and prints the flag
line. The doctor reads the declaration back against the flag: a flag with no
declaration, or a declaration with the flag off, is disclosed.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from .ids import derive_uid
from .shadow import GATE_CLEAN_RUN, GATE_TRANSITIONS, Streak

EVENT_DECLARED = "cutover_declared"
READ_FLAGS = {"open": "PLANE_READ_OPEN", "overdue": "PLANE_READ_OVERDUE"}

LATEST_DECLARED_SQL = (
    "SELECT json_extract(e.detail, '$.reader'), e.occurred_at,"
    " json_extract(e.detail, '$.forced') FROM events e"
    f" WHERE e.kind = 'system' AND e.event = '{EVENT_DECLARED}'"
    " AND e.detail_truncated = 0 AND json_extract(e.detail, '$.fleet') = ?"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC"
)


def unmet(streaks: list[Streak]) -> list[Streak]:
    return [s for s in streaks if not s.gate_ok]


def declaration_event(fleet: str, reader: str, streaks: list[Streak], now: str,
                      *, forced: Optional[str] = None) -> dict:
    """The system event recording the flip. Id derived from (fleet, reader,
    instant) so a re-run at the same instant is one fact, not two."""
    if reader not in READ_FLAGS:
        raise ValueError(f"unknown reader {reader!r}")
    return {
        "event_type": "system",
        "emitter": "plane-cutover",
        "fleet": fleet,
        "occurred_at": now,
        "event_id": derive_uid("ev", f"cutover:{fleet}:{reader}:{now}"),
        "payload": {
            "event": EVENT_DECLARED,
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
    for reader, at, forced in conn.execute(LATEST_DECLARED_SQL, (fleet,)):
        if reader in READ_FLAGS and reader not in out:
            out[reader] = (at, forced)
    return out


def flag_vs_declaration(flag_on: bool, declared_at: Optional[str]) -> tuple[bool, str]:
    """The doctor's verdict for one reader: consistent, or which half is missing."""
    if flag_on and declared_at is None:
        return False, "flag set but NO declaration recorded — `plane cutover --reader` first"
    if not flag_on and declared_at is not None:
        return False, f"declared {declared_at} but the flag is off — the flip did not land"
    if flag_on:
        return True, f"flipped to the plane (declared {declared_at})"
    return True, "legacy (not declared, not flipped)"
