"""Unit tests for lib/dispatch-overdue.py — the dispatch watchdog matcher,
including the P4 task-id join matrix (semantics: overdue_all docstring)."""

from __future__ import annotations

import datetime as _dt
import os

import pytest

from tests.conftest import (
    dispatch_row as _dispatch,
    load_lib_module,
    report_row as _report,
    write_jsonl as _write_jsonl,
)

dispatch_overdue = load_lib_module("dispatch-overdue")


class TestOverdue:
    def test_not_yet_due(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        # now (1500) < expected_by (2000) → not overdue
        assert dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 1500) == []

    def test_overdue_no_report(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        res = dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 2600)
        assert res == [(1000, 2000, 600, "-")]

    def test_closed_by_terminal_report(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        # report at 2026-05-27T10:30:00Z is after dispatch → closes it
        _write_jsonl(rlog, [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        assert dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 9999999999) == []

    def test_stale_report_before_dispatch_does_not_close(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        # dispatched_at corresponds to a time AFTER this old report
        _write_jsonl(dlog, [_dispatch("eng-1", 1800000000, 1800000600)])
        _write_jsonl(rlog, [_report("eng-1", "2020-01-01T00:00:00Z", "completed")])
        res = dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 1800001000)
        assert len(res) == 1

    def test_progress_report_does_not_close(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [_report("eng-1", "2026-05-27T10:30:00Z", "progress")])
        assert len(dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 2600)) == 1

    def test_other_bot_ignored(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-2", 1000, 2000)])
        _write_jsonl(rlog, [])
        assert dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 2600) == []

    def test_case_insensitive_bot_match(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("Eng-1", 1000, 2000)])
        _write_jsonl(rlog, [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        # report (lowercase) should close dispatch (mixed case)
        assert dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 9999999999) == []

    def test_missing_files(self, tmp_path):
        assert (
            dispatch_overdue.overdue(
                "eng-1", str(tmp_path / "no"), str(tmp_path / "no2"), 99
            )
            == []
        )


class TestExpiryCap:
    """#460: a never-closing dispatch must stop being reported overdue past max_age,
    so fleet-pulse stops re-emitting overdue_dispatch every cycle forever."""

    def test_stale_open_dispatch_expires(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        # Past deadline, never reported, but age (100000s) > cap (3600s) → expired.
        assert (
            dispatch_overdue.overdue(
                "eng-1", str(dlog), str(rlog), 101000, max_age=3600
            )
            == []
        )

    def test_within_max_age_still_overdue(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        # age (3000s) < cap (3600s), past deadline → still overdue.
        assert dispatch_overdue.overdue(
            "eng-1", str(dlog), str(rlog), 4000, max_age=3600
        ) == [(1000, 2000, 2000, "-")]

    def test_default_cap_is_24h(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("mason", 1000, 2000)])
        _write_jsonl(rlog, [])
        # Just under 24h → overdue; just over → expired — under the DEFAULT (no max_age passed).
        assert (
            len(dispatch_overdue.overdue("mason", str(dlog), str(rlog), 1000 + 86399))
            == 1
        )
        assert (
            dispatch_overdue.overdue("mason", str(dlog), str(rlog), 1000 + 86401) == []
        )

    def test_cap_disabled_with_zero(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        # max_age=0 disables the cap → an ancient open dispatch still counts as overdue.
        assert (
            len(
                dispatch_overdue.overdue(
                    "eng-1", str(dlog), str(rlog), 10_000_000, max_age=0
                )
            )
            == 1
        )

    def test_never_closing_dispatch_stops_being_flagged(self, tmp_path):
        """#460 anchor: a never-reported dispatch drops out of the overdue set past the
        cap, so fleet-pulse (which emits only what the matcher returns) stops re-emitting
        overdue_dispatch. The matcher is stateless, so one past-cap empty result is the
        guarantee for this cycle and every later one."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [])
        # Far past the default 24h cap, never reported → nothing to emit.
        assert (
            dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 1000 + 500000) == []
        )

    def test_closed_report_recognized_even_when_aged(self, tmp_path):
        """A dispatch closed by a terminal report is closed, not merely expired —
        the report path still short-circuits before the age cap."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2000)])
        _write_jsonl(rlog, [_report("eng-1", "2026-05-27T10:30:00Z", "completed")])
        # Aged well past the cap AND closed → still [] (closed wins, order preserved).
        assert (
            dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), 1000 + 999999) == []
        )


# --- join matrix (dispatch-overdue.py) --------------------------------------------


class TestJoinMatrix:
    NOW = 2000

    def _run(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return dispatch_overdue.overdue_all(str(dlog), str(rlog), self.NOW)

    def test_id_report_closes_exactly_its_own_dispatch(self, tmp_path):
        out = self._run(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            ],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert [d[0] for d in out.get("w1", [])] == [200], (
            "the un-reported sibling dispatch must stay open"
        )

    def test_idless_terminal_never_closes_an_id_dispatch(self, tmp_path):
        # The #447 fix, preserved: one id-less report must not blanket-close.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z")],  # no task_id
        )
        assert out.get("w1"), "id'd dispatch must remain overdue"

    def test_idless_terminal_closes_idless_dispatch(self, tmp_path):
        # Legacy rows keep the pre-migration (bot, ts) semantics — no flag-day.
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z")],
        )
        assert not out.get("w1")

    def test_id_report_closes_idless_dispatch_too(self, tmp_path):
        # An id-carrying terminal report still satisfies a legacy dispatch
        # (it is strictly more informative than the old contract required).
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000)],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-999-ffff")],
        )
        assert not out.get("w1")

    def test_report_from_wrong_bot_does_not_close(self, tmp_path):
        # Review finding (#518): the id join must be scoped by (bot, id) —
        # a peer echoing (or mishearing) another bot's task id must not
        # silence the watchdog on the real owner's still-open dispatch.
        out = self._run(
            tmp_path,
            [_dispatch("worker-a", 100, 1000, task_id="t-100-aaaa")],
            [_report("worker-b", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        assert out.get("worker-a"), "wrong-bot report must not close the dispatch"

    def test_wrong_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-777-dddd")],
        )
        assert out.get("w1")

    def test_progress_report_with_id_defers_but_never_closes(self, tmp_path):
        """Progress DEFERS the alarm while it is fresh and never closes the dispatch.

        Both halves matter. Asserting only the deferral would pass against a change
        that silenced the row permanently — which is closing by another name, and the
        thing this test originally existed to forbid.
        """
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")])
        _write_jsonl(
            rlog,
            [
                _report(
                    "w1",
                    "1970-01-01T00:05:00Z",
                    status="progress",
                    task_id="t-100-aaaa",
                )
            ],
        )
        # progress at epoch 300; at NOW=2000 it is 1700s old, inside the grace
        assert not dispatch_overdue.overdue_all(str(dlog), str(rlog), self.NOW).get(
            "w1"
        )
        # once the grace lapses the row is overdue again — deferred, never closed
        later = 300 + dispatch_overdue.DEFAULT_PROGRESS_GRACE_S + 1
        assert dispatch_overdue.overdue_all(str(dlog), str(rlog), later).get("w1"), (
            "a progress report must never permanently close a dispatch"
        )


class TestMissingIdCounter:
    def test_counts_idless_terminal_reports(self, tmp_path):
        rlog = tmp_path / "r.jsonl"
        _write_jsonl(
            rlog,
            [
                _report("w1", "1970-01-01T00:05:00Z"),
                _report("w1", "1970-01-01T00:06:00Z", task_id="t-1-aaaa"),
                _report("w2", "1970-01-01T00:07:00Z", status="progress"),
                _report("w2", "1970-01-01T00:08:00Z", status="failed"),
            ],
        )
        # terminal + id-less: w1's first report and w2's failed report
        assert dispatch_overdue.missing_id_count(str(rlog)) == 2


class TestOrphanSplit:
    """#835 — a past-deadline row whose worker RESPAWNED after dispatch.

    The session holding the task id is gone, so the id can never be echoed and
    the row would alarm every cycle until it aged out. Split it out of overdue,
    but keep it listable: a task lost to a restart is evidence, not noise.
    """

    NOW = 2000

    def _bots_dir(self, tmp_path, bot, spawn_epoch):
        data = tmp_path / "bots" / bot / "data"
        data.mkdir(parents=True, exist_ok=True)
        marker = data / ".spawn"
        marker.write_text("")
        os.utime(marker, (spawn_epoch, spawn_epoch))
        return str(tmp_path / "bots")

    def _logs(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return str(dlog), str(rlog)

    def test_respawn_after_dispatch_moves_row_out_of_overdue(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")], []
        )
        bots = self._bots_dir(tmp_path, "w1", 500)  # respawned AFTER dispatch
        assert dispatch_overdue.overdue_all(dlog, rlog, self.NOW, bots_dir=bots) == {}
        orphans = dispatch_overdue.orphaned_all(dlog, rlog, self.NOW, bots_dir=bots)
        assert [d[3] for d in orphans["w1"]] == ["t-100-aaaa"], (
            "the orphan must stay listable — reaping it silently deletes the "
            "evidence that a task was lost to a restart"
        )

    def test_same_incarnation_still_reports_overdue(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")], []
        )
        bots = self._bots_dir(tmp_path, "w1", 50)  # spawned BEFORE dispatch
        assert "w1" in dispatch_overdue.overdue_all(dlog, rlog, self.NOW, bots_dir=bots)
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW, bots_dir=bots) == {}

    def test_without_bots_dir_nothing_is_orphaned(self, tmp_path):
        """No marker access => keep alarming. Never retire a row on a guess."""
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")], []
        )
        assert "w1" in dispatch_overdue.overdue_all(dlog, rlog, self.NOW)
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW) == {}

    def test_missing_spawn_marker_keeps_row_overdue(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")], []
        )
        (tmp_path / "bots").mkdir()
        bots = str(tmp_path / "bots")
        assert "w1" in dispatch_overdue.overdue_all(dlog, rlog, self.NOW, bots_dir=bots)

    def test_idless_dispatch_never_orphans(self, tmp_path):
        """An id-less row closes on ANY later terminal report, so a respawned
        worker's next report still retires it — nothing to remember, nothing to
        orphan."""
        dlog, rlog = self._logs(tmp_path, [_dispatch("w1", 100, 1000)], [])
        bots = self._bots_dir(tmp_path, "w1", 500)
        assert "w1" in dispatch_overdue.overdue_all(dlog, rlog, self.NOW, bots_dir=bots)
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW, bots_dir=bots) == {}

    def test_a_closed_row_is_neither_overdue_nor_orphan(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-100-aaaa")],
        )
        bots = self._bots_dir(tmp_path, "w1", 500)
        assert dispatch_overdue.overdue_all(dlog, rlog, self.NOW, bots_dir=bots) == {}
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW, bots_dir=bots) == {}


