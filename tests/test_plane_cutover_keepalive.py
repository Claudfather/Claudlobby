"""Cutover chunk B2 → F18 closure R1 — the keepalive tick's transitions and
the vitals hook go through the one fleet-event door (provenance,
alias-anchored), no per-bot event file is written any more (the reader-less
keepalive-<day>.jsonl and the fleet-<day>.jsonl both went with R1), and
`claudlobby fleet uptime` reads the plane's heartbeat samples + restart transitions
and nothing else (F18 closure R2b — no retirement fact, no log; refuses when
the plane cannot answer). The old log parser's plane half remains in
test_uptime_metrics_from_the_plane; the public operation is checked by
test_fleet_uptime_reads_the_plane_and_refuses_without_it.
"""
from __future__ import annotations

from tests.plane_setup import initialize_plane

import json
import pytest
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.uptime import compute_metrics, entries_from_plane
from tests.plane_fixtures import _stdlib_readers
from tests.test_plane_keepalive_door import CLI, LIB, _replay_pending, _rig, _tick

FLEET = "kfleet"
TODAY = datetime.now().strftime("%Y-%m-%d")


def _manifest(root: Path):
    (root / "local" / FLEET).mkdir(parents=True, exist_ok=True)
    (root / "local" / FLEET / "fleet.yaml").write_text(
        f"fleet:\n  name: {FLEET}\n  manager: b1\n  service_prefix: com.k\n  bots:\n    b1:\n      expertise: [software-engineering]\n")
    if not (root / "lib").exists():
        (root / "lib").symlink_to(LIB)


def _cli(root: Path, *args, env=None):
    base = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", FLEET, *args],
                          capture_output=True, text=True, timeout=180, env={**base, **(env or {})})


def _await(root: Path, sql: str, want, *, timeout=30):
    """The tick's emission is detached; replay its raw stage before reading."""
    import sqlite3
    deadline = time.monotonic() + timeout
    while True:
        got = None
        _replay_pending(root)
        if db_path(root).exists():
            try:
                with connect(db_path(root)) as conn:
                    got = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                got = None
        if got == want or time.monotonic() > deadline:
            return got
        time.sleep(0.25)


# --- the keepalive tick ------------------------------------------------------------

