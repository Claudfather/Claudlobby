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


def test_channel_links_old_cross_fleet_task_to_recorded_owner_not_emitter_or_room(tmp_path):
    _seed(tmp_path)
    emit_batch(tmp_path, [{"event_type": "work_item", "emitter": "t", "fleet": "engineering",
        "payload": {"work_item_id": "wi_" + f"{i:032x}", "title": f"newer {i}",
                    "created_by": "bot:engineering/mgr"}} for i in range(205)])
    emit_batch(tmp_path, [{"event_type": "communication", "emitter": "t", "fleet": "data",
        "payload": {"msg_id": "msg_" + "c" * 32, "sender": "bot:data/one",
                    "recipient": "bot:engineering/one", "message_class": "answer",
                    "reply_to_msg_id": "msg_" + "a" * 32, "work_item_id": TASK,
                    "body": "Recent cross-team reply to older work"}}])
    c = client(tmp_path)
    assert TASK not in {t["task_id"] for t in c.get("/api/tasks?fleet=engineering").json()["data"]["tasks"]}
    with sqlite3.connect(db_file(tmp_path)) as conn:
        owner = conn.execute("SELECT fleet_uid,host_uid FROM work_items WHERE work_item_id=?", (TASK,)).fetchone()
    before = hashlib.sha256(db_file(tmp_path).read_bytes()).hexdigest()
    for room in ("engineering", "data", "all"):
        channel = c.get("/api/channel?fleet=" + room).json()["data"]
        linked = next(t for t in channel["threads"] if t["work_item_id"] == TASK)
        assert linked["task_link"] == {"task_id": TASK, "fleet": "engineering",
                                      "fleet_uid": owner[0], "host_uid": owner[1]}
        assert any(m["sender_short"] == "data/one" for m in linked["messages"])
        assert c.get("/api/tasks/" + linked["task_link"]["task_id"] + "?fleet=" + linked["task_link"]["fleet"]).json()["data"]["task"]["task_id"] == TASK
    assert c.get("/api/tasks/" + TASK + "?fleet=data").json()["state"] == "not_found"
    assert hashlib.sha256(db_file(tmp_path).read_bytes()).hexdigest() == before


def test_channel_does_not_offer_a_task_link_when_canonical_host_ownership_is_invalid(tmp_path):
    _seed(tmp_path)
    emit_batch(tmp_path, [{"event_type": "communication", "emitter": "t", "fleet": "engineering",
        "payload": {"msg_id": "msg_" + "c" * 32, "sender": "bot:engineering/mgr",
                    "recipient": "bot:engineering/one", "message_class": "chat",
                    "work_item_id": TASK, "body": "task ID in text is not authority"}}])
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET host_uid=? WHERE work_item_id=?", ("host_" + "f" * 32, TASK))
    threads = client(tmp_path).get("/api/channel?fleet=engineering").json()["data"]["threads"]
    assert all(t["task_link"] is None for t in threads)


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


def task_event(kind, *, actor="bot:engineering/one", **detail):
    return {"event_type": "task", "emitter": "t", "fleet": "engineering",
        "payload": {"work_item_id": TASK, "assignment_id": "asg_" + "a" * 32,
                    "event": kind, "actor": actor, **detail}}


def test_detail_attention_uses_full_snapshot_before_caps_and_board_window(tmp_path, monkeypatch):
    _seed(tmp_path)
    question = "Choose <one> or two?\nThe next step depends on your answer."
    emit_batch(tmp_path, [task_event("escalated", question=question, by="synthetic-manager"),
        *[task_event("nudged", reason="Still waiting", by="synthetic-owner") for _ in range(4)],
        *[{"event_type": "work_item", "emitter": "t", "fleet": "engineering",
           "payload": {"work_item_id": "wi_" + f"{i:032x}", "title": f"newer {i}",
                       "created_by": "bot:engineering/mgr"}} for i in range(205)]])
    monkeypatch.setattr(view, "_TASK_DETAIL_HISTORY", 1)
    c = client(tmp_path)
    assert TASK not in {t["task_id"] for t in c.get("/api/tasks?fleet=engineering").json()["data"]["tasks"]}
    task = c.get(URL).json()["data"]["task"]
    assert task["attention_question"] == question and task["attention_by"] == "synthetic-manager"
    assert task["attention_reason"][0] == "escalated"
    assert [e["event"] for e in task["history"]] == ["nudged"]
    assert task["history_window"]["truncated"] is True
    emit_batch(tmp_path, [task_event("progress", summary="Answer incorporated")])
    assert c.get(URL).json()["data"]["task"]["attention_question"] is None


