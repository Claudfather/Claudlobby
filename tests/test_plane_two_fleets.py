"""Chunk U's fold pins (F18 closure, #1467): the rules the four lenses named,
each pinned where a mutant would revert it — one fleet axis on every arm,
the unknown-fleet refusal, the sender's own fleet, the matcher's open rule,
the host card's recorded facts, one bot-dir walk."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane import sampler as _sampler  # noqa: E402
from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.inventory import qualified_labels  # noqa: E402
from claudlobby.plane.queries import (  # noqa: E402
    fleet_alias_range, fleet_range_params, not_sentinel_sql)
from claudlobby.plane.view import create_app  # noqa: E402

PAST, FUTURE = "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"


def _seed(root: Path, fleets=(("engineering", "a"), ("data", "b")),
          expected_by=None) -> None:
    """One manager, one worker `one`, one dispatched assignment per fleet."""
    for fleet, h in fleets:
        mgr, worker = f"bot:{fleet}/mgr", f"bot:{fleet}/one"
        asg = {"assignment_id": "asg_" + h * 32, "work_item_id": "wi_" + h * 32,
               "assignee": worker, "assigned_by": mgr,
               "dispatch_msg_id": "msg_" + h * 32}
        if expected_by:
            asg["expected_by"] = expected_by
        emit_batch(root, [
            {"event_type": "work_item", "emitter": "t", "fleet": fleet,
             "payload": {"work_item_id": "wi_" + h * 32,
                         "title": f"work for {fleet}", "created_by": mgr}},
            {"event_type": "assignment", "emitter": "t", "fleet": fleet,
             "payload": asg},
            {"event_type": "communication", "emitter": "t", "fleet": fleet,
             "payload": {"msg_id": "msg_" + h * 32, "sender": mgr,
                         "recipient": worker, "message_class": "task_request",
                         "command_type": "task", "body": f"go {fleet}"}}])


def _comm(fleet, h, sender, recipient, cls="chat"):
    return {"event_type": "communication", "emitter": "t", "fleet": fleet,
            "payload": {"msg_id": "msg_" + h * 32, "sender": sender,
                        "recipient": recipient, "message_class": cls,
                        "body": "hi"}}


class _Sampler:
    available = True

    def __init__(self, panes):
        self._panes = panes

    def snapshot(self):
        return {"panes": list(self._panes), "sampler_running": True}

    def focus(self, *_a, **_k):
        pass

    def start(self):
        pass

    async def stop(self):
        pass


# --- the fleet axis: one rule, every arm, case-sensitive ----------------------

def test_fleet_axis_is_one_case_sensitive_range_on_every_arm(tmp_path):
    """A LIKE arm is ASCII-case-insensitive while the room's equality arms
    are not, so `Eng` counted `eng`'s bots on the tab and lost them in the
    room (adversarial lens). Every arm now binds queries.fleet_alias_range:
    a case-variant fleet name is a different fleet everywhere."""
    _seed(tmp_path, fleets=(("eng", "a"), ("Eng", "b")))
    c = TestClient(create_app(tmp_path))
    fleets = {f["alias"]: f["bots"] for f in c.get("/api/fleets").json()["data"]["fleets"]}
    assert fleets == {"Eng": 2, "eng": 2}
    tasks = c.get("/api/tasks?fleet=Eng").json()["data"]["assignments"]
    assert [a["title"] for a in tasks] == ["work for Eng"]
    rail = c.get("/api/identities?fleet=Eng").json()["data"]["identities"]
    assert {r["alias"] for r in rail if r["alias"].startswith("bot:")} == {
        "bot:Eng/mgr", "bot:Eng/one"}
    ov = {r["alias"]: r for r in c.get("/api/overview").json()["data"]["fleets"]}
    assert (ov["Eng"]["open"], ov["eng"]["open"]) == (1, 1)
    # the predicate itself: a range, no LIKE metacharacter to escape
    assert "LIKE" not in fleet_alias_range() and fleet_range_params("x") == ("x", "x")
    assert "NOT LIKE" in not_sentinel_sql()


def test_unknown_fleet_is_a_typed_state_on_every_route(tmp_path):
    """plane-lookup's rule applied to the view: a fleet the plane holds no
    identity for (while it holds others) answers `unknown` naming the fleets
    held — never a healthy empty room; `fleet=` and `fleet=all` are the
    host-wide read on every route; a plane holding NO fleet yet passes the
    name through to the route's own idle remedy (run `generate`)."""
    _seed(tmp_path)
    live = [{"fleet": "engineering", "bot": "one", "status": "up"},
            {"fleet": "disk-only", "bot": "z", "status": "up"}]
    c = TestClient(create_app(tmp_path, sampler=_Sampler(live)))
    for route in ("tasks", "identities", "channel", "search?q=go&x=1",
                  "inventory", "org", "utilization", "presence", "grid"):
        sep = "&" if "?" in route else "?"
        body = c.get(f"/api/{route}{sep}fleet=ghost").json()
        assert body["state"] == "unknown", route
        assert "data, engineering" in body["remediation"] and "data" not in body, route
    for axis in ("", "all"):
        assert len(c.get(f"/api/tasks?fleet={axis}").json()["data"]["assignments"]) == 2
        assert c.get(f"/api/presence?fleet={axis}").json()["state"] == "ok"
    # a fleet the SAMPLER knows (a bot dir before its first plane row) is not unknown
    assert c.get("/api/grid?fleet=disk-only").json()["data"]["panes"] == [live[1]]
    assert c.get("/api/presence?fleet=disk-only").json()["state"] == "ok"
    # a plane holding no fleet at all: the name passes through
    bare = tmp_path / "bare"; bare.mkdir()
    emit_batch(bare, [{"event_type": "metric_sample", "emitter": "probe",
                       "fleet": "_host", "payload": {"subject_kind": "host",
                                                     "subject": "h1",
                                                     "metric": "host.job_ran",
                                                     "value": 1}}])
    fresh = TestClient(create_app(bare)).get("/api/tasks?fleet=anything").json()
    assert fresh["state"] == "ok" and fresh["data"]["assignments"] == []


