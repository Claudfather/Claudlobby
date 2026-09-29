"""Attention expiry sweep — emits `expired` for assignments overdue past the
horizon, through normal ingest. Laws pinned: only stale NON-terminal
assignments (a fresh one and a completed one are untouched); the event
removes the card from ATTENTION_SQL and TASK_STATUS_SQL says `expired`;
a second run is a no-op (idempotent by construction); dry-run emits
nothing; the launcher self-gates; the composer stamps the arming flag.
Public expiry requires an active selected release and filters to its fleets.
"""

from __future__ import annotations

from tests.plane_setup import initialize_plane

import subprocess
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.expiry import ExpiryChanged, expirable, expired_events, require_expirable
from claudlobby.plane.queries import ATTENTION_SQL, TASK_STATUS_SQL, attention_params
from tests.conftest import constructed_env
from tests.plane_fixtures import plane_root

REPO = Path(__file__).resolve().parent.parent
# The REAL clock, not a literal instant, and that is load-bearing rather than
# lazy. Three of these tests run lib/plane-expire.sh as a SUBPROCESS, which
# reads the wall clock; every other test seeds its rows at NOW +/- N days. Pin
# NOW to a fixed past instant and the two clocks drift apart at one day per day
# until the `fresh` row (NOW - 2d) crosses the 7-day horizon by the real clock
# and the launcher expires 2 where the fixture says 1 — which is exactly what
# happened, on schedule, five days after the literal was written. Every
# assertion here is about a RELATIONSHIP between seeded rows and the horizon, so
# a live NOW keeps all of them intact and keeps the subprocess in agreement.
NOW = datetime.now(timezone.utc)
F = "example-fleet"


def _root(tmp_path):
    return plane_root(tmp_path)