class TestOpenTaskResolution:
    """#835 — the id report-back.sh supplies when the worker omits --task."""

    def _logs(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return str(dlog), str(rlog)

    def test_resolves_the_oldest_open_dispatch(self, tmp_path):
        """Oldest, not newest: the oldest is the row past its deadline and
        alarming, and it is what a serial FIFO worker just finished. Rows are
        written out of dispatch order to pin that this is time-ordered, not
        file-ordered."""
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            ],
            [],
        )
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-100-aaaa"

    def test_concurrent_dispatches_retire_in_dispatch_order(self, tmp_path):
        """The normal case, not an edge one — most active bots carry 2-3 open.
        Each report closes exactly one row, oldest first, so a sequence of
        reports drains the queue in the order it was sent."""
        dispatches = [
            _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
            _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
        ]
        reports: list = []
        drained = []
        for _ in range(3):
            dlog, rlog = self._logs(tmp_path, dispatches, reports)
            tid = dispatch_overdue.open_task_id("w1", dlog, rlog)
            drained.append(tid)
            reports.append(_report("w1", "1970-01-01T00:20:00Z", task_id=tid))
        assert drained == ["t-100-aaaa", "t-200-bbbb", "t-300-cccc"]
        dlog, rlog = self._logs(tmp_path, dispatches, reports)
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) is None

    def test_skips_already_closed_dispatches(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
            ],
            [_report("w1", "1970-01-01T00:10:00Z", task_id="t-300-cccc")],
        )
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-100-aaaa"

    def test_none_when_nothing_open(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:10:00Z", task_id="t-100-aaaa")],
        )
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) is None

    def test_scoped_to_the_bot(self, tmp_path):
        """A peer's open dispatch must never be handed to this bot — that is
        the cross-bot leak the watchdog join is deliberately scoped against."""
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w2", 300, 1000, task_id="t-300-cccc")], []
        )
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) is None

    def test_idless_dispatches_are_not_resolvable(self, tmp_path):
        dlog, rlog = self._logs(tmp_path, [_dispatch("w1", 100, 1000)], [])
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) is None

    def test_a_peers_report_does_not_close_this_bots_dispatch(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w2", "1970-01-01T00:10:00Z", task_id="t-100-aaaa")],
        )
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-100-aaaa"


