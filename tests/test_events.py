"""Tests for the claudlobby events subcommand."""

import json
import os
from datetime import datetime, timezone

import pytest

from claudlobby.commands.events import (
    CRITICAL_TYPES,
    collect_events,
    format_event_table,
)


@pytest.fixture
def events_dir(tmp_path):
    """Create a mock bots directory with event files."""
    bot_dir = tmp_path / "alpha"
    events = bot_dir / "data" / "events"
    events.mkdir(parents=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events_file = events / f"fleet-{today}.jsonl"

    lines = [
        json.dumps(
            {
                "ts": "2026-06-10T10:00:00-04:00",
                "bot": "alpha",
                "type": "session_missing",
                "source": "pulse",
                "data": {"session": "alpha"},
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-10T10:05:00-04:00",
                "bot": "alpha",
                "type": "service_down",
                "source": "pulse",
                "data": {"unit": "com.test.eng.alpha", "state": "failed"},
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-10T10:10:00-04:00",
                "bot": "alpha",
                "type": "tool_call",
                "source": "vitals",
                "data": {"tool": "Read", "event": "PreToolUse"},
            }
        ),
        json.dumps(
            {
                "ts": "2026-06-10T10:15:00-04:00",
                "bot": "alpha",
                "type": "keepalive",
                "source": "keepalive",
                "data": {"state": "IDLE"},
            }
        ),
    ]
    events_file.write_text("\n".join(lines) + "\n")

    return tmp_path


class TestCollectEvents:
    def test_collects_all_events(self, events_dir):
        events = collect_events(events_dir)
        assert len(events) == 4

    def test_filter_by_type(self, events_dir):
        events = collect_events(events_dir, event_type="service_down")
        assert len(events) == 1
        assert events[0]["type"] == "service_down"

    def test_filter_by_bot(self, events_dir):
        events = collect_events(events_dir, bot="alpha")
        assert len(events) == 4

    def test_filter_by_bot_no_match(self, events_dir):
        events = collect_events(events_dir, bot="nonexistent")
        assert len(events) == 0

    def test_filter_by_source(self, events_dir):
        events = collect_events(events_dir, source="pulse")
        assert len(events) == 2

    def test_filter_critical_only(self, events_dir):
        events = collect_events(events_dir, critical_only=True)
        types = {e["type"] for e in events}
        assert "tool_call" not in types
        assert "keepalive" not in types
        assert "session_missing" in types
        assert "service_down" in types

    def test_filter_critical_only_includes_bridge_and_lifecycle_failures(
        self, tmp_path
    ):
        """bridge_down/reload_failed/restart_failed are emit_failure_alert events —
        operator-actionable, so they must surface under --critical like service_down."""
        bot_dir = tmp_path / "alpha"
        events = bot_dir / "data" / "events"
        events.mkdir(parents=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events_file = events / f"fleet-{today}.jsonl"
        lines = [
            json.dumps(
                {
                    "ts": "2026-07-06T10:00:00-04:00",
                    "bot": "alpha",
                    "type": t,
                    "source": "s",
                    "data": {},
                }
            )
            for t in ("bridge_down", "reload_failed", "restart_failed", "send_miss")
        ]
        events_file.write_text("\n".join(lines) + "\n")

        result = collect_events(tmp_path, critical_only=True)
        types = {e["type"] for e in result}
        assert "bridge_down" in types
        assert "reload_failed" in types
        assert "restart_failed" in types
        # send_miss is informational (emit_fleet_notice), not operator-actionable
        assert "send_miss" not in types

    def test_critical_types_set_contents(self):
        assert {
            "bridge_down",
            "reload_failed",
            "restart_failed",
            "rc_timeout",
        } <= CRITICAL_TYPES
        assert "send_miss" not in CRITICAL_TYPES

    def test_events_sorted_by_timestamp(self, events_dir):
        events = collect_events(events_dir)
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps)

    def test_skips_malformed_json(self, events_dir):
        bad_file = events_dir / "alpha" / "data" / "events" / "bad.jsonl"
        bad_file.write_text('{"ts":"ok"}\nnot json\n{"ts":"also ok"}\n')
        events = collect_events(events_dir, bot="alpha")
        # 4 from fixture + 2 valid from bad file
        assert len(events) == 6

    def test_empty_dir_returns_empty(self, tmp_path):
        bots = tmp_path / "bots"
        bots.mkdir()
        assert collect_events(bots) == []


@pytest.fixture
def fleet_ledger(tmp_path):
    """A fleet-level ledger at <root>/state/events, alongside the bots dir."""
    ledger = tmp_path / "state" / "events"
    ledger.mkdir(parents=True)
    (ledger / "fleet-2026-06-10.jsonl").write_text(
        "\n".join(
            json.dumps(ev)
            for ev in (
                {
                    "ts": "2026-06-10T09:00:00-04:00",
                    "bot": "alpha",
                    "type": "bot_teardown_started",
                    "source": "spin-down",
                    "data": {"action": "spin-down --purge", "actor": "clog"},
                },
                {
                    "ts": "2026-06-10T09:30:00-04:00",
                    "bot": "fleet",
                    "type": "reload_failed",
                    "source": "reload",
                    "data": {},
                },
            )
        )
        + "\n"
    )
    return ledger


class TestFleetLedger:
    """Events that outlive — or never had — a bot dir live in the fleet ledger.

    Reading only per-bot dirs makes them write-only in practice: a spin-down
    receipt exists precisely to survive the directory it documents, so a reader
    blind to that ledger cannot show the one record it was written for.
    """

    def test_fleet_events_are_collected(self, events_dir, fleet_ledger):
        events = collect_events(events_dir, fleet_events_dir=fleet_ledger)
        assert len(events) == 6  # 4 per-bot + 2 fleet-level

    def test_fleet_events_honour_type_filter(self, events_dir, fleet_ledger):
        events = collect_events(
            events_dir, event_type="reload_failed", fleet_events_dir=fleet_ledger
        )
        assert len(events) == 1
        assert events[0]["type"] == "reload_failed"

    def test_bot_filter_applies_to_the_shared_ledger(self, events_dir, fleet_ledger):
        # Per-bot rows are filtered by directory; fleet rows share one file, so
        # --bot has to key on the field or it would leak other bots' events.
        events = collect_events(events_dir, bot="alpha", fleet_events_dir=fleet_ledger)
        assert {e["bot"] for e in events} == {"alpha"}
        assert any(e["type"] == "bot_teardown_started" for e in events)

    def test_absent_ledger_is_harmless(self, events_dir, tmp_path):
        events = collect_events(events_dir, fleet_events_dir=tmp_path / "nope")
        assert len(events) == 4


class TestFormatEventTable:
    def test_table_output(self, events_dir):
        events = collect_events(events_dir)
        output = format_event_table(events)
        assert "alpha" in output
        assert "session_missing" in output
        assert "TIME" in output

    def test_no_events(self):
        assert format_event_table([]) == "No events found."

    def test_detail_truncation(self):
        ev = {
            "ts": "2026-06-10T10:00:00-04:00",
            "bot": "alpha",
            "type": "test",
            "source": "test",
            "data": {"detail": "x" * 100},
        }
        output = format_event_table([ev])
        assert "..." in output


class TestEventCoverageIsStatedNotAssumed:
    """The events half of the class. ``No events found.`` was the same output
    whether the fleet was quiet or no instrument had ever written — but unlike
    #1216 this one is a COVERAGE question, not a refusal: partial data is still
    worth having, so the read continues and states its floor.
    """

    def test_full_coverage_says_nothing(self, tmp_path):
        """Silence on a healthy run is deliberate. A bound printed every time is
        wallpaper within a week, and a disclosure people have learned to skip is
        worse than none — it is the same false assurance with an alibi."""
        from claudlobby.commands.events import collect_events, coverage_line

        ev = tmp_path / "b1" / "data" / "events"
        ev.mkdir(parents=True)
        (ev / "fleet-2026-01-01.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "bot": "b1", "type": "x"}) + "\n"
        )
        cov: dict = {}
        collect_events(tmp_path, coverage=cov)
        assert cov["sources_read"] == cov["sources_total"]
        assert coverage_line(cov) == ""

    def test_a_bot_with_no_events_dir_is_counted_as_unread(self, tmp_path):
        from claudlobby.commands.events import collect_events, coverage_line

        (tmp_path / "b1" / "data" / "events").mkdir(parents=True)
        (tmp_path / "b2").mkdir()  # never emitted — no data/events
        cov: dict = {}
        collect_events(tmp_path, coverage=cov)
        assert cov["sources_total"] == 2
        assert cov["sources_read"] == 1
        line = coverage_line(cov)
        assert "read 1 of 2" in line
        assert "no events directory" in line

    def test_zero_readable_sources_is_a_refusal_not_a_quiet_fleet(self, tmp_path):
        """The one case that must be loud: "No events found." over ZERO reachable
        sources is a claim about the estate drawn from an instrument that was
        never wired."""
        from claudlobby.__main__ import main

        fleet = tmp_path / "local" / "f1"
        (fleet / "runtime" / "bots" / "b1").mkdir(parents=True)
        (fleet / "fleet.yaml").write_text("fleet:\n  name: f1\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        rc = main(["--root", str(tmp_path), "--fleet", "f1", "events"])
        assert rc == 1

    def test_partial_coverage_keeps_rc_zero(self, tmp_path):
        """Partial is not a failure — the rows that WERE read are real, and
        refusing would throw away good data to protest missing data."""
        from claudlobby.__main__ import main

        fleet = tmp_path / "local" / "f1"
        bots = fleet / "runtime" / "bots"
        ev = bots / "b1" / "data" / "events"
        ev.mkdir(parents=True)
        (ev / "fleet-2026-01-01.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "bot": "b1", "type": "x"}) + "\n"
        )
        (bots / "b2").mkdir()
        (fleet / "fleet.yaml").write_text("fleet:\n  name: f1\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        rc = main(["--root", str(tmp_path), "--fleet", "f1", "events"])
        assert rc == 0

    def test_an_unreadable_file_is_named_rather_than_merely_counted(self, tmp_path):
        """An absent events dir is normal for a bot that has not emitted yet, so
        it is counted. An unreadable FILE is a fault someone can fix, so it is
        named."""
        from claudlobby.commands.events import collect_events, coverage_line

        if os.geteuid() == 0:
            pytest.skip("root ignores the mode bits")
        ev = tmp_path / "b1" / "data" / "events"
        ev.mkdir(parents=True)
        bad = ev / "fleet-2026-01-01.jsonl"
        bad.write_text("{}\n")
        bad.chmod(0o000)
        try:
            cov: dict = {}
            collect_events(tmp_path, coverage=cov)
            assert cov["unreadable"] == [str(bad)]
            assert "unreadable:" in coverage_line(cov)
            assert str(bad) in coverage_line(cov)
        finally:
            bad.chmod(0o644)

    def test_coverage_is_opt_in_so_existing_callers_are_untouched(self, tmp_path):
        """brief.py and eleven tests consume the list return. The bound is an
        out-param precisely so nothing else had to change."""
        from claudlobby.commands.events import collect_events

        (tmp_path / "b1" / "data" / "events").mkdir(parents=True)
        assert collect_events(tmp_path) == []
