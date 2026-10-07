"""Phase-4 view daemon battery (design walk 2026-08-28).

Pins the walk's rulings as behavior: strictly read-only structurally; the
panel-state envelope on every endpoint (absent/unreadable are typed states
with remediation — never a zero); story-first thread assembly (dispatch +
delivery + reports + closure as ONE thread, alias-first names, telegram
destinations resolved through channels.json); SSE cursor semantics.
"""

from __future__ import annotations

from tests.plane_setup import initialize_plane

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from tests.package_fixtures import source_package  # noqa: E402
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
    initialize_plane(root)
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
    return TestClient(create_app(tmp_path, package=source_package()))


# ---------------------------------------------------------------------------
# Ruling: strictly read-only, structurally
# ---------------------------------------------------------------------------

def test_no_write_routes_exist(tmp_path):
    app = create_app(tmp_path, package=source_package())
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
                                  "/api/tasks", "/api/identities",
                                  "/api/fleets", "/api/overview"])
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
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/summary").json()
    assert body["state"] == "absent"


def test_unreadable_db_is_distinct_from_absent(tmp_path):
    d = tmp_path / "state" / "plane"
    d.mkdir(parents=True)
    db = d / "plane.db"
    db.write_bytes(b"x")
    db.chmod(0)  # exists, cannot be opened
    try:
        body = TestClient(create_app(tmp_path, package=source_package())).get("/api/summary").json()
    finally:
        db.chmod(0o600)
    assert body["state"] == "unreadable"


def test_healthz_503_when_absent(client):
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["state"] == "absent"


def test_ok_with_empty_data_is_ok(tmp_path):
    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t",
                           "payload": {"event": "daemon_started"}}])
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
    assert body["state"] == "ok"
    assert body["data"]["threads"] == []  # legitimately idle — UI's word


# ---------------------------------------------------------------------------
# Ruling: story-first threads
# ---------------------------------------------------------------------------