class TestProgressLiveness:
    """The #1390-shaped question: does the alarm distinguish BUSY from STUCK?

    A fixed 30-minute budget was applied to every dispatch (measured 2026-08-04: 66 of
    66), while real tasks routinely ran longer — so the watchdog fired at T+30 on work
    still being done, and by the time the manager read the page the work was merged.
    Paging on finished work trains the reader to ignore the alarm, which costs the day
    something genuinely IS stuck.

    The two halves are inseparable. Silencing a busy worker is only safe if a dead one
    still alarms; a test for either alone passes against a change that breaks the other.
    """

    GRACE = 2700  # DEFAULT_PROGRESS_GRACE_S; asserted equal below so it cannot drift

    def _overdue(self, tmp_path, reports, now):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        # dispatched at 1000, due at 2800 — the real 30-minute budget
        _write_jsonl(dlog, [_dispatch("eng-1", 1000, 2800, task_id="t-1000-aaaa")])
        _write_jsonl(rlog, reports)
        return dispatch_overdue.overdue("eng-1", str(dlog), str(rlog), now)

    def test_grace_matches_the_measured_default(self):
        assert dispatch_overdue.DEFAULT_PROGRESS_GRACE_S == self.GRACE

    def test_a_busy_worker_is_silenced(self, tmp_path):
        """Past deadline, but reported progress 10 minutes ago → working, not stuck."""
        res = self._overdue(
            tmp_path,
            [_report("eng-1", "1970-01-01T01:00:00Z", status="progress")],  # epoch 3600
            now=3600 + 600,
        )
        assert res == [], "a worker reporting progress 10min ago was paged as overdue"

    def test_a_dead_worker_still_alarms(self, tmp_path):
        """Same dispatch, same single progress report — but the worker then went silent.

        This is the half that makes the other half safe. Deferral is bounded by the
        worker's own reporting: stop, and the alarm returns.
        """
        res = self._overdue(
            tmp_path,
            [_report("eng-1", "1970-01-01T01:00:00Z", status="progress")],  # epoch 3600
            now=3600 + self.GRACE + 1,
        )
        assert res, "a worker silent for longer than the grace must still alarm"

    def test_progress_before_the_dispatch_does_not_defer(self, tmp_path):
        """Liveness must be evidence about THIS dispatch's lifetime.

        A progress report from before the work was even handed out says nothing about
        whether the worker is on it now — the same reasoning as the existing
        stale-report-before-dispatch guard for terminal reports.
        """
        # epoch 900 — BEFORE the dispatch at 1000, but only 2100s before now=3000, so
        # it is comfortably inside the 2700s grace. Only the `da < lp` bound can reject
        # it. An earlier draft used epoch 30, which the grace bound rejected on age
        # alone: the test passed while saying nothing about the bound it names, and the
        # mutation run caught it.
        res = self._overdue(
            tmp_path,
            [_report("eng-1", "1970-01-01T00:15:00Z", status="progress")],
            now=3000,
        )
        assert res, "a progress report predating the dispatch deferred the alarm"

    def test_a_future_dated_progress_report_cannot_mute_the_alarm(self, tmp_path):
        """Clock skew or a hand-edited ledger must not buy silence.

        A report dated ahead of `now` yields a negative age, which satisfies any grace
        bound and would suppress the row forever — a permanent silent mute, the one
        outcome this change must never produce. Found by an existing test failing
        against the first draft, not by inspection.
        """
        res = self._overdue(
            tmp_path,
            [_report("eng-1", "2099-01-01T00:00:00Z", status="progress")],
            now=3000,
        )
        assert res, "a future-dated progress report muted the alarm"

    def test_grace_of_zero_disables_deferral(self, tmp_path, monkeypatch):
        """The escape hatch, mirroring DISPATCH_OVERDUE_MAX_AGE_S's `<= 0 disables`."""
        monkeypatch.setenv("DISPATCH_PROGRESS_GRACE_S", "0")
        res = self._overdue(
            tmp_path,
            [_report("eng-1", "1970-01-01T01:00:00Z", status="progress")],
            now=3600 + 600,
        )
        assert res, "grace=0 must restore the pre-change behaviour exactly"

    def test_terminal_report_still_closes_regardless_of_progress(self, tmp_path):
        """Deferral must not shadow closure: a finished dispatch reads closed, not
        deferred, so the row never reappears when the grace lapses."""
        res = self._overdue(
            tmp_path,
            [
                _report("eng-1", "1970-01-01T01:00:00Z", status="progress"),
                _report(
                    "eng-1",
                    "1970-01-01T01:05:00Z",
                    status="completed",
                    task_id="t-1000-aaaa",
                ),
            ],
            now=3600 + self.GRACE + 5000,
        )
        assert res == [], "a completed dispatch reappeared after the grace lapsed"


