"""Source attestation over disposable actual Plane schemas; no live host data."""
import sqlite3

import pytest

from claudlobby.plane.db import connect_ro, db_file
from claudlobby.plane.ids import ensure_host_uid, read_host_uid
from claudlobby.plane.ingest import CONSTRUCT_TABLES
from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable
from claudlobby.plane.owner_source import admit_source, bind_source, inspect_source
from claudlobby.plane.view import _envelope
from tests.plane_setup import initialize_plane
from tests.test_plane_two_fleets import _seed

TABLES = (*CONSTRUCT_TABLES.values(), "events")


def add_row(root, table, host_uid, *, legacy=False):
    """Explicit synthetic SQL fixture, including tombstone and null fleet."""
    with sqlite3.connect(db_file(root)) as conn:
        seq = conn.execute("INSERT INTO ingest_ledger(event_id,family,ingested_at)"
                           " VALUES ('fixture-event', ?, 'now')", (table,)).lastrowid
        values = {r[1]: "fixture" for r in conn.execute(f"PRAGMA table_info({table})")
                  if r[3] and r[4] is None}
        values.update(ingest_seq=seq, event_id="fixture-event", host_uid=host_uid,
                      fleet_uid=None, origin="legacy" if legacy else "live")
        if table == "communications":
            values.update(message_class="chat", privacy="metadata")
        elif table == "registry_snapshots":
            values.update(entity_type="host", tombstone=1, cause="probe")
        elif table == "metric_samples":
            values.update(subject_kind="host")
        elif table == "events":
            values.update(kind="system", event="fixture", subject_kind="host", subject_uid="fixture", severity="notice")
        columns = list(values)
        conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES "
                     f"({','.join('?' for _ in columns)})", tuple(values.values()))


def test_binding_is_explicit_idempotent_existing_only_and_preserves_schema(tmp_path):
    with pytest.raises(AccessUnavailable):
        bind_source(tmp_path)
    assert not (tmp_path / "state").exists()
    _seed(tmp_path)
    path = db_file(tmp_path)
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    with pytest.raises(AccessUnavailable):
        inspect_source(tmp_path)
    host = read_host_uid(tmp_path / "state")
    assert bind_source(tmp_path, expected_host_uid=host) == host
    assert bind_source(tmp_path) == inspect_source(tmp_path) == host
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT * FROM owner_source_binding").fetchall() == [(1, host)]


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("legacy", [False, True])
def test_every_family_refuses_mixed_host_including_legacy_null_fleet_tombstone(tmp_path, table, legacy):
    _seed(tmp_path)
    bind_source(tmp_path)
    add_row(tmp_path, table, "foreign-host", legacy=legacy)
    with pytest.raises(AccessDenied):
        inspect_source(tmp_path)
    with pytest.raises(AccessDenied):
        bind_source(tmp_path)


@pytest.mark.parametrize("table", TABLES)
def test_each_family_alone_cannot_be_attested_to_wrong_host(tmp_path, table):
    initialize_plane(tmp_path)
    ensure_host_uid(tmp_path / "state")
    add_row(tmp_path, table, "foreign-host", legacy=True)
    with pytest.raises(AccessDenied):
        bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_source_binding'").fetchone()


