"""Connection factory + path resolution (design v2 F3).

The db is HOST-scoped: <root>/state/plane/plane.db — outside every vault
working tree so message bodies can never ride a vault sync (spec §5).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def db_path(root: Path) -> Path:
    p = Path(root) / "state" / "plane" / "plane.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)            # round-2 F8: dirs 0700
    return p


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