class TestOpenList:
    """#904 — the read door's list form. Same join as the resolver, wider set."""

    def _logs(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return str(dlog), str(rlog)

    def test_lists_every_open_row_oldest_first(self, tmp_path):
        """Written out of dispatch order, to pin that this is time-ordered."""
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 300, 1000, task_id="t-300-cccc"),
                _dispatch("w1", 100, 1000, task_id="t-100-aaaa"),
                _dispatch("w1", 200, 1000, task_id="t-200-bbbb"),
            ],
            [],
        )
        rows = dispatch_overdue.open_dispatches("w1", dlog, rlog)
        assert [t for _, _, t in rows] == ["t-100-aaaa", "t-200-bbbb", "t-300-cccc"]

    def test_open_task_id_is_this_lists_head(self, tmp_path):
        """One loop, not two: a resolver that could hand back an id this list
        does not contain is the desync class the module exists to prevent."""
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 300, 1000, task_id="t-c"),
                _dispatch("w1", 100, 1000, task_id="t-a"),
            ],
            [],
        )
        rows = dispatch_overdue.open_dispatches("w1", dlog, rlog)
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == rows[0][2]

    def test_is_deadline_blind(self, tmp_path):
        """A row inside its deadline is OPEN but not overdue — the distinction
        the door was added to make readable."""
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 9_999_999, task_id="t-early")], []
        )
        assert [
            t for _, _, t in dispatch_overdue.open_dispatches("w1", dlog, rlog)
        ] == ["t-early"]
        assert dispatch_overdue.overdue("w1", dlog, rlog, 1000) == []

    def test_is_a_superset_of_overdue(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-late"),
                _dispatch("w1", 200, 9_999_999, task_id="t-early"),
            ],
            [],
        )
        open_ids = {t for _, _, t in dispatch_overdue.open_dispatches("w1", dlog, rlog)}
        overdue_ids = {t for *_, t in dispatch_overdue.overdue("w1", dlog, rlog, 5000)}
        assert overdue_ids == {"t-late"}
        assert overdue_ids <= open_ids

    def test_terminal_report_removes_the_row(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-a")],
            [_report("w1", "2026-05-27T10:05:00Z", task_id="t-a")],
        )
        assert dispatch_overdue.open_dispatches("w1", dlog, rlog) == []

    def test_a_peers_report_does_not_close_this_bots_row(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-a")],
            [_report("w2", "2026-05-27T10:05:00Z", task_id="t-a")],
        )
        assert [
            t for _, _, t in dispatch_overdue.open_dispatches("w1", dlog, rlog)
        ] == ["t-a"]

    def test_idless_rows_are_not_listed(self, tmp_path):
        """Same gate as the resolver: only id'd rows are addressable."""
        dlog, rlog = self._logs(tmp_path, [_dispatch("w1", 100, 1000)], [])
        assert dispatch_overdue.open_dispatches("w1", dlog, rlog) == []

    def test_missing_expected_by_is_None_not_a_filter(self, tmp_path):
        """A row the resolver would still hand back must remain listable, or
        the door hides work that can still be closed."""
        row = _dispatch("w1", 100, 1000, task_id="t-a")
        del row["expected_by"]
        dlog, rlog = self._logs(tmp_path, [row], [])
        assert dispatch_overdue.open_dispatches("w1", dlog, rlog) == [
            (100, None, "t-a")
        ]
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-a"

    def test_scoped_to_the_bot(self, tmp_path):
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-mine"),
                _dispatch("w2", 100, 1000, task_id="t-theirs"),
            ],
            [],
        )
        assert [
            t for _, _, t in dispatch_overdue.open_dispatches("w1", dlog, rlog)
        ] == ["t-mine"]

    def test_cli_open_mode_prints_rows(self, tmp_path, monkeypatch, capsys):
        row = _dispatch("w1", 100, 1000, task_id="t-a")
        idless = _dispatch("w1", 150, 1000, task_id="t-b")
        del idless["expected_by"]
        dlog, rlog = self._logs(tmp_path, [row, idless], [])
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == "100 1000 t-a\n150 - t-b\n"

    def test_cli_open_mode_is_silent_when_nothing_is_open(
        self, tmp_path, monkeypatch, capsys
    ):
        dlog, rlog = self._logs(tmp_path, [], [])
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == ""

    def test_ties_keep_ledger_order_matching_the_old_strict_min(self, tmp_path):
        """The tie-break was asserted from reading the sort's stability; this
        pins it. `open_task_id` USED to scan with a strict `<`, which keeps the
        FIRST row seen on a tie. `list.sort` is stable, so it must agree — if it
        ever did not, the resolver would close a different dispatch than the one
        the list shows first, silently."""
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-ccc"),
                _dispatch("w1", 100, 1000, task_id="t-bbb"),
                _dispatch("w1", 100, 1000, task_id="t-aaa"),
            ],
            [],
        )
        rows = dispatch_overdue.open_dispatches("w1", dlog, rlog)
        assert [t for _, _, t in rows] == ["t-ccc", "t-bbb", "t-aaa"]
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-ccc"

    def test_a_tie_at_the_head_still_resolves_to_the_head(self, tmp_path):
        """Tie at the oldest timestamp, with a younger row written first — so
        file order and time order disagree and only the sort can be right."""
        dlog, rlog = self._logs(
            tmp_path,
            [
                _dispatch("w1", 500, 1000, task_id="t-young"),
                _dispatch("w1", 100, 1000, task_id="t-old-z"),
                _dispatch("w1", 100, 1000, task_id="t-old-a"),
            ],
            [],
        )
        rows = dispatch_overdue.open_dispatches("w1", dlog, rlog)
        assert [t for _, _, t in rows] == ["t-old-z", "t-old-a", "t-young"]
        assert (
            dispatch_overdue.open_task_id("w1", dlog, rlog) == rows[0][2] == "t-old-z"
        )

    # --- #1124: the identical-dispatched_at tie-break -------------------------
    #
    # open_task_id resolves the dispatch an id-less report-back closes, which is
    # MOST reports (report-back.sh omits --task in the common path, #847). #904
    # replaced its inline `da < best[0]` scan — strict less-than, so the first
    # row in ledger order wins a tie — with `rows[0]` off open_dispatches()'s
    # stable-sorted list. The two agree, and open_dispatches' docstring states
    # the guarantee, but nothing enforced it: it held by measurement, not by
    # mechanism, and one refactor away from silently inverting.
    #
    # Live harm class, not hypothetical (#878): on 2026-08-08 three ai-platform
    # reports resolved to task ids dispatched 2026-08-04 — a 4.6-day gap. A
    # tie-break regression adds one more way for an id-less report to close the
    # wrong dispatch: the finished task never shows done, the wrong one is
    # marked closed, and the watchdog alarms on work that actually completed.
    #
    # THE IDS ARE CHOSEN SO FILE ORDER AND ALPHABETICAL ORDER DISAGREE. An
    # implementation that broke ties by sorting on task_id would satisfy a
    # same-order-only test by coincidence; with t-bbb written first, ledger
    # order says t-bbb and alphabetical says t-aaa, so the two are separable.

    def _tied(self, tmp_path, first, second):
        """Two dispatches to one bot with IDENTICAL dispatched_at, in file order."""
        return self._logs(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id=first),
                _dispatch("w1", 100, 1000, task_id=second),
            ],
            [],
        )

    def test_tie_resolves_to_the_row_written_first(self, tmp_path):
        dlog, rlog = self._tied(tmp_path, "t-bbb", "t-aaa")
        # Ledger order, NOT the alphabetically-smaller id.
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-bbb"

    def test_tie_reversed_file_order_resolves_to_the_other_row(self, tmp_path):
        """Step 3, and the whole point: same two rows, order swapped, other
        answer. Without this a constant-by-coincidence implementation passes."""
        dlog, rlog = self._tied(tmp_path, "t-aaa", "t-bbb")
        assert dispatch_overdue.open_task_id("w1", dlog, rlog) == "t-aaa"

    def test_tie_answer_is_order_dependent_not_a_fixed_value(self, tmp_path):
        """States the property directly: the two orders must disagree. A single
        assertion no implementation can satisfy by returning a constant."""
        a, _ = self._tied(tmp_path, "t-bbb", "t-aaa")
        first = dispatch_overdue.open_task_id("w1", a, str(tmp_path / "r.jsonl"))
        b, _ = self._tied(tmp_path, "t-aaa", "t-bbb")
        second = dispatch_overdue.open_task_id("w1", b, str(tmp_path / "r.jsonl"))
        assert first != second, "tie-break is not order-dependent"
        assert {first, second} == {"t-aaa", "t-bbb"}

    def test_tie_head_of_open_dispatches_matches_the_resolver(self, tmp_path):
        """The resolver is the list's head — the invariant #904 created and the
        reason a tie-break regression would desync them rather than just
        reorder a display."""
        for first, second in (("t-bbb", "t-aaa"), ("t-aaa", "t-bbb")):
            dlog, rlog = self._tied(tmp_path, first, second)
            rows = dispatch_overdue.open_dispatches("w1", dlog, rlog)
            assert [t for _, _, t in rows] == [first, second]
            assert dispatch_overdue.open_task_id("w1", dlog, rlog) == rows[0][2]


