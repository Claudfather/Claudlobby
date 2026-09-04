"""Cutover Phase B1 — the bot-events ledger (`data/events/fleet-*.jsonl` per
bot, `state/events/` for the fleet) moves to the plane as a DIRECT MOVE: no
shadow (operator ruling 2026-09-03 — no backward compat, hard flip, fix
forward). `emit_fleet_event` lands every fleet event as a system event anchored
on the bot's actor (or the fleet) whose detail carries {source, legacy_ts,
data}, so the plane re-renders the legacy row byte for byte; the JSONL append
retires on the same four facts as the other doors. The readers — `claudlobby
events`, brief's alerts, fleet-pulse's escalation + summary — follow the flip:
PLANE_READ_EVENTS=1 AND a recorded `cutover_declared` for `events`; a flag
alone is disclosed; an unreachable plane refuses, never reads as quiet.
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
from claudlobby.plane import cutover as cut, shadow as sh
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import _cli, _declare, _env, _stdlib_readers
from tests.test_plane_lookup import _run as _lookup
from tests.test_plane_shadow import F, REPO, _scene

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
    """Under dual-write the door's plane emission is DETACHED (the ledger line
    is the record; the row lands when the cold CLI lands it), so a test reads
    the plane after the row is there, never at the instant the door returned."""
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
    ledger = bot_dir / "data" / "events" / f"fleet-{TODAY}.jsonl"
    legacy = json.loads(ledger.read_text().strip())                # still appended: nothing retired
    assert (legacy["bot"], legacy["type"], legacy["source"], legacy["data"]) == \
        ("w1", "session_missing", "pulse", {"session": "w1"})
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
    assert [_public(x) for x in rows] == [legacy]                  # byte for byte the legacy row
    assert rows[0]["_severity"] == "critical"


def test_a_fleet_level_receipt_anchors_on_the_fleet_and_renders_as_bot_fleet(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _door(root, 'fleet_rescue pulse \'{"rescued":2}\' ""')     # an EMPTY bot_dir: the fleet ledger
    assert r.returncode == 0, r.stderr
    legacy = json.loads((root / "state" / "events" / f"fleet-{TODAY}.jsonl").read_text().strip())
    assert legacy["bot"] == "fleet" and legacy["data"] == {"rescued": 2}
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'fleet_rescue'", 1) == 1
    pr = _stdlib_readers()
    with _ro(root) as conn:
        kind, alias = tuple(conn.execute(
            "SELECT subject_kind, subject_alias FROM events WHERE event = 'fleet_rescue'").fetchone())
        rows = pr.fleet_events(conn, F)
    assert (kind, alias) == ("fleet", F)
    assert [_public(x) for x in rows] == [legacy]


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


def test_the_append_retires_only_on_the_four_facts(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    ledger = bot_dir / "data" / "events" / f"fleet-{TODAY}.jsonl"
    call = f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1'
    lines = lambda: ledger.read_text().count("\n") if ledger.exists() else 0

    # flag 0 with NO recorded retirement: the ledger is written and the gap named
    r = _door(root, call, PLANE_LEGACY_WRITE_EVENTS="0")
    assert "no legacy_write_retired is recorded" in r.stderr and lines() == 1
    for reader in sh.GATED:
        _declare(root, reader)
    # a retirement from BEFORE this door existed (chunk 6b's names dispatch and
    # report) covers nothing it never named: the door keeps writing, and
    # --retire-writes records the EXTENSION rather than "already retired"
    old = cut.retirement_event(F, {}, "2026-09-03T00:00:00+00:00")
    old["payload"]["data"]["flags"].pop("events")
    assert emit_batch(root, [old])[0].status == "committed"
    r = _door(root, call, PLANE_LEGACY_WRITE_EVENTS="0")
    assert "that covers 'events'" in r.stderr and lines() == 2
    ext = _cli(root, "cutover", "--retire-writes")
    assert ext.returncode == 0 and "recording the extension" in ext.stdout, ext.stdout + ext.stderr
    assert "['dispatch', 'report']" in ext.stdout and "['events']" in ext.stdout
    # all four facts: flag 0, armed, retired (covering this door), THIS emission recorded -> skipped
    r = _door(root, call, PLANE_LEGACY_WRITE_EVENTS="0")
    assert "legacy write retired" in r.stderr and lines() == 2
    # the fourth fact fails (the plane did not record this one) -> written
    r = _door(root, call, PLANE_LEGACY_WRITE_EVENTS="0", PLANE_EMIT_CLI="/usr/bin/false")
    assert "did not record this one" in r.stderr and lines() == 3
    # unarmed: the plane is never touched, the ledger is the record
    r = _door(root, call, PLANE_LEGACY_WRITE_EVENTS="0", PLANE_EMIT_ENABLED="0")
    assert r.stderr == "" and lines() == 4
    # the flag is the operator's: default 1 keeps writing after the retirement too
    r = _door(root, call)
    assert lines() == 5
    assert _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 4) == 4


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


def test_under_dual_write_a_wedged_rung_never_holds_the_door(tmp_path):
    """The door runs inside every lib/ hot path (the keepalive tick's ERR trap,
    its send verifier); under dual-write the ledger line IS the record, so the
    plane emission is detached and reaped at the tick's bound — measured: a
    synchronous emission held the keepalive tick 60s per fleet event behind a
    wedged rung, failing the tick's own 15s pin."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    w = _wedge(tmp_path, 60)
    t0 = time.monotonic()
    r = _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(w), KEEPALIVE_EMIT_TIMEOUT_S="3")
    elapsed = time.monotonic() - t0
    assert r.returncode == 0 and elapsed < 5, (elapsed, r.stderr)
    assert "session_missing" in (bot_dir / "data" / "events" / f"fleet-{TODAY}.jsonl").read_text()
    deadline = time.monotonic() + 15                                  # the wedge appears, then is reaped
    while not _wedge_alive(w) and time.monotonic() < deadline:
        time.sleep(0.25)
    assert _wedge_alive(w), "the detached emission never spawned the rung"
    deadline = time.monotonic() + 15
    while _wedge_alive(w) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not _wedge_alive(w), "the wedged rung outlived the reaper"


