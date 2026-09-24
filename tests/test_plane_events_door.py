"""Cutover Phase B1 → F18 closure R1 — the bot-events ledger
(`data/events/fleet-*.jsonl` per bot, `state/events/` for the fleet) moved to
the plane as a DIRECT MOVE (operator ruling 2026-09-03 — no backward compat,
hard flip, fix forward), and with R1 the file is GONE: `emit_fleet_event`
lands every fleet event as a system event anchored on the bot's actor (or the
fleet) whose detail carries {source, legacy_ts, data}, so the plane re-renders
the legacy row byte for byte, and writes nothing else — waited on, bounded,
a failed or reaped emission disclosed as not recorded. R2b: `claudlobby
events` and brief's alerts read the plane and NOTHING else — no flag, no
declaration, no file; an unreachable plane refuses (rc 3 / the section
omitted), never reads as quiet. fleet-pulse's escalation + summary still ride
the flag and the declaration that once gated them are gone (R2b-2 / R3). (R2a removed the
matcher's legacy side and the shadow;
test_the_events_reader_declares_as_a_direct_move_without_a_gate no longer
contrasts a shadowed reader: every declaration is a direct move now.)

Deleted with the cutover machinery (F18 closure, R3):
test_the_events_reader_declares_as_a_direct_move_without_a_gate (no declaration
exists to record); the `--declared` lookups and the flag loops went with it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.brief import _alerts_section
from claudlobby.commands.events import CRITICAL_TYPES
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import F, REPO, _cli, _env, _scene, _stdlib_readers, ro as _ro
from tests.test_plane_lookup import _run as _lookup

LIB = REPO / "lib"
CLI = Path(sys.executable).parent / "claudlobby"
TODAY = datetime.now().strftime("%Y-%m-%d")          # the door names its file by the LOCAL date
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="fleet-pulse needs tmux")


_N = [0]


def _land(root, bot, etype, ts, data=None, *, source="pulse", provenance=True):
    """One fleet event through `emit_batch`, in the door's exact shape — the
    provenance the door stamps (`fleet-events:sha:<key>`) unless a test lands
    a system event that is NOT a fleet event."""
    kind, subj = ("fleet", F) if bot == "fleet" else ("actor", f"bot:{F}/{bot}")
    _N[0] += 1
    ev = {"event_type": "system", "emitter": source, "fleet": F, "occurred_at": ts,
          **({"source_ref": f"fleet-events:sha:{_N[0]:0>32x}"} if provenance else {}),
          "payload": {"event": etype, "subject_kind": kind, "subject": subj,
                      "data": {"source": source, "legacy_ts": ts, "data": data or {}}}}
    out = emit_batch(root, [ev])
    assert out[0].status == "committed", out
    return out[0].event_id


def _door_env(root, **extra):
    """The e2e battery's no-daemon convention (`_plane_lib`): the shim's socket
    rung fails, disclosed, and the cold CLI ingests — pointed at this scene.
    Built here rather than borrowed: `_plane_lib` lays a stub lib down under
    the scene and the fleet-pulse test lays its own."""
    env = {"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home"), "FLEET_NAME": F,
           "PLANE_EMIT_ENABLED": "1", "PLANE_EMIT_CLI": str(CLI),
           "PLANE_SOCKET": str(root / "no-daemon.sock"), "PATH": "/usr/bin:/bin"}
    env.update(extra)
    return env


def _door(root, args, **extra):
    """The REAL door: lib-common's emit_fleet_event, sourced as a bot script would."""
    return subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; emit_fleet_event {args}'],
                          capture_output=True, text=True, timeout=180, env=_door_env(root, **extra))


def _public(row):
    return _stdlib_readers().public(row)


def _await(root, sql, want, *, timeout=30):
    """The door's emission is waited on (bounded), so the row is normally there
    when the door returns; the poll keeps the fleet-pulse tests honest, whose
    sweep runs several doors."""
    deadline = time.monotonic() + timeout
    while True:
        with _ro(root) as conn:
            got = conn.execute(sql).fetchone()[0]
        if got == want or time.monotonic() > deadline:
            return got
        time.sleep(0.25)