class TestBotSlotShapeGate:
    """#1187 — right count, wrong order was silent; wrong count never was.

    --open, --open-task and SINGLE-BOT MODE each name one bot and take it
    first; --all/--orphans/--unassigned name none. Calling a bot-slot mode with
    the every-bot grammar keeps the arity valid, so a path lands in the bot
    slot, nothing matches, and the door prints nothing at rc 0 — the same
    output as a genuinely empty result. That is how a manager checking whether
    its closures had worked read a full backlog as all-clear.

    Only the two flagged doors are gated here; single-bot mode is the
    documented remaining gap and is pinned below.
    """

    def _logs(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return str(dlog), str(rlog)

    def _rows(self, tmp_path):
        return self._logs(tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")], [])

    # -- the filed defect: right count, wrong order --------------------------

    @pytest.mark.parametrize("mode", ["--open", "--open-task"])
    def test_wrong_order_is_refused_loudly_not_silently(
        self, tmp_path, monkeypatch, capsys, mode
    ):
        """THE regression this gate exists for. Both doors share the grammar,
        so both share the hazard; a fix on one only would leave the other."""
        dlog, rlog = self._rows(tmp_path)
        # --all's grammar (logs first), which is exactly 3 positionals — the
        # arity check passes and main() reaches the join with a path as `bot`.
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", mode, dlog, rlog, "1786700000"]
        )
        assert dispatch_overdue.main() == 2
        out = capsys.readouterr()
        assert out.out == ""  # never a partial result alongside a refusal
        assert "expects <bot_id> first" in out.err
        # The refusal must name the remedy, not merely reject: the operator's
        # actual error is not knowing the two grammars differ.
        assert "take the LOGS first" in out.err
        assert "take the BOT first" in out.err

    @pytest.mark.parametrize(
        "bad,label",
        [
            ("/abs/path/dispatch-log.jsonl", "a path"),
            ("relative/dir/name", "a path"),
            ("dispatch-log.jsonl", "a ledger file"),
            ("   ", "an empty bot id"),
        ],
    )
    def test_every_refused_shape_names_what_it_saw(
        self, tmp_path, monkeypatch, capsys, bad, label
    ):
        """A bare filename carries no separator, so the '/' test alone would
        miss the commonest form — invoking from the ledger's own directory."""
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", bad, dlog, rlog]
        )
        assert dispatch_overdue.main() == 2
        assert label in capsys.readouterr().err

    # -- positive control: the gate must not refuse a real bot id ------------

    @pytest.mark.parametrize("bot", ["w1", "gilfoyle", "bot-2", "Worker_3", "a.b"])
    def test_real_bot_ids_pass_the_gate(self, bot):
        """Without this, a gate that refused everything would pass the tests
        above. `a.b` guards the suffix test against becoming a bare dot test."""
        assert dispatch_overdue._not_a_bot_id(bot) is None

    def test_gate_is_inert_for_the_report_back_call_shape(
        self, tmp_path, monkeypatch, capsys
    ):
        """report-back.sh:99 passes its own $BOT first. Same rc, same stdout as
        before the gate — its fail-open contract is untouched."""
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open-task", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == "t-a\n"

    # -- dara's narrowing, pinned: arity was never the hole ------------------

    @pytest.mark.parametrize("mode", ["--open", "--open-task"])
    def test_wrong_arity_was_already_loud_and_stays_loud(
        self, tmp_path, monkeypatch, capsys, mode
    ):
        """Two positionals already returned rc 2 before this change. Pinned so
        the shape gate is never mistaken for the thing that made misuse loud —
        a reviewer measuring THIS shape reads the defect as unreproducible."""
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", mode, dlog, rlog])
        assert dispatch_overdue.main() == 2
        assert capsys.readouterr().out == ""

    # -- which modes have a bot slot, pinned in code rather than prose --------

    def test_single_bot_mode_is_BOT_first_like_the_gated_doors(
        self, tmp_path, monkeypatch, capsys
    ):
        """Executable because the prose got this backwards once (#1188 review):
        the docstring filed single-bot mode under "logs first", which would send
        the next reader hunting a dlog/rlog-swap detector instead of reusing
        _reject_bot_slot. argv[1] is the BOT — assert it, do not describe it."""
        # now=2000: past expected_by (1000) but inside the 24h age cap, or the
        # row expires and the mode looks bot-blind for an unrelated reason.
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "w1", dlog, rlog, "2000"]
        )
        assert dispatch_overdue.main() == 0
        assert "t-a" in capsys.readouterr().out  # argv[1] selected the bot

        # Control: a DIFFERENT name in the same slot returns nothing, so the
        # assertion above is about argv[1] and not about the row merely existing.
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "other", dlog, rlog, "2000"]
        )
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("mode", ["--all", "--orphans", "--unassigned"])
    def test_every_bot_modes_have_no_bot_slot_and_fail_LOUDLY(
        self, tmp_path, monkeypatch, mode
    ):
        """The other half of the same correction. These name no bot, so there is
        nothing for the gate to check — and mis-ordering them is already loud: a
        ledger path reaches the `now` slot and int() raises. Pins WHY they are
        excluded, so "ungated" is never re-read as "silently broken like #1187".
        """
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", mode, "w1", dlog, rlog])
        with pytest.raises(ValueError):
            dispatch_overdue.main()

    def test_single_bot_mode_is_the_ONE_remaining_silent_shape(
        self, tmp_path, monkeypatch, capsys
    ):
        """A tripwire on a disclosed gap, deliberately asserting today's WRONG
        behaviour: a path in single-bot mode's bot slot is still silent at rc 0.

        When someone wires _reject_bot_slot into single-bot mode this test FAILS
        — which is the point. It forces the module docstring, the CLAUDE.md row
        and this class to move in the same commit, instead of the doc drifting
        out of date the way it just did. Flip the assertion, do not delete it.
        """
        dlog, rlog = self._rows(tmp_path)
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py", dlog, rlog, "100000"])
        assert dispatch_overdue.main() == 0
        assert capsys.readouterr().out == ""