def test_channel_threads_the_whole_conversation(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
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
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
    notice = [m for t in body["data"]["threads"] for m in t["messages"]
              if m["message_class"] == "notice"][0]
    assert notice["recipient_short"] == "Engineering group"


def test_unmapped_telegram_destination_is_generic_not_raw(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
    notice = [m for t in body["data"]["threads"] for m in t["messages"]
              if m["message_class"] == "notice"][0]
    assert notice["recipient_short"] == "Telegram"    # never "-100999"


# ---------------------------------------------------------------------------
# Tasks + identities
# ---------------------------------------------------------------------------

def test_tasks_carry_status_and_attention(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/tasks").json()
    rows = body["data"]["tasks"]
    assert rows[0]["state"] == "completed"
    assert rows[0]["task_id"] == f"wi_{H}"
    assert rows[0]["attention"] is False
    assert rows[0]["current_assignment"] is None
    assert rows[0]["assignment_history"][0]["assignee_short"] == "ramanujan"


def test_identities_are_alias_first(tmp_path):
    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/identities").json()
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
    payload = _first_sse_chunk(TestClient(create_app(tmp_path, package=source_package())),
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
    payload = _first_sse_chunk(TestClient(create_app(tmp_path, package=source_package())),
                               f"/api/stream?cursor={head}&once=1")
    assert payload is None  # nothing to replay — a ping, never stale rows


# ---------------------------------------------------------------------------
# Gauntlet pins (8-reviewer round on the v1)
# ---------------------------------------------------------------------------

def _seed_one_sided(root, tagged_side):
    _full_capture(root)
    wi = "wi_" + "e" * 32
    initialize_plane(root)
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
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
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
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/channel").json()
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
    """Lifecycle comes from the Task reducer; current-assignment attention
    remains the one SQL arm projection."""
    import sqlite3 as _sq

    from claudlobby.plane import view
    from claudlobby.plane.queries import ATTENTION_SQL as A
    from claudlobby.plane.queries import attention_params
    from claudlobby.task_state import read_tasks

    _seed_conversation(tmp_path)
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/tasks").json()
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    conn.row_factory = _sq.Row
    fleet_uid = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet' AND alias='f'").fetchone()[0]
    lifecycle = {task.task_id: task.state for task in read_tasks(conn, fleet_uid=fleet_uid).tasks}
    unrestricted_att = {r[0] for r in conn.execute(
        A, attention_params(view._now_iso()))}
    conn.close()
    for r in body["data"]["tasks"]:
        assert r["state"] == lifecycle[r["task_id"]]
        aid = r["current_assignment"]["assignment_id"] if r["current_assignment"] else None
        assert r["attention"] == (aid in unrestricted_att)


def test_spool_count_excludes_quarantine_and_sidecars(tmp_path):
    _seed_conversation(tmp_path)
    spool = tmp_path / "state" / "plane" / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "ev_a.json").write_text('{"spooled_at": "2026-01-01T00:00:00"}')
    q = spool / "quarantine"
    q.mkdir()
    (q / "ev_b.json").write_text("{}")
    (q / "ev_b.json.reason").write_text("poison")
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/summary").json()
    assert body["data"]["spool_files"] == 1  # pending only, doctor's number
    assert body["data"]["spool_oldest_at"] == "2026-01-01T00:00:00"


def test_summary_honors_plane_socket_override(tmp_path, monkeypatch):
    _seed_conversation(tmp_path)
    override = tmp_path / "elsewhere.sock"
    override.write_text("")  # present (a stale FILE — liveness must not lie)
    monkeypatch.setenv("PLANE_SOCKET", str(override))
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/summary").json()
    assert body["data"]["ingest_socket_present"] is True
    assert body["data"]["daemon_serving"] is False  # probe, not presence


def test_healthz_ok_is_one_envelope_with_summary(tmp_path):
    _seed_conversation(tmp_path)
    r = TestClient(create_app(tmp_path, package=source_package())).get("/healthz")
    assert r.status_code == 200
    data = r.json()["data"]
    from claudlobby.plane.migrations import SCHEMA_USER_VERSION
    assert data["schema_user_version"] == SCHEMA_USER_VERSION
    assert "counts" in data and "spool_files" in data
    assert "corrective" in data


def test_index_served_from_package_data(tmp_path):
    r = TestClient(create_app(tmp_path, package=source_package())).get("/")
    assert r.status_code == 200
    assert "observable plane" in r.text
    assert r.headers.get("cache-control") == "no-store"
    # asset refs carry the cache-bust token (defeats a pinned ES module)
    assert "/app.js?v=" in r.text
    assert "/workspace.css?v=" in r.text


def test_app_js_import_is_cache_busted(tmp_path):
    r = TestClient(create_app(tmp_path, package=source_package())).get("/app.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    assert "/panel-state.js?v=" in r.text  # intra-module import busts too
    assert "/api-client.js?v=" in r.text
    transport = TestClient(create_app(tmp_path, package=source_package())).get("/api-client.js")
    assert transport.status_code == 200
    assert transport.headers.get("cache-control") == "no-store"


def test_stream_defaults_to_head_never_replays(tmp_path):
    """Gauntlet consensus (3 reviewers, measured): a cursor-less connect used
    to replay the ENTIRE ledger per viewer per reconnect."""
    _seed_conversation(tmp_path)
    payload = _first_sse_chunk(TestClient(create_app(tmp_path, package=source_package())),
                               "/api/stream?once=1")
    assert payload is None  # at head: a ping, never a replay


def test_stream_honors_last_event_id(tmp_path):
    _seed_conversation(tmp_path)
    client = TestClient(create_app(tmp_path, package=source_package()))
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


# ---------------------------------------------------------------------------
# The fleet DIMENSION (U1/U4, #1467): two fleets on one host.
# ---------------------------------------------------------------------------

def _seed_twins(root: Path) -> None:
    """Two fleets, each with a manager and a worker BOTH named `one`/`mgr`
    (the #526 collision class), one assignment each, one human notice."""
    _full_capture(root)
    for fleet, h in (("engineering", "a"), ("data", "b")):
        mgr, worker = f"bot:{fleet}/mgr", f"bot:{fleet}/one"
        initialize_plane(root)
        emit_batch(root, [
            {"event_type": "work_item", "emitter": "t", "fleet": fleet,
             "payload": {"work_item_id": "wi_" + h * 32,
                         "title": f"work for {fleet}", "created_by": mgr}},
            {"event_type": "assignment", "emitter": "t", "fleet": fleet,
             "payload": {"assignment_id": "asg_" + h * 32,
                         "work_item_id": "wi_" + h * 32,
                         "assignee": worker, "assigned_by": mgr,
                         "dispatch_msg_id": "msg_" + h * 32}},
            {"event_type": "communication", "emitter": "t", "fleet": fleet,
             "payload": {"msg_id": "msg_" + h * 32, "sender": mgr,
                         "recipient": worker, "message_class": "task_request",
                         "command_type": "task", "body": f"go {fleet}"}},
        ])
    emit_batch(root, [{
        "event_type": "communication", "emitter": "t", "fleet": "data",
        "payload": {"msg_id": "msg_" + "c" * 32, "sender": "human:chris",
                    "recipient": "bot:data/mgr", "message_class": "chat",
                    "body": "hi data"}}])


def test_fleets_door_lists_registry_fleets_beyond_the_rail_window(tmp_path):
    """U1: the fleet list comes from the registry's fleet identities, NOT
    the roster rail's LIMIT-200 last-seen window — 300 newer identities
    push the quiet fleet out of the rail, and its tab must survive."""
    _seed_twins(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "communication", "emitter": "t", "fleet": "engineering",
        "payload": {"msg_id": "msg_" + f"{i:032x}",
                    "sender": f"bot:engineering/x{i}",
                    "recipient": "bot:engineering/mgr",
                    "message_class": "chat", "body": "noise"}}
        for i in range(300)])
    # a host-job sentinel is not a fleet
    emit_batch(tmp_path, [{
        "event_type": "metric_sample", "emitter": "probe", "fleet": "_host",
        "payload": {"subject_kind": "host", "subject": "h1",
                    "metric": "host.job_ran", "value": 1}}])
    client = TestClient(create_app(tmp_path, package=source_package()))
    rail = client.get("/api/identities").json()["data"]["identities"]
    rail_fleets = {r["alias"] for r in rail if r["kind"] == "fleet"}
    assert "data" not in rail_fleets           # the window dropped it (premise)
    fl = client.get("/api/fleets").json()
    assert fl["state"] == "ok"
    assert [f["alias"] for f in fl["data"]["fleets"]] == ["data", "engineering"]
    by = {f["alias"]: f for f in fl["data"]["fleets"]}
    assert by["data"]["bots"] == 2 and by["engineering"]["bots"] == 302
    assert fl["data"]["default"] == "engineering"   # its room moved last


def test_fleets_default_is_the_room_that_moved_last(tmp_path):
    """The first-visit tab: the fleet whose room (sent BY it or TO it)
    carries the newest message — data's human notice landed last."""
    _seed_twins(tmp_path)
    fl = TestClient(create_app(tmp_path, package=source_package())).get("/api/fleets").json()["data"]
    assert fl["default"] == "data"
    # silence everywhere: alphabetical, and an empty plane has no default
    empty = tmp_path / "empty"
    _full_capture(empty)
    initialize_plane(empty)
    emit_batch(empty, [{"event_type": "metric_sample", "emitter": "p",
                        "fleet": "zeta", "payload": {
                            "subject_kind": "host", "subject": "h",
                            "metric": "host.job_ran", "value": 1}},
                       {"event_type": "metric_sample", "emitter": "p",
                        "fleet": "alpha", "payload": {
                            "subject_kind": "host", "subject": "h",
                            "metric": "host.job_ran", "value": 1}}])
    fl = TestClient(create_app(empty, package=source_package())).get("/api/fleets").json()["data"]
    assert fl["default"] == "alpha"
    assert all(f["last_comm_at"] is None for f in fl["fleets"])


def test_tasks_and_identities_follow_the_fleet_and_qualify_twins(tmp_path):
    """The board scopes by work ownership, and the host read labels twins fleet/name
    through inventory's ONE rule."""
    _seed_twins(tmp_path)
    client = TestClient(create_app(tmp_path, package=source_package()))
    eng = client.get("/api/tasks?fleet=engineering").json()["data"]
    assert [a["title"] for a in eng["tasks"]] == ["work for engineering"]
    assert eng["tasks"][0]["current_assignment"]["assignee_short"] == "one"
    host = client.get("/api/tasks").json()["data"]["tasks"]
    assert sorted(a["current_assignment"]["assignee_short"] for a in host) == [
        "data/one", "engineering/one"]
    # a fleet the plane holds no identity for is a typed refusal naming the
    # fleets it does hold — never a healthy empty room (plane-lookup's rule)
    none = client.get("/api/tasks?fleet=nonexistent").json()
    assert none["state"] == "unknown" and "data" not in none
    assert "data, engineering" in none["remediation"]
    # a name built from LIKE metacharacters is simply not a fleet — it can
    # neither absorb another's bots nor pass as one
    assert client.get("/api/tasks?fleet=e_gineering").json()["state"] == "unknown"
    # an empty axis is the host-wide read on every route, not a refusal
    assert len(client.get("/api/tasks?fleet=").json()["data"]["tasks"]) == 2

    data = client.get("/api/identities?fleet=data").json()["data"]["identities"]
    aliases = {r["alias"] for r in data}
    assert aliases == {"data", "bot:data/mgr", "bot:data/one", "human:chris"}
    assert all(r["short"] in ("data", "mgr", "one", "chris") for r in data)
    rail = client.get("/api/identities").json()["data"]["identities"]
    shorts = {r["alias"]: r["short"] for r in rail}
    assert shorts["bot:data/one"] == "data/one"
    assert shorts["bot:engineering/one"] == "engineering/one"
    assert shorts["human:chris"] == "chris"       # never fleet-qualified


def test_index_ids_are_unique_and_the_room_panel_owns_its_id(tmp_path):
    """U4: the fleet room shared id="fleet" with the roster rail, so
    getElementById always returned the rail and the room never rendered
    into its own panel. Ids are unique; app.js targets the room's id."""
    import re
    from importlib.resources import files
    ui = files("claudlobby.plane").joinpath("ui")
    html = ui.joinpath("index.html").read_text()
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), sorted(
        i for i in ids if ids.count(i) > 1)
    assert "fleet-room" in ids and "fleet" in ids
    js = ui.joinpath("app.js").read_text()
    assert '$("fleet-room")' in js
    assert 'jget("/api/fleets")' in js          # the dimension's source
    assert "localStorage" in js and "try {" in js   # the remembered pick


# ---------------------------------------------------------------------------
# Identity keeps its fleet (U2) + the overview strip (U3), #1467
# ---------------------------------------------------------------------------

PAST, FUTURE = "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"


def _seed_cross_fleet(root: Path) -> None:
    """The room suite's cross-fleet conversation (eng lead -> data worker,
    the data -> eng report) plus one INTRA-fleet eng thread."""
    _full_capture(root)
    wi = "wi_" + "e" * 32
    initialize_plane(root)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": "engineering",
         "payload": {"work_item_id": wi, "title": "cross",
                     "created_by": "bot:engineering/lead"}},
        {"event_type": "communication", "emitter": "t", "fleet": "engineering",
         "payload": {"msg_id": "msg_" + "e" * 32,
                     "sender": "bot:engineering/lead",
                     "recipient": "bot:data/worker",
                     "message_class": "task_request", "work_item_id": wi,
                     "body": "the ask"}},
        {"event_type": "communication", "emitter": "t", "fleet": "data",
         "payload": {"msg_id": "msg_" + "f" * 32, "sender": "bot:data/worker",
                     "recipient": "bot:engineering/lead",
                     "message_class": "report", "work_item_id": wi,
                     "reply_to_msg_id": "msg_" + "e" * 32,
                     "body": "the answer"}},
        {"event_type": "communication", "emitter": "t", "fleet": "engineering",
         "payload": {"msg_id": "msg_" + "d" * 32,
                     "sender": "bot:engineering/lead",
                     "recipient": "bot:engineering/solo",
                     "message_class": "chat", "body": "intra"}},
    ])


def _threads(client: TestClient, fleet: str | None = None) -> dict:
    url = "/api/channel" + (f"?fleet={fleet}" if fleet else "")
    env = client.get(url).json()
    assert env["state"] == "ok", env
    return {t["key"]: t for t in env["data"]["threads"]}