def _wedge(tmp_path, seconds):
    w = tmp_path / "wedge-cli"
    w.write_text(f"#!/bin/bash\nsleep {seconds}\n")
    w.chmod(0o755)
    return w


def _wedge_alive(w):
    return bool(subprocess.run(["pgrep", "-f", str(w)], capture_output=True, text=True).stdout.strip())


def _bot_dir(paths, bot):
    d = paths.runtime_bots / bot
    (d / "data").mkdir(parents=True, exist_ok=True)
    return d


# --- the registry ------------------------------------------------------------

def test_every_critical_type_is_registered_critical():
    """`--critical` on the plane path is the registry-stamped severity, so
    the files' hand list and the registry must agree on every fleet event."""
    for t in CRITICAL_TYPES:
        assert SYSTEM_EVENT_SEVERITY.get(t) == "critical", t
    assert SYSTEM_EVENT_SEVERITY["report_status"] == "notice"


# --- the writer: the real door -----------------------------------------------

def test_the_door_lands_the_event_on_the_plane_and_the_reader_renders_the_legacy_row_back(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    r = _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1')
    assert r.returncode == 0, r.stderr
    assert not (bot_dir / "data" / "events").exists()               # no file, ever (R1)
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        stored = tuple(conn.execute(
            "SELECT severity, subject_kind, subject_alias, subject_uid IS NOT NULL, kind,"
            " source_ref, occurred_at FROM events WHERE event = 'session_missing'").fetchone())
        rows = pr.fleet_events(conn, F)
    assert stored[:5] == ("critical", "actor", f"bot:{F}/w1", 1, "system")
    assert stored[5].startswith("fleet-events:sha:") and len(stored[5]) == len("fleet-events:sha:") + 32
    assert stored[6].endswith("+00:00")                            # stamped UTC; the legacy ts keeps its offset
    legacy = _public(rows[0])                                      # the row as the retired ledger wrote it
    assert (legacy["bot"], legacy["type"], legacy["source"], legacy["data"]) == \
        ("w1", "session_missing", "pulse", {"session": "w1"})
    assert legacy["ts"] and set(legacy) == {"ts", "bot", "type", "source", "data"}
    assert rows[0]["_severity"] == "critical"


def test_a_fleet_level_receipt_anchors_on_the_fleet_and_renders_as_bot_fleet(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _door(root, 'fleet_rescue pulse \'{"rescued":2}\' ""')     # an EMPTY bot_dir: the fleet anchor
    assert r.returncode == 0, r.stderr
    assert not (root / "state" / "events").exists()                # no fleet-level file, ever (R1)
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'fleet_rescue'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        kind, alias = tuple(conn.execute(
            "SELECT subject_kind, subject_alias FROM events WHERE event = 'fleet_rescue'").fetchone())
        rows = pr.fleet_events(conn, F)
    assert (kind, alias) == ("fleet", F)
    legacy = _public(rows[0])
    assert legacy["bot"] == "fleet" and legacy["data"] == {"rescued": 2}


def test_a_timer_run_door_names_its_fleet_from_the_units_carrier(tmp_path):
    """fleet-pulse runs from a timer unit that carries CLAUDLOBBY_FLEET and no
    FLEET_NAME; the door reads the same pair resolve_bots_dir does — measured
    on the live estate: with FLEET_NAME alone the plane branch was skipped
    and a whole sweep's events reached only the JSONL."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    env = _door_env(root, CLAUDLOBBY_FLEET=F)
    env.pop("FLEET_NAME")
    r = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; emit_fleet_event pane_stuck pulse \'{{"s":1}}\' "{bot_dir}" w1'],
                       capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0, r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'pane_stuck'", 1) == 1
    with _ro(root) as conn:
        assert conn.execute("SELECT subject_alias FROM events WHERE event = 'pane_stuck'").fetchone()[0] == f"bot:{F}/w1"


def test_a_nested_fleet_event_never_clobbers_the_callers_own_emission_verdict(tmp_path):
    """report-back emits its report, then sends through pane_send_verified —
    whose send_miss is a fleet event through THIS door — then asks
    plane_write_retired about ITS emission. The door restores the caller's
    PLANE_EMIT_LAST_RC: a failed report emission must not read as recorded
    because a nested fleet event succeeded (the structural lens reproduced a
    report recorded NOWHERE under the first build)."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    prog = (f'. "{LIB}/lib-common.sh"; PLANE_EMIT_LAST_RC=4; emit_fleet_event send_miss pane \'{{}}\' "{bot_dir}" w1;'
            ' echo "outer=$PLANE_EMIT_LAST_RC"')
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=180, env=_door_env(root))
    assert r.returncode == 0 and "outer=4" in r.stdout, r.stdout + r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'send_miss'", 1) == 1