class TestOpenScopeDisclosure:
    """#1187 — an empty result that names its own scope cannot be misread.

    The shape gate above kills the instance; this kills the class. A plausible
    but WRONG bot — a typo, or a live name belonging to another fleet under the
    #526 host-global/per-fleet join — passes every shape test and still returns
    zero rows at rc 0. Coverage honesty applied to a read door: state the bound.
    """

    def _logs(self, tmp_path, dispatches, reports):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, reports)
        return str(dlog), str(rlog)

    def test_empty_result_names_the_bot_it_filtered_on(
        self, tmp_path, monkeypatch, capsys
    ):
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")], []
        )
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", "typo-bot", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == ""
        assert "typo-bot" in out.err and "0 open" in out.err

    def test_scope_is_stated_on_a_NON_empty_result_too(
        self, tmp_path, monkeypatch, capsys
    ):
        """Always, not only when empty. Under #526 a reader can be looking at
        another fleet's rows; 'which bot' is part of reading the answer, and a
        line that appears only on zero makes its own presence the signal."""
        dlog, rlog = self._logs(
            tmp_path, [_dispatch("w1", 100, 1000, task_id="t-a")], []
        )
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == "100 1000 t-a\n"
        assert "w1" in out.err and "1 open" in out.err

    def test_scope_line_NEVER_reaches_stdout(self, tmp_path, monkeypatch, capsys):
        """Load-bearing, not stylistic. report-back.sh:117 pipes this stdout
        through `awk {print $3}` to decide whether a supplied --task id is open,
        and only a NON-EMPTY open set may contradict the caller (#1146). On
        stdout the scope line becomes a phantom row whose field 3 is "->".

        NOTE THE SHAPE: the empty log here is not incidental. A bot that holds
        an open row still matches its own id — the phantom adds an entry rather
        than displacing the real one — so that case stays clean and would read
        as proof the placement is free. It bites with NOTHING open, where a
        valid id meets an open set of exactly ["->"] and a correct report is
        flagged `supplied-id-not-open`. Verified against the real report-back.sh
        with the scope line moved to stdout.

        Assert the whole stream, so no header can slip in beside the rows.
        """
        dlog, rlog = self._logs(tmp_path, [], [])
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == ""
        assert out.err != ""  # the disclosure went somewhere — just not stdout

    def test_open_task_stays_silent_on_stdout_and_stderr(
        self, tmp_path, monkeypatch, capsys
    ):
        """The resolver is machine-consumed and prints one id or nothing. It
        gets the shape gate but NOT the scope line: narration on every terminal
        report-back fleet-wide would be noise with no reader."""
        dlog, rlog = self._logs(tmp_path, [], [])
        monkeypatch.setattr(
            "sys.argv", ["dispatch-overdue.py", "--open-task", "w1", dlog, rlog]
        )
        assert dispatch_overdue.main() == 0
        out = capsys.readouterr()
        assert out.out == "" and out.err == ""


