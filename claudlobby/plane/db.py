"""Connection factory + path resolution (design v2 F3).

The db is HOST-scoped: <root>/state/plane/plane.db — outside every vault
working tree so message bodies can never ride a vault sync (spec §5).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def db_file(root: Path) -> Path:
    """WHERE the db lives — a pure join, no side effect. Readers and every
    exists-before-connect check use this; ``db_path`` (which creates the
    0700 directory) is for writers only."""
    return Path(root) / "state" / "plane" / "plane.db"


def db_path(root: Path) -> Path:
    p = db_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)            # round-2 F8: dirs 0700
    return p


def open_ro(root: Path, *, timeout: float = 5.0) -> tuple[sqlite3.Connection | None, str | None]:
    """The exists-before-connect probe every read door shares: a read-only
    connection, or ``(None, reason)`` when the db is absent or cannot be
    opened — the caller prints or degrades, never guesses. ``db_file`` is a
    pure join, so a refusal leaves no directory behind."""
    path = db_file(root)
    if not path.is_file():
        return None, f"no plane db at {path}"
    try:
        return connect_ro(path, timeout=timeout), None
    except sqlite3.Error as exc:
        return None, f"plane db unreadable: {exc}"


def connect_ro(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Read-only, and the file must ALREADY exist: ``connect`` auto-creates a
    db, so a read door on a typo'd root would otherwise open an empty plane
    and report everything missing (the J1 exists-before-connect finding).
    Rows are ``sqlite3.Row``; ``query_only`` makes a stray write a SQL error."""
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def connect(path: Path) -> sqlite3.Connection:
    # sqlite creates 0644 by default (probe-confirmed) — pre-create 0600 and
    # re-tighten the WAL/SHM siblings, which are created at their own time.
    if str(path) != ":memory:" and not Path(path).exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, timeout=5.0)
    conn.isolation_level = None           # autocommit — ingest/migrate own txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            f = str(path) + suffix
            if os.path.exists(f):
                os.chmod(f, 0o600)
    return conn