def test_explicit_foreign_fleet_parent_refused_but_shared_human_and_two_fleets_allowed(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("INSERT INTO identity_registry VALUES ('human','actor','human:shared',"
                     " NULL,0,'now','now')")
        # Null parent is unattributed history, explicitly attested by binding.
        conn.execute("UPDATE identity_registry SET parent_uid=NULL WHERE kind='fleet' AND alias='data'")
    bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE identity_registry SET parent_uid='foreign-host' WHERE kind='fleet' AND alias='data'")
    with pytest.raises(AccessDenied):
        inspect_source(tmp_path)


def test_copied_pruned_marker_cannot_be_rebound(tmp_path):
    original, copied = tmp_path / "original", tmp_path / "copied"
    _seed(original)
    bind_source(original)
    initialize_plane(copied)
    ensure_host_uid(copied / "state")
    with sqlite3.connect(db_file(original)) as source, sqlite3.connect(db_file(copied)) as target:
        source.backup(target)
    with sqlite3.connect(db_file(copied)) as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM identity_registry")
        conn.execute("DELETE FROM ingest_ledger")
    for operation in (inspect_source, bind_source):
        with pytest.raises(AccessDenied):
            operation(copied)


def test_expected_displayed_host_cannot_bind_changed_identity(tmp_path):
    _seed(tmp_path)
    with pytest.raises(AccessDenied):
        bind_source(tmp_path, expected_host_uid="other-host")
    with pytest.raises(AccessUnavailable):
        inspect_source(tmp_path)


@pytest.mark.parametrize("mutation", ["marker", "rows"])
def test_admission_query_and_provenance_share_snapshot_then_next_query_refuses(tmp_path, mutation):
    _seed(tmp_path)
    host = bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        previous_seq = conn.execute("SELECT MAX(ingest_seq) FROM ingest_ledger").fetchone()[0]

    def admit_and_change(conn):
        admit_source(conn, host)
        with sqlite3.connect(db_file(tmp_path)) as writer:
            if mutation == "marker":
                writer.execute("UPDATE owner_source_binding SET host_uid='foreign-host'")
            else:
                writer.execute("UPDATE work_items SET host_uid='foreign-host', title='foreign secret'")
            writer.execute("INSERT INTO ingest_ledger(event_id,family,ingested_at) VALUES ('later','test','later')")

    env = _envelope(tmp_path, lambda c: [r[0] for r in c.execute("SELECT title FROM work_items")],
                    admit_connection=admit_and_change)
    assert set(env["data"]) == {"work for engineering", "work for data"}
    assert env["provenance"]["last_ingest_seq"] == previous_seq
    with pytest.raises(AccessDenied):
        _envelope(tmp_path, lambda c: None, admit_connection=lambda c: admit_source(c, host))


@pytest.mark.parametrize("shape", ["absent", "directory", "corrupt", "missing-table", "bad-marker"])
def test_unavailable_sources_are_not_repaired_or_exposed(tmp_path, shape):
    _seed(tmp_path)
    bind_source(tmp_path)
    path = db_file(tmp_path)
    if shape == "absent":
        path.unlink()
    elif shape == "directory":
        path.unlink(); path.mkdir()
    elif shape == "corrupt":
        path.write_bytes(b"not SQLite")
    else:
        with sqlite3.connect(path) as conn:
            if shape == "missing-table":
                conn.execute("DROP TABLE metric_samples")
            else:
                conn.execute("DELETE FROM owner_source_binding")
    for operation in (inspect_source, bind_source):
        with pytest.raises(AccessUnavailable, match="owner source unavailable"):
            operation(tmp_path)


def test_admission_requires_explicit_transaction(tmp_path):
    _seed(tmp_path)
    host = bind_source(tmp_path)
    conn = connect_ro(db_file(tmp_path))
    try:
        with pytest.raises(AccessUnavailable):
            admit_source(conn, host)
    finally:
        conn.close()


@pytest.mark.parametrize("version", [0, 999])
def test_schema_mismatch_is_unavailable_without_binding_or_migration(tmp_path, version):
    _seed(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute(f"PRAGMA user_version={version}")
    with pytest.raises(AccessUnavailable):
        bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_source_binding'").fetchone()


@pytest.mark.parametrize("table", TABLES)
def test_host_extrema_use_covering_indexes_created_only_by_binding(tmp_path, table):
    _seed(tmp_path)
    bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        for direction in ("ASC", "DESC"):
            rows = conn.execute(f"EXPLAIN QUERY PLAN SELECT host_uid FROM {table}"
                                f" ORDER BY host_uid COLLATE BINARY {direction} LIMIT 1").fetchall()
            plan = " ".join(r[3] for r in rows)
            assert f"COVERING INDEX owner_source_{table}_host" in plan
            assert "TEMP B-TREE" not in plan


@pytest.mark.parametrize("foreign", ["!before", "zz-after"])
def test_binary_host_extrema_refuse_both_sides_of_expected_host(tmp_path, foreign):
    _seed(tmp_path)
    bind_source(tmp_path)
    add_row(tmp_path, "metric_samples", foreign)
    with pytest.raises(AccessDenied):
        inspect_source(tmp_path)


def test_missing_index_does_not_disable_invariant_or_repair_on_read(tmp_path):
    _seed(tmp_path)
    bind_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("DROP INDEX owner_source_work_items_host")
        conn.execute("UPDATE work_items SET host_uid='foreign-host'")
    with pytest.raises(AccessDenied):
        inspect_source(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_source_work_items_host'").fetchone()
