"""Explicit, recoverable apply on real historical SQLite; no live Plane imports."""

from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3

import pytest

from claudlobby import activation_state as activation
from claudlobby import migration_apply as apply
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.migration_plan import build_migration_manifest
from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import SCHEMA_USER_VERSION, _migration_files
from claudlobby.plane.queue_paths import staged_dir
from claudlobby.releases import seal_release
from tests.test_migration_plan import _database, _event_request, _pending, releases
from tests.test_releases import installed
from tests.test_task_audit import _assignment, _event, _insert, _task


@pytest.fixture
def candidate(installed):
    root, inputs, paths, _, directory = installed
    source = Path(__file__).resolve().parents[1] / "claudlobby/plane/migrations"
    shutil.copytree(source, directory / "package/plane/migrations")
    release = seal_release(root, inputs, paths)
    config = root / "fleet.yaml"
    config.write_text("fleet: {name: example, manager: manager}\n")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("example",), effects={})
    builder.input(config)
    plan = builder.seal()
    return root, release, plan


def _quiesce(store, plan, manifest):
    store.prepare("upgrade", plan, source_release_id=manifest.source["release_id"],
                  recovery_release_id=plan.release_id, enrollment_digest="1" * 64)
    for step in activation.STEPS[:activation.STEPS.index("backup_saved")]:
        store.begin("upgrade", step)
        store.complete("upgrade", step, evidence_digest=(
            manifest.manifest_id[2:] if step == "queues_classified" else "2" * 64))


def _preview(candidate, *, initialize_empty=False):
    root, release, _ = candidate
    return build_migration_manifest(root, release, release, initialize_empty=initialize_empty)


