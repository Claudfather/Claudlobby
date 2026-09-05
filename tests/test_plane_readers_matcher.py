"""The matcher's list readers (`--open`, `--all`, `--orphans`) answer from the
PLANE and from nothing else — the plane-reader pins that survived the F18
closure's cutover-era suite (chunk 5 → R2a → R3).

The matcher opens the plane under `--fleet`/`--root` (else the CLAUDLOBBY_FLEET
→ FLEET_NAME and CLAUDLOBBY_ROOT carriers) and refuses at rc 3 when it cannot
(UNREACHABLE is not empty). No flag, no declaration, no gate: R3 deleted the
cutover machinery, and the composer stamps no transition flag (pinned below).

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


F18 closure R3 — the cutover machinery is gone (no flags, no declarations, no
doctor rungs), so these went with it: test_the_reader_set_and_its_flags_are_one_fact,
test_cutover_declares_a_direct_move_and_records_the_reason,
test_the_declaration_id_is_derived_so_a_re_run_at_one_instant_is_one_fact,
test_the_latest_declaration_wins_and_a_same_instant_re_run_is_disclosed,
test_flag_vs_declaration_names_the_missing_half, test_the_doctor_reads_the_flag_against_the_declaration,
test_bot_conf_carries_the_read_flags_the_fleet_tier_arms, test_the_fleet_pulse_unit_is_the_multi_flag_job.
What remains is the plane-reader suite (the file is renamed to say so).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from claudlobby.plane import queries
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import (F, NOW_EPOCH, REPO, _cli, _matcher, _scene,
                                  _stdlib_readers, plane_root, ro as _ro)
from tests.plane_fixtures import _live_dispatch


# --- the twins cannot drift --------------------------------------------------------

def test_the_stdlib_open_sql_is_byte_identical_to_the_package():
    assert _stdlib_readers().OPEN_SQL == queries.OPEN_ASSIGNMENTS_AT_SQL


def test_the_stdlib_report_and_ack_sql_are_byte_identical_to_the_package():
    """Chunk K: a fleet's reports and its read position are defined ONCE
    (queries.py); the stdlib reader's copies cannot drift from them."""
    pr = _stdlib_readers()
    assert pr.FLEET_REPORTS_SQL == queries.FLEET_REPORTS_SQL
    assert pr.NEWEST_ACK_SQL == queries.NEWEST_ACK_SQL


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
    timers = compose_fleet_timers(fleet, paths, md)
    conf = compose_bot_conf(next(iter(fleet.bots.values())), fleet, paths)
    return timers, conf


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


def test_the_composer_stamps_no_transition_flag_whatever_the_tier_says(tmp_path, monkeypatch):
    """F18 closure R3: the cutover carriers are gone. A tier that still says
    PLANE_READ_OPEN=1 or PLANE_LEGACY_WRITE_DISPATCH=0 composes NOTHING of it
    into bot.conf or the fleet-pulse unit; the emission arming still rides."""
    from claudlobby.composer import FLEET_JOB_ARMING
    timers, conf = _composed(tmp_path, monkeypatch, {"PLANE_READ_OPEN": "1", "PLANE_READ_OVERDUE": "1",
                                                     "PLANE_LEGACY_WRITE_DISPATCH": "0", "PLANE_EMIT_ENABLED": "1"})
    assert "PLANE_READ_" not in conf and "PLANE_LEGACY_WRITE_" not in conf
    pulse = next(p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "PLANE_READ_" not in pulse and "PLANE_LEGACY_WRITE_" not in pulse
    assert "Environment=PLANE_EMIT_ENABLED=1" in pulse
    assert "fleet-pulse" not in FLEET_JOB_ARMING and FLEET_JOB_ARMING["keepalive"] == ("PLANE_EMIT_ENABLED",)


def test_the_stdlib_readers_hold_no_cutover_twin():
    """`declared` / `retired` and their SQL went with the cutover facts (R3)."""
    pr = _stdlib_readers()
    assert not any(hasattr(pr, n) for n in ("declared", "retired", "DECLARED_SQL", "RETIRED_SQL"))


def test_the_orphan_list_is_the_planes_own_split(tmp_path):
    """Every dispatch the plane holds for a bot older than its .spawn is the
    orphan list's — the one landed by the live door and the scene's alike;
    a bot that never respawned contributes nothing."""
    import os
    root, paths, _, _ = _scene(tmp_path)
    _live_dispatch(root, "8", "t-8-only-plane", ts="2026-09-02T09:00:00Z", expected_by="2026-09-02T10:00:00+00:00")
    bots = root / "bots"
    (bots / "w1" / "data").mkdir(parents=True)
    spawn = bots / "w1" / "data" / ".spawn"
    spawn.write_text("")
    os.utime(spawn, (NOW_EPOCH - 60, NOW_EPOCH - 60))                  # w1 respawned after both dispatches
    orphans = _matcher(root, "--orphans", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert orphans.returncode == 0, orphans.stderr
    assert "t-8-only-plane" in orphans.stdout and "t-2-bbbb" in orphans.stdout
    assert "w2" not in orphans.stdout and "t-3-cccc" not in orphans.stdout
    over = _matcher(root, "--all", str(NOW_EPOCH), "--fleet", F, "--bots-dir", str(bots))
    assert "t-8-only-plane" not in over.stdout and "t-3-cccc" in over.stdout      # split, never paged twice