def test_channel_stamps_fleets_and_marks_cross_fleet_threads(tmp_path):
    """U2a: every message carries sender_fleet / recipient_fleet (the room
    query's two facts — fleet_uid <-> the registry, 0004's virtual column)
    and `cross_fleet` when they differ; a cross-fleet thread is marked
    and renders `eng/lead -> data/worker` in EVERY room, while an intra-
    fleet thread keeps short names in its own room."""
    _seed_cross_fleet(tmp_path)
    client = TestClient(create_app(tmp_path, package=source_package()))
    for room in ("engineering", "data", None):
        x = _threads(client, room)["wi_" + "e" * 32]
        assert x["cross_fleet"] is True, room
        ask, answer = x["messages"]
        assert (ask["sender_fleet"], ask["recipient_fleet"],
                ask["cross_fleet"]) == ("engineering", "data", True)
        assert (f"{ask['sender_short']} -> {ask['recipient_short']}"
                == "engineering/lead -> data/worker"), room
        assert (answer["sender_short"], answer["recipient_short"]) == (
            "data/worker", "engineering/lead"), room
        assert "fleet_uid" not in ask      # identifiers stay off the story
    intra = _threads(client, "engineering")["chain:msg_" + "d" * 32]
    assert intra["cross_fleet"] is False
    m = intra["messages"][0]
    assert (m["sender_fleet"], m["recipient_fleet"]) == ("engineering",
                                                          "engineering")
    assert (m["sender_short"], m["recipient_short"]) == ("lead", "solo")
    # the host-wide read on a two-fleet host qualifies EVERY bot, the
    # intra-fleet pair included: a bare name among qualified ones could
    # not say whether it is unique or simply un-fleeted (U, #1467)
    m = _threads(client)["chain:msg_" + "d" * 32]["messages"][0]
    assert (m["sender_short"], m["recipient_short"]) == ("engineering/lead",
                                                          "engineering/solo")
    # the data room's cross-fleet thread must not show only its own half
    assert len(_threads(client, "data")["wi_" + "e" * 32]["messages"]) == 2


def _seed_twins_with_deadlines(root: Path) -> None:
    """`_seed_twins`, with a PAST deadline on engineering's assignment (an
    overdue, attention-class row) and a future one on data's, an id'd
    source_ref on engineering's (only an id'd dispatch can orphan), a
    report in data's room, and a heartbeat per twin."""
    _full_capture(root)
    for fleet, h, due, state in (("engineering", "a", PAST, "BUSY"),
                                 ("data", "b", FUTURE, "IDLE")):
        mgr, worker = f"bot:{fleet}/mgr", f"bot:{fleet}/one"
        asg = {"event_type": "assignment", "emitter": "t", "fleet": fleet,
               "payload": {"assignment_id": "asg_" + h * 32,
                           "work_item_id": "wi_" + h * 32,
                           "assignee": worker, "assigned_by": mgr,
                           "dispatch_msg_id": "msg_" + h * 32,
                           "expected_by": due}}
        if fleet == "engineering":
            asg["source_ref"] = "dispatch-log:t-1-aaaa"
        initialize_plane(root)
        emit_batch(root, [
            {"event_type": "work_item", "emitter": "t", "fleet": fleet,
             "payload": {"work_item_id": "wi_" + h * 32,
                         "title": f"work for {fleet}", "created_by": mgr}},
            asg,
            {"event_type": "communication", "emitter": "t", "fleet": fleet,
             "payload": {"msg_id": "msg_" + h * 32, "sender": mgr,
                         "recipient": worker, "message_class": "task_request",
                         "command_type": "task", "body": f"go {fleet}"}},
            {"event_type": "metric_sample", "emitter": "keepalive",
             "fleet": fleet,
             "payload": {"subject_kind": "bot_instance", "subject": worker,
                         "metric": "bot.heartbeat",
                         "value": {"state": state, "marker_age_s": 3}}}])
    emit_batch(root, [{
        "event_type": "communication", "emitter": "t", "fleet": "data",
        "payload": {"msg_id": "msg_" + "c" * 32, "sender": "bot:data/one",
                    "recipient": "bot:data/mgr", "message_class": "report",
                    "body": "done data"}}])


def test_all_tab_never_collapses_twins_in_channel_attention_or_search(tmp_path):
    """U2b/c — the grid suite's twin pin, extended: same-named bots on two
    fleets are two cards in the attention queue, two threads in the
    channel and two senders in search under `all`, every one labeled
    `fleet/name` through inventory's ONE rule; in its own room a twin
    stays bare, and twins are NOT cross-fleet (each thread is intra)."""
    _seed_twins_with_deadlines(tmp_path)
    # data's row needs attention too: a dispatch whose only transmission
    # never activated (the trouble arm, not the deadline arm)
    emit_batch(tmp_path, [{
        "event_type": "transmission", "emitter": "t", "fleet": "data",
        "payload": {"msg_id": "msg_" + "b" * 32, "attempt_no": 1,
                    "carrier": "tmux", "destination": "one",
                    "state": "failed"}}])
    client = TestClient(create_app(tmp_path, package=source_package()))
    host = client.get("/api/tasks").json()["data"]
    attn = sorted(a["current_assignment"]["assignee_short"] for a in host["tasks"]
                  if a["attention"])
    assert attn == ["data/one", "engineering/one"]     # two cards, never one
    assert host["attention_count"] == 2
    threads = _threads(client)
    who = sorted(f"{m['sender_short']} -> {m['recipient_short']}"
                 for t in threads.values() for m in t["messages"]
                 if m["message_class"] == "task_request")
    assert who == ["data/mgr -> data/one", "engineering/mgr -> engineering/one"]
    assert all(t["cross_fleet"] is False for t in threads.values())
    eng = _threads(client, "engineering")
    assert {m["sender_short"] for t in eng.values() for m in t["messages"]} \
        == {"mgr"}
    hits = client.get("/api/search?q=go").json()["data"]["results"]
    assert sorted(h["sender_short"] for h in hits) == ["data/mgr",
                                                       "engineering/mgr"]
    room = client.get("/api/search?q=go&fleet=data").json()["data"]["results"]
    assert [h["sender_short"] for h in room] == ["mgr"]
    none = client.get("/api/channel?fleet=nonexistent").json()
    assert none["state"] == "unknown" and "nonexistent" in none["remediation"]


class _TwinSampler:
    available = True

    def __init__(self, panes=None):
        self._panes = panes if panes is not None else [
            {"fleet": "engineering", "bot": "one", "status": "up"},
            {"fleet": "data", "bot": "one", "status": "up"},
            {"fleet": "data", "bot": "two", "status": "down"}]

    def snapshot(self):
        return {"panes": list(self._panes), "sampler_running": True}

    def start(self):
        pass

    async def stop(self):
        pass


