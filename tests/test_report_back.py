"""Tests for `claudlobby report-back` (issue #242) — the plane is the ONLY
source (F18 closure, R2b): the rows are the fleet's report communications
with the task event or the `report_status` marker that names each status,
read through `lib/plane-readers.py::report_rows`. No ledger probe, no
retirement fact, no file. Unreachable is not empty: a plane that cannot
answer (no db, or one that holds no bot of the fleet) REFUSES at rc 3 with
the note on stderr — never an empty table, which is exactly what let a
manager read "this worker is fresh" off an unreachable ledger for a day
(#1216).

Deleted with the ledger: test_report_back_no_ledger, and in
TestUnreachableIsNotEmpty the fleet-flag-remedy pair
(test_the_absent_message_names_the_fleet_flag_in_root_mode,
test_the_fleet_flag_remedy_is_omitted_in_overlay_mode) and
test_an_unreadable_ledger_is_not_told_to_pass_fleet — the tier remedy answered
"the file is one directory over"; the plane is per host and the fleet name
selects the rows, so a wrong fleet reads "holds no bot of fleet" instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudlobby.__main__ import main

REPO = Path(__file__).resolve().parent.parent
_SEQ = [0]


def _root(tmp_path: Path, *, overlay: str | None = None) -> Path:
    """A root-mode tree (fleet `test`) or an overlay fleet, with the install's
    real lib/ so the command loads the stdlib plane readers."""
    (tmp_path / "library").mkdir(exist_ok=True)
    if not (tmp_path / "lib").exists():
        (tmp_path / "lib").symlink_to(REPO / "lib")
    cap = tmp_path / "state" / "plane" / "capture.json"        # full capture: the bodies (summaries) are kept
    if not cap.exists():
        cap.parent.mkdir(parents=True, exist_ok=True)
        cap.write_text('{"*": "full"}')
    if overlay:
        fleet_dir = tmp_path / "local" / overlay
        (fleet_dir / "runtime").mkdir(parents=True, exist_ok=True)
        (fleet_dir / "fleet.yaml").write_text(f"fleet:\n  name: {overlay}\n  bots: {{}}\n")
    else:
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: test\n  bots: {}\n")
    return tmp_path


def _land(root: Path, fleet: str, bot: str, ts: str, status: str, summary: str) -> None:
    """One report as the report door lands it: the report communication (the
    wire line as its body) and the `report_status` marker naming the status."""
    from claudlobby.plane.emit_api import emit_batch
    _SEQ[0] += 1
    msg = f"msg_{'d' * 24}{_SEQ[0]:0>8x}"
    ref = f"report-back:{msg}"
    out = emit_batch(root, [
        {"event_type": "communication", "emitter": "report-back", "fleet": fleet,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"msg_id": msg, "sender": f"bot:{fleet}/{bot}", "recipient": f"bot:{fleet}/lead",
                     "recipient_raw": "lead", "message_class": "report",
                     "body": f"[BOTREPORT] {bot} | {status} | {summary}"}},
        {"event_type": "system", "emitter": "report-back", "fleet": fleet,
         "source_ref": ref, "occurred_at": ts,
         "payload": {"event": "report_status", "subject_kind": "actor", "subject": f"bot:{fleet}/{bot}",
                     "data": {"status": status, "msg_id": msg}}},
    ])
    assert all(o.status == "committed" for o in out), out


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def plane_root(tmp_path):
    """Root mode, fleet `test`, three reports on the plane."""
    root = _root(tmp_path)
    now = datetime.now(timezone.utc)
    _land(root, "test", "eng-1", _stamp(now - timedelta(hours=2)), "completed", "Fixed auth")
    _land(root, "test", "eng-2", _stamp(now - timedelta(hours=1)), "blocked", "Missing token")
    _land(root, "test", "eng-1", _stamp(now - timedelta(minutes=30)), "progress", "Working on tests")
    return root


def test_report_back_all(plane_root, capsys):
    rc = main(["--root", str(plane_root), "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 event(s)" in out
    assert "eng-1" in out
    assert "eng-2" in out


def test_report_back_filter_bot(plane_root, capsys):
    rc = main(["--root", str(plane_root), "report-back", "--bot", "eng-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 event(s)" in out
    assert "eng-2" not in out


def test_report_back_filter_status(plane_root, capsys):
    rc = main(["--root", str(plane_root), "report-back", "--status", "blocked"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Missing token" in out


def test_report_back_since(plane_root, capsys):
    # Use 59m instead of 1h to avoid boundary race — the 1h-ago entry's
    # timestamp can land inside/outside the window depending on sub-second timing.
    rc = main(["--root", str(plane_root), "report-back", "--since", "59m"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out
    assert "Working on tests" in out


def test_report_back_json_output(plane_root, capsys):
    rc = main(["--root", str(plane_root), "report-back", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.strip().split("\n") if l]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "bot" in parsed
        assert "status" in parsed
        assert not [k for k in parsed if k.startswith("_")]      # the legacy row, private keys stripped


def test_report_back_no_plane_refuses(tmp_path, capsys):
    """No plane under the root must not raise — and must not read as empty:
    the plane is the only source, so the command REFUSES (rc 3) and says so.
    """
    root = _root(tmp_path)
    rc = main(["--root", str(root), "report-back"])
    cap = capsys.readouterr()
    assert rc == 3
    assert cap.out == "" and "UNREACHABLE" in cap.err


def test_report_back_overlay(tmp_path, capsys):
    root = _root(tmp_path, overlay="myfleet")
    _land(root, "myfleet", "worker", _stamp(datetime.now(timezone.utc)), "completed", "Done")

    rc = main(["--root", str(root), "--fleet", "myfleet", "report-back"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 event(s)" in out


class TestUnreachableIsNotEmpty:
    """#1216. An unreachable source and a source with no matching rows used to
    be byte-identical: an INFO line on stderr, rc 0, zero bytes on stdout. The
    composed manager guidance routed worker RESTARTS on that output, so it read
    as "this worker is fresh". On the plane the rule is the same: refusal rides
    rc AND text, emptiness states the row count it read.
    """

    def _fleet_with_rows(self, tmp_path, n=3, fleet="myfleet"):
        root = _root(tmp_path, overlay="myfleet")
        now = _stamp(datetime.now(timezone.utc))
        for i in range(n):
            _land(root, fleet, "worker", now, "completed", f"Done {i}")
        return root

    def test_an_absent_plane_exits_three_and_says_so(self, tmp_path, capsys):
        """Both halves are load-bearing and cover different readers: rc is
        invisible to a human at a terminal, and a line is invisible to a
        script. Only the pair makes the state distinguishable to both."""
        root = _root(tmp_path)
        rc = main(["--root", str(root), "report-back"])
        cap = capsys.readouterr()
        assert rc == 3
        assert "UNREACHABLE" in cap.err and "plane" in cap.err

    def test_a_plane_that_never_saw_the_fleet_is_unreachable_not_empty(self, tmp_path, capsys):
        """A schema-valid plane holding no bot of the named fleet is a wrong
        root or a fleet it never saw: refused, never "0 event(s)"."""
        root = self._fleet_with_rows(tmp_path, fleet="another-fleet")
        rc = main(["--root", str(root), "--fleet", "myfleet", "report-back"])
        cap = capsys.readouterr()
        assert rc == 3 and cap.out == ""
        assert "holds no bot of fleet" in cap.err

    def test_a_present_plane_with_no_matching_rows_stays_rc_zero(self, tmp_path):
        """The line is PRESENCE, not emptiness. A filter that excludes everything
        is a true answer and must not be reported as a broken instrument, or the
        fix trades a false all-clear for a false alarm."""
        root = self._fleet_with_rows(tmp_path)
        rc = main(["--root", str(root), "--fleet", "myfleet", "report-back",
                   "--bot", "nobody-by-that-name"])
        assert rc == 0

    def test_emptiness_states_the_row_count_it_read(self, tmp_path, capsys):
        """This is what makes the two states distinguishable on STDOUT ALONE,
        without inspecting rc: "0 matched of 3 rows from the plane" cannot be
        confused with "UNREACHABLE"."""
        root = self._fleet_with_rows(tmp_path, n=3)
        main(["--root", str(root), "--fleet", "myfleet", "report-back",
              "--bot", "nobody-by-that-name"])
        out = capsys.readouterr().out
        assert "0 event(s) matched" in out
        assert "3 row(s)" in out and "the plane" in out
        assert "UNREACHABLE" not in out

    def test_the_two_states_differ_on_stdout_and_on_rc(self, tmp_path, capsys):
        """The regression this whole class exists to prevent, asserted as the
        DIFFERENCE rather than as two independent facts — the defect was never
        either output on its own, it was that they matched."""
        root = _root(tmp_path)
        rc_absent = main(["--root", str(root), "report-back"])
        out_absent = capsys.readouterr().out

        root = self._fleet_with_rows(tmp_path)
        rc_empty = main(["--root", str(root), "--fleet", "myfleet", "report-back", "--bot", "nobody"])
        out_empty = capsys.readouterr().out

        assert out_absent != out_empty
        assert rc_absent != rc_empty

    def test_json_mode_keeps_stdout_machine_clean_in_both_states(self, tmp_path, capsys):
        """--json makes stdout MACHINE-facing, so the disclosure rides stderr.
        A prose line in a JSONL stream is the phantom-row defect this fix exists
        to prevent, re-created by the fix — rc still carries the refusal."""
        root = _root(tmp_path)
        rc = main(["--root", str(root), "report-back", "--json"])
        cap = capsys.readouterr()
        assert rc == 3
        assert cap.out == ""
        assert "UNREACHABLE" in cap.err

        root = self._fleet_with_rows(tmp_path)
        rc2 = main(["--root", str(root), "--fleet", "myfleet", "report-back", "--json", "--bot", "nobody"])
        cap2 = capsys.readouterr()
        assert rc2 == 0
        assert cap2.out == ""

    def test_a_plane_with_rows_is_unaffected(self, tmp_path, capsys):
        """The positive control. Without it every assertion above would hold on a
        command that had simply stopped returning rows at all."""
        root = self._fleet_with_rows(tmp_path, n=3)
        rc = main(["--root", str(root), "--fleet", "myfleet", "report-back"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "3 event(s)" in out
        assert "UNREACHABLE" not in out
