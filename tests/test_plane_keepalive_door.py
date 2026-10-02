"""Keepalive-as-a-door: presence's RECORDED half (#1361, harvest item 1).

Every pin drives the REAL claudlobby/_runtime_scripts/keepalive.sh tick (real lib-common, real
shim, real staged replay into a scratch plane db; tmux and start-bot.sh
stubbed) — the door-test pattern from test_plane_gauntlet_doors. The
load-bearing laws: the tick's ALREADY-COMPUTED verdict is what gets
recorded (the sampler classifies nothing); the dead-session path records
session_up=false and NO heartbeat (no pane was classified — a fabricated
verdict is the lie this lane kills); the emit is always on (only
PLANE_EMIT_DISABLED=1 silences it — F18 closure R1); and the sample's
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
from claudlobby.plane.daemon import PlaneDaemon
from claudlobby.plane.emit_api import emit_batch
from tests.fixtures.native_admission import admit_watchdog_fixture

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "claudlobby/_runtime_scripts"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = ("keepalive.sh", "lib-common.sh", "supervisor.sh", "plane-emit.sh",
              "plane-socket-client.py")


def _rig(tmp_path: Path, *, pane: str = "> ", has_session: bool = True,
         fresh_marker: bool = False, armed: bool = True, disabled: bool = False, scratch_plane_env):
    # pane default is the ASCII form of the idle glyph class — the ❯ glyph
    # byte-matches unreliably under the rig's minimal (C-locale) env
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in DOOR_FILES:
        (libdir / name).symlink_to(LIB / name)
    admit_watchdog_fixture(libdir)
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
    # A supervised watchdog only restarts enrolled bots. Model the selected
    # unit and its native restart, rather than relying on the retired direct
    # start fallback (which would undo an explicit bot stop).
    for relative in (".config/systemd/user/com.k.b1.service",
                     "Library/LaunchAgents/com.k.b1.plist"):
        unit = tmp_path / relative
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("systemctl", "launchctl"):
        native = bindir / name
        native.write_text(
            '#!/bin/bash\ncase " $* " in\n'
            f'  *" restart "*|*" kickstart "*) echo started >> "{bot}/start-stub.log" ;;\n'
            'esac\nexit 0\n')
        native.chmod(0o755)
    if fresh_marker:
        (bot / "data" / ".last-tool-call").touch()
    (tmp_path / "state" / "plane").mkdir(parents=True)
    (tmp_path / "state" / "plane" / "capture.json").write_text('{"*": "full"}')
    env = {
        **scratch_plane_env(tmp_path, initialize=not disabled),
        "TMUX_BIN": str(tmux),
        "HOME": str(tmp_path),


        "PATH": f"{bindir}:/usr/bin:/bin",
    }
    if armed:
        env["PLANE_EMIT_ENABLED"] = "1"     # ignored since R1; kept for the shape
    if not armed:
        env.pop("PLANE_EMIT_DISABLED")  # validated scratch destination, default-on contract
    if disabled:
        env["PLANE_EMIT_DISABLED"] = "1"
    return libdir, bot, env


def _tick(libdir: Path, bot: Path, env: dict):
    calls = libdir / "admission-fixture.calls"
    before = calls.read_text() if calls.exists() else ""
    result = subprocess.run(
        ["bash", str(libdir / "keepalive.sh"), str(bot)],
        capture_output=True, text=True, env=env, timeout=120)
    assert calls.read_text() == before + "keepalive\n", result.stderr
    return result


def _samples(root: Path):
    _replay_pending(root)
    db = db_path(root)
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT subject_kind, subject_uid, metric, value"
            " FROM metric_samples ORDER BY ingest_seq")]
    except sqlite3.OperationalError:
        return []          # db mid-creation by the background emit
    finally:
        conn.close()


def _replay_pending(root: Path) -> None:
    """Use the daemon's existing raw replay owner before committed-row reads."""
    if not list((root / "state/plane/staged").glob("*.batch")):
        return
    daemon = PlaneDaemon(root)
    try:
        report = daemon._replay_staged()
        assert not report.error and not report.quarantined and not report.spooled, report
    finally:
        daemon.writer.close()


def _wait_samples(root: Path, n: int = 1, timeout: float = 20.0):
    """The emit is backgrounded; poll staging and replay before asserting."""
    deadline = time.monotonic() + timeout
    rows = _samples(root)
    while len(rows) < n and time.monotonic() < deadline:
        time.sleep(0.2)
        rows = _samples(root)
    return rows


