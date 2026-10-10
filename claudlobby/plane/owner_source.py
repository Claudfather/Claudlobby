"""Explicit local attestation of the recorder source; reads never bind or repair.

The operator attests unattributed/imported history when binding. This is not a
cryptographic provenance claim against a same-UID host adversary. Each protected
query checks retained host facts and the marker in its own SQLite snapshot.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .db import connect_ro, db_file
from .ids import read_host_uid
from .ingest import CONSTRUCT_TABLES
from .owner_access import AccessDenied, AccessUnavailable
from .migrations import DowngradeError
from .schema_state import PendingMigrationError, require_current_schema


HOST_TABLES = (*dict.fromkeys(CONSTRUCT_TABLES.values()), "events")
_UNAVAILABLE = (sqlite3.Error, OSError, PendingMigrationError, DowngradeError)


def source_host_uid(root: Path) -> str:
    try:
        return read_host_uid(Path(root) / "state")
    except (ValueError, OSError) as exc:
        raise AccessUnavailable("owner source unavailable") from exc


def _marker(conn: sqlite3.Connection, host_uid: str, *, required: bool) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("owner_source_binding",)
    ).fetchone()
    if not exists:
        if required:
            raise AccessUnavailable("owner source unavailable")
        return False
    rows = conn.execute("SELECT singleton, host_uid FROM owner_source_binding LIMIT 2").fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str):
        raise AccessUnavailable("owner source unavailable")
    if rows[0][1] != host_uid:
        raise AccessDenied("owner source denied")
    return True


def _retained(conn: sqlite3.Connection, host_uid: str) -> None:
    # No active/fleet filter: legacy, null-fleet and tombstone rows count too.
    # Separate extrema use the operator-created covering indexes. Equal
    # BINARY extrema imply every retained host equals the expected identity.
    # Bound pathological databases without fetching whole tables or holding an
    # arbitrarily long read snapshot. This budget applies only to admission.
    deadline = time.monotonic() + 2.0
    remaining = 20_000_000

    def progress():
        nonlocal remaining
        remaining -= 10_000
        return int(remaining <= 0 or time.monotonic() >= deadline)

    conn.set_progress_handler(progress, 10_000)
    try:
        for table in HOST_TABLES:
            for direction in ("ASC", "DESC"):
                row = conn.execute(
                    f"SELECT host_uid FROM {table}"
                    f" ORDER BY host_uid COLLATE BINARY {direction} LIMIT 1"
                ).fetchone()
                if row is not None and row[0] != host_uid:
                    raise AccessDenied("owner source denied")
        if conn.execute(
            "SELECT 1 FROM identity_registry WHERE kind='fleet'"
            " AND parent_uid IS NOT NULL AND parent_uid IS NOT ? LIMIT 1",
            (host_uid,),
        ).fetchone():
            raise AccessDenied("owner source denied")
    finally:
        conn.set_progress_handler(None, 0)


def admit_source(conn: sqlite3.Connection, host_uid: str) -> None:
    """Require binding and retained invariants in the caller's read transaction."""
    if not conn.in_transaction:
        raise AccessUnavailable("owner source unavailable")
    try:
        require_current_schema(conn)
        _marker(conn, host_uid, required=True)
        _retained(conn, host_uid)
    except _UNAVAILABLE as exc:
        raise AccessUnavailable("owner source unavailable") from exc


def inspect_source(root: Path) -> str:
    """Read-only startup check; query admission must still run per snapshot."""
    host_uid = source_host_uid(root)
    conn = None
    try:
        conn = connect_ro(db_file(root))
        conn.execute("BEGIN")
        admit_source(conn, host_uid)
        return host_uid
    except _UNAVAILABLE as exc:
        raise AccessUnavailable("owner source unavailable") from exc
    finally:
        if conn is not None:
            conn.close()


def bind_source(root: Path, *, expected_host_uid: str | None = None) -> str:
    """Operator-only explicit attestation, existing DB only, never foreign rebind.

    Authority/interactive confirmation belong to the local CLI caller. This
    narrow writer creates metadata and read indexes, with no schema-version migration.
    """
    host_uid = source_host_uid(root)
    if expected_host_uid is not None and host_uid != expected_host_uid:
        raise AccessDenied("owner source denied")
    path = db_file(root)
    conn = None
    try:
        if path.resolve() != path.absolute():
            raise AccessUnavailable("owner source unavailable")
        # Share the reader's regular-file check before SQLite can open a FIFO.
        probe = connect_ro(path)
        probe.close()
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=rw", uri=True,
                               isolation_level=None, timeout=5.0)
        conn.execute("BEGIN IMMEDIATE")
        require_current_schema(conn)
        present = _marker(conn, host_uid, required=False)
        # Operator-authorized indexes only; a refusal rolls their DDL back.
        # Readers never create indexes, including on a retained older marker.
        for table in HOST_TABLES:
            conn.execute(f"CREATE INDEX IF NOT EXISTS owner_source_{table}_host"
                         f" ON {table}(host_uid COLLATE BINARY)")
        _retained(conn, host_uid)
        if not present:
            conn.execute("CREATE TABLE owner_source_binding ("
                         "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                         "host_uid TEXT NOT NULL)")
            conn.execute("INSERT INTO owner_source_binding VALUES (1, ?)", (host_uid,))
        # A concurrent installation identity change must not bind stale state.
        if source_host_uid(root) != host_uid:
            raise AccessUnavailable("owner source unavailable")
        conn.commit()
        return host_uid
    except _UNAVAILABLE as exc:
        raise AccessUnavailable("owner source unavailable") from exc
    finally:
        if conn is not None:
            conn.close()  # rolls back every refused binding