def _dispatch(root, n, *, expected_by, fleet=F):
    """work_item + assignment — the 6b fixture shape."""
    wi, aid = f"wi_{n:0>32}", f"asg_{n:0>32}"   # ID_PATTERNS: asg_ + 32 hex
    initialize_plane(root)
    emit_batch(root, [
        {"event_type": "work_item", "emitter": "t", "fleet": fleet,
         "payload": {"work_item_id": wi, "title": "t",
                     "created_by": f"bot:{fleet}/mgr"}},
        {"event_type": "assignment", "emitter": "t", "fleet": fleet,
         "payload": {"assignment_id": aid, "work_item_id": wi,
                     "assignee": f"bot:{fleet}/w1", "assigned_by": f"bot:{fleet}/mgr",
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


def _selected_sweep(monkeypatch, root, *, dry_run=False):
    from claudlobby.commands import plane_expire

    @contextmanager
    def admitted(*_args, **_kwargs):
        yield SimpleNamespace(release_id="selected-release", seal_sha256="selected-seal")

    monkeypatch.setattr(plane_expire, "mutation_admission", admitted)
    monkeypatch.setattr(plane_expire, "resolve_paths", lambda **_kwargs:
                        SimpleNamespace(root=root))
    monkeypatch.setattr(plane_expire.RuntimeIdentity, "current", classmethod(
        lambda cls: SimpleNamespace()))
    monkeypatch.setattr("claudlobby.activation_state.read_selection",
                        lambda _root: {"plan_id": "selected-plan"})
    monkeypatch.setattr("claudlobby.config_plan.read_plan", lambda *_args:
                        SimpleNamespace(release_id="selected-release", release_seal="selected-seal",
                                        fleets=(F,)))
    return plane_expire.dispatch(SimpleNamespace(root=root, fleet=None, seed=False,
                                                  after_days=None, dry_run=dry_run))


def test_selected_dry_run_then_live(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _seed(root)
    _dispatch(root, "d", expected_by=NOW - timedelta(days=10), fleet="retired-fleet")
    dry = _selected_sweep(monkeypatch, root, dry_run=True)
    assert dry.data["candidates"] == 1 and dry.data["skipped_unselected"] == 1
    assert "expired" not in dry.data
    assert "would expire 1" in dry.lines[0]
    conn = connect(db_path(root))
    try:
        assert not list(conn.execute(
            "SELECT 1 FROM events WHERE kind='task' AND event='expired'"))
    finally:
        conn.close()
    live = _selected_sweep(monkeypatch, root)
    assert live.data["recording"] == "committed" and "expired 1" in live.lines[0]
    conn = connect(db_path(root))
    try:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='task'"
                         " AND event='expired'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert _selected_sweep(monkeypatch, root).data["expired"] == 0


def test_completion_between_preview_and_commit_refuses_expiry(tmp_path):
    import pytest

    root = _root(tmp_path)
    stale = _dispatch(root, "e", expected_by=NOW - timedelta(days=10))
    with closing(connect(db_path(root))) as conn:
        plan = expirable(conn, now=NOW, after_days=7)
    with pytest.raises(ValueError, match="precondition requires require_commit"):
        emit_batch(root, expired_events(plan, now=NOW, after_days=7),
                   precondition=lambda conn: require_expirable(
                       conn, plan, now=NOW, after_days=7))
    _complete(root, *stale)
    with pytest.raises(ExpiryChanged):
        emit_batch(root, expired_events(plan, now=NOW, after_days=7),
                   require_commit=True,
                   precondition=lambda conn: require_expirable(
                       conn, plan, now=NOW, after_days=7))
    with closing(connect(db_path(root))) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event='expired'").fetchone()[0] == 0


def test_unproved_expiry_commit_never_claims_expired(tmp_path, monkeypatch):
    import pytest
    from claudlobby.command_result import CommandFailure

    root = _root(tmp_path)
    _dispatch(root, "f", expected_by=NOW - timedelta(days=10))
    def uncertain(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O")
    monkeypatch.setattr("claudlobby.commands.plane_expire.emit_batch", uncertain)
    with pytest.raises(CommandFailure) as error:
        _selected_sweep(monkeypatch, root)
    assert error.value.error.code == "commit_unknown"
    assert error.value.data["recording"] == "unknown"
    assert "expired" not in error.value.data
    assert len(error.value.data["event_ids"]) == 1


def _launcher(root, *argv, cli, armed):
    env = constructed_env(CLAUDLOBBY_ROOT=root, CLAUDLOBBY_CLI=cli)
    # Since the defaults flip the flag is an opt-OUT: absence RUNS the sweep,
    # and only an exact 0 stops it. The harness spells both explicitly rather
    # than relying on absence, so the pin reads the same way the door does.
    env["PLANE_EXPIRE_ENABLED"] = "1" if armed else "0"
    return subprocess.run(["bash", str(REPO / "lib" / "plane-expire.sh"), *argv],
                          capture_output=True, text=True, timeout=120, env=env)


def test_launcher_runs_by_default_and_the_off_switch_is_LOUD(tmp_path):
    """The ON arm reaches its CLI; the OFF arm reports the disabled sweep."""
    root = _root(tmp_path)
    _seed(root)
    on = _launcher(root, "--dry-run", cli=Path("/bin/false"), armed=True)
    assert on.returncode != 0  # switch let the selected CLI run
    off = _launcher(root, "--dry-run", cli=Path("/bin/false"), armed=False)
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
    """Absence is ON — the launcher still reaches its CLI."""
    root = _root(tmp_path)
    _seed(root)
    env = constructed_env(CLAUDLOBBY_ROOT=root, CLAUDLOBBY_CLI=Path("/bin/false"))
    r = subprocess.run(["bash", str(REPO / "lib" / "plane-expire.sh"), "--dry-run"],
                       capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode != 0  # absence does not disarm the launcher


def test_job_composes_and_carries_its_own_flag(tmp_path, monkeypatch):
    import yaml
    from claudlobby.composer import compose_host_timers
    from tests.package_fixtures import source_package
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
    out = compose_host_timers(Paths(root=root, package=source_package()))
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
    from tests.package_fixtures import source_package
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
    out = compose_host_timers(Paths(root=root, package=source_package()))
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