def test_idle_tick_records_the_heartbeat_only(tmp_path, *, scratch_plane_env):
    """One sample per live tick (r2 volume fold): session-up-ness is
    derivable from heartbeat presence, so the per-tick session_up=true row
    is gone — pinned, because its return would double the lane."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    rows = _wait_samples(tmp_path)
    assert [s["metric"] for s in rows] == ["bot.heartbeat"]
    hb = json.loads(rows[0]["value"])
    assert hb["state"] == "IDLE"
    assert "marker_age_s" not in hb       # no marker -> no fabricated age
    assert rows[0]["subject_kind"] == "bot_instance"
    assert rows[0]["subject_uid"].startswith("boti_")


def test_busy_marker_tick_carries_marker_age(tmp_path, *, scratch_plane_env):
    libdir, bot, env = _rig(tmp_path, fresh_marker=True, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _wait_samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "BUSY"
    assert isinstance(hb["marker_age_s"], int) and hb["marker_age_s"] >= 0


def test_unknown_pane_records_unknown(tmp_path, *, scratch_plane_env):
    libdir, bot, env = _rig(tmp_path, pane="#### garbage ####", scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _wait_samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "UNKNOWN"



def test_held_pane_records_held_and_types_nothing(tmp_path, *, scratch_plane_env):
    """#2070: a box holding text that was never submitted, with no turn running,
    is HELD. The heartbeat says so, data/.held carries the time it was first
    seen, .idle is not written, and no key goes into the box: a pending reload
    stays pending. The rig runs with no locale set, where the idle pattern
    matched this frame's border bytes and called it IDLE."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    frame = tmp_path / "held-frame.txt"
    frame.write_bytes(
        (REPO / "tests/fixtures/pane-states/input-held-cr.txt").read_bytes())
    sends = tmp_path / "sends.log"
    (tmp_path / "tmux").write_text(
        "#!/bin/bash\ncase \"$*\" in\n"
        "  *has-session*) exit 0 ;;\n"
        f"  *capture-pane*) cat {json.dumps(str(frame))} ;;\n"
        f"  *send-keys*) printf '%s\\n' \"$*\" >> {json.dumps(str(sends))} ;;\n"
        "  *) exit 0 ;;\nesac\n")
    (bot / "data" / ".reload-pending").touch()
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _wait_samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "HELD"
    held = bot / "data" / ".held"
    assert held.is_file() and held.read_text().strip().isdigit()
    assert not (bot / "data" / ".idle").exists()
    assert (bot / "data" / ".reload-pending").exists()
    assert not sends.exists() or not sends.read_text().strip(), sends.read_text()
    assert " HELD " in (bot / "keepalive.log").read_text()

