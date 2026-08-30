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


def test_dir_at_db_path_is_absent_not_unreadable(tmp_path):
    """source_state's decided-once rule: a directory where a file belongs is
    ABSENT — an unreadable+check-permissions answer sends someone to chmod a
    path that is simply not a file (the first version got this backwards;
    gauntlet)."""
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "plane.db").mkdir()  # a dir, not a db
    body = TestClient(create_app(tmp_path)).get("/api/summary").json()
    assert body["state"] == "absent"


def test_unreadable_db_is_distinct_from_absent(tmp_path):
    d = tmp_path / "state" / "plane"
    d.mkdir(parents=True)
    db = d / "plane.db"
    db.write_bytes(b"x")
    db.chmod(0)  # exists, cannot be opened
    try:
        body = TestClient(create_app(tmp_path)).get("/api/summary").json()
    finally:
        db.chmod(0o600)
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
    # Server-stamped semantics (gauntlet): the client renders facts, never
    # re-derives vocabulary.
    assert [x["activated"] for x in dispatch["tx"]] == [False, True]
    assert t["delivered"] is True
    assert t["terminal"] == "completed"
    assert "recipient_raw" not in dispatch            # §11: raw id stays home


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


# ---------------------------------------------------------------------------
# Gauntlet pins (8-reviewer round on the v1)
# ---------------------------------------------------------------------------

def _seed_one_sided(root, tagged_side):
    _full_capture(root)
    wi = "wi_" + "e" * 32
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": "f",
         "payload": {"work_item_id": wi, "title": "One-sided tag",
                     "created_by": "bot:f/mgr"}},
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "e" * 32, "sender": "bot:f/mgr",
                     "recipient": "bot:f/w1", "message_class": "task_request",
                     "body": "do it",
                     **({"work_item_id": wi} if tagged_side == "dispatch"
                        else {})}},
        {"event_type": "communication", "emitter": "t", "fleet": "f",
         "payload": {"msg_id": "msg_" + "f" * 32, "sender": "bot:f/w1",
                     "recipient": "bot:f/mgr", "message_class": "report",
                     "reply_to_msg_id": "msg_" + "e" * 32, "body": "done",
                     **({"work_item_id": wi} if tagged_side == "reply"
                        else {})}},
    ])


@pytest.mark.parametrize("tagged_side", ["dispatch", "reply"])
def test_one_sided_work_item_still_one_thread(tmp_path, tagged_side):
    _seed_one_sided(tmp_path, tagged_side)
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    threads = body["data"]["threads"]
    assert len(threads) == 1, f"{tagged_side}-tagged pair split the story"
    assert threads[0]["work_item_id"] == "wi_" + "e" * 32
    assert len(threads[0]["messages"]) == 2


def test_terminal_stamp_is_first_terminal_monotone(tmp_path):
    """The reducer's rule: a late terminal never rewrites — the channel must
    agree with TASK_STATUS_SQL (the client copy took the LAST terminal and
    forked live; gauntlet)."""
    _seed_conversation(tmp_path)
    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": "t", "fleet": "f",
         "payload": {"event": "superseded", "work_item_id": f"wi_{H}",
                     "assignment_id": f"asg_{H}"}}])
    body = TestClient(create_app(tmp_path)).get("/api/channel").json()
    t = [x for x in body["data"]["threads"]
         if x["work_item_id"] == f"wi_{H}"][0]
    assert t["terminal"] == "completed"  # first terminal wins, always


def test_body_words_strips_real_door_shapes():
    from claudlobby.plane.view import body_words
    d = ("[BOTCOMMAND] erlich | task | Review the thing carefully"
         " | repo:Artemis-xyz/huntress | priority:high | task:t-123-abcd")
    assert body_words(d) == "Review the thing carefully"
    r = ("[BOTREPORT] jian-yang | completed | Approve on #2073"
         " | progress:100 | pr:https://github.com/x/y/pull/1 | task:t-1-ffff")
    assert body_words(r) == "Approve on #2073"
    assert body_words("plain words, no framing") == "plain words, no framing"
    assert body_words("keep | this: colon | prose") is not None
    assert body_words(None) is None


