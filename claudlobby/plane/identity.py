"""Alias→uid resolution with lazy minting (design v2 §3, F10).

Doors speak aliases (bash cannot mint uuids sanely); rows store uids. First
sight of an alias mints a PROVISIONAL identity — doctor lists provisionals so
a typo'd alias becomes a visible finding instead of a phantom colleague. A
generate-time registry pass (Phase 2+) confirms real bots (provisional=0).

Race rule: INSERT OR IGNORE then SELECT — two emitters resolving one new
alias concurrently converge on the winner's uid.
"""

from __future__ import annotations

import sqlite3

from .ids import mint_uid


def resolve(
    conn: sqlite3.Connection,
    kind: str,
    alias: str,
    *,
    now: str,
    parent_uid: str | None = None,
) -> str:
    # NO transaction management here (round-2 F1): ingest() is the sole
    # transaction owner — a nested `with conn` COMMITTED the outer ledger
    # insert early (probe-confirmed), creating the ledger-without-family
    # sequence that made replay delete a never-stored event. Standalone
    # callers run in autocommit; inside ingest these ride its transaction.
    candidate = mint_uid(kind)
    conn.execute(
        "INSERT OR IGNORE INTO identity_registry"
        " (uid, kind, alias, parent_uid, provisional, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, 1, ?, ?)",
        (candidate, kind, alias, parent_uid, now, now),
    )
    uid = lookup(conn, kind, alias)
    conn.execute(
        "UPDATE identity_registry SET last_seen = ? WHERE uid = ?",
        (now, uid),
    )
    return uid


def lookup(conn: sqlite3.Connection, kind: str, alias: str) -> str | None:
    """The uid an alias already has, or None. The read doors' half of
    resolve(): it never mints, because a read must not create an identity."""
    row = conn.execute(
        "SELECT uid FROM identity_registry WHERE kind = ? AND alias = ?",
        (kind, alias),
    ).fetchone()
    return row[0] if row else None


def aliases_of_kind(conn: sqlite3.Connection, kind: str) -> list[str]:
    """Every alias recorded for one kind, sorted: what a read door offers
    when its caller names no subject, or one the plane does not hold."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT alias FROM identity_registry WHERE kind = ? ORDER BY alias", (kind,)
        ).fetchall()
    ]


def resolve_fleet(conn: sqlite3.Connection, fleet_alias: str, now: str) -> str:
    return resolve(conn, "fleet", fleet_alias, now=now)


def resolve_party(
    conn: sqlite3.Connection,
    alias: str,
    now: str,
    fleet_uid: str | None = None,
) -> str:
    return resolve(conn, "actor", alias, now=now, parent_uid=fleet_uid)


def provisional_actors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The typo-suspect list: lazily-minted actors a generate scan never
    confirmed. EXCLUDES ``human:`` actors — a human minted from a real
    Telegram message is a known external party, NEVER in a declared
    roster, so it is legitimately-provisional forever and is not a typo
    suspect. Counting it trained the doctor rung and the trust panel to
    cry wolf on the operator's own name (live-flagged)."""
    return conn.execute(
        "SELECT uid, alias, first_seen, last_seen FROM identity_registry"
        " WHERE kind = 'actor' AND provisional = 1"
        " AND alias NOT LIKE 'human:%' ORDER BY first_seen"
    ).fetchall()
