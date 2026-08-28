"""Phase-4 view daemon battery (design walk 2026-08-28).

Pins the walk's rulings as behavior: strictly read-only structurally; the
panel-state envelope on every endpoint (absent/unreadable are typed states
with remediation — never a zero); story-first thread assembly (dispatch +
delivery + reports + closure as ONE thread, alias-first names, telegram
destinations resolved through channels.json); SSE cursor semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.view import create_app  # noqa: E402

H = "a" * 32


def _full_capture(root: Path) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _seed_conversation(root: Path) -> None:
    """A full story: erlich dispatches ramanujan (work item + assignment +
    dispatch comm), delivery queued then delivered, ramanujan accepts,
    reports progress, completes — plus one telegram notice."""
    _full_capture(root)
    mgr, worker = "bot:f/erlich", "bot:f/ramanujan"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": "f",
         "payload": {"work_item_id": f"wi_{H}", "title": "Review PR #7768",
                     "created_by": mgr}},
        {"event_type": "assignment", "emitter": "t", "fleet": "f",
         "payload": {"assignment_id": f"asg_{H}", "work_item_id": f"wi_{H}",
                     "assignee": worker, "assigned_by": mgr,
                     "dispatch_msg_id": f"msg_{H}"}},
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": f"msg_{H}", "sender": mgr,
                     "recipient": worker, "message_class": "task_request",
                     "command_type": "task", "work_item_id": f"wi_{H}",
                     "assignment_id": f"asg_{H}",
                     "body": "Please review PR #7768 and post a verdict."}},
    ])
    for state in ("carrier_queued", "pane_submitted"):
        emit_batch(root, [{
            "event_type": "transmission", "emitter": "t", "fleet": "f",
            "payload": {"msg_id": f"msg_{H}", "attempt_no": 1,
                        "carrier": "tmux", "destination": "ramanujan",
                        "state": state}}])
    reply = "msg_" + "b" * 32
    emit_batch(root, [
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": reply, "sender": worker, "recipient": mgr,
                     "message_class": "report", "work_item_id": f"wi_{H}",
                     "reply_to_msg_id": f"msg_{H}",
                     "body": "Verdict posted: approve with two nits."}},
        {"event_type": "task", "emitter": "t", "fleet": "f",
         "payload": {"event": "completed", "work_item_id": f"wi_{H}",
                     "assignment_id": f"asg_{H}", "actor": worker}},
    ])
    emit_batch(root, [{
        "event_type": "communication", "emitter": "t", "fleet": "f",
        "payload": {"msg_id": "msg_" + "c" * 32, "sender": worker,
                    "recipient_raw": "-100999", "message_class": "notice",
                    "body": "FYI: verdict posted."}}])


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path))


# ---------------------------------------------------------------------------
# Ruling: strictly read-only, structurally
# ---------------------------------------------------------------------------

def test_no_write_routes_exist(tmp_path):
    app = create_app(tmp_path)
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            assert methods <= {"GET", "HEAD"}, (route.path, methods)


def test_connections_are_query_only(tmp_path):
    import sqlite3

    from claudlobby.plane import view

    _seed_conversation(tmp_path)
    conn = view._ro_conn(tmp_path / "state" / "plane" / "plane.db")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO ingest_ledger (event_id, family,"
                     " ingested_at) VALUES ('x', 'y', 'z')")
    conn.close()


# ---------------------------------------------------------------------------
# Ruling: panel-state envelope — absent/unreadable are typed, never zero
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/summary", "/api/channel",
                                  "/api/tasks", "/api/identities"])
def test_absent_db_is_a_typed_state(client, path):
    body = client.get(path).json()
    assert body["state"] == "absent"
    assert "remediation" in body
    assert "data" not in body  # never a fabricated zero


def test_unreadable_db_is_distinct_from_absent(tmp_path):
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "plane.db").mkdir()  # a dir, not a db
    body = TestClient(create_app(tmp_path)).get("/api/summary").json()
    assert body["state"] == "unreadable"


def test_healthz_503_when_absent(client):
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["state"] == "absent"


def test_ok_with_empty_data_is_ok(tmp_path):
    _full_capture(tmp_path)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t",
                           "payload": {"event": "daemon_started"}}])
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    assert body["state"] == "ok"
    assert body["data"]["threads"] == []  # legitimately idle — UI's word


# ---------------------------------------------------------------------------
# Ruling: story-first threads
# ---------------------------------------------------------------------------

def test_channel_threads_the_whole_conversation(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    assert body["state"] == "ok"
    threads = body["data"]["threads"]
    wi = [t for t in threads if t["work_item_id"] == f"wi_{H}"]
    assert len(wi) == 1, "dispatch + report group into ONE thread"
    t = wi[0]
    assert t["title"] == "Review PR #7768"
    assert [m["message_class"] for m in t["messages"]] == [
        "task_request", "report"]
    assert [e["event"] for e in t["task_events"]] == ["completed"]
    dispatch = t["messages"][0]
    assert dispatch["sender_short"] == "erlich"       # alias-first
    assert dispatch["recipient_short"] == "ramanujan"
    assert dispatch["body"].startswith("Please review")  # full capture words
    assert [x["event"] for x in dispatch["tx"]] == [
        "carrier_queued", "pane_submitted"]           # delivery history rides


def test_telegram_destination_resolves_to_name_never_raw_id(tmp_path):
    _seed_conversation(tmp_path)
    (tmp_path / "state" / "plane" / "channels.json").write_text(
        json.dumps({"-100999": "Engineering group"}))
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    notice = [m for t in body["data"]["threads"] for m in t["messages"]
              if m["message_class"] == "notice"][0]
    assert notice["recipient_short"] == "Engineering group"


def test_unmapped_telegram_destination_is_generic_not_raw(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    notice = [m for t in body["data"]["threads"] for m in t["messages"]
              if m["message_class"] == "notice"][0]
    assert notice["recipient_short"] == "Telegram"    # never "-100999"


# ---------------------------------------------------------------------------
# Tasks + identities
# ---------------------------------------------------------------------------

def test_tasks_carry_status_and_attention(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/tasks").json()
    rows = body["data"]["assignments"]
    assert rows[0]["status"] == "completed"
    assert rows[0]["attention"] is False
    assert rows[0]["assignee_short"] == "ramanujan"


def test_identities_are_alias_first(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/identities").json()
    shorts = {i["short"] for i in body["data"]["identities"]}
    assert {"erlich", "ramanujan"} <= shorts


# ---------------------------------------------------------------------------
# SSE cursor semantics
# ---------------------------------------------------------------------------

def _first_sse_chunk(client, url):
    with client.stream("GET", url) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                return json.loads(line[5:])
            if line.startswith(": ping"):
                return None


def test_stream_replays_rows_past_cursor(tmp_path):
    _seed_conversation(tmp_path)
    payload = _first_sse_chunk(TestClient(create_app(tmp_path)),
                               "/api/stream?cursor=0&once=1")
    assert payload is not None
    assert payload["rows"][0]["ingest_seq"] == 1
    assert payload["cursor"] >= len(payload["rows"])


def test_stream_at_head_pings_not_replays(tmp_path):
    _seed_conversation(tmp_path)
    from claudlobby.plane.db import connect, db_path
    conn = connect(db_path(tmp_path))
    head = conn.execute("SELECT MAX(ingest_seq) FROM ingest_ledger"
                        ).fetchone()[0]
    conn.close()
    payload = _first_sse_chunk(TestClient(create_app(tmp_path)),
                               f"/api/stream?cursor={head}&once=1")
    assert payload is None  # nothing to replay — a ping, never stale rows
