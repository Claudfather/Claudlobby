from __future__ import annotations

from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.identity import provisional_actors, resolve, resolve_party
from claudlobby.plane.migrations import migrate

NOW = "2026-08-19T00:00:00.000000+00:00"
LATER = "2026-08-19T01:00:00.000000+00:00"


@pytest.fixture()
def conn(tmp_path: Path):
    c = connect(db_path(tmp_path))
    migrate(c)
    yield c
    c.close()


def test_first_sight_mints_provisional(conn):
    uid = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    assert uid.startswith("actor_")
    row = conn.execute(
        "SELECT * FROM identity_registry WHERE uid = ?", (uid,)
    ).fetchone()
    assert row["provisional"] == 1
    assert row["first_seen"] == NOW


def test_resolution_is_stable(conn):
    a = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    b = resolve_party(conn, "bot:example-fleet/alpha", LATER)
    assert a == b
    row = conn.execute(
        "SELECT first_seen, last_seen FROM identity_registry WHERE uid = ?", (a,)
    ).fetchone()
    assert row["first_seen"] == NOW and row["last_seen"] == LATER


def test_distinct_aliases_distinct_uids(conn):
    a = resolve_party(conn, "bot:example-fleet/alpha", NOW)
    b = resolve_party(conn, "bot:example-fleet/beta", NOW)
    assert a != b


def test_kinds_do_not_collide(conn):
    fleet = resolve(conn, "fleet", "example-fleet", now=NOW)
    actor = resolve(conn, "actor", "example-fleet", now=NOW)
    assert fleet != actor and fleet.startswith("fleet_")


def test_provisional_listing(conn):
    resolve_party(conn, "operator", NOW)
    rows = provisional_actors(conn)
    assert [r["alias"] for r in rows] == ["operator"]


def test_concurrent_mint_race_yields_one_uid(conn, tmp_path: Path):
    # Second connection simulates a concurrent emitter losing the insert race.
    other = connect(db_path(tmp_path))
    a = resolve_party(conn, "bot:example-fleet/gamma", NOW)
    b = resolve_party(other, "bot:example-fleet/gamma", LATER)
    other.close()
    assert a == b
