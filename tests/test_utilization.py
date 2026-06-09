"""Tests for worker utilization rollup (claudlobby/utilization.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.utilization import (
    BotUtilization,
    _compute_busy_pct,
    _find_state_transition,
    compute_bot_utilization,
    compute_fleet_utilization,
    format_utilization_summary,
    write_utilization_json,
)


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


def _make_bot_dir(tmp_path: Path, bot_name: str, log_lines: list[str]) -> Path:
    """Create a bot dir with a keepalive.log file."""
    bot_dir = tmp_path / bot_name
    bot_dir.mkdir(parents=True)
    (bot_dir / "bot.conf").write_text(f"BOT_ID={bot_name}\n")
    if log_lines:
        (bot_dir / "keepalive.log").write_text("\n".join(log_lines) + "\n")
    return bot_dir


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
    def test_idle_bot(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        log_lines = [
            f"{(now - timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%S+00:00')} IDLE — at prompt"
            for m in range(10, 0, -1)
        ]
        bot_dir = _make_bot_dir(tmp_path, "eng-1", log_lines)
        util = compute_bot_utilization("eng-1", bot_dir, {"status": "idle"}, now=now)

        assert util.busy_pct_today == 0.0
        assert util.idle_since is not None
        assert util.current_task_age_secs is None
        assert util.stall is False

    def test_busy_bot(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        log_lines = [
            f"{(now - timedelta(minutes=m)).strftime('%Y-%m-%dT%H:%M:%S+00:00')} BUSY — active"
            for m in range(10, 0, -1)
        ]
        bot_dir = _make_bot_dir(tmp_path, "eng-1", log_lines)
        util = compute_bot_utilization("eng-1", bot_dir, {"status": "working"}, now=now)

        assert util.busy_pct_today == 100.0
        assert util.idle_since is None
        assert util.current_task_age_secs is not None
        assert util.current_task_age_secs > 0

    def test_stall_detection(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        # Bot has been busy for 3 hours (> 2h stall threshold)
        log_lines = [
            f"{(now - timedelta(hours=3, minutes=-m)).strftime('%Y-%m-%dT%H:%M:%S+00:00')} BUSY — active"
            for m in range(0, 180)  # every minute for 3h
        ]
        bot_dir = _make_bot_dir(tmp_path, "eng-1", log_lines)
        util = compute_bot_utilization("eng-1", bot_dir, {"status": "working"}, now=now)

        assert util.stall is True
        assert util.current_task_age_secs > 7200

    def test_no_keepalive_log(self, tmp_path):
        bot_dir = _make_bot_dir(tmp_path, "eng-1", [])
        util = compute_bot_utilization(
            "eng-1", bot_dir, {}, now=datetime.now(timezone.utc)
        )

        assert util.busy_pct_today == 0.0
        assert util.idle_since is None
        assert util.current_task_age_secs is None

    def test_fleet_state_fields(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        bot_dir = _make_bot_dir(tmp_path, "eng-1", [])
        fleet_state = {
            "status": "working",
            "current_task": "Fix auth bug",
        }
        util = compute_bot_utilization("eng-1", bot_dir, fleet_state, now=now)

        assert util.state == "working"
        assert util.current_task == "Fix auth bug"


# ── compute_fleet_utilization ────────────────────────────────────────────────


class TestComputeFleetUtilization:
    def test_discovers_bots_from_dirs(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1", [])
        _make_bot_dir(bots_dir, "eng-2", [])

        # Minimal paths
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()
        from claudlobby.paths import Paths

        paths = Paths(root=tmp_path)

        results = compute_fleet_utilization(bots_dir, paths, now=now)
        assert len(results) == 2
        assert {r.name for r in results} == {"eng-1", "eng-2"}

    def test_explicit_bot_names(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        _make_bot_dir(bots_dir, "eng-1", [])
        _make_bot_dir(bots_dir, "eng-2", [])

        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()
        from claudlobby.paths import Paths

        paths = Paths(root=tmp_path)

        results = compute_fleet_utilization(
            bots_dir, paths, bot_names=["eng-1"], now=now
        )
        assert len(results) == 1
        assert results[0].name == "eng-1"


# ── write_utilization_json ───────────────────────────────────────────────────


class TestWriteUtilizationJson:
    def test_writes_valid_json(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()
        from claudlobby.paths import Paths

        paths = Paths(root=tmp_path)

        results = [
            BotUtilization(
                name="eng-1",
                busy_pct_today=42.3,
                busy_pct_7d=38.1,
                idle_since=datetime(2026, 6, 9, 14, 30, 0, tzinfo=timezone.utc),
                state="idle",
            ),
        ]
        out_path = write_utilization_json(results, paths, now=now)

        assert out_path.exists()
        data = json.loads(out_path.read_text())
        assert "updated" in data
        assert "eng-1" in data["bots"]
        assert data["bots"]["eng-1"]["busy_pct_today"] == 42.3
        assert data["bots"]["eng-1"]["state"] == "idle"

    def test_creates_state_dir(self, tmp_path):
        now = datetime(2026, 6, 9, 18, 0, 0, tzinfo=timezone.utc)
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()
        from claudlobby.paths import Paths

        paths = Paths(root=tmp_path)

        out_path = write_utilization_json([], paths, now=now)
        assert out_path.parent.is_dir()
        assert out_path.parent.name == "state"


# ── format_utilization_summary ───────────────────────────────────────────────


class TestFormatUtilizationSummary:
    def test_mixed_states(self):
        results = [
            BotUtilization(
                name="eng-1",
                busy_pct_today=75.0,
                state="working",
                current_task_age_secs=3600,
            ),
            BotUtilization(
                name="eng-2",
                busy_pct_today=10.0,
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
            BotUtilization(name="eng-1", busy_pct_today=50.0, state="unknown"),
        ]
        summary = format_utilization_summary(results)
        assert "50% busy" in summary
