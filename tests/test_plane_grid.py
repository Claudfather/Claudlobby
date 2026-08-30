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
    assert snap["up"]["alive"] is True
    assert "green" in snap["up"]["lines"]
    assert snap["down"]["alive"] is False  # a down bot is a FACT on the grid


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

H1, H2 = "1" * 32, "2" * 32


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
