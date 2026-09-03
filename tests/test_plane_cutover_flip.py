"""Cutover chunk 5 — the FLIP of the two list readers, and the epoch recorded.

A flip is TWO facts: the reader's flag (PLANE_READ_OPEN / PLANE_READ_OVERDUE —
the fleet .env tier, composed into bot.conf and stamped on the fleet-pulse
unit) AND a `cutover_declared` record. The matcher's providers serve the plane
only when both hold (`source="auto"`); `--source jsonl` stays callable by name
(the shadow's legacy side); `--source plane` serves the plane regardless (the
operator's probe). An unreachable plane under a set flag REFUSES rather than
falling back. `plane cutover --reader` refuses until the J4 gate is met (or
--force with a reason) and records the declaration anchored on the fleet.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from claudlobby.brief import dispatch_ledger_path, load_dispatch_doors, report_ledger_path
from claudlobby.plane import cutover as cut
from claudlobby.plane import queries, shadow as sh
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import plane_root, ro as _ro
from tests.test_plane_cutover_parity import _live_dispatch, _rrow, _write
from tests.test_plane_shadow import F, NOW, REPO, _record, _scene

MATCHER = REPO / "lib" / "dispatch-overdue.py"
NOW_EPOCH = int(datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc).timestamp())


def _env(root, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PLANE_READ_") and k not in ("CLAUDLOBBY_FLEET", "FLEET_NAME")}
    env.update({"CLAUDLOBBY_ROOT": str(root), "HOME": str(root / "home")})
    env.update(extra)
    return env


def _matcher(root, *args, **extra):
    return subprocess.run([sys.executable, str(MATCHER), *args], capture_output=True,
                          text=True, timeout=120, env=_env(root, **extra))


def _cli(root, *args, **extra):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
                           "--fleet", F, "plane", *args], capture_output=True, text=True,
                          timeout=180, env=_env(root, **extra))


def _declare(root, reader, reason="test"):
    r = _cli(root, "cutover", "--reader", reader, "--force", reason)
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _ledgers(paths):
    return str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))


def _stdlib_readers():
    spec = importlib.util.spec_from_file_location("pr", REPO / "lib" / "plane-readers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the twins cannot drift --------------------------------------------------------

def test_the_stdlib_open_sql_is_byte_identical_to_the_package():
    assert _stdlib_readers().OPEN_SQL == queries.OPEN_ASSIGNMENTS_AT_SQL


def test_the_flag_map_is_one_fact_across_the_boundary(tmp_path):
    from claudlobby.paths import Paths
    root, paths, _, _ = _scene(tmp_path)
    doors = load_dispatch_doors(paths)
    assert doors.PLANE_READ_FLAGS == cut.READ_FLAGS == {"open": "PLANE_READ_OPEN",
                                                        "overdue": "PLANE_READ_OVERDUE"}
    assert SYSTEM_EVENT_SEVERITY["cutover_declared"] == "notice"


# --- the two sources answer identically ------------------------------------------

def test_open_and_all_answer_the_same_from_the_plane_and_the_jsonl(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    for bot, want in (("w1", "t-2-bbbb"), ("w2", "t-3-cccc")):
        jsonl = _matcher(root, "--open", bot, dl, rl, "--source", "jsonl")
        plane = _matcher(root, "--open", bot, dl, rl, "--source", "plane", "--fleet", F)
        assert jsonl.returncode == 0 and plane.returncode == 0, (jsonl.stderr, plane.stderr)
        assert jsonl.stdout == plane.stdout and want in plane.stdout
        assert "[source=plane]" in plane.stderr and "[source=jsonl]" in jsonl.stderr
    jsonl = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout
    assert [l.split()[0] for l in plane.stdout.splitlines()] == ["w1", "w2"]   # both past due


def test_the_expiry_cap_and_the_progress_grace_hold_on_both_sources(tmp_path):
    root, paths, _, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    later = NOW_EPOCH + 30 * 86400                      # the cap drops the ancient rows on both
    jsonl = _matcher(root, "--all", dl, rl, str(later), "--source", "jsonl")
    plane = _matcher(root, "--all", dl, rl, str(later), "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout == ""
    prog_at = datetime.fromtimestamp(NOW_EPOCH - 300, timezone.utc)   # w1 reported progress 5 min ago
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'8':0>32}", "occurred_at": sh.dt_iso(prog_at),
                       "payload": {"work_item_id": f"wi_{'2':0>32}", "assignment_id": f"asg_{'2':0>32}",
                                   "event": "progress", "actor": f"bot:{F}/w1", "progress": 10}}])
    r.append({**_rrow(sh.dt_iso(prog_at), "t-2-bbbb", "progress", progress="10"),
              "ts": sh.dt_iso(prog_at).replace("+00:00", "Z")})
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout and "t-2-bbbb" not in plane.stdout and "t-3-cccc" in plane.stdout


def test_the_orphan_split_holds_on_both_sources(tmp_path):
    """A dispatch older than the bot's .spawn is the orphan list's row on the
    plane side too — never paged as overdue twice (#835)."""
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))      # w1 respawned after t-2 was dispatched
    jsonl = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "jsonl", "--bots-dir", str(bots))
    plane = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F,
                     "--bots-dir", str(bots))
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout
    assert "t-2-bbbb" not in plane.stdout and "t-3-cccc" in plane.stdout
    orphans = _matcher(root, "--orphans", dl, rl, str(NOW_EPOCH), "--bots-dir", str(bots))
    assert "t-2-bbbb" in orphans.stdout
    unsplit = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert "t-2-bbbb" in unsplit.stdout                    # no bots dir: no split, like legacy


