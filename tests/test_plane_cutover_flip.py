"""Cutover chunk 5 — the FLIP of the two list readers behind per-reader flags,
and the epoch recorded when it happens.

The matcher answers `--open` / `--all` from the plane (stdlib twin of the
package SQL, byte-identical) when PLANE_READ_OPEN / PLANE_READ_OVERDUE say
so; brief and the watchdog follow the same flags; an unreachable plane under
a set flag REFUSES rather than falling back; `plane cutover --reader` refuses
until the J4 gate is met (or --force with a reason) and records
`cutover_declared`; the doctor reads the flag against the declaration.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from claudlobby.brief import dispatch_ledger_path, report_ledger_path
from claudlobby.plane import cutover as cut
from claudlobby.plane import queries, shadow as sh
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import ro as _ro
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


def _stdlib_readers():
    spec = importlib.util.spec_from_file_location("pr", REPO / "lib" / "plane-readers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the stdlib twin cannot drift from the package --------------------------------

def test_the_stdlib_open_sql_is_byte_identical_to_the_package():
    assert _stdlib_readers().OPEN_SQL == queries.OPEN_ASSIGNMENTS_AT_SQL


def test_the_epoch_token_is_registered():
    assert SYSTEM_EVENT_SEVERITY["cutover_declared"] == "notice"
    assert cut.READ_FLAGS == {"open": "PLANE_READ_OPEN", "overdue": "PLANE_READ_OVERDUE"}


# --- the two sources answer identically, and the flag picks the default ----------

def test_open_answers_the_same_from_the_plane_and_the_jsonl(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    for bot, want in (("w1", "t-2-bbbb"), ("w2", "t-3-cccc")):
        jsonl = _matcher(root, "--open", bot, dl, rl, "--source", "jsonl")
        plane = _matcher(root, "--open", bot, dl, rl, "--source", "plane", "--fleet", F)
        assert jsonl.returncode == 0 and plane.returncode == 0, (jsonl.stderr, plane.stderr)
        assert jsonl.stdout == plane.stdout and want in plane.stdout
        assert "source=plane" in plane.stderr and "source=plane" not in jsonl.stderr


def test_all_answers_the_same_overdue_set_from_both_sources(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    jsonl = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 and plane.returncode == 0, (jsonl.stderr, plane.stderr)
    assert jsonl.stdout == plane.stdout
    assert [l.split()[0] for l in plane.stdout.splitlines()] == ["w1", "w2"]   # both past due
    assert "t-2-bbbb" in plane.stdout and "t-3-cccc" in plane.stdout


def test_the_flag_flips_the_default_and_the_jsonl_stays_callable_by_name(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    on = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="1", CLAUDLOBBY_FLEET=F)
    assert on.returncode == 0 and "source=plane" in on.stderr and "t-2-bbbb" in on.stdout
    off = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="0", CLAUDLOBBY_FLEET=F)
    assert off.returncode == 0 and "source=plane" not in off.stderr and off.stdout == on.stdout
    named = _matcher(root, "--open", "w1", dl, rl, "--source", "jsonl", PLANE_READ_OPEN="1")
    assert named.returncode == 0 and "source=plane" not in named.stderr    # the shadow's legacy side
    over = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), PLANE_READ_OVERDUE="1", FLEET_NAME=F)
    assert over.returncode == 0 and "t-3-cccc" in over.stdout


def test_an_unreachable_plane_under_a_set_flag_refuses_and_never_falls_back(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    nofleet = _matcher(root, "--open", "w1", dl, rl, PLANE_READ_OPEN="1")
    assert nofleet.returncode == 3 and nofleet.stdout == "" and "UNREACHABLE" in nofleet.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    nodb = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), PLANE_READ_OVERDUE="1", CLAUDLOBBY_FLEET=F)
    assert nodb.returncode == 3 and nodb.stdout == "" and "UNREACHABLE" in nodb.stderr
    legacy = _matcher(root, "--all", dl, rl, str(NOW_EPOCH))            # unflipped: still answers
    assert legacy.returncode == 0 and "t-2-bbbb" in legacy.stdout


def test_brief_follows_the_flags_and_omits_loudly_on_an_unreachable_plane(tmp_path, monkeypatch):
    from claudlobby.brief import build_brief
    from claudlobby.config import load_fleet
    root, paths, _, _ = _scene(tmp_path)
    fleet, _ = load_fleet(paths.fleet_yaml)
    monkeypatch.setenv("PLANE_READ_OPEN", "1")
    monkeypatch.setenv("PLANE_READ_OVERDUE", "1")
    b = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert [x["task_id"] for x in b["dispatches"]["open"]] == ["t-2-bbbb"]
    assert [x["task_id"] for x in b["dispatches"]["overdue"]] == ["t-2-bbbb"]
    assert not {x["field"] for x in b["degraded"]} & {"dispatches.open", "dispatches.overdue"}
    (root / "state" / "plane" / "plane.db").unlink()
    b2 = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert b2["dispatches"]["open"] == [] and b2["dispatches"]["overdue"] == []
    modes = {x["field"]: x["mode"] for x in b2["degraded"]}
    assert modes["dispatches.open"] == "omitted" and modes["dispatches.overdue"] == "omitted"
    assert any("PLANE_READ_OPEN=1" in x["reason"] for x in b2["degraded"])


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
        gate_met = conn.execute(
            "SELECT json_extract(detail, '$.gate_met'), json_extract(detail, '$.reader')"
            " FROM events WHERE event = 'cutover_declared' ORDER BY occurred_at").fetchall()
        gate_met = [tuple(r) for r in gate_met]
        assert (1, "open") in gate_met and (0, "overdue") in gate_met


def test_the_declaration_id_is_derived_so_a_re_run_at_one_instant_is_one_fact():
    st = sh.Streak(F, "w1", sh.READER_OPEN)
    a = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:00+00:00")
    b = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:00+00:00", forced="x")
    c = cut.declaration_event(F, "open", [st], "2026-09-03T05:00:01+00:00")
    assert a["event_id"] == b["event_id"] != c["event_id"]
    assert a["payload"]["event"] == "cutover_declared" and a["payload"]["data"]["gate_met"] is False
    try:
        cut.declaration_event(F, "orphans", [], "2026-09-03T05:00:00+00:00")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown reader must not declare")


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
    _cli(root, "cutover", "--reader", "open", "--force", "operator")
    d2 = _cli(root, "doctor")
    assert "flipped to the plane" in d2.stdout and "NO declaration" not in d2.stdout


# --- the carriers: the watchdog unit is stamped, the watchdog names its fleet ------

def test_the_fleet_pulse_unit_carries_the_overdue_flag_when_the_fleet_tier_flips_it(tmp_path, monkeypatch):
    from textwrap import dedent
    from claudlobby.composer import FLEET_JOB_ARMING, compose_fleet_timers
    from claudlobby.config import load_fleet
    from claudlobby.env_tiers import Resolution
    from claudlobby.paths import Paths
    from tests.test_composer_briefing_arming import _FLEET
    assert FLEET_JOB_ARMING["fleet-pulse"] == ("PLANE_READ_OVERDUE",)
    fl = dedent(_FLEET).replace("system_defaults: false", "system_defaults: true")
    root = tmp_path / "fs"
    root.mkdir()
    (root / "fleet.yaml").write_text(fl)
    fleet, md = load_fleet(root / "fleet.yaml")
    import claudlobby.env_tiers as env_tiers_mod
    res = Resolution(name="PLANE_READ_OVERDUE", value="1", tier="fleet", path=None)
    monkeypatch.setattr(env_tiers_mod, "read_tiers", lambda paths, fleet_name=None, bot_name=None: [])
    monkeypatch.setattr(env_tiers_mod, "cascade", lambda tiers: {"PLANE_READ_OVERDUE": res})
    timers = compose_fleet_timers(fleet, Paths(root=root, fleet_dir=root), md)
    pulse = [p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service"]
    assert pulse and "Environment=PLANE_READ_OVERDUE=1" in pulse[0].read_text()
    assert "PLANE_READ_OVERDUE" not in (timers / "com.test.plane-shadow.service").read_text()


def test_the_watchdog_names_its_fleet_on_the_overdue_call():
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    line = next(l for l in src.splitlines() if 'dispatch-overdue.py" --all' in l)
    assert '--fleet "$fleet"' in line


# --- pins the first mutation pass found missing ------------------------------------

def test_the_expiry_cap_and_the_progress_grace_hold_on_both_sources(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    from tests.test_plane_cutover_parity import _rrow, _write
    root, paths, _, r = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
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


def test_another_fleets_bot_never_leaks_into_the_overdue_set(tmp_path):
    from claudlobby.plane.emit_api import emit_batch
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    ts, wi, asg, msg = "2026-09-02T09:00:00Z", f"wi_{'9':0>32}", f"asg_{'9':0>32}", f"msg_{'9':0>32}"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": "g", "occurred_at": ts,
         "source_ref": "dispatch-log:t-9-zzzz",
         "payload": {"work_item_id": wi, "title": "t", "created_by": "bot:g/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": "g", "occurred_at": ts,
         "source_ref": "dispatch-log:t-9-zzzz",
         "payload": {"assignment_id": asg, "work_item_id": wi, "assignee": "bot:g/w9",
                     "assigned_by": "bot:g/mgr", "dispatch_msg_id": msg,
                     "expected_by": "2026-09-02T10:00:00+00:00"}},
        {"event_type": "communication", "emitter": "dispatch-task", "fleet": "g", "occurred_at": ts,
         "source_ref": "dispatch-log:t-9-zzzz",
         "payload": {"msg_id": msg, "sender": "bot:g/mgr", "recipient": "bot:g/w9",
                     "message_class": "task_request", "command_type": "task",
                     "work_item_id": wi, "assignment_id": asg, "body": "t"}}])
    ours = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    theirs = _matcher(root, "--all", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", "g")
    assert ours.returncode == 0 and "w9" not in ours.stdout and "t-9-zzzz" not in ours.stdout
    assert [l.split()[0] for l in ours.stdout.splitlines()] == ["w1", "w2"]
    assert theirs.stdout.startswith("w9 ") and "t-9-zzzz" in theirs.stdout and "w1" not in theirs.stdout


def test_the_latest_declaration_wins(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    import time
    first = _cli(root, "cutover", "--reader", "overdue", "--force", "first")
    assert first.returncode == 0 and "committed=1" in first.stdout
    again = _cli(root, "cutover", "--reader", "overdue", "--force", "again")   # same second: one fact
    if "duplicate=1" in again.stdout:
        assert "already declared at this instant" in again.stdout
    time.sleep(1.1)
    assert _cli(root, "cutover", "--reader", "overdue", "--force", "second").returncode == 0
    with _ro(root) as conn:
        assert cut.declared(conn, F)["overdue"][1] == "second"


def test_a_hash_keyed_assignment_has_no_legacy_id_and_is_skipped_by_both_readers(tmp_path):
    """A `dispatch-log:sha:` assignment (an import of a row the ledger held
    without a task id) is not an id'd row: the legacy list never had it, so
    the plane source must not manufacture one."""
    from claudlobby.plane.emit_api import emit_batch
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = str(dispatch_ledger_path(paths)), str(report_ledger_path(paths))
    ts, wi, asg, msg = "2026-09-02T09:30:00Z", f"wi_{'7':0>32}", f"asg_{'7':0>32}", f"msg_{'7':0>32}"
    ref = "dispatch-log:sha:" + "ab" * 8
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F, "occurred_at": ts,
         "source_ref": ref, "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F, "occurred_at": ts,
         "source_ref": ref, "payload": {"assignment_id": asg, "work_item_id": wi,
                                        "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr",
                                        "dispatch_msg_id": msg, "expected_by": "2026-09-02T10:00:00+00:00"}},
        {"event_type": "communication", "emitter": "dispatch-task", "fleet": F, "occurred_at": ts,
         "source_ref": ref, "payload": {"msg_id": msg, "sender": f"bot:{F}/mgr", "recipient": f"bot:{F}/w1",
                                        "message_class": "task_request", "command_type": "task",
                                        "work_item_id": wi, "assignment_id": asg, "body": "t"}}])
    for args in (("--open", "w1", dl, rl), ("--all", dl, rl, str(NOW_EPOCH))):
        jsonl = _matcher(root, *args, "--source", "jsonl")
        plane = _matcher(root, *args, "--source", "plane", "--fleet", F)
        assert jsonl.stdout == plane.stdout and "sha:" not in plane.stdout and "t-2-bbbb" in plane.stdout
