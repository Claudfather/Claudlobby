"""`task move` through the command (#1053): its route, its result, its replay and every read.

It runs on an installed release, as the other task-write commands' tests do, so it lives apart
from `test_task_move.py`, whose fixture needs the ordinary temporary directory.
"""
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import brief, paths
from claudlobby.plane.db import db_file
from tests.package_fixtures import source_package
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_write_cli import _call, active  # noqa: F401 — the installed-release CLI fixture


def _moves(root):
    with sqlite3.connect(db_file(root)) as conn:
        return conn.execute("SELECT count(*) FROM events WHERE kind='system' AND event='task_moved'"
                            ).fetchone()[0]


@pytest.fixture
def readers(active, monkeypatch):  # noqa: F811 — fixture dependency
    """The sealed test release carries the native scripts without the package beside them, and
    the workstream registry reads the tasks' moves through the package's task reducer. So the
    test tree's readers are selected at the import boundary, as the check-in commands' tests do:
    the loader, and the brief's plane session, which binds its loader when it is imported."""
    root, release = active
    load_module = paths.load_lib_module
    native = source_package().native
    monkeypatch.setattr(paths, "load_lib_module", lambda directory, name:
                        load_module(native if directory == release.native else directory, name))
    monkeypatch.setattr(brief, "load_dispatch_doors", lambda selected:
                        load_module(native, "dispatch-overdue.py"))
    return root, release


def test_task_move_through_the_command_records_one_move_and_replays_it(readers, capsys):
    root, _ = readers
    admitted = _call(capsys, root, "task", "admit", "--title", "Moves through the command",
                     "--request-id", str(uuid4()))["data"]
    own = admitted["workstream_id"]
    target = _call(capsys, root, "workstream", "open", "Command target",
                   "--request-id", str(uuid4()))["data"]["workstream_ids"][0]
    request_id = str(uuid4())
    argv = ("task", "move", admitted["task_id"], "--workstream", target,
            "--reason", "Belongs to the target", "--request-id", request_id)
    moved = _call(capsys, root, *argv)
    assert moved["command"] == "task.move"
    assert (moved["data"]["workstream_id"], moved["data"]["replayed"]) == (target, False)
    assert _moves(root) == 1
    stage, = _call(capsys, root, "request", "show", request_id)["data"]["request"]["stages"]
    assert [fact["family"] for fact in stage["expected_facts"]] == ["system"]
    assert (stage["proof"]["matched"], stage["recorded_status"]) == (1, "committed")
    assert _call(capsys, root, "task", "show", admitted["task_id"])["data"]["task"][
        "workstream_id"] == target
    shown = _call(capsys, root, "workstream", "show", target)["data"]["workstream"]
    assert shown["task_ids"] == [admitted["task_id"]]
    # `workstream show` takes an opened workstream's ID; the list holds a task's own one too.
    left = _call(capsys, root, "workstream", "list")["data"]["workstreams"][own]
    assert (left["task_ids"], left["status"], left["closed_by"]) == (
        [], "abandoned", "task:" + admitted["task_id"])
    assert _call(capsys, root, *argv)["data"]["replayed"] is True
    assert _moves(root) == 1
    refused = _call(capsys, root, "task", "move", admitted["task_id"], "--workstream", own,
                    "--reason", "Back home", "--request-id", str(uuid4()), expected=4)
    assert refused["error"]["code"] == "conflict" and _moves(root) == 1

