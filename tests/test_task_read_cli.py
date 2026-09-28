"""Public canonical reads from a selected private activation and real SQLite."""

import builtins
import json
import sqlite3

import pytest

from claudlobby import activation, context
from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from tests.test_activation import cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_state import _assignment, _task


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, _, plan, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(context, "get_resources", lambda: host.package)
    return root, host


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    assert result["request_id"] is None
    return result


def test_list_show_assignment_and_scoped_reference_hints_are_read_only(active, capsys):
    root, host = active
    from claudlobby.activation_identity import read_selected_identity_bindings
    bindings = read_selected_identity_bindings(root, "example", package=host.package)
    with sqlite3.connect(db_file(root)) as conn:
        _task(conn, "wi_first", fleet_uid=bindings["fleet_uid"], source_ref="dispatch-log:old-17")
        _task(conn, "wi_second", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE work_items SET project_key=NULL, workstream_id=NULL "
                     "WHERE fleet_uid=?", (bindings["fleet_uid"],))
        _assignment(conn, "asg_first", "wi_first", fleet_uid=bindings["fleet_uid"])
        conn.execute("UPDATE assignments SET assignee_uid=? WHERE assignment_id='asg_first'",
                     (bindings["bots"]["worker"],))
    with sqlite3.connect(db_file(root)) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])

    first_result = _call(capsys, root, "task", "list", "--limit", "1")
    assert first_result["release_id"] == host.release.release_id
    first = first_result["data"]
    assert [item["task_id"] for item in first["items"]] == ["wi_first"]
    assert first["next_cursor"]
    second = _call(capsys, root, "task", "list", "--limit", "1", "--cursor",
                   first["next_cursor"])["data"]
    assert [item["task_id"] for item in second["items"]] == ["wi_second"]
    assert second["next_cursor"] is None
    filtered = _call(capsys, root, "task", "list", "--bot", "worker")["data"]
    assert [item["task_id"] for item in filtered["items"]] == ["wi_first"]
    shown = _call(capsys, root, "task", "show", "wi_first")["data"]["task"]
    assert shown["display_ids"] == ["old-17"]
    assignment = _call(capsys, root, "assignment", "show", "asg_first")["data"]
    assert assignment["assignment"]["task_id"] == shown["task_id"]
    wrong = _call(capsys, root, "task", "show", "asg_first", expected=4)["error"]
    assert wrong["code"] == "wrong_reference"
    assert wrong["hint"]["next_command"] == ["task", "show", "wi_first"]
    assert main(["--root", str(root), "task", "show", "asg_first"]) == 4
    plain = capsys.readouterr()
    assert "inspect claudlobby task show wi_first" in plain.err
    assert "ReferenceHint(" not in plain.err and "{'candidates'" not in plain.err
    legacy = _call(capsys, root, "task", "show", "old-17", expected=4)["error"]
    assert legacy["hint"]["total_matches"] == 1
    missing = _call(capsys, root, "task", "show", "wi_foreign", expected=3)["error"]
    assert missing["code"] == "not_found"
    if missing["hint"] is not None:
        assert missing["hint"]["candidates"] == []
    _call(capsys, root, "task", "list", "--limit", "2", "--cursor",
          first["next_cursor"], expected=2)
    _call(capsys, root, "task", "list", "--bot", "outside", expected=3)
    with sqlite3.connect(db_file(root)) as conn:
        after = (conn.execute("SELECT COUNT(*) FROM identity_registry").fetchone()[0],
                 conn.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0])
    assert after == before


def test_empty_or_absent_plane_and_invalid_generated_origin_never_initialize(active, monkeypatch, capsys):
    root, _ = active
    assert _call(capsys, root, "task", "list")["data"]["items"] == []
    assert _call(capsys, root, "--seed", "task", "list", expected=4)["error"]["code"] == "conflict"
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "")
    error = _call(capsys, root, "task", "list", expected=2)["error"]
    assert error["code"] == "invalid_argument"
    monkeypatch.delenv("BOT_ID")
    monkeypatch.delenv("FLEET_NAME")
    db_file(root).rename(root / "plane-db-offline")
    assert _call(capsys, root, "task", "list", expected=6)["error"]["code"] == "unavailable"
    assert not db_file(root).exists()


def test_help_and_syntax_remain_dependency_light(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.split(".")[0] in {"yaml", "jinja2", "pydantic"} or name.endswith(".task_read"):
            raise ImportError("blocked task read dependency")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["task", "list", "--help"])
        assert exited.value.code == 0
        assert "--cursor" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "assignment", "show", "--bad=private-value"])
    output = capsys.readouterr().out
    assert exited.value.code == 2 and "private-value" not in output
    assert json.loads(output)["command"] == "assignment.show"
