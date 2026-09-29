"""Tests for worker utilization rollup (claudlobby/utilization.py).

F18 closure R2b: the series is the plane's heartbeat samples — `compute_bot_utilization`
takes a bot's (instant, state) entries and `compute_fleet_utilization` opens the
plane for the fleet (refusing, never zeros, when it cannot). The keepalive.log
fixtures went with the file (test_no_keepalive_log became test_no_samples).
"""

from __future__ import annotations

from tests.plane_setup import initialize_plane

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.plane.emit_api import emit_batch
from claudlobby.task_work_queries import BotWork, CurrentWork
from claudlobby.utilization import (
    BotUtilization,
    PlaneUnreachable,
    _compute_busy_pct,
    _find_state_transition,
    compute_bot_utilization,
    compute_fleet_utilization,
    format_utilization_summary,
)

REPO = Path(__file__).resolve().parent.parent


def _make_entries(
    states: list[str],
    interval_secs: int = 60,
    start: datetime | None = None,
) -> list[tuple[datetime, str]]:
    """Build keepalive entries from a list of states, spaced evenly."""
    if start is None:
        start = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    return [
        (start + timedelta(seconds=i * interval_secs), s) for i, s in enumerate(states)
    ]


def _make_bot_dir(tmp_path: Path, bot_name: str) -> Path:
    """Create a bot dir (the fleet rollup discovers bots by their bot.conf)."""
    bot_dir = tmp_path / bot_name
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.conf").write_text(f"BOT_ID={bot_name}\n")
    return bot_dir


def _series(now: datetime, states: list[str], step: timedelta = timedelta(minutes=1)) -> list[tuple[datetime, str]]:
    """Entries ending at `now`, one per `step`, in time order."""
    n = len(states)
    return [(now - step * (n - i), s) for i, s in enumerate(states)]


def _land_heartbeats(root: Path, fleet: str, bot: str, entries) -> None:
    """The bot's heartbeat samples on a plane under `root`, as keepalive lands them."""
    (root / "state" / "plane").mkdir(parents=True, exist_ok=True)
    initialize_plane(root)
    out = emit_batch(root, [{"event_type": "metric_sample", "emitter": "keepalive", "fleet": fleet,
                             "occurred_at": ts.isoformat(),
                             "payload": {"subject_kind": "bot_instance", "subject": f"bot:{fleet}/{bot}",
                                         "metric": "bot.heartbeat", "value": {"state": state}}}
                            for ts, state in entries])
    assert all(o.status == "committed" for o in out), out


# ── _compute_busy_pct ────────────────────────────────────────────────────────


