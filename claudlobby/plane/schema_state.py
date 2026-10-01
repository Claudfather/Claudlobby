"""Read-only schema admission for ordinary plane readers and writers.

Only explicit migration apply may run the migration runner, including initial
database creation. Admission checks the SQL version; release/pending-format
compatibility and writer quiescence belong to the activation manifest owner.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .db import connect_ro, db_file
from .migrations import DowngradeError, SCHEMA_USER_VERSION


class PendingMigrationError(RuntimeError):
    """The database is absent or older than this release; apply is required."""


def require_current_schema(conn: sqlite3.Connection) -> int:
    """Refuse an unapplied or newer schema without changing the connection.

    Recheck the actual connection before reading or writing. This does not
    commit, acquire a write lock, or replace activation's quiescence boundary.
    Newer databases retain the runner's existing downgrade refusal type.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_USER_VERSION:
        raise DowngradeError(
            f"plane.db user_version={current} is newer than this code"
            f" (supports <={SCHEMA_USER_VERSION}) — refusing downgrade"
        )
    if current < SCHEMA_USER_VERSION:
        raise PendingMigrationError(
            f"plane.db user_version={current} requires migration to"
            f" {SCHEMA_USER_VERSION} — run explicit migration apply before use"
        )
    return current


def preflight_schema(root: Path, *, timeout: float = 5.0) -> int:
    """Check an existing host database before any writable connection opens.

    ``db_path``/``connect`` provision storage and change journal settings, so
    they must follow this check. Absence is a pending initialization, not
    permission for an ordinary emit, read, or daemon startup to create a DB.
    """
    path = db_file(root)
    try:
        conn = connect_ro(path, timeout=timeout)
    except FileNotFoundError as exc:
        raise PendingMigrationError(
            f"no plane db at {path} — run explicit migration apply to initialize it"
        ) from exc
    try:
        return require_current_schema(conn)
    finally:
        conn.close()