def test_a_wedged_rung_is_waited_on_only_to_the_bound_and_disclosed(tmp_path):
    """The door runs inside every lib/ hot path (the keepalive tick's ERR trap,
    its send verifier), so a wedged rung must never hold it for a minute —
    measured: a synchronous emission held the keepalive tick 60s per fleet
    event. It waits, bounded, and a reaped emission is DISCLOSED as not
    recorded: there is no file to write instead (R1)."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    w = _wedge(tmp_path, 60)
    t0 = time.monotonic()
    r = _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(w), FLEET_EVENT_EMIT_TIMEOUT_S="2")
    elapsed = time.monotonic() - t0
    assert r.returncode == 0 and 2 <= elapsed < 8, (elapsed, r.stderr)
    assert "reaped at 2s" in r.stderr, r.stderr
    # `not recorded` DELIBERATELY no longer asserted -- this line used to pin it,
    # and it was pinning a claim the door cannot make (#1657). The contract note
    # on plane_emit_bounded already said the plane may hold the row after a kill
    # past the commit, so the code contradicted its own comment and the test
    # froze the contradiction. What the door can say is UNKNOWN, plus what to do
    # about it.
    assert "state UNKNOWN" in r.stderr, r.stderr
    assert "Re-emitting is safe" in r.stderr, (
        "an UNKNOWN with no remedy is a worse disclosure than a wrong one: the "
        "pre-minted event id is what makes a retry safe, so say it here")
    assert not (bot_dir / "data" / "events").exists()
    assert not _wedge_alive(w)


def test_a_reaped_emit_is_COUNTED_where_a_health_door_can_find_it(tmp_path):
    """#1657's third defect: a reaped emit's fate was uncounted.

    Both disclosures go to the caller's stderr -- the journal for a timer, a tmux
    pane for a bot session. Measured on one host: 24h of journal showed 3 reaps,
    and that 3 is a FLOOR from a partial channel, because every bot-session reap
    in the same window left no trace anywhere. `plane doctor` printed fourteen
    green rungs over a live reap.

    So the reap appends one row beside the db, where a health door can read it.
    """
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    losses = root / "state" / "plane" / ".emit-losses"
    assert not losses.exists(), "precondition: nothing counted yet"

    w = _wedge(tmp_path, 60)
    r = _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(w), FLEET_EVENT_EMIT_TIMEOUT_S="2")
    assert r.returncode == 0, r.stderr

    assert losses.exists(), (
        "a reaped emit left no durable trace -- which is the whole defect: "
        "stderr is a pane for a bot session and nothing else records it")
    rows = [l for l in losses.read_text().splitlines() if l.strip()]
    assert len(rows) == 1, rows
    fields = rows[0].split("\t")
    assert len(fields) == 4, fields
    assert fields[0].isdigit() and int(fields[0]) > 0, "an epoch, so age is derivable"
    assert fields[1] == "reap", fields
    assert fields[2] == "emit_fleet_event", "the DOOR, so a reader knows what lost it"
    assert not _wedge_alive(w)


def test_the_loss_counter_ages_rows_out_and_keeps_the_recent_ones(tmp_path):
    """Rotation is by AGE, not size: the question it answers is always `how often
    lately`. Asserted both ways in one call, because a rotation that dropped
    everything would satisfy a stale-rows-gone assertion on its own."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    plane_state = root / "state" / "plane"
    plane_state.mkdir(parents=True, exist_ok=True)
    losses = plane_state / ".emit-losses"
    losses.write_text(
        "1\treap\tancient\tbound=10s\n"                      # epoch 1970 — far outside
        f"{int(time.time()) - 60}\treap\trecent\tbound=10s\n"  # a minute ago — inside
    )
    w = _wedge(tmp_path, 60)
    _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1',
          PLANE_EMIT_CLI=str(w), FLEET_EVENT_EMIT_TIMEOUT_S="2")
    text = losses.read_text()
    assert "ancient" not in text, "a row past the 24h window must age out"
    assert "recent" in text, (
        "a row INSIDE the window must survive -- a rotation that truncated "
        "everything would pass the assertion above on its own")
    assert "emit_fleet_event" in text, "and the new reap must still be appended"
    assert not _wedge_alive(w)


