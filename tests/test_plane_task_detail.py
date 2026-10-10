"""Selected task reads over disposable Plane roots, including protected admission."""
import hashlib
import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from claudlobby.plane import view
from claudlobby.plane.db import db_file
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.owner_access import SESSION_SECONDS
from tests.package_fixtures import source_package
from tests.test_plane_owner_view import protected  # noqa: F401
from tests.test_plane_two_fleets import _seed, _Sampler

TASK = "wi_" + "a" * 32
URL = "/api/tasks/" + TASK + "?fleet=engineering"


def client(root):
    return TestClient(view.create_app(root, sampler=_Sampler([]), package=source_package()))


def test_selected_detail_retains_full_body_and_history_outside_board_cap(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET body=? WHERE work_item_id=?", ("Full <task>\nSecond line", TASK))
    emit_batch(tmp_path, [{"event_type": "work_item", "emitter": "t", "fleet": "engineering",
        "payload": {"work_item_id": "wi_" + f"{i:032x}", "title": f"newer {i}",
                    "created_by": "bot:engineering/mgr"}} for i in range(205)])
    c = client(tmp_path)
    assert TASK not in {t["task_id"] for t in c.get("/api/tasks?fleet=engineering").json()["data"]["tasks"]}
    before = hashlib.sha256(db_file(tmp_path).read_bytes()).hexdigest()
    response = c.get(URL)
    assert response.status_code == 200
    detail = response.json()
    task = detail["data"]["task"]
    assert detail["state"] == "ok" and task["body"] == "Full <task>\nSecond line"
    assert task["task_id"] == TASK and task["fleet"] == "engineering"
    assert task["assignments"][0]["assignment_id"] == "asg_" + "a" * 32
    assert task["assignments_window"]["total"] == 1
    assert task["current_assignment"]["assignee_alias"] == "bot:engineering/one"
    assert hashlib.sha256(db_file(tmp_path).read_bytes()).hexdigest() == before


@pytest.mark.parametrize("path,state", [
    ("/api/tasks/historical-name?fleet=engineering", "invalid"),
    ("/api/tasks/" + TASK, "unknown"),
    ("/api/tasks/" + TASK + "?fleet=all", "unknown"),
    ("/api/tasks/" + TASK + "?fleet=Engineering", "unknown"),
    ("/api/tasks/" + TASK + "?fleet=data", "not_found"),
    ("/api/tasks/wi_" + "f" * 32 + "?fleet=engineering", "not_found"),
])
def test_missing_wrong_id_and_scope_are_honest(tmp_path, path, state):
    _seed(tmp_path)
    result = client(tmp_path).get(path).json()
    assert result["state"] == state and "data" not in result


def test_absent_detail_does_not_initialize_plane(tmp_path):
    result = client(tmp_path).get(URL).json()
    assert result["state"] == "absent"
    assert not (tmp_path / "state").exists()


def test_detail_bounds_presentation_after_reducing_all_events(tmp_path, monkeypatch):
    _seed(tmp_path)
    emit_batch(tmp_path, [{"event_type": "task", "emitter": "t", "fleet": "engineering",
        "payload": {"work_item_id": TASK, "assignment_id": "asg_" + "a" * 32,
                    "event": e, "summary": "recorded"}}
        for e in ("accepted", "progress", "completed")])
    monkeypatch.setattr(view, "_TASK_DETAIL_HISTORY", 1)
    monkeypatch.setattr(view, "_TASK_DETAIL_ASSIGNMENT_HISTORY", 1)
    task = client(tmp_path).get(URL).json()["data"]["task"]
    assert task["state"] == "completed"
    assert task["terminal_event"]["event"] == "completed"
    assert task["history_window"] == {"total": 3, "shown": 1, "limit": 1, "truncated": True}
    assert [e["event"] for e in task["history"]] == ["completed"]
    assert task["assignments"][0]["history_window"]["truncated"] is True


def test_large_body_is_unavailable_never_silently_truncated(tmp_path, monkeypatch):
    _seed(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET body=? WHERE work_item_id=?", ("Complete body " * 100, TASK))
    monkeypatch.setattr(view, "_TASK_DETAIL_BYTES", 32)
    result = client(tmp_path).get(URL).json()
    assert result["state"] == "unavailable" and "data" not in result
    assert "size limit" in result["remediation"]


def test_unresolved_history_remains_visible(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET emitter='claudlobby.tasks.future' WHERE work_item_id=?", (TASK,))
    task = client(tmp_path).get(URL).json()["data"]["task"]
    assert task["resolved"] is False
    assert any(i["code"] == "unknown_task_producer" and i["blocking"] for i in task["issues"])


def test_protected_detail_requires_session_and_same_source(protected, tmp_path):
    _, c, _, _, identity, _ = protected
    result = c.get(URL)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET host_uid='foreign-host',body='foreign secret'")
    result = c.get(URL)
    assert result.status_code == 403 and "foreign" not in result.text and "plane.db" not in result.text
    identity[0] = None
    assert c.get(URL).status_code == 403


def test_detail_held_response_rechecks_session(protected, tmp_path, monkeypatch):
    _, c, _, _, _, clock = protected
    original = view._fetch_task_detail
    def expire(*args):
        result = original(*args)
        clock[0] += SESSION_SECONDS
        return result
    monkeypatch.setattr(view, "_fetch_task_detail", expire)
    result = c.get(URL)
    assert result.status_code == 403 and "work for engineering" not in result.text


def test_detail_query_and_provenance_share_admitted_snapshot(protected, tmp_path, monkeypatch):
    _, c, *_ = protected
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original = view._fetch_task_detail
    checked = []
    def concurrent(conn, *args):
        assert conn.in_transaction
        before = conn.execute("SELECT ingest_seq FROM ingest_ledger ORDER BY ingest_seq DESC LIMIT 1").fetchone()[0]
        with sqlite3.connect(db_file(tmp_path)) as writer:
            writer.execute("UPDATE work_items SET title='later title' WHERE work_item_id=?", (TASK,))
            writer.execute("INSERT INTO ingest_ledger(event_id,family,ingested_at) VALUES('later','events','later')")
        result = original(conn, *args)
        checked.append(before)
        return result
    monkeypatch.setattr(view, "_fetch_task_detail", concurrent)
    result = c.get(URL).json()
    assert result["state"] == "ok" and result["data"]["task"]["title"] == "work for engineering"
    assert result["provenance"]["last_ingest_seq"] == checked[0]