def test_another_fleets_bot_never_leaks_into_the_overdue_set(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _live_dispatch(root, "9", "t-9-zzzz", ts="2026-09-02T09:00:00Z", bot="w9", fleet="g",
                   expected_by="2026-09-02T10:00:00+00:00")
    ours = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    theirs = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", "g")
    assert ours.returncode == 0 and "w9" not in ours.stdout and "t-9-zzzz" not in ours.stdout
    assert [l.split()[0] for l in ours.stdout.splitlines()] == ["w1", "w2"]
    assert theirs.stdout.startswith("w9 ") and "t-9-zzzz" in theirs.stdout and "w1" not in theirs.stdout


def test_an_id_less_row_is_the_overdue_readers_but_never_the_open_lists(tmp_path):
    """A `sha:` assignment (an id-less legacy row the importer keyed by content)
    prints as `-` in --all, like the legacy reader, and is absent from --open."""
    root, paths, d, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    from tests.test_plane_cutover_parity import _drow
    from tests.test_plane_shadow import _epoch
    ts = "2026-09-02T11:00:00Z"                          # after t-2: ledger order is time order
    deadline = datetime.fromtimestamp(1788000000, timezone.utc).isoformat()
    _live_dispatch(root, "7", "sha:" + "ab" * 8, ts=ts, expected_by=deadline)
    row = _drow(ts, "", expected_by=1788000000)         # the id-less legacy row, the same deadline
    row["dispatched_at"] = _epoch(ts)
    d.append(row)
    _write(dispatch_ledger_path(paths), d)
    for args in (("--open", "w1", dl, rl), ("--all", dl, rl, str(NOW_EPOCH))):
        jsonl = _matcher(root, *args, "--source", "jsonl")
        plane = _matcher(root, *args, "--source", "plane", "--fleet", F)
        assert jsonl.stdout == plane.stdout, (args, jsonl.stdout, plane.stdout)
    assert " - " not in _matcher(root, "--open", "w1", dl, rl, "--source", "plane", "--fleet", F).stdout
    assert any(l.split()[-1] == "-" for l in
               _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F).stdout.splitlines())


# --- the flip is flag AND declaration ---------------------------------------------