def _version(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_wal_backup_preserves_ids_cursor_and_retry_is_read_only(candidate):
    root, _, plan = candidate
    conn = _database(root)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    _task(conn, "wi_original")
    conn.execute("UPDATE work_items SET title=?", ("Historical\0original",))
    _assignment(conn, "asg_original", "wi_original")
    _event(conn, "wi_original", "asg_original", "completed")
    _insert(conn, "events", kind="system", event="reports_acked", detail='{"acked_through_seq":17}')
    assert Path(str(db_file(root)) + "-wal").stat().st_size > 0
    manifest = _preview(candidate)
    assert not manifest.blockers
    try:
        with activation.locked_activation(root) as store:
            _quiesce(store, plan, manifest)
            result = apply.apply_migration(store, "upgrade", manifest)
            backup = Path(result["backup"]["path"])
            with sqlite3.connect(backup) as saved:
                assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
                assert saved.execute("SELECT work_item_id FROM work_items").fetchall() == [("wi_original",)]
                assert saved.execute("SELECT assignment_id FROM assignments").fetchall() == [("asg_original",)]
                assert saved.execute("SELECT detail FROM events WHERE event='reports_acked'").fetchone()[0] == '{"acked_through_seq":17}'
            assert backup.stat().st_mode & 0o777 == 0o600
            assert _version(db_file(root)) == SCHEMA_USER_VERSION
            assert conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == 4
            original = {p: p.read_bytes() for p in (backup, backup.with_name("migration.json"),
                                                    backup.with_name("activation.json"))}
            assert apply.apply_migration(store, "upgrade", manifest) == result
            assert all(path.read_bytes() == content for path, content in original.items())
            assert activation.read_activation(root, "upgrade").body["completed"][-1] == "migration_applied"
            assert activation.read_selection(root) is None
            assert apply.read_migration(root, "upgrade") == result
    finally:
        conn.close()


def test_current_schema_uses_verified_backup_digest_without_sql_rehearsal(candidate, monkeypatch):
    root, _, plan = candidate
    conn = _database(root, version=SCHEMA_USER_VERSION)
    _insert(conn, "events", kind="system", event="already_current")
    conn.close()
    manifest = _preview(candidate)
    assert not manifest.blockers

    def no_scratch(*_args, **_kwargs):
        raise AssertionError("current schema must not create a SQL rehearsal copy")

    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        with monkeypatch.context() as patch:
            patch.setattr(apply.tempfile, "NamedTemporaryFile", no_scratch)
            result = apply.apply_migration(store, "upgrade", manifest)
        assert result["backup"]["user_version"] == SCHEMA_USER_VERSION
        assert result["result"]["logical_sha256"] == result["backup"]["logical_sha256"]
        with sqlite3.connect(db_file(root)) as changed:
            _insert(changed, "events", kind="system", event="unreviewed_writer")
        with pytest.raises(apply.MigrationApplyError, match="contents differ"):
            apply.apply_migration(store, "upgrade", manifest)


def test_only_explicit_empty_initialization_can_create_database(candidate):
    root, _, plan = candidate
    manifest = _preview(candidate, initialize_empty=True)
    assert not manifest.blockers and not db_file(root).exists()
    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        result = apply.apply_migration(store, "upgrade", manifest)
        assert result["source_version"] is None and result["backup"]["user_version"] == 0
        assert _version(db_file(root)) == SCHEMA_USER_VERSION
        assert apply.apply_migration(store, "upgrade", manifest) == result
    with pytest.raises(activation.ActivationError, match="lock is not held"):
        apply.apply_migration(store, "upgrade", manifest)


def test_final_manifest_follows_ingest_shutdown_writes(candidate):
    # Shutdown may record an event/checkpoint after queue draining. Freezing
    # the DB/WAL manifest before that point makes an otherwise safe apply stale.
    root, release, plan = candidate
    conn = _database(root)
    before_shutdown = _preview(candidate)
    with activation.locked_activation(root) as store:
        store.prepare("upgrade", plan, source_release_id=release.release_id,
                      recovery_release_id=release.release_id, enrollment_digest="1" * 64)
        for step in activation.STEPS[:activation.STEPS.index("ingest_quiesced")]:
            store.begin("upgrade", step)
            store.complete("upgrade", step, evidence_digest="2" * 64)
        with pytest.raises(activation.ActivationError, match="out of order"):
            store.begin("upgrade", "queues_classified")
        store.begin("upgrade", "ingest_quiesced")
        _insert(conn, "events", kind="system", event="daemon_stopped")
        conn.close()
        store.complete("upgrade", "ingest_quiesced", evidence_digest="3" * 64)
        manifest = _preview(candidate)
        assert manifest.manifest_id != before_shutdown.manifest_id
        store.begin("upgrade", "queues_classified")
        store.complete("upgrade", "queues_classified", evidence_digest=manifest.manifest_id[2:])
        assert apply.apply_migration(store, "upgrade", manifest)["source_version"] == 1


def test_initial_cutover_binds_old_source_and_distinct_compatible_recovery(releases):
    root, source, target = releases
    _database(root).close()
    manifest = build_migration_manifest(root, source, target)
    assert manifest.rollback["source_after_sql_blockers"] and not manifest.blockers
    builder = ConfigPlanBuilder(root, target.release_id, target.seal_sha256, ("example",), effects={})
    plan = builder.seal()
    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        assert activation.read_activation(root, "upgrade").body["previous_selection"] is None
        result = apply.apply_migration(store, "upgrade", manifest)
        assert result["manifest"]["source"]["release_id"] == source.release_id
        assert result["backup"]["user_version"] == 1 and _version(db_file(root)) == SCHEMA_USER_VERSION


def test_preconditions_refuse_before_backup_or_database_writes(candidate):
    root, _, plan = candidate
    _database(root).close()
    manifest = _preview(candidate)
    with activation.locked_activation(root) as store:
        store.prepare("upgrade", plan, recovery_release_id=plan.release_id, enrollment_digest="1" * 64)
        before = db_file(root).read_bytes()
        with pytest.raises(apply.MigrationApplyError, match="recorded quiescence"):
            apply.apply_migration(store, "upgrade", manifest)
        _quiesce(store, plan, manifest)
        altered = replace(manifest, migrations=tuple(
            {**item, "sha256": "0" * 64} if item["version"] == SCHEMA_USER_VERSION else item
            for item in manifest.migrations))
        # A recorded manifest is still refused if its reviewed SQL is not the
        # existing runner's exact SQL; caller flags cannot select another DDL.
        record = activation.read_activation(root, "upgrade")
        record.body["evidence"]["queues_classified"] = altered.manifest_id[2:]
        store._save(record)
        with pytest.raises(apply.MigrationApplyError, match="SQL hashes"):
            apply.apply_migration(store, "upgrade", altered)
        record.body["evidence"]["queues_classified"] = manifest.manifest_id[2:]
        store._save(record)
        with sqlite3.connect(db_file(root)) as changed:
            changed.execute("PRAGMA user_version=2")
        changed_bytes = db_file(root).read_bytes()
        assert changed_bytes != before
        with pytest.raises(apply.MigrationApplyError, match="inventory changed after review"):
            apply.apply_migration(store, "upgrade", manifest)
        assert db_file(root).read_bytes() == changed_bytes
        assert apply.read_migration(root, "upgrade") is None
        assert not list((root / "state/activations/upgrade").glob("*sqlite3"))


def test_conditional_queue_and_queue_changes_never_admit_sql(candidate):
    root, _, plan = candidate
    _database(root).close()
    pending = staged_dir(root) / "task.batch"
    _pending(pending, [_event_request("task", payload={"work_item_id": "wi_old", "event": "completed"})], raw=True)
    manifest = _preview(candidate)
    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        before = db_file(root).read_bytes()
        with pytest.raises(apply.MigrationApplyError, match="non-telemetry pending records need a drain/quarantine decision"):
            apply.apply_migration(store, "upgrade", manifest)
        assert db_file(root).read_bytes() == before
        assert apply.read_migration(root, "upgrade") is None


def test_interrupted_script_resumes_from_exact_backup_and_rejects_extra_work(candidate, monkeypatch):
    root, _, plan = candidate
    _database(root).close()
    manifest = _preview(candidate)
    original = apply.migrate

    def stop_after_script(conn):
        # The same runner is used on a private copy to derive postconditions. Inject
        # interruption only into the owned, real database migration call.
        if conn.execute("PRAGMA database_list").fetchone()[2] == str(db_file(root)):
            conn.executescript(_migration_files()[1][1])
            raise OSError("interrupted after script 2 commit")
        return original(conn)

    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        with monkeypatch.context() as patch:
            patch.setattr(apply, "migrate", stop_after_script)
            with pytest.raises(OSError, match="script 2"):
                apply.apply_migration(store, "upgrade", manifest)
        assert _version(db_file(root)) == 2
        before = apply.read_migration(root, "upgrade")
        backup_bytes = Path(before["backup"]["path"]).read_bytes()
        with sqlite3.connect(db_file(root)) as changed:
            _task(changed, "wi_unexpected_writer")
        with pytest.raises(apply.MigrationApplyError, match="contents differ"):
            apply.apply_migration(store, "upgrade", manifest)
        assert _version(db_file(root)) == 2
        # Remove only the test-injected rows to recover the precise interrupted
        # fixture; production apply has no repair/restore or rewind operation.
        with sqlite3.connect(db_file(root)) as changed:
            changed.execute("DELETE FROM work_items")
            changed.execute("DELETE FROM ingest_ledger")
            changed.execute("DELETE FROM sqlite_sequence")
        result = apply.apply_migration(store, "upgrade", manifest)
        assert result["result"]["user_version"] == SCHEMA_USER_VERSION
        assert Path(result["backup"]["path"]).read_bytes() == backup_bytes


def test_commit_before_result_record_is_reconciled_without_duplicate_sql(candidate, monkeypatch):
    root, _, plan = candidate
    _database(root).close()
    manifest = _preview(candidate)
    original = apply._save

    def interrupted(store, activation_id, body):
        if body["result"] is not None:
            raise OSError("interrupted before result journal")
        original(store, activation_id, body)

    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        with monkeypatch.context() as patch:
            patch.setattr(apply, "_save", interrupted)
            with pytest.raises(OSError, match="result journal"):
                apply.apply_migration(store, "upgrade", manifest)
        assert _version(db_file(root)) == SCHEMA_USER_VERSION
        assert apply.read_migration(root, "upgrade")["result"] is None
        result = apply.apply_migration(store, "upgrade", manifest)
        assert result["result"]["user_version"] == SCHEMA_USER_VERSION
        store.begin_rollback("upgrade")
        before = db_file(root).read_bytes()
        with pytest.raises(apply.MigrationApplyError, match="recorded quiescence"):
            apply.apply_migration(store, "upgrade", manifest)
        assert db_file(root).read_bytes() == before
        assert Path(result["backup"]["path"]).is_file()


def test_published_backup_crash_retains_backup_and_changed_pending_blocks_resume(candidate, monkeypatch):
    root, _, plan = candidate
    _database(root).close()
    manifest = _preview(candidate)
    original = apply._save

    def interrupted(store, activation_id, body):
        if body["backup"] is not None:
            raise OSError("interrupted after backup publication")
        original(store, activation_id, body)

    with activation.locked_activation(root) as store:
        _quiesce(store, plan, manifest)
        with monkeypatch.context() as patch:
            patch.setattr(apply, "_save", interrupted)
            with pytest.raises(OSError, match="backup publication"):
                apply.apply_migration(store, "upgrade", manifest)
        backup = root / "state/activations/upgrade/plane-before.sqlite3"
        saved_bytes = backup.read_bytes()
        assert _version(db_file(root)) == 1
        pending = staged_dir(root) / "arrived.batch"
        _pending(pending, [_event_request()], raw=True)
        with pytest.raises(ValueError, match="queue inventory changed"):
            apply.apply_migration(store, "upgrade", manifest)
        assert _version(db_file(root)) == 1 and backup.read_bytes() == saved_bytes
        pending.unlink()
        pending.parent.rmdir()  # restore the reviewed absent directory state
        result = apply.apply_migration(store, "upgrade", manifest)
        assert backup.read_bytes() == saved_bytes
        backup.write_bytes(saved_bytes + b"changed")
        with pytest.raises(apply.MigrationApplyError, match="backup bytes changed"):
            apply.apply_migration(store, "upgrade", manifest)
        assert apply.read_migration(root, "upgrade") == result
