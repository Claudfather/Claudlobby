"""Phase-4 chunk 2 battery: the thumbnail-grid sampler + fleet-aware channel.

Pins the chunk's rulings: ONE bounded sampler whose cost is invariant in the
number of viewers (§14); typed degradation when tmux is absent (§16 — never
a silent empty grid); dead sessions stay ON the grid as facts; per-team
channel filtering (operator ruling: rooms, not a firehose); discovery over
flat, nested, and root-mode layouts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from claudlobby.plane.emit_api import emit_batch  # noqa: E402
from claudlobby.plane.sampler import PaneSampler, discover_panes  # noqa: E402
from claudlobby.plane.view import create_app  # noqa: E402


def _bot(root: Path, layout: str, fleet: str, bot: str, sock: str = "") -> None:
    base = {"flat": root / "local" / fleet / "runtime" / "bots" / bot,
            "nested": root / "local" / "sys" / fleet / "runtime" / "bots" / bot,
            "root": root / "runtime" / "bots" / bot}[layout]
    base.mkdir(parents=True)
    line = f'export BOT_SERVICE="{sock or f"com.test.{bot}"}"\n'
    (base / "bot.conf").write_text(f'export BOT_ID="{bot}"\n{line}')


def _fake_tmux(tmp_path: Path, body: str) -> Path:
    t = tmp_path / "fake-tmux"
    t.write_text(f"#!/bin/bash\n{body}\n")
    t.chmod(0o755)
    return t


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovery_covers_flat_nested_and_root_layouts(tmp_path):
    _bot(tmp_path, "flat", "f1", "alpha")
    _bot(tmp_path, "nested", "f2", "beta")
    _bot(tmp_path, "root", "fleet", "gamma")  # root-mode: parent name is fleet
    panes = discover_panes(tmp_path)
    by_bot = {p["bot"]: p for p in panes}
    assert by_bot["alpha"]["fleet"] == "f1"
    assert by_bot["beta"]["fleet"] == "f2"
    assert by_bot["alpha"]["socket"] == "com.test.alpha"
    assert "gamma" in by_bot


def test_discovery_skips_bots_without_a_socket(tmp_path):
    d = tmp_path / "local" / "f1" / "runtime" / "bots" / "nosock"
    d.mkdir(parents=True)
    (d / "bot.conf").write_text('export BOT_ID="nosock"\n')
    assert discover_panes(tmp_path) == []


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------

def test_capture_marks_alive_and_dead(tmp_path):
    _bot(tmp_path, "flat", "f1", "up")
    _bot(tmp_path, "flat", "f1", "down")
    tmux = _fake_tmux(tmp_path,
                      'case "$*" in *down*) exit 1 ;; esac\n'
                      'printf "line one\\n\\033[32mgreen\\033[0m\\n"')
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        for p in s._panes:
            await s._capture(p, 14)
    asyncio.run(run())
    snap = {p["bot"]: p for p in s.snapshot()["panes"]}
    assert snap["up"]["status"] == "up"
    assert "green" in snap["up"]["lines"]
    assert snap["down"]["status"] == "down"  # a down bot is a FACT on the grid


def test_snapshot_is_pure_cache_read(tmp_path):
    """§14: N viewers reading the grid spawn ZERO captures — the sampler
    owns cadence; snapshot() never samples."""
    _bot(tmp_path, "flat", "f1", "up")
    counter = tmp_path / "calls"
    tmux = _fake_tmux(tmp_path, f'echo x >> "{counter}"\nprintf "hi\\n"')
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        await s._capture(s._panes[0], 14)
    asyncio.run(run())
    before = counter.read_text().count("x")
    for _ in range(25):
        s.snapshot()
    assert counter.read_text().count("x") == before


# ---------------------------------------------------------------------------
# /api/grid endpoint
# ---------------------------------------------------------------------------

def test_grid_unavailable_is_typed_never_empty(tmp_path):
    s = PaneSampler(tmp_path, tmux="/nonexistent/tmux")
    s.tmux = None  # simulate: no tmux resolvable anywhere
    body = TestClient(create_app(tmp_path, sampler=s)).get("/api/grid").json()
    assert body["state"] == "unavailable"
    assert "tmux" in body["remediation"]


def test_grid_ok_serves_snapshot_and_focus(tmp_path):
    _bot(tmp_path, "flat", "f1", "up")
    tmux = _fake_tmux(tmp_path, 'printf "pane content\\n"')
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        await s._capture(s._panes[0], 14)
    asyncio.run(run())
    client = TestClient(create_app(tmp_path, sampler=s))
    body = client.get("/api/grid").json()
    assert body["state"] == "ok"
    assert body["data"]["panes"][0]["bot"] == "up"
    body = client.get("/api/grid?focus=up").json()
    assert body["data"]["panes"][0]["focused"] is True


# ---------------------------------------------------------------------------
# Fleet-aware channel (rooms, not a firehose)
# ---------------------------------------------------------------------------

def _seed_two_fleets(root: Path) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')
    for fleet, h in (("engineering", "a"), ("data", "b")):
        emit_batch(root, [{
            "event_type": "communication", "emitter": "t", "fleet": fleet,
            "payload": {"msg_id": "msg_" + h * 32,
                        "sender": f"bot:{fleet}/one",
                        "recipient": f"bot:{fleet}/two",
                        "message_class": "chat",
                        "body": f"hello from {fleet}"}}])


def test_channel_fleet_filter_scopes_the_room(tmp_path):
    _seed_two_fleets(tmp_path)
    client = TestClient(create_app(tmp_path))
    eng = client.get("/api/channel?fleet=engineering").json()
    bodies = [m["body"] for t in eng["data"]["threads"]
              for m in t["messages"]]
    assert bodies == ["hello from engineering"]
    both = client.get("/api/channel").json()
    assert len(both["data"]["threads"]) == 2  # no filter = the firehose


def test_channel_unknown_fleet_is_ok_empty_not_error(tmp_path):
    _seed_two_fleets(tmp_path)
    body = TestClient(create_app(tmp_path)).get(
        "/api/channel?fleet=nonexistent").json()
    assert body["state"] == "ok"
    assert body["data"]["threads"] == []  # legitimately idle — UI's word


# ---------------------------------------------------------------------------
# UI load-order guard (a server test can't run the browser; this pins the
# structural invariant whose violation froze the page: the bootstrap calls
# must follow every top-level `let`/`const` they read, or those bindings are
# touched in their temporal dead zone on first load).
# ---------------------------------------------------------------------------

def test_app_js_bootstrap_runs_after_declarations():
    app_js = (Path(__file__).resolve().parent.parent / "claudlobby"
              / "plane" / "ui" / "app.js").read_text()
    boot = app_js.index("refreshBoards();\nopenStream();")
    # every module-scope state binding the bootstrap path reads transitively
    for decl in ("let currentFleet", "let currentView", "let generation",
                 "let safetyTimer"):
        assert decl in app_js, decl
        assert app_js.index(decl) < boot, (
            f"{decl} is declared AFTER the bootstrap — TDZ freeze on load")


# ---------------------------------------------------------------------------
# Gauntlet pins (grid chunk review round)
# ---------------------------------------------------------------------------

def test_failed_capture_keeps_last_good_frame_and_ages_it(tmp_path):
    """§16 last-successful-observation: a capture that fails must not erase
    the last good frame or restamp it fresh (the first version stamped
    captured_at=now on an empty frame — freshness that lies)."""
    _bot(tmp_path, "flat", "f1", "up")
    marker = tmp_path / "die"
    body = '[ -f "MARKER" ] && exit 1\nprintf "good frame\\n"'
    tmux = _fake_tmux(tmp_path, body.replace("MARKER", str(marker)))
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        await s._capture(s._panes[0], 14)      # succeeds
        marker.write_text("x")
        await s._capture(s._panes[0], 14)      # now fails
    asyncio.run(run())
    pane = s.snapshot()["panes"][0]
    assert pane["status"] == "down"
    assert "good frame" in pane["lines"]       # last good frame retained
    assert pane["captured_ago_s"] is not None  # aged, not reset to fresh


def test_same_name_bots_across_fleets_do_not_share_a_slot(tmp_path):
    """#526: bot-name collision across fleets — each keeps its own pane."""
    _bot(tmp_path, "flat", "f1", "twin", sock="com.f1.twin")
    _bot(tmp_path, "flat", "f2", "twin", sock="com.f2.twin")
    tmux = _fake_tmux(tmp_path, 'printf "sock %s\n" "$2"')  # $2 = -L socket
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        for p in s._panes:
            await s._capture(p, 14)
    asyncio.run(run())
    frames = {(p["fleet"]): p["lines"] for p in s.snapshot()["panes"]}
    assert len(frames) == 2                     # two slots, not one
    assert frames["f1"] != frames["f2"]         # distinct captures