def test_a_flag_alone_is_not_a_flip_and_a_declaration_makes_it_one(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    half = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="1", CLAUDLOBBY_FLEET=F)
    assert half.returncode == 0 and "[source=jsonl]" in half.stderr and "t-2-bbbb" in half.stdout
    assert "no cutover_declared" in half.stderr and "plane cutover --reader open" in half.stderr
    _declare(root, "open")
    on = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="1", CLAUDLOBBY_FLEET=F)
    assert on.returncode == 0 and "[source=plane]" in on.stderr and on.stdout == half.stdout
    off = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="0", CLAUDLOBBY_FLEET=F)
    assert "[source=jsonl]" in off.stderr and off.stdout == on.stdout      # rollback = the flag
    named = _matcher(root, "--open", "w1", dl, rl, "--source", "jsonl", PLANE_READ_OPEN="1", CLAUDLOBBY_FLEET=F)
    assert "[source=jsonl]" in named.stderr                                 # the shadow's legacy side
    over_half = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), PLANE_READ_OVERDUE="1", FLEET_NAME=F)
    assert over_half.returncode == 0 and "no cutover_declared" in over_half.stderr and "t-3-cccc" in over_half.stdout
    _declare(root, "overdue")
    over = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), PLANE_READ_OVERDUE="1", FLEET_NAME=F)
    assert over.returncode == 0 and "no cutover_declared" not in over.stderr and over.stdout == over_half.stdout


def test_an_unreachable_plane_under_a_set_flag_refuses_and_never_falls_back(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _declare(root, "open"); _declare(root, "overdue")
    nofleet = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="1")
    assert nofleet.returncode == 3 and nofleet.stdout == "" and "UNREACHABLE" in nofleet.stderr
    # a schema-valid plane that holds no bot of this fleet (a wrong root) is refused, not "nothing open"
    other = plane_root(tmp_path / "elsewhere")
    _live_dispatch(other, "1", "t-1-aaaa", ts="2026-09-02T09:00:00Z", fleet="zz")
    wrong = _matcher(root, "--open", "w1", dl, rl, "--source", "plane", "--fleet", F, "--root", str(other))
    assert wrong.returncode == 3 and wrong.stdout == "" and "holds no bot of fleet" in wrong.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    nodb = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), PLANE_READ_OVERDUE="1", CLAUDLOBBY_FLEET=F)
    assert nodb.returncode == 3 and nodb.stdout == "" and "UNREACHABLE" in nodb.stderr
    legacy = _matcher(root, "--all", dl, rl, str(NOW_EPOCH))            # unflipped: still answers
    assert legacy.returncode == 0 and "t-2-bbbb" in legacy.stdout


def test_the_shadow_keeps_grading_the_jsonl_after_the_flip(tmp_path):
    from tests.test_plane_shadow import _cli as shadow_cli
    root, paths, _, _ = _scene(tmp_path)
    before = shadow_cli(root)
    _declare(root, "open"); _declare(root, "overdue")
    flipped = subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root), "--fleet", F,
                              "plane", "shadow"], capture_output=True, text=True, timeout=180,
                             env=_env(root, PLANE_READ_OPEN="1", PLANE_READ_OVERDUE="1", CLAUDLOBBY_FLEET=F))
    assert flipped.returncode == 0 and flipped.stdout == before.stdout and "18 clean" not in flipped.stdout
    assert "no cutover_declared" not in flipped.stderr and "clean" in flipped.stdout


