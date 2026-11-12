"""Historical migration receipts are read-only; the SQLite backup primitive stays real.

No apply workflow exists. Journals are authored directly in their exact on-disk
schema; backups run against real SQLite files under private test roots.
"""

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from claudlobby import activation_state as activation
from claudlobby import migration_apply as apply
from claudlobby.plane.db import db_file
from tests.release_fixtures import _database
from tests.test_task_audit import _assignment, _event, _insert, _task


def write_migration(root, activation_id="upgrade", **overrides):
    """Author one historical migration.json exactly as the retired writer sealed it."""
    root = root.resolve()
    manifest = {"source": {"release_id": "r-" + "a" * 64}, "migrations": []}
    body = {
        "schema": 1,
        "activation_id": activation_id,
        "root": str(root),
        "manifest": manifest,
        "manifest_id": "m-" + activation._digest(manifest),
        "source_version": 1,
        "target_version": 2,
        "backup": {
            "path": str(
                root / "state/activations" / activation_id / "plane-before.sqlite3"
            ),
            "user_version": 1,
            "logical_sha256": "1" * 64,
            "sha256": "2" * 64,
        },
        "result": {"user_version": 2, "logical_sha256": "3" * 64},
        **overrides,
    }
    path = root / "state/activations" / activation_id / "migration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"record": body, "sha256": activation._digest(body)}))
    return path, body


def _rewrite(path, body):
    path.write_text(json.dumps({"record": body, "sha256": activation._digest(body)}))


def test_apply_workflow_is_gone_but_reads_and_backup_primitive_remain():
    for name in ("apply_migration", "migrate", "_save"):
        assert not hasattr(apply, name)
    assert issubclass(apply.MigrationApplyError, activation.ActivationError)


def test_read_migration_returns_recorded_evidence_and_none_when_absent(tmp_path):
    assert apply.read_migration(tmp_path, "upgrade") is None
    path, body = write_migration(tmp_path)
    before = path.read_bytes()
    assert apply.read_migration(tmp_path, "upgrade") == body
    assert path.read_bytes() == before
    # A receipt whose result is still unrecorded is evidence, not a claim of completion.
    _rewrite(path, {**body, "result": None})
    assert apply.read_migration(tmp_path, "upgrade")["result"] is None


