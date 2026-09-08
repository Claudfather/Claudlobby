"""Attention expiry sweep — emits `expired` for assignments overdue past the
horizon, through normal ingest. Laws pinned: only stale NON-terminal
assignments (a fresh one and a completed one are untouched); the event
removes the card from ATTENTION_SQL and TASK_STATUS_SQL says `expired`;
a second run is a no-op (idempotent by construction); dry-run emits
nothing; the launcher self-gates; the composer stamps the arming flag.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.expiry import expirable, expired_events
from claudlobby.plane.queries import ATTENTION_SQL, TASK_STATUS_SQL, attention_params
from tests.plane_fixtures import plane_root

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
F = "example-fleet"


def _root(tmp_path):
    return plane_root(tmp_path)


def _dispatch(root, n, *, expected_by):
    """work_item + assignment — the 6b fixture shape."""
    wi, aid = f"wi_{n:0>32}", f"asg_{n:0>32}"   # ID_PATTERNS: asg_ + 32 hex
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": F,
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": f"bot:{F}/mgr"}},
        {"event_type": "assignment", "emitter": "t", "fleet": F,
         "payload": {"assignment_id": aid, "work_item_id": wi,
                     "assignee": f"bot:{F}/w1", "assigned_by": f"bot:{F}/mgr",
                     "expected_by": expected_by.isoformat(),
                     "dispatch_msg_id": f"msg_{n:0>32}"}}])
    return wi, aid


def _complete(root, wi, aid):
    emit_batch(root, [{"event_type": "task", "emitter": "t", "fleet": F,
                       "payload": {"work_item_id": wi, "assignment_id": aid,
                                   "event": "completed"}}])


def _live_now():
    """The instant the CLI and the launcher will actually use."""
    return datetime.now(timezone.utc)


def _seed(root, ref):
    """Seed the three rows RELATIVE to `ref`. The parameter is REQUIRED, and
    that is the fix rather than an inconvenience.

    This file has two kinds of test and they run on two different clocks: the
    in-process ones pass `now=NOW` to `expirable()` and are frozen, while the
    CLI and launcher ones shell out to a real process that reads the wall
    clock. While the seed hardcoded NOW, the second kind was a time bomb —
    `fresh` (NOW - 2d) sat 7 days short of the horizon on 2026-09-02 and
    crossed it on 2026-09-07T12:00:00Z, at which point the sweep counted 2
    where the assertion says 1. It went off 15 minutes after the last green
    run on main and took every PR on the repo with it (#1498).

    A default value here would re-arm it: the next live-clock test would get
    the frozen instant by omission, which is precisely how this one was
    written. So every caller must say which clock it is on, and the DATES
    disappear — only the offsets remain, and those hold at every instant.
    `test_the_seed_holds_at_any_instant` pins that.
    """
    stale = _dispatch(root, "a", expected_by=ref - timedelta(days=10))
    fresh = _dispatch(root, "b", expected_by=ref - timedelta(days=2))
    done = _dispatch(root, "c", expected_by=ref - timedelta(days=30))
    _complete(root, *done)
    return stale, fresh, done


def test_only_stale_non_terminal_assignments_are_expirable(tmp_path):
    root = _root(tmp_path)
    stale, fresh, done = _seed(root, NOW)
    conn = connect(db_path(root))
    try:
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    assert [r["assignment_id"] for r in plan.rows] == [stale[1]]
    assert plan.rows[0]["fleet"] == F
    assert plan.unattributed == []


def test_the_seed_holds_at_any_instant(tmp_path):
    """The property the hardcoded dates did not have (#1498).

    The old seed was correct on the day it was written and wrong seven days
    later, and nothing in the file said which day that was. This asserts the
    invariant instead of the dates: at ANY reference instant the seed produces
    exactly one expirable row, so no clock can arm it.

    The instants below are deliberately chosen to include the one that did:
    2026-09-07T12:00Z is when the old `fresh` row (NOW - 2d) crossed the 7-day
    horizon and turned a green suite red for the whole repo.
    """
    for i, ref in enumerate([
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        NOW,
        datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),   # the trigger
        datetime(2026, 9, 7, 12, 0, 1, tzinfo=timezone.utc),
        _live_now(),
        datetime(2099, 12, 31, tzinfo=timezone.utc),
    ]):
        root = _root(tmp_path / f"i{i}")
        stale, _fresh, _done = _seed(root, ref)
        conn = connect(db_path(root))
        try:
            plan = expirable(conn, now=ref, after_days=7)
        finally:
            conn.close()
        assert [r["assignment_id"] for r in plan.rows] == [stale[1]], (
            f"seed is not clock-relative at {ref.isoformat()}"
        )


def test_expired_event_clears_attention_and_sets_status_idempotently(tmp_path):
    root = _root(tmp_path)
    stale, fresh, done = _seed(root, NOW)
    conn = connect(db_path(root))
    try:
        before = [r[0] for r in conn.execute(
            ATTENTION_SQL, attention_params(NOW.isoformat()))]
        assert stale[1] in before and fresh[1] in before   # both overdue
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    emit_batch(root, expired_events(plan, now=NOW, after_days=7))
    conn = connect(db_path(root))
    try:
        after = [r[0] for r in conn.execute(
            ATTENTION_SQL, attention_params(NOW.isoformat()))]
        assert stale[1] not in after                # the card is gone
        assert fresh[1] in after                    # the fresh one stays
        assert {r[0]: r[1] for r in conn.execute(TASK_STATUS_SQL)}[stale[1]] == "expired"
        again = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    assert again.rows == []                         # idempotent: nothing left


def test_negative_horizon_refused():
    import pytest
    import sqlite3
    with pytest.raises(ValueError):
        expirable(sqlite3.connect(":memory:"), now=NOW, after_days=-1)


def _cli(root, *argv):
    return subprocess.run(
        [sys.executable, "-m", "claudlobby", "--root", str(root),
         "plane", "expire", *argv], capture_output=True, text=True, timeout=120)


def test_cli_dry_run_then_live(tmp_path):
    root = _root(tmp_path)
    _seed(root, _live_now())   # shells out: real clock, not NOW
    dry = _cli(root, "--dry-run")
    assert dry.returncode == 0 and "would expire 1" in dry.stdout
    conn = connect(db_path(root))
    try:
        assert not list(conn.execute(
            "SELECT 1 FROM events WHERE kind='task' AND event='expired'"))
    finally:
        conn.close()
    live = _cli(root)
    assert live.returncode == 0 and "expired 1" in live.stdout
    conn = connect(db_path(root))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'"
                         " AND event='expired'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert _cli(root, "--after-days", "-1").returncode == 2   # clean refusal
    assert _cli(tmp_path / "nope").returncode == 0            # absent db no-op


def _launcher(root, *argv, armed):
    env = dict(os.environ, CLAUDLOBBY_ROOT=str(root),
               PATH=f"{REPO / '.venv' / 'bin'}:" + os.environ.get("PATH", ""))
    # Since the defaults flip the flag is an opt-OUT: absence RUNS the sweep,
    # and only an exact 0 stops it. The harness spells both explicitly rather
    # than relying on absence, so the pin reads the same way the door does.
    env["PLANE_EXPIRE_ENABLED"] = "1" if armed else "0"
    return subprocess.run(["bash", str(REPO / "lib" / "plane-expire.sh"), *argv],
                          capture_output=True, text=True, timeout=120, env=env)


def test_launcher_runs_by_default_and_the_off_switch_is_LOUD(tmp_path):
    """Opt-OUT since the defaults flip. Two halves, and the second is the one
    the ruling is about: a door turned off must SAY so, because a silent skip
    is indistinguishable from a broken timer and that ambiguity is exactly
    what a dormant-by-default estate taught operators to ignore."""
    root = _root(tmp_path)
    _seed(root, _live_now())   # shells out: real clock, not NOW
    on = _launcher(root, "--dry-run", armed=True)
    assert on.returncode == 0 and "would expire 1" in on.stdout
    off = _launcher(root, "--dry-run", armed=False)
    assert off.returncode == 0
    # REWRITTEN by the fold (F6): the loud line is now the SHARED gate's
    # (lib-common `switch_is_on`), not this door's own copy — four launchers
    # had four spellings of one comparison. What is pinned is unchanged: the
    # door names itself, names the flag, and says what will not happen.
    assert "plane-expire: OFF here" in off.stderr
    assert "PLANE_EXPIRE_ENABLED=0" in off.stderr
    assert "no assignment will be expired" in off.stderr
    assert "would expire" not in off.stdout


def test_launcher_runs_with_no_flag_at_all(tmp_path):
    """Absence is ON — the flip itself, pinned. This is the assertion that
    fails if someone restores `${FLAG:-0}` while leaving the comments alone."""
    import os as _os
    root = _root(tmp_path)
    _seed(root, _live_now())   # shells out: real clock, not NOW
    env = dict(HOME=str(root), CLAUDLOBBY_ROOT=str(root),
               PATH=f"{REPO / '.venv' / 'bin'}:" + _os.environ.get("PATH", ""))
    r = subprocess.run(["bash", str(REPO / "lib" / "plane-expire.sh"), "--dry-run"],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0 and "would expire 1" in r.stdout


def test_job_composes_and_carries_its_own_flag(tmp_path, monkeypatch):
    import yaml
    from claudlobby.composer import compose_host_timers
    from claudlobby.paths import Paths
    from claudlobby.env_tiers import Resolution
    import claudlobby.env_tiers as et

    job = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())[
        "host"]["jobs"]["plane-expire"]
    # Enrolled by default since the defaults flip: absence of `enroll` IS
    # enrolled for a host timer.
    assert job.get("enroll", True) is True and "plane-expire.sh" in job["script"]
    root = tmp_path / "r"
    (root / "claudlobby").mkdir(parents=True)
    (root / "claudlobby" / "system.yaml").write_text(
        "host:\n  jobs:\n    plane-expire:\n      enroll: false\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/plane-expire.sh\"\n"
        "      schedule: \"*-*-* 05:30:00\"\n      type: oneshot\n"
        "    plane-prune:\n      enroll: false\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/plane-prune.sh\"\n"
        "      schedule: \"*-*-* 05:15:00\"\n      type: oneshot\n")
    monkeypatch.setattr(et, "read_tiers", lambda paths, bot_name=None, fleet_name=None: [])
    monkeypatch.setattr(et, "cascade", lambda tiers: {
        "PLANE_EXPIRE_ENABLED": Resolution(name="PLANE_EXPIRE_ENABLED",
                                           value="1", tier="host", path=None)})
    out = compose_host_timers(Paths(root=root))
    assert "Environment=PLANE_EXPIRE_ENABLED=1" in (
        out / "claudlobby-plane-expire.service").read_text()
    assert "PLANE_EXPIRE" not in (out / "claudlobby-plane-prune.service").read_text()


def test_an_OFF_tier_reaches_the_unit_too(tmp_path, monkeypatch):
    """The load-bearing half of an opt-out default: a host timer runs in a
    CLOSED env, so if the composer only ever stamped a "1" then writing
    PLANE_EXPIRE_ENABLED=0 in the host .env would change nothing and the sweep
    would keep firing with no way to tell why (#1383's class, inverted)."""
    import yaml  # noqa: F401 — mirrors the fixture above
    from claudlobby.composer import compose_host_timers
    from claudlobby.paths import Paths
    from claudlobby.env_tiers import Resolution
    import claudlobby.env_tiers as et

    root = tmp_path / "off"
    (root / "claudlobby").mkdir(parents=True)
    (root / "claudlobby" / "system.yaml").write_text(
        "host:\n  jobs:\n    plane-expire:\n"
        "      script: \"$CLAUDLOBBY_ROOT/lib/plane-expire.sh\"\n"
        "      schedule: \"*-*-* 05:30:00\"\n      type: oneshot\n")
    monkeypatch.setattr(et, "read_tiers", lambda paths, bot_name=None, fleet_name=None: [])
    monkeypatch.setattr(et, "cascade", lambda tiers: {
        "PLANE_EXPIRE_ENABLED": Resolution(name="PLANE_EXPIRE_ENABLED",
                                           value="0", tier="host", path=None)})
    out = compose_host_timers(Paths(root=root))
    assert "Environment=PLANE_EXPIRE_ENABLED=0" in (
        out / "claudlobby-plane-expire.service").read_text()


def _progress(root, wi, aid):
    emit_batch(root, [{"event_type": "task", "emitter": "t", "fleet": F,
                       "payload": {"work_item_id": wi, "assignment_id": aid,
                                   "event": "progress", "progress": 1}}])


def test_an_alive_assignment_is_never_expired(tmp_path):
    """Gauntlet SEV-1 fold (proven live): the default dispatch deadline is
    30 min, so 'overdue >7d' is just 'dispatched >7d ago'. A bot posting
    progress is ALIVE whatever the deadline says — no-evidence is not
    evidence-of-death. Expiring it would shadow its later `completed`
    forever (status takes the first terminal event)."""
    root = _root(tmp_path)
    stale = _dispatch(root, "a", expected_by=NOW - timedelta(days=10))
    alive = _dispatch(root, "d", expected_by=NOW - timedelta(days=10))
    _progress(root, *alive)                # progress lands NOW (inside horizon)
    conn = connect(db_path(root))
    try:
        plan = expirable(conn, now=NOW + timedelta(minutes=1), after_days=7)
    finally:
        conn.close()
    assert [r["assignment_id"] for r in plan.rows] == [stale[1]]   # alive kept


def test_a_changed_deadline_is_honored(tmp_path):
    """Gauntlet F2 fold: a deadline_changed that moved the deadline into
    the future must keep the assignment — the sweep reads the EFFECTIVE
    deadline, not the original row."""
    root = _root(tmp_path)
    wi, aid = _dispatch(root, "e", expected_by=NOW - timedelta(days=10))
    emit_batch(root, [{"event_type": "task", "emitter": "t", "fleet": F,
                       "payload": {"work_item_id": wi, "assignment_id": aid,
                                   "event": "deadline_changed",
                                   "deadline": (NOW + timedelta(days=5)).isoformat()}}])
    # backdate that event's ingest so the quiet-horizon clause is not what saves it
    db = connect(db_path(root))
    try:
        db.execute("UPDATE events SET ingested_at=? WHERE event='deadline_changed'",
                   ((NOW - timedelta(days=9)).isoformat(),))
    finally:
        db.close()
    conn = connect(db_path(root))
    try:
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    assert plan.rows == []


def test_concurrent_sweeps_collapse_to_one_expired_row(tmp_path):
    """Gauntlet F3 fold: the event_id is derived from the assignment id,
    so two sweeps that both read non-terminal and both emit produce ONE
    ledger row — idempotent concurrently, not just in sequence."""
    root = _root(tmp_path)
    _seed(root, NOW)
    conn = connect(db_path(root))
    try:
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    evs = expired_events(plan, now=NOW, after_days=7)
    emit_batch(root, evs)
    emit_batch(root, evs)                  # the racing second sweep
    conn = connect(db_path(root))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'"
                         " AND event='expired'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