def test_under_the_retirement_a_wedged_rung_is_waited_on_only_to_the_bound(tmp_path):
    """With the write retired the door needs its fourth fact, so it waits —
    bounded: a reaped emission is 'not recorded' and the ledger is written."""
    root, paths, _, _ = _scene(tmp_path)
    bot_dir = _bot_dir(paths, "w1")
    for reader in sh.GATED:
        _declare(root, reader)
    assert _cli(root, "cutover", "--retire-writes").returncode == 0
    w = _wedge(tmp_path, 60)
    t0 = time.monotonic()
    r = _door(root, f'session_missing pulse \'{{"session":"w1"}}\' "{bot_dir}" w1',
              PLANE_EMIT_CLI=str(w), PLANE_LEGACY_WRITE_EVENTS="0", FLEET_EVENT_EMIT_TIMEOUT_S="2")
    elapsed = time.monotonic() - t0
    assert r.returncode == 0 and 2 <= elapsed < 8, (elapsed, r.stderr)
    assert "reaped at 2s" in r.stderr and "did not record this one" in r.stderr, r.stderr
    assert "session_missing" in (bot_dir / "data" / "events" / f"fleet-{TODAY}.jsonl").read_text()
    assert not _wedge_alive(w)


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


def test_claudlobby_events_serves_the_plane_on_flag_and_declaration(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    paths.runtime_bots.mkdir(parents=True)                         # the command's first gate
    _land(root, "w1", "session_missing", "2026-09-03T10:00:00Z", {"session": "w1"})
    _land(root, "w2", "keepalive", "2026-09-03T10:01:00Z", {"state": "IDLE"}, source="keepalive")
    _land(root, "fleet", "fleet_rescue", "2026-09-03T10:02:00Z", {"rescued": 1})
    _land(root, "w1", "report_status", "2026-09-03T10:03:00Z", {"status": "completed"},
          source="report-back", provenance=False)                  # a report door's marker: NOT a fleet event
    off = _events_cmd(root, "--json")                              # no flag: the files (none readable -> rc 1)
    assert off.returncode == 1 and off.stdout == "" and "no event source was readable" in off.stderr
    flag_only = _events_cmd(root, "--json", PLANE_READ_EVENTS="1")
    assert flag_only.returncode == 1 and flag_only.stdout == ""    # a flag alone changes nothing
    assert "no cutover_declared" in flag_only.stderr
    _declare(root, "events")
    for off_value in ("0", "true", "yes"):                          # a declaration alone changes nothing either
        r = _events_cmd(root, "--json", PLANE_READ_EVENTS=off_value)
        assert r.returncode == 1 and r.stdout == "" and "no cutover_declared" not in r.stderr, off_value
    assert _events_cmd(root, "--json").returncode == 1
    rows = _rows(_events_cmd(root, "--json", PLANE_READ_EVENTS="1"))
    assert [(r["bot"], r["type"], r["source"]) for r in rows] == [
        ("w1", "session_missing", "pulse"), ("w2", "keepalive", "keepalive"), ("fleet", "fleet_rescue", "pulse")]
    assert rows[0]["ts"] == "2026-09-03T10:00:00Z" and rows[0]["data"] == {"session": "w1"}
    assert {"cutover_declared", "report_status"}.isdisjoint({r["type"] for r in rows})   # provenance, not a name list
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--critical", PLANE_READ_EVENTS="1"))] == ["w1"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--bot", "W2", PLANE_READ_EVENTS="1"))] == ["w2"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--type", "fleet_rescue", PLANE_READ_EVENTS="1"))] == ["fleet"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--source", "keepalive", PLANE_READ_EVENTS="1"))] == ["w2"]
    assert [r["bot"] for r in _rows(_events_cmd(root, "--json", "--bot", "fleet", PLANE_READ_EVENTS="1"))] == ["fleet"]
    table = _events_cmd(root, PLANE_READ_EVENTS="1")
    assert table.returncode == 0 and "session_missing" in table.stdout
    _drop_plane(root)
    gone = _events_cmd(root, "--json", PLANE_READ_EVENTS="1")
    assert gone.returncode == 3 and "UNREACHABLE" in gone.stderr and gone.stdout == ""


