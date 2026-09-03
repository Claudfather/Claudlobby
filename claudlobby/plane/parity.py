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

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..source_state import SOURCE_OK, probe_source, unreachable_line

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
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()[:32]


def ts19(value) -> str:
    """The comparable UTC instant: ``YYYY-MM-DDTHH:MM:SS`` — both the ledger
    (``...Z``) and the plane (``...+00:00``) write UTC, so the first 19
    characters compare across the two forms."""
    return str(value or "")[:19]


@dataclass
class LegacyRow:
    line_no: int
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
    ts: str
    cause: str
    row: LegacyRow


@dataclass
class LedgerParity:
    ledger: str
    path: Path
    state: str                       # ok | empty | absent | unreadable
    detail: str = ""
    total: int = 0
    malformed: int = 0
    matched: int = 0
    missing: list[Missing] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    go_live: Optional[str] = None    # first occurred_at this ledger's door landed

    @property
    def reachable(self) -> bool:
        return self.state in ("ok", "empty")

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
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
        except (json.JSONDecodeError, ValueError):
            malformed += 1
            continue
        rows.append(LegacyRow(n, line, row))
    return ("ok" if rows else "empty"), "", rows, malformed


def connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only, and the file must ALREADY exist: ``db.connect`` auto-creates
    a db, so a typo'd root would otherwise compare against an empty plane
    and report every row missing (the J1 exists-before-connect finding)."""
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = 1")
    return conn


@dataclass
class PlaneKeys:
    task_ids: set[str]
    msg_ids: set[str]
    sha_keys: set[str]
    go_live: Optional[str]


TABLES = ("communications", "work_items", "assignments", "events")


def plane_keys(conn: sqlite3.Connection, ledger: str) -> PlaneKeys:
    """Everything the plane holds that a row of *ledger* can join to. The
    plane keeps one table per family (communications, work_items,
    assignments, events) — a msg id lives on communications, never events."""
    task_ids: set[str] = set()
    if ledger == DISPATCH:
        task_ids = {
            r[0][len(DISPATCH) + 1:] for r in conn.execute(
                "SELECT source_ref FROM assignments WHERE source_ref LIKE ?",
                (f"{DISPATCH}:%",))
            if r[0] and not r[0].startswith(f"{DISPATCH}:sha:")}
    msg_ids = {r[0] for r in conn.execute("SELECT msg_id FROM communications")}
    sha_keys: set[str] = set()
    go_live: Optional[str] = None
    for table in TABLES:
        sha_keys |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT source_ref FROM {table} WHERE source_ref LIKE ?",
            (f"{ledger}:sha:%",))}
        first = conn.execute(
            f"SELECT MIN(occurred_at) FROM {table} WHERE emitter = ?",
            (DOOR_EMITTER[ledger],)).fetchone()[0]
        if first and (go_live is None or ts19(first) < go_live):
            go_live = ts19(first)
    return PlaneKeys(task_ids, msg_ids, sha_keys, go_live)


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


def present(ledger: str, row: LegacyRow, keys: PlaneKeys) -> bool:
    task_id = row.row.get("task_id")
    if ledger == DISPATCH and task_id and task_id in keys.task_ids:
        return True
    msg = row.row.get("plane_msg_id")
    if msg and msg in keys.msg_ids:
        return True
    return f"{ledger}:sha:{content_key(row.raw)}" in keys.sha_keys


def cause_of(row: LegacyRow, go_live: Optional[str]) -> str:
    if row.stamped:
        return CAUSE_STAMPED_LOST
    if go_live and ts19(row.ts) < go_live:
        return CAUSE_PRE_GO_LIVE
    return CAUSE_UNSTAMPED


def compare(conn: sqlite3.Connection, ledger: str, path: Path, *,
            since: Optional[str] = None) -> LedgerParity:
    """One ledger against the plane. *since* is an ISO instant (``Z`` or
    offset form); rows older than it are outside the window and not judged."""
    if ledger not in LEDGERS:
        raise ValueError(f"unknown ledger {ledger!r}")
    state, detail, rows, malformed = read_ledger(path)
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
        if present(ledger, row, keys):
            out.matched += 1
        else:
            out.missing.append(Missing(row_key(ledger, row), row.ts,
                                       cause_of(row, keys.go_live), row))
    out.duplicates = duplicates(conn, ledger)
    return out
