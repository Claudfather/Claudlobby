"""Unit tests for lib/dispatch-overdue.py — the dispatch watchdog matcher,
including the P4 task-id join matrix (semantics: overdue_all docstring)."""

from __future__ import annotations

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
        assert out.get("worker-a"), (
            "wrong-bot report must not close the dispatch"
        )

    def test_wrong_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [_report("w1", "1970-01-01T00:05:00Z", task_id="t-777-dddd")],
        )
        assert out.get("w1")

    def test_progress_report_with_id_does_not_close(self, tmp_path):
        out = self._run(
            tmp_path,
            [_dispatch("w1", 100, 1000, task_id="t-100-aaaa")],
            [
                _report(
                    "w1", "1970-01-01T00:05:00Z", status="progress",
                    task_id="t-100-aaaa",
                )
            ],
        )
        assert out.get("w1"), "non-terminal progress reuses the id, never closes"


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