# --- identity: the sender's own fleet, and qualification where fleets meet ----

def test_sender_fleet_is_read_off_the_alias_not_the_emitting_fleet(tmp_path):
    """`fleet_uid` is the fleet a row was EMITTED under: a human writing
    through the data bridge to an eng bot acquired a fleet, and an eng bot
    emitting under data passed as data's own (adversarial lens, both
    reproduced). Each party's fleet is inventory.fleet_of(its alias)."""
    _seed(tmp_path)
    emit_batch(tmp_path, [
        _comm("data", "c", "human:chris", "bot:engineering/one"),
        _comm("data", "d", "bot:engineering/one", "bot:data/one")])
    c = TestClient(create_app(tmp_path))
    msgs = {m["msg_id"]: m for t in c.get("/api/channel").json()["data"]["threads"]
            for m in t["messages"]}
    human = msgs["msg_" + "c" * 32]
    assert (human["sender_fleet"], human["recipient_fleet"]) == (None, "engineering")
    assert human["cross_fleet"] is False and human["sender_short"] == "chris"
    bot = msgs["msg_" + "d" * 32]
    assert (bot["sender_fleet"], bot["recipient_fleet"]) == ("engineering", "data")
    assert bot["cross_fleet"] is True
    assert (bot["sender_short"], bot["recipient_short"]) == ("engineering/one", "data/one")
    # ...in the data room too, where a bare `one` would read as data's own
    room = {m["msg_id"]: m for t in c.get("/api/channel?fleet=data").json()["data"]["threads"]
            for m in t["messages"]}
    assert room["msg_" + "d" * 32]["sender_short"] == "engineering/one"
    assert "fleet_uid" not in bot


