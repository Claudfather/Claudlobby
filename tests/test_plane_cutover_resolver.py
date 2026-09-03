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
from tests.plane_fixtures import open_assignment_ids, plane_root, ro as _ro
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


def test_the_head_streak_needs_200_agreeing_resolver_answers_and_a_change(tmp_path):
    """The resolver's streak grades the RESOLVER's answers recorded on the open
    records (the guard included), not the list's head: 200 agreeing non-empty
    answers and at least one change; an idle record (nothing to resolve on
    either side) is skipped, neither counted nor breaking; a disagreement, a
    pre-6a record or a truncated one ends the run."""
    root = plane_root(tmp_path)
    t0 = NOW - timedelta(hours=600)
    for k in range(sh.GATE_HEAD_CLEAN_RUN):
        _record(root, "w1", t0 + timedelta(hours=k), ["t-1", "t-2"])
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
    assert (s.reader, s.clean_bar, s.clean_run, s.transitions, s.gate_ok) == \
        ("open_task", 200, 200, 0, False)              # 200 agreeing answers, never a change
    for k in range(5):                                   # five idle records: skipped, not counted
        _record(root, "w1", t0 + timedelta(hours=200 + k), [])
    _record(root, "w1", t0 + timedelta(hours=205), ["t-2"])   # t-1 closed: the answer moved
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
        assert (s.clean_run, s.transitions, s.gate_ok) == (201, 1, True)
        assert "[open_task]" in s.line() and "/200" in s.line()
        summary = sh.gate_summary(conn, F, ["w1", "w2"], (sh.READER_OPEN_TASK,))
        assert [(x.bot, x.gate_ok) for x in summary] == [("w1", True), ("w2", False)]
        assert sh.gate_summary(conn, F, ["w1"], (sh.READER_OPEN,))[0].clean_bar == sh.GATE_CLEAN_RUN
    # the list's head agrees but the RESOLVER disagrees (the guard fired on one side): the run ends
    _record(root, "w1", t0 + timedelta(hours=206), ["t-2"], resolver=("t-2", None))
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
        assert s.clean_run == 0 and s.transitions == 0 and s.last_diverged_at and not s.gate_ok
        assert s.latest_diverged                            # --check sees it too


def test_idle_records_alone_never_meet_the_resolver_bar(tmp_path):
    root = plane_root(tmp_path)
    t0 = NOW - timedelta(hours=400)
    for k in range(sh.GATE_HEAD_CLEAN_RUN):
        _record(root, "w1", t0 + timedelta(hours=k), [])
    _record(root, "w1", t0 + timedelta(hours=200), ["t-9"])
    with _ro(root) as conn:
        s = sh.head_streak(conn, F, "w1")
    assert (s.clean_run, s.transitions, s.gate_ok) == (1, 0, False)   # one real answer, not 201


def test_a_truncated_or_pre_6a_record_ends_the_resolver_run(tmp_path):
    """A truncated record loses its JSON and stays keyed only by its subject
    alias (the chunk-3 rule); for the resolver's run it is unreadable, so it
    ends the run — as does a chunk-3 record that predates the resolver
    fields."""
    from claudlobby.plane.ids import derive_uid
    root, paths, _, _ = _scene(tmp_path)
    t0 = NOW - timedelta(hours=10)
    alias = f"bot:{F}/w1"
    with _ro(root) as conn:
        uid = sh.actor_uid(conn, alias)
    old = {"event_type": "system", "emitter": "plane-shadow", "fleet": F,
           "occurred_at": sh.dt_iso(t0), "event_id": derive_uid("ev", "pre6a"),
           "payload": {"event": sh.EVENT_CLEAN,
                       "data": {"reader": "open", "bot": alias, "head_agrees": True, "head_legacy": "t-1"}}}
    emit_batch(root, [old])                                  # a chunk-3 record: no resolver fields
    _record(root, "w1", t0 + timedelta(hours=1), ["t-1"])
    _record(root, "w1", t0 + timedelta(hours=2), ["t-1"])
    with _ro(root) as conn:
        assert sh.head_streak(conn, F, "w1").clean_run == 2   # the pre-6a record ended the run below them
    big = {"event_type": "system", "emitter": "plane-shadow", "fleet": F,
           "occurred_at": sh.dt_iso(t0 + timedelta(hours=3)), "event_id": derive_uid("ev", "big"),
           "payload": {"event": sh.EVENT_CLEAN, "subject_kind": "actor", "subject_uid": uid,
                       "subject_alias": alias,
                       "data": {"reader": "open", "bot": alias, "resolver_agrees": True,
                                "resolver_legacy": "t-1", "pad": "x" * 20000}}}
    emit_batch(root, [big])                                  # over the diagnostic cap: truncated
    _record(root, "w1", t0 + timedelta(hours=4), ["t-1"])
    with _ro(root) as conn:
        row = conn.execute("SELECT detail_truncated FROM events WHERE event_id = ?", (big["event_id"],)).fetchone()
        assert row[0] == 1
        assert sh.head_streak(conn, F, "w1").clean_run == 1   # the truncated record ended the run


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
    rec = _cli(root, "shadow", "--record")                      # a live comparison records the resolver too
    assert rec.returncode == 0, rec.stderr
    with _ro(root) as conn:
        data = conn.execute("SELECT detail FROM events WHERE event = ? AND json_extract(detail, '$.reader') = 'open'"
                            " ORDER BY ingest_seq DESC LIMIT 1", (sh.EVENT_CLEAN,)).fetchone()[0]
    import json
    d = json.loads(data)
    assert d["resolver_agrees"] is True and d["resolver_legacy"] == d["resolver_plane"] in ("t-2-bbbb", "t-3-cccc")
    assert _cli(root, "shadow", "--check", "--reader", "open_task").returncode == 0
    _record(root, "w1", NOW, ["t-2-bbbb"], resolver=("t-2-bbbb", None))
    assert _cli(root, "shadow", "--check", "--reader", "open_task").returncode == 1
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


def test_a_re_import_of_a_live_idless_row_adds_nothing(tmp_path):
    """The rationale, pinned: a ledger holding a live-emitted id-less row
    imports as NOTHING new — the row's stamped plane ids already match (the
    importer never keys dispatch rows by content; id-less rows are not
    attributable through the report ledger and are skipped, disclosed)."""
    from datetime import datetime, timezone
    from pathlib import Path
    from claudlobby.plane.legacy_import import apply_import, plan_import
    root, paths, d, r = _scene(tmp_path)
    ts = "2026-09-02T11:00:00Z"
    _live_dispatch(root, "7", "sha:" + "ab" * 8, ts=ts, expected_by="2026-09-02T12:00:00+00:00")
    row = _drow(ts, "", expected_by=1788000000, plane=(f"msg_{'7':0>32}", f"wi_{'7':0>32}", f"asg_{'7':0>32}"))
    row["dispatched_at"] = _epoch(ts)
    d.append(row)
    _write(dispatch_ledger_path(paths), d)
    before = len(open_assignment_ids(root))
    with _ro(root) as conn:
        plan = plan_import(conn, fleet=F, dispatch_path=Path(dispatch_ledger_path(paths)),
                           report_path=Path(report_ledger_path(paths)), now=datetime.now(timezone.utc))
    assert plan.dispatches == 0                                      # the id-less row imports nothing
    assert not [e for e in plan.events if e["event_type"] in ("assignment", "work_item")]
    apply_import(root, plan)                                         # (unstamped REPORT rows may import: not this row)
    assert len(open_assignment_ids(root)) == before
