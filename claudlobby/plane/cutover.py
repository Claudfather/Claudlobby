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

import json
import sqlite3
from typing import Optional

from .ids import derive_uid

# The five readers the cutover flipped, one flag each. The shadow that once
# GATED a declaration (a clean run of legacy-vs-plane comparisons) is gone
# with the legacy readers (F18 closure, R2a): there is no legacy side left to
# grade, so every declaration is the direct move the operator ruled.
READERS = ("open", "overdue", "open_task", "unassigned", "events")
GATED = READERS          # the name the callers and the tests grew up with
DIRECT_MOVE_REASON = ("operator ruling 2026-09-03: no backward compat, hard flip, fix forward"
                      " — declared as a direct move (F18 closure: no shadow)")

EVENT_DECLARED = "cutover_declared"
READ_FLAGS = {r: f"PLANE_READ_{r.upper()}" for r in GATED}
# Chunk 6b — the legacy WRITES, per door. Default 1 (keep writing); a fleet
# retires a door's JSONL append with 0, and retiring a write ENDS the shadow
# for every reader that read that ledger (there is no legacy side left to
# grade) — so the retirement is gated on every reader being declared, and
# recorded as its own epoch.
EVENT_RETIRED = "legacy_write_retired"
WRITE_FLAGS = {"dispatch": "PLANE_LEGACY_WRITE_DISPATCH", "report": "PLANE_LEGACY_WRITE_REPORT",
               "workstreams": "PLANE_LEGACY_WRITE_WORKSTREAMS",
               "events": "PLANE_LEGACY_WRITE_EVENTS"}

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
LATEST_RETIRED_SQL = (
    "SELECT e.occurred_at, json_extract(e.detail, '$.forced'), json_extract(e.detail, '$.flags')"
    " FROM events e"
    " WHERE e.kind = 'system' AND e.event = ? AND e.detail_truncated = 0"
    " AND (e.subject_alias = ? OR json_extract(e.detail, '$.fleet') = ?)"
    " ORDER BY e.occurred_at DESC, e.ingest_seq DESC LIMIT 1"
)


def fleet_alias(fleet: str) -> str:
    """The registry mints the fleet identity under its BARE name (measured on
    the estate: `artemis-engineering`, never `fleet:artemis-engineering`) —
    the prefixed form the first build used resolved nothing, so no
    declaration was ever anchored on its fleet."""
    return fleet


def fleet_uid(conn: sqlite3.Connection, fleet: str) -> Optional[str]:
    row = conn.execute(FLEET_UID_SQL, (fleet_alias(fleet),)).fetchone()
    return row[0] if row else None


def declaration_event(fleet: str, reader: str, now: str, *,
                      forced: Optional[str] = None, subject_uid: Optional[str] = None) -> dict:
    """The system event recording the flip. Id derived from (fleet, reader,
    instant, reason): a re-run at the same instant is one fact, not two, while
    a CORRECTED reason at that instant is a new fact, never dropped as a
    duplicate. Anchored on the fleet's identity when it has one. Every
    declaration is a DIRECT MOVE (shadowed=false, no gate, the reason
    recorded): the shadow that once gated a reader went with the legacy side."""
    if reader not in READ_FLAGS:
        raise ValueError(f"unknown reader {reader!r}")
    forced = forced or DIRECT_MOVE_REASON
    shadowed = False
    gate = {"clean_run": None, "transitions": None}
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
                "shadowed": shadowed,
                "gate": gate,
                "gate_met": None,
                "forced": forced,
                "streaks": [],
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


def undeclared(decl: dict[str, tuple[str, Optional[str]]]) -> list[str]:
    """The readers a write retirement still waits on."""
    return [r for r in GATED if r not in decl]


def retirement_event(fleet: str, decl: dict[str, tuple[str, Optional[str]]], now: str, *,
                     forced: Optional[str] = None, subject_uid: Optional[str] = None) -> dict:
    """The system event recording the retirement of the legacy writes: the
    doors, the declarations it stands on, the flags it expects. Id derived
    from (fleet, instant, reason) like the declaration's."""
    return {
        "event_type": "system",
        "emitter": "plane-cutover",
        "fleet": fleet,
        "occurred_at": now,
        "event_id": derive_uid("ev", f"retire:{fleet}:{now}:{forced or ''}"),
        "payload": {
            "event": EVENT_RETIRED,
            **({"subject_kind": "fleet", "subject_uid": subject_uid,
                "subject_alias": fleet_alias(fleet)} if subject_uid else {}),
            "data": {
                "fleet": fleet,
                "flags": {d: f"{f}=0" for d, f in WRITE_FLAGS.items()},
                "declared": {r: at for r, (at, _f) in decl.items()},
                "undeclared": undeclared(decl),
                "forced": forced,
            },
        },
    }


def retired(conn: sqlite3.Connection, fleet: str) -> Optional[tuple[str, Optional[str]]]:
    """(instant, forced reason or None) of the LATEST retirement on this
    fleet, or None: the legacy writes are still the record."""
    row = conn.execute(LATEST_RETIRED_SQL, (EVENT_RETIRED, fleet_alias(fleet), fleet)).fetchone()
    return (row[0], row[1]) if row else None


def retired_doors(conn: sqlite3.Connection, fleet: str) -> set[str]:
    """The doors the LATEST retirement names (its recorded flags). A record
    from before a door existed names nothing for it, so the door keeps
    writing and `--retire-writes` records the extension rather than
    answering 'already retired'."""
    row = conn.execute(LATEST_RETIRED_SQL, (EVENT_RETIRED, fleet_alias(fleet), fleet)).fetchone()
    if not row or not row[2]:
        return set()
    try:
        flags = json.loads(row[2])
    except ValueError:
        return set()
    return set(flags) if isinstance(flags, dict) else set()


def _flag_verdict(flag_active: bool, recorded_at: Optional[str], *, flag_only: str,
                  record_only: str, both: str, neither: str) -> tuple[bool, str]:
    """The doctor's verdict for one flag against its record — ONE 4-branch shape
    for both epochs: consistent (both or neither), or which half is missing."""
    if flag_active and recorded_at is None:
        return False, flag_only
    if not flag_active and recorded_at is not None:
        return False, record_only.format(at=recorded_at)
    if flag_active:
        return True, both.format(at=recorded_at)
    return True, neither


def write_flag_vs_retirement(flag_retired: bool, retired_at: Optional[str]) -> tuple[bool, str]:
    return _flag_verdict(
        flag_retired, retired_at,
        flag_only="flag retired (0) but NO retirement recorded — the door keeps writing the ledger"
                  " and says so; `plane cutover --retire-writes` first",
        record_only="retirement recorded {at} but the flag still writes — the door has not stopped",
        both="retired (recorded {at})", neither="writing (not retired)")


def flag_vs_declaration(flag_on: bool, declared_at: Optional[str]) -> tuple[bool, str]:
    return _flag_verdict(
        flag_on, declared_at,
        flag_only="flag set but NO declaration recorded — the matcher keeps serving the JSONL;"
                  " `plane cutover --reader` first",
        record_only="declared {at} but the flag is off — the flip did not land",
        both="flipped to the plane (declared {at})", neither="legacy (not declared, not flipped)")
