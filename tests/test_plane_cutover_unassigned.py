"""Cutover chunk 7a — the idle-worker check (`--unassigned`) gets a plane path,
its own shadow reader and flag, so the hard flip leaves no matcher reader on
the frozen ledger."""

from __future__ import annotations

from datetime import datetime, timezone

from claudlobby.plane import cutover as cut
from claudlobby.plane import shadow as sh
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import ro as _ro
from tests.test_plane_cutover_flip import F, NOW_EPOCH, _cli, _declare, _ledgers, _matcher, _scene
from tests.test_plane_cutover_parity import _rrow, _write
from tests.test_plane_shadow import _complete, _report


def _finish(root, paths, r, *, wi, asg, task_id, ts, bot="w1"):
    _complete(root, wi, asg, ts, task_id, r, bot=bot)
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)


def test_unassigned_answers_the_same_from_both_sources(tmp_path):
    """w1 finishes t-2 and is never re-tasked: idle on both sides; w2 holds an
    open task and reported nothing: on neither."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    done = "2026-09-02T13:00:00Z"
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts=done)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 == plane.returncode, (jsonl.stderr, plane.stderr)
    assert jsonl.stdout == plane.stdout and jsonl.stdout.startswith("w1 ") and "w2" not in jsonl.stdout
    _, rts, idle, tid, status = jsonl.stdout.split()
    assert tid == "t-2-bbbb" and status == "completed" and int(idle) == NOW_EPOCH - int(rts)


def test_a_dispatch_after_the_report_clears_it_on_both_sides(tmp_path):
    from tests.test_plane_cutover_parity import _drow, _live_dispatch
    from tests.test_plane_shadow import _epoch
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts="2026-09-02T13:00:00Z")
    ts = "2026-09-02T14:00:00Z"                             # re-tasked after reporting
    _live_dispatch(root, "4", "t-4-dddd", ts=ts, expected_by="2026-09-03T00:00:00+00:00")
    row = _drow(ts, "t-4-dddd"); row["dispatched_at"] = _epoch(ts); d.append(row)
    from claudlobby.brief import dispatch_ledger_path
    _write(dispatch_ledger_path(paths), d)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout == ""


def test_the_flag_and_declaration_flip_the_idle_check_and_the_shadow_grades_it(tmp_path):
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts="2026-09-02T13:00:00Z")
    half = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), PLANE_READ_UNASSIGNED="1", CLAUDLOBBY_FLEET=F)
    assert half.returncode == 0 and half.stdout.startswith("w1 ") and "no cutover_declared" in half.stderr
    _declare(root, "unassigned")
    on = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), PLANE_READ_UNASSIGNED="1", CLAUDLOBBY_FLEET=F)
    assert on.returncode == 0 and on.stdout == half.stdout and "no cutover_declared" not in on.stderr
    assert cut.READ_FLAGS["unassigned"] == "PLANE_READ_UNASSIGNED" and "unassigned" in sh.GATED
    rec = _cli(root, "shadow", "--record", "--reader", "unassigned")
    assert rec.returncode == 0 and "fleet [unassigned]" in rec.stdout and "clean" in rec.stdout, rec.stdout + rec.stderr
    with _ro(root) as conn:
        st = sh.gate_summary(conn, F, ["w1", "w2"], (sh.READER_UNASSIGNED,))
        assert [(x.bot, x.reader, x.comparisons, x.clean_run) for x in st] == [("_fleet", "unassigned", 1, 1)]
    gate = _cli(root, "shadow", "--gate", "--reader", "unassigned")
    assert gate.returncode == 1 and "_fleet [unassigned]" in gate.stdout
    (root / "state" / "plane" / "plane.db").unlink()
    gone = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), PLANE_READ_UNASSIGNED="1", CLAUDLOBBY_FLEET=F)
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


def test_the_fleet_pulse_unit_carries_the_unassigned_flag_and_names_its_fleet(tmp_path, monkeypatch):
    from claudlobby.composer import FLEET_JOB_ARMING
    from tests.test_plane_cutover_flip import _composed
    from tests.test_plane_shadow import REPO
    assert FLEET_JOB_ARMING["fleet-pulse"] == ("PLANE_SHADOW_ENABLED", "PLANE_READ_OVERDUE", "PLANE_READ_UNASSIGNED")
    timers, _ = _composed(tmp_path, monkeypatch, {"PLANE_READ_UNASSIGNED": "1"})
    pulse = next(p for p in timers.iterdir() if "fleet-pulse" in p.name and p.suffix == ".service").read_text()
    assert "Environment=PLANE_READ_UNASSIGNED=1" in pulse
    src = (REPO / "lib" / "fleet-pulse.sh").read_text()
    line = next(l for l in src.splitlines() if 'dispatch-overdue.py" --unassigned' in l)
    assert '--fleet "$fleet"' in line
    assert "|| _unassigned_rc=$?" in src and "cannot be judged this pass" in src   # a refusal is disclosed, never an empty answer


def test_retire_writes_no_longer_names_a_frozen_reader(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    for reader in sh.GATED:
        _declare(root, reader)
    done = _cli(root, "cutover", "--retire-writes")
    assert done.returncode == 0 and "frozen" not in done.stdout.lower() and "--unassigned" in done.stdout


def test_a_plane_only_completion_makes_the_flipped_check_answer_from_the_plane(tmp_path):
    """Asymmetric on purpose: w1's completion is recorded on the plane only (the
    ledger never saw it). Unflipped, the check follows the ledger (w1 not
    idle); flipped, it follows the plane (w1 idle) — the mutant that never
    consulted the plane survived every symmetric pin."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _report(root, f"wi_{'2':0>32}", f"asg_{'2':0>32}", "2026-09-02T13:00:00Z")   # plane only: no ledger row
    _declare(root, "unassigned")
    legacy = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), PLANE_READ_UNASSIGNED="0", CLAUDLOBBY_FLEET=F)
    flipped = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), PLANE_READ_UNASSIGNED="1", CLAUDLOBBY_FLEET=F)
    assert legacy.returncode == 0 and legacy.stdout == ""                         # the ledger saw no report
    assert flipped.returncode == 0 and flipped.stdout.startswith("w1 ") and "t-2-bbbb completed" in flipped.stdout


