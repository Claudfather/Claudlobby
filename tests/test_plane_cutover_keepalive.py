"""Cutover chunk B2 → F18 closure R1 — the keepalive tick's transitions and
the vitals hook go through the one fleet-event door (provenance,
alias-anchored), no per-bot event file is written any more (the reader-less
keepalive-<day>.jsonl and the fleet-<day>.jsonl both went with R1), and
`claudlobby uptime` reads the plane's heartbeat samples + restart transitions
and nothing else (F18 closure R2b — no retirement fact, no log; refuses when
the plane cannot answer). Deleted with the log parser:
test_uptime_from_the_plane_equals_uptime_from_the_log (its plane half lives on
as test_uptime_metrics_from_the_plane); test_cmd_uptime_reads_the_plane_once_the
_events_write_is_retired became test_cmd_uptime_reads_the_plane_and_refuses_without_it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.uptime import compute_metrics, entries_from_plane
from tests.plane_fixtures import _stdlib_readers
from tests.test_plane_keepalive_door import CLI, LIB, _rig, _tick

FLEET = "kfleet"
TODAY = datetime.now().strftime("%Y-%m-%d")


def _manifest(root: Path):
    (root / "local" / FLEET).mkdir(parents=True, exist_ok=True)
    (root / "local" / FLEET / "fleet.yaml").write_text(
        f"fleet:\n  name: {FLEET}\n  service_prefix: com.k\n  bots:\n    b1:\n      expertise: [software-engineering]\n")
    if not (root / "lib").exists():
        (root / "lib").symlink_to(LIB)


def _cli(root: Path, *args, env=None):
    base = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", FLEET, *args],
                          capture_output=True, text=True, timeout=180, env={**base, **(env or {})})


def _await(root: Path, sql: str, want, *, timeout=30):
    """The tick's plane emission is DETACHED (the cold CLI lands the row in the
    background), so the db may not even exist when the tick returns: poll
    without creating it (a `connect` would mint an empty, schema-less file
    ahead of the CLI) and read a missing table as nothing yet."""
    import sqlite3
    deadline = time.monotonic() + timeout
    while True:
        got = None
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

def test_a_dead_session_restart_lands_as_a_fleet_event_and_no_file_is_written(tmp_path):
    """The tick under a dead session restarts the bot (the start-bot stub) and
    the RESTART transition is a `keepalive_restart` fleet event on the plane
    with provenance; no keepalive-<day>.jsonl, no fleet-<day>.jsonl (R1)."""
    libdir, bot, env = _rig(tmp_path, has_session=False)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert (bot / "start-stub.log").exists()                                       # restarted through the stub
    assert not list((bot / "data").glob("events/*.jsonl"))                         # no file, ever
    assert _await(tmp_path, "SELECT COUNT(*) FROM events WHERE event = 'keepalive_restart'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        ref, alias, sev = conn.execute(
            "SELECT source_ref, subject_alias, severity FROM events WHERE event = 'keepalive_restart'").fetchone()
    assert ref.startswith("fleet-events:sha:") and alias == f"bot:{FLEET}/b1" and sev == "notice"


def test_an_idle_tick_lands_no_fleet_event_the_heartbeat_carries_the_verdict(tmp_path):
    libdir, bot, env = _rig(tmp_path)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert _await(tmp_path, "SELECT COUNT(*) FROM metric_samples WHERE metric = 'bot.heartbeat'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE source_ref LIKE 'fleet-events:%'").fetchone()[0] == 0
    assert not list((bot / "data").glob("events/*.jsonl"))                         # no file, ever (R1)


# --- the vitals hook ---------------------------------------------------------------

def test_the_vitals_hook_lands_its_events_through_the_door(tmp_path):
    libdir, bot, env = _rig(tmp_path)
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


def test_cmd_uptime_reads_the_plane_and_refuses_without_it(tmp_path):
    root = tmp_path
    _manifest(root)
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    bot = root / "local" / FLEET / "runtime" / "bots" / "b1"
    bot.mkdir(parents=True); (bot / "bot.conf").write_text('BOT_NAME="b1"\n')
    now = datetime.now(timezone.utc)
    emit_batch(root, [{"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET,
                       "occurred_at": (now - timedelta(minutes=m)).isoformat(),
                       "payload": {"subject_kind": "bot_instance", "subject": f"bot:{FLEET}/b1", "metric": "bot.heartbeat",
                                   "value": {"state": "IDLE"}}} for m in (3, 2, 1)])
    served = _cli(root, "uptime", "--json", "--window", "24h")
    assert served.returncode == 0, served.stderr
    assert json.loads(served.stdout)["b1"]["24h"]["entries_in_window"] == 3           # the plane, no flag, no fact
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    refused = _cli(root, "uptime", "--json", "--window", "24h")
    assert refused.returncode == 3 and refused.stdout == "", (refused.returncode, refused.stdout)
    assert "UNREACHABLE" in refused.stderr and "plane.db" in refused.stderr             # the remedy, never an empty table