def test_overview_is_one_row_per_fleet_plus_the_host(tmp_path):
    """U3: every figure on a fleet card is a plane fact through the door
    that defines it — presence scoped like the panel, the open set through
    OPEN_ASSIGNMENTS_AT_SQL, attention/overdue through ATTENTION_SQL with
    the fleet's assignees appended, reports on the room axis, capture via
    the ONE rule — and a figure whose source is missing is None with a
    reason, never a zero (orphan-ness needs the bot's directory)."""
    _seed_twins_with_deadlines(tmp_path)
    client = TestClient(create_app(tmp_path, sampler=_TwinSampler(), package=source_package()))
    ov = client.get("/api/overview").json()
    assert ov["state"] == "ok", ov
    rows = {r["alias"]: r for r in ov["data"]["fleets"]}
    assert sorted(rows) == ["data", "engineering"]
    e, d = rows["engineering"], rows["data"]
    assert (e["bots"], d["bots"]) == (2, 2)
    assert e["presence"]["counts"]["working"] == 1
    assert e["presence"]["counts"]["down"] == 0          # data's dead pane
    assert d["presence"]["counts"]["idle"] == 1          # is not eng's
    assert d["presence"]["counts"]["down"] == 1
    assert e["presence"]["live_poll"] == "ok"
    assert (e["open"], e["attention"], e["overdue"]) == (1, 1, 1)
    assert (d["open"], d["attention"], d["overdue"]) == (1, 0, 0)
    assert e["orphaned"] is None
    assert "no bot directories" in e["orphaned_reason"]
    assert d["newest_report_at"] and d["reports_24h"] == 1
    assert e["newest_report_at"] is None and e["reports_24h"] == 0
    assert e["capture"] == "full" and e["last_activity_at"]
    assert ov["data"]["capture_config"] == "ok"
    h = ov["data"]["host"]
    assert h["daemon_serving"] is False
    assert (h["spool_files"], h["spool_state"]) == (0, "ok")
    assert isinstance(h["ingest_lag_s"], float) and h["rows"] > 0
    assert h["last_ingest_at"] == ov["provenance"]["last_ingest_at"]

    # the orphan split once the bots' directories exist: engineering's
    # id'd overdue dispatch predates its bot's `.spawn` (a restart since)
    # -> orphaned; data has no `.spawn` -> the matcher's "not an orphan"
    for fleet in ("engineering", "data"):
        bd = tmp_path / "local" / fleet / "runtime" / "bots" / "one"
        bd.mkdir(parents=True)
        (bd / "bot.conf").write_text('export BOT_ID="one"\n')
    (tmp_path / "local" / "engineering" / "runtime" / "bots" / "one"
     / "data").mkdir()
    spawn = (tmp_path / "local" / "engineering" / "runtime" / "bots" / "one"
             / "data" / ".spawn")
    spawn.write_text("")
    import os
    import time
    later = time.time() + 5          # the restart landed AFTER the dispatch
    os.utime(spawn, (later, later))
    rows = {r["alias"]: r
            for r in client.get("/api/overview").json()["data"]["fleets"]}
    assert (rows["engineering"]["orphaned"],
            rows["engineering"]["orphaned_reason"]) == (1, None)
    assert (rows["data"]["orphaned"], rows["data"]["orphaned_reason"]) == (0, None)


def test_overview_discloses_a_missing_live_poll_and_a_malformed_policy(tmp_path):
    """The strip never fakes the half it lacks: with no sampler the
    verdicts come from the record alone and `live_poll` says so; a
    malformed capture.json is disclosed, the modes shown being defaults."""
    _seed_twins_with_deadlines(tmp_path)

    class _NoSampler(_TwinSampler):
        available = False

    client = TestClient(create_app(tmp_path, sampler=_NoSampler(), package=source_package()))
    rows = {r["alias"]: r
            for r in client.get("/api/overview").json()["data"]["fleets"]}
    assert rows["engineering"]["presence"]["live_poll"] == "unavailable"
    assert rows["engineering"]["presence"]["counts"]["working"] == 1
    assert rows["data"]["presence"]["counts"]["down"] == 0   # no live poll
    (tmp_path / "state" / "plane" / "capture.json").write_text("{nope")
    ov = client.get("/api/overview").json()["data"]
    assert ov["capture_config"] == "malformed"
    # The shipped default since 2026-09-20 is `full`, and this line asserts the
    # DISPLAY falls back to it while `capture_config: malformed` discloses that
    # the shown modes are defaults, not policy. (The recorder itself writes
    # nothing at all under a malformed file — CaptureConfigError — so no body is
    # stored on the strength of this display.)
    assert {r["capture"] for r in ov["fleets"]} == {"full"}


def test_ui_carries_the_overview_strip_and_the_cross_fleet_mark():
    """Structural: the page owns the strip's panel, app.js fetches the
    overview door and routes card clicks through the ONE pick path the
    tabs use, and the mark + the strip are styled."""
    from importlib.resources import files
    ui = files("claudlobby.plane").joinpath("ui")
    html = ui.joinpath("index.html").read_text()
    assert 'id="overview"' in html
    js = ui.joinpath("app.js").read_text()
    assert 'jget("/api/overview")' in js and "function renderOverview" in js
    assert js.count("function pickFleet") == 1
    assert "pickFleet(b.dataset.fleet)" in js       # the tab row
    assert "pickFleet(c.dataset.fleet)" in js       # the strip's cards
    assert "t.cross_fleet" in js and "xfleet" in js
    css = ui.joinpath("style.css").read_text()
    assert ".tag.xfleet" in css and ".ov-card" in css


# --- chunk L (#1479): what the operator reads ---------------------------------

_UNSET = object()          # `title=None` MEANS wordless — hence a sentinel


def _dispatch(root: Path, h: str, *, expected_by: str | None,
              tx_state: str | None, fleet: str = "f",
              worker: str | None = None, mgr: str | None = None,
              at: str | None = None, title=_UNSET) -> str:
    """One dispatched assignment: work item + assignment + task_request, and
    optionally one transmission row in *tx_state* (None = no transmission).
    Returns the assignment id.

    `title=None` emits an orphan assignment without a work item; the task
    reducer exposes that as an issue, not a Task card. `at` stamps the dispatch
    instant (default: whatever ingest stamps).

    *h* seeds every id, so ids stay distinct per row; `(h * 32)[:32]` keeps
    a one-character *h* reading `asg_1111…` as it always did and lets a
    longer tag repeat to the same width."""
    stem = (h * 32)[:32]
    mgr = mgr or f"bot:{fleet}/erlich"
    worker = worker or f"bot:{fleet}/ramanujan"
    if title is _UNSET:
        title = f"task {h}"
    asg = {"assignment_id": "asg_" + stem, "work_item_id": "wi_" + stem,
           "assignee": worker, "assigned_by": mgr,
           "dispatch_msg_id": "msg_" + stem}
    if expected_by:
        asg["expected_by"] = expected_by
    assignment = {"event_type": "assignment", "emitter": "t", "fleet": fleet,
                  "payload": asg}
    if at:
        assignment["occurred_at"] = at
    initialize_plane(root)
    emit_batch(root, ([] if title is None else [
        {"event_type": "work_item", "emitter": "t", "fleet": fleet,
         "payload": {"work_item_id": "wi_" + stem, "title": title,
                     "created_by": mgr}}]) + [
        assignment,
        {"event_type": "communication", "emitter": "t", "fleet": fleet,
         "payload": {"msg_id": "msg_" + stem, "sender": mgr,
                     "recipient": worker, "message_class": "task_request",
                     "command_type": "task", "body": title or f"go {h}"}}])
    if tx_state:
        emit_batch(root, [{
            "event_type": "transmission", "emitter": "t", "fleet": fleet,
            "payload": {"msg_id": "msg_" + stem, "attempt_no": 1,
                        "carrier": "tmux",
                        "destination": worker.rsplit("/", 1)[-1],
                        "state": tx_state}}])
    return "asg_" + stem


def test_tasks_payload_stamps_the_terminal_instant_and_the_attention_reason(tmp_path):
    """A finished task carries the reducer's first terminal event so its card
    reads "completed 1m ago" instead of a deadline it no longer has; an attention
    row carries WHY (`attention_reason`: never_activated / overdue, primary
    first) and since when; an open, delivered, not-yet-due row carries
    neither."""
    _seed_conversation(tmp_path)                                    # asg_{H}: completed
    _dispatch(tmp_path, "1", expected_by="2020-01-01T00:00:00+00:00",
              tx_state="carrier_queued")                            # queued, never activated, overdue
    _dispatch(tmp_path, "2", expected_by="2099-01-01T00:00:00+00:00",
              tx_state="pane_submitted")                            # delivered, not due
    _dispatch(tmp_path, "3", expected_by="2020-01-01T00:00:00+00:00",
              tx_state="pane_submitted")                            # delivered, overdue
    rows = _tasks(tmp_path)
    done = rows[f"asg_{H}"]
    assert done["state"] == "completed" and done["terminal_event"]["occurred_at"]
    assert done["current_assignment"] is None
    assert done["attention"] is False and done["attention_reason"] == []
    assert done["attention_since"] is None
    queued = rows["asg_" + "1" * 32]
    assert queued["attention"] is True and queued["terminal_event"] is None
    assert queued["attention_reason"] == ["never_activated", "overdue"]
    assert queued["attention_since"] == queued["current_assignment"]["occurred_at"]
    fine = rows["asg_" + "2" * 32]
    assert fine["attention"] is False and fine["attention_reason"] == []
    assert fine["terminal_event"] is None
    assert fine["current_assignment"]["expected_by"].startswith("2099")
    late = rows["asg_" + "3" * 32]
    assert late["attention"] is True and late["attention_reason"] == ["overdue"]
    assert late["attention_since"] == late["current_assignment"]["expected_by"]


