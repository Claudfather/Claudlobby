"""Keepalive-as-a-door: presence's RECORDED half (#1361, harvest item 1).

Every pin drives the REAL lib/keepalive.sh tick (real lib-common, real
shim, real cold-CLI ingest into a scratch plane db; tmux and start-bot.sh
stubbed) — the door-test pattern from test_plane_gauntlet_doors. The
load-bearing laws: the tick's ALREADY-COMPUTED verdict is what gets
recorded (the sampler classifies nothing); the dead-session path records
session_up=false and NO heartbeat (no pane was classified — a fabricated
verdict is the lie this lane kills); dormancy holds; and the sample's
subject resolves to the SAME uid the registry keyframes use, so presence
joins equipment with no glue.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claudlobby.plane.db import db_path
from claudlobby.plane.emit_api import emit_batch

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = ("keepalive.sh", "lib-common.sh", "plane-emit.sh",
              "plane-socket-client.py")


def _rig(tmp_path: Path, *, pane: str = "> ", has_session: bool = True,
         fresh_marker: bool = False, armed: bool = True):
    # pane default is the ASCII form of the idle glyph class — the ❯ glyph
    # byte-matches unreliably under the rig's minimal (C-locale) env
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in DOOR_FILES:
        (libdir / name).symlink_to(LIB / name)
    sb = libdir / "start-bot.sh"
    sb.write_text("#!/bin/bash\necho started >> \"$1/start-stub.log\"\n"
                  "exit 0\n")
    sb.chmod(0o755)
    tmux = tmp_path / "tmux"
    hs = "exit 0" if has_session else "exit 1"
    tmux.write_text(
        "#!/bin/bash\ncase \"$*\" in\n"
        f"  *has-session*) {hs} ;;\n"
        f"  *capture-pane*) printf '%s\\n' {json.dumps(pane)} ;;\n"
        "  *) exit 0 ;;\nesac\n")
    tmux.chmod(0o755)
    bot = tmp_path / "bots" / "b1"
    (bot / "data").mkdir(parents=True)
    (bot / "bot.conf").write_text(
        'BOT_NAME="b1"\nFLEET_NAME="kfleet"\nBOT_SERVICE="com.k.b1"\n')
    if fresh_marker:
        (bot / "data" / ".last-tool-call").touch()
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "HOME": str(tmp_path),
        "PLANE_EMIT_CLI": str(CLI),
        "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "PATH": "/usr/bin:/bin",
    }
    if armed:
        env["PLANE_EMIT_ENABLED"] = "1"
    return libdir, bot, env


def _tick(libdir: Path, bot: Path, env: dict):
    return subprocess.run(
        ["bash", str(libdir / "keepalive.sh"), str(bot)],
        capture_output=True, text=True, env=env, timeout=120)


def _samples(root: Path):
    db = db_path(root)
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT subject_kind, subject_uid, metric, value"
            " FROM metric_samples ORDER BY ingest_seq")]
    finally:
        conn.close()


def test_idle_tick_records_session_up_and_heartbeat(tmp_path):
    libdir, bot, env = _rig(tmp_path)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    rows = _samples(tmp_path)
    by_metric = {s["metric"]: s for s in rows}
    assert json.loads(by_metric["bot.session_up"]["value"]) is True
    hb = json.loads(by_metric["bot.heartbeat"]["value"])
    assert hb["state"] == "IDLE"
    assert "marker_age_s" not in hb       # no marker -> no fabricated age
    assert by_metric["bot.heartbeat"]["subject_kind"] == "bot_instance"
    assert by_metric["bot.heartbeat"]["subject_uid"].startswith("boti_")


def test_busy_marker_tick_carries_marker_age(tmp_path):
    libdir, bot, env = _rig(tmp_path, fresh_marker=True)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "BUSY"
    assert isinstance(hb["marker_age_s"], int) and hb["marker_age_s"] >= 0


def test_unknown_pane_records_unknown(tmp_path):
    libdir, bot, env = _rig(tmp_path, pane="#### garbage ####")
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "UNKNOWN"


def test_dead_session_records_session_down_and_no_heartbeat(tmp_path):
    """The dead path records the one fact it observed (session_up=false)
    and NO heartbeat — no pane was classified, and a fabricated verdict is
    the lie this lane exists to kill. The restart still runs (stub)."""
    libdir, bot, env = _rig(tmp_path, has_session=False)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    rows = _samples(tmp_path)
    assert [s["metric"] for s in rows] == ["bot.session_up"]
    assert json.loads(rows[0]["value"]) is False
    assert (bot / "start-stub.log").exists()   # the restart ladder ran


def test_dormant_without_arming_emits_nothing(tmp_path):
    libdir, bot, env = _rig(tmp_path, armed=False)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert not db_path(tmp_path).is_file()


def test_heartbeat_subject_joins_the_registry_keyframe(tmp_path):
    """THE join pin: identity resolution lands the sample on the SAME uid
    the registry keyframes use for this instance — presence joins
    equipment/history with no glue. (bot entity_type -> bot_instance kind,
    same alias, one identity.)"""
    libdir, bot, env = _rig(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "registry_snapshot", "emitter": "t", "fleet": "kfleet",
        "payload": {"entity_type": "bot", "entity_alias": "bot:kfleet/b1",
                    "cause": "generate", "scan_id": "s1",
                    "payload": {"alias": "bot:kfleet/b1", "account": "a",
                                "service": "s", "model": "opus",
                                "posture": {"permissions_mode": "plan"},
                                "composed_hashes": {}, "declared_hash": "d",
                                "schema_version": "1"}}}])
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(db_path(tmp_path))
    key_uid = conn.execute(
        "SELECT entity_uid FROM registry_snapshots").fetchone()[0]
    hb_uid = conn.execute(
        "SELECT subject_uid FROM metric_samples"
        " WHERE metric='bot.heartbeat'").fetchone()[0]
    conn.close()
    assert hb_uid == key_uid
