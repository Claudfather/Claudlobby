"""The public Plane doctor keeps its rungs in the common JSON result."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from types import SimpleNamespace

from claudlobby.__main__ import main
from claudlobby.commands import plane
from claudlobby.plane.schema_state import PendingMigrationError
from tests.ingest_listener import listening_socket, short_socket_dir
from tests.package_fixtures import source_package
from tests.plane_setup import initialize_plane


def test_plane_doctor_json_on_unused_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    rc = main(["--root", str(tmp_path), "plane", "doctor", "--json"])

    stdout, stderr = capsys.readouterr()
    result = json.loads(stdout)
    assert rc == 0
    assert not stderr
    assert result["schema_version"] == 1
    assert result["command"] == "plane.doctor"
    assert result["ok"] is True
    assert result["data"]["status"] == "ok"
    assert result["data"]["attention_count"] == 0
    assert any(r["name"] == "db" for r in result["data"]["rungs"])


def test_plane_doctor_json_preserves_attention_rungs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    monkeypatch.setattr(plane, "scan_spool", lambda _root: SimpleNamespace(
        spool_state="unreadable", quarantine_state="ok", pending=(),
        inflight=(), quarantined=()))

    rc = plane.cmd_plane_doctor(SimpleNamespace(root=str(tmp_path), json=True))

    stdout, stderr = capsys.readouterr()
    result = json.loads(stdout)
    assert rc == 4
    assert not stderr
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert result["data"]["status"] == "attention"
    assert result["data"]["attention_count"] >= 1
    assert any(r["name"] == "spool depth" and r["status"] == "attention"
               and "UNREADABLE" in r["detail"] for r in result["data"]["rungs"])


def test_plane_doctor_json_schema_refusal_uses_common_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    monkeypatch.setattr(plane, "db_file", lambda _root: tmp_path / "existing.db")
    (tmp_path / "existing.db").touch()
    monkeypatch.setattr(plane, "connect_ro", lambda _path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(plane, "require_current_schema", lambda _conn: (_ for _ in ()).throw(
        PendingMigrationError("private migration detail")))

    rc = plane.cmd_plane_doctor(SimpleNamespace(root=str(tmp_path), json=True))

    stdout, stderr = capsys.readouterr()
    result = json.loads(stdout)
    assert rc == 7
    assert not stderr
    assert result["error"]["code"] == "migration_required"
    assert "private migration detail" not in stdout
    assert result["data"] == {"status": "refused", "rungs": [], "attention_count": 0}


def test_plane_doctor_json_storage_refusal_is_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    monkeypatch.setattr(plane, "db_file", lambda _root: tmp_path / "existing.db")
    (tmp_path / "existing.db").touch()

    def inaccessible(_path):
        raise sqlite3.OperationalError("private storage detail")

    monkeypatch.setattr(plane, "connect_ro", inaccessible)
    rc = plane.cmd_plane_doctor(SimpleNamespace(root=str(tmp_path), json=True))

    stdout, stderr = capsys.readouterr()
    result = json.loads(stdout)
    assert rc == 6
    assert not stderr
    assert result["error"]["code"] == "unavailable"
    assert result["data"]["status"] == "refused"
    assert "private storage detail" not in stdout


def test_plane_doctor_switch_resolution_failure_is_attention(tmp_path, monkeypatch, capsys):
    from claudlobby import switches

    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    monkeypatch.setattr(switches, "resolve", lambda *_: (_ for _ in ()).throw(
        RuntimeError("switches unavailable")))

    rc = main(["--root", str(tmp_path), "plane", "doctor", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert rc == 4
    assert result["data"]["status"] == "attention"
    assert any(row["name"] == "switches" and row["status"] == "attention"
               for row in result["data"]["rungs"])


def test_orphaned_stages_awaiting_their_hour_are_not_a_paused_replay(tmp_path):
    """#2086. The daemon replays a stage that was never renamed only once it is
    an hour old (STAGED_ORPHAN_AGE_S), so until then its age is that rule, not
    a lag. The staged-depth rung read every such wait as "replay is not keeping
    up or is paused". It now reports them apart, and calls one late only once it
    is STAGED_STALE_S past the hour (measured, they leave within ~50 s)."""
    from claudlobby.plane import health
    from claudlobby.plane.daemon import STAGED_ORPHAN_AGE_S
    staged = tmp_path / "state" / "plane" / "staged"
    staged.mkdir(parents=True)
    now = time.time()

    def stage(name, age):
        path = staged / name
        path.write_text('{"events": []}\n')
        os.utime(path, (now - age, now - age))
        return path

    for n in range(3):
        stage(f".{n}-ev_{n:032x}.batch.{4_000_000 + n}.tmp", STAGED_ORPHAN_AGE_S - 600)
    ok, detail = health.staged_rung(health.scan_staged(tmp_path), True)
    assert ok, detail
    assert "3 of them orphaned stage(s)" in detail and "paused" not in detail
    assert health.staged_summary(tmp_path)["orphaned"] == 3
    batch = stage("1-ev_x.batch", health.STAGED_STALE_S + 100)  # a renamed batch this old is stalled
    ok, detail = health.staged_rung(health.scan_staged(tmp_path), True)
    assert not ok and detail.endswith("replay is not keeping up or is paused; see the daemon log")
    batch.unlink()
    stage(f".9-ev_{'9' * 32}.batch.4000009.tmp", STAGED_ORPHAN_AGE_S + health.STAGED_STALE_S + 60)
    ok, detail = health.staged_rung(health.scan_staged(tmp_path), True)
    assert not ok and "past its replay at the hour" in detail


def test_a_daemon_listening_past_the_probe_deadline_reads_slow_not_stopped(tmp_path, monkeypatch,
                                                                         capsys):
    """#2086. One 2 s probe that a listening daemon left unanswered read "started
    Nx historically but not serving", pointing at the service, and under #1693's
    load the same daemon answered minutes later. A stopped daemon refuses the
    connection; one that takes it and does not answer is slow or stuck, and its
    queue is still replayed, so the staged rung does not call it unrecorded."""
    from claudlobby.plane.emit_api import emit_batch
    from claudlobby.plane.ids import ensure_host_uid
    monkeypatch.setattr("claudlobby.context.get_resources", source_package)
    initialize_plane(tmp_path)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "plane-daemon",
                           "payload": {"event": "daemon_started", "subject_kind": "host",
                                       "subject_uid": ensure_host_uid(tmp_path / "state")}}])
    (tmp_path / "state" / "plane" / "staged").mkdir()
    (tmp_path / "state" / "plane" / "staged" / "1-ev_x.batch").write_text('{"events": []}\n')
    directory = short_socket_dir("pd-")
    try:
        with listening_socket(directory / "s", answer=None) as sock:
            monkeypatch.setenv("PLANE_SOCKET", str(sock))
            rc = plane.cmd_plane_doctor(SimpleNamespace(root=str(tmp_path), json=True))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    rungs = {row["name"]: row for row in json.loads(capsys.readouterr().out)["data"]["rungs"]}
    assert rc == 4 and rungs["daemon"]["status"] == "attention"
    assert rungs["daemon"]["detail"].startswith(f"listening on {sock} but no answer within 2s"
                                                " — slow or stuck, not stopped"), rungs["daemon"]
    assert "historically but not serving" not in rungs["daemon"]["detail"]
    assert rungs["staged depth"]["status"] == "ok", rungs["staged depth"]