def test_brief_follows_the_flip_and_omits_loudly_on_an_unreachable_plane(tmp_path, monkeypatch):
    from claudlobby.brief import build_brief
    from claudlobby.config import load_fleet
    root, paths, _, _ = _scene(tmp_path)
    fleet, _ = load_fleet(paths.fleet_yaml)
    monkeypatch.setenv("PLANE_READ_OPEN", "1")
    monkeypatch.setenv("PLANE_READ_OVERDUE", "1")
    half = build_brief(fleet, paths, "w1", NOW_EPOCH)            # flag without declaration: legacy, sound
    assert [x["task_id"] for x in half["dispatches"]["open"]] == ["t-2-bbbb"]
    assert not {x["field"] for x in half["degraded"]} & {"dispatches.open", "dispatches.overdue"}
    _declare(root, "open"); _declare(root, "overdue")
    b = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert [x["task_id"] for x in b["dispatches"]["open"]] == ["t-2-bbbb"]
    assert [x["task_id"] for x in b["dispatches"]["overdue"]] == ["t-2-bbbb"]
    assert not {x["field"] for x in b["degraded"]} & {"dispatches.open", "dispatches.overdue"}
    (root / "state" / "plane" / "plane.db").unlink()
    b2 = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert b2["dispatches"]["open"] == [] and b2["dispatches"]["overdue"] == []
    modes = {x["field"]: x["mode"] for x in b2["degraded"]}
    assert modes["dispatches.open"] == "omitted" and modes["dispatches.overdue"] == "omitted"
    assert any("flip the flag back" in x["reason"] for x in b2["degraded"])


# --- the epoch: refused until the gate, recorded when declared -------------------

