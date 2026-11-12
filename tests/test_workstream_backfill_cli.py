"""`migration workstream-backfill` and `workstream close` through the command (#1053).

The backfill previews by default and records nothing; `--apply` with a request ID records it, and
the same ID replays. A close's dry run reads the workstream's tasks: it names the refusal the close
would raise, or the tasks it would withdraw, and records nothing; the close's text names every task
it withdrew. They run on an installed release, as `task move`'s command test does.
"""
import sqlite3
from uuid import uuid4

from claudlobby.__main__ import main
from claudlobby.plane import emit_api
from claudlobby.plane.db import db_file
from claudlobby.plane.ids import mint_work_item_id
from claudlobby.task_state import TASK_EMITTER
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401 — activation and short socket root
from tests.test_releases import installed  # noqa: F401 — dependency of cold
from tests.test_task_move_cli import readers  # noqa: F401 — the test tree's readers
from tests.test_task_write_cli import _call, active  # noqa: F401 — the installed-release CLI fixture
from tests.test_workstream_backfill import before_a_live_task_needed_a_workstream


def _legacy(root, title="Before workstreams"):
    """A task as admit recorded it before every task had a workstream: a live row that names none."""
    task_id = mint_work_item_id()
    with before_a_live_task_needed_a_workstream():
        emit_api.emit_batch(root, [{
            "event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "example",
            "payload": {"work_item_id": task_id, "title": title, "created_by": "bot:example/manager"}}],
            require_commit=True)
    return task_id


def _rows(root):
    """The newest row in either table a backfill writes: `workstreams` for each task's own
    workstream, and `events` for each move."""
    with sqlite3.connect(db_file(root)) as conn:
        return conn.execute("SELECT max(seq) FROM (SELECT max(ingest_seq) AS seq FROM workstreams"
                            " UNION ALL SELECT max(ingest_seq) FROM events)").fetchone()[0]


def _text(capsys, root, *argv, expected=0):
    assert main(["--root", str(root), *argv]) == expected
    return capsys.readouterr().out.splitlines()


def _admit(capsys, root, title, *extra):
    return _call(capsys, root, "task", "admit", "--title", title, *extra,
                 "--request-id", str(uuid4()))["data"]["task_id"]


def test_the_backfill_previews_then_files_each_task_through_the_command(readers, capsys):
    root, _ = readers
    legacy = _legacy(root)
    _admit(capsys, root, "Admitted with its own")
    own = "ws_" + legacy[3:]
    before = _rows(root)
    preview = _call(capsys, root, "migration", "workstream-backfill")
    assert preview["command"] == "migration.workstream-backfill"
    data = preview["data"]
    assert (data["mode"], data["recording"], data["left"]) == ("preview", "not_attempted", [])
    assert [(task["task_id"], task["from_workstream_id"], task["workstream_id"])
            for task in data["tasks"]] == [(legacy, None, own)]
    assert _text(capsys, root, "migration", "workstream-backfill") == [
        "backfill preview: 1 task(s) to file into their own workstreams; no request or fact recorded",
        f"would file (no workstream): {legacy}"]
    assert _rows(root) == before  # a preview writes nothing
    # --apply needs a request ID, and a preview takes none.
    for argv in (("--apply",), ("--request-id", str(uuid4()))):
        refused = _call(capsys, root, "migration", "workstream-backfill", *argv, expected=2)
        assert refused["error"]["code"] == "invalid_argument"
    assert _rows(root) == before
    request_id = str(uuid4())
    argv = ("migration", "workstream-backfill", "--apply", "--request-id", request_id)
    applied = _call(capsys, root, *argv)
    assert applied["request_id"] == request_id
    result = applied["data"]
    assert (result["mode"], result["recording"], result["replayed"]) == ("apply", "committed", False)
    assert result["filed_tasks"] == [{"task_id": legacy, "from_workstream_id": None,
                                      "workstream_id": own}]
    stage, = _call(capsys, root, "request", "show", request_id)["data"]["request"]["stages"]
    assert [fact["family"] for fact in stage["expected_facts"]] == ["workstream", "system"]
    assert (stage["proof"]["matched"], stage["recorded_status"]) == (2, "committed")
    assert _call(capsys, root, "task", "show", legacy)["data"]["task"]["workstream_id"] == own
    listed = _call(capsys, root, "workstream", "list")["data"]["workstreams"][own]
    assert (listed["task_ids"], listed["status"]) == ([legacy], "active")
    # The same request replays; its text names the task it filed.
    assert _text(capsys, root, *argv) == [
        "backfill: 1 task(s) filed into their own workstreams; recording=committed; replayed=True",
        f"filed (no workstream): {legacy}"]
    after = _rows(root)
    # Run again, it finds nothing to file and records nothing.
    again = _call(capsys, root, "migration", "workstream-backfill", "--apply",
                  "--request-id", str(uuid4()))["data"]
    assert (again["recording"], again["filed_tasks"]) == ("not_attempted", [])
    assert _rows(root) == after