def test_detail_explicit_projections_and_consistent_actor_labels(tmp_path):
    _seed(tmp_path)
    emit_batch(tmp_path, [task_event("accepted", summary="Started"),
                         task_event("completed", summary="Finished")])
    task = client(tmp_path).get(URL).json()["data"]["task"]
    assert set(task) == {"task_id", "fleet_uid", "title", "body", "repo", "project_key",
        "workstream_id", "created_by_uid", "occurred_at", "state", "fleet", "created_by_alias",
        "resolved", "current_assignment", "assignments", "history", "terminal_event", "issues",
        "display_ids", "history_window", "assignments_window", "issues_window", "display_ids_window",
        "attention", "attention_reason", "attention_since", "attention_question", "attention_by",
        "attention_act_reason", "delivery"}
    assignment = task["assignments"][0]
    assert set(assignment) == {"assignment_id", "task_id", "assignee_uid", "assigned_by_uid", "expected_by",
        "dispatch_message_id", "occurred_at", "state", "assignee_alias", "assignee_short", "assigned_by_alias",
        "assigned_by_short", "terminal_event", "history", "history_window"}
    assert assignment["assignee_short"] == "one" and assignment["assigned_by_short"] == "mgr"
    assert task["current_assignment"] is None
    for event in [*task["history"], task["terminal_event"], *assignment["history"], assignment["terminal_event"]]:
        assert set(event) == {"event_id", "task_id", "assignment_id", "event", "actor_uid", "occurred_at",
                             "detail", "deadline", "successor_id", "actor_alias", "actor_short"}
        assert event["actor_alias"] == "bot:engineering/one" and event["actor_short"] == "one"
        assert "host_uid" not in event and "emitter" not in event and "ingested_at" not in event


def test_assignment_history_actor_aliases_use_bounded_lookup_batches(tmp_path, monkeypatch):
    _seed(tmp_path)
    emit_batch(tmp_path, [task_event("progress", actor=f"bot:engineering/synthetic-{i}", summary="Recorded")
                          for i in range(405)])
    monkeypatch.setattr(view, "_TASK_DETAIL_HISTORY", 1)
    monkeypatch.setattr(view, "_TASK_DETAIL_ASSIGNMENT_HISTORY", 500)
    conn = view._ro_conn(db_file(tmp_path))
    class BoundedReader:
        def execute(self, query, parameters=()):
            # Python 3.10 lacks Connection.setlimit; enforce the same bound
            # around the real SQL query on every supported interpreter.
            assert len(parameters) <= 400, "query exceeds the parameter limit"
            return conn.execute(query, parameters)

        def __getattr__(self, name):
            return getattr(conn, name)
    try:
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 400)
        conn.execute("BEGIN")
        task = view._fetch_task_detail(BoundedReader(), TASK, "engineering")["task"]
    finally:
        conn.close()
    history = task["assignments"][0]["history"]
    assert len(history) == 405 and len(task["history"]) == 1
    assert {event["actor_short"] for event in history} == {f"synthetic-{i}" for i in range(405)}
    assert all(event["actor_alias"].startswith("bot:engineering/synthetic-") for event in history)


def test_detail_attention_and_provenance_share_snapshot(protected, tmp_path, monkeypatch):
    _, c, *_ = protected
    emit_batch(tmp_path, [task_event("escalated", question="Original question", by="synthetic-manager")])
    with sqlite3.connect(db_file(tmp_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
    original = view._detail_attention
    def concurrent(conn, snapshot, task):
        assert conn.in_transaction
        emit_batch(tmp_path, [task_event("progress", summary="Later answer")])
        return original(conn, snapshot, task)
    with monkeypatch.context() as patch:
        patch.setattr(view, "_detail_attention", concurrent)
        result = c.get(URL).json()
    assert result["data"]["task"]["attention_question"] == "Original question"
    assert c.get(URL).json()["data"]["task"]["attention_question"] is None