def test_brief_alerts_follow_the_flip_and_omit_when_the_plane_is_unreachable(tmp_path, monkeypatch):
    root, paths, _, _ = _scene(tmp_path)
    paths.runtime_bots.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    _land(root, "w1", "session_missing", fresh, {"session": "w1"})
    _land(root, "w1", "keepalive", fresh, {"state": "IDLE"}, source="keepalive")   # a notice is never an alert
    _land(root, "w2", "service_down", fresh, {"unit": "x"})                         # another bot's
    monkeypatch.setenv("PLANE_READ_EVENTS", "1")
    degraded = []
    assert _alerts_section(paths, "w1", int(now.timestamp()), degraded) == []      # flag alone: the files
    _declare(root, "events")
    monkeypatch.setenv("PLANE_READ_EVENTS", "0")
    degraded = []
    assert _alerts_section(paths, "w1", int(now.timestamp()), degraded) == []      # declaration alone: the files
    assert not any(d.field == "alerts" and d.mode == "omitted" for d in degraded)
    monkeypatch.setenv("PLANE_READ_EVENTS", "1")
    degraded = []
    alerts = _alerts_section(paths, "w1", int(now.timestamp()), degraded)
    assert [(a["type"], a["data"]) for a in alerts] == [("session_missing", {"session": "w1"})]
    assert not any(d.field == "alerts" and d.mode == "omitted" for d in degraded)
    _drop_plane(root)
    degraded = []
    assert _alerts_section(paths, "w1", int(now.timestamp()), degraded) == []
    assert any(d.field == "alerts" and d.mode == "omitted" and "unreachable" in d.reason for d in degraded)


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
    assert _lookup(root, "--declared", "events", "--fleet", F).stdout == ""        # not declared: nothing
    _declare(root, "events")
    at = _lookup(root, "--declared", "events", "--fleet", F)
    assert at.returncode == 0 and at.stdout.strip().startswith("20") and _lookup(root, "--declared", "open", "--fleet", F).stdout == ""
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


# --- the declaration: a direct move -------------------------------------------