class TestSupersession:
    """A re-dispatch replaces an earlier task, so the older row is never answered.

    It then ages out and pages the manager about work that already shipped — six such
    rows in one night, five of them one thread re-dispatched five times. Asking workers
    to echo the right id was broadcast three times and did not hold, which is #835's
    own argument arriving: correct by default beats correct by discipline.

    THE BOUNDARY IS THE WHOLE DESIGN. Retiring too eagerly converts a false-page bug
    into a silently-dropped-task bug, which is strictly worse — nobody chases a task
    that looks finished. So retirement happens ONLY on an explicit declaration from the
    dispatcher, never on a pattern inferred afterwards. Inference was measured against
    the real ledger and rejected: of 189 closed rows, 14 had a later row close first and
    were still answered after, 3 unambiguously genuine (work answered 6-7h late).
    """

    NOW = 5000

    def _overdue(self, tmp_path, dispatches):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, dispatches)
        _write_jsonl(rlog, [])
        return dispatch_overdue.overdue_all(str(dlog), str(rlog), self.NOW)

    def test_an_explicitly_superseded_row_is_retired(self, tmp_path):
        """The stranded row goes quiet; the replacement stays accountable."""
        out = self._overdue(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-old"),
                _dispatch("w1", 200, 1100, task_id="t-200-new", supersedes="t-100-old"),
            ],
        )
        ids = [row[3] for row in out.get("w1", [])]
        assert "t-100-old" not in ids, "the superseded row still pages"
        assert "t-200-new" in ids, (
            "the REPLACEMENT was retired too — a re-dispatch must still be answered, "
            "or supersession becomes a way to silence live work"
        )

    def test_a_queued_fifo_dispatch_is_NOT_retired(self, tmp_path):
        """The property that keeps this from becoming a dropped-task generator.

        Same shape as above minus the declaration: two dispatches to one bot, nothing
        saying the second replaces the first. Both are owed, both must stay visible.
        This is what the oldest-first resolver exists to serve, and it is exactly the
        case timing-based inference got wrong.
        """
        out = self._overdue(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-a"),
                _dispatch("w1", 200, 1100, task_id="t-200-b"),
            ],
        )
        ids = sorted(row[3] for row in out.get("w1", []))
        assert ids == ["t-100-a", "t-200-b"], (
            f"a queued dispatch was retired without any declaration: {ids}"
        )

    def test_omitting_the_field_reproduces_prior_behaviour_exactly(self, tmp_path):
        """The inert-failure property: a forgotten flag costs a false page, never a
        dropped task. Rows with no `supersedes` key at all must behave as before."""
        rows = [_dispatch("w1", 100, 1000, task_id="t-100-a")]
        assert "supersedes" not in rows[0]
        assert [r[3] for r in self._overdue(tmp_path, rows).get("w1", [])] == [
            "t-100-a"
        ]

    def test_supersedes_is_scoped_by_bot(self, tmp_path):
        """One bot's dispatch must not retire another's row, however the id was typed —
        the same scoping `_terminal_reported_ids` carries for the same reason (#518)."""
        out = self._overdue(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-old"),
                _dispatch("w2", 200, 1100, task_id="t-200-new", supersedes="t-100-old"),
            ],
        )
        assert [r[3] for r in out.get("w1", [])] == ["t-100-old"], (
            "w2's declaration silenced w1's dispatch"
        )

    def test_an_empty_supersedes_retires_nothing(self, tmp_path):
        """The dispatcher always emits the key, so the common row carries
        `supersedes: ""`. Empty must be falsy, not a wildcard.

        HONEST LABEL: this documents an invariant it cannot currently violate. An empty
        value can only match a row whose `task_id` is also empty, and such a row takes
        the id-LESS branch above, so the pathological case is unreachable. Removing the
        falsy guard leaves all 48 tests green — the mutation run proved that. The guard
        stays as defence against a future caller that emits a placeholder rather than an
        empty string; the test is a statement of intent, not a detector.
        """
        out = self._overdue(
            tmp_path,
            [
                _dispatch("w1", 100, 1000, task_id="t-100-a", supersedes=""),
                _dispatch("w1", 200, 1100, task_id="t-200-b", supersedes=""),
            ],
        )
        assert sorted(r[3] for r in out.get("w1", [])) == ["t-100-a", "t-200-b"]

    def test_a_chain_of_re_dispatches_retires_every_link_but_the_last(self, tmp_path):
        """The observed shape: one thread re-dispatched five times leaves four strays.

        Each link names its immediate predecessor, so the chain collapses to the row
        that is actually live.
        """
        out = self._overdue(
            tmp_path,
            [
                _dispatch("w1", 100, 200, task_id="t-1"),
                _dispatch("w1", 300, 400, task_id="t-2", supersedes="t-1"),
                _dispatch("w1", 500, 600, task_id="t-3", supersedes="t-2"),
                _dispatch("w1", 700, 800, task_id="t-4", supersedes="t-3"),
                _dispatch("w1", 900, 1000, task_id="t-5", supersedes="t-4"),
            ],
        )
        assert [r[3] for r in out.get("w1", [])] == ["t-5"]


class TestUnassigned:
    """#1024 — the MIRROR of overdue: reported, then never re-dispatched.

    The join is the point, and so is what it refuses to look at. Semantics live
    in the unassigned_all docstring; these pin the behaviour that decides whether
    the check is useful or noise.
    """

    # Derived, never hardcoded: the two must agree exactly or every case below
    # silently becomes a different one — a stale dispatch epoch lands AFTER the
    # report and the row reads as re-tasked, which is a pass-shaped failure.
    T10 = "2026-05-27T10:00:00Z"
    E10 = int(_dt.datetime.fromisoformat(T10.replace("Z", "+00:00")).timestamp())

    def test_reported_and_never_retasked_is_flagged(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)])
        _write_jsonl(rlog, [_report("eng-1", self.T10, "completed")])
        res = dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300)
        assert "eng-1" in res
        reported_at, idle, _tid, status = res["eng-1"]
        assert reported_at == self.E10
        assert idle == 7300
        assert status == "completed"

    def test_retasked_after_reporting_is_not_flagged(self, tmp_path):
        """The positive control. Without it, a check that fired on every
        terminal report would satisfy every other test in this class."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(
            dlog,
            [
                _dispatch("eng-1", self.E10 - 3600, self.E10 - 3000),
                _dispatch("eng-1", self.E10 + 60, self.E10 + 660),
            ],
        )
        _write_jsonl(rlog, [_report("eng-1", self.T10, "completed")])
        assert (
            dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300) == {}
        )

    def test_five_stale_open_dispatches_do_not_mask_the_strand(self, tmp_path):
        """THE case the design exists for, and it is a real one.

        Six dispatches to one worker inside 2143s for a single evolving task;
        the worker answers only the last id, so five rows stay open afterwards.
        Replayed against the real ledgers at successive cutoffs, this function
        is silent through the busy stretch, raises at the 4797s gap that
        follows, and goes silent again when the next dispatch lands — with all
        five stale ids open throughout (vera, review of #1121).

        A predicate keyed on "has an open dispatch" reads those five as
        still-busy and never fires — the #1024 incident recurring inside its own
        watchdog. The fixture below is that shape, reduced.
        """
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(
            dlog,
            [
                _dispatch(
                    "eng-1",
                    self.E10 - 2100 + i * 300,
                    self.E10 - 1500 + i * 300,
                    task_id=f"t-{i}",
                )
                for i in range(6)
            ],
        )
        # Terminal report against the LAST id only; t-0..t-4 remain open.
        _write_jsonl(rlog, [_report("eng-1", self.T10, "completed", task_id="t-5")])
        res = dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300)
        assert "eng-1" in res, "five stale open rows masked a genuine strand"

    def test_progress_as_newest_report_is_not_flagged(self, tmp_path):
        """Still working, or stalled mid-task — the stall is overdue's to report."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)])
        _write_jsonl(
            rlog,
            [
                _report("eng-1", "2026-05-27T09:00:00Z", "completed"),
                _report("eng-1", self.T10, "progress"),
            ],
        )
        assert (
            dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300) == {}
        )

    def test_a_bot_that_never_reported_is_not_flagged(self, tmp_path):
        """No terminal report means nothing came back, so there is nothing to be
        unassigned FROM. That case belongs to overdue_all, not here."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)])
        _write_jsonl(rlog, [])
        assert (
            dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300) == {}
        )

    def test_threshold_filters_when_asked(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-1", self.E10 - 3600, self.E10 - 3000)])
        _write_jsonl(rlog, [_report("eng-1", self.T10, "completed")])
        now = self.E10 + 100
        assert dispatch_overdue.unassigned_all(str(dlog), str(rlog), now, 7200) == {}
        # ...and defaults to reporting everything, because fleet-pulse applies
        # the threshold per bot and one scan must serve differently-tuned bots.
        assert "eng-1" in dispatch_overdue.unassigned_all(str(dlog), str(rlog), now)

    def test_never_dispatched_bot_that_reported_is_flagged(self, tmp_path):
        """No dispatch row at all is still an unassigned worker, not an error."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [])
        _write_jsonl(rlog, [_report("eng-1", self.T10, "blocked")])
        res = dispatch_overdue.unassigned_all(str(dlog), str(rlog), self.E10 + 7300)
        assert res["eng-1"][3] == "blocked"

    def test_bot_scoping(self, tmp_path):
        """One bot's dispatch must not clear another's strand."""
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("eng-2", self.E10 + 60, self.E10 + 660)])
        _write_jsonl(rlog, [_report("eng-1", self.T10, "completed")])
        assert "eng-1" in dispatch_overdue.unassigned_all(
            str(dlog), str(rlog), self.E10 + 7300
        )

    def test_missing_ledgers_are_empty_not_fatal(self, tmp_path):
        assert (
            dispatch_overdue.unassigned_all(
                str(tmp_path / "nope.jsonl"), str(tmp_path / "nada.jsonl"), self.E10
            )
            == {}
        )