def test_ui_reads_in_the_operators_language():
    """Structural (chunk L, #1479 + its fold): the page's RULES, not its
    identifiers. The header and the machinery rail read the overview envelope
    and are BLANKED when its source fails (§16); the page keeps no copy of a
    server vocabulary and re-derives no magnitude of its own; the clamp height
    is derived from the one constant that decides a body is long."""
    from importlib.resources import files
    ui = files("claudlobby.plane").joinpath("ui")
    html = ui.joinpath("index.html").read_text()
    assert 'id="fleet-totals"' in html and 'id="host-facts"' in html
    assert 'id="age"' not in html and 'id="total"' not in html and 'id="spool"' not in html
    js = ui.joinpath("app.js").read_text()
    # one door for both: the header's totals and the rail's host line come
    # from the overview envelope, and both clear on a source failure
    assert "renderHeader(ov)" in js and "renderHostFacts(ov)" in js
    assert "renderHeader(null)" in js and "renderHostFacts(null)" in js
    assert "hostFacts" not in js              # the dead module state is gone
    # no copied vocabulary, no client-side re-derivation (folds F4 + F7)
    assert "TERMINAL_STATUSES" not in js
    assert "r.terminal_event?.occurred_at" in js
    assert 'dueLabel(r.expected_by).replace' not in js
    assert "attention_since" in js and "never delivered" in js
    assert "check the worker" in js and "send failed" in js
    # the clamp: ONE number, read by the stylesheet; the toggle finds its
    # body by structure and remembers what the operator opened
    assert "--clamp-lines" in js and "openBodies" in js
    assert "previousElementSibling" not in js
    css = ui.joinpath("style.css").read_text()
    assert ".msg .body.clamp" in css and ".card .why" in css
    assert "var(--clamp-lines)" in css and "11.5em" not in css
    assert "var(--panel, " not in css         # a fallback that was the wrong color


# --- chunk L, the fold: one row, the queue's own arms, totals with caveats --

def _tasks(root: Path) -> dict:
    return {assignment["assignment_id"]: task
            for task in TestClient(create_app(root, package=source_package()))
            .get("/api/tasks").json()["data"]["tasks"]
            for assignment in task["assignment_history"]}


def test_status_and_its_instant_come_from_the_same_terminal_row(tmp_path):
    """Fold F1 (#1479): an assignment with TWO terminal events must not show
    one event's name over the other's instant. `completed` is ingested FIRST
    and stamped LATER; `superseded` lands after it carrying an EARLIER
    occurred_at. Ledger order is authoritative (queries.py:5-8), so the status
    is `completed` and the instant is completed's — the first build read the
    instant from a separate MIN(occurred_at) and printed superseded's."""
    _dispatch(tmp_path, "4", expected_by=None, tx_state="pane_submitted")
    aid, wid = "asg_" + "4" * 32, "wi_" + "4" * 32
    later, earlier = "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"
    for event, ts in (("completed", later), ("superseded", earlier)):
        emit_batch(tmp_path, [{
            "event_type": "task", "emitter": "t", "fleet": "f",
            "occurred_at": ts,
            "payload": {"event": event, "work_item_id": wid,
                        "assignment_id": aid}}])
    row = _tasks(tmp_path)[aid]
    assert row["state"] == "completed"
    assert row["terminal_event"]["occurred_at"] == later


def test_every_attention_arm_is_stamped_by_the_query_that_selected_it(tmp_path):
    """Fold F2 (#1479): the reason a card is in the queue comes from
    ATTENTION_ARMS_SQL — the query that selected it — one column per arm,
    never a second derivation beside it. A failed send, a queued-and-never-
    delivered send and a passed deadline are three different remedies; both
    send arms date from the dispatch, a bare deadline from the deadline."""
    _dispatch(tmp_path, "5", expected_by=FUTURE, tx_state="failed")
    _dispatch(tmp_path, "6", expected_by=FUTURE, tx_state="carrier_queued")
    _dispatch(tmp_path, "7", expected_by=PAST, tx_state=None)
    _dispatch(tmp_path, "8", expected_by=PAST, tx_state="failed")
    rows = _tasks(tmp_path)
    failed = rows["asg_" + "5" * 32]
    assert failed["attention_reason"] == ["send_failed"]
    assert failed["attention_since"] == failed["current_assignment"]["occurred_at"]
    queued = rows["asg_" + "6" * 32]
    assert queued["attention_reason"] == ["never_activated"]
    assert queued["attention_since"] == queued["current_assignment"]["occurred_at"]
    late = rows["asg_" + "7" * 32]
    assert late["attention_reason"] == ["overdue"]     # no transmission = silence
    assert late["attention_since"] == late["current_assignment"]["expected_by"]
    both = rows["asg_" + "8" * 32]
    assert both["attention_reason"] == ["send_failed", "overdue"]
    assert both["attention_since"] == both["current_assignment"]["occurred_at"]
    # the arms EXHAUST attention, which is what makes the card's bare
    # "needs you" fallback unreachable
    assert all(r["attention_reason"] for r in rows.values() if r["attention"])
    assert not any(r["attention_reason"] for r in rows.values()
                   if not r["attention"])


def _canonical_delivery(root: Path, h: str, *, tx_state: str | None,
                        fleet: str = "f", recipient: str | None = None) -> str:
    """The `assignment deliver` shape: the assignment row carries NO
    dispatch_msg_id; the task communication links task + assignment."""
    stem = (h * 32)[:32]
    mgr, worker = f"bot:{fleet}/erlich", f"bot:{fleet}/ramanujan"
    initialize_plane(root)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": fleet,
         "payload": {"work_item_id": "wi_" + stem, "title": f"task {h}",
                     "created_by": mgr}},
        {"event_type": "assignment", "emitter": "t", "fleet": fleet,
         "payload": {"assignment_id": "asg_" + stem, "work_item_id": "wi_" + stem,
                     "assignee": worker, "assigned_by": mgr,
                     "expected_by": FUTURE}},
        {"event_type": "communication", "emitter": "t", "fleet": fleet,
         "payload": {"msg_id": "msg_" + stem, "sender": mgr,
                     "recipient": recipient or worker, "message_class": "task_request",
                     "command_type": "task", "work_item_id": "wi_" + stem,
                     "assignment_id": "asg_" + stem, "body": f"go {h}"}}])
    if tx_state:
        emit_batch(root, [{
            "event_type": "transmission", "emitter": "t", "fleet": fleet,
            "payload": {"msg_id": "msg_" + stem, "attempt_no": 1, "carrier": "tmux",
                        "destination": "ramanujan", "state": tx_state}}])
    return "asg_" + stem


def test_board_joins_canonical_task_delivery_not_only_dispatch_msg_id(tmp_path):
    """S5a-05: canonical delivery never writes assignments.dispatch_msg_id, so
    its failed send must still raise send_failed and fill the delivery field;
    a queued one stays never_activated and a submitted one stays quiet. A task
    communication addressed to someone else is not this assignment's delivery."""
    failed = _canonical_delivery(tmp_path, "c1", tx_state="failed")
    queued = _canonical_delivery(tmp_path, "c2", tx_state="carrier_queued")
    fine = _canonical_delivery(tmp_path, "c3", tx_state="pane_submitted")
    stray = _canonical_delivery(tmp_path, "c4", tx_state="failed",
                                recipient="bot:f/someone-else")
    rows = _tasks(tmp_path)
    assert rows[failed]["attention_reason"] == ["send_failed"]
    assert rows[failed]["delivery"]["message_id"] == "msg_" + ("c1" * 16)
    assert rows[failed]["current_assignment"]["dispatch_message_id"] == "msg_" + ("c1" * 16)
    assert rows[queued]["attention_reason"] == ["never_activated"]
    assert rows[fine]["attention"] is False
    assert rows[fine]["delivery"]["integrity"] == "unconfirmed"
    assert rows[stray]["attention"] is False and rows[stray]["delivery"] is None