def test_a_worker_cannot_run_the_backfill(readers, monkeypatch, capsys):
    root, release = readers
    legacy = _legacy(root)
    for key, value in {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root), "FLEET_NAME": "example",
                       "CLAUDLOBBY_INSTALLATION": str(release.directory), "BOT_ID": "worker",
                       "BOT_DIR": str(root / "runtime/bots/worker")}.items():
        monkeypatch.setenv(key, value)
    before = _rows(root)
    for argv in ((), ("--apply", "--request-id", str(uuid4()))):
        refused = _call(capsys, root, "migration", "workstream-backfill", *argv, expected=4)
        assert refused["error"]["code"] == "conflict"
    assert _rows(root) == before
    assert _call(capsys, root, "task", "show", legacy)["data"]["task"]["workstream_id"] is None


def test_a_close_dry_run_reads_its_tasks_and_the_close_names_each_one_it_withdrew(readers, capsys):
    root, _ = readers
    wid = _call(capsys, root, "workstream", "open", "To close",
                "--request-id", str(uuid4()))["data"]["workstream_ids"][0]
    first = _admit(capsys, root, "First", "--workstream", wid)
    second = _admit(capsys, root, "Second", "--workstream", wid)
    _call(capsys, root, "task", "assign", first, "--bot", "worker", "--request-id", str(uuid4()))
    before = _rows(root)
    # A task with a live assignment: the dry run names the refusal the close would raise.
    dry = _call(capsys, root, "workstream", "close", wid, "--dry-run",
                "--request-id", str(uuid4()))["data"]
    assert (dry["recording"], dry["scope_state"]) == ("not_attempted", "read")
    refusal = dry["close"]["refusal"]
    assert refusal["code"] == "conflict" and dry["close"]["withdraw_task_ids"] == []
    assert f"{first} (assigned, a live assignment)" in refusal["message"]
    assert _text(capsys, root, "workstream", "close", wid, "--dry-run", "--request-id", str(uuid4())) == [
        f"close {wid} would be refused (conflict): {refusal['message']}; no request or fact recorded."]
    assert _rows(root) == before
    # With that task ended, the dry run names the queued tasks the close would withdraw.
    _call(capsys, root, "task", "withdraw", first, "--reason", "Not needed", "--request-id", str(uuid4()))
    third = _admit(capsys, root, "Third", "--workstream", wid)
    queued = sorted((second, third))
    before = _rows(root)
    dry = _call(capsys, root, "workstream", "close", wid, "--dry-run",
                "--request-id", str(uuid4()))["data"]["close"]
    assert dry == {"refusal": None, "withdraw_task_ids": queued}
    assert _rows(root) == before
    # The close itself names, in its text, every task it withdrew.
    assert _text(capsys, root, "workstream", "close", wid, "--request-id", str(uuid4())) == [
        f"close: {wid}; recording=committed; replayed=False",
        f"withdrawn by the close: {', '.join(queued)}"]
    for task_id in queued:
        assert _call(capsys, root, "task", "show", task_id)["data"]["task"]["state"] == "cancelled"
    # A closed workstream's dry run names the close's refusal.
    dry = _call(capsys, root, "workstream", "close", wid, "--dry-run",
                "--request-id", str(uuid4()))["data"]["close"]
    assert dry == {"refusal": {"code": "conflict",
                               "message": "terminal workstream cannot be changed except by prune"},
                   "withdraw_task_ids": []}