class TestOrphansRefusesWhenItCannotLook:
    """#1014, and the same defect as #1216 on a sibling command.

    Orphan-ness is decided by comparing a dispatch against
    ``<bots_dir>/<bot>/data/.spawn``. Without a readable bots dir there is nothing
    to compare, so the honest answer is UNKNOWN — but the mode printed an empty
    set at rc 0, which is byte-identical to "no work was lost to a restart".
    Measured on the reporting host before the fix: no ``--bots-dir``, a real one
    with no orphans, and a ``--bots-dir`` naming a path that does not exist ALL
    returned 0 bytes at rc 0 against a 295-row dispatch log. Three states, one
    output, and the collapsed one reads as good news.
    """

    NOW = 3000

    def _logs(self, tmp_path):
        dlog, rlog = tmp_path / "d.jsonl", tmp_path / "r.jsonl"
        _write_jsonl(dlog, [_dispatch("w1", 100, 1000, task_id="t-1")])
        _write_jsonl(rlog, [])
        return str(dlog), str(rlog)

    def _main(self, argv, monkeypatch):
        monkeypatch.setattr("sys.argv", ["dispatch-overdue.py"] + argv)
        return dispatch_overdue.main()

    def test_no_bots_dir_refuses_at_rc_three(self, tmp_path, monkeypatch, capsys):
        dlog, rlog = self._logs(tmp_path)
        rc = self._main(["--orphans", dlog, rlog, str(self.NOW)], monkeypatch)
        assert rc == 3
        assert "cannot determine orphans without --bots-dir" in capsys.readouterr().err

    def test_an_unreadable_bots_dir_refuses_too(self, tmp_path, monkeypatch, capsys):
        """The second silent state, and the one a real caller reaches: a
        --bots-dir that resolves to nothing (a moved fleet, a wrong root) looked
        identical to a healthy fleet with no orphans."""
        dlog, rlog = self._logs(tmp_path)
        rc = self._main(
            [
                "--orphans",
                dlog,
                rlog,
                str(self.NOW),
                "--bots-dir",
                str(tmp_path / "no-such-dir"),
            ],
            monkeypatch,
        )
        assert rc == 3
        assert "cannot read the bots dir" in capsys.readouterr().err

    def test_a_real_bots_dir_with_no_orphans_still_answers_empty_at_rc_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        """THE control that makes the two above mean something. Presence, not
        emptiness, is the line — a fleet that genuinely lost nothing must still
        get the true answer, or the fix has traded a false all-clear for a
        refusal that fires on healthy fleets."""
        dlog, rlog = self._logs(tmp_path)
        bots = tmp_path / "bots" / "w1" / "data"
        bots.mkdir(parents=True)
        rc = self._main(
            [
                "--orphans",
                dlog,
                rlog,
                str(self.NOW),
                "--bots-dir",
                str(tmp_path / "bots"),
            ],
            monkeypatch,
        )
        cap = capsys.readouterr()
        assert rc == 0
        assert cap.out == ""

    def test_a_real_orphan_is_still_listed(self, tmp_path, monkeypatch, capsys):
        """The positive control on the other side: the mode must still find what
        it exists to find. Without this, every assertion above would hold on a
        command that had stopped classifying anything at all."""
        dlog, rlog = self._logs(tmp_path)
        data = tmp_path / "bots" / "w1" / "data"
        data.mkdir(parents=True)
        spawn = data / ".spawn"
        spawn.write_text("")
        os.utime(spawn, (500, 500))  # respawned AFTER the dispatch at 100
        rc = self._main(
            [
                "--orphans",
                dlog,
                rlog,
                str(self.NOW),
                "--bots-dir",
                str(tmp_path / "bots"),
            ],
            monkeypatch,
        )
        cap = capsys.readouterr()
        assert rc == 0
        assert "t-1" in cap.out

    def test_rc_three_is_distinct_from_the_usage_code(self, tmp_path, monkeypatch):
        """rc 2 means "you called me wrong"; rc 3 means "I cannot answer that".
        Collapsing them would re-create this very bug one level up — a caller
        could no longer tell a typo from an unreachable instrument."""
        dlog, rlog = self._logs(tmp_path)
        cannot_answer = self._main(["--orphans", dlog, rlog], monkeypatch)
        malformed = self._main(["--orphans", dlog], monkeypatch)
        assert cannot_answer == 3
        assert malformed == 2

    def test_all_mode_is_untouched_by_the_orphan_refusal(
        self, tmp_path, monkeypatch, capsys
    ):
        """--all takes the same positional grammar and legitimately runs without
        a bots dir (it just cannot split orphans out). Gating it would break the
        watchdog's primary call."""
        dlog, rlog = self._logs(tmp_path)
        rc = self._main(["--all", dlog, rlog, str(self.NOW)], monkeypatch)
        cap = capsys.readouterr()
        assert rc == 0
        assert "t-1" in cap.out

    def test_orphaned_all_keeps_its_contract_exactly(self, tmp_path):
        """The refusal lives in the CLI mode, NOT the join. brief.py imports
        orphaned_all directly and labels this gap its own way, so changing the
        function would have broken a caller that already had it right."""
        dlog, rlog = self._logs(tmp_path)
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW) == {}
        assert dispatch_overdue.orphaned_all(dlog, rlog, self.NOW, bots_dir=None) == {}
