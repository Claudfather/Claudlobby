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
from claudlobby.plane.queries import ATTENTION_SQL, TASK_STATUS_SQL
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


def _seed(root):
    stale = _dispatch(root, "a", expected_by=NOW - timedelta(days=10))
    fresh = _dispatch(root, "b", expected_by=NOW - timedelta(days=2))
    done = _dispatch(root, "c", expected_by=NOW - timedelta(days=30))
    _complete(root, *done)
    return stale, fresh, done


def test_only_stale_non_terminal_assignments_are_expirable(tmp_path):
    root = _root(tmp_path)
    stale, fresh, done = _seed(root)
    conn = connect(db_path(root))
    try:
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    assert [r["assignment_id"] for r in plan.rows] == [stale[1]]
    assert plan.rows[0]["fleet"] == F
    assert plan.unattributed == []


def test_expired_event_clears_attention_and_sets_status_idempotently(tmp_path):
    root = _root(tmp_path)
    stale, fresh, done = _seed(root)
    conn = connect(db_path(root))
    try:
        before = [r[0] for r in conn.execute(ATTENTION_SQL,
                                             (NOW.isoformat(),))]
        assert stale[1] in before and fresh[1] in before   # both overdue
        plan = expirable(conn, now=NOW, after_days=7)
    finally:
        conn.close()
    emit_batch(root, expired_events(plan, now=NOW, after_days=7))
    conn = connect(db_path(root))
    try:
        after = [r[0] for r in conn.execute(ATTENTION_SQL, (NOW.isoformat(),))]
        assert stale[1] not in after                # the card is gone
        assert fresh[1] in after                    # the fresh one stays
        assert dict(conn.execute(TASK_STATUS_SQL))[stale[1]] == "expired"
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
    _seed(root)
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
    if armed:
        env["PLANE_EXPIRE_ENABLED"] = "1"
    return subprocess.run(["bash", str(REPO / "lib" / "plane-expire.sh"), *argv],
                          capture_output=True, text=True, timeout=120, env=env)


def test_launcher_self_gates(tmp_path):
    root = _root(tmp_path)
    _seed(root)
    d = _launcher(root, "--dry-run", armed=False)
    assert d.returncode == 0 and "dormant" in d.stderr
    a = _launcher(root, "--dry-run", armed=True)
    assert a.returncode == 0 and "would expire 1" in a.stdout


def test_job_composes_dormant_and_arms_on_its_own_flag(tmp_path, monkeypatch):
    import yaml
    from claudlobby.composer import compose_host_timers
    from claudlobby.paths import Paths
    from claudlobby.env_tiers import Resolution
    import claudlobby.env_tiers as et

    job = yaml.safe_load((REPO / "claudlobby" / "system.yaml").read_text())[
        "host"]["jobs"]["plane-expire"]
    assert job["enroll"] is False and "plane-expire.sh" in job["script"]
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
    _seed(root)
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