def test_focus_disambiguates_by_fleet(tmp_path):
    _bot(tmp_path, "flat", "f1", "twin", sock="com.f1.twin")
    _bot(tmp_path, "flat", "f2", "twin", sock="com.f2.twin")
    s = PaneSampler(tmp_path, tmux=str(_fake_tmux(tmp_path, "true")))
    s._panes = discover_panes(tmp_path)
    s.focus("twin", "f2")
    snap = {(p["fleet"]): p["focused"] for p in s.snapshot()["panes"]}
    assert snap["f2"] is True and snap["f1"] is False


def test_ansi_truecolor_never_emits_a_stray_basic_class():
    """The truecolor arg 38;2;255;135;95 must be CONSUMED, not re-read as
    a-95 (measured against real Claude-pane color)."""
    app_js = (Path(__file__).resolve().parent.parent / "claudlobby"
              / "plane" / "ui" / "app.js").read_text()
    # structural: the extended-color branch consumes args before the basic
    # 30-37/90-97 branch can see them.
    assert "code === 38 || code === 48" in app_js
    i38 = app_js.index("code === 38")
    ibasic = app_js.index("code >= 30 && code <= 37")
    assert i38 < ibasic, "extended-color must be handled before basic SGR"


def test_focus_endpoint_ships_only_the_focused_pane(tmp_path):
    """Efficiency (measured 6.3x waste): the overlay renders ONE pane, so
    /api/grid?focus= must return one, not all N."""
    _bot(tmp_path, "flat", "f1", "up")
    _bot(tmp_path, "flat", "f1", "other")
    tmux = _fake_tmux(tmp_path, 'printf "x\\n"')
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        for p in s._panes:
            await s._capture(p, 14)
    asyncio.run(run())
    client = TestClient(create_app(tmp_path, sampler=s))
    full = client.get("/api/grid").json()
    assert len(full["data"]["panes"]) == 2
    focused = client.get("/api/grid?focus=up&fleet=f1").json()
    assert len(focused["data"]["panes"]) == 1
    assert focused["data"]["panes"][0]["bot"] == "up"
    assert "pid" in focused["provenance"]  # two grids betray themselves