def test_overview_attention_is_the_boards_owning_fleet_count(tmp_path):
    """S5a-06: the strip and the board count the same cards. Work fleet `a`
    owns but assigned a bot of fleet `b` is `a`'s attention on both surfaces;
    the assignee axis stays `open`, disclosed as such."""
    _dispatch(tmp_path, "a1", fleet="a", worker="bot:b/ramanujan",
              expected_by=PAST, tx_state=None)
    _dispatch(tmp_path, "b1", fleet="b", expected_by=FUTURE, tx_state="pane_submitted")
    client = TestClient(create_app(tmp_path, package=source_package()))
    fleets = {r["alias"]: r for r in client.get("/api/overview").json()["data"]["fleets"]}
    for alias in ("a", "b"):
        board = client.get(f"/api/tasks?fleet={alias}").json()["data"]
        assert fleets[alias]["attention"] == board["attention_count"]
        assert fleets[alias]["attention_scope"] == "owning_fleet_tasks"
    assert (fleets["a"]["attention"], fleets["b"]["attention"]) == (1, 0)
    assert fleets["b"]["open_scope"] == "assigned_to_fleet_bots"


def test_the_arms_query_selects_exactly_the_attention_query(tmp_path):
    """Both are built from `queries.ATTENTION_ARMS`, so the queue and the
    reasons cannot disagree about who is in it — a new arm reaches both or
    neither."""
    import sqlite3 as _sq

    from claudlobby.plane import view
    from claudlobby.plane.queries import (ATTENTION_ARMS_SQL, ATTENTION_SQL,
                                          attention_arms_params, attention_params)

    _seed_conversation(tmp_path)                       # a closed one
    _dispatch(tmp_path, "5", expected_by=FUTURE, tx_state="failed")
    _dispatch(tmp_path, "6", expected_by=FUTURE, tx_state="carrier_queued")
    _dispatch(tmp_path, "7", expected_by=PAST, tx_state=None)
    _dispatch(tmp_path, "9", expected_by=FUTURE, tx_state="pane_submitted")
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    now = view._now_iso()
    plain = {r[0] for r in conn.execute(ATTENTION_SQL, attention_params(now))}
    armed = {r[0] for r in conn.execute(ATTENTION_ARMS_SQL, attention_arms_params(now))}
    conn.close()
    assert plain == armed and len(plain) == 3


def test_the_header_totals_ship_with_their_disclosures(tmp_path):
    """Fold F5 (#1479): the totals are the SERVER's, summed once, and carry
    what the page dropped when it summed the cards itself — the unconfirmed
    share of the bot count and the worst live-poll state on the host."""
    _seed_twins_with_deadlines(tmp_path)
    client = TestClient(create_app(tmp_path, sampler=_TwinSampler(), package=source_package()))
    data = client.get("/api/overview").json()["data"]
    cards, t = data["fleets"], data["totals"]
    assert t["fleets"] == 2
    assert t["bots"] == sum(c["bots"] for c in cards) == 4
    assert t["provisional"] == sum(c["provisional"] for c in cards) > 0
    assert t["working"] == 1 and t["attention"] == 1 and t["overdue"] == 1
    assert t["live_poll"] == "ok"

    class _NoSampler(_TwinSampler):
        available = False

    degraded = TestClient(create_app(tmp_path, sampler=_NoSampler(), package=source_package())) \
        .get("/api/overview").json()["data"]["totals"]
    assert degraded["live_poll"] == "unavailable"   # never swallowed by a sum


def test_totals_of_a_plane_with_no_fleet_are_zero_fleets_not_four_zeros(tmp_path):
    """A host that has recorded nothing under a fleet says so — the header
    reads "no fleet recorded" rather than four confident zeros."""
    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    emit_batch(tmp_path, [{"event_type": "system", "emitter": "t",
                           "payload": {"event": "daemon_started"}}])
    body = TestClient(create_app(tmp_path, package=source_package())).get("/api/overview").json()
    assert body["state"] == "ok"
    assert body["data"]["fleets"] == [] and body["data"]["totals"]["fleets"] == 0


# --- one canonical Task card, with lifecycle separate from delivery --------

def test_board_keeps_queued_and_reassigned_work_under_its_owning_fleet(tmp_path):
    from claudlobby.task_state import TASK_EMITTER

    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    queued = "wi_" + "1" * 32
    moved = "wi_" + "2" * 32
    first, second = "asg_" + "1" * 32, "asg_" + "2" * 32
    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"work_item_id": queued, "title": "Unassigned intake",
                     "created_by": "bot:f/mgr"}},
        {"event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"work_item_id": moved, "title": "Move this task",
                     "created_by": "bot:f/mgr"}},
        {"event_type": "assignment", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"assignment_id": first, "work_item_id": moved,
                     "assignee": "bot:g/worker", "assigned_by": "bot:f/mgr"}},
        {"event_type": "task", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"work_item_id": queued, "event": "escalated",
                     "actor": "bot:f/mgr", "question": "Which priority?", "by": "mgr"}},
        {"event_type": "communication", "emitter": "t", "fleet": "g",
         "payload": {"msg_id": "msg_" + "9" * 32, "sender": "bot:g/worker",
                     "recipient": "bot:g/mgr", "message_class": "chat",
                     "body": "fleet g is recorded"}},
    ], require_commit=True)
    client = TestClient(create_app(tmp_path, package=source_package()))
    room = client.get("/api/tasks?fleet=f").json()["data"]
    assert {r["task_id"] for r in room["tasks"]} == {queued, moved}
    intake = next(r for r in room["tasks"] if r["task_id"] == queued)
    assert intake["state"] == "queued" and intake["current_assignment"] is None
    assert intake["attention"] is True and intake["attention_reason"] == ["escalated"]
    assert intake["attention_question"] == "Which priority?"
    routed = next(r for r in room["tasks"] if r["task_id"] == moved)
    assert routed["state"] == "assigned"
    assert routed["current_assignment"]["assignment_id"] == first
    assert routed["current_assignment"]["assignee_alias"] == "bot:g/worker"
    other = client.get("/api/tasks?fleet=g").json()
    assert other["state"] == "ok"
    assert moved not in {r["task_id"] for r in other["data"]["tasks"]}

    emit_batch(tmp_path, [
        {"event_type": "task", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"work_item_id": moved, "assignment_id": first,
                     "event": "returned_blocked", "actor": "bot:g/worker"}},
        {"event_type": "assignment", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"assignment_id": second, "work_item_id": moved,
                     "assignee": "bot:f/worker", "assigned_by": "bot:f/mgr"}},
    ], require_commit=True)
    card = next(r for r in client.get("/api/tasks?fleet=f").json()["data"]["tasks"]
                if r["task_id"] == moved)
    assert card["state"] == "assigned" and card["assignment_count"] == 2
    assert card["current_assignment"]["assignment_id"] == second
    assert [a["assignment_id"] for a in card["assignment_history"]] == [first, second]
    emit_batch(tmp_path, [{"event_type": "task", "emitter": TASK_EMITTER, "fleet": "f",
                          "payload": {"work_item_id": moved, "assignment_id": second,
                                      "event": "completed", "actor": "bot:f/worker"}}], require_commit=True)
    closed = next(r for r in client.get("/api/tasks?fleet=f").json()["data"]["tasks"]
                  if r["task_id"] == moved)
    assert closed["state"] == "completed" and closed["current_assignment"] is None
    assert closed["terminal_event"]["event"] == "completed"
    assert closed["attention"] is False and closed["assignment_count"] == 2

    # The capped latest slice cannot certify that older intake needs nothing.
    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "f",
         "payload": {"work_item_id": f"wi_{i:032x}", "title": f"Later {i}",
                     "created_by": "bot:f/mgr"}}
        for i in range(201)], require_commit=True)
    capped = client.get("/api/tasks?fleet=f").json()["data"]
    assert len(capped["tasks"]) == 200 and capped["truncated"] is True
    assert queued not in {card["task_id"] for card in capped["tasks"]}
    assert capped["attention_count"] == 0  # only the visible slice is counted