def test_the_events_reader_declares_as_a_direct_move_without_a_gate(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _cli(root, "cutover", "--reader", "events")                 # no --force: the ruling is the reason
    assert r.returncode == 0, r.stdout + r.stderr
    assert "direct move" in r.stdout and "PLANE_READ_EVENTS=1" in r.stdout
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
        data = json.loads(conn.execute(
            "SELECT detail FROM events WHERE event = 'cutover_declared'").fetchone()[0])
        readers = {s.reader for s in sh.gate_summary(conn, F, ["w1", "w2"], sh.GATED)}
    assert "events" in decl and "direct move" in (decl["events"][1] or "")
    assert data["shadowed"] is False and data["gate"] == {"clean_run": None, "transitions": None}
    assert data["gate_met"] is None and data["streaks"] == []
    with _ro(root) as conn:                                          # anchored on the fleet's identity
        anchored = conn.execute(
            "SELECT e.subject_alias, e.subject_uid = i.uid FROM events e JOIN identity_registry i"
            " ON i.kind = 'fleet' AND i.alias = ? WHERE e.event = 'cutover_declared'", (F,)).fetchone()
    assert tuple(anchored) == (F, 1)
    assert "events" not in readers and readers == {"open", "overdue", "open_task", "unassigned"}
    _declare(root, "open")                                          # a shadowed reader keeps its real gate block
    with _ro(root) as conn:
        shadowed = json.loads(conn.execute(
            "SELECT detail FROM events WHERE event = 'cutover_declared' AND json_extract(detail, '$.reader') = 'open'"
        ).fetchone()[0])
    assert shadowed["shadowed"] is True and shadowed["gate"]["clean_run"] == sh.GATE_CLEAN_RUN
    assert shadowed["gate_met"] is False


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
                    FLEET_PULSE_ESCALATION_THRESHOLD="2", **extra)
    return subprocess.run(["bash", str(libdir / "fleet-pulse.sh"), F], capture_output=True, text=True,
                          timeout=300, env=env)


@needs_tmux
def test_fleet_pulse_escalates_from_the_plane_once_the_files_are_retired(tmp_path):
    """Two declared bots with no tmux session: the sweep emits session_missing
    for both (through the real door, onto the plane) and its escalation must
    find them — from the files before the flip, from the plane after it, the
    same page either way. After the write is retired the files hold nothing,
    so a page can only have come from the plane."""
    root, paths, _, _ = _scene(tmp_path)
    for b in ("w1", "w2"):
        (_bot_dir(paths, b) / "bot.conf").write_text(f"TMUX_SOCKET=b1-none-{b}\n")   # no server anywhere
    (root / "tmux").mkdir()
    capture = tmp_path / "tg.log"
    libdir = _pulse_lib(tmp_path, capture)
    page = "FLEET ALERT: session_missing on 2 bots (w1 w2)."

    before = _pulse(root, libdir)                                    # the files answer the escalation
    assert before.returncode == 0, before.stderr[-2000:]
    assert page in capture.read_text(), capture.read_text() + before.stderr[-2000:]
    ledgers = list(paths.runtime_bots.glob("*/data/events/fleet-*.jsonl"))
    assert len(ledgers) == 2 and all("session_missing" in p.read_text() for p in ledgers)

    for reader in sh.GATED:                                          # the hard flip: declare + retire
        _declare(root, reader, "operator ruling 2026-09-03: hard flip, fix forward")
    assert _cli(root, "cutover", "--retire-writes").returncode == 0
    for p in ledgers:
        p.unlink()
    (root / "state" / "pulse" / "escalation_session_missing").unlink()   # the debounce marker: re-arm
    capture.write_text("")

    after = _pulse(root, libdir, PLANE_READ_EVENTS="1", PLANE_LEGACY_WRITE_EVENTS="0")
    assert after.returncode == 0, after.stderr[-2000:]
    assert page in capture.read_text(), capture.read_text() + after.stderr[-2000:]
    assert not list(paths.runtime_bots.glob("*/data/events/fleet-*.jsonl"))       # retired: no file came back
    assert "UNREACHABLE" not in after.stderr and "no cutover_declared" not in after.stderr
    n = _await(root, "SELECT COUNT(*) FROM events WHERE event = 'session_missing'", 4)
    assert n >= 4, n                                                 # two sweeps, two bots each, on the plane
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
    dark = _pulse(root, libdir, PLANE_READ_EVENTS="1", PLANE_LEGACY_WRITE_EVENTS="0")
    assert dark.returncode == 0, dark.stderr[-2000:]
    assert "UNREACHABLE" in dark.stderr and "cannot be judged this pass" in dark.stderr
    # the escalation loop never reaches for a cache no read produced (a read
    # regardless of the verdict would be a bash redirect error on a missing file)
    assert ".critical-window" not in dark.stderr and "No such file" not in dark.stderr
    paged = capture.read_text()
    assert "events reader for f is UNREACHABLE" in paged and page not in paged, paged
    summary = (root / "state" / "pulse" / "pulse-summary.txt").read_text()
    assert "unknown (events reader unreachable)" in summary and " none" not in summary
