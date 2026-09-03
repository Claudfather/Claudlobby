"""Legacy-ledger <-> plane parity, as the OPERATOR'S door (cutover chunk 2, #1444).

``lib/plane-parity.py`` is the generic instrument — any ledger, any id
field, any table — and it had never been run against a live ledger for
exactly that reason: six flags, an id field that differs per ledger, a
timestamp form that rejects ``Z``. This module bakes in what the estate
actually has, so the question "is the plane a complete record of the
legacy ledgers?" is one command:

- **the two ledgers and their join keys.** ``dispatch-log.jsonl`` is
  host-global; a TASK row joins the plane by ``source_ref =
  "dispatch-log:<task_id>"`` on assignments, while a QUERY row carries no
  task id BY DESIGN (dispatch-task: a query must degrade to untracked, never
  to unclosable) and joins by its ``plane_msg_id`` on communications. The
  per-fleet ``report-back.jsonl`` joins by ``plane_msg_id`` on
  communications. A row the importer landed under content-hashed ids joins
  by its ``<ledger>:sha:<hash>`` source_ref.
- **a cause for every missing row**, derived from the row and the plane,
  never guessed: ``pre-go-live`` (older than the first row that ledger's
  door ever landed in the plane), ``unstamped`` (the door ran with the plane
  disarmed — the row carries no plane ids; measured on the Mini this was
  every dispatch from the unarmed fleet), ``stamped-lost`` (ids were
  minted, no row landed — the emit was lost; measured 0 over five days).
- **multiplicity per (source_ref, kind, event)**: one report legitimately
  yields two task events (``completed`` + ``supplied_id_not_open``), which
  the generic instrument counted as a duplicate.

Pure over (conn, ledger path): no writes, no emits. Unreachable != empty
(``source_state``): an absent or unreadable ledger or db REFUSES; an
existing ledger holding no rows is a fleet that has not written yet.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..source_state import SOURCE_OK, probe_source, unreachable_line
from .db import connect_ro  # noqa: F401 — the read-only open every parity consumer uses
from .ids import derive_hex

DISPATCH = "dispatch-log"
REPORT = "report-back"
LEDGERS = (DISPATCH, REPORT)
DOOR_EMITTER = {DISPATCH: "dispatch-task", REPORT: "report-back"}

CAUSE_PRE_GO_LIVE = "pre-go-live"
CAUSE_UNSTAMPED = "unstamped"
CAUSE_STAMPED_LOST = "stamped-lost"

PLANE_ID_FIELDS = ("plane_msg_id", "plane_work_item_id", "plane_assignment_id")


def content_key(raw_line: str) -> str:
    """The content hash an importer stamps as ``<ledger>:sha:<key>`` — over
    the raw line, never its position: rotation rewrites the file."""
    return derive_hex(raw_line)


def epoch_iso(epoch) -> Optional[str]:
    """The ledgers store instants as epoch seconds (``dispatched_at``,
    ``expected_by``; null or junk when absent); the contract and the shadow
    want an aware ISO instant. ONE definition — the importer and the shadow
    both ride it, and a zero/negative/junk value is ABSENT (None), never
    1970."""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def ts19(value) -> str:
    """The comparable UTC instant: ``YYYY-MM-DDTHH:MM:SS`` — both the ledger
    (``...Z``) and the plane (``...+00:00``) write UTC, so the first 19
    characters compare across the two forms."""
    return str(value or "")[:19]


@dataclass
class LegacyRow:
    raw: str
    row: dict

    @property
    def ts(self) -> str:
        return str(self.row.get("ts") or "")

    @property
    def stamped(self) -> bool:
        return any(self.row.get(k) for k in PLANE_ID_FIELDS)


@dataclass
class Missing:
    key: str
    cause: str
    row: LegacyRow

    @property
    def ts(self) -> str:
        return self.row.ts


@dataclass
class LedgerParity:
    ledger: str
    path: Path
    state: str                       # ok | empty | absent | unreadable
    detail: str = ""
    total: int = 0
    malformed: int = 0
    missing: list[Missing] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    go_live: Optional[str] = None    # first occurred_at this ledger's door landed

    @property
    def reachable(self) -> bool:
        return self.state in ("ok", "empty")

    @property
    def matched(self) -> int:
        return self.total - len(self.missing)     # derived: every judged row is one or the other

    @property
    def clean(self) -> bool:
        return self.reachable and not self.missing and not self.duplicates

    def causes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.missing:
            out[m.cause] = out.get(m.cause, 0) + 1
        return out


def read_ledger(path: Path) -> tuple[str, str, list[LegacyRow], int]:
    """(state, detail, rows, malformed). Absent / unreadable are REFUSALS
    (the caller must not read them as parity-clean); an existing file with
    no rows is ``empty``. Malformed lines are counted, never dropped silently."""
    probe = probe_source(path)
    if probe.state != SOURCE_OK:
        return probe.state, unreachable_line(str(path), probe), [], 0
    rows: list[LegacyRow] = []
    malformed = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "unreadable", f"{path}: {exc}", [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
        except (json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        rows.append(LegacyRow(line, row))
    return ("ok" if rows else "empty"), "", rows, malformed


@dataclass
class PlaneKeys:
    sha_keys: set[str]
    go_live: Optional[str]


def plane_keys(conn: sqlite3.Connection, ledger: str) -> PlaneKeys:
    """The two facts about *ledger* the plane holds as a SET. Both live on
    ``communications``: every batch either door lands carries a communication
    (a task dispatch's wi+asg+comm, a query's bare comm, a report's comm),
    so the door's first communication IS its go-live, and an imported row
    always lands a communication under its ``<ledger>:sha:`` ref. Presence by
    task id / msg id is a per-row point lookup instead (both indexed), sized
    to the ledger rather than the plane."""
    sha_keys = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_ref FROM communications WHERE source_ref LIKE ?",
        (f"{ledger}:sha:%",))}
    first = conn.execute(
        "SELECT MIN(occurred_at) FROM communications WHERE emitter = ?",
        (DOOR_EMITTER[ledger],)).fetchone()[0]
    return PlaneKeys(sha_keys, ts19(first) or None)


def duplicates(conn: sqlite3.Connection, ledger: str) -> list[str]:
    """Same (source_ref, kind, event) landed more than once."""
    return [
        f"{ref} {kind}/{ev or '-'}: {n} rows"
        for ref, kind, ev, n in conn.execute(
            "SELECT source_ref, kind, COALESCE(event, ''), COUNT(*) FROM events"
            " WHERE source_ref LIKE ? GROUP BY 1, 2, 3 HAVING COUNT(*) > 1"
            " ORDER BY 1, 2, 3", (f"{ledger}:%",))]


def row_key(ledger: str, row: LegacyRow) -> str:
    """The join key parity and the importer agree on for a row."""
    if ledger == DISPATCH and row.row.get("task_id"):
        return f"task:{row.row['task_id']}"
    if row.row.get("plane_msg_id"):
        return f"msg:{row.row['plane_msg_id']}"
    return f"sha:{content_key(row.raw)}"


def present(conn: sqlite3.Connection, ledger: str, row: LegacyRow, keys: PlaneKeys) -> bool:
    """The same precedence ``row_key`` uses: task id, then msg id, then the
    content key — each a point lookup on an indexed column."""
    task_id = row.row.get("task_id")
    if ledger == DISPATCH and task_id and conn.execute(
            "SELECT 1 FROM assignments WHERE source_ref = ? LIMIT 1",
            (f"{DISPATCH}:{task_id}",)).fetchone():
        return True
    msg = row.row.get("plane_msg_id")
    if msg and conn.execute(
            "SELECT 1 FROM communications WHERE msg_id = ? LIMIT 1", (msg,)).fetchone():
        return True
    return f"{ledger}:sha:{content_key(row.raw)}" in keys.sha_keys


def cause_of(row: LegacyRow, go_live: Optional[str]) -> str:
    if row.stamped:
        return CAUSE_STAMPED_LOST
    if go_live and ts19(row.ts) < go_live:
        return CAUSE_PRE_GO_LIVE
    return CAUSE_UNSTAMPED


def compare(conn: sqlite3.Connection, ledger: str, path: Path, *,
            since: Optional[str] = None,
            ledger_read: Optional[tuple] = None) -> LedgerParity:
    """One ledger against the plane. *since* is an ISO instant (``Z`` or
    offset form); rows older than it are outside the window and not judged.
    A caller that already read the ledger passes ``read_ledger``'s tuple as
    *ledger_read* so the file is parsed once."""
    if ledger not in LEDGERS:
        raise ValueError(f"unknown ledger {ledger!r}")
    state, detail, rows, malformed = ledger_read or read_ledger(path)
    out = LedgerParity(ledger, path, state, detail, malformed=malformed)
    if not out.reachable:
        return out
    keys = plane_keys(conn, ledger)
    out.go_live = keys.go_live
    floor = ts19(since) if since else ""
    for row in rows:
        if floor and ts19(row.ts) < floor:
            continue
        out.total += 1
        if not present(conn, ledger, row, keys):
            out.missing.append(Missing(row_key(ledger, row), cause_of(row, keys.go_live), row))
    out.duplicates = duplicates(conn, ledger)
    return out