def test_a_progress_report_is_not_terminal_and_a_blocked_one_keeps_the_legacy_word(tmp_path):
    """Two rules the symmetric scene could not distinguish: a later `progress`
    (non-terminal) report means the worker is NOT idle on either side; a
    `returned_blocked` task event prints as the legacy `blocked`."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts="2026-09-02T13:00:00Z")
    prog = "2026-09-02T13:30:00Z"
    _report(root, f"wi_{'2':0>32}", f"asg_{'2':0>32}", prog, event="progress", extra={"progress": 50})
    r.append(_rrow(prog, "t-2-bbbb", "progress", progress="50"))
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout == ""                                      # progress: not idle, both sides
    # w2's open task ends blocked on both sides
    blocked = "2026-09-02T14:00:00Z"
    _report(root, f"wi_{'3':0>32}", f"asg_{'3':0>32}", blocked, bot="w2", event="returned_blocked")
    r.append(_rrow(blocked, "t-3-cccc", "blocked", bot="w2"))
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout and plane.stdout.startswith("w2 ") and plane.stdout.rstrip().endswith("t-3-cccc blocked")


def test_a_report_that_resolved_nothing_is_the_newest_report_but_not_terminal(tmp_path):
    """A bare note (the door lands its communication, no task event) after a
    completion: the ledger holds a non-terminal row, the plane a report with
    no status — not idle on either side, like a progress report."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts="2026-09-02T13:00:00Z")
    note = "2026-09-02T13:15:00Z"
    _report(root, None, None, note, event=None)
    r.append(_rrow(note, "", "progress", summary="just a note"))
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout == ""


def test_a_peers_later_report_does_not_touch_this_bots_idleness(tmp_path):
    """The newest-report question is PER BOT on both sides: w2's later progress
    report leaves w1 idle (w1's own newest report is still its completion)."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    _finish(root, paths, r, wi=f"wi_{'2':0>32}", asg=f"asg_{'2':0>32}", task_id="t-2-bbbb", ts="2026-09-02T13:00:00Z")
    later = "2026-09-02T13:45:00Z"
    _report(root, f"wi_{'3':0>32}", f"asg_{'3':0>32}", later, bot="w2", event="progress", extra={"progress": 10})
    r.append(_rrow(later, "t-3-cccc", "progress", bot="w2", progress="10"))
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout and plane.stdout.startswith("w1 ") and "w2" not in plane.stdout


def test_a_terminal_note_that_resolved_nothing_is_idle_on_both_sides(tmp_path):
    """The general lens's finding: a `completed` report with no task id and no
    open id-less dispatch lands only a communication — its status rode nowhere
    on the plane, so the idle check said busy while the ledger said idle. The
    door now lands a report_status marker; both sides agree."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    note = "2026-09-02T13:15:00Z"
    _report(root, None, None, note, event=None, status="completed")
    r.append(_rrow(note, "", "completed", summary="all done, nothing open"))
    from claudlobby.brief import report_ledger_path
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout and plane.stdout.startswith("w1 ") and plane.stdout.rstrip().endswith("- completed")


def test_a_case_variant_alias_does_not_lose_the_bots_report(tmp_path):
    """The adversarial lens: a dispatch to `bot:f/W1` and a report from
    `bot:f/w1` mint two actor uids; every per-bot read spans all of them, so
    the idle check agrees with the ledger (which is case-insensitive)."""
    from tests.test_plane_cutover_parity import _drow, _live_dispatch
    from tests.test_plane_shadow import _epoch
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    ts = "2026-09-02T12:30:00Z"
    _live_dispatch(root, "6", "t-6-ffff", ts=ts, bot="W1", expected_by="2026-09-03T00:00:00+00:00")
    row = _drow(ts, "t-6-ffff", bot="W1"); row["dispatched_at"] = _epoch(ts); d.append(row)
    from claudlobby.brief import dispatch_ledger_path, report_ledger_path
    _write(dispatch_ledger_path(paths), d)
    done = "2026-09-02T13:00:00Z"
    _report(root, f"wi_{'6':0>32}", f"asg_{'6':0>32}", done, bot="w1")        # the lower-case alias reports
    r.append(_rrow(done, "t-6-ffff", "completed", bot="w1"))
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "jsonl")
    plane = _matcher(root, "--unassigned", dl, rl, str(NOW_EPOCH), "--source", "plane", "--fleet", F)
    assert jsonl.stdout == plane.stdout and plane.stdout.startswith("w1 ") and "t-6-ffff completed" in plane.stdout
