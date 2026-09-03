"""Cutover chunk 3: the shadow-diff primitive.

Both answers are built from ONE fixture: ledger rows in the captured shape
and the plane events the live doors would have landed for them, so the
legacy matcher (the real lib/dispatch-overdue.py, through brief's seam) and
the plane's SQL are compared on the same facts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.brief import load_dispatch_doors
from claudlobby.paths import Paths
from claudlobby.plane import shadow as sh
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root as _root, ro as _ro
from tests.test_plane_cutover_parity import _drow, _rrow, _write

REPO = Path(__file__).resolve().parent.parent
F = "f"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _paths(root):
    """An overlay root whose lib/ IS the repo's lib/ — the matcher the shadow
    loads is the install's own script, never a copy."""
    (root / "local" / F / "runtime").mkdir(parents=True, exist_ok=True)
    if not (root / "lib").exists():
        (root / "lib").symlink_to(REPO / "lib")
    return Paths(root=root, fleet_dir=root / "local" / F)


def _epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _dispatch(root, n, task_id, ts, *, bot="w1", ledger):
    """One dispatch on BOTH sides: the plane's three events + the ledger row."""
    wi, asg, msg = f"wi_{n:0>32}", f"asg_{n:0>32}", f"msg_{n:0>32}"
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"work_item_id": wi, "title": "t", "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"assignment_id": asg, "work_item_id": wi, "assignee": f"bot:{F}/{bot}",
                     "assigned_by": f"bot:{F}/mgr", "dispatch_msg_id": msg}},
        {"event_type": "communication", "emitter": "dispatch-task", "fleet": F,
         "source_ref": f"dispatch-log:{task_id}", "occurred_at": ts,
         "payload": {"msg_id": msg, "sender": f"bot:{F}/mgr", "recipient": f"bot:{F}/{bot}",
                     "message_class": "task_request", "command_type": "task",
                     "work_item_id": wi, "assignment_id": asg, "body": "t"}}])
    row = _drow(ts, task_id, bot=bot, plane=(msg, wi, asg))
    row["dispatched_at"] = _epoch(ts)
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
    _write(root / "state" / "dispatch-log.jsonl", d)
    _write(root / "local" / F / "runtime" / "report-back.jsonl", r)
    return root, paths, d, r


def _both(root, paths, bot, **kw):
    doors = load_dispatch_doors(paths)
    dlog = root / "state" / "dispatch-log.jsonl"
    rlog = root / "local" / F / "runtime" / "report-back.jsonl"
    legacy = sh.legacy_open(doors, bot, dlog, rlog, at=kw.get("at"))
    with _ro(root) as conn:
        plane = sh.plane_open(conn, F, bot, at=kw.get("at") or sh.FAR_FUTURE)
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
    _write(root / "state" / "dispatch-log.jsonl", d)
    doors, legacy, plane = _both(root, paths, "w1")
    fresh = sh.diff(F, "w1", legacy, plane, now=datetime(2026, 9, 2, 14, 5, tzinfo=timezone.utc))
    # heads: legacy [t-2, t-4] vs plane [t-2] -> the head agrees; the skew row is explained
    assert fresh.classes() == {sh.CLASS_SKEW: 1}
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
    _write(root / "state" / "dispatch-log.jsonl", d)
    doors, legacy, plane = _both(root, paths, "w1")
    assert [x.task_id for x in legacy] == ["t-5-eeee"]           # t-2 retired on the legacy side
    assert [x.task_id for x in plane] == ["t-2-bbbb"]             # still open on the plane
    sup = sh.superseded_ids(doors, "w1", root / "state" / "dispatch-log.jsonl")
    assert sup == {"t-2-bbbb"}
    d1 = sh.diff(F, "w1", legacy, plane, now=NOW, superseded=sup)
    assert {x.cls for x in d1.divergences} == {sh.CLASS_LEGACY_SUPERSEDES, sh.CLASS_DIVERGENCE}
    assert not d1.clean                                          # t-5 legacy-only: real divergence + head differs
    d2 = sh.diff(F, "w1", legacy, plane, now=NOW, superseded=sup, intentional={"t-5-eeee"})
    assert d2.classes() == {sh.CLASS_LEGACY_SUPERSEDES: 1, sh.CLASS_INTENTIONAL: 1}
    assert d2.unexplained == [] and not d2.head_agrees and not d2.clean   # heads still differ -> not clean


