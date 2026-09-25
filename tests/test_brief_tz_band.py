"""Exercise the brief/event production read doors with the real local clock."""
from datetime import datetime, timedelta, timezone
import time

import pytest

from claudlobby.brief import build_brief, format_brief
from claudlobby.commands.events import collect_plane_events, format_event_table, plane_events_conn
from claudlobby.plane.emit_api import emit_batch
from tests.test_brief import root, paths, _fleet, _plane_carrier  # real isolated fixtures


@pytest.mark.parametrize("offset", [0, -4, 2])
def test_alert_window_tz_band_uses_instant_and_preserves_local_clock(paths, monkeypatch, offset):
    try:
        with monkeypatch.context() as m:
            m.setenv("TZ", "America/New_York")
            time.tzset()
            now = datetime(2026, 9, 2, 0, tzinfo=timezone.utc)
            local = timezone(timedelta(hours=offset))
            instants = [now - timedelta(hours=24, minutes=1), now - timedelta(hours=23, minutes=59)]
            for i, at in enumerate(instants):
                result = emit_batch(paths.root, [{
                    "event_type": "system", "emitter": "test", "fleet": "test-fleet", "source_ref": f"fleet-events:sha:boundary{i}",
                    "occurred_at": at.isoformat(),
                    "payload": {"event": "script_error", "subject_kind": "actor", "subject": "bot:test-fleet/alex",
                                "data": {"source": "test", "legacy_ts": at.astimezone(local).isoformat(), "data": {"n": i}}}}])
                assert result[0].status == "committed"
            brief = build_brief(_fleet(), paths, "alex", int(now.timestamp()))
            assert len(brief["alerts"]) == 1
            assert brief["alerts"][0]["ts"] == "2026-09-01T00:01:00Z"
            assert "UTC" in next(line for line in format_brief(brief).splitlines() if line.startswith("ALERTS"))
            conn, note = plane_events_conn(paths)
            assert conn is not None, note
            try:
                events = collect_plane_events(conn, paths, fleet="test-fleet", bot="alex", since="2026-09-01T00:00:00Z")
            finally:
                conn.close()
            assert len(events) == 1
            assert events[0]["ts"] == brief["alerts"][0]["ts"]
            assert events[0]["ts_local"] == instants[1].astimezone(local).isoformat()
            assert "2026-09-01T00:01:00Z" in format_event_table(events)
    finally:
        time.tzset()


def test_legacy_row_normalizes_an_offset_occurred_at_without_losing_microseconds():
    from tests.plane_fixtures import _stdlib_readers
    readers = _stdlib_readers()
    row = readers.legacy_event_row("2026-09-02T02:00:00.000001+02:00", "script_error", "critical", "actor", "bot:f/a", None, 0, "f")
    assert row["ts"] == "2026-09-02T00:00:00.000001Z"
    assert row["ts_local"] is None
    assert "2026-09-02T00:00:00Z" in format_event_table([row])
