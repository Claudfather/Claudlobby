"""Tests for claudlobby report-back subcommand (issue #242)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from claudlobby.__main__ import main


@pytest.fixture
def ledger_root(tmp_path):
    """Create a tmp_path with runtime/fleet/report-back.jsonl for root mode."""
    fleet_dir = tmp_path / "runtime" / "fleet"
    fleet_dir.mkdir(parents=True)

    (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()

    now = datetime.now(timezone.utc)
    entries = [
        {
            "ts": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-1",
            "status": "completed",
            "summary": "Fixed auth",
            "pr_url": "https://github.com/org/repo/pull/1",
            "issues": "",
            "skill": "",
        },
        {
            "ts": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-2",
            "status": "blocked",
            "summary": "Missing token",
            "pr_url": "",
            "issues": "",
            "skill": "",
        },
        {
            "ts": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": "eng-1",
            "status": "progress",
            "summary": "Working on tests",
            "pr_url": "",
            "issues": "",
            "skill": "prs",
        },
    ]
    ledger = fleet_dir / "report-back.jsonl"
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    return tmp_path


def test_report_back_all(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 event(s)" in out
    assert "eng-1" in out
    assert "eng-2" in out


def test_report_back_filter_bot(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--bot", "eng-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 event(s)" in out
    assert "eng-2" not in out


def test_report_back_filter_status(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--status", "blocked"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Missing token" in out


def test_report_back_since(ledger_root, capsys):
    # Use 59m instead of 1h to avoid boundary race — the 1h-ago entry's
    # timestamp can land inside/outside the window depending on sub-second timing.
    rc = main(["--root", str(ledger_root), "report-back", "--since", "59m"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Working on tests" in out


def test_report_back_json_output(ledger_root, capsys):
    rc = main(["--root", str(ledger_root), "report-back", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.strip().split("\n") if l]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "bot" in parsed
        assert "status" in parsed


def test_report_back_no_ledger(tmp_path, capsys):
    """A missing ledger must not raise — and since #1216 must not read as empty.

    This test previously asserted ``rc == 0``, which is the defect written down
    as a contract: it is exactly what made "I could not find a ledger"
    indistinguishable from "no rows matched", and it is why a manager routed
    restart decisions off an empty result for a day. The surviving requirement is
    the one it was really protecting — the command DEGRADES rather than crashing.
    """
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()
    rc = main(["--root", str(tmp_path), "report-back"])
    assert rc == 1
    assert "cannot read the report-back ledger" in capsys.readouterr().out


def test_report_back_overlay(tmp_path, capsys):
    fleet_dir = tmp_path / "local" / "myfleet"
    runtime = fleet_dir / "runtime"
    runtime.mkdir(parents=True)
    (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: myfleet\n  bots: {}\n")
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()

    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bot": "worker",
        "status": "completed",
        "summary": "Done",
        "pr_url": "",
        "issues": "",
        "skill": "",
    }
    (runtime / "report-back.jsonl").write_text(json.dumps(entry) + "\n")

    rc = main(["--root", str(tmp_path), "--fleet", "myfleet", "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out


class TestUnreachableIsNotEmpty:
    """#1216. An unreachable ledger and a ledger with no matching rows used to be
    byte-identical: an INFO line on stderr, rc 0, zero bytes on stdout. The
    composed manager guidance routed worker RESTARTS on that output, without
    --fleet, so it resolved the root tier and read as "this worker is fresh".
    """

    def _fleet_with_rows(self, tmp_path, n=3):
        fleet_dir = tmp_path / "local" / "myfleet"
        runtime = fleet_dir / "runtime"
        runtime.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: myfleet\n  bots: {}\n")
        # exist_ok: two tests below build a root-mode tree first and then an
        # overlay in the SAME tmp_path, on purpose — comparing the two states
        # needs both to exist under one root.
        (tmp_path / "library").mkdir(exist_ok=True)
        (tmp_path / "lib").mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        rows = [
            {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bot": "worker",
                "status": "completed",
                "summary": f"Done {i}",
                "pr_url": "",
                "issues": "",
                "skill": "",
            }
            for i in range(n)
        ]
        (runtime / "report-back.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        return tmp_path

    def test_an_absent_ledger_exits_nonzero_and_says_so_on_stdout(self, tmp_path):
        """Both halves are load-bearing and cover different readers: rc is
        invisible to a human at a terminal, and a stdout line is invisible to a
        script. Only the pair makes the state distinguishable to both."""
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        rc = main(["--root", str(tmp_path), "report-back"])
        assert rc == 1

    def test_the_absent_message_names_the_fleet_flag_in_root_mode(
        self, tmp_path, capsys
    ):
        """#1216's real cause was a resolved TIER, not a deleted file. A message
        saying only "not found" sends the reader to create a ledger that already
        exists one directory over."""
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        main(["--root", str(tmp_path), "report-back"])
        out = capsys.readouterr().out
        assert "cannot read the report-back ledger" in out
        assert "--fleet" in out

    def test_the_fleet_flag_remedy_is_omitted_in_overlay_mode(self, tmp_path, capsys):
        """--fleet was already passed, so naming it would tell the reader to
        re-run the command they just ran. A remedy that does not apply is worse
        than none: it costs a cycle and teaches people to skip the line."""
        fleet_dir = tmp_path / "local" / "myfleet"
        (fleet_dir / "runtime").mkdir(parents=True)
        (fleet_dir / "fleet.yaml").write_text("fleet:\n  name: myfleet\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        rc = main(["--root", str(tmp_path), "--fleet", "myfleet", "report-back"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "cannot read the report-back ledger" in out
        assert "--fleet" not in out

    def test_a_present_ledger_with_no_matching_rows_stays_rc_zero(self, tmp_path):
        """The line is PRESENCE, not emptiness. A filter that excludes everything
        is a true answer and must not be reported as a broken instrument, or the
        fix trades a false all-clear for a false alarm."""
        root = self._fleet_with_rows(tmp_path)
        rc = main(
            [
                "--root",
                str(root),
                "--fleet",
                "myfleet",
                "report-back",
                "--bot",
                "nobody-by-that-name",
            ]
        )
        assert rc == 0

    def test_emptiness_states_the_row_count_it_read(self, tmp_path, capsys):
        """This is what makes the two states distinguishable on STDOUT ALONE,
        without inspecting rc: "0 matched of 3 rows in <path>" cannot be confused
        with "cannot read <path>"."""
        root = self._fleet_with_rows(tmp_path, n=3)
        main(
            [
                "--root",
                str(root),
                "--fleet",
                "myfleet",
                "report-back",
                "--bot",
                "nobody-by-that-name",
            ]
        )
        out = capsys.readouterr().out
        assert "0 event(s) matched" in out
        assert "3 row(s)" in out
        assert "cannot read" not in out

    def test_the_two_states_differ_on_stdout_and_on_rc(self, tmp_path, capsys):
        """The regression this whole class exists to prevent, asserted as the
        DIFFERENCE rather than as two independent facts — the defect was never
        either output on its own, it was that they matched."""
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()
        rc_absent = main(["--root", str(tmp_path), "report-back"])
        out_absent = capsys.readouterr().out

        root = self._fleet_with_rows(tmp_path)
        rc_empty = main(
            [
                "--root",
                str(root),
                "--fleet",
                "myfleet",
                "report-back",
                "--bot",
                "nobody",
            ]
        )
        out_empty = capsys.readouterr().out

        assert out_absent != out_empty
        assert rc_absent != rc_empty

    def test_json_mode_keeps_stdout_machine_clean_in_both_states(
        self, tmp_path, capsys
    ):
        """--json makes stdout MACHINE-facing, so the disclosure moves to stderr.
        A prose line in a JSONL stream is the phantom-row defect this fix exists
        to prevent, re-created by the fix — rc still carries the refusal."""
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
        (tmp_path / "library").mkdir()
        (tmp_path / "lib").mkdir()

        rc = main(["--root", str(tmp_path), "report-back", "--json"])
        cap = capsys.readouterr()
        assert rc == 1
        assert cap.out == ""
        assert "cannot read the report-back ledger" in cap.err

        root = self._fleet_with_rows(tmp_path)
        rc2 = main(
            [
                "--root",
                str(root),
                "--fleet",
                "myfleet",
                "report-back",
                "--json",
                "--bot",
                "nobody",
            ]
        )
        cap2 = capsys.readouterr()
        assert rc2 == 0
        assert cap2.out == ""

    def test_a_ledger_with_rows_is_unaffected(self, tmp_path, capsys):
        """The positive control. Without it every assertion above would hold on a
        command that had simply stopped returning rows at all."""
        root = self._fleet_with_rows(tmp_path, n=3)
        rc = main(["--root", str(root), "--fleet", "myfleet", "report-back"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "3 event(s)" in out
        assert "cannot read" not in out
