"""Tests for claudlobby.uptime — keepalive log aggregation metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudlobby.uptime import (
    parse_keepalive_log,
    collect_bot_logs,
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


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


# -- parse_keepalive_log ---------------------------------------------------


class TestParseKeepaliveLog:
    def test_parses_all_states(self, tmp_path: Path):
        log = tmp_path / "keepalive.log"
        _write_log(
            log,
            [
                f"{_ts(10, 0)} BUSY \u2014 active processing",
                f"{_ts(10, 5)} IDLE \u2014 at prompt",
                f"{_ts(10, 10)} RESTART \u2014 session dead, systemctl --user restart bot",
                f"{_ts(10, 15)} UNKNOWN \u2014 pane state did not match (1 consecutive)",
                f"{_ts(10, 20)} SKIP \u2014 session reappeared",
            ],
        )
        entries = parse_keepalive_log(log)
        assert len(entries) == 5
        assert [s for _, s in entries] == ["BUSY", "IDLE", "RESTART", "UNKNOWN", "SKIP"]

    def test_skips_non_matching_lines(self, tmp_path: Path):
        log = tmp_path / "keepalive.log"
        _write_log(
            log,
            [
                f"{_ts(10)} BUSY \u2014 active processing",
                "some random noise",
                "",
                f"{_ts(10, 5)} IDLE \u2014 at prompt",
            ],
        )
        entries = parse_keepalive_log(log)
        assert len(entries) == 2

    def test_missing_file(self, tmp_path: Path):
        assert parse_keepalive_log(tmp_path / "nope.log") == []


# -- collect_bot_logs ------------------------------------------------------


class TestCollectBotLogs:
    def test_combines_rotated_and_current(self, tmp_path: Path):
        _write_log(
            tmp_path / "keepalive.log.1",
            [
                f"{_ts(8)} IDLE \u2014 at prompt",
            ],
        )
        _write_log(
            tmp_path / "keepalive.log",
            [
                f"{_ts(12)} BUSY \u2014 active processing",
            ],
        )
        entries = collect_bot_logs(tmp_path)
        assert len(entries) == 2
        assert entries[0][0] < entries[1][0]
        assert entries[0][1] == "IDLE"
        assert entries[1][1] == "BUSY"


# -- compute_metrics -------------------------------------------------------


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
        _write_log(
            bot1 / "keepalive.log",
            [
                f"{_ts(11)} IDLE \u2014 at prompt",
            ],
        )

        bot2 = tmp_path / "beta"
        bot2.mkdir()
        (bot2 / "bot.conf").write_text("BOT_NAME=beta\n")

        results = aggregate_fleet(tmp_path, windows=["24h"])
        assert "alpha" in results
        assert "beta" in results
        assert results["alpha"]["24h"]["entries_in_window"] == 1
        assert results["beta"]["24h"]["entries_in_window"] == 0

    def test_bot_filter(self, tmp_path: Path):
        for name in ("alpha", "beta"):
            d = tmp_path / name
            d.mkdir()
            (d / "bot.conf").write_text(f"BOT_NAME={name}\n")

        results = aggregate_fleet(tmp_path, windows=["24h"], bot_filter="alpha")
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
