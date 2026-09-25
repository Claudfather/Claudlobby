"""Read-side ordering of stored ISO instants; never rewrite the ledger.

Aware timestamps sort by a fixed-width UTC key at Python datetime's full
microsecond precision. Malformed, naive, or UTC-overflowing historical values
return NULL in SQL and are excluded by chronology queries, never promoted to
current state. Stored source timestamps remain the presentation values.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def instant_key(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            return None
        return at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def register_instant_key(conn: sqlite3.Connection) -> None:
    """Register our reserved function once, including on caller-owned DBs.

    Replacing a function while a cursor is active fails in sqlite3, so probe
    its presence before registering. This also leaves row_factory untouched.
    """
    try:
        conn.execute("SELECT claudlobby_instant_key(NULL)").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such function: claudlobby_instant_key" not in str(exc):
            raise
        conn.create_function("claudlobby_instant_key", 1, instant_key, deterministic=True)
