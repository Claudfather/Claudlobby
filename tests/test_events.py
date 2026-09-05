"""Tests for the claudlobby events subcommand — the plane, and nothing else
(F18 closure, R2b).

Every fixture lands its events on a plane under a throwaway root the way the
door does (`_land`: a system event with the `fleet-events:` provenance and a
{source, legacy_ts, data} detail); the reader renders them back as the rows
the retired files held, so the assertions are the old suite's.

Deleted with the files (their subject no longer exists):
TestCollectEvents.test_skips_malformed_json (ingest validates the detail; a
truncated one is disclosed — the renderer's pin lives in
test_plane_cutover_events), TestFleetLedger.test_absent_ledger_is_harmless,
TestEventCoverageIsStatedNotAssumed (five tests: a coverage floor over file
sources), TestUnlistableDirectorySources (three) and
test_late_enumeration_failure_lands_in_unreadable_coverage. The one
unreachable state left is the plane's, and it REFUSES (rc 3) rather than
stating a floor: partial data was worth having when the sources were files;
the plane is one source, reachable or not.
"""

from __future__ import annotations

import sqlite3

import pytest

from claudlobby.commands.events import (
    CRITICAL_TYPES,
    collect_plane_events,
    format_event_table,
    plane_events_conn,
)
from claudlobby.paths import Paths
from claudlobby.plane.registries import SYSTEM_EVENT_SEVERITY
from tests.plane_fixtures import F, _scene
from tests.test_plane_cutover_events import _drop_plane, _events_cmd, _land, _rows


@pytest.fixture
def scene(tmp_path):
    """The old fixture's four rows for `alpha`, on the plane."""
    root, paths, _, _ = _scene(tmp_path)
    _land(root, "alpha", "session_missing", "2026-06-10T10:00:00-04:00", {"session": "alpha"})
    _land(root, "alpha", "service_down", "2026-06-10T10:05:00-04:00",
          {"unit": "com.test.eng.alpha", "state": "failed"})
    _land(root, "alpha", "tool_call", "2026-06-10T10:10:00-04:00",
          {"tool": "Read", "event": "PreToolUse"}, source="vitals")
    _land(root, "alpha", "keepalive", "2026-06-10T10:15:00-04:00", {"state": "IDLE"}, source="keepalive")
    return root, paths


def _collect(paths, **kw):
    conn, note = plane_events_conn(paths)
    assert conn is not None, note
    try:
        return collect_plane_events(conn, paths, **kw)
    finally:
        conn.close()


class TestCollectPlaneEvents:
    def test_collects_all_events(self, scene):
        _root, paths = scene
        assert len(_collect(paths)) == 4

    def test_filter_by_type(self, scene):
        _root, paths = scene
        events = _collect(paths, event_type="service_down")
        assert len(events) == 1
        assert events[0]["type"] == "service_down"

    def test_filter_by_bot(self, scene):
        _root, paths = scene
        assert len(_collect(paths, bot="alpha")) == 4
        assert len(_collect(paths, bot="ALPHA")) == 4          # the alias compare is case-insensitive

    def test_filter_by_bot_no_match(self, scene):
        _root, paths = scene
        assert _collect(paths, bot="nonexistent") == []

    def test_filter_by_source(self, scene):
        _root, paths = scene
        assert len(_collect(paths, source="pulse")) == 2

    def test_filter_critical_only(self, scene):
        _root, paths = scene
        types = {e["type"] for e in _collect(paths, critical_only=True)}
        assert "tool_call" not in types
        assert "keepalive" not in types
        assert "session_missing" in types
        assert "service_down" in types

    def test_filter_critical_only_includes_bridge_and_lifecycle_failures(self, tmp_path):
        """bridge_down/reload_failed/restart_failed are emit_failure_alert events —
        operator-actionable, so they must surface under --critical like
        service_down. On the plane that is the registry's severity, stamped at
        ingest — one definition for the CLI, brief and fleet-pulse."""
        root, paths, _, _ = _scene(tmp_path)
        for i, t in enumerate(("bridge_down", "reload_failed", "restart_failed", "send_miss")):
            _land(root, "alpha", t, f"2026-07-06T10:0{i}:00-04:00", {}, source="s")
        types = {e["type"] for e in _collect(paths, critical_only=True)}
        assert types == {"bridge_down", "reload_failed", "restart_failed"}
        # send_miss is informational (emit_fleet_notice), not operator-actionable
        assert SYSTEM_EVENT_SEVERITY["send_miss"] != "critical"

    def test_critical_types_set_contents(self):
        assert {
            "bridge_down",
            "reload_failed",
            "restart_failed",
            "rc_timeout",
        } <= CRITICAL_TYPES
        assert "send_miss" not in CRITICAL_TYPES

    def test_events_sorted_by_timestamp(self, scene):
        _root, paths = scene
        timestamps = [e["ts"] for e in _collect(paths)]
        assert timestamps == sorted(timestamps)

    def test_the_rows_are_the_legacy_shape_and_nothing_private_leaks(self, scene):
        _root, paths = scene
        row = _collect(paths, event_type="session_missing")[0]
        assert row == {"ts": "2026-06-10T10:00:00-04:00", "bot": "alpha", "type": "session_missing",
                       "source": "pulse", "data": {"session": "alpha"}}

    def test_a_fleet_with_no_events_yet_is_an_honest_empty(self, tmp_path):
        """A reachable plane that holds no fleet event for the fleet: nothing
        — rc 0 and "No events found." — because the instrument was reached
        and found nothing, the one case that line is true for."""
        root, paths, _, _ = _scene(tmp_path)               # dispatches and reports, no fleet event
        assert _collect(paths) == []
        r = _events_cmd(root)
        assert r.returncode == 0 and "No events found." in r.stdout


