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
    WINDOWS,
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
        # #891: nothing observed is unknown, not 0% up.
        m = compute_metrics([], timedelta(hours=24))
        assert m["uptime_pct"] is None
        assert m["observed_pct"] == 0.0
        assert m["observed_seconds"] == 0
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
        # #891: each restart follows an IDLE tick a minute earlier (seen from up),
        # and MTBR is the gap between them, not up-seconds per restart.
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 10, 29, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 10, 30, tzinfo=TZ), "RESTART"),
            (datetime(2026, 5, 17, 10, 31, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 10, 59, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 0, tzinfo=TZ), "RESTART"),
            (datetime(2026, 5, 17, 11, 1, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["restart_count"] == 2
        assert m["mtbr_seconds"] == 1800

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
        """uptime_pct is up_secs / observed_secs; observed_pct is observed / window (#891).

        Scenario: two IDLE entries 5 min apart, now is 5 min after the last.
        Interval 1: 300s (< 600s cap) → 300s IDLE.
        Interval 2: 300s (< 600s cap) → 300s IDLE.
        up = observed = 600 → uptime 100.0%; 600/86400 → observed_pct 0.7%.
        """
        now = datetime(2026, 5, 17, 12, 0, tzinfo=TZ)
        entries = [
            (datetime(2026, 5, 17, 11, 50, tzinfo=TZ), "IDLE"),
            (datetime(2026, 5, 17, 11, 55, tzinfo=TZ), "IDLE"),
        ]
        m = compute_metrics(entries, timedelta(hours=24), now=now)
        assert m["uptime_pct"] == 100.0
        assert m["observed_pct"] == 0.7
        assert m["observed_seconds"] == 600
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


# -- #891: observed time, restart episodes, MTBR between restarts ----------

_NOW = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)


def _every(start, end, state, step=60):
    """(instant, state) every *step* seconds in [start, end): keepalive's ticks."""
    out, t = [], start
    while t < end:
        out.append((t, state))
        t += timedelta(seconds=step)
    return out


def _dead_tick(at):
    """keepalive finding the session dead: the RESTART event and the DOWN sample
    land on the same tick (the event's instant is whole seconds, the sample's is not)."""
    return [(at, "RESTART"), (at + timedelta(milliseconds=578), "DOWN")]


def _host_outage():
    """#1616's shape: up until the host goes off for 17h40m; on boot keepalive
    finds the session dead three times, 80 s apart, before it comes up."""
    off = _NOW - timedelta(hours=18)
    back = off + timedelta(hours=17, minutes=40)
    entries = _every(_NOW - timedelta(hours=24), off, "IDLE")
    for k in range(3):
        entries += _dead_tick(back + timedelta(seconds=80 * k))
    return entries + _every(back + timedelta(seconds=240), _NOW, "IDLE")


class TestObservedTime891:
    def test_partial_history_reads_the_up_share_of_observed_time(self):
        # A healthy bot whose record holds 500 one-minute ticks (8h20m): the
        # wall-clock denominator read 34.7% / 5.0% / 1.2% for a bot that was
        # never down. The share of the window observed is reported beside it.
        entries = _every(_NOW - timedelta(seconds=500 * 60), _NOW, "IDLE")
        for window, observed_pct in (("24h", 34.7), ("7d", 5.0), ("30d", 1.2)):
            m = compute_metrics(entries, WINDOWS[window], now=_NOW)
            assert (m["uptime_pct"], m.get("observed_pct"), m.get("observed_seconds")) == \
                (100.0, observed_pct, 30000), window

    def test_a_host_outage_is_not_a_bot_restart(self):
        m = compute_metrics(_host_outage(), WINDOWS["24h"], now=_NOW)
        # (restart_count, restarts_after_silence, restart_events, mtbr_seconds):
        # one episode after silence, not three bot restarts, and no MTBR from it
        assert (m["restart_count"], m.get("restarts_after_silence"), m.get("restart_events"),
                m["mtbr_seconds"]) == (0, 1, 3, None)
        # the 17h40m the host was off is unobserved, not downtime: only the three
        # DOWN samples are observed down (80 + 80 + 79.4 s); 23100 up / 23339 observed
        assert (m["uptime_pct"], m.get("down_seconds")) == (99.0, 239)

    def test_a_dead_tick_storm_seen_from_up_is_one_restart(self):
        # the control for the outage case: the same three dead ticks a minute
        # after an IDLE tick are the bot's own restart, counted once
        crash = _NOW - timedelta(hours=2)
        entries = _every(_NOW - timedelta(hours=24), crash, "IDLE")
        for k in range(3):
            entries += _dead_tick(crash + timedelta(seconds=80 * k))
        entries += _every(crash + timedelta(seconds=240), _NOW, "IDLE")
        m = compute_metrics(entries, WINDOWS["24h"], now=_NOW)
        assert m["restart_count"] == 1
        assert m["restarts_after_silence"] == 0
        assert m["restart_events"] == 3

    def test_first_boot_is_the_first_up_sample_after_a_restart(self):
        # the DOWN sample keepalive lands with the RESTART is the dead session,
        # not its boot
        crash = _NOW - timedelta(hours=2)
        entries = (_every(_NOW - timedelta(hours=24), crash, "IDLE") + _dead_tick(crash)
                   + _every(crash + timedelta(seconds=60), _NOW, "IDLE"))
        m = compute_metrics(entries, WINDOWS["24h"], now=_NOW)
        assert m["first_boot"] == (crash + timedelta(seconds=60)).isoformat()

    def test_one_restart_has_no_interval_to_average(self):
        crash = _NOW - timedelta(hours=2)
        entries = (_every(_NOW - timedelta(hours=24), crash, "IDLE") + _dead_tick(crash)
                   + _every(crash + timedelta(seconds=60), _NOW, "IDLE"))
        m = compute_metrics(entries, WINDOWS["24h"], now=_NOW)
        assert m["restart_count"] == 1
        assert m["mtbr_seconds"] is None            # not up-seconds per restart

    def test_mtbr_is_the_gap_between_restarts_and_survives_a_rotation(self):
        # Three crashes a day apart, each a minute after an IDLE tick. A rotation
        # (a plane prune, or a record that starts late) drops the older samples
        # and keeps the restart events, which retention never prunes.
        crashes = [_NOW - timedelta(hours=h) for h in (50, 26, 2)]
        full, start = [], _NOW - timedelta(hours=72)
        for c in crashes:
            full += _every(start, c - timedelta(seconds=60), "IDLE", step=300)
            full += [(c - timedelta(seconds=60), "IDLE")] + _dead_tick(c)
            start = c + timedelta(seconds=60)
        full += _every(start, _NOW, "IDLE", step=300)
        horizon = _NOW - timedelta(hours=8)
        rotated = [(t, s) for t, s in full if t >= horizon or s == "RESTART"]
        for window in ("24h", "7d"):
            before = compute_metrics(full, WINDOWS[window], now=_NOW)
            after = compute_metrics(rotated, WINDOWS[window], now=_NOW)
            # 24h: the one restart in the window measured back to the one before
            # it, outside the window; 7d: the mean of the two gaps
            assert (before["mtbr_seconds"], after["mtbr_seconds"]) == (86400, 86400), window
            assert after["uptime_pct"] > 99, window

    def test_table_labels_the_observed_share_and_says_what_it_did_not_count(self):
        results = {"b1": {"24h": compute_metrics(_host_outage(), WINDOWS["24h"], now=_NOW)}}
        table = format_table(results, window="24h")
        assert "Up (obs)" in table and "Observed" in table
        assert "99.0%" in table
        assert "b1: 1 restart not counted" in table


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