def test_board_discloses_unknown_task_history_without_an_all_clear(tmp_path):
    from claudlobby.task_state import TASK_EMITTER

    _full_capture(tmp_path)
    initialize_plane(tmp_path)
    task_id = "wi_" + "9" * 32
    ambiguous = "wi_" + "8" * 32
    emit_batch(tmp_path, [{"event_type": "work_item", "emitter": "claudlobby.tasks.v999",
                          "fleet": "f", "payload": {"work_item_id": task_id,
                                                     "title": "Unknown producer",
                                                     "created_by": "bot:f/mgr"}},
                         {"event_type": "work_item", "emitter": TASK_EMITTER, "fleet": "f",
                          "payload": {"work_item_id": ambiguous, "title": "Ambiguous workers",
                                      "created_by": "bot:f/mgr"}},
                         *({"event_type": "assignment", "emitter": TASK_EMITTER, "fleet": "f",
                            "payload": {"assignment_id": "asg_" + stem * 32,
                                        "work_item_id": ambiguous,
                                        "assignee": "bot:f/" + bot,
                                        "assigned_by": "bot:f/mgr"}}
                           for stem, bot in (("8", "one"), ("7", "two")))], require_commit=True)
    data = TestClient(create_app(tmp_path, package=source_package())).get("/api/tasks").json()["data"]
    cards = {card["task_id"]: card for card in data["tasks"]}
    assert cards[task_id]["state"] is None
    assert cards[task_id]["resolved"] is False and cards[task_id]["attention"] is True
    assert "unknown_task_producer" in {issue["code"] for issue in cards[task_id]["issues"]}
    assert cards[ambiguous]["state"] is None and cards[ambiguous]["current_assignment"] is None
    assert "multiple_current_assignments" in {issue["code"] for issue in cards[ambiguous]["issues"]}
    assert data["issue_count"] >= 2 and data["attention_count"] == 2


def test_a_rail_row_carries_the_fleet_the_server_says_it_belongs_to(tmp_path):
    """Fold F5: WHICH fleet a roster row belongs to is stamped by the API
    (`inventory.fleet_of`, the one Python spelling), never parsed by the
    page — which had a third spelling that split on the FIRST `/` where
    `fleet_of` takes the LAST. A fleet identity is its own fleet; a human
    belongs to none, being a participant of every room."""
    _seed_twins(tmp_path)
    rail = {r["alias"]: r for r in TestClient(create_app(tmp_path, package=source_package()))
            .get("/api/identities").json()["data"]["identities"]}
    assert rail["bot:data/one"]["fleet"] == "data"
    assert rail["bot:engineering/one"]["fleet"] == "engineering"
    assert rail["data"]["kind"] == "fleet" and rail["data"]["fleet"] == "data"
    assert rail["human:chris"]["fleet"] is None
    room = TestClient(create_app(tmp_path, package=source_package())) \
        .get("/api/identities?fleet=data").json()["data"]["identities"]
    assert {r["fleet"] for r in room} == {"data", None}   # the fleet + a human


def test_ui_renders_one_card_per_task_and_keeps_roster_fleet_grouping():
    from importlib.resources import files
    js = files("claudlobby.plane").joinpath("ui/app.js").read_text()
    assert "env.data.tasks" in js and "r.current_assignment" in js
    assert "task ${esc(r.task_id)}" in js
    assert "groupBroadcasts" not in js and "broadcast_key" not in js
    assert "groupBy(rows, (a) => a.fleet" in js
    assert "taskIssueNote(r)" in js and "Message delivery:" in js
    assert "Older work may still need attention" in js


# --- the task loop's human arms (chunk M-A, #1481) ---------------------------

def _task_event(root: Path, stem: str, event: str, *, at: str,
                fleet: str = "f", **detail):
    emit_batch(root, [{
        "event_type": "task", "emitter": "t", "fleet": fleet, "occurred_at": at,
        "payload": {"work_item_id": "wi_" + (stem * 32)[:32],
                    "assignment_id": "asg_" + (stem * 32)[:32],
                    "event": event, **detail}}])


def test_an_escalation_is_attention_with_its_question_and_its_own_instant(tmp_path):
    """#1481 M3: `escalated` is NON-terminal, so nothing in the open set or
    the status ladder distinguishes it — the arm is what makes it visible.
    The card dates it from the ESCALATION, never from the dispatch (an old
    row escalated a minute ago must not read "needs you 3d ago"), and carries
    the question and the person off the event's own detail."""
    _full_capture(tmp_path)          # the question is CONTENT, like every prose
    _dispatch(tmp_path, "a", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "a", "escalated", at="2026-01-01T00:00:00+00:00",
                question="ship it without the migration?", by="erlich")
    row = _tasks(tmp_path)["asg_" + ("a" * 32)]
    assert row["attention"] is True
    assert row["attention_reason"] == ["escalated"]
    assert row["attention_since"] == "2026-01-01T00:00:00+00:00"
    assert row["attention_question"] == "ship it without the migration?"
    assert row["attention_by"] == "erlich"
    assert row["state"] == "assigned"          # escalation is attention, not lifecycle


def test_a_later_act_clears_the_escalation_with_no_second_door(tmp_path):
    """The arm holds only while `escalated` is the assignment's NEWEST task
    event. A worker's progress, a manager's withdrawal, a re-dispatch — any
    of them ends it, which is why nothing has to remember to un-escalate."""
    _dispatch(tmp_path, "b", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "b", "escalated", at="2026-01-01T00:00:00+00:00",
                question="which repo?", by="erlich")
    assert _tasks(tmp_path)["asg_" + ("b" * 32)]["attention_reason"] == ["escalated"]
    _task_event(tmp_path, "b", "progress", at="2026-01-01T01:00:00+00:00")
    cleared = _tasks(tmp_path)["asg_" + ("b" * 32)]
    assert cleared["attention"] is False and cleared["attention_reason"] == []
    assert cleared["attention_question"] is None
    # ...and a withdrawal ends it terminally, so the row leaves the board's queue
    _task_event(tmp_path, "b", "escalated", at="2026-01-01T02:00:00+00:00",
                question="again?", by="erlich")
    assert _tasks(tmp_path)["asg_" + ("b" * 32)]["attention_reason"] == ["escalated"]
    _task_event(tmp_path, "b", "cancelled", at="2026-01-01T03:00:00+00:00",
                reason="overtaken by events", by="erlich")
    withdrawn = _tasks(tmp_path)["asg_" + ("b" * 32)]
    assert withdrawn["attention"] is False and withdrawn["state"] == "cancelled"


def test_a_nudge_is_quiet_inside_its_grace_and_attention_past_it(tmp_path):
    """#1481 M6: a nudge is an ASK and the manager is owed a moment, so it is
    not attention until `NUDGE_GRACE_S` has passed with no act. Both sides of
    the boundary, off ONE clock — the grace and the deadline arm derive from
    the same `now` through `attention_params`, so they can never disagree."""
    from datetime import datetime, timedelta, timezone

    from claudlobby.plane.queries import NUDGE_GRACE_S

    _full_capture(tmp_path)          # the nudge's reason is CONTENT, like the question
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=NUDGE_GRACE_S // 2)).isoformat()
    stale = (now - timedelta(seconds=NUDGE_GRACE_S * 2)).isoformat()
    _dispatch(tmp_path, "c", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "c", "nudged", at=fresh, reason="any movement?", by="chris")
    assert _tasks(tmp_path)["asg_" + ("c" * 32)]["attention"] is False

    _dispatch(tmp_path, "d", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "d", "nudged", at=stale, reason="any movement?", by="chris")
    row = _tasks(tmp_path)["asg_" + ("d" * 32)]
    assert row["attention_reason"] == ["nudged"]
    assert row["attention_since"] == stale
    assert row["attention_by"] == "chris"
    assert row["attention_act_reason"] == "any movement?"   # the fold's F10

    # the manager acting clears it even though the nudge is long past the grace
    _task_event(tmp_path, "d", "progress", at=now.isoformat())
    assert _tasks(tmp_path)["asg_" + ("d" * 32)]["attention"] is False


def test_the_human_arms_outrank_the_machine_ones_in_the_reason_order(tmp_path):
    """ATTENTION_ARMS order IS the operator's priority and dates the card: a
    row that is overdue AND escalated is a question waiting on a human, not a
    late task, so it dates from the question."""
    _dispatch(tmp_path, "e", expected_by=PAST, tx_state="failed")
    _task_event(tmp_path, "e", "escalated", at="2026-02-02T00:00:00+00:00",
                question="who owns this?", by="erlich")
    row = _tasks(tmp_path)["asg_" + ("e" * 32)]
    assert row["attention_reason"] == ["escalated", "send_failed", "overdue"]
    assert row["attention_since"] == "2026-02-02T00:00:00+00:00"