class TestFleetLevelRows:
    """Events that outlive — or never had — a bot dir: a spin-down receipt
    exists precisely to survive the directory it documents. On the plane they
    anchor on the FLEET and render as bot `fleet`; --bot keys on the row."""

    def test_fleet_rows_are_collected_and_filtered(self, scene):
        root, paths = scene
        _land(root, "alpha", "bot_teardown_started", "2026-06-10T09:00:00-04:00",
              {"action": "spin-down --purge", "actor": "clog"}, source="spin-down")
        _land(root, "fleet", "reload_failed", "2026-06-10T09:30:00-04:00", {}, source="reload")
        assert len(_collect(paths)) == 6                            # 4 + the receipt + the fleet row
        only = _collect(paths, event_type="reload_failed")
        assert len(only) == 1 and only[0]["bot"] == "fleet"
        mine = _collect(paths, bot="alpha")
        assert {e["bot"] for e in mine} == {"alpha"}
        assert any(e["type"] == "bot_teardown_started" for e in mine)
        assert [e["type"] for e in _collect(paths, bot="fleet")] == ["reload_failed"]


class TestFormatEventTable:
    def test_table_output(self, scene):
        _root, paths = scene
        output = format_event_table(_collect(paths))
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


class TestPlaneEventsConn:
    """The one contract brief consumes: (conn, None) when the plane can
    answer, (None, why) when it cannot — and NEVER a flag or a declaration
    in between (F18 closure, R2b)."""

    def test_a_reachable_plane_opens_without_any_flag(self, scene, monkeypatch):
        _root, paths = scene
        for value in ("0", "1", "true"):
            monkeypatch.setenv("PLANE_READ_EVENTS", value)             # the retired flag: inert
            conn, note = plane_events_conn(paths)
            assert conn is not None and note is None
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1   # read-only, structurally
            conn.close()
        monkeypatch.delenv("PLANE_READ_EVENTS", raising=False)
        conn, note = plane_events_conn(paths)
        assert conn is not None and note is None
        conn.close()

    def test_no_fleet_named_is_refused(self, scene):
        root, _paths = scene
        conn, note = plane_events_conn(Paths(root=root, fleet_dir=None))
        assert conn is None and "no fleet is named" in note

    def test_no_db_is_refused(self, tmp_path):
        root = tmp_path / "root"
        (root / "local" / F).mkdir(parents=True)
        conn, note = plane_events_conn(Paths(root=root, fleet_dir=root / "local" / F))
        assert conn is None and "no plane db" in note

    def test_a_schema_less_db_is_refused(self, tmp_path):
        root = tmp_path / "root"
        (root / "local" / F).mkdir(parents=True)
        (root / "state" / "plane").mkdir(parents=True)
        with sqlite3.connect(root / "state" / "plane" / "plane.db") as c:
            c.execute("CREATE TABLE x (a)")
        conn, note = plane_events_conn(Paths(root=root, fleet_dir=root / "local" / F))
        assert conn is None and note

    def test_a_fleet_the_plane_never_saw_is_refused_not_quiet(self, scene):
        root, _paths = scene
        conn, note = plane_events_conn(Paths(root=root, fleet_dir=root / "local" / "ghost"))
        assert conn is None and "no bot of fleet 'ghost'" in note

    def test_an_empty_plane_root_refuses_rather_than_creating_a_db(self, tmp_path):
        """A read door on a typo'd root must not open an empty plane and
        report everything missing (the J1 exists-before-connect finding)."""
        root, _paths, _, _ = _scene(tmp_path)                         # a real plane, then the wrong root
        wrong = tmp_path / "elsewhere"
        (wrong / "local" / F).mkdir(parents=True)
        conn, note = plane_events_conn(Paths(root=wrong, fleet_dir=wrong / "local" / F))
        assert conn is None and "no plane db" in note
        assert not (wrong / "state" / "plane" / "plane.db").exists()
        assert (root / "state" / "plane" / "plane.db").exists()


class TestTheCommandRefuses:
    def test_an_unreachable_plane_is_rc_3_with_nothing_on_stdout(self, scene):
        """"No events found." over an instrument that could not be reached
        is a claim about the estate drawn from nothing — the #1216 class."""
        root, _paths = scene
        assert _rows(_events_cmd(root, "--json"))                          # reachable: rows
        _drop_plane(root)
        gone = _events_cmd(root, "--json")
        assert gone.returncode == 3 and gone.stdout == ""
        assert "UNREACHABLE" in gone.stderr and "no plane db" in gone.stderr
        table = _events_cmd(root)
        assert table.returncode == 3 and table.stdout == ""                # the table mode refuses the same way

    def test_a_missing_bots_dir_is_not_a_gate_any_more(self, tmp_path):
        """The files' first gate ("No bots directory", rc 1) guarded a walk
        that no longer happens; the plane's identity probe is the gate."""
        root, paths, _, _ = _scene(tmp_path)
        assert not paths.runtime_bots.exists()
        _land(root, "w1", "session_missing", "2026-09-03T10:00:00Z", {"session": "w1"})
        rows = _rows(_events_cmd(root, "--json"))
        assert [(r["bot"], r["type"]) for r in rows] == [("w1", "session_missing")]