def test_dead_session_records_session_down_and_no_heartbeat(tmp_path, *, scratch_plane_env):
    """The dead path records the one fact it observed (session_up=false)
    and NO heartbeat — no pane was classified, and a fabricated verdict is
    the lie this lane exists to kill. The restart still runs (stub)."""
    libdir, bot, env = _rig(tmp_path, has_session=False, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    rows = _wait_samples(tmp_path)
    assert [s["metric"] for s in rows] == ["bot.session_up"]
    assert json.loads(rows[0]["value"]) is False
    assert (bot / "start-stub.log").exists()   # the restart ladder ran


def test_records_without_any_flag_and_disabled_emits_nothing(tmp_path, *, scratch_plane_env):
    """The always-on contract (F18 closure R1): a tick with NO plane flag in
    its environment records its heartbeat; PLANE_EMIT_DISABLED=1 records
    nothing (the tick itself still runs, rc 0)."""
    libdir, bot, env = _rig(tmp_path, armed=False, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    rows = _wait_samples(tmp_path)
    assert "bot.heartbeat" in [s["metric"] for s in rows]
    d = tmp_path / "disabled"
    d.mkdir()
    libdir2, bot2, env2 = _rig(d, disabled=True, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir2, bot2, env2)
    assert r.returncode == 0, r.stderr
    time.sleep(2)                       # the emit is backgrounded; give a silenced one nothing to do
    assert not db_path(d).is_file()


def test_heartbeat_subject_joins_the_registry_keyframe(tmp_path, *, scratch_plane_env):
    """THE join pin: identity resolution lands the sample on the SAME uid
    the registry keyframes use for this instance — presence joins
    equipment/history with no glue. (bot entity_type -> bot_instance kind,
    same alias, one identity.)"""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
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
    assert len(_wait_samples(tmp_path, n=1)) >= 1
    conn = sqlite3.connect(db_path(tmp_path))
    key_uid = conn.execute(
        "SELECT entity_uid FROM registry_snapshots").fetchone()[0]
    hb_uid = conn.execute(
        "SELECT subject_uid FROM metric_samples"
        " WHERE metric='bot.heartbeat'").fetchone()[0]
    conn.close()
    assert hb_uid == key_uid


def test_future_marker_age_clamps_at_zero(tmp_path, *, scratch_plane_env):
    """r2 (probed): an RTC-skewed future mtime recorded a gigantic
    negative marker_age_s verbatim. Age has floor semantics — clamp at 0,
    never a signed delta."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    marker = bot / "data" / ".last-tool-call"
    marker.touch()
    import os
    future = time.time() + 86400 * 30
    os.utime(marker, (future, future))
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    hb = next(json.loads(s["value"]) for s in _wait_samples(tmp_path)
              if s["metric"] == "bot.heartbeat")
    assert hb["state"] == "BUSY"          # pre-existing marker_age_within
    assert hb["marker_age_s"] == 0


def test_wedged_emit_never_stalls_the_tick(tmp_path, *, scratch_plane_env):
    """The fold's load-bearing property, pinned by TIME: with the emit
    rung wedged (a 60s-sleeping native client, no daemon), the tick must return
    promptly — the watchdog can never wait on a record. The row pins
    cannot see this (a synchronous emit passes them too — caught when the
    unbackground mutation came back green)."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    wedge = tmp_path / "bin/python3"
    wedge.write_text(f'#!/bin/bash\nsleep 60\nexec "{sys.executable}" "$@"\n')
    wedge.chmod(0o755)
    t0 = time.monotonic()
    r = _tick(libdir, bot, env)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert elapsed < 15, f"tick stalled {elapsed:.1f}s behind a wedged emit"


def test_pid_guard_honors_fresh_claims_and_ignores_stale_ones(tmp_path, *, scratch_plane_env):
    """Live-found within minutes of deploy: kill -0 alone let a RE-USED
    pid block a bot's emissions indefinitely (takahashi, pid 1543 held by
    an unrelated long-lived process). The claim is honored only while
    FRESH; a stale pidfile — wedge or reuse alike — admits one new emit."""
    import os
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    holder = subprocess.Popen(["sleep", "300"])
    try:
        pidf = bot / "data" / ".plane-presence.pid"
        pidf.write_text(str(holder.pid))
        r = _tick(libdir, bot, env)          # fresh claim + live pid: skip
        assert r.returncode == 0, r.stderr
        time.sleep(2)
        assert _samples(tmp_path) == []
        old = time.time() - 600
        os.utime(pidf, (old, old))           # stale claim, same live pid
        r2 = _tick(libdir, bot, env)
        assert r2.returncode == 0, r2.stderr
        rows = _wait_samples(tmp_path)
        assert [s["metric"] for s in rows] == ["bot.heartbeat"]
    finally:
        holder.kill()


def test_wedged_emit_is_reaped_at_the_timeout(tmp_path, *, scratch_plane_env):
    """Retro round: under a PERMANENTLY wedged rung the unbounded emit
    made pileup a rate, not a ceiling (~720 stuck procs/day on the
    documented D-state mode). The reaper kills the emit at
    KEEPALIVE_EMIT_TIMEOUT_S; the pin runs a 3s bound against a 300s
    wedge and asserts the wedge process is GONE shortly after."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    wedge = tmp_path / "bin/python3"
    wedge.write_text(f'#!/bin/bash\nsleep 300\nexec "{sys.executable}" "$@"\n')
    wedge.chmod(0o755)
    # 8s, not 3: under battery load the reaper can win the race against
    # the native-client SPAWN itself, failing
    # phase 1 vacuously — measured as a battery-only flake
    env["KEEPALIVE_EMIT_TIMEOUT_S"] = "8"
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr

    def _wedge_alive() -> bool:
        p = subprocess.run(["pgrep", "-f", str(wedge)],
                           capture_output=True, text=True)
        return bool(p.stdout.strip())

    # phase 1: the wedge must APPEAR first — asserting absence from t=0
    # passes vacuously before the native client has spawned it (caught
    # when the remove-the-reaper mutation came back green)
    deadline = time.monotonic() + 15
    while not _wedge_alive() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert _wedge_alive(), "wedge never spawned — cannot exercise the reaper"
    # phase 2: the reaper removes it well before its 300s sleep
    deadline = time.monotonic() + 30
    while _wedge_alive() and time.monotonic() < deadline:
        time.sleep(1)
    assert not _wedge_alive(), "the wedged emit survived its reaper"