def test_the_overview_counts_the_human_arms_through_the_same_columns(tmp_path):
    """The strip reads attention off ATTENTION_ARMS_SQL, so a new arm reaches
    the header count without a second derivation (the fold's F2)."""
    _dispatch(tmp_path, "f", expected_by=FUTURE, tx_state="pane_submitted")
    before = TestClient(create_app(tmp_path, package=source_package())).get("/api/overview").json()
    assert sum(r["attention"] for r in before["data"]["fleets"]) == 0
    _task_event(tmp_path, "f", "escalated", at="2026-01-01T00:00:00+00:00",
                question="?", by="erlich")
    after = TestClient(create_app(tmp_path, package=source_package())).get("/api/overview").json()
    fleets = {r["alias"]: r for r in after["data"]["fleets"]}
    assert fleets["f"]["attention"] == 1 and fleets["f"]["overdue"] == 0


def test_a_metadata_capture_drops_the_question_but_never_the_arm(tmp_path):
    """The question is authored prose, so it is CONTENT and a metadata-mode
    fleet drops it AT THE DOOR like every other body — deliberately, rather
    than routing this one text around the policy. What must survive is the
    ARM and the person: the card still says a human is waiting and who asked,
    and reads "the question was not recorded" instead of inventing one."""
    (tmp_path / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "metadata"}')
    _dispatch(tmp_path, "a", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "a", "escalated", at="2026-01-01T00:00:00+00:00",
                question="ship it without the migration?", by="erlich")
    row = _tasks(tmp_path)["asg_" + ("a" * 32)]   # explicit metadata opt-out
    assert row["attention_reason"] == ["escalated"]
    assert row["attention_by"] == "erlich"
    assert row["attention_question"] is None


# --- the M-A fold's pins (#1481) ---------------------------------------------

def test_a_nudge_cannot_erase_an_escalation(tmp_path):
    """FOLD F1, the highest-severity finding of the round. Both the escalated
    arm and `plane-lookup --escalated` hold only while `escalated` is the
    assignment's NEWEST task event — and a nudge IS a task event, so
    escalate → nudge extinguished the escalation on every surface at once,
    permanently, since nothing ever re-raises one. A nudge is an ASK, not an
    ANSWER: the ruling enumerates what clears an escalation (progress, a
    terminal report, a withdrawal, a supersede) and a nudge is not on it."""
    _full_capture(tmp_path)
    _dispatch(tmp_path, "0", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "0", "escalated", at="2026-01-01T00:00:00+00:00",
                question="ship without the migration?", by="erlich")
    _task_event(tmp_path, "0", "nudged", at="2026-01-01T00:10:00+00:00",
                reason="any movement?", by="chris")
    row = _tasks(tmp_path)["asg_" + ("0" * 32)]
    assert row["attention"] is True
    assert row["attention_reason"][0] == "escalated"
    # ...dated and quoted from the ESCALATION, not from the nudge that followed
    assert row["attention_since"] == "2026-01-01T00:00:00+00:00"
    assert row["attention_question"] == "ship without the migration?"
    assert row["attention_by"] == "erlich"
    # ...and the nudge is not lost: it rides its own columns
    assert row["nudged_at"] == "2026-01-01T00:10:00+00:00"
    assert row["nudged_by"] == "chris"
    # a REAL act still clears it
    _task_event(tmp_path, "0", "progress", at="2026-01-01T02:00:00+00:00")
    assert _tasks(tmp_path)["asg_" + ("0" * 32)]["attention"] is False


def test_the_nudge_grace_compares_instants_not_strings(tmp_path):
    """FOLD F7. `occurred_at` is stored as the emitter's own isoformat, offset
    and all, so a lexical `<` compares wall-clock DIGITS: a nudge stamped ten
    minutes ago at `-04:00` sorts before a `+00:00` cutoff hours older and
    read as long past the grace. Reproduced; the fix compares epochs."""
    from datetime import datetime, timedelta, timezone

    from claudlobby.plane.queries import NUDGE_GRACE_S

    # ten minutes ago, stamped in a -04:00 offset — well inside the grace
    at = (datetime.now(timezone(timedelta(hours=-4)))
          - timedelta(seconds=NUDGE_GRACE_S // 3)).isoformat()
    assert at.endswith("-04:00")
    _dispatch(tmp_path, "1", expected_by=FUTURE, tx_state="pane_submitted")
    _task_event(tmp_path, "1", "nudged", at=at, reason="?", by="chris")
    row = _tasks(tmp_path)["asg_" + ("1" * 32)]
    assert row["attention"] is False, "an in-grace nudge paged because of its offset"
    assert row["nudged_at"] == at and row["nudged_by"] == "chris"


def test_an_in_grace_nudge_is_shown_but_never_credited_to_another_arm(tmp_path):
    """FOLD F10 + F11. The row is OVERDUE and separately carries a nudge that
    is still inside its grace. Two facts, and they must not be merged: the
    reason line is the deadline's (which names no person), while the nudge
    rides `nudged_at`/`nudged_by` so the card can still say "nudged 3m ago by
    chris" — attention only once the grace has passed."""
    from datetime import datetime, timedelta, timezone

    from claudlobby.plane.queries import NUDGE_GRACE_S

    fresh = (datetime.now(timezone.utc)
             - timedelta(seconds=NUDGE_GRACE_S // 2)).isoformat()
    _dispatch(tmp_path, "2", expected_by=PAST, tx_state="pane_submitted")
    _task_event(tmp_path, "2", "nudged", at=fresh, reason="any movement?", by="chris")
    row = _tasks(tmp_path)["asg_" + ("2" * 32)]
    assert row["attention_reason"] == ["overdue"]
    assert row["attention_since"] == PAST          # the deadline, not the nudge
    assert row["attention_by"] is None and row["attention_question"] is None
    assert row["nudged_at"] == fresh and row["nudged_by"] == "chris"


def test_every_arm_carries_its_own_date_column(tmp_path):
    """FOLD F12: the date used to live in a second list (`EVENT_DATED_ARMS`)
    plus an if/elif ladder in the view — the shape that lets a new arm ship
    undated. Every arm now emits `<name>_at` and `since` is a lookup."""
    import sqlite3

    from claudlobby.plane.db import connect, db_path
    from claudlobby.plane.queries import (ATTENTION_ARMS, ATTENTION_ARMS_SQL,
                                          attention_arms_params)
    from claudlobby.plane import view

    _dispatch(tmp_path, "3", expected_by=PAST, tx_state="failed")
    conn: sqlite3.Connection = connect(db_path(tmp_path))
    row = conn.execute(ATTENTION_ARMS_SQL,
                       attention_arms_params(view._now_iso())).fetchone()
    conn.close()
    cols = row.keys()
    for name, _sql, _at, _window in ATTENTION_ARMS:
        assert name in cols and f"{name}_at" in cols
    # send_failed leads here, so the card dates from the DISPATCH
    got = _tasks(tmp_path)["asg_" + ("3" * 32)]
    assert got["attention_reason"][0] == "send_failed"
    assert got["attention_since"] == got["current_assignment"]["occurred_at"]


def test_the_page_speaks_exactly_the_arms_and_canonical_task_states():
    """Attention names SQL arms; lifecycle labels name only reducer states."""
    import re
    from pathlib import Path as _P

    from claudlobby.plane.queries import ATTENTION_ARMS

    src = (_P(__file__).resolve().parent.parent / "claudlobby" / "plane" / "ui"
           / "app.js").read_text()
    why = src.split("const WHY = {", 1)[1].split("\n};", 1)[0]
    assert set(re.findall(r"^  (\w+):", why, re.M)) == \
        {name for name, _s, _a, _w in ATTENTION_ARMS}
    status = src.split("const TASK_STATUS = {", 1)[1].split("\n};", 1)[0]
    labelled = set(re.findall(r"^  (\w+):", status, re.M))
    assert labelled == {"queued", "assigned", "active", "blocked",
                        "completed", "failed", "cancelled"}
    assert "state unknown" in src