class TestComputeBusyPct:
    def test_all_busy(self):
        entries = _make_entries(["BUSY"] * 10)
        now = entries[-1][0] + timedelta(seconds=60)
        pct = _compute_busy_pct(entries, timedelta(hours=24), now)
        assert pct == 100.0

    def test_all_idle(self):
        entries = _make_entries(["IDLE"] * 10)
        now = entries[-1][0] + timedelta(seconds=60)
        pct = _compute_busy_pct(entries, timedelta(hours=24), now)
        assert pct == 0.0

    def test_half_and_half(self):
        entries = _make_entries(["BUSY"] * 5 + ["IDLE"] * 5)
        now = entries[-1][0] + timedelta(seconds=60)
        pct = _compute_busy_pct(entries, timedelta(hours=24), now)
        assert 45.0 <= pct <= 55.0  # approximately 50%

    def test_empty_entries(self):
        pct = _compute_busy_pct([], timedelta(hours=24), datetime.now(timezone.utc))
        assert pct == 0.0

    def test_window_filters_old_entries(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        # Old entries (25h ago) — should be excluded from 24h window
        old = _make_entries(
            ["BUSY"] * 5,
            start=now - timedelta(hours=25),
        )
        # Recent entries (1h ago)
        recent = _make_entries(
            ["IDLE"] * 5,
            start=now - timedelta(hours=1),
        )
        pct = _compute_busy_pct(old + recent, timedelta(hours=24), now)
        assert pct == 0.0  # only recent IDLE entries count

    def test_restart_and_unknown_excluded_from_ratio(self):
        entries = _make_entries(["BUSY", "RESTART", "UNKNOWN", "IDLE", "BUSY"])
        now = entries[-1][0] + timedelta(seconds=60)
        pct = _compute_busy_pct(entries, timedelta(hours=24), now)
        # BUSY: 2 intervals, IDLE: 1 interval → ~67% busy
        assert 60.0 <= pct <= 75.0

    def test_gap_capped_at_max_interval(self):
        """Gaps > 600s should not inflate busy/idle counts."""
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        entries = [
            (now - timedelta(hours=2), "BUSY"),
            # 2-hour gap — capped at 600s
            (now - timedelta(seconds=60), "IDLE"),
        ]
        pct = _compute_busy_pct(entries, timedelta(hours=24), now)
        # BUSY gets 600s (capped), IDLE gets 60s
        assert 85.0 <= pct <= 95.0


# ── _find_state_transition ──────────────────────────────────────────────────


class TestFindStateTransition:
    def test_idle_tail(self):
        entries = _make_entries(["BUSY", "BUSY", "IDLE", "IDLE", "IDLE"])
        ts = _find_state_transition(entries, "IDLE")
        assert ts == entries[2][0]

    def test_busy_tail(self):
        entries = _make_entries(["IDLE", "IDLE", "BUSY", "BUSY"])
        ts = _find_state_transition(entries, "BUSY")
        assert ts == entries[2][0]

    def test_no_match(self):
        entries = _make_entries(["BUSY", "BUSY"])
        ts = _find_state_transition(entries, "IDLE")
        assert ts is None

    def test_empty(self):
        assert _find_state_transition([], "IDLE") is None

    def test_single_entry(self):
        entries = _make_entries(["IDLE"])
        ts = _find_state_transition(entries, "IDLE")
        assert ts == entries[0][0]

    def test_all_same_state(self):
        entries = _make_entries(["IDLE"] * 10)
        ts = _find_state_transition(entries, "IDLE")
        assert ts == entries[0][0]


# ── compute_bot_utilization ──────────────────────────────────────────────────


class TestComputeBotUtilization:
    def test_idle_bot(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        util = compute_bot_utilization("eng-1", _series(now, ["IDLE"] * 10), now=now)

        assert util.busy_pct_24h == 0.0
        assert util.idle_since is not None
        assert util.busy_age_secs is None
        assert util.stall is False

    def test_busy_bot(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        util = compute_bot_utilization("eng-1", _series(now, ["BUSY"] * 10), now=now)

        assert util.busy_pct_24h == 100.0
        assert util.idle_since is None
        assert util.busy_age_secs is not None
        assert util.busy_age_secs > 0

    def test_stall_detection(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        # Bot has been busy for 3 hours (> 2h stall threshold), a sample a minute
        util = compute_bot_utilization("eng-1", _series(now, ["BUSY"] * 180), now=now)

        assert util.stall is True
        assert util.busy_age_secs > 7200

    def test_no_samples(self):
        util = compute_bot_utilization("eng-1", [], now=datetime.now(timezone.utc))

        assert util.busy_pct_24h == 0.0
        assert util.idle_since is None
        assert util.busy_age_secs is None

    def test_naive_entries_are_read_as_utc(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        naive = [(ts.replace(tzinfo=None), s) for ts, s in _series(now, ["BUSY"] * 3)]
        assert compute_bot_utilization("eng-1", naive, now=now).busy_pct_24h == 100.0

    def test_work_is_separate_from_observed_state(self):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        work = BotWork((CurrentWork("wi_auth", "asg_auth", "Fix auth bug", "assigned"),))
        util = compute_bot_utilization("eng-1", [], work, now=now)

        assert util.state == "unknown"
        assert util.current_task == "Fix auth bug"
        assert util.work_assignments[0].assignment_id == "asg_auth"


# ── compute_fleet_utilization ────────────────────────────────────────────────


class TestComputeFleetUtilization:
    def _paths(self, tmp_path):
        (tmp_path / "library").mkdir(exist_ok=True)
        (tmp_path / "lib").exists() or (tmp_path / "lib").symlink_to(REPO / "lib")
        from tests.package_fixtures import source_package
        from claudlobby.paths import Paths
        return Paths(root=tmp_path, package=source_package())

    def test_discovers_bots_from_dirs_and_reads_their_series_from_the_plane(self, tmp_path):
        now = datetime(2026, 9, 2, 18, 0, 0, tzinfo=timezone.utc)
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1")
        _make_bot_dir(bots_dir, "eng-2")
        _land_heartbeats(tmp_path, "f", "eng-1", _series(now, ["BUSY"] * 5))
        paths = self._paths(tmp_path)

        results = compute_fleet_utilization(bots_dir, paths, now=now, fleet="f")
        assert {r.name for r in results} == {"eng-1", "eng-2"}
        by_name = {r.name: r for r in results}
        assert by_name["eng-1"].busy_pct_24h == 100.0          # the plane's samples
        assert by_name["eng-2"].busy_pct_24h == 0.0            # no sample recorded: nothing to roll up
        assert by_name["eng-1"].current_task is None
        assert by_name["eng-1"].state == "working"
        assert "no actor identity" in by_name["eng-1"].work_unavailable
        assert by_name["eng-2"].state == "unknown"

        from claudlobby.plane.db import connect, db_file
        from claudlobby.plane.identity import resolve_party
        from tests.test_task_state import _insert

        (tmp_path / "state" / "fleet-state.json").write_text(
            '{"bots":{"eng-1":{"current_task":"stale file task","status":"blocked"}}}')
        conn = connect(db_file(tmp_path))
        fleet_uid = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet'"
                                 " AND alias='f'").fetchone()[0]
        worker_uid = resolve_party(conn, "bot:f/eng-1", now=now.isoformat(), fleet_uid=fleet_uid)
        _insert(conn, "work_items", fleet_uid=fleet_uid, work_item_id="wi_rollup",
                title="Canonical rollup work", created_by_uid=worker_uid)
        _insert(conn, "assignments", fleet_uid=fleet_uid, assignment_id="asg_rollup",
                work_item_id="wi_rollup", assignee_uid=worker_uid, assigned_by_uid=worker_uid)
        assigned = compute_fleet_utilization(bots_dir, paths, now=now, fleet="f")
        worker = next(row for row in assigned if row.name == "eng-1")
        assert worker.current_task == "Canonical rollup work" and worker.state == "working"
        assert not worker.work_unavailable
        assert worker.work_assignments[0].task_id == "wi_rollup"
        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_rollup",
                assignment_id="asg_rollup", event="returned_blocked")
        returned = compute_fleet_utilization(bots_dir, paths, now=now, fleet="f")
        assert next(row for row in returned if row.name == "eng-1").current_task is None
        _insert(conn, "events", fleet_uid=fleet_uid, kind="task", work_item_id="wi_rollup",
                assignment_id="asg_missing", event="progress")
        conn.close()
        unresolved = compute_fleet_utilization(bots_dir, paths, now=now, fleet="f")
        assert any(issue["code"] == "dangling_task_event" for issue in unresolved[0].work_issues)
        assert unresolved[0].work_unresolved
        assert unresolved[0].work_unavailable

    def test_explicit_bot_names(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1")
        _make_bot_dir(bots_dir, "eng-2")
        _land_heartbeats(tmp_path, "f", "eng-2", _series(now, ["IDLE"] * 2))
        paths = self._paths(tmp_path)

        results = compute_fleet_utilization(bots_dir, paths, bot_names=["eng-1"], now=now, fleet="f")
        assert len(results) == 1
        assert results[0].name == "eng-1"

    def test_a_case_variant_alias_joins_the_bots_series(self, tmp_path):
        """A `bot:f/ENG-1` identity is the same bot as `eng-1` (the roster rule
        every per-bot plane read follows)."""
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1")
        _land_heartbeats(tmp_path, "f", "ENG-1", _series(now, ["BUSY"] * 3))
        results = compute_fleet_utilization(bots_dir, self._paths(tmp_path), now=now, fleet="f")
        assert results[0].busy_pct_24h == 100.0

    def test_an_unreachable_plane_refuses_rather_than_rolling_up_zeros(self, tmp_path):
        import pytest

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1")
        with pytest.raises(PlaneUnreachable, match="plane"):
            compute_fleet_utilization(bots_dir, self._paths(tmp_path), fleet="f")

    def test_no_fleet_named_is_refused(self, tmp_path):
        import pytest

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        with pytest.raises(PlaneUnreachable, match="fleet"):
            compute_fleet_utilization(bots_dir, self._paths(tmp_path))

    def test_public_read_reports_missing_bot_history_as_unknown(self, tmp_path, monkeypatch, capsys):
        from types import SimpleNamespace
        from claudlobby import activation_state
        from claudlobby.__main__ import main
        from claudlobby.commands import orientation

        now = datetime.now(timezone.utc)
        # An unrelated heartbeat establishes the fleet in a reachable Plane;
        # the declared worker still has no observed BUSY/IDLE duration.
        _land_heartbeats(tmp_path, "f", "other", [(now - timedelta(minutes=1), "IDLE")])
        paths = self._paths(tmp_path)
        monkeypatch.setattr(orientation, "_context", lambda args: SimpleNamespace(
            paths=paths, fleet=SimpleNamespace(name="f", bots={"worker": object()})))
        monkeypatch.setattr(activation_state, "read_selection", lambda root: None)

        assert main(["--json", "fleet", "utilization"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["command"] == "fleet.utilization"
        worker = result["data"]["items"][0]
        assert worker["name"] == "worker" and worker["state"] == "unknown"
        assert worker["observed_secs_24h"] == 0
        assert worker["busy_pct_24h"] is None and worker["busy_pct_7d"] is None
        assert "worker unknown" in result["data"]["summary"]


# ── format_utilization_summary ───────────────────────────────────────────────


class TestFormatUtilizationSummary:
    def test_mixed_states(self):
        results = [
            BotUtilization(
                name="eng-1",
                busy_pct_24h=75.0,
                state="working",
                busy_age_secs=3600,
            ),
            BotUtilization(
                name="eng-2",
                busy_pct_24h=10.0,
                state="idle",
                idle_since=datetime(2026, 6, 9, 16, 0, 0, tzinfo=timezone.utc),
            ),
        ]
        summary = format_utilization_summary(results)
        assert "team utilization:" in summary
        assert "eng-1 heads-down 1h" in summary
        assert "eng-2 idle" in summary

    def test_empty(self):
        assert format_utilization_summary([]) == "no bots"

    def test_busy_only(self):
        results = [
            BotUtilization(name="eng-1", busy_pct_24h=50.0, observed_secs_24h=600, state="unknown"),
        ]
        summary = format_utilization_summary(results)
        assert "50% busy" in summary
