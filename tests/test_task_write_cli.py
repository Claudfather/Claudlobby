"""Public task writes against private selected activation and real Plane storage."""

import builtins
import json
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import activation, context, runtime_admission
from claudlobby.__main__ import main
from claudlobby.plane.db import db_file
from tests.test_activation import cold  # noqa: F401 — shared activation fixture
from tests.test_releases import installed  # noqa: F401 — dependency of cold


@pytest.fixture
def active(cold, monkeypatch):  # noqa: F811 — pytest fixture parameter
    root, release, plan, host = cold
    for key in ("FLEET_NAME", "CLAUDLOBBY_FLEET", "BOT_ID", "BOT_NAME", "BOT_DIR",
                "FLEET_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key, raising=False)
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(context, "get_resources", lambda: host.package)
    return root, release


def _call(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), "--json", *argv]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1 and result["ok"] is (expected == 0)
    return result


def _counts(root):
    with sqlite3.connect(db_file(root)) as conn:
        counts = tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("identity_registry", "work_items", "assignments", "communications"))
        return (*counts, conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'").fetchone()[0])


def test_admit_assign_accept_are_distinct_committed_operations(active, monkeypatch, capsys, tmp_path):
    root, release = active
    body = tmp_path / "task.txt"
    body.write_text("Review the private evidence", encoding="utf-8")
    admit_id, assign_id, accept_id = (str(uuid4()) for _ in range(3))
    admitted = _call(capsys, root, "task", "admit", "--title", "Review evidence",
                     "--body-file", str(body), "--repo", "owner/repo",
                     "--request-id", admit_id)
    task_id = admitted["data"]["task_id"]
    assert admitted["request_id"] == admit_id and admitted["release_id"] == release.release_id
    assert admitted["data"]["state"] == "queued"
    assert admitted["data"]["fleet"] == "example" and admitted["data"]["replayed"] is False
    assert admitted["data"]["recording"] == "committed"
    assert admitted["data"]["assignment_id"] is None
    assert admitted["data"]["delivery"] == "not_requested"
    before_replay = _counts(root)
    again = _call(capsys, root, "task", "admit", "--title", "Review evidence",
                  "--body-file", str(body), "--repo", "owner/repo", "--request-id", admit_id)
    assert again["data"]["outcome"] == again["data"]["recording"] == "unchanged"
    assert again["data"]["replayed"] is True and again["data"]["task_id"] == task_id
    assert _counts(root) == before_replay and before_replay[2:] == (0, 0, 0)
    assert main(["--root", str(root), "task", "admit", "--title", "Review evidence",
                 "--body-file", str(body), "--repo", "owner/repo", "--request-id", admit_id]) == 0
    plain = capsys.readouterr().out
    assert plain.count("\n") == 1 and task_id in plain
    assert "recording=unchanged" in plain and "delivery=not_requested" in plain
    assert "notification=not_requested" in plain and _counts(root) == before_replay

    assigned = _call(capsys, root, "task", "assign", task_id, "--bot", "worker",
                     "--request-id", assign_id)
    assignment_id = assigned["data"]["assignment_id"]
    assert assigned["request_id"] == assign_id and assigned["data"]["state"] == "assigned"
    assert assigned["data"]["delivery"] == "not_requested"
    assert _counts(root)[2:] == (1, 0, 0)
    before_wrong_refs = _counts(root)
    wrong_task = _call(capsys, root, "task", "assign", assignment_id, "--bot", "worker",
                       "--request-id", str(uuid4()), expected=4)
    assert wrong_task["error"]["code"] == "wrong_reference"
    assert wrong_task["error"]["hint"]["next_command"] == ["task", "show", task_id]
    wrong_assignment = _call(capsys, root, "assignment", "accept", task_id,
                             "--request-id", str(uuid4()), expected=4)
    assert wrong_assignment["error"]["code"] == "wrong_reference"
    assert wrong_assignment["error"]["hint"]["next_command"] == [
        "assignment", "show", assignment_id]
    assert _counts(root) == before_wrong_refs

    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/worker"))
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", "r-" + "0" * 64)
    before_stale = _counts(root)
    stale = _call(capsys, root, "assignment", "accept", assignment_id,
                  "--request-id", accept_id, expected=7)
    assert stale["error"]["code"] == "release_mismatch" and _counts(root) == before_stale
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    accepted = _call(capsys, root, "assignment", "accept", assignment_id,
                     "--request-id", accept_id)
    assert accepted["request_id"] == accept_id and accepted["release_id"] == release.release_id
    assert accepted["data"]["task_id"] == task_id
    assert accepted["data"]["assignment_id"] == assignment_id
    assert accepted["data"]["state"] == "active"
    assert accepted["data"]["delivery"] == "not_requested"
    assert _counts(root)[2:] == (1, 0, 1)


def test_bad_inputs_and_wrong_executable_refuse_before_identity_or_task_writes(active, monkeypatch, capsys,
                                                                               tmp_path):
    root, release = active
    before = _counts(root)
    _call(capsys, root, "task", "admit", "--title", "Valid", "--request-id", "not-a-uuid",
          expected=2)
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    _call(capsys, root, "task", "admit", "--title", "Valid", "--body-file", str(invalid),
          "--request-id", str(uuid4()), expected=2)
    assert _counts(root) == before

    monkeypatch.setattr(runtime_admission.RuntimeIdentity, "current", classmethod(lambda cls:
        runtime_admission.RuntimeIdentity(release.cli_path.with_name("other-cli"),
                                          release.native_path, release.inputs.artifact_id)))
    wrong = _call(capsys, root, "task", "admit", "--title", "Valid",
                  "--request-id", str(uuid4()), expected=7)
    assert wrong["error"]["code"] == "release_mismatch"
    assert _counts(root) == before


def test_task_write_help_and_syntax_do_not_import_mutation_owner(monkeypatch, capsys):
    original = builtins.__import__

    def reject(name, *args, **kwargs):
        if name.endswith(".task_write") or name.endswith(".task_operations"):
            raise ImportError("task mutation owner is unavailable")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", reject)
        with pytest.raises(SystemExit) as exited:
            main(["task", "admit", "--help"])
        assert exited.value.code == 0
        assert "--request-id" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exited:
        main(["--json", "assignment", "accept", "--bad=private-value"])
    output = capsys.readouterr().out
    assert exited.value.code == 2 and "private-value" not in output
    assert json.loads(output)["command"] == "assignment.accept"
