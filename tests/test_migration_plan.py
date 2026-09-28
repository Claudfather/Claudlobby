"""Read-only migration inventory of real historical DDL and pending shapes."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from claudlobby.migration_plan import build_migration_manifest, verify_pending_queues
from claudlobby.plane.db import db_file
from claudlobby.plane.migrations import _migration_files, migrate
from claudlobby.plane.queue_paths import spool_path, staged_dir
from claudlobby.releases import ReleaseError
from tests.test_releases import installed, r
from tests.test_task_audit import _assignment, _event, _insert, _task


@pytest.fixture
def releases(installed):
    root, inputs, paths, _, directory = installed
    target_inputs = replace(inputs, source_revision="c" * 40, artifact_id="candidate")
    target_dir = r.release_path(root, target_inputs.release_id)
    shutil.copytree(directory, target_dir)
    sql_dir = Path(__file__).resolve().parents[1] / "claudlobby/plane/migrations"
    for dest, assembly, version in ((directory, inputs, 1), (target_dir, target_inputs, 12)):
        metadata = json.loads((dest / paths.artifact).read_text())
        metadata.update(source_revision=assembly.source_revision, artifact_id=assembly.artifact_id)
        metadata["compatibility"]["schema"] = {"read": [version], "write": version}
        (dest / paths.artifact).write_text(json.dumps(metadata))
        (dest / paths.cli).write_text(f"#!{dest / paths.interpreter}\n")
        selected = dest / "package/plane/migrations"
        selected.mkdir(parents=True)
        for source in sql_dir.glob("*.sql"):
            if int(source.name[:4]) <= version:
                shutil.copy2(source, selected / source.name)
    return root, r.seal_release(root, inputs, paths), r.seal_release(root, target_inputs, paths)


def _database(root, version=1):
    path = db_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    for number, sql in _migration_files():
        if number <= version:
            conn.executescript(sql)
    return conn


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _event_request(event_type="system", *, event_id="event-one", payload=None):
    return {"event_id": event_id, "occurred_at": "2026-09-01T00:00:00Z",
            "schema_version": "1.0.0", "emitter": "fixture", "event_type": event_type,
            "payload": payload if payload is not None else {"event": "bot_started"}}


def _pending(path, requests, *, raw=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"events": requests} if raw else {
        "event_ids": [item["event_id"] for item in requests], "requests": requests,
        "spooled_at": "2026-09-01T00:00:00Z", "attempts": 0, "history": [], "error": "locked",
    }
    path.write_text(json.dumps(value))


def test_old_schema_preview_binds_sql_task_blockers_and_recovery_floor(releases):
    root, source, target = releases
    conn = _database(root)
    _task(conn, "wi_closed")
    _assignment(conn, "asg_closed", "wi_closed")
    _event(conn, "wi_closed", "asg_closed", "expired")
    _task(conn, "wi_unscoped", fleet=None)
    _insert(conn, "events", kind="system", event="reports_acked", detail='{"acked_through_seq":17}')
    conn.close()
    before = _snapshot(root)
    plan = build_migration_manifest(root, source, target)
    assert plan == build_migration_manifest(root, source, target)
    assert plan.manifest_id == build_migration_manifest(root, source, target).manifest_id
    assert _snapshot(root) == before  # includes database, cursor and release metadata bytes
    assert plan.preview_only and not plan.database["consistent_backup"]
    assert plan.database["user_version"] == 1
    assert plan.task_audit["counts"]["closed_tasks"] == 1
    assert any("task audit: unscoped_task" in value for value in plan.blockers)
    assert plan.source["release_id"] == source.release_id
    assert plan.target["seal_sha256"] == target.seal_sha256
    assert [item["version"] for item in plan.migrations if item["proposed"]] == list(range(2, 13))
    assert all(item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()
               for item in plan.migrations)
    assert plan.rollback["source_after_sql_blockers"] == ("unsupported schema: 12",)
    assert plan.rollback["compatible_recovery_release_required_before_sql"]
    assert "name a state-compatible recovery release before irreversible SQL" in plan.proposed_steps
    assert not any("recovery release" in blocker for blocker in plan.blockers)
    assert not plan.rollback["backup_restoration_over_accepted_work_permitted"]
    # Simulate the separate apply owner advancing SQL: recovery can still
    # verify queue ownership without pretending the reviewed DB is unchanged.
    with sqlite3.connect(db_file(root)) as conn:
        migrate(conn)
    verify_pending_queues(root, plan)
    sql = Path(plan.migrations[-1]["path"])
    sql.write_bytes(sql.read_bytes() + b"\n-- altered candidate\n")
    with pytest.raises(ReleaseError, match="inventory digest mismatch"):
        build_migration_manifest(root, source, target)


def test_absent_and_empty_databases_are_distinct_and_never_initialized(releases):
    root, source, target = releases
    absent = build_migration_manifest(root, source, target)
    assert absent.database["state"] == "absent" and absent.task_audit is None
    assert not db_file(root).parent.exists()
    assert all(queue["file_count"] == 0 for queue in absent.queues.values())
    initial = build_migration_manifest(root, source, target, initialize_empty=True)
    assert initial.database["initialize_empty"]
    assert not any("database absent" in reason for reason in initial.blockers)
    assert initial.manifest_id != absent.manifest_id
    assert not db_file(root).parent.exists()
    db_file(root).parent.mkdir(parents=True)
    wal = Path(str(db_file(root)) + "-wal")
    wal.write_bytes(b"unresolved old WAL")
    unresolved = build_migration_manifest(root, source, target, initialize_empty=True)
    assert any("both database and WAL" in reason for reason in unresolved.blockers)
    assert wal.read_bytes() == b"unresolved old WAL" and not db_file(root).exists()
    wal.unlink()
    conn = _database(root, version=0)
    conn.close()
    before = _snapshot(root)
    empty = build_migration_manifest(root, source, target)
    assert empty.database["state"] == "ok" and empty.database["user_version"] == 0
    assert empty.task_audit["counts"]["tasks"] == 0
    assert len([item for item in empty.migrations if item["proposed"]]) == 12
    assert _snapshot(root) == before
    assert not spool_path(root).exists() and not staged_dir(root).exists()
    verify_pending_queues(root, empty)
    _pending(staged_dir(root) / "arrived.batch", [_event_request()], raw=True)
    with pytest.raises(ValueError, match="queue inventory changed"):
        verify_pending_queues(root, empty)


def test_both_pending_lanes_preserve_telemetry_and_name_conditional_decisions(releases):
    root, source, target = releases
    _database(root).close()
    telemetry = _event_request()
    task = _event_request("task", event_id="task-close",
                          payload={"work_item_id": "wi_old", "event": "completed"})
    transmission = _event_request("transmission", event_id="send-proof", payload={
        "msg_id": "msg_old", "attempt_no": 1, "carrier": "tmux",
        "destination": "bot:fleet/worker", "state": "pane_submitted"})
    _pending(spool_path(root) / "conditional.json", [telemetry, task, transmission])
    raw_telemetry = _event_request(event_id="raw-health")
    raw_telemetry.pop("schema_version")
    cursor = _event_request(event_id="raw-cursor", payload={"event": "reports_acked"})
    _pending(staged_dir(root) / "raw.batch", [raw_telemetry, cursor], raw=True)
    _pending(spool_path(root) / "claim.json.inflight.999.abc", [telemetry])
    _pending(spool_path(root) / "quarantine/retained.json", [task])
    before = _snapshot(root)
    plan = build_migration_manifest(root, source, target)
    assert _snapshot(root) == before
    assert all(queue["file_count"] == 1 and queue["sha256"] for queue in plan.queues.values())
    spool = plan.queues["spool"]["files"][0]
    assert [record["classification"] for record in spool["records"]] == [
        "ordinary_telemetry", "conditional_legacy_task_mutation", "legacy_task_evidence"]
    staged = plan.queues["staged"]["files"][0]
    assert [record["classification"] for record in staged["records"]] == [
        "ordinary_telemetry", "legacy_task_cursor_or_status"]
    assert all(not record["replay_validated"] for record in spool["records"] + staged["records"])
    assert not spool["issues"] and not staged["issues"]
    assert sum("drain/quarantine decision" in reason for reason in plan.blockers) == 2
    assert any("inflight spool claims" in reason for reason in plan.blockers)
    assert "no replay/discard permission" in plan.queues["quarantine"]["files"][0]["disposition"]
    with pytest.raises(ValueError, match="pending queues remain blocked"):
        verify_pending_queues(root, plan)


def test_malformed_unknown_and_unreadable_pending_is_blocked_not_empty(releases):
    root, source, target = releases
    _database(root).close()
    pending = spool_path(root)
    pending.mkdir()
    (pending / "broken.json").write_text('{"requests":')
    unknown = _event_request("future_kind")
    _pending(pending / "unknown.json", [unknown])
    version = _event_request(event_id="future-format")
    version["schema_version"] = "2.0.0"
    _pending(pending / "version.json", [version])
    _pending(pending / "shape.json", [_event_request(payload={"event": []})])
    # A file where a lazy queue should be is not permission to report zero.
    staged_dir(root).write_text("not a readable queue")
    before = _snapshot(root)
    plan = build_migration_manifest(root, source, target)
    assert _snapshot(root) == before
    assert plan.queues["spool"]["file_count"] == 4
    assert all(item["issues"] for item in plan.queues["spool"]["files"])
    assert plan.queues["staged"]["file_count"] is None
    assert plan.queues["staged"]["sha256"] is None
    assert any("staged queue unreadable" in reason for reason in plan.blockers)
    assert any("unknown event family" in reason for reason in plan.blockers)
    assert any("unknown pending envelope version" in reason for reason in plan.blockers)
    assert any("system.event payload shape" in reason for reason in plan.blockers)
