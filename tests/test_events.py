"""Tests for the claudlobby events subcommand."""

import json
from datetime import datetime, timezone

import pytest

from claudlobby.commands.events import CRITICAL_TYPES, collect_events, format_event_table


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

    def test_filter_critical_only_includes_bridge_and_lifecycle_failures(self, tmp_path):
        """bridge_down/reload_failed/restart_failed are emit_failure_alert events —
        operator-actionable, so they must surface under --critical like service_down."""
        bot_dir = tmp_path / "alpha"
        events = bot_dir / "data" / "events"
        events.mkdir(parents=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events_file = events / f"fleet-{today}.jsonl"
        lines = [
            json.dumps({"ts": "2026-07-06T10:00:00-04:00", "bot": "alpha", "type": t, "source": "s", "data": {}})
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