@pytest.mark.parametrize(
    "tamper", ["body", "digest", "id", "root", "schema", "manifest", "garbage", "shape"]
)
def test_read_migration_refuses_changed_evidence_without_repairing_it(tmp_path, tamper):
    path, body = write_migration(tmp_path)
    raw = json.loads(path.read_text())
    if tamper == "body":
        raw["record"]["target_version"] = 99
    elif tamper == "digest":
        raw["sha256"] = "0" * 64
    elif tamper == "garbage":
        raw = None
    elif tamper == "shape":
        raw = {"record": body}
    else:
        changed = {
            "id": {"activation_id": "other"},
            "root": {"root": str(tmp_path.resolve() / "x")},
            "schema": {"schema": 2},
            "manifest": {
                "manifest": {
                    "source": {"release_id": "r-" + "c" * 64},
                    "migrations": [],
                }
            },
        }[tamper]
        raw["record"] = {**body, **changed}
        raw["sha256"] = activation._digest(raw["record"])
    path.write_text("not json" if raw is None else json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(apply.MigrationApplyError):
        apply.read_migration(tmp_path, "upgrade")
    assert path.read_bytes() == before


def test_read_migration_refuses_redirected_or_irregular_journals(tmp_path):
    path, _ = write_migration(tmp_path / "root")
    foreign = tmp_path / "foreign.json"
    foreign.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(foreign)
    with pytest.raises(apply.MigrationApplyError, match="redirected"):
        apply.read_migration(tmp_path / "root", "upgrade")
    path.unlink()
    path.mkdir()
    with pytest.raises(apply.MigrationApplyError, match="not a regular file"):
        apply.read_migration(tmp_path / "root", "upgrade")
    with pytest.raises(
        activation.ActivationError, match="explicit simple activation id"
    ):
        apply.read_migration(tmp_path / "root", "../escape")


def _historical(root):
    conn = _database(root)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    _task(conn, "wi_original")
    conn.execute("UPDATE work_items SET title=?", ("Historical\0original",))
    _assignment(conn, "asg_original", "wi_original")
    _event(conn, "wi_original", "asg_original", "completed")
    _insert(
        conn,
        "events",
        kind="system",
        event="reports_acked",
        detail='{"acked_through_seq":17}',
    )
    return conn


def test_wal_backup_is_standalone_and_preserves_ids_cursor_and_digests(tmp_path):
    conn = _historical(tmp_path)
    try:
        assert Path(str(db_file(tmp_path)) + "-wal").stat().st_size > 0
        destination = tmp_path / "backup" / "plane-before.sqlite3"
        destination.parent.mkdir()
        apply._backup(conn, destination)
        assert sorted(p.name for p in destination.parent.iterdir()) == [
            "plane-before.sqlite3"
        ]
        assert destination.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as saved:
            assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
            assert saved.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert saved.execute("SELECT work_item_id FROM work_items").fetchall() == [
                ("wi_original",)
            ]
            assert (
                saved.execute("SELECT title FROM work_items").fetchone()[0]
                == "Historical\0original"
            )
            assert saved.execute(
                "SELECT assignment_id FROM assignments"
            ).fetchall() == [("asg_original",)]
            assert (
                saved.execute(
                    "SELECT detail FROM events WHERE event='reports_acked'"
                ).fetchone()[0]
                == '{"acked_through_seq":17}'
            )
            assert (
                saved.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 4
            )
            # Page layout and WAL bytes differ; the logical digest binds rows and schema only.
            assert apply._logical_digest(saved) == apply._logical_digest(conn)
        assert not Path(str(destination) + "-wal").exists()
        assert (
            apply._file_digest(destination)
            == hashlib.sha256(destination.read_bytes()).hexdigest()
        )
    finally:
        conn.close()


def test_logical_digest_binds_rows_embedded_nul_and_user_version(tmp_path):
    conn = _historical(tmp_path)
    try:
        first = apply._logical_digest(conn)
        assert apply._logical_digest(conn) == first
        conn.execute("UPDATE work_items SET title=?", ("Historical\0changed",))
        second = apply._logical_digest(conn)
        assert second != first  # text after an embedded NUL is still bound
        conn.execute("UPDATE work_items SET title=?", ("Historical",))
        assert apply._logical_digest(conn) not in {first, second}
        conn.execute("PRAGMA user_version=7")
        assert apply._logical_digest(conn) != second
    finally:
        conn.close()


def test_failed_backup_publishes_nothing_and_keeps_the_prior_file(tmp_path):
    conn = _historical(tmp_path)
    try:
        directory = tmp_path / "backup"
        directory.mkdir()
        destination = directory / "plane-before.sqlite3"

        class Broken:
            def backup(self, _dest):
                raise sqlite3.OperationalError("interrupted")

        with pytest.raises(sqlite3.OperationalError):
            apply._backup(Broken(), destination)
        assert (
            list(directory.iterdir()) == []
        )  # no partial file or temporary left behind
        apply._backup(conn, destination)
        kept = destination.read_bytes()
        with pytest.raises(sqlite3.OperationalError):
            apply._backup(Broken(), destination)
        assert destination.read_bytes() == kept and [
            p.name for p in directory.iterdir()
        ] == [destination.name]
    finally:
        conn.close()


def test_backup_refuses_a_copy_that_fails_its_integrity_check(tmp_path, monkeypatch):
    conn = _historical(tmp_path)
    try:
        directory = tmp_path / "backup"
        directory.mkdir()
        real = sqlite3.connect

        class Corrupt:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args):
                if sql == "PRAGMA quick_check":

                    class Result:
                        def fetchall(self):
                            return [("*** in database main ***",)]

                    return Result()
                return self._inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def connect(target, *args, **kwargs):
            inner = real(target, *args, **kwargs)
            return Corrupt(inner) if str(target).startswith(str(directory)) else inner

        monkeypatch.setattr(apply.sqlite3, "connect", connect)
        class Source:
            def backup(self, dest):
                conn.backup(getattr(dest, "_inner", dest))

        with pytest.raises(apply.MigrationApplyError, match="integrity check"):
            apply._backup(Source(), directory / "plane-before.sqlite3")
        assert list(directory.iterdir()) == []
    finally:
        conn.close()