def test_a_bot_anchored_door_with_no_fleet_in_its_environment_reads_the_bots_own_conf(tmp_path):
    """A hand-run pre-stop hook carries the bot dir and nothing else: the fleet
    comes from the bot's own bot.conf (plane_peer_fleet's rule), so the row is
    anchored on the bot — measured on the data flip, where nine such hooks
    wrote into a frozen file instead."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    (bot_dir / "bot.conf").write_text(f"export FLEET_NAME={F}\nexport BOT_NAME=w1\n")
    env = _door_env(root); env.pop("FLEET_NAME")
    r = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; emit_fleet_event pane_stuck pulse \'{{"s":1}}\' "{bot_dir}" w1; echo rc=$?'],
                       capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "rc=0" in r.stdout, r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'pane_stuck'", 1) == 1
    with _ro(root) as conn:
        assert tuple(conn.execute("SELECT subject_kind, subject_alias FROM events WHERE event = 'pane_stuck'").fetchone()) == ("actor", f"bot:{F}/w1")


def test_a_host_job_with_no_fleet_anywhere_anchors_on_the_host(tmp_path):
    """disk-monitor, host-health-check, a sibling pull: no fleet, no bot. The
    state/events/ file used to take their receipts; the plane takes them now,
    anchored on the HOST (the probe's convention) — never silence."""
    import socket
    root, paths, _, _ = _scene(tmp_path)
    env = _door_env(root); env.pop("FLEET_NAME")
    r = subprocess.run(["bash", "-c", f'. "{LIB}/lib-common.sh"; emit_fleet_event disk_high disk-monitor \'{{"pct":93}}\' ""; echo rc=$?'],
                       capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "rc=0" in r.stdout, r.stderr
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'disk_high'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        kind, alias = conn.execute("SELECT subject_kind, subject_alias FROM events WHERE event = 'disk_high'").fetchone()
        rows = pr.fleet_events(conn, "_host")
    assert kind == "host" and alias == socket.gethostname()
    legacy = _public(rows[0])
    assert (legacy["bot"], legacy["type"], legacy["source"], legacy["data"]) == ("host", "disk_high", "disk-monitor", {"pct": 93})
    assert not (root / "state" / "events").exists()


# --- the readers follow the flip ---------------------------------------------

def _events_cmd(root, *args, **extra):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", F,
                           "events", *args], capture_output=True, text=True, timeout=180,
                          env=_env(root, **extra))


def _rows(r):
    assert r.returncode == 0, r.stdout + r.stderr
    return [json.loads(line) for line in r.stdout.splitlines()]


def _drop_plane(root):
    for p in (root / "state" / "plane").glob("plane.db*"):
        p.unlink()