def test_never_sampled_pane_is_sampling_not_down(tmp_path):
    """source_state #1216: a pane the sampler has not reached is 'sampling',
    NOT 'down' — no-evidence must not read as evidenced-dead."""
    _bot(tmp_path, "flat", "f1", "fresh")
    s = PaneSampler(tmp_path, tmux=str(_fake_tmux(tmp_path, "true")))
    s._panes = discover_panes(tmp_path)   # discovered, never captured
    pane = s.snapshot()["panes"][0]
    assert pane["status"] == "sampling"


def test_room_shows_cross_fleet_threads_from_both_sides(tmp_path):
    """The default room must show a thread that TOUCHES the fleet as sender
    OR recipient — a sender-only predicate halved cross-fleet conversations
    (44.6% of dispatch traffic)."""
    d = tmp_path / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')
    wi = "wi_" + "e" * 32
    # eng -> data dispatch, and the data -> eng reply
    emit_batch(tmp_path, [
        {"event_type": "work_item", "emitter": "t", "fleet": "engineering",
         "payload": {"work_item_id": wi, "title": "cross", "created_by":
                     "bot:engineering/lead"}},
        {"event_type": "communication", "emitter": "t", "fleet": "engineering",
         "payload": {"msg_id": "msg_" + "e" * 32, "sender":
                     "bot:engineering/lead", "recipient": "bot:data/worker",
                     "message_class": "task_request", "work_item_id": wi,
                     "body": "the ask"}},
        {"event_type": "communication", "emitter": "t", "fleet": "data",
         "payload": {"msg_id": "msg_" + "f" * 32, "sender": "bot:data/worker",
                     "recipient": "bot:engineering/lead",
                     "message_class": "report", "work_item_id": wi,
                     "reply_to_msg_id": "msg_" + "e" * 32,
                     "body": "the answer"}}])
    client = TestClient(create_app(tmp_path))
    for room in ("engineering", "data"):
        threads = client.get(f"/api/channel?fleet={room}").json()["data"]["threads"]
        bodies = sorted(m["body"] for t in threads for m in t["messages"])
        assert bodies == ["the answer", "the ask"], f"{room} split the thread"


def test_hidden_attribute_actually_hides():
    """Operator-found (2026-08-30): #focus-overlay's id-selector display rule
    outranked [hidden]{display:none} on specificity, so `hidden` was a no-op
    and the overlay could never be closed. Pin both halves: the reset exists
    and the overlay ships hidden."""
    ui = Path(__file__).resolve().parent.parent / "claudlobby" / "plane" / "ui"
    css = (ui / "style.css").read_text()
    assert "[hidden] { display: none !important; }" in css
    html = (ui / "index.html").read_text()
    import re as _re
    m = _re.search(r'<div id="focus-overlay"[^>]*>', html)
    assert m and " hidden" in m.group(0), "overlay must ship with hidden"


# ---------------------------------------------------------------------------
# Gauntlet round 2 pins (the fix-round review — three reviewers)
# ---------------------------------------------------------------------------

