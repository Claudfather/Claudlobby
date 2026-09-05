"""Cutover chunk 5 → F18 closure R2a: the matcher's list readers (`--open`,
`--all`) answer from the PLANE and from nothing else.

The flip used to be two facts — a per-reader PLANE_READ_* flag AND a recorded
`cutover_declared` — with `--source jsonl` naming the legacy side the shadow
graded. R2a removed the legacy side and the shadow with it: the matcher opens
the plane under `--fleet`/`--root` (else the CLAUDLOBBY_FLEET → FLEET_NAME and
CLAUDLOBBY_ROOT carriers), refuses at rc 3 when it cannot (UNREACHABLE is not
empty), and `plane cutover --reader` records a DIRECT MOVE with no gate. The
flags still compose (bot.conf, the fleet-pulse unit) and the doctor still
reads them against the declaration; R3 retires that surface.

Deleted with the shadow and the legacy side (F18 closure, R2a):
test_open_and_all_answer_the_same_from_the_plane_and_the_jsonl (its plane half
is test_open_and_all_answer_from_the_plane),
test_the_expiry_cap_and_the_progress_grace_hold_on_both_sources (→ ..._hold_on_the_plane),
test_the_orphan_split_holds_on_both_sources (→ ..._holds_on_the_plane),
test_a_flag_alone_is_not_a_flip_and_a_declaration_makes_it_one,
test_the_shadow_keeps_grading_the_jsonl_after_the_flip,
test_cutover_refuses_short_of_the_gate_records_when_met_and_force_records_the_reason
(→ test_cutover_declares_a_direct_move_and_records_the_reason).
Re-pointed and renamed: test_the_flag_map_is_one_fact_across_the_boundary →
test_the_reader_set_and_its_flags_are_one_fact (the matcher publishes no
PLANE_READ_FLAGS), test_an_unreachable_plane_under_a_set_flag_refuses_and_never_falls_back
→ test_an_unreachable_plane_refuses_and_never_answers_empty,
test_brief_follows_the_flip_and_omits_loudly_on_an_unreachable_plane →
test_brief_serves_the_plane_and_omits_loudly_when_it_is_unreachable,
test_the_grammar_refuses_a_dropped_value_and_a_plane_source_off_its_modes →
test_the_grammar_refuses_a_dropped_value_and_a_path_in_the_bot_slot.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import pytest

from claudlobby.plane import cutover as cut, queries
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import (F, NOW_EPOCH, REPO, _cli, _declare, _matcher, _scene,
                                  _stdlib_readers, plane_root, ro as _ro)
from tests.test_plane_cutover_parity import _live_dispatch


# --- the twins cannot drift --------------------------------------------------------

def test_the_stdlib_open_sql_is_byte_identical_to_the_package():
    assert _stdlib_readers().OPEN_SQL == queries.OPEN_ASSIGNMENTS_AT_SQL


def test_the_reader_set_and_its_flags_are_one_fact():
    assert cut.READERS == ("open", "overdue", "open_task", "unassigned", "events")
    assert cut.GATED == cut.READERS                                  # the name the callers grew up with
    assert cut.READ_FLAGS == {"open": "PLANE_READ_OPEN", "overdue": "PLANE_READ_OVERDUE",
                              "open_task": "PLANE_READ_OPEN_TASK", "unassigned": "PLANE_READ_UNASSIGNED",
                              "events": "PLANE_READ_EVENTS"}
    assert SYSTEM_EVENT_SEVERITY["cutover_declared"] == "notice"


# --- the plane answers -------------------------------------------------------------

def test_open_and_all_answer_from_the_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    for bot, want in (("w1", "t-2-bbbb"), ("w2", "t-3-cccc")):
        r = _matcher(root, "--open", bot, "--fleet", F)
        assert r.returncode == 0, r.stderr
        assert [l.split()[-1] for l in r.stdout.splitlines()] == [want]
        # the scope, ALWAYS and on stderr (#1187): stdout is parsed by report-back
        assert f"--open: bot={bot!r} -> 1 open id'd dispatch(es) [source=plane]" in r.stderr
    over = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    assert over.returncode == 0, over.stderr
    assert [(l.split()[0], l.split()[-1]) for l in over.stdout.splitlines()] == \
        [("w1", "t-2-bbbb"), ("w2", "t-3-cccc")]                     # both past due
    # the carriers: a session's FLEET_NAME, a timer unit's CLAUDLOBBY_FLEET (which
    # wins over a session's), an explicit --root over CLAUDLOBBY_ROOT
    named = _matcher(root, "--open", "w1", "--fleet", F)
    session = _matcher(root, "--open", "w1", FLEET_NAME=F)
    unit = _matcher(root, "--open", "w1", CLAUDLOBBY_FLEET=F, FLEET_NAME="other")
    rooted = _matcher(root, "--open", "w1", "--fleet", F, "--root", str(root),
                      CLAUDLOBBY_ROOT=str(tmp_path / "nowhere"))
    assert named.stdout and session.stdout == unit.stdout == rooted.stdout == named.stdout
    assert session.returncode == unit.returncode == rooted.returncode == 0


def test_the_expiry_cap_and_the_progress_grace_hold_on_the_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    later = NOW_EPOCH + 30 * 86400                       # the cap drops the ancient rows
    capped = _matcher(root, "--all", str(later), "--fleet", F)
    assert capped.returncode == 0 and capped.stdout == "", capped.stderr
    prog_at = datetime.fromtimestamp(NOW_EPOCH - 300, timezone.utc).isoformat(timespec="seconds")
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,     # w1 reported progress 5 min ago
                       "source_ref": f"report-back:msg_{'8':0>32}", "occurred_at": prog_at,
                       "payload": {"work_item_id": f"wi_{'2':0>32}", "assignment_id": f"asg_{'2':0>32}",
                                   "event": "progress", "actor": f"bot:{F}/w1", "progress": 10}}])
    graced = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    assert graced.returncode == 0 and "t-2-bbbb" not in graced.stdout and "t-3-cccc" in graced.stdout
    opened = _matcher(root, "--open", "w1", "--fleet", F)             # open is deadline-blind and grace-blind
    assert [l.split()[-1] for l in opened.stdout.splitlines()] == ["t-2-bbbb"]


def test_the_orphan_split_holds_on_the_plane(tmp_path):
    """A dispatch older than the bot's .spawn is the orphan list's row — never
    paged as overdue twice (#835); with no bots dir there is no split, and the
    orphan mode refuses rather than answering 'none' (#1014)."""
    root, paths, _, _ = _scene(tmp_path)
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))      # w1 respawned after t-2 was dispatched
    split = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert split.returncode == 0, split.stderr
    assert "t-2-bbbb" not in split.stdout and "t-3-cccc" in split.stdout
    orphans = _matcher(root, "--orphans", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert orphans.returncode == 0, orphans.stderr
    assert "t-2-bbbb" in orphans.stdout and "t-3-cccc" not in orphans.stdout
    unsplit = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    assert "t-2-bbbb" in unsplit.stdout                    # no bots dir: no split
    blind = _matcher(root, "--orphans", str(NOW_EPOCH), "--fleet", F)
    assert blind.returncode == 3 and blind.stdout == "" and "cannot determine orphans" in blind.stderr


def test_another_fleets_bot_never_leaks_into_the_overdue_set(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    _live_dispatch(root, "9", "t-9-zzzz", ts="2026-09-02T09:00:00Z", bot="w9", fleet="g",
                   expected_by="2026-09-02T10:00:00+00:00")
    ours = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    theirs = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", "g")
    assert ours.returncode == 0 and "w9" not in ours.stdout and "t-9-zzzz" not in ours.stdout
    assert [l.split()[0] for l in ours.stdout.splitlines()] == ["w1", "w2"]
    assert theirs.stdout.startswith("w9 ") and "t-9-zzzz" in theirs.stdout and "w1" not in theirs.stdout


def test_an_id_less_row_is_the_overdue_readers_but_never_the_open_lists(tmp_path):
    """A `sha:` assignment (an id-less dispatch keyed by content) prints as `-`
    in --all, as the legacy reader printed it, and is absent from --open."""
    root, paths, _, _ = _scene(tmp_path)
    ts = "2026-09-02T11:00:00Z"
    deadline = datetime.fromtimestamp(1788000000, timezone.utc).isoformat()
    _live_dispatch(root, "7", "sha:" + "ab" * 8, ts=ts, expected_by=deadline)
    opened = _matcher(root, "--open", "w1", "--fleet", F)
    assert opened.returncode == 0 and [l.split()[-1] for l in opened.stdout.splitlines()] == ["t-2-bbbb"]
    over = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F)
    assert over.returncode == 0, over.stderr
    rows = [l.split() for l in over.stdout.splitlines()]
    assert {r[-1] for r in rows if r[0] == "w1"} == {"t-2-bbbb", "-"}


# --- unreachable is not empty --------------------------------------------------------

def test_an_unreachable_plane_refuses_and_never_answers_empty(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    nofleet = _matcher(root, "--open", "w1")                        # no --fleet, no carrier
    assert nofleet.returncode == 3 and nofleet.stdout == "" and "UNREACHABLE" in nofleet.stderr
    assert "needs a fleet" in nofleet.stderr
    noroot = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F, CLAUDLOBBY_ROOT="")
    assert noroot.returncode == 3 and noroot.stdout == "" and "UNREACHABLE" in noroot.stderr
    # a schema-valid plane that holds no bot of this fleet (a wrong root) is refused, not "nothing open"
    other = plane_root(tmp_path / "elsewhere")
    _live_dispatch(other, "1", "t-1-aaaa", ts="2026-09-02T09:00:00Z", fleet="zz")
    wrong = _matcher(root, "--open", "w1", "--fleet", F, "--root", str(other))
    assert wrong.returncode == 3 and wrong.stdout == "" and "holds no bot of fleet" in wrong.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    for args in (("--open", "w1"), ("--open-task", "w1"), ("--all", str(NOW_EPOCH)),
                 ("--unassigned", str(NOW_EPOCH)), ("--orphans", str(NOW_EPOCH), "--bots-dir", str(root))):
        gone = _matcher(root, *args, "--fleet", F)
        assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr, (args, gone.stderr)


def test_brief_serves_the_plane_and_omits_loudly_when_it_is_unreachable(tmp_path):
    from claudlobby.brief import build_brief
    from claudlobby.config import load_fleet
    root, paths, _, _ = _scene(tmp_path)
    fleet, _ = load_fleet(paths.fleet_yaml)
    b = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert [x["task_id"] for x in b["dispatches"]["open"]] == ["t-2-bbbb"]
    assert [x["task_id"] for x in b["dispatches"]["overdue"]] == ["t-2-bbbb"]
    assert not {x["field"] for x in b["degraded"]} & {"dispatches.open", "dispatches.overdue"}
    assert "shadow" not in b                                         # the envelope carries no shadow section
    (root / "state" / "plane" / "plane.db").unlink()
    b2 = build_brief(fleet, paths, "w1", NOW_EPOCH)
    assert b2["dispatches"] == {}                                   # the WHOLE section withheld: never "0 open"
    modes = {(x["field"], x["mode"]) for x in b2["degraded"]}
    assert {("dispatches.open", "omitted"), ("dispatches.overdue", "omitted"),
            ("dispatches.orphaned", "omitted")} <= modes
    assert any("the plane cannot answer" in x["reason"] for x in b2["degraded"])


# --- the epoch: a direct move, recorded when declared ----------------------------------

def test_cutover_declares_a_direct_move_and_records_the_reason(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    plain = _cli(root, "cutover", "--reader", "open")               # no gate, no --force needed
    assert plain.returncode == 0, plain.stdout + plain.stderr
    assert "PLANE_READ_OPEN=1" in plain.stdout and "committed=1" in plain.stdout
    assert "direct move" in plain.stdout and "REFUSED" not in plain.stdout
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
        assert set(decl) == {"open"} and decl["open"][1] == cut.DIRECT_MOVE_REASON
        anchor = cut.fleet_uid(conn, F)
        row = conn.execute("SELECT subject_kind, subject_uid, subject_alias FROM events"
                           " WHERE event = 'cutover_declared'").fetchone()
        assert tuple(row) == (("fleet", anchor, F) if anchor else (None, None, None))   # the bare alias the registry mints
        data = json.loads(conn.execute("SELECT detail FROM events WHERE event = 'cutover_declared'").fetchone()[0])
    assert data["shadowed"] is False and data["gate"] == {"clean_run": None, "transitions": None}
    assert data["gate_met"] is None and data["streaks"] == [] and data["flag"] == "PLANE_READ_OPEN"
    forced = _cli(root, "cutover", "--reader", "overdue", "--force", "bootstrap")
    assert forced.returncode == 0 and "PLANE_READ_OVERDUE=1" in forced.stdout, forced.stderr
    assert "FORCED: bootstrap" in forced.stdout
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
    assert set(decl) == {"open", "overdue"} and decl["overdue"][1] == "bootstrap"


def test_the_declaration_id_is_derived_so_a_re_run_at_one_instant_is_one_fact():
    a = cut.declaration_event(F, "open", "2026-09-03T05:00:00+00:00")
    a2 = cut.declaration_event(F, "open", "2026-09-03T05:00:00+00:00")
    b = cut.declaration_event(F, "open", "2026-09-03T05:00:00+00:00", forced="x")
    c = cut.declaration_event(F, "open", "2026-09-03T05:00:01+00:00")
    assert a["event_id"] == a2["event_id"]                              # a re-run: one fact
    assert len({a["event_id"], b["event_id"], c["event_id"]}) == 3      # a reason or a later instant: new facts
    data = a["payload"]["data"]
    assert a["payload"]["event"] == "cutover_declared" and data["gate_met"] is None
    assert data["shadowed"] is False and data["streaks"] == [] and data["forced"] == cut.DIRECT_MOVE_REASON
    assert data["gate"] == {"clean_run": None, "transitions": None}
    assert "subject_kind" not in a["payload"]
    anchored = cut.declaration_event(F, "open", "2026-09-03T05:00:00+00:00", subject_uid="flt_x")
    assert anchored["payload"]["subject_kind"] == "fleet" and anchored["payload"]["subject_alias"] == F
    with pytest.raises(ValueError):
        cut.declaration_event(F, "orphans", "2026-09-03T05:00:00+00:00")   # an unknown reader must not declare


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
    assert re.search(r"^export PLANE_READ_OPEN='?1'?$", conf, re.M) and "PLANE_READ_OVERDUE" not in conf
    _, bare = _composed(tmp_path / "b", monkeypatch, {"PLANE_READ_OPEN": "0", "PLANE_EMIT_ENABLED": "1"})
    assert "PLANE_READ_" not in bare


def test_the_fleet_pulse_unit_is_the_multi_flag_job(tmp_path, monkeypatch):
    from claudlobby.composer import FLEET_JOB_ARMING
    assert FLEET_JOB_ARMING["fleet-pulse"] == ("PLANE_READ_OVERDUE", "PLANE_READ_UNASSIGNED", "PLANE_READ_EVENTS")
    assert not any("SHADOW" in f for flags in FLEET_JOB_ARMING.values() for f in flags)
    timers, _ = _composed(tmp_path, monkeypatch, {"PLANE_READ_OVERDUE": "1", "PLANE_READ_EVENTS": "1"})
    pulse = next(p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "Environment=PLANE_READ_OVERDUE=1" in pulse and "Environment=PLANE_READ_EVENTS=1" in pulse
    assert not [p.name for p in timers.iterdir() if "plane-shadow" in p.name]   # no shadow unit composes any more
    assert "PLANE_READ_OVERDUE" not in (timers / "com.test.keepalive.service").read_text()
    timers2, _ = _composed(tmp_path / "b", monkeypatch, {"PLANE_READ_OVERDUE": "1"})
    pulse2 = next(p for p in timers2.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "PLANE_READ_OVERDUE=1" in pulse2 and "PLANE_READ_EVENTS" not in pulse2


def test_the_watchdog_names_its_fleet_on_every_matcher_call():
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    for mode in ("--all", "--orphans", "--unassigned"):
        line = next(l for l in src.splitlines() if f'dispatch-overdue.py" {mode}' in l)
        assert '--fleet "$fleet"' in line, mode
    assert "--source" not in src and "dispatch-log.jsonl" not in src


def test_env_tiers_armed_is_exact():
    from claudlobby.env_tiers import Resolution, armed
    res = lambda v: {"X": Resolution(name="X", value=v, tier="fleet", path=None)}
    assert armed(res("1"), "X") and not armed(res("0"), "X") and not armed(res("true"), "X")
    assert not armed(res("1 "), "X") and not armed({}, "X")


# --- the grammar -------------------------------------------------------------------

def test_the_grammar_refuses_a_dropped_value_and_a_path_in_the_bot_slot(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    for args in (("--open", "w1", "--fleet"),
                 ("--open", "w1", "--root", "--fleet", F),
                 ("--all", "--root"),
                 ("--all", str(NOW_EPOCH), "--fleet")):
        r = _matcher(root, *args, CLAUDLOBBY_FLEET="other")
        assert r.returncode == 2 and r.stdout == "" and "needs a value" in r.stderr, args
    for mode in ("--open", "--open-task"):                          # the #1187 shape gate
        for bad in ("/a/path", "x.jsonl", " "):
            r = _matcher(root, mode, bad, "--fleet", F)
            assert r.returncode == 2 and r.stdout == "" and "expects <bot_id> first" in r.stderr, (mode, bad)
    for args in (("--open-task", "w1"), ("--orphans", "--bots-dir", str(root)), ("--unassigned",)):
        r = _matcher(root, *args, "--fleet", F)
        assert r.returncode == 0, (args, r.stderr)
    assert _matcher(root).returncode == 2 and _matcher(root, "--source", "plane").returncode == 2   # no such mode
    for args in (("--all", "later"), ("--open-task", "w1", "soon")):
        r = _matcher(root, *args, "--fleet", F)
        assert r.returncode == 2 and "must be an integer" in r.stderr, args
    # --bots-dir need not be last in what the caller assembles: --fleet/--root are stripped first
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    a = _matcher(root, "--all", str(NOW_EPOCH), "--bots-dir", str(bots), "--fleet", F)
    b = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert a.returncode == 0 == b.returncode and a.stdout == b.stdout and a.stdout


def test_the_stdlib_open_falls_back_to_a_query_only_connection_on_cantopen(tmp_path, monkeypatch):
    """Under the system python3 a `mode=ro` URI open fails with CANTOPEN on a
    WAL database whose writer has closed; the fallback opens normally and
    holds the connection read-only by pragma — a write still refuses."""
    import sqlite3 as _sq
    root, _, _, _ = _scene(tmp_path)
    m = _stdlib_readers()
    real = _sq.connect
    seen = []

    def uri_fails(*a, **kw):
        seen.append(a[0])
        if kw.get("uri"):
            raise _sq.OperationalError("unable to open database file")
        return real(*a, **kw)
    monkeypatch.setattr(m.sqlite3, "connect", uri_fails)
    conn = m.connect(str(root))
    assert seen[0].startswith("file:") and not seen[1].startswith("file:")      # ro URI first, then the plain path
    assert {"w1", "w2"} <= set(m.roster(conn, F))
    try:
        conn.execute("DELETE FROM identity_registry")
    except _sq.OperationalError as exc:
        assert "readonly" in str(exc) or "query_only" in str(exc).lower() or "attempt to write" in str(exc)
    else:
        raise AssertionError("the fallback connection must refuse writes")
    conn.close()


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