def test_a_redispatched_id_is_two_rows_on_both_sides(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    _dispatch(root, "6", "t-2-bbbb", "2026-09-02T15:00:00Z", ledger=d)   # same id, resent
    _write(root / "state" / "dispatch-log.jsonl", d)
    doors, legacy, plane = _both(root, paths, "w1")
    assert [x.task_id for x in legacy] == ["t-2-bbbb", "t-2-bbbb"] == [x.task_id for x in plane]
    assert sh.diff(F, "w1", legacy, plane, now=NOW).clean


# --- the record and the gate ---------------------------------------------------

def test_the_record_is_a_registry_governed_system_event_with_a_derived_id(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    doors, legacy, plane = _both(root, paths, "w1")
    d = sh.diff(F, "w1", legacy, plane, now=NOW)
    ev = sh.shadow_event(d)
    assert ev["payload"]["event"] == sh.EVENT_CLEAN and ev["payload"]["data"]["bot"] == f"bot:{F}/w1"
    assert "subject_alias" not in ev["payload"]                        # unanchored without a uid
    assert ev["event_id"] == sh.shadow_event(d)["event_id"]        # derived from (bot, instant)
    outcomes = emit_batch(root, [ev])
    assert [o.status for o in outcomes] == ["committed"]
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
    assert kind is None and uid is None                                # unanchored: keyed by data.bot
    assert anchored["payload"]["subject_kind"] == "actor" and anchored["payload"]["subject_uid"].startswith("actor_")
    assert [o.status for o in emit_batch(root, [anchored])] == ["duplicate"]   # same (bot, instant)
    bad = sh.diff(F, "w1", legacy, [], now=NOW)
    assert sh.shadow_event(bad)["payload"]["event"] == sh.EVENT_DIVERGED


def _record(root, bot, at, legacy_ids, *, clean=True):
    d = sh.ShadowDiff(F, bot, sh._iso(at), legacy_ids, list(legacy_ids) if clean else [])
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


# --- replay --------------------------------------------------------------------

def test_replay_answers_what_both_sides_held_at_a_past_instant(tmp_path):
    """At 10:30 on 09-01, t-1 was open on both sides (completed at 11:00);
    both answers are cut to the instant, so a replay grades like for like."""
    root, paths, _, _ = _scene(tmp_path)
    at = "2026-09-01T10:30:00+00:00"
    doors, legacy, plane = _both(root, paths, "w1", at=at)
    assert [x.task_id for x in legacy] == ["t-1-aaaa"] == [x.task_id for x in plane]
    assert sh.diff(F, "w1", legacy, plane, now=datetime.fromisoformat(at)).clean


# --- the CLI door --------------------------------------------------------------

def _cli(root, *args):
    return subprocess.run([sys.executable, "-m", "claudlobby", "--root", str(root),
                           "--fleet", F, "plane", "shadow", *args],
                          capture_output=True, text=True, timeout=180)


def test_cli_compares_records_and_gates(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    (root / "local" / F / "fleet.yaml").write_text(          # bots: nests under fleet:
        "fleet:\n  name: f\n  service_prefix: com.test\n  bots:\n"
        "    w1:\n      expertise: [software-engineering]\n"
        "    w2:\n      expertise: [software-engineering]\n")
    r = _cli(root, "--record", "--replay-hours", "2")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "2 bot(s) x 3 instant(s): 6 clean, 0 diverged" in r.stdout
    assert "recorded: committed=6" in r.stdout
    again = _cli(root, "--record")
    assert "duplicate=0" in again.stdout and "committed=2" in again.stdout   # a new instant, new facts
    g = _cli(root, "--gate")
    assert g.returncode == 1 and "NOT met" in g.stdout                       # 4 comparisons, no transition


def test_cli_refuses_without_the_ledgers_or_the_db(tmp_path):
    root = _root(tmp_path)
    _paths(root)
    (root / "local" / F / "fleet.yaml").write_text(
        "fleet:\n  name: f\n  service_prefix: com.test\n  bots:\n    w1:\n      expertise: [software-engineering]\n")
    r = _cli(root)
    assert r.returncode == 3 and "UNREACHABLE" in r.stdout                   # no dispatch log
    _write(root / "state" / "dispatch-log.jsonl", [])
    _write(root / "local" / F / "runtime" / "report-back.jsonl", [])
    (root / "state" / "plane" / "plane.db").unlink(missing_ok=True)   # never created: no emit yet
    r = _cli(root)
    assert r.returncode == 3 and "no plane db" in r.stdout


def test_the_launcher_is_dormant_and_the_job_is_composed_unarmed(tmp_path):
    r = subprocess.run(["bash", str(REPO / "lib" / "plane-shadow.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and "dormant" in r.stderr
    assert subprocess.run(["bash", "-n", str(REPO / "lib" / "plane-shadow.sh")]).returncode == 0
    sysyaml = (REPO / "claudlobby" / "system.yaml").read_text()
    assert "plane-shadow:" in sysyaml and 'script: "$CLAUDLOBBY_ROOT/lib/plane-shadow.sh"' in sysyaml