def test_room_query_never_scans_communications(tmp_path):
    """Three reviewers independently EXPLAIN'd the OR form back to a full
    reverse scan — 0003 shipped inert against its own query. The UNION
    equality form must SEARCH both arms; this pin keeps the scan from
    silently returning."""
    import sqlite3 as _sq

    _seed_two_fleets(tmp_path)
    conn = _sq.connect(tmp_path / "state" / "plane" / "plane.db")
    cols = "ingest_seq, msg_id"
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        f"SELECT * FROM (SELECT {cols} FROM communications"
        "  WHERE fleet_uid = (SELECT uid FROM identity_registry"
        "   WHERE kind='fleet' AND alias = ?)"
        "  ORDER BY ingest_seq DESC LIMIT ?)"
        " UNION "
        f"SELECT * FROM (SELECT {cols} FROM communications"
        "  WHERE recipient_fleet = ?"
        "  ORDER BY ingest_seq DESC LIMIT ?)"
        " ORDER BY ingest_seq DESC LIMIT ?",
        ("engineering", 10, "engineering", 10, 10)).fetchall()
    conn.close()
    detail = " | ".join(r[-1] for r in plan)
    assert "SCAN communications" not in detail, detail
    assert "idx_intents_fleet_seq" in detail, detail
    assert "idx_intents_recipient_fleet" in detail, detail


def test_room_is_immune_to_like_metacharacters(tmp_path):
    """Probed in review: `?fleet=en_` absorbed `eng`'s room and `?fleet=%`
    returned the whole firehose dressed as a room. Equality arms retire the
    metacharacter class."""
    _seed_two_fleets(tmp_path)
    client = TestClient(create_app(tmp_path))
    for evil in ("en_", "%", "engineerin_"):
        body = client.get(f"/api/channel?fleet={evil}").json()
        assert body["state"] == "ok"
        assert body["data"]["threads"] == [], f"{evil!r} leaked a room"


def test_index_html_alias_is_rewritten_too(tmp_path):
    """Probed in review: /index.html served the RAW file via the mount —
    an unbusted second door that re-pins stale modules."""
    client = TestClient(create_app(tmp_path))
    for path in ("/", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200
        assert "/app.js?v=" in r.text, path
        assert r.headers.get("cache-control") == "no-store"


def test_asset_token_tracks_in_place_updates(tmp_path):
    """The token must change when a UI file changes UNDER the running
    daemon (update-siblings pulls weekly; host services are not restarted
    by the worker restart) — a process-lifetime token went stale in exactly
    that window."""
    import os as _os
    import re as _re

    from claudlobby.plane import view as view_mod

    client = TestClient(create_app(tmp_path))
    tok1 = _re.search(r"/app\.js\?v=([a-f0-9]+)", client.get("/").text).group(1)
    app_js = view_mod.UI_DIR / "app.js"
    st = app_js.stat()
    _os.utime(app_js, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    try:
        tok2 = _re.search(r"/app\.js\?v=([a-f0-9]+)",
                          client.get("/").text).group(1)
    finally:
        _os.utime(app_js, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert tok1 != tok2


def test_focus_ships_exactly_one_pane_for_twin_names(tmp_path):
    """Probed in review: the endpoint's own filter re-derived the sampler's
    resolution and shipped TWO panes for twin-named bots. It now filters on
    the sampler's stamped flag — one pane, always."""
    _bot(tmp_path, "flat", "f1", "twin", sock="com.f1.twin")
    _bot(tmp_path, "flat", "f2", "twin", sock="com.f2.twin")
    tmux = _fake_tmux(tmp_path, 'printf "x\\n"')
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        for p in s._panes:
            await s._capture(p, 14)
    asyncio.run(run())
    client = TestClient(create_app(tmp_path, sampler=s))
    body = client.get("/api/grid?focus=twin").json()
    assert len(body["data"]["panes"]) == 1


def test_wedged_capture_reap_does_not_block_on_grandchild_pipe(tmp_path):
    """Probed in review: kill-then-communicate() waited on the stdout PIPE,
    which an orphaned grandchild held for its lifetime (30s probed;
    unbounded on a D-state tmux). wait() + bound must return promptly."""
    import time as _time

    _bot(tmp_path, "flat", "f1", "wedge")
    # parent hangs (trips the 4s timeout); its backgrounded child holds the
    # stdout pipe long past the kill.
    tmux = _fake_tmux(tmp_path, "sleep 30 &\nexec sleep 30")
    s = PaneSampler(tmp_path, tmux=str(tmux))
    s._panes = discover_panes(tmp_path)

    async def run():
        t0 = _time.monotonic()
        await s._capture(s._panes[0], 14)
        return _time.monotonic() - t0
    elapsed = asyncio.run(run())
    assert elapsed < 7.0, f"reap blocked {elapsed:.1f}s on the orphan pipe"
    assert s.snapshot()["panes"][0]["status"] == "down"
