"""The second retention lane deletes an ALLOWLIST and proves the complement (#1659).

Measured on this host's one complete post-cutover day (2026-09-21): **15,422**
system events, of which `tool_call` 12,963 and `wip_uncommitted` 2,163 — 98% of
the volume in two types. Projected at that rate: 462k rows in 30 days, ~5.6M in
a year, each also pinning an ingest-ledger row, on an SD card.

**Why these tests are shaped as a complement rather than a checklist.** The
hazard is not deleting too little, it is deleting something a door depends on:
`lib/selfstart-snapshot.sh` fails CLOSED on an unreachable rescue-receipt read
(exit 7) but reads an ABSENT receipt as a certain no-receipt — so a pruned
receipt silently credits a rescued boot as a self-start. Enumerating what we
checked would leave the next type unprotected; enumerating what may GO makes
the omission safe by construction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from claudlobby.plane.retention import (
    PRUNABLE_SYSTEM_EVENTS,
    prune_system_events,
)


def _db(rows):
    """(kind, event, ingested_at) -> a plane-shaped events table + a ledger."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE events (event_id TEXT, kind TEXT, event TEXT,"
              " ingested_at TEXT)")
    c.execute("CREATE TABLE ingest_ledger (ingest_seq INTEGER PRIMARY KEY,"
              " event_id TEXT, family TEXT, ingested_at TEXT)")
    for i, (kind, ev, at) in enumerate(rows):
        c.execute("INSERT INTO events VALUES (?,?,?,?)", (f"e{i}", kind, ev, at))
        c.execute("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                  " VALUES (?,?,?)", (f"e{i}", kind, at))
    c.commit()
    return c


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _events(c):
    return sorted(r[0] for r in c.execute("SELECT event FROM events"))


class TestTheLaneDeletesOnlyItsAllowlist:
    def test_it_ages_out_the_two_that_are_98_percent_of_the_volume(self):
        c = _db([("system", "tool_call", _ago(40)),
                 ("system", "wip_uncommitted", _ago(40))])
        assert prune_system_events(c, days=30) == 2
        assert _events(c) == []

    def test_a_type_NOT_on_the_allowlist_survives_however_old(self):
        """The complement, and the case the lane exists to not break:
        `fleet_rescue` is read by a boot gate that treats absence as a certain
        no-receipt, so pruning one is worse than keeping a year of them."""
        c = _db([("system", "fleet_rescue", _ago(4000)),
                 ("system", "tool_call", _ago(4000))])
        assert prune_system_events(c, days=30) == 1
        assert _events(c) == ["fleet_rescue"]

    @pytest.mark.parametrize("ev", [
        "fleet_rescue", "checkin_decision", "session_digest", "reports_acked",
        "vault_guard_denied", "script_error", "boot_capture", "probe_noop",
        "bridge_down", "overdue_dispatch", "keepalive_skip", "pane_stuck",
        "some_event_invented_next_year",
    ])
    def test_everything_else_is_kept_including_a_type_nobody_has_written_yet(self, ev):
        """The complement stated as a property rather than a list. The last
        parameter is the point: a type that does not exist yet is protected by
        the DIRECTION of the allowlist, not by anyone remembering it."""
        c = _db([("system", ev, _ago(9999))])
        assert prune_system_events(c, days=30) == 0
        assert _events(c) == [ev]

    def test_a_caller_cannot_widen_the_lane_by_passing_a_set(self):
        """The allowlist is the contract, not a default. If a call site could
        pass its own set, the complement proof would hold only for the call
        sites somebody checked — which is the enumerate-what-you-checked
        failure this design exists to avoid."""
        c = _db([("system", "fleet_rescue", _ago(40))])
        with pytest.raises(ValueError, match="not allowlisted"):
            prune_system_events(c, days=30, events={"fleet_rescue"})
        assert _events(c) == ["fleet_rescue"]

    def test_the_allowlist_is_small_and_every_member_is_named(self):
        """A growing allowlist is the thing to notice in review, so pin it.
        Adding a member should require touching this assertion and writing why."""
        assert PRUNABLE_SYSTEM_EVENTS == {"tool_call", "wip_uncommitted"}


class TestTheHardEdgesItInheritsFromTheSampleLane:
    def test_the_ingest_ledger_is_never_touched(self):
        """It is the dedupe horizon; shrinking it lets a replayed old event
        re-ingest as new. This lane reclaims event rows and not ledger rows,
        which is a deliberate half-measure stated as one."""
        c = _db([("system", "tool_call", _ago(40))])
        before = c.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0]
        prune_system_events(c, days=30)
        assert c.execute("SELECT COUNT(*) FROM ingest_ledger").fetchone()[0] == before

    def test_rows_inside_the_window_are_kept(self):
        c = _db([("system", "tool_call", _ago(5))])
        assert prune_system_events(c, days=30) == 0

    def test_it_never_reaches_a_non_system_kind(self):
        """Scoped by kind AND event: a `task` row that happened to share a name
        is not this lane's to delete."""
        c = _db([("task", "tool_call", _ago(40))])
        assert prune_system_events(c, days=30) == 0
        assert _events(c) == ["tool_call"]

    def test_an_empty_allowlist_deletes_nothing(self):
        c = _db([("system", "tool_call", _ago(40))])
        assert prune_system_events(c, days=30, events=frozenset()) == 0
        assert _events(c) == ["tool_call"]
