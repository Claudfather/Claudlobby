"""Cutover chunk 3: the shadow-diff primitive.

Both answers are built from ONE fixture: ledger rows in the captured shape
and the plane events the live doors would have landed for them (the
chunk-2 tests' own live-dispatch helper), so the legacy matcher (the real
lib/dispatch-overdue.py, through brief's seam) and the plane's SQL are
compared on the same facts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.brief import dispatch_ledger_path, load_dispatch_doors, report_ledger_path
from claudlobby.paths import Paths
from claudlobby.plane import shadow as sh
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root as _root, ro as _ro
from tests.test_plane_cutover_parity import _drow, _live_dispatch, _rrow, _write

REPO = Path(__file__).resolve().parent.parent
F = "f"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
FLEET_YAML = ("fleet:\n  name: f\n  service_prefix: com.test\n  bots:\n"
              "    w1:\n      expertise: [software-engineering]\n"
              "    w2:\n      expertise: [software-engineering]\n")


def _paths(root):
    """An overlay root whose lib/ IS the repo's lib/ — the matcher the shadow
    loads is the install's own script, never a copy. `bots:` nests under
    `fleet:` (a top-level `bots:` parses to zero bots, silently)."""
    (root / "local" / F / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "local" / F / "fleet.yaml").write_text(FLEET_YAML)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    return Paths(root=root, fleet_dir=root / "local" / F)


def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _dispatch(root, n, task_id, ts, *, bot="w1", ledger):
    """One dispatch on BOTH sides: the live door's three plane events (the
    chunk-2 helper) + the ledger row with a real epoch-int dispatched_at."""
    row = _drow(ts, task_id, bot=bot)
    row["dispatched_at"] = _epoch(ts)
    deadline = datetime.fromtimestamp(row["expected_by"], timezone.utc).isoformat()
    wi, asg, msg = _live_dispatch(root, n, task_id, ts=ts, bot=bot, expected_by=deadline)
    row["plane_msg_id"], row["plane_work_item_id"], row["plane_assignment_id"] = msg, wi, asg
    ledger.append(row)
    return wi, asg


def _complete(root, wi, asg, ts, task_id, reports, *, bot="w1"):
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'9':0>32}", "occurred_at": ts,
                       "payload": {"work_item_id": wi, "assignment_id": asg,
                                   "event": "completed", "actor": f"bot:{F}/{bot}"}}])
    reports.append(_rrow(ts, task_id, "completed", bot=bot))


def _scene(tmp_path):
    """Two bots; w1 has one open and one completed task, w2 one open."""
    root = _root(tmp_path)
    paths = _paths(root)
    d, r = [], []
    wi1, asg1 = _dispatch(root, "1", "t-1-aaaa", "2026-09-01T10:00:00Z", ledger=d)
    _complete(root, wi1, asg1, "2026-09-01T11:00:00Z", "t-1-aaaa", r)
    _dispatch(root, "2", "t-2-bbbb", "2026-09-02T10:00:00Z", ledger=d)
    _dispatch(root, "3", "t-3-cccc", "2026-09-02T12:00:00Z", bot="w2", ledger=d)
    _write(dispatch_ledger_path(paths), d)
    _write(report_ledger_path(paths), r)
    return root, paths, d, r


def _both(root, paths, bot, *, at=None):
    doors = load_dispatch_doors(paths)
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), at) as (dl, rl):
        legacy = sh.legacy_open(doors, bot, dl, rl)
    with _ro(root) as conn:
        plane = sh.plane_open(conn, F, bot, at=at)
    return doors, legacy, plane


# --- the two answers agree on the same facts ----------------------------------

def test_the_two_answers_agree_and_the_diff_is_clean(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    doors, legacy, plane = _both(root, paths, "w1")
    assert [r.task_id for r in legacy] == ["t-2-bbbb"] == [r.task_id for r in plane]
    d = sh.diff(F, "w1", legacy, plane, now=NOW)
    assert d.clean and d.head_agrees and d.head_legacy == "t-2-bbbb" and d.divergences == []
    doors, legacy, plane = _both(root, paths, "W2")          # case-insensitive both sides
    assert [r.task_id for r in legacy] == ["t-3-cccc"] == [r.task_id for r in plane]


def test_a_legacy_only_row_is_skew_inside_the_grace_and_a_divergence_outside(tmp_path):
    """The dispatch door stamps the ledger BEFORE it emits: a row newer than
    the grace is skew; an old one the plane never got is a divergence."""
    root, paths, d, r = _scene(tmp_path)
    d.append({**_drow("2026-09-02T14:00:00Z", "t-4-dddd"), "dispatched_at": _epoch("2026-09-02T14:00:00Z")})
    _write(dispatch_ledger_path(paths), d)
    doors, legacy, plane = _both(root, paths, "w1")
    fresh = sh.diff(F, "w1", legacy, plane, now=datetime(2026, 9, 2, 14, 5, tzinfo=timezone.utc))
    assert fresh.classes() == {sh.CLASS_SKEW: 1}          # heads: [t-2, t-4] vs [t-2] -> agree
    assert fresh.head_agrees and fresh.unexplained == [] and fresh.clean
    stale = sh.diff(F, "w1", legacy, plane, now=NOW)
    assert stale.classes() == {sh.CLASS_DIVERGENCE: 1} and not stale.clean
    assert stale.divergences[0].side == "legacy_only"


def test_a_pre_cutover_supersession_is_explained_and_an_intentional_id_too(tmp_path):
    """The JSONL retired t-2 by --supersedes before chunk 1 wired it to the
    plane: the plane still holds it open — explained, not a divergence."""
    root, paths, d, r = _scene(tmp_path)
    d.append({**_drow("2026-09-02T13:00:00Z", "t-5-eeee"), "dispatched_at": _epoch("2026-09-02T13:00:00Z"),
              "supersedes": "t-2-bbbb"})
    _write(dispatch_ledger_path(paths), d)
    doors, legacy, plane = _both(root, paths, "w1")
    assert [x.task_id for x in legacy] == ["t-5-eeee"]           # t-2 retired on the legacy side
    assert [x.task_id for x in plane] == ["t-2-bbbb"]             # still open on the plane
    sup = sh.superseded_by_bot(doors, dispatch_ledger_path(paths))
    assert sup == {"w1": {"t-2-bbbb"}}
    d1 = sh.diff(F, "w1", legacy, plane, now=NOW, superseded=sup["w1"])
    assert {x.cls for x in d1.divergences} == {sh.CLASS_LEGACY_SUPERSEDES, sh.CLASS_DIVERGENCE}
    assert not d1.clean                                          # t-5 legacy-only: real + head differs
    d2 = sh.diff(F, "w1", legacy, plane, now=NOW, superseded=sup["w1"], intentional={"t-5-eeee"})
    assert d2.classes() == {sh.CLASS_LEGACY_SUPERSEDES: 1, sh.CLASS_INTENTIONAL: 1}
    assert d2.unexplained == [] and not d2.head_agrees and not d2.clean   # heads still differ


def test_a_redispatched_id_is_two_rows_on_both_sides_and_one_report_closes_both(tmp_path):
    """The legacy join closes by (bot, task id): a redispatched id is two
    open rows on both sides, and ONE terminal report closes every row that
    carried it — the plane answer mirrors that, deliberately."""
    root, paths, d, r = _scene(tmp_path)
    wi6, asg6 = _dispatch(root, "6", "t-2-bbbb", "2026-09-02T15:00:00Z", ledger=d)   # same id, resent
    _write(dispatch_ledger_path(paths), d)
    doors, legacy, plane = _both(root, paths, "w1")
    assert [x.task_id for x in legacy] == ["t-2-bbbb", "t-2-bbbb"] == [x.task_id for x in plane]
    assert sh.diff(F, "w1", legacy, plane, now=NOW).clean
    _complete(root, wi6, asg6, "2026-09-02T16:00:00Z", "t-2-bbbb", r)   # the newer assignment reports
    _write(report_ledger_path(paths), r)
    doors, legacy, plane = _both(root, paths, "w1")
    assert legacy == [] and plane == []


# --- the record and the gate ---------------------------------------------------

def test_the_record_is_a_registry_governed_system_event_with_a_derived_id(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    doors, legacy, plane = _both(root, paths, "w1")
    d = sh.diff(F, "w1", legacy, plane, now=NOW)
    ev = sh.shadow_event(d)
    assert ev["payload"]["event"] == sh.EVENT_CLEAN and ev["payload"]["data"]["bot"] == f"bot:{F}/w1"
    assert "subject_alias" not in ev["payload"]                        # unanchored without a uid
    assert ev["event_id"] == sh.shadow_event(d)["event_id"]            # derived from (bot, instant)
    assert [o.status for o in emit_batch(root, [ev])] == ["committed"]
    assert [o.status for o in emit_batch(root, [ev])] == ["duplicate"]
    conn = connect(db_path(root))
    try:
        sev, detail, kind, uid = conn.execute(
            "SELECT severity, detail, subject_kind, subject_uid FROM events"
            " WHERE kind='system' AND event=?", (sh.EVENT_CLEAN,)).fetchone()
        with _ro(root) as ro_conn:
            anchored = sh.shadow_event(d, subject_uid=sh.actor_uid(ro_conn, f"bot:{F}/w1"))
    finally:
        conn.close()
    assert sev == "notice" and json.loads(detail)["legacy"] == ["t-2-bbbb"]
    assert kind is None and uid is None                                # keyed by data.bot
    assert anchored["payload"]["subject_kind"] == "actor" and anchored["payload"]["subject_uid"].startswith("actor_")
    assert [o.status for o in emit_batch(root, [anchored])] == ["duplicate"]   # same (bot, instant)
    bad = sh.diff(F, "w1", legacy, [], now=NOW)
    assert sh.shadow_event(bad)["payload"]["event"] == sh.EVENT_DIVERGED


def test_the_record_stays_far_under_the_diagnostic_cap(tmp_path):
    """A record that truncated would lose its JSON and its bot key — and the
    bigger the divergence the likelier the truncation. Every list is capped."""
    ids = [f"t-{i}-{i:04x}" for i in range(500)]
    d = sh.diff(F, "w1", [sh.OpenRow(i, "2026-01-01T00:00:00+00:00") for i in ids],
                [], now=NOW)
    data = sh.shadow_event(d)["payload"]["data"]
    assert data["legacy_n"] == 500 and len(data["legacy"]) == sh.LIST_CAP
    assert data["divergences_n"] == 500 and len(data["divergences"]) == sh.DIVERGENCE_CAP
    assert len(json.dumps(data).encode()) < 12000


def _record(root, bot, at, legacy_ids, *, clean=True):
    d = sh.ShadowDiff(F, bot, sh.dt_iso(at), legacy_ids, list(legacy_ids) if clean else [])
    emit_batch(root, [sh.shadow_event(d)])


def test_the_gate_needs_a_clean_run_with_a_transition(tmp_path):
    root = _root(tmp_path)
    t0 = NOW - timedelta(hours=40)
    for k in range(sh.GATE_CLEAN_RUN):                 # 20 clean, the open set never changes
        _record(root, "w1", t0 + timedelta(hours=k), ["t-1-aaaa"])
    with _ro(root) as conn:
        s = sh.streak(conn, F, "w1")
    assert s.clean_run == sh.GATE_CLEAN_RUN and s.transitions == 0 and not s.gate_ok
    _record(root, "w1", t0 + timedelta(hours=21), [])  # t-1 closed: a transition
    with _ro(root) as conn:
        s = sh.streak(conn, F, "w1")
    assert s.clean_run == sh.GATE_CLEAN_RUN + 1 and s.transitions == 1 and s.gate_ok
    _record(root, "w1", t0 + timedelta(hours=22), ["t-2-bbbb"], clean=False)   # a divergence resets
    with _ro(root) as conn:
        s = sh.streak(conn, F, "w1")
        assert s.clean_run == 0 and s.transitions == 0 and not s.gate_ok
        assert s.last_diverged_at and s.comparisons == sh.GATE_CLEAN_RUN + 2
        assert sh.shadowed_bots(conn, F) == ["w1"]
        summary = sh.gate_summary(conn, F, ["w1", "w2"], readers=(sh.READER_OPEN,))   # w2 declared, never compared: SHORT
        assert [(x.bot, x.comparisons, x.gate_ok) for x in summary] == [("w1", 22, False), ("w2", 0, False)]


def test_a_truncated_divergence_still_ends_the_run(tmp_path):
    """The caps make truncation unreachable through shadow_event; the gate
    must still survive a record that lost its JSON — keyed by its subject
    alias — and a truncated DIVERGENCE ends the run rather than vanishing.
    The record is landed through REAL ingest with an over-cap data field."""
    root, paths, _, _ = _scene(tmp_path)
    t0 = NOW - timedelta(hours=10)
    for k in range(5):
        _record(root, "w1", t0 + timedelta(hours=k), ["t-1-aaaa"])
    with _ro(root) as conn:
        uid = sh.actor_uid(conn, f"bot:{F}/w1")
    assert uid
    emit_batch(root, [{"event_type": "system", "emitter": "plane-shadow", "fleet": F,
                       "occurred_at": sh.dt_iso(t0 + timedelta(hours=6)),
                       "payload": {"event": sh.EVENT_DIVERGED, "subject_kind": "actor",
                                   "subject_uid": uid, "subject_alias": f"bot:{F}/w1",
                                   "data": {"bot": f"bot:{F}/w1", "blob": "x" * 20000}}}])
    _record(root, "w1", t0 + timedelta(hours=7), ["t-1-aaaa"])
    with _ro(root) as conn:
        truncated = conn.execute("SELECT COUNT(*) FROM events WHERE detail_truncated = 1").fetchone()[0]
        s = sh.streak(conn, F, "w1")
    assert truncated == 1
    assert s.clean_run == 1 and s.last_diverged_at and s.comparisons == 7


def test_record_lands_a_replay_beside_a_live_comparison(tmp_path):
    """The ingest refuses a MIXED batch; record() lands one instant per batch."""
    root, paths, _, _ = _scene(tmp_path)
    marks = sh.replay_instants(NOW, 2)
    live = NOW + timedelta(seconds=30)          # a live instant never IS an hour mark in practice
    def events(now):
        return [sh.shadow_event(sh.ShadowDiff(F, "w1", sh.dt_iso(at), ["t-2-bbbb"], ["t-2-bbbb"]))
                for at in marks + [now]]
    assert sh.record(root, events(live)) == {"committed": 3, "duplicate": 0, "spooled": 0}
    later = NOW + timedelta(minutes=5)
    assert sh.record(root, events(later)) == {"committed": 1, "duplicate": 2, "spooled": 0}
    # a live instant that coincides with a mark is ONE fact, not an intra-batch collision
    assert sh.record(root, events(NOW)) == {"committed": 0, "duplicate": 2, "spooled": 0}


# --- replay --------------------------------------------------------------------

def test_replay_answers_what_both_sides_held_at_a_past_instant(tmp_path):
    """At 10:30 on 09-01, t-1 was open on both sides (completed at 11:00);
    both answers are cut to the instant, so a replay grades like for like."""
    root, paths, _, _ = _scene(tmp_path)
    at = "2026-09-01T10:30:00+00:00"
    doors, legacy, plane = _both(root, paths, "w1", at=at)
    assert [x.task_id for x in legacy] == ["t-1-aaaa"] == [x.task_id for x in plane]
    assert sh.diff(F, "w1", legacy, plane, now=datetime.fromisoformat(at)).clean


def test_replay_instants_are_hour_marks_and_ledgers_at_leaves_nothing_behind(tmp_path):
    now = datetime(2026, 9, 3, 12, 34, 56, 789, tzinfo=timezone.utc)
    marks = sh.replay_instants(now, 3)
    assert [m.isoformat() for m in marks] == ["2026-09-03T10:00:00+00:00", "2026-09-03T11:00:00+00:00",
                                              "2026-09-03T12:00:00+00:00"]
    assert sh.replay_instants(now + timedelta(seconds=30), 3) == marks      # same marks a re-run later
    root, paths, _, _ = _scene(tmp_path)
    import glob, tempfile
    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "plane-shadow-*")))
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), "2026-09-02T00:00:00+00:00") as (dl, rl):
        assert Path(dl).exists() and Path(rl).exists()
    assert set(glob.glob(str(Path(tempfile.gettempdir()) / "plane-shadow-*"))) == before


# --- the CLI door --------------------------------------------------------------

def _cli(root, *args):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
                           "--fleet", F, "plane", "shadow", *args],
                          capture_output=True, text=True, timeout=180)


def test_cli_compares_records_and_gates(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _cli(root, "--record", "--replay-hours", "2")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "2 bot(s) x 2 reader(s) x 3 instant(s): 12 clean, 0 diverged" in r.stdout
    assert "recorded: committed=12" in r.stdout
    again = _cli(root, "--record", "--replay-hours", "2")
    assert "committed=4 duplicate=8" in again.stdout        # the hour marks replay as duplicates
    g = _cli(root, "--gate")
    assert g.returncode == 1 and "NOT met" in g.stdout and "w2 [open]:" in g.stdout   # 4 comparisons, no transition
    assert _cli(root, "--bot", "nobody").returncode == 2                        # not on the roster
    assert _cli(root, "--intentional", "t-1-aaaa, t-2-bbbb").returncode == 0    # spaces tolerated


def test_cli_refuses_without_the_ledgers_the_db_or_the_manifest(tmp_path):
    root = _root(tmp_path)
    paths = _paths(root)
    r = _cli(root)
    assert r.returncode == 3 and "UNREACHABLE" in r.stdout                   # no dispatch log
    _write(dispatch_ledger_path(paths), [])
    _write(report_ledger_path(paths), [])
    (root / "state" / "plane" / "plane.db").unlink(missing_ok=True)          # never created: no emit yet
    r = _cli(root)
    assert r.returncode == 3 and "no plane db" in r.stdout
    (root / "local" / F / "fleet.yaml").unlink()
    r = _cli(root)
    assert r.returncode == 3 and "fleet manifest" in r.stdout and "Traceback" not in r.stderr


def test_the_launcher_is_dormant_and_the_job_is_composed_unarmed(tmp_path):
    r = subprocess.run(["bash", str(REPO / "lib" / "plane-shadow.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and "dormant" in r.stderr
    assert subprocess.run(["bash", "-n", str(REPO / "lib" / "plane-shadow.sh")]).returncode == 0
    sysyaml = (REPO / "claudlobby" / "system.yaml").read_text()
    assert "plane-shadow:" in sysyaml and 'script: "$CLAUDLOBBY_ROOT/lib/plane-shadow.sh"' in sysyaml


# --- chunk 4: the overdue reader, the check, the bridge, brief -----------------

def _epoch_of(dt):
    return int(dt.timestamp())


def test_the_overdue_reader_agrees_and_mirrors_the_watchdog_rules(tmp_path):
    """Both answers to the watchdog's question on one fixture: a past
    deadline is overdue on both sides; a report closes it on both; a
    progress report inside the grace shields it on both; the expiry cap
    drops an ancient one on both."""
    root, paths, d, r = _scene(tmp_path)
    now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    doors = load_dispatch_doors(paths)
    max_age = doors.DEFAULT_OVERDUE_MAX_AGE_S
    grace = doors._resolve_progress_grace()
    # t-2 (dispatched 09-02 10:00, expected_by from the fixture = 1788000000 = 08-29): overdue
    with _ro(root) as conn:
        plane = sh.plane_overdue(conn, F, "w1", now=now, max_age_s=max_age, progress_grace_s=grace)
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), None) as (dl, rl):
        legacy = sh.legacy_overdue(doors, "w1", dl, rl, now=now, max_age_s=max_age)
    assert [x.task_id for x in legacy] == ["t-2-bbbb"] == [x.task_id for x in plane]
    assert sh.diff(F, "w1", legacy, plane, now=now, reader=sh.READER_OVERDUE).clean
    # a progress report by w1 inside the grace shields the row on both sides
    prog_at = now - timedelta(seconds=min(grace, 600) // 2)
    wi2, asg2 = next((f"wi_{'2':0>32}", f"asg_{'2':0>32}") for _ in [0])
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'8':0>32}", "occurred_at": sh.dt_iso(prog_at),
                       "payload": {"work_item_id": wi2, "assignment_id": asg2, "event": "progress",
                                   "actor": f"bot:{F}/w1", "progress": 10}}])
    r.append({**_rrow(sh.dt_iso(prog_at), "t-2-bbbb", "progress", progress="10"), "ts": sh.dt_iso(prog_at).replace("+00:00", "Z")})
    _write(report_ledger_path(paths), r)
    with _ro(root) as conn:
        plane = sh.plane_overdue(conn, F, "w1", now=now, max_age_s=max_age, progress_grace_s=grace)
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), None) as (dl, rl):
        legacy = sh.legacy_overdue(doors, "w1", dl, rl, now=now, max_age_s=max_age)
    assert legacy == [] and plane == []
    # far later, the expiry cap drops the ancient row on both sides
    later = now + timedelta(days=30)
    with _ro(root) as conn:
        plane = sh.plane_overdue(conn, F, "w1", now=later, max_age_s=max_age, progress_grace_s=grace)
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), None) as (dl, rl):
        legacy = sh.legacy_overdue(doors, "w1", dl, rl, now=later, max_age_s=max_age)
    assert legacy == [] and plane == []


def test_records_and_streaks_are_keyed_by_reader(tmp_path):
    root = _root(tmp_path)
    t0 = NOW - timedelta(hours=5)
    d_open = sh.ShadowDiff(F, "w1", sh.dt_iso(t0), ["t-1-aaaa"], ["t-1-aaaa"], reader=sh.READER_OPEN)
    d_over = sh.ShadowDiff(F, "w1", sh.dt_iso(t0), ["t-1-aaaa"], [], reader=sh.READER_OVERDUE)
    assert sh.shadow_event(d_open)["event_id"] != sh.shadow_event(d_over)["event_id"]
    sh.record(root, [sh.shadow_event(d_open), sh.shadow_event(d_over)])
    with _ro(root) as conn:
        assert sh.streak(conn, F, "w1", sh.READER_OPEN).clean_run == 1
        s_over = sh.streak(conn, F, "w1", sh.READER_OVERDUE)
        assert s_over.clean_run == 0 and s_over.last_diverged_at
        assert sh.latest_diverged(conn, F, ["w1"]) == [("w1", sh.READER_OVERDUE, sh.dt_iso(t0))]
        pairs = {(x.bot, x.reader): x.gate_ok for x in sh.gate_summary(conn, F, ["w1", "w2"])}
    assert set(pairs) == {("w1", "open"), ("w1", "overdue"), ("w2", "open"), ("w2", "overdue")}
    assert not any(pairs.values())


def test_cli_check_reader_and_gate_per_reader(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _cli(root, "--record")
    assert r.returncode in (0, 1), r.stdout + r.stderr
    assert "x 2 reader(s) x 1 instant(s)" in r.stdout
    c = _cli(root, "--check")
    assert c.returncode == 0 and "0 diverged (bot, reader) pair(s)" in c.stdout
    only = _cli(root, "--reader", "open")
    assert "x 1 reader(s)" in only.stdout and "[overdue]" not in only.stdout
    g = _cli(root, "--gate")
    assert g.returncode == 1 and "w1 [overdue]" in g.stdout and "(bot, reader) pairs met" in g.stdout
    # a recorded divergence flips the check
    d_bad = sh.ShadowDiff(F, "w2", sh.dt_iso(NOW + timedelta(hours=1)), ["t-3-cccc"], [], reader=sh.READER_OPEN)
    sh.record(root, [sh.shadow_event(d_bad)])
    c2 = _cli(root, "--check")
    assert c2.returncode == 1 and "w2/open" in c2.stdout


def test_the_fleet_pulse_bridge_is_gated_and_reads_the_check():
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    assert 'plane shadow --check' in src and '"${PLANE_SHADOW_ENABLED:-0}" = "1"' in src
    assert "escalation_shadow_divergence" in src
    assert src.index("_shadow_bridge() {") < src.index("_shadow_bridge || true")
    assert subprocess.run(["bash", "-n", str(REPO / "lib" / "fleet-pulse.sh")]).returncode == 0


def test_brief_carries_the_streaks_and_degrades_on_silence(tmp_path):
    from claudlobby.brief import build_brief, format_brief
    from claudlobby.config import load_fleet
    root, paths, _, _ = _scene(tmp_path)
    fleet, _ = load_fleet(paths.fleet_yaml)
    now = int(NOW.timestamp())
    b = build_brief(fleet, paths, "w1", now)
    assert set(b["shadow"]) == {"open", "overdue"}
    assert any(x["field"] == "shadow" for x in b["degraded"])          # nothing recorded yet
    assert "SHADOW — cutover comparisons" in format_brief(b)
    _cli(root, "--record")
    b2 = build_brief(fleet, paths, "w1", now)
    assert b2["shadow"]["open"]["comparisons"] == 1 and b2["shadow"]["open"]["gate_ok"] is False
    assert not any(x["field"] == "shadow" for x in b2["degraded"])
    assert "open: clean_run 1/20" in format_brief(b2)
    (root / "state" / "plane" / "plane.db").unlink()
    b3 = build_brief(fleet, paths, "w1", now)
    assert b3["shadow"] == {} and any(x["field"] == "shadow" and x["mode"] == "omitted" for x in b3["degraded"])


def test_a_future_deadline_is_open_but_not_overdue_on_both_sides(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)
    future = int((now + timedelta(days=2)).timestamp())
    row = _drow("2026-09-02T13:00:00Z", "t-7-7777", expected_by=future)
    row["dispatched_at"] = _epoch("2026-09-02T13:00:00Z")
    _live_dispatch(root, "7", "t-7-7777", ts="2026-09-02T13:00:00Z",
                   expected_by=datetime.fromtimestamp(future, timezone.utc).isoformat())
    d.append(row)
    _write(dispatch_ledger_path(paths), d)
    doors = load_dispatch_doors(paths)
    with _ro(root) as conn:
        open_ids = [x.task_id for x in sh.plane_open(conn, F, "w1")]
        over = [x.task_id for x in sh.plane_overdue(conn, F, "w1", now=now,
                                                     max_age_s=doors.DEFAULT_OVERDUE_MAX_AGE_S,
                                                     progress_grace_s=0)]
    with sh.ledgers_at(dispatch_ledger_path(paths), report_ledger_path(paths), None) as (dl, rl):
        legacy = [x.task_id for x in sh.legacy_overdue(doors, "w1", dl, rl, now=now,
                                                         max_age_s=doors.DEFAULT_OVERDUE_MAX_AGE_S)]
    assert "t-7-7777" in open_ids and "t-7-7777" not in over and over == legacy == ["t-2-bbbb"]