def test_cutover_refuses_short_of_the_gate_records_when_met_and_force_records_the_reason(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    short = _cli(root, "cutover", "--reader", "open")
    assert short.returncode == 1 and "REFUSED" in short.stdout and "SHORT" in short.stdout
    with _ro(root) as conn:
        assert cut.declared(conn, F) == {}
    forced = _cli(root, "cutover", "--reader", "overdue", "--force", "bootstrap")
    assert forced.returncode == 0 and "PLANE_READ_OVERDUE=1" in forced.stdout, forced.stderr
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
        assert set(decl) == {"overdue"} and decl["overdue"][1] == "bootstrap"
        anchor = cut.fleet_uid(conn, F)
        row = conn.execute("SELECT subject_kind, subject_uid, subject_alias FROM events"
                           " WHERE event = 'cutover_declared'").fetchone()
        assert tuple(row) == (("fleet", anchor, f"fleet:{F}") if anchor else (None, None, None))
    t0 = NOW - timedelta(hours=40)
    for bot, ids in (("w1", ["t-2-bbbb"]), ("w2", ["t-3-cccc"])):
        for k in range(sh.GATE_CLEAN_RUN):
            _record(root, bot, t0 + timedelta(hours=k), ids)
        _record(root, bot, t0 + timedelta(hours=21), [])           # the transition
    met = _cli(root, "cutover", "--reader", "open")
    assert met.returncode == 0 and "PLANE_READ_OPEN=1" in met.stdout and "FORCED" not in met.stdout
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
        assert set(decl) == {"open", "overdue"} and decl["open"][1] is None
        gate_met = [tuple(r) for r in conn.execute(
            "SELECT json_extract(detail, '$.gate_met'), json_extract(detail, '$.reader')"
            " FROM events WHERE event = 'cutover_declared' ORDER BY occurred_at")]
        assert (1, "open") in gate_met and (0, "overdue") in gate_met


def test_the_declaration_id_is_derived_so_a_re_run_at_one_instant_is_one_fact():
    st = sh.Streak(F, "w1", sh.READER_OPEN)
    a = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:00+00:00")
    a2 = cut.declaration_event(F, "open", [], "2026-09-03T05:00:00+00:00")
    b = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:00+00:00", forced="x")
    c = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:01+00:00")
    assert a["event_id"] == a2["event_id"]                              # a re-run: one fact
    assert len({a["event_id"], b["event_id"], c["event_id"]}) == 3      # a reason or a later instant: new facts
    assert a["payload"]["event"] == "cutover_declared" and a["payload"]["data"]["gate_met"] is False
    assert "subject_kind" not in a["payload"]
    anchored = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:00+00:00", subject_uid="flt_x")
    assert anchored["payload"]["subject_kind"] == "fleet" and anchored["payload"]["subject_alias"] == f"fleet:{F}"
    try:
        cut.declaration_event(F, "orphans", [], "2026-09-03T05:00:00+00:00")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown reader must not declare")


def test_the_latest_declaration_wins_and_a_same_instant_re_run_is_disclosed(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    first = _cli(root, "cutover", "--reader", "overdue", "--force", "first")
    assert first.returncode == 0 and "committed=1" in first.stdout
    again = _cli(root, "cutover", "--reader", "overdue", "--force", "first")   # same reason, same second?
    if "duplicate=1" in again.stdout:
        assert "already declared at this instant" in again.stdout
    time.sleep(1.1)
    _declare(root, "overdue", "second")
    with _ro(root) as conn:
        assert cut.declared(conn, F)["overdue"][1] == "second"


def test_flag_vs_declaration_names_the_missing_half():
    assert cut.flag_vs_declaration(True, None)[0] is False
    assert "NO declaration" in cut.flag_vs_declaration(True, None)[1]
    assert cut.flag_vs_declaration(False, "2026-09-03T05:00:00+00:00")[0] is False
    assert cut.flag_vs_declaration(True, "2026-09-03T05:00:00+00:00")[0] is True
    assert cut.flag_vs_declaration(False, None) == (True, "legacy (not declared, not flipped)")


def test_the_doctor_reads_the_flag_against_the_declaration(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    (root / "home").mkdir()
    (root / "local" / F / ".env").write_text("PLANE_READ_OPEN=1\n")
    d = _cli(root, "doctor")
    assert "cutover open" in d.stdout and "NO declaration" in d.stdout, d.stdout + d.stderr
    assert "cutover overdue" in d.stdout and "legacy" in d.stdout
    _declare(root, "open", "operator")
    d2 = _cli(root, "doctor")
    assert "flipped to the plane" in d2.stdout and "NO declaration" not in d2.stdout


# --- the carriers ------------------------------------------------------------------

def _composed(tmp_path, monkeypatch, armed: dict[str, str]):
    from textwrap import dedent
    import claudlobby.composer as composer_mod
    import claudlobby.env_tiers as env_tiers_mod
    from claudlobby.composer import compose_bot_conf, compose_fleet_timers
    from claudlobby.config import load_fleet
    from claudlobby.env_tiers import Resolution
    from claudlobby.paths import Paths
    from tests.test_composer_briefing_arming import _FLEET
    fl = dedent(_FLEET).replace("system_defaults: false", "system_defaults: true")
    root = tmp_path / "fs"
    root.mkdir(parents=True)
    (root / "fleet.yaml").write_text(fl)
    fleet, md = load_fleet(root / "fleet.yaml")
    paths = Paths(root=root, fleet_dir=root)
    res = {k: Resolution(name=k, value=v, tier="fleet", path=None) for k, v in armed.items()}
    monkeypatch.setattr(env_tiers_mod, "read_tiers", lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade", lambda tiers: res)
    composer_mod._READ_FLAG_MEMO.clear()
    timers = compose_fleet_timers(fleet, paths, md)
    conf = compose_bot_conf(next(iter(fleet.bots.values())), fleet, paths)
    return timers, conf


def test_bot_conf_carries_the_read_flags_the_fleet_tier_arms(tmp_path, monkeypatch):
    _, conf = _composed(tmp_path, monkeypatch, {"PLANE_READ_OPEN": "1"})
    import re
    assert re.search(r"^export PLANE_READ_OPEN='?1'?$", conf, re.M) and "PLANE_READ_OVERDUE" not in conf
    _, bare = _composed(tmp_path / "b", monkeypatch, {"PLANE_READ_OPEN": "0", "PLANE_EMIT_ENABLED": "1"})
    assert "PLANE_READ_" not in bare


def test_the_fleet_pulse_unit_is_the_multi_flag_job(tmp_path, monkeypatch):
    from claudlobby.composer import FLEET_JOB_ARMING
    assert FLEET_JOB_ARMING["fleet-pulse"] == ("PLANE_SHADOW_ENABLED", "PLANE_READ_OVERDUE")
    timers, _ = _composed(tmp_path, monkeypatch, {"PLANE_SHADOW_ENABLED": "1", "PLANE_READ_OVERDUE": "1"})
    pulse = next(p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "Environment=PLANE_SHADOW_ENABLED=1" in pulse and "Environment=PLANE_READ_OVERDUE=1" in pulse
    assert "PLANE_READ_OVERDUE" not in (timers / "com.test.plane-shadow.service").read_text()
    timers2, _ = _composed(tmp_path / "b", monkeypatch, {"PLANE_READ_OVERDUE": "1"})
    pulse2 = next(p for p in timers2.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "PLANE_READ_OVERDUE=1" in pulse2 and "PLANE_SHADOW_ENABLED" not in pulse2


def test_the_watchdog_names_its_fleet_on_the_overdue_call():
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    line = next(l for l in src.splitlines() if 'dispatch-overdue.py" --all' in l)
    assert '--fleet "$fleet"' in line


def test_env_tiers_armed_is_exact():
    from claudlobby.env_tiers import Resolution, armed
    res = lambda v: {"X": Resolution(name="X", value=v, tier="fleet", path=None)}
    assert armed(res("1"), "X") and not armed(res("0"), "X") and not armed(res("true"), "X")
    assert not armed(res("1 "), "X") and not armed({}, "X")


# --- the grammar -------------------------------------------------------------------

def test_the_grammar_refuses_a_dropped_value_and_a_plane_source_off_its_modes(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    for args in (("--open", "w1", dl, rl, "--fleet"),
                 ("--open", "w1", dl, rl, "--fleet", "--source", "plane"),
                 ("--all", dl, rl, "--source"),
                 ("--all", dl, rl, "--root")):
        r = _matcher(root, *args, CLAUDLOBBY_FLEET="other")
        assert r.returncode == 2 and r.stdout == "" and "needs a value" in r.stderr, args
    for args in (("--orphans", dl, rl, "--bots-dir", str(root)),
                 ("--unassigned", dl, rl),
                 ("--open-task", "w1", dl, rl),
                 ("w1", dl, rl)):
        r = _matcher(root, *args, "--source", "plane", "--fleet", F)
        assert r.returncode == 2 and r.stdout == "" and "no meaning" in r.stderr, args
        auto = _matcher(root, *args, PLANE_READ_OPEN="1", PLANE_READ_OVERDUE="1", CLAUDLOBBY_FLEET=F)
        assert auto.returncode in (0, 3) and "no meaning" not in auto.stderr, args   # flags apply where defined
    # --bots-dir need not be last any more: the source flags are stripped first
    bots = root / "bots"; (bots / "w1" / "data").mkdir(parents=True)
    a = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--bots-dir", str(bots), "--source", "jsonl")
    b = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "jsonl", "--bots-dir", str(bots))
    assert a.returncode == 0 == b.returncode and a.stdout == b.stdout


def test_the_stdlib_open_retries_a_transient_cantopen_once_then_refuses(tmp_path, monkeypatch):
    """Measured on the Mini: a mode=ro open answered CANTOPEN for ~20s while the
    daemon held the WAL, then cleared. One retry absorbs the blip; a persistent
    failure still raises PlaneUnreachable (refuse, never answer empty)."""
    import sqlite3 as _sq
    root, _, _, _ = _scene(tmp_path)
    m = _stdlib_readers()
    monkeypatch.setattr(m, "OPEN_RETRY_S", 0.0)
    real = _sq.connect
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _sq.OperationalError("unable to open database file")
        return real(*a, **kw)
    monkeypatch.setattr(m.sqlite3, "connect", flaky)
    conn = m.connect(str(root))
    assert calls["n"] == 2 and {"w1", "w2"} <= set(m.roster(conn, F))
    conn.close()
    calls["n"] = 0
    monkeypatch.setattr(m.sqlite3, "connect", lambda *a, **kw: (_ for _ in ()).throw(_sq.OperationalError("unable to open database file")))
    try:
        m.connect(str(root))
    except m.PlaneUnreachable as exc:
        assert "unable to open" in str(exc)
    else:
        raise AssertionError("a persistent CANTOPEN must refuse")
