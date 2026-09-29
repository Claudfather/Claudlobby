"""The public Plane doctor keeps its rungs in the common JSON result."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from claudlobby.__main__ import main
from claudlobby.commands import plane
from claudlobby.plane.schema_state import PendingMigrationError
from tests.package_fixtures import source_package


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
