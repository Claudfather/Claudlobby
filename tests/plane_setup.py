"""Explicit schema setup for private test roots, never an ordinary writer hook.

Call initialize_plane(root) in a positive fixture before using production emit
or daemon entrypoints. Absent/pending/refused-startup tests omit it. An explicit
schema_version selects a historical fixture at creation; existing databases
are only checked, never upgraded, replaced or repaired by this helper.
"""

from pathlib import Path
import os
import sqlite3

from claudlobby.plane.db import connect_ro, db_file
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files, migrate


def initialize_plane(root: Path, *, schema_version: int = SCHEMA_USER_VERSION) -> Path:
    if type(schema_version) is not int or not 0 <= schema_version <= SCHEMA_USER_VERSION:
        raise ValueError("fixture schema_version must name an implemented schema")
    path = db_file(Path(root).resolve())
    if path.resolve() != path:
        raise ValueError("fixture database path is redirected")
    if path.exists():
        conn = connect_ro(path)
        try:
            actual = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        if actual != schema_version:
            raise ValueError(f"existing fixture schema {actual} is not requested {schema_version}; refusing migration")
        return path
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("absent fixture database has retained SQLite sidecars")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        if schema_version == SCHEMA_USER_VERSION:
            migrate(conn)
        else:
            for number, sql in _migration_files():
                if number <= schema_version:
                    conn.executescript(sql)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == schema_version
    finally:
        conn.close()
    return path
