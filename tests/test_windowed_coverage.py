"""Every windowed read door states how much of its window the record covers (#1658).

The defect these pin is not a wrong number — it is a right number rendered as
though its window were covered. Measured on this estate while the issue was
filed: `report-back --since 14d` returned 325 rows over a record **2.2 days**
old, and printed `325 event(s)` with nothing naming the denominator.

**These tests are about the line being DERIVED, not about it existing.** A
constant string would satisfy "a coverage line is printed" and assert nothing,
so every case below moves the record or the window and requires the line to
move with it.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _readers():
    spec = importlib.util.spec_from_file_location(
        "plane_readers", REPO / "lib" / "plane-readers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PR = _readers()


def _plane(tmp_path: Path, rows: list[tuple[str, str]]) -> sqlite3.Connection:
    """A throwaway ingest_ledger holding (family, ingested_at)."""
    db = tmp_path / "plane.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ingest_ledger (ingest_seq INTEGER PRIMARY KEY,"
              " event_id TEXT, family TEXT, ingested_at TEXT)")
    c.executemany("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                  " VALUES (?, ?, ?)",
                  [(f"e{i}", fam, at) for i, (fam, at) in enumerate(rows)])
    c.commit()
    return c


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestCoverageIsDerived:
    def test_an_under_covered_window_names_the_shortfall(self, tmp_path):
        conn = _plane(tmp_path, [("system", _ago(2.2)), ("system", _ago(0))])
        first, last, rows = PR.coverage(conn)
        line = PR.coverage_line(first, last, rows, 14 * 86400)
        assert "of the 14.0d window" in line
        assert "2.2d" in line, line
        assert "16%" in line or "15%" in line or "17%" in line, line

    def test_a_covered_window_says_so_rather_than_going_silent(self, tmp_path):
        """The line prints either way ON PURPOSE. A statement that appears only
        when something is wrong makes its ABSENCE carry the all-clear, which is
        the shape this whole issue is about."""
        conn = _plane(tmp_path, [("system", _ago(9)), ("system", _ago(0))])
        first, last, rows = PR.coverage(conn)
        line = PR.coverage_line(first, last, rows, 7 * 86400)
        assert "full 7.0d window" in line, line

    def test_the_line_MOVES_with_the_record(self, tmp_path):
        """The control against a constant string: same window, two record ages,
        two different lines. A hardcoded sentence passes every other test here
        and fails this one."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        old = _plane(tmp_path / "a", [("system", _ago(6)), ("system", _ago(0))])
        young = _plane(tmp_path / "b", [("system", _ago(0.5)), ("system", _ago(0))])
        la = PR.coverage_line(*PR.coverage(old), 30 * 86400)
        lb = PR.coverage_line(*PR.coverage(young), 30 * 86400)
        assert la != lb, (la, lb)

    def test_the_line_MOVES_with_the_window(self, tmp_path):
        conn = _plane(tmp_path, [("system", _ago(3)), ("system", _ago(0))])
        short = PR.coverage_line(*PR.coverage(conn), 86400)
        long_ = PR.coverage_line(*PR.coverage(conn), 30 * 86400)
        assert short != long_
        assert "full" in short and "of the 30.0d window" in long_

    def test_coverage_is_per_family_because_families_differ(self, tmp_path):
        """Measured on the live estate: `system` reached back 2.35d while
        `workstream` reached 1.32d. One global instant would overstate coverage
        for one door and understate it for another."""
        conn = _plane(tmp_path, [
            ("system", _ago(5)), ("system", _ago(0)),
            ("workstream", _ago(1)), ("workstream", _ago(0)),
        ])
        sys_first, _, _ = PR.coverage(conn, "system")
        ws_first, _, _ = PR.coverage(conn, "workstream")
        assert sys_first < ws_first
        assert PR.coverage(conn, "system")[2] == 2
        assert PR.coverage(conn, "workstream")[2] == 2
        assert PR.coverage(conn)[2] == 4          # family=None is the whole record

    def test_an_empty_plane_is_not_a_covered_window(self, tmp_path):
        """Nothing recorded yet is a legitimate state and must not render as
        100% coverage — the fail-open direction this issue is about."""
        conn = _plane(tmp_path, [])
        line = PR.coverage_line(*PR.coverage(conn), 7 * 86400)
        assert "no rows" in line
        assert "full" not in line

    def test_no_window_states_the_record_rather_than_a_fraction(self, tmp_path):
        """A door with no `--since` (events) has no window to fall short of, so
        it names the record's extent instead of inventing a percentage."""
        conn = _plane(tmp_path, [("system", _ago(3)), ("system", _ago(0))])
        line = PR.coverage_line(*PR.coverage(conn), None)
        assert "record starts" in line and "%" not in line


class TestTheDoorsPrintIt:
    @pytest.mark.parametrize("window_s,needle", [
        (14 * 86400, "of the 14.0d window"),
        (86400, "full 24h window"),
    ])
    def test_the_shared_helper_is_what_the_doors_call(self, tmp_path, window_s, needle):
        """`commands/core._coverage_line` is the one wording for report-back and
        uptime; events has its own entry point but the same two reader calls.
        Pinned so a door cannot quietly grow a second phrasing."""
        from claudlobby.commands.core import _coverage_line

        conn = _plane(tmp_path, [("system", _ago(2.2)), ("system", _ago(0))])

        class _Session:
            pr = PR

        sess = _Session()
        sess.conn = conn
        assert needle in _coverage_line(sess, window_s)

    def test_an_install_whose_readers_predate_this_says_so(self, tmp_path):
        """The degradation path, and it is NOT dead code: it fired live the
        first time the door ran against a shared install that had not been
        updated. A door must not lose its answer because the sentence about
        that answer could not be built."""
        from claudlobby.commands.core import _coverage_line

        class _Old:
            class pr:                      # readers without coverage()
                pass

        assert "predate" in _coverage_line(_Old(), 86400)
