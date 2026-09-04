"""Cutover chunk B2 — the per-bot event files that never went through
`emit_fleet_event`: the keepalive tick's transitions and the vitals hook go
through the one fleet-event door (provenance, alias-anchored, retired with the
family), the reader-less keepalive-<day>.jsonl stops with the retirement, and
`claudlobby uptime` reads the plane's heartbeat samples + restart transitions
once the events write is retired — the same arithmetic over the same pairs.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane import shadow as sh
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.uptime import compute_metrics, entries_from_plane, parse_keepalive_log
from tests.test_plane_cutover_flip import _stdlib_readers
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


def _retire(root: Path):
    for reader in sh.GATED:
        r = _cli(root, "plane", "cutover", "--reader", reader, "--force", "ruling")
        assert r.returncode == 0, r.stdout + r.stderr
    r = _cli(root, "plane", "cutover", "--retire-writes")
    assert r.returncode == 0, r.stdout + r.stderr


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

def test_a_dead_session_restart_lands_as_a_fleet_event_and_the_reader_less_file_follows_the_flag(tmp_path):
    """The tick under a dead session restarts the bot (the start-bot stub) and
    the RESTART transition is a `keepalive_restart` fleet event on the plane
    with provenance; under dual-write the legacy keepalive-<day>.jsonl and the
    fleet-<day>.jsonl lines are still written, under the flag at 0 the
    reader-less keepalive file is not (the fleet line keeps the four facts)."""
    libdir, bot, env = _rig(tmp_path, has_session=False)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert (bot / "start-stub.log").exists()                                       # restarted through the stub
    kfile = bot / "data" / "events" / f"keepalive-{TODAY}.jsonl"
    assert kfile.exists() and '"state":"RESTART"' in kfile.read_text()             # dual-write: the old file
    ffile = bot / "data" / "events" / f"fleet-{TODAY}.jsonl"
    assert ffile.exists() and '"type":"keepalive_restart"' in ffile.read_text()    # the fleet line (four facts: written)
    assert _await(tmp_path, "SELECT COUNT(*) FROM events WHERE event = 'keepalive_restart'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        ref, alias, sev = conn.execute(
            "SELECT source_ref, subject_alias, severity FROM events WHERE event = 'keepalive_restart'").fetchone()
    assert ref.startswith("fleet-events:sha:") and alias == f"bot:{FLEET}/b1" and sev == "notice"
    # the flag at 0: the keepalive file is NOT written; nothing retired the fleet line, so it still is
    (tmp_path / "second").mkdir()
    libdir2, bot2, env2 = _rig(tmp_path / "second", has_session=False)
    env2["PLANE_LEGACY_WRITE_EVENTS"] = "0"
    r = _tick(libdir2, bot2, env2)
    assert r.returncode == 0, r.stderr
    assert not (bot2 / "data" / "events" / f"keepalive-{TODAY}.jsonl").exists()
    assert (bot2 / "data" / "events" / f"fleet-{TODAY}.jsonl").exists()


def test_an_idle_tick_lands_no_fleet_event_the_heartbeat_carries_the_verdict(tmp_path):
    libdir, bot, env = _rig(tmp_path)
    r = _tick(libdir, bot, env)
    assert r.returncode == 0, r.stderr
    assert _await(tmp_path, "SELECT COUNT(*) FROM metric_samples WHERE metric = 'bot.heartbeat'", 1) == 1
    with connect(db_path(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE source_ref LIKE 'fleet-events:%'").fetchone()[0] == 0
    assert not (bot / "data" / "events" / f"fleet-{TODAY}.jsonl").exists()
    assert '"state":"IDLE"' in (bot / "data" / "events" / f"keepalive-{TODAY}.jsonl").read_text()   # dual-write keeps the old file


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
    line = (bot / "data" / "events" / f"fleet-{TODAY}.jsonl").read_text().strip()   # dual-write: the legacy row
    row = json.loads(line)
    assert (row["bot"], row["type"], row["source"], row["data"]) == \
        ("b1", "tool_call", "vitals", {"tool": "Read", "event": "PostToolUse", "session": "s-1"})
    assert _await(tmp_path, "SELECT COUNT(*) FROM events WHERE event = 'tool_call'", 1) == 1
    pr = _stdlib_readers()
    with connect(db_path(tmp_path)) as conn:
        rows = pr.fleet_events(conn, FLEET)
    assert [pr.public(x) for x in rows] == [row]                                    # byte for byte the legacy row


# --- uptime ------------------------------------------------------------------------

def test_uptime_from_the_plane_equals_uptime_from_the_log(tmp_path):
    """The same (instant, state) pairs on both sides: heartbeat samples for the
    verdicts, a `keepalive_restart` fleet event for the RESTART, the dead
    session as `bot.session_up` false (no uptime, like the log's gap)."""
    root = tmp_path
    _manifest(root)
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    bot = root / "local" / FLEET / "runtime" / "bots" / "b1"
    bot.mkdir(parents=True)
    now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    t = lambda m: (now - timedelta(minutes=m))
    log_lines, samples = [], []
    for m, state in ((50, "IDLE"), (49, "IDLE"), (48, "BUSY"), (47, "BUSY"), (46, "RESTART"), (45, "IDLE"), (44, "UNKNOWN"), (43, "IDLE")):
        log_lines.append(f"{t(m).isoformat()} {state} — detail")
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
    (bot / "keepalive.log").write_text("\n".join(log_lines) + "\n")
    # the dead-session fact the log never writes (its gap IS the fact): counts
    # as no uptime on the plane side too, so the metrics stay equal
    samples.append({"event_type": "metric_sample", "emitter": "keepalive", "fleet": FLEET,
                    "occurred_at": (now - timedelta(minutes=46, seconds=30)).isoformat(),
                    "payload": {"subject_kind": "bot_instance", "subject": f"bot:{FLEET}/b1",
                                "metric": "bot.session_up", "value": False}})
    out = emit_batch(root, samples)
    assert all(o.status == "committed" for o in out), out
    from_log = compute_metrics(parse_keepalive_log(bot / "keepalive.log"), timedelta(hours=24), now=now)
    pr = _stdlib_readers()
    with connect(db_path(root)) as conn:
        plane_entries = entries_from_plane(pr, conn, FLEET, "B1", (now - timedelta(days=30)).isoformat())   # case-insensitive alias
    assert [st for _, st in plane_entries].count("DOWN") == 1
    from_plane = compute_metrics([e for e in plane_entries if e[1] != "DOWN"], timedelta(hours=24), now=now)
    assert from_plane == from_log
    assert from_plane["restart_count"] == 1 and from_plane["entries_in_window"] == 8
    with_down = compute_metrics(plane_entries, timedelta(hours=24), now=now)
    assert with_down["uptime_pct"] <= from_log["uptime_pct"] and with_down["restart_count"] == 1   # DOWN adds no uptime


def test_cmd_uptime_reads_the_plane_once_the_events_write_is_retired(tmp_path):
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
    before = _cli(root, "uptime", "--json", "--window", "24h")
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout)["b1"]["24h"]["entries_in_window"] == 0          # no log: nothing (not retired)
    _retire(root)
    after = _cli(root, "uptime", "--json", "--window", "24h")
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout)["b1"]["24h"]["entries_in_window"] == 3           # retired: the plane
    assert "may be stale" not in after.stderr
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()
    unknown = _cli(root, "uptime", "--json", "--window", "24h")
    assert unknown.returncode == 0 and "may be stale" in unknown.stderr               # the fact cannot be read: the log, LABELED
    assert json.loads(unknown.stdout)["b1"]["24h"]["entries_in_window"] == 0