def test_a_dead_session_restart_lands_as_a_fleet_event_and_no_file_is_written(tmp_path, *, scratch_plane_env):
    """The tick under a dead session restarts the bot (the start-bot stub) and
    the RESTART transition is a `keepalive_restart` fleet event on the plane
    with provenance; no keepalive-<day>.jsonl, no fleet-<day>.jsonl (R1)."""
    libdir, bot, env = _rig(tmp_path, has_session=False, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert (bot / "start-stub.log").exists()                                       # restarted through the stub
    assert not list((bot / "data").glob("events/*.jsonl"))                         # no file, ever
    assert _await(tmp_path, "SELECT COUNT(*) FROM events WHERE event = 'keepalive_restart'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        ref, alias, sev = conn.execute(
            "SELECT source_ref, subject_alias, severity FROM events WHERE event = 'keepalive_restart'").fetchone()
    assert ref.startswith("fleet-events:sha:") and alias == f"bot:{FLEET}/b1" and sev == "notice"


def test_an_idle_tick_lands_no_fleet_event_the_heartbeat_carries_the_verdict(tmp_path, *, scratch_plane_env):
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert _await(tmp_path, "SELECT COUNT(*) FROM metric_samples WHERE metric = 'bot.heartbeat'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE source_ref LIKE 'fleet-events:%'").fetchone()[0] == 0
    assert not list((bot / "data").glob("events/*.jsonl"))                         # no file, ever (R1)


# --- the vitals hook ---------------------------------------------------------------

def test_the_vitals_hook_lands_its_events_through_the_door(tmp_path, *, scratch_plane_env):
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    (libdir / "bot-vitals.sh").symlink_to(LIB / "bot-vitals.sh")
    env = {**env, "BOT_DIR": str(bot), "BOT_ID": "b1", "FLEET_NAME": FLEET}
    payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read", "session_id": "s-1"})
    r = subprocess.run(["bash", str(libdir / "bot-vitals.sh")], input=payload, capture_output=True, text=True,
                       timeout=180, env=env)
    assert r.returncode == 0, r.stderr
    assert (bot / "data" / ".last-tool-call").exists()                                # the activity marker
    assert not list((bot / "data").glob("events/*.jsonl"))                             # no file, ever (R1)
    assert _await(tmp_path, "SELECT COUNT(*) FROM events WHERE event = 'tool_call'", 1) == 1
    pr = _stdlib_readers()
    with connect(db_path(tmp_path)) as conn:
        rows = pr.fleet_events(conn, FLEET)
    row = pr.public(rows[0])                                                            # the row as the ledger wrote it
    assert (row["bot"], row["type"], row["source"], row["data"]) == \
        ("b1", "tool_call", "vitals", {"tool": "Read", "event": "PostToolUse", "session": "s-1"})


@pytest.mark.parametrize("bot_dir", [None, "", "runtime/bots/b1"], ids=["unset", "empty", "relative"])
def test_the_vitals_hook_never_falls_back_to_the_cwd(tmp_path, bot_dir, *, scratch_plane_env):
    """#874: with no usable BOT_DIR (unset, empty or relative) the hook writes no marker into the dir it runs
    in (a bot's cwd is its project checkout) and records no row against a bot it cannot name. It exits 0."""
    libdir, bot, env = _rig(tmp_path, scratch_plane_env=scratch_plane_env)
    (libdir / "bot-vitals.sh").symlink_to(LIB / "bot-vitals.sh")
    env = {k: v for k, v in {**env, "BOT_ID": "b1", "FLEET_NAME": FLEET}.items() if k != "BOT_DIR"}
    if bot_dir is not None:
        env["BOT_DIR"] = bot_dir
    cwd = tmp_path / "project-checkout"
    (cwd / "data").mkdir(parents=True)
    (cwd / "runtime" / "bots" / "b1" / "data").mkdir(parents=True)   # a relative BOT_DIR would resolve here
    payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Read", "session_id": "s-1"})
    r = subprocess.run(["bash", str(libdir / "bot-vitals.sh")], input=payload, capture_output=True, text=True,
                       timeout=180, env=env, cwd=cwd)
    assert r.returncode == 0, r.stderr
    assert "recording nothing" in r.stderr
    assert not list(cwd.rglob(".last-tool-call"))
    assert not (bot / "data" / ".last-tool-call").exists()
    # The hook has exited; replay anything it staged, then read once: an emit that slipped through is counted.
    _replay_pending(tmp_path)
    if db_path(tmp_path).exists():
        with connect(db_path(tmp_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM events WHERE event = 'tool_call'").fetchone()[0] == 0
    assert not list((tmp_path / "state" / "plane").rglob("*.batch"))


# --- uptime ------------------------------------------------------------------------

def test_uptime_metrics_from_the_plane(tmp_path):
    """The (instant, state) pairs the metrics consume: heartbeat samples for the
    verdicts, a `keepalive_restart` fleet event for the RESTART, the dead
    session as `bot.session_up` false (DOWN — no uptime)."""
    root = tmp_path
    _manifest(root)
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    t = lambda m: (now - timedelta(minutes=m))
    pairs, samples = [], []
    for m, state in ((50, "IDLE"), (49, "IDLE"), (48, "BUSY"), (47, "BUSY"), (46, "RESTART"), (45, "IDLE"), (44, "UNKNOWN"), (43, "IDLE")):
        pairs.append((t(m), state))
        subj = f"bot:{FLEET}/b1"
        if state == "RESTART":
            samples.append({"event_type": "system", "emitter": "keepalive", "fleet": FLEET, "occurred_at": t(m).isoformat(),
                            "source_ref": f"fleet-events:sha:{m:0>32x}",
                            "payload": {"event": "keepalive_restart", "subject_kind": "actor", "subject": subj,
                                        "data": {"source": "keepalive", "legacy_ts": t(m).isoformat(), "data": {"detail": "d"}}}})
        else:
            samples.append({"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET, "occurred_at": t(m).isoformat(),
                            "payload": {"subject_kind": "bot_instance", "subject": subj, "metric": "bot.heartbeat",
                                        "value": {"state": state}}})
    samples.append({"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET,
                    "occurred_at": (now - timedelta(minutes=46, seconds=30)).isoformat(),
                    "payload": {"subject_kind": "bot_instance", "subject": f"bot:{FLEET}/b1",
                                "metric": "bot.session_up", "value": False}})
    initialize_plane(root)
    out = emit_batch(root, samples)
    assert all(o.status == "committed" for o in out), out
    pr = _stdlib_readers()
    with connect(db_path(root)) as conn:
        plane_entries = entries_from_plane(pr, conn, FLEET, "B1", (now - timedelta(days=30)).isoformat())   # case-insensitive alias
    assert [st for _, st in plane_entries].count("DOWN") == 1
    assert [(ts, st) for ts, st in plane_entries if st != "DOWN"] == pairs                 # the recorded pairs, in time order
    expected = compute_metrics(pairs, timedelta(hours=24), now=now)
    from_plane = compute_metrics([e for e in plane_entries if e[1] != "DOWN"], timedelta(hours=24), now=now)
    assert from_plane == expected
    assert from_plane["restart_count"] == 1 and from_plane["entries_in_window"] == 8
    with_down = compute_metrics(plane_entries, timedelta(hours=24), now=now)
    assert with_down["uptime_pct"] <= expected["uptime_pct"] and with_down["restart_count"] == 1   # DOWN adds no uptime


def test_fleet_uptime_reads_the_plane_and_refuses_without_it(tmp_path):
    root = tmp_path
    _manifest(root)
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    bot = root / "local" / FLEET / "runtime" / "bots" / "b1"
    bot.mkdir(parents=True); (bot / "bot.conf").write_text('BOT_NAME="b1"\n')
    now = datetime.now(timezone.utc)
    initialize_plane(root)
    emit_batch(root, [{"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET,
                       "occurred_at": (now - timedelta(minutes=m)).isoformat(),
                       "payload": {"subject_kind": "bot_instance", "subject": f"bot:{FLEET}/b1", "metric": "bot.heartbeat",
                                   "value": {"state": "IDLE"}}} for m in (3, 2, 1)])
    served = _cli(root, "fleet", "uptime", "--json", "--window", "24h")
    assert served.returncode == 0, served.stderr
    served_result = json.loads(served.stdout)
    assert served_result["schema_version"] == 1 and served_result["command"] == "fleet.uptime"
    assert served_result["ok"] is True
    assert served_result["data"]["bots"]["b1"]["24h"]["entries_in_window"] == 3  # the plane, no flag, no fact
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    refused = _cli(root, "fleet", "uptime", "--json", "--window", "24h")
    refused_result = json.loads(refused.stdout)
    assert refused.returncode == 6 and refused_result["ok"] is False, (refused.returncode, refused.stdout)
    assert refused_result["command"] == "fleet.uptime" and refused_result["error"]["code"] == "unavailable"
    assert refused_result["data"] == {} and "plane.db" in refused_result["error"]["message"]  # never an empty table


def test_fleet_uptime_says_what_it_measured_even_with_no_rows(tmp_path):
    """#891: with no bot rows the text path still prints its coverage line (the
    `No bots found` path #1742's review noted is gone; this pins it), and the JSON
    says what `uptime_pct` divides by. The plane holds the fleet (a plane that
    never saw it is refused as the wrong root); the runtime has no bot dirs."""
    root = tmp_path
    _manifest(root)
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    (root / "local" / FLEET / "runtime" / "bots").mkdir(parents=True)
    initialize_plane(root)
    emit_batch(root, [{"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET,
                       "occurred_at": datetime.now(timezone.utc).isoformat(),
                       "payload": {"subject_kind": "bot_instance", "subject": f"bot:{FLEET}/b1",
                                   "metric": "bot.heartbeat", "value": {"state": "IDLE"}}}])
    text = _cli(root, "fleet", "uptime", "--window", "24h")
    assert text.returncode == 0, text.stderr
    assert "coverage:" in text.stdout
    served = _cli(root, "fleet", "uptime", "--json", "--window", "24h")
    result = json.loads(served.stdout)
    assert result["ok"] is True and result["data"]["bots"] == {}, served.stdout
    assert "observed" in result["data"]["meaning"]
