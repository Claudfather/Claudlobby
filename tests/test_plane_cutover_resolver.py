"""Cutover chunk 6a — the resolver (`--open-task`) shadowed by its head streak,
then flipped; id-less dispatches emitted live so the plane can apply the
resolver's guard; the #1418 legacy rule.

The resolver's answer is the open list's HEAD, and every open record already
carries head_legacy / head_plane / head_agrees — so `open_task` is a STREAK
MODE over the open reader's records (bar: 200 agreeing heads + a head
change), never a third comparison. The flip is flag AND declaration, like the
list readers (`PLANE_READ_OPEN_TASK` + `cutover_declared`).
"""

from __future__ import annotations

from datetime import timedelta

from claudlobby.brief import dispatch_ledger_path, report_ledger_path
from claudlobby.plane import cutover as cut
from claudlobby.plane import shadow as sh
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import plane_root, ro as _ro
from tests.test_plane_cutover_flip import F, _cli, _declare, _ledgers, _matcher, _scene
from tests.test_plane_cutover_parity import _drow, _live_dispatch, _rrow, _write
from tests.test_plane_shadow import NOW, _epoch, _record


def test_the_resolver_answers_the_same_from_both_sources(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    for bot, want in (("w1", "t-2-bbbb"), ("w2", "t-3-cccc")):
        jsonl = _matcher(root, "--open-task", bot, dl, rl, "--source", "jsonl")
        plane = _matcher(root, "--open-task", bot, dl, rl, "--source", "plane", "--fleet", F)
        assert jsonl.returncode == 0 == plane.returncode, (jsonl.stderr, plane.stderr)
        assert jsonl.stdout.strip() == plane.stdout.strip() == want


def test_an_unanswered_idless_dispatch_makes_both_resolvers_answer_nothing(tmp_path):
    """The #1418 guard on both sides: the bot's newest dispatch is id-less and
    unanswered, so the next terminal report answers THAT — neither resolver
    hands back the oldest id'd row; a terminal report after it discharges
    the guard on both."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    ts = "2026-09-02T11:00:00Z"                         # newer than w1's open t-2 (10:00)
    _live_dispatch(root, "7", "sha:" + "cd" * 8, ts=ts, expected_by="2026-09-02T12:00:00+00:00")
    row = _drow(ts, "", expected_by=1788000000)
    row["dispatched_at"] = _epoch(ts)
    d.append(row)
    _write(dispatch_ledger_path(paths), d)
    jsonl = _matcher(root, "--open-task", "w1", dl, rl, "--source", "jsonl")
    plane = _matcher(root, "--open-task", "w1", dl, rl, "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout == ""
    done = "2026-09-02T11:30:00Z"                       # a terminal report after it, on both sides
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'7':0>32}", "occurred_at": done,
                       "payload": {"work_item_id": f"wi_{'7':0>32}", "assignment_id": f"asg_{'7':0>32}",
                                   "event": "completed", "actor": f"bot:{F}/w1"}}])
    r.append(_rrow(done, "", "completed"))
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--open-task", "w1", dl, rl, "--source", "jsonl")
    plane = _matcher(root, "--open-task", "w1", dl, rl, "--source", "plane", "--fleet", F)
    assert jsonl.stdout.strip() == plane.stdout.strip() == "t-2-bbbb"


def test_the_head_streak_needs_200_agreeing_heads_and_a_head_change(tmp_path):
    root = plane_root(tmp_path)
    t0 = NOW - timedelta(hours=400)
    for k in range(sh.GATE_HEAD_CLEAN_RUN):
        _record(root, "w1", t0 + timedelta(hours=k), ["t-1", "t-2"])
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
    assert (s.reader, s.clean_bar, s.clean_run, s.transitions, s.gate_ok) == \
        ("open_task", 200, 200, 0, False)              # 200 agreeing heads, never a change
    _record(root, "w1", t0 + timedelta(hours=200), ["t-2"])   # t-1 closed: the head moved
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
        assert (s.clean_run, s.transitions, s.gate_ok) == (201, 1, True)
        assert "[open_task]" in s.line() and "/200" in s.line()
        summary = sh.gate_summary(conn, F, ["w1", "w2"], (sh.READER_OPEN_TASK,))
        assert [(x.bot, x.gate_ok) for x in summary] == [("w1", True), ("w2", False)]
        assert sh.gate_summary(conn, F, ["w1"], (sh.READER_OPEN,))[0].clean_bar == sh.GATE_CLEAN_RUN
    _record(root, "w1", t0 + timedelta(hours=201), ["t-2"], clean=False)   # heads disagree: run ends
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
        assert s.clean_run == 0 and s.transitions == 0 and s.last_diverged_at and not s.gate_ok


def test_cutover_open_task_refuses_short_and_declares_when_met(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    short = _cli(root, "cutover", "--reader", "open_task")
    assert short.returncode == 1 and "REFUSED" in short.stdout and "/200" in short.stdout
    t0 = NOW - timedelta(hours=400)
    for bot, ids in (("w1", ["t-2-bbbb", "t-x"]), ("w2", ["t-3-cccc", "t-y"])):
        for k in range(sh.GATE_HEAD_CLEAN_RUN):
            _record(root, bot, t0 + timedelta(hours=k), ids)
        _record(root, bot, t0 + timedelta(hours=200), ids[1:])          # the head change
    met = _cli(root, "cutover", "--reader", "open_task")
    assert met.returncode == 0 and "PLANE_READ_OPEN_TASK=1" in met.stdout and "FORCED" not in met.stdout
    with _ro(root) as conn:
        assert set(cut.declared(conn, F)) == {"open_task"}
    (root / "home").mkdir()
    (root / "local" / F / ".env").write_text("PLANE_READ_OPEN_TASK=1\n")
    d = _cli(root, "doctor")
    assert "cutover open_task — flipped to the plane" in d.stdout, d.stdout


def test_the_flag_and_declaration_flip_the_resolver(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    half = _matcher(root, "--open-task", "w1", dl, rl, PLANE_READ_OPEN_TASK="1", CLAUDLOBBY_FLEET=F)
    assert half.returncode == 0 and half.stdout.strip() == "t-2-bbbb"
    assert "no cutover_declared" in half.stderr and "--reader open_task" in half.stderr
    _declare(root, "open_task")
    on = _matcher(root, "--open-task", "w1", dl, rl, PLANE_READ_OPEN_TASK="1", CLAUDLOBBY_FLEET=F)
    assert on.returncode == 0 and on.stdout.strip() == "t-2-bbbb" and "no cutover_declared" not in on.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    gone = _matcher(root, "--open-task", "w1", dl, rl, PLANE_READ_OPEN_TASK="1", CLAUDLOBBY_FLEET=F)
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


def test_shadow_open_task_is_a_gate_mode_only(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    compare = _cli(root, "shadow", "--reader", "open_task")
    assert compare.returncode == 2 and "gate mode" in compare.stderr
    gate = _cli(root, "shadow", "--gate", "--reader", "open_task")
    assert gate.returncode == 1 and "[open_task]" in gate.stdout and "/200" in gate.stdout
    assert cut.READ_FLAGS["open_task"] == "PLANE_READ_OPEN_TASK" and sh.GATED == ("open", "overdue", "open_task")


def test_a_peers_terminal_report_does_not_discharge_this_bots_idless_dispatch(tmp_path):
    """The guard is per bot on both sides: w2 finishing something after w1's
    unanswered id-less dispatch says nothing about w1 — both resolvers keep
    answering nothing for w1 until w1 itself reports."""
    root, paths, d, r = _scene(tmp_path)
    dl, rl = _ledgers(paths)
    ts = "2026-09-02T11:00:00Z"
    _live_dispatch(root, "7", "sha:" + "ef" * 8, ts=ts, expected_by="2026-09-02T12:00:00+00:00")
    row = _drow(ts, "", expected_by=1788000000)
    row["dispatched_at"] = _epoch(ts)
    d.append(row)
    _write(dispatch_ledger_path(paths), d)
    done = "2026-09-02T11:30:00Z"
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{'3':0>32}", "occurred_at": done,
                       "payload": {"work_item_id": f"wi_{'3':0>32}", "assignment_id": f"asg_{'3':0>32}",
                                   "event": "completed", "actor": f"bot:{F}/w2"}}])
    r.append(_rrow(done, "t-3-cccc", "completed", bot="w2"))
    _write(report_ledger_path(paths), r)
    jsonl = _matcher(root, "--open-task", "w1", dl, rl, "--source", "jsonl")
    plane = _matcher(root, "--open-task", "w1", dl, rl, "--source", "plane", "--fleet", F)
    assert jsonl.returncode == 0 == plane.returncode and jsonl.stdout == plane.stdout == ""