def test_claudlobby_events_serves_the_plane_and_nothing_else(tmp_path):
    """No flag, no declaration (F18 closure, R2b): the plane is the only
    source, with no flag and no declaration to consult (the reader was once
    ever declared; the files' bots-dir gate is gone with the files; an
    unreachable plane refuses at rc 3 with nothing on stdout."""
    root, paths, _, _ = _scene(tmp_path)
    assert not paths.runtime_bots.exists()                         # the files' first gate: no longer one
    _land(root, "w1", "session_missing", "2026-09-03T10:00:00Z", {"session": "w1"})
    _land(root, "w2", "keepalive", "2026-09-03T10:01:00Z", {"state": "IDLE"}, source="keepalive")
    _land(root, "fleet", "fleet_rescue", "2026-09-03T10:02:00Z", {"rescued": 1})
    _land(root, "w1", "report_status", "2026-09-03T10:03:00Z", {"status": "completed"},
          source="report-back", provenance=False)                  # a report door's marker: NOT a fleet event
    rows = _rows(_events_cmd(root, "--json"))                      # nothing set: the plane
    assert [(r["bot"], r["type"], r["source"]) for r in rows] == [
        ("w1", "session_missing", "pulse"), ("w2", "keepalive", "keepalive"), ("fleet", "fleet_rescue", "pulse")]
    assert rows[0]["ts"] == "2026-09-03T10:00:00Z" and rows[0]["data"] == {"session": "w1"}
    assert {"cutover_declared", "report_status"}.isdisjoint({r["type"] for r in rows})   # provenance, not a name list
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--critical"))] == ["w1"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--bot", "W2"))] == ["w2"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--type", "fleet_rescue"))] == ["fleet"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--source", "keepalive"))] == ["w2"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--bot", "fleet"))] == ["fleet"]
    table = _events_cmd(root)
    assert table.returncode == 0 and "session_missing" in table.stdout
    _drop_plane(root)
    gone = _events_cmd(root, "--json")
    assert gone.returncode == 3 and "UNREACHABLE" in gone.stderr and gone.stdout == ""


def test_brief_alerts_read_the_plane_and_omit_when_it_is_unreachable(tmp_path, monkeypatch):
    """brief's alerts section rides `plane_events_conn`: the plane whatever
    the retired flag says, and OMITTED with the reason when it cannot be
    reached — never the files (they hold nothing), never a quiet list."""
    root, paths, _, _ = _scene(tmp_path)
    paths.runtime_bots.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    _land(root, "w1", "session_missing", fresh, {"session": "w1"})
    _land(root, "w1", "keepalive", fresh, {"state": "IDLE"}, source="keepalive")   # a notice is never an alert
    _land(root, "w2", "service_down", fresh, {"unit": "x"})                         # another bot's
    degraded = []
    alerts = _alerts_section(paths, "w1", int(now.timestamp()), degraded)
    assert [(a["type"], a["data"]) for a in alerts] == [("session_missing", {"session": "w1"})]
    assert not any(d.field == "alerts" and d.mode == "omitted" for d in degraded)
    _drop_plane(root)
    degraded = []
    assert _alerts_section(paths, "w1", int(now.timestamp()), degraded) == []
    assert any(d.field == "alerts" and d.mode == "omitted"
               and ("unreachable" in d.reason.lower() or "no plane db" in d.reason) for d in degraded), degraded


def test_plane_lookup_answers_the_events_and_escalation_questions(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _land(root, "w1", "session_missing", "2026-09-03T10:00:00Z", {"session": "w1"})
    _land(root, "w1", "session_missing", "2026-09-03T10:05:00Z", {"session": "w1"})
    _land(root, "w2", "service_down", "2026-09-03T10:06:00Z", {"unit": "x"})
    _land(root, "w2", "keepalive", "2026-09-03T10:07:00Z", {"state": "IDLE"}, source="keepalive")
    ev = _lookup(root, "--events", "--fleet", F)
    rows = [json.loads(line) for line in ev.stdout.splitlines()]
    assert ev.returncode == 0 and [(r["bot"], r["type"]) for r in rows] == [
        ("w1", "session_missing"), ("w1", "session_missing"), ("w2", "service_down"), ("w2", "keepalive")]
    assert rows[0] == {"ts": "2026-09-03T10:00:00Z", "bot": "w1", "type": "session_missing",
                       "source": "pulse", "data": {"session": "w1"}}
    assert len(_lookup(root, "--events", "--fleet", F, "--since", "2026-09-03T10:05:00Z").stdout.splitlines()) == 3
    assert len(_lookup(root, "--events", "--fleet", F, "--type", "service_down").stdout.splitlines()) == 1
    assert len(_lookup(root, "--events", "--fleet", F, "--bot", "W1").stdout.splitlines()) == 2
    # the escalation: every CRITICAL (bot, type) at once, the latest per pair,
    # strictly AFTER the window start (the legacy grep's compare), keepalive never
    esc = _lookup(root, "--escalation", "--since", "2026-09-03T10:00:00Z", "--fleet", F)
    assert esc.returncode == 0, esc.stderr
    assert esc.stdout == "w1 session_missing 2026-09-03T10:05:00+00:00\nw2 service_down 2026-09-03T10:06:00+00:00\n"
    at_start = _lookup(root, "--escalation", "--since", "2026-09-03T10:05:00Z", "--fleet", F)
    assert at_start.stdout == "w2 service_down 2026-09-03T10:06:00+00:00\n"     # 10:05:00 itself is NOT after
    one = _lookup(root, "--escalation", "--since", "2026-09-03T10:00:00Z", "--fleet", F, "--type", "service_down")
    assert one.stdout == "w2 service_down 2026-09-03T10:06:00+00:00\n"
    # a NAIVE window start is the host's local clock (fleet-pulse's `date +%Y-%m-%dT%H:%M`):
    # the local rendering of 10:05:30Z must read back as 10:05:30Z — on a host behind
    # UTC a naive-as-UTC reading would open the window hours earlier and admit w1's 10:05
    local_naive = datetime(2026, 9, 3, 10, 5, 30, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    naive = _lookup(root, "--escalation", "--since", local_naive, "--fleet", F)
    assert naive.stdout == "w2 service_down 2026-09-03T10:06:00+00:00\n", (local_naive, naive.stdout)
    assert _stdlib_readers().since_form(local_naive) == "2026-09-03T10:05:30+00:00"
    quiet = _lookup(root, "--escalation", "--since", "2026-09-03T10:07:00Z", "--fleet", F)
    assert quiet.returncode == 0 and quiet.stdout == ""                            # nothing inside the window
    garbage = _lookup(root, "--escalation", "--since", "yesterday-ish", "--fleet", F)
    assert garbage.returncode == 2 and garbage.stdout == "" and "ISO instant" in garbage.stderr
    assert _lookup(root, "--escalation", "--fleet", F).returncode == 2              # needs --since
    assert _lookup(root, "--events").returncode == 2                                # needs --fleet
    never = _lookup(root, "--events", "--fleet", "ghost")
    assert never.returncode == 3 and never.stdout == "" and "no identity for fleet" in never.stderr
    _drop_plane(root)
    gone = _lookup(root, "--events", "--fleet", F)
    assert gone.returncode == 3 and gone.stdout == "" and "unreachable, not empty" in gone.stderr


def test_the_row_renderer_discloses_a_truncated_detail_and_never_strips_another_fleets_alias():
    pr = _stdlib_readers()
    detail = '{"source":"pulse","legacy_ts":"2026-09-03T10:00:00-04:00","data":{"a":1}}'
    whole = pr.legacy_event_row("2026-09-03T14:00:00+00:00", "x", "notice", "actor", f"bot:{F}/w1", detail, 0, F)
    assert whole == {"ts": "2026-09-03T10:00:00-04:00", "bot": "w1", "type": "x", "source": "pulse",
                     "data": {"a": 1}, "_severity": "notice", "_truncated": False}
    assert pr.public(whole) == {"ts": "2026-09-03T10:00:00-04:00", "bot": "w1", "type": "x", "source": "pulse",
                                "data": {"a": 1}}
    cut_off = pr.legacy_event_row("2026-09-03T14:00:00+00:00", "x", None, "actor", "bot:g/w1", detail, 1, F)
    assert cut_off["_truncated"] and cut_off["data"] == {} and cut_off["source"] == "plane"
    assert cut_off["bot"] == "bot:g/w1" and cut_off["ts"] == "2026-09-03T14:00:00Z"
    assert pr.since_form("2026-09-03T10:00:00Z") == "2026-09-03T10:00:00+00:00"
    assert pr.since_form("2026-09-03T06:00:00-04:00") == "2026-09-03T10:00:00+00:00"
    assert pr.since_form(None) is None and pr.since_form("") is None
    with pytest.raises(ValueError):
        pr.since_form("not-an-instant")


# --- fleet-pulse: the escalation and the summary read the plane ---------------

def _pulse_lib(tmp_path, capture):
    """The repo's lib/ with ONE stub: tg-post.sh appends its message to *capture*."""
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for f in LIB.iterdir():
        if f.name != "tg-post.sh":
            (libdir / f.name).symlink_to(f)
    stub = libdir / "tg-post.sh"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$1" >> "{capture}"\n')
    stub.chmod(0o755)
    return libdir


def _pulse(root, libdir, **extra):
    env = _door_env(root, TMUX_TMPDIR=str(root / "tmux"),
                    PATH=os.environ.get("PATH", "/usr/bin:/bin"),
                    FLEET_PULSE_ESCALATION_CHAT_ID="-1001234567890",
                    FLEET_PULSE_ESCALATION_STATE_DIR=str(root / "escalation-sender"),
                    FLEET_PULSE_ESCALATION_THRESHOLD="2", **extra)
    return subprocess.run(["bash", str(libdir / "fleet-pulse.sh"), F], capture_output=True, text=True,
                          timeout=300, env=env)


@needs_tmux
def test_fleet_pulse_escalates_from_the_plane_once_the_files_are_retired(tmp_path):
    """Two declared bots with no tmux session: the sweep emits session_missing
    for both (through the real door, onto the plane) and its escalation must
    find them from the plane under the flip — no file holds anything now, so
    a page can only have come from the plane."""
    root, paths, _, _ = _scene(tmp_path)
    for b in ("w1", "w2"):
        (_bot_dir(paths, b) / "bot.conf").write_text(f"TMUX_SOCKET=b1-none-{b}\n")   # no server anywhere
    (root / "tmux").mkdir()
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    page = "FLEET ALERT: session_missing on 2 bots (w1 w2)."

    after = _pulse(root, libdir)                                     # no flag, no declaration: the plane
    assert after.returncode == 0, after.stderr[-2000:]
    assert page in capture.read_text(), capture.read_text() + after.stderr[-2000:]
    assert not list(paths.runtime_bots.glob("*/data/events/fleet-*.jsonl"))       # no file, ever (R1)
    assert "UNREACHABLE" not in after.stderr
    n = _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 2)
    assert n >= 2, n                                                 # one sweep, two bots, on the plane
    summary = (root / "state" / "pulse" / "pulse-summary.txt").read_text()
    assert "session_missing" in summary and "unknown" not in summary

    # the plane UNREACHABLE under the declared flip: NOT the files (they hold
    # nothing now), not "none" — unknown, disclosed, and paged like the overdue
    # reader. Unreachable, not absent: the sweep's own doors emit before its
    # readers ask, and the cold CLI would re-create an absent db as an EMPTY
    # plane whose "not declared" answer is the files again — so the db path is
    # made unopenable (a directory), the shape a wedged disk presents.
    (root / "state" / "pulse" / "escalation_session_missing").unlink()
    capture.write_text("")
    _drop_plane(root)
    (root / "state" / "plane" / "plane.db").mkdir()
    dark = _pulse(root, libdir)
    assert dark.returncode == 0, dark.stderr[-2000:]
    assert "UNREACHABLE" in dark.stderr and "cannot be judged this pass" in dark.stderr
    # the escalation loop never reaches for a cache no read produced (a read
    # regardless of the verdict would be a bash redirect error on a missing file)
    assert ".critical-window" not in dark.stderr and "No such file" not in dark.stderr
    paged = capture.read_text()
    assert "events reader for f is UNREACHABLE" in paged and page not in paged, paged
    summary = (root / "state" / "pulse" / "pulse-summary.txt").read_text()
    assert "unknown (events reader unreachable)" in summary and " none" not in summary


# --- #1602: the poll floor, and the bound that must survive removing it -------
# The wedged-rung test above asserts `2 <= elapsed` on a child that NEVER exits,
# so it already catches a patch that reaps early. What it cannot see is either
# half of what #1602 is actually about: that the fast path is FAST, and that a
# slow-but-SUCCESSFUL child is not killed. Its child never succeeds, so
# "reaped correctly at the bound" and "reaped a child that would have finished"
# are the same observation to it.

def test_a_fast_emission_does_not_pay_a_full_second(tmp_path):
    """#1602's whole deliverable, and nothing asserted it before.

    `plane_emit_bounded` polled with `sleep 1` while the socket rung answers in
    ~40ms, so the first liveness check landed a full second after the work was
    done — measured 1038ms for a 41ms child, 96% of it asleep. This door runs
    twice per tool call on every bot, so that second was on the session's
    critical path all day.

    Asserts the CEILING only. A floor would pin the poll granularity, which is a
    knob (`PLANE_EMIT_POLL_S`); what must not come back is the second.
    """
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    # A REAL but fast rung, deliberately not PLANE_EMIT_DISABLED=1: that gate is
    # checked by `plane_armed` BEFORE plane_emit_bounded is ever called
    # (lib-common.sh:1904), so a silenced emission never enters the poll loop and
    # an assertion on its duration passes at any granularity. The first cut of
    # this test did exactly that and stayed green with `sleep 1` restored --
    # caught by mutating the floor back in rather than by reading it.
    fast = _wedge(tmp_path, 0)          # a cold rung that exits immediately
    t0 = time.monotonic()
    r = _door(root, f'session_event vitals \'{{"e":"fast"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(fast))
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert elapsed < 0.90, (
        f"a no-op emission took {elapsed:.3f}s — the 1s poll floor is back "
        "(#1602). This door runs twice per tool call on every bot.")


def test_a_slow_but_SUCCESSFUL_emission_is_not_reaped(tmp_path):
    """The bound must be elapsed TIME, not a count of sleeps — the trap that
    makes this change dangerous rather than easy.

    With `_i -lt bound` and `sleep 1`, the bound's duration and its unit were
    the same number by coincidence. Converting the poll to 50ms without
    converting that reaps at a fraction of the bound: measured, the obvious
    `_max=$((bound*20))` + `_ticks+=20` form kills a 1.9s child at ~0.5s. That
    is the COLD RUNG's ordinary cost, taken on ~34% of this estate's day
    whenever the socket breaker is armed — so the "fix" would convert a latency
    defect into silent recording loss, which is strictly worse than the floor.

    A child slower than the poll but WELL INSIDE the bound must therefore come
    back rc=0 and unreaped.
    """
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    slow = _wedge(tmp_path, 2)          # exits on its own, well inside the bound
    t0 = time.monotonic()
    r = _door(root, f'session_event vitals \'{{"e":"slow"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(slow), FLEET_EVENT_EMIT_TIMEOUT_S="10")
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert "reaped" not in r.stderr, (
        f"a 2s child was REAPED under a 10s bound after {elapsed:.2f}s — the "
        "bound is being counted in poll ticks rather than elapsed seconds, so "
        "every cold-rung emission would be killed and reported 'not recorded'.")
    assert elapsed >= 1.8, (
        f"returned in {elapsed:.2f}s without waiting for a 2s child — the door "
        "must WAIT for its emission, or PLANE_EMIT_LAST_RC means nothing.")
    assert not _wedge_alive(slow)


def test_a_cooldown_DIVERSION_is_not_counted_as_a_loss(tmp_path):
    """The decision, pinned — not just the corrected comment (#1657 review).

    An earlier draft of `plane_emit_loss`'s docstring claimed two counted kinds:
    a reap and a cooldown diversion. Review argued the second should not be
    counted at all rather than be wired, and measuring settles it: with the
    wedge marker armed the shim skips the socket, takes the cold rung, and the
    batch COMMITS. Its fate is stated, so counting it in a file named for losses
    would record a success as a loss and a reader would take it for a loss
    series.

    The rule the file actually holds is "an emission whose fate this door cannot
    state". This pins the rule's boundary at the one case most likely to be
    re-added, and it fails if someone wires the diversion in.
    """
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    plane_state = root / "state" / "plane"
    plane_state.mkdir(parents=True, exist_ok=True)
    # Arm the breaker so the emit takes the cooldown branch.
    (plane_state / ".socket-wedged").write_text(str(int(time.time())))

    r = _door(root, f'session_event vitals \'{{"e":"diverted"}}\' "{bot_dir}" w1')
    assert r.returncode == 0, r.stderr
    assert "cooldown" in r.stderr, (
        f"the emit did not take the cooldown branch, so this proves nothing: {r.stderr}")

    # It RECORDED — which is why it is not a loss.
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_event'", 1) == 1, (
        "a diverted emit must still record via the cold rung — if it does not, "
        "the premise for excluding it from the loss count is wrong")
    losses = plane_state / ".emit-losses"
    assert not losses.exists(), (
        "a cooldown diversion was counted as a loss. It records via the cold "
        "rung, so this file would be reporting a success as a loss — see the "
        "rule in plane_emit_loss's docstring")
