"""user_version-gated migration runner (F2 + round-2 F2/F6).

Forward-only; scripts own their transactions; downgrade raises DowngradeError
(refused loudly at the CLI, never spooled).
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources

SCHEMA_USER_VERSION = 2

_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def _migration_files() -> list[tuple[int, str]]:
    pkg = resources.files("claudlobby.plane") / "migrations"
    out = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.read_text()))
    return sorted(out)


class DowngradeError(RuntimeError):
    """db user_version newer than this code — refuse loudly, NEVER spool (F6)."""


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    # Scripts own their transactions (BEGIN IMMEDIATE ... PRAGMA user_version;
    # COMMIT) — round-2 F2: executescript's implicit commit made an outer
    # `with conn` a no-op, committing a partial schema stamped version 0.
    conn.isolation_level = None   # autocommit; the SCRIPT is the transaction
    current = _user_version(conn)
    if current > SCHEMA_USER_VERSION:
        raise DowngradeError(
            f"plane.db user_version={current} is newer than this code"
            f" (supports <={SCHEMA_USER_VERSION}) — refusing downgrade"
        )
    if current == SCHEMA_USER_VERSION:
        # Steady state — every emit_batch lands here. Skip the resource
        # iteration + full read of every migration file (~0.27ms/call
        # measured, 1-2ms on the Pi) that the loop below would only discard.
        return current
    for number, sql in _migration_files():
        if number <= current:
            continue
        try:
            conn.executescript(sql)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raced = _user_version(conn)
            if raced > SCHEMA_USER_VERSION:
                # The concurrent migrator was a NEWER binary — accepting its
                # result here would bypass the downgrade guard that only ran
                # at entry.
                raise DowngradeError(
                    f"plane.db user_version={raced} (concurrent migration) is"
                    f" newer than this code (supports <={SCHEMA_USER_VERSION})"
                    " — refusing downgrade"
                )
            if raced >= number:   # concurrent first emitter applied it — benign
                current = raced
                continue
            raise
        current = _user_version(conn)
        if current != number:
            raise RuntimeError(
                f"migration {number} did not stamp user_version (got {current})"
            )
    return current
