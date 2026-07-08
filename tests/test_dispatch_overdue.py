"""Unit tests for lib/dispatch-overdue.py — the dispatch watchdog matcher.

A dispatch is overdue when now > expected_by AND no terminal report
(completed|failed|blocked) for the same bot with ts >= dispatched_at exists.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "dispatch_overdue", REPO_ROOT / "lib" / "dispatch-overdue.py"
)
dispatch_overdue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dispatch_overdue)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _dispatch(bot, dispatched_at, expected_by):
    return {
        "ts": "2026-05-27T10:00:00Z",
        "manager": "lead",
        "bot": bot,
        "task": "do x",
        "dispatched_at": dispatched_at,
        "expected_by": expected_by,
    }


def _report(bot, ts, status="completed"):
    return {
        "ts": ts,
        "bot": bot,
        "status": status,
        "summary": "done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }


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