def test_tasks_restriction_matches_unrestricted_derivation(tmp_path):
    """The IN-restriction is an efficiency append; output must be
    byte-identical to the unrestricted one-definition queries."""
    import sqlite3 as _sq

    from claudlobby.plane import view
    from claudlobby.plane.queries import ATTENTION_SQL as A, TASK_STATUS_SQL as T

    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path)).get("/api/tasks").json()
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    conn.row_factory = _sq.Row
    unrestricted = {r["assignment_id"]: r["status"]
                    for r in conn.execute(T)}
    unrestricted_att = {r[0] for r in conn.execute(
        A, (view._now_iso(),))}
    conn.close()
    for r in body["data"]["assignments"]:
        assert r["status"] == unrestricted[r["assignment_id"]]
        assert r["attention"] == (r["assignment_id"] in unrestricted_att)


def test_spool_count_excludes_quarantine_and_sidecars(tmp_path):
    _seed_conversation(tmp_path)
    spool = tmp_path / "state" / "plane" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "ev_a.json").write_text('{"spooled_at": "2026-01-01T00:00:00"}')
    q = spool / "quarantine"
    q.mkdir()
    (q / "ev_b.json").write_text("{}")
    (q / "ev_b.json.reason").write_text("poison")
    body = TestClient(create_app(tmp_path)).get("/api/summary").json()
    assert body["data"]["spool_files"] == 1  # pending only, doctor's number
    assert body["data"]["spool_oldest_at"] == "2026-01-01T00:00:00"


def test_summary_honors_plane_socket_override(tmp_path, monkeypatch):
    _seed_conversation(tmp_path)
    override = tmp_path / "elsewhere.sock"
    override.write_text("")  # present (a stale FILE — liveness must not lie)
    monkeypatch.setenv("PLANE_SOCKET", str(override))
    body = TestClient(create_app(tmp_path)).get("/api/summary").json()
    assert body["data"]["ingest_socket_present"] is True
    assert body["data"]["daemon_serving"] is False  # probe, not presence


def test_healthz_ok_is_one_envelope_with_summary(tmp_path):
    _seed_conversation(tmp_path)
    r = TestClient(create_app(tmp_path)).get("/healthz")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["schema_user_version"] == 3
    assert "counts" in data and "spool_files" in data
    assert "corrective" in data


def test_index_served_from_package_data(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/")
    assert r.status_code == 200
    assert "observable plane" in r.text
    assert r.headers.get("cache-control") == "no-store"
    # asset refs carry the cache-bust token (defeats a pinned ES module)
    assert "/app.js?v=" in r.text


def test_app_js_import_is_cache_busted(tmp_path):
    r = TestClient(create_app(tmp_path)).get("/app.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    assert "/panel-state.js?v=" in r.text  # intra-module import busts too


def test_stream_defaults_to_head_never_replays(tmp_path):
    """Gauntlet consensus (3 reviewers, measured): a cursor-less connect used
    to replay the ENTIRE ledger per viewer per reconnect."""
    _seed_conversation(tmp_path)
    payload = _first_sse_chunk(TestClient(create_app(tmp_path)),
                               "/api/stream?once=1")
    assert payload is None  # at head: a ping, never a replay


def test_stream_honors_last_event_id(tmp_path):
    _seed_conversation(tmp_path)
    client = TestClient(create_app(tmp_path))
    with client.stream("GET", "/api/stream?once=1",
                       headers={"Last-Event-ID": "1"}) as r:
        payload = None
        for line in r.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:])
                break
            if line.startswith(": ping"):
                break
    assert payload is not None
    assert payload["rows"][0]["ingest_seq"] == 2  # resumed AFTER the id


def test_plane_open_matches_its_own_port(monkeypatch, capsys, tmp_path):
    """Probed in review: the first-https match opened someone else's service
    the moment Tailscale Serve fronted a second app."""
    import shutil
    import types

    from claudlobby.commands.plane import cmd_plane_open

    stub = tmp_path / "tailscale"
    stub.write_text(
        "#!/bin/bash\n"
        "cat <<'OUT'\n"
        "https://other.tail.ts.net (tailnet only)\n"
        "|-- proxy http://127.0.0.1:3000\n"
        "\n"
        "https://mini.tail.ts.net (tailnet only)\n"
        "|-- proxy http://127.0.0.1:8899\n"
        "OUT\n")
    stub.chmod(0o755)
    monkeypatch.setattr(shutil, "which",
                        lambda name: str(stub) if name == "tailscale" else None)
    args = types.SimpleNamespace(port=8899, no_browser=True)
    assert cmd_plane_open(args) == 0
    out = capsys.readouterr().out.strip()
    assert out == "https://mini.tail.ts.net"
