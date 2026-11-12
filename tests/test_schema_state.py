"""Ordinary schema admission cannot perform an implicit migration."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import (
    DowngradeError,
    SCHEMA_USER_VERSION,
    _migration_files,
    migrate,
)
from claudlobby.plane.schema_state import (
    PendingMigrationError,
    preflight_schema,
    require_current_schema,
)


def test_absent_database_requires_explicit_initialization(tmp_path):
    root = tmp_path / "not-created"
    with pytest.raises(PendingMigrationError, match="explicit migration apply"):
        preflight_schema(root)
    assert not root.exists()
    # A path that exists but cannot hold SQLite is an infrastructure fault,
    # not authorization to initialize it. Emit's existing CANTOPEN spool case
    # must retain that distinction instead of losing the event as "absent".
    path = db_file(root)
    path.mkdir(parents=True)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database"):
        preflight_schema(root)
    assert path.is_dir() and list(path.iterdir()) == []
    path.rmdir()
    path.parent.rmdir()
    path.parent.write_text("unavailable parent")
    with pytest.raises(sqlite3.OperationalError, match="unable to open database") as caught:
        preflight_schema(root)
    assert caught.value.sqlite_errorname == "SQLITE_CANTOPEN"
    if hasattr(sqlite3, "SQLITE_CANTOPEN"):
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN
    assert path.parent.read_text() == "unavailable parent"


@pytest.mark.parametrize("version", [0, SCHEMA_USER_VERSION - 1, SCHEMA_USER_VERSION + 1])
def test_incompatible_schema_is_refused_without_mutation(tmp_path, version):
    path = db_file(tmp_path)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('keep')")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        before = path.read_bytes()
        error = DowngradeError if version > SCHEMA_USER_VERSION else PendingMigrationError
        for check in (lambda: require_current_schema(conn), lambda: preflight_schema(tmp_path)):
            with pytest.raises(error, match=f"user_version={version}"):
                check()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT value FROM sentinel").fetchone()[0] == "keep"
        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == ["plane.db"]


def test_only_explicit_runner_applies_a_pending_migration(tmp_path):
    path = db_file(tmp_path)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        for number, sql in _migration_files():
            if number < SCHEMA_USER_VERSION:
                conn.executescript(sql)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_USER_VERSION - 1
        before = path.read_bytes()
        with pytest.raises(PendingMigrationError):
            preflight_schema(tmp_path)
        assert path.read_bytes() == before
        assert migrate(conn) == SCHEMA_USER_VERSION
        applied = path.read_bytes()
        assert require_current_schema(conn) == preflight_schema(tmp_path) == SCHEMA_USER_VERSION
        assert path.read_bytes() == applied


def test_current_schema_check_preserves_the_callers_transaction(tmp_path):
    with closing(sqlite3.connect(":memory:")) as conn:
        migrate(conn)
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE uncommitted (value TEXT)")
        assert require_current_schema(conn) == SCHEMA_USER_VERSION
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'uncommitted'"
        ).fetchone() is None


def test_diagnostic_doors_refuse_pending_schema_without_advancing_it(tmp_path, monkeypatch, capsys):
    """The old status/doctor/registry doors each called migrate on a read."""
    from types import SimpleNamespace
    from claudlobby.commands import plane, plane_registry, plane_status
    from claudlobby.command_result import CommandFailure

    monkeypatch.setattr(plane_registry, "resolve_paths", lambda **_: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(plane_status, "resolve_paths", lambda **_: SimpleNamespace(root=tmp_path))
    path = db_file(tmp_path)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        for version, sql in _migration_files():
            if version < SCHEMA_USER_VERSION:
                conn.executescript(sql)
    before = path.read_bytes()
    with pytest.raises(CommandFailure) as status_error:
        plane_status.dispatch(SimpleNamespace(root=tmp_path, fleet=None, seed=False))
    assert status_error.value.error.code == "migration_required"
    assert "explicit migration apply" in status_error.value.error.message
    assert path.read_bytes() == before
    assert plane.cmd_plane_doctor(SimpleNamespace(root=tmp_path, fleet=None, seed=False)) == 7
    assert "explicit Plane migration is required" in capsys.readouterr().err
    with pytest.raises(CommandFailure) as registry_error:
        plane_registry.dispatch(SimpleNamespace(root=tmp_path, fleet=None, seed=False))
    assert registry_error.value.error.code == "migration_required"
    assert "explicit migration apply" in registry_error.value.error.message
    assert path.read_bytes() == before
    assert [item.name for item in path.parent.iterdir()] == ["plane.db"]


def test_status_on_absent_root_remains_read_only(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from claudlobby.commands import plane_status
    from claudlobby.command_result import execute

    root = tmp_path / "absent"
    monkeypatch.setattr(plane_status, "resolve_paths", lambda **_: SimpleNamespace(root=root))
    out = plane_status.dispatch(SimpleNamespace(root=root, fleet=None, seed=False))
    assert out.data["database"]["state"] == "absent"
    assert "absent" in out.lines[0]
    assert execute("plane.status", lambda: out, json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "plane.status" and result["ok"] is True
    assert result["data"]["spool"]["pending"] == 0
    assert not root.exists()


def test_writer_refuses_absent_or_pending_database_before_opening_for_write(tmp_path):
    from claudlobby.plane.writer import PlaneWriter

    root = tmp_path / "host"
    writer = PlaneWriter(root)
    with pytest.raises(PendingMigrationError):
        writer.connection()
    assert not root.exists()
    path = db_file(root)
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        for version, sql in _migration_files():
            if version < SCHEMA_USER_VERSION:
                conn.executescript(sql)
    before = path.read_bytes()
    with pytest.raises(PendingMigrationError):
        writer.connection()
    assert path.read_bytes() == before
    assert [item.name for item in path.parent.iterdir()] == ["plane.db"]
    assert writer._conn is None