def test_qualification_applies_wherever_two_fleets_meet_only():
    """One fleet in a read (a room, or a single-fleet host — most installs):
    bare names, they are unambiguous. Two: every bot reads fleet/name."""
    assert qualified_labels(["bot:f/erlich", "bot:f/dinesh", "human:chris"]) == {}
    assert qualified_labels(["bot:f/erlich", "bot:g/erlich", "bot:f/dinesh",
                             "human:chris"]) == {
        "bot:f/erlich": "f/erlich", "bot:g/erlich": "g/erlich",
        "bot:f/dinesh": "f/dinesh"}


# --- the strip: the matcher's open rule, provisional disclosed, host facts ----

def test_overview_open_is_the_matchers_rule(tmp_path):
    """One task id dispatched twice to the same worker, the second completed:
    the matcher (OPEN_ASSIGNMENTS_AT_SQL, brief/fleet-pulse) closes BOTH
    through its sibling rule, so the strip must say 0 — a second definition
    of "open" in the view said 1 (structural lens, reproduced)."""
    _seed(tmp_path)
    worker = "bot:engineering/one"
    emit_batch(tmp_path, [
        {"event_type": "assignment", "emitter": "t", "fleet": "engineering",
         "source_ref": "dispatch-log:t-9",
         "payload": {"assignment_id": "asg_" + "1" * 32, "work_item_id": "wi_" + "a" * 32,
                     "assignee": worker, "assigned_by": "bot:engineering/mgr",
                     "dispatch_msg_id": "msg_" + "1" * 32}},
        {"event_type": "assignment", "emitter": "t", "fleet": "engineering",
         "source_ref": "dispatch-log:t-9",
         "payload": {"assignment_id": "asg_" + "2" * 32, "work_item_id": "wi_" + "a" * 32,
                     "assignee": worker, "assigned_by": "bot:engineering/mgr",
                     "dispatch_msg_id": "msg_" + "2" * 32}},
        {"event_type": "task", "emitter": "t", "fleet": "engineering",
         "payload": {"event": "completed", "work_item_id": "wi_" + "a" * 32,
                     "assignment_id": "asg_" + "2" * 32, "actor": worker}}])
    c = TestClient(create_app(tmp_path))
    ov = {r["alias"]: r for r in c.get("/api/overview").json()["data"]["fleets"]}
    # the seed's own assignment (no source_ref) stays open; the re-dispatched pair closed
    assert ov["engineering"]["open"] == 1 and ov["data"]["open"] == 1


def test_overview_discloses_provisional_actors_and_the_host_facts(tmp_path):
    """A mistyped dispatch target mints a provisional actor: the count says
    "3 bots, 1 unconfirmed" rather than absorbing it (adversarial lens).
    The host card carries the host probe's newest facets (the `_host`
    sentinel's samples) — None until the probe ever recorded — and the
    ingest-lag STATE, stamped by the API rather than the page."""
    _seed(tmp_path)
    c = TestClient(create_app(tmp_path))
    before = c.get("/api/overview").json()["data"]
    eng = {r["alias"]: r for r in before["fleets"]}["engineering"]
    assert (eng["bots"], eng["provisional"]) == (2, 2)   # no scan yet: all unconfirmed
    assert before["host"]["samples"] is None
    assert before["host"]["ingest_lag_state"] in ("ok", "warn")
    assert before["default"] in ("engineering", "data")
    emit_batch(tmp_path, [_comm("engineering", "e", "bot:engineering/mgr", "bot:engineering/oen")])
    emit_batch(tmp_path, [{
        "event_type": "metric_sample", "emitter": "host-probe", "fleet": "_host",
        "payload": {"subject_kind": "host", "subject": "h1", "metric": m, "value": v}}
        for m, v in (("host.load", "0.42 0.40 0.39"), ("host.mem_available_mb", 8192),
                     ("host.load", "1.00 0.90 0.80"))])
    after = c.get("/api/overview").json()["data"]
    eng = {r["alias"]: r for r in after["fleets"]}["engineering"]
    assert (eng["bots"], eng["provisional"]) == (3, 3)
    samples = after["host"]["samples"]
    assert samples["host.load"]["value"] == "1.00 0.90 0.80"      # the newest wins
    assert samples["host.mem_available_mb"]["value"] == 8192
    assert "host.disk_free_gb" not in samples                      # never emitted: absent, not 0
    assert not [f for f in after["fleets"] if f["alias"].startswith("_")]


