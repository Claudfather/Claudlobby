"""Tests for claudlobby.uptime — uptime metrics from the plane.

F18 closure R2b: the keepalive.log parser is gone, so TestParseKeepaliveLog and
TestCollectBotLogs went with it; `aggregate_fleet` takes the plane's entries
through its required `entries_for` seam, and canonical `fleet uptime` refuses when
the plane cannot answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.uptime import (
    compute_metrics,
    aggregate_fleet,
    format_table,
    format_json,
    _fmt_duration,
)


# -- Helpers ---------------------------------------------------------------

TZ = timezone(timedelta(hours=-4))


def _ts(hour: int, minute: int = 0) -> str:
    """Build an ISO timestamp string for 2026-05-17 at given hour:min -04:00."""
    return datetime(2026, 5, 17, hour, minute, tzinfo=TZ).isoformat()


class TestComputeMetrics:
    def test_empty_entries(self):
        m = compute_metrics([], timedelta(hours=24))
        assert m["uptime_pct"] == 0.0
        assert m["restart_count"] == 0
        assert m["entries_in_window"] == 0

    def test_all_idle(self):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 11, 0, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 5, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 10, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["restart_count"] == 0
        assert m["mtbr_seconds"] is None
        assert m["idle_seconds"] > 0
        assert m["busy_seconds"] == 0

    def test_held_counts_as_up_time(self):
        # #2070: HELD is a live session whose input box holds text that was
        # never submitted. Those ticks read UNKNOWN until keepalive could name
        # them, and counted as up time; a held bot must not start reading down.
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        stamps = [datetime(2026, 5, 17, 11, m, tzinfo=TZ) for m in (0, 5, 10)]
        held = compute_metrics([(t, "HELD") for t in stamps], timedelta(hours=24), now=now)
        unknown = compute_metrics([(t, "UNKNOWN") for t in stamps], timedelta(hours=24), now=now)
        assert held["uptime_pct"] > 0
        assert held["uptime_pct"] == unknown["uptime_pct"]
        assert held["busy_seconds"] == 0

    def test_restart_counted(self):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 10, 0, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 10, 30, tzinfo=TZ), "RESTART"),
            (datetime(2026, 5, 17, 10, 31, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 0, tzinfo=TZ), "RESTART"),
            (datetime(2026, 5, 17, 11, 1, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["restart_count"] == 2
        assert m["mtbr_seconds"] == 900

    def test_first_boot_after_restart(self):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        restart_ts = datetime(2026, 5, 17, 11, 0, tzinfo=TZ)
        boot_ts = datetime(2026, 5, 17, 11, 1, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 10, 0, tzinfo=TZ), "IDLE"),
            (restart_ts, "RESTART"),
            (boot_ts, "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["first_boot"] == boot_ts.isoformat()

    def test_first_boot_no_restart(self):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        first_ts = datetime(2026, 5, 17, 10, 0, tzinfo=TZ)
        entries = [
            (first_ts, "IDLE"),
            (datetime(2026, 5, 17, 10, 5, tzinfo=TZ), "BUSY"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["first_boot"] == first_ts.isoformat()

    def test_uptime_pct_numerical(self):
        """uptime_pct must equal (up_secs / window_secs) * 100, capped at 100.

        Scenario: two IDLE entries 5 min apart, now is 5 min after the last.
        Interval 1: 300s (< 600s cap) → 300s IDLE.
        Interval 2: 300s (< 600s cap) → 300s IDLE.
        up_secs = 600, window = 86400 → pct = round(600/86400*100, 1) = 0.7%.
        """
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 11, 50, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 55, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["uptime_pct"] == 0.7
        assert m["idle_seconds"] == 600

    def test_window_filtering(self):
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 10, 10, 0, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 0, tzinfo=TZ), "BUSY"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["entries_in_window"] == 1

    def test_duration_capped_at_max_interval(self):
        """Gaps > 10 min should not be fully credited to any state."""
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 8, 0, tzinfo=TZ), "BUSY"),
            (datetime(2026, 5, 17, 11, 0, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["busy_seconds"] == 600  # capped


# -- aggregate_fleet -------------------------------------------------------


class TestAggregateFleet:
    def test_finds_bots_with_conf(self, tmp_path: Path):
        bot1 = tmp_path / "alpha"
        bot1.mkdir()
        (bot1 / "bot.conf").write_text("BOT_NAME=alpha\n")
        # Relative timestamp so the entry always lands inside the now-relative
        # 24h window, regardless of the calendar date the suite runs on.
        recent = (datetime.now(TZ) - timedelta(hours=1)).replace(microsecond=0)
        entries = {"alpha": [(recent, "IDLE")]}

        bot2 = tmp_path / "beta"
        bot2.mkdir()
        (bot2 / "bot.conf").write_text("BOT_NAME=beta\n")

        results = aggregate_fleet(tmp_path, windows=["24h"],
                                  entries_for=lambda d: entries.get(d.name, []))
        assert "alpha" in results
        assert "beta" in results
        assert results["alpha"]["24h"]["entries_in_window"] == 1
        assert results["beta"]["24h"]["entries_in_window"] == 0

    def test_bot_filter(self, tmp_path: Path):
        for name in ("alpha", "beta"):
            d = tmp_path / name
            d.mkdir()
            (d / "bot.conf").write_text(f"BOT_NAME={name}\n")

        results = aggregate_fleet(tmp_path, windows=["24h"], bot_filter="alpha",
                                  entries_for=lambda d: [])
        assert "alpha" in results
        assert "beta" not in results


# -- format_table ----------------------------------------------------------


class TestFormatTable:
    def test_includes_header_and_bots(self):
        results = {
            "mybot": {
                "24h": {
                    "uptime_pct": 95.2,
                    "restart_count": 1,
                    "mtbr_seconds": 3600,
                    "busy_seconds": 1800,
                    "idle_seconds": 7200,
                    "unknown_seconds": 0,
                    "first_boot": "2026-05-17T10:00:00-04:00",
                    "entries_in_window": 100,
                },
            },
        }
        table = format_table(results, window="24h")
        assert "mybot" in table
        assert "95.2%" in table
        assert "1h00m" in table

    def test_empty_bot_shows_dash(self):
        results = {
            "empty": {
                "24h": {
                    "uptime_pct": 0.0,
                    "restart_count": 0,
                    "mtbr_seconds": None,
                    "busy_seconds": 0,
                    "idle_seconds": 0,
                    "unknown_seconds": 0,
                    "first_boot": None,
                    "entries_in_window": 0,
                },
            },
        }
        table = format_table(results, window="24h")
        assert "empty" in table
        assert "\u2014" in table


# -- format_json -----------------------------------------------------------


class TestFormatJson:
    def test_valid_json_output(self):
        import json

        results = {"bot1": {"24h": {"uptime_pct": 99.0}}}
        output = format_json(results)
        parsed = json.loads(output)
        assert parsed["bot1"]["24h"]["uptime_pct"] == 99.0


# -- _fmt_duration ---------------------------------------------------------


class TestFmtDuration:
    def test_seconds(self):
        assert _fmt_duration(45) == "45s"

    def test_minutes(self):
        assert _fmt_duration(300) == "5m"

    def test_hours_minutes(self):
        assert _fmt_duration(3720) == "1h02m"

    def test_days_hours(self):
        assert _fmt_duration(90000) == "1d1h"


class TestAggregateListing:
    """The aggregate consumes one materialized bot list without re-enumerating."""

    def test_aggregate_consumes_the_list_and_never_reenumerates(self, tmp_path):
        from claudlobby.uptime import aggregate_fleet

        bots = tmp_path / "bots"
        real_bot = bots / "realbot"
        (real_bot / "logs").mkdir(parents=True)
        (real_bot / "bot.conf").write_text('BOT_ID="realbot"\n')

        # an EMPTY materialized listing must yield no rows even though a
        # real bot sits in bots_dir — proof nothing re-globs the dir (the
        # mutation that ignores bot_dirs finds realbot here and goes red)
        assert aggregate_fleet(bots, windows=["24h"], bot_dirs=[], entries_for=lambda d: []) == {}

        # ...and the listing IS what gets consumed
        got = aggregate_fleet(bots, windows=["24h"], bot_dirs=[real_bot], entries_for=lambda d: [])
        assert "realbot" in got