# --- one bot-dir walk ---------------------------------------------------------

def test_discover_bot_dirs_is_the_walk_the_grid_gates(tmp_path):
    """The overview's orphan split and the grid's pane discovery walk the
    layout through ONE function; the grid only adds the socket gate."""
    bots = tmp_path / "local" / "f" / "runtime" / "bots"
    (bots / "sock").mkdir(parents=True)
    (bots / "sock" / "bot.conf").write_text("BOT_NAME=sock\nTMUX_SOCKET=/tmp/x\nFLEET_NAME=f\n")
    (bots / "nosock").mkdir()
    (bots / "nosock" / "bot.conf").write_text("BOT_NAME=nosock\nFLEET_NAME=f\n")
    (bots / "notabot").mkdir()
    walked = _sampler.discover_bot_dirs(tmp_path)
    assert [(f, b) for f, b, _ in walked] == [("f", "nosock"), ("f", "sock")]
    panes = _sampler.discover_panes(tmp_path)
    assert [p["bot"] for p in panes] == ["sock"] or panes == []   # gate: socket or nothing


# --- the strip's unacked figure: the fleet's newest ack (chunk K) --------------

def test_overview_unacked_is_past_the_fleets_newest_ack(tmp_path):
    """`brief --ack` records a `reports_acked` event on the acking bot; the card
    counts report-class communications on the room axis past the fleet's newest
    ack by ANY of its actors; a fleet that never acked reads null + reason —
    never a count of everything ever — and another fleet's ack is not its own."""
    _seed(tmp_path)
    emit_batch(tmp_path, [
        _comm("engineering", "1", "bot:engineering/one", "bot:engineering/mgr", cls="report"),
        _comm("engineering", "2", "bot:engineering/one", "bot:engineering/mgr", cls="report")])
    c = TestClient(create_app(tmp_path))
    rows = lambda: {r["alias"]: r for r in c.get("/api/overview").json()["data"]["fleets"]}
    eng = rows()["engineering"]
    assert eng["unacked"] is None and "never run" in eng["unacked_reason"]
    assert (eng["acked_by"], eng["acked_at"]) == (None, None)

    conn = sqlite3.connect(tmp_path / "state" / "plane" / "plane.db")
    first_seq = conn.execute("SELECT ingest_seq FROM communications WHERE msg_id = ?",
                             ("msg_" + "1" * 32,)).fetchone()[0]
    conn.close()

    def ack(data):
        return emit_batch(tmp_path, [{
            "event_type": "system", "emitter": "brief", "fleet": "engineering",
            "payload": {"event": "reports_acked", "subject_kind": "actor",
                        "subject": "bot:engineering/mgr", "data": data}}])[0].status
    assert ack({"acked_through_seq": first_seq, "acked_through_ts": "2026-09-05T00:00:00Z",
                "count": 1}) == "committed"
    eng = rows()["engineering"]
    assert eng["unacked"] == 1 and eng["acked_by"] == "mgr" and eng["acked_at"]
    assert rows()["data"]["unacked"] is None            # data's manager has not acked
    # a newer ack whose detail carries no cursor is no read position (never a 0)
    assert ack({"note": "nothing readable"}) == "committed"
    assert rows()["engineering"]["unacked"] is None
