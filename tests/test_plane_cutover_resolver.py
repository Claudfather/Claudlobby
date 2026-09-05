"""Cutover chunk 6a → F18 closure R2a: the resolver (`--open-task`) answers
from the plane — the open list's HEAD, guarded by the plane twin of the #1418
rule (the bot's newest dispatch is id-less and unanswered → the next terminal
report answers THAT, so the resolver hands back nothing) — and the id-less
dispatches the live door emits are what make that guard answerable.

Deleted with the shadow and the legacy side (F18 closure, R2a):
test_the_resolver_answers_the_same_from_both_sources (→
test_the_resolver_answers_the_open_lists_head_from_the_plane),
test_an_unanswered_idless_dispatch_makes_both_resolvers_answer_nothing (→
..._makes_the_resolver_answer_nothing),
test_the_head_streak_needs_200_agreeing_resolver_answers_and_a_change,
test_idle_records_alone_never_meet_the_resolver_bar,
test_a_truncated_or_pre_6a_record_ends_the_resolver_run,
test_cutover_open_task_refuses_short_and_declares_when_met (→
test_cutover_open_task_declares_a_direct_move_and_the_doctor_reads_it),
test_the_flag_and_declaration_flip_the_resolver (its unreachable half →
test_the_resolver_refuses_an_unreachable_plane),
test_shadow_open_task_is_a_gate_mode_only.
"""

from __future__ import annotations

from claudlobby.plane import cutover as cut
from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import (F, NOW_EPOCH, REPO, _cli, _epoch, _matcher, _scene,
                                  _stdlib_readers, open_assignment_ids, ro as _ro)
from tests.test_plane_cutover_parity import _drow, _live_dispatch, _write


def _terminal(root, n, bot, at):
    emit_batch(root, [{"event_type": "task", "emitter": "report-back", "fleet": F,
                       "source_ref": f"report-back:msg_{n:0>32}", "occurred_at": at,
                       "payload": {"work_item_id": f"wi_{n:0>32}", "assignment_id": f"asg_{n:0>32}",
                                   "event": "completed", "actor": f"bot:{F}/{bot}"}}])


def test_the_resolver_answers_the_open_lists_head_from_the_plane(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    for bot, want in (("w1", "t-2-bbbb"), ("w2", "t-3-cccc")):
        r = _matcher(root, "--open-task", bot, "--fleet", F)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == want
        assert f"--open-task: bot={bot!r} -> {want} [source=plane]" in r.stderr   # stdout is machine-consumed; the note rides stderr
        at = _matcher(root, "--open-task", bot, str(NOW_EPOCH), "--fleet", F)   # an explicit instant, like the other modes
        assert at.returncode == 0 and at.stdout.strip() == want
    pr = _stdlib_readers()
    with _ro(root) as conn:
        assert pr.head(conn, F, "w1") == "t-2-bbbb" and pr.head(conn, F, "w2") == "t-3-cccc"
    _terminal(root, "2", "w1", "2026-09-02T13:00:00Z")               # w1 closes t-2: nothing left to resolve
    closed = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert closed.returncode == 0 and closed.stdout == "" and "-> - [source=plane]" in closed.stderr


def test_an_unanswered_idless_dispatch_makes_the_resolver_answer_nothing(tmp_path):
    """The #1418 guard's plane twin: the bot's newest dispatch is id-less and
    unanswered, so the next terminal report answers THAT — the resolver never
    hands back the oldest id'd row; a terminal report after it discharges
    the guard."""
    root, paths, _, _ = _scene(tmp_path)
    ts = "2026-09-02T11:00:00Z"                         # newer than w1's open t-2 (10:00)
    _live_dispatch(root, "7", "sha:" + "cd" * 8, ts=ts, expected_by="2026-09-02T12:00:00+00:00")
    guarded = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert guarded.returncode == 0 and guarded.stdout == "" and "-> - [source=plane]" in guarded.stderr
    pr = _stdlib_readers()
    with _ro(root) as conn:
        assert pr.answering_idless(conn, F, "w1") and pr.head(conn, F, "w1") is None
        assert pr.head(conn, F, "w2") == "t-3-cccc"                  # the guard is per bot
    _terminal(root, "7", "w1", "2026-09-02T11:30:00Z")               # a terminal report after it
    freed = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert freed.returncode == 0 and freed.stdout.strip() == "t-2-bbbb"


def test_cutover_open_task_declares_a_direct_move_and_the_doctor_reads_it(tmp_path):
    root, paths, _, _ = _scene(tmp_path)
    r = _cli(root, "cutover", "--reader", "open_task")               # no gate, no --force needed
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PLANE_READ_OPEN_TASK=1" in r.stdout and "REFUSED" not in r.stdout
    with _ro(root) as conn:
        decl = cut.declared(conn, F)
    assert set(decl) == {"open_task"} and decl["open_task"][1] == cut.DIRECT_MOVE_REASON
    (root / "home").mkdir()
    (root / "local" / F / ".env").write_text("PLANE_READ_OPEN_TASK=1\n")
    d = _cli(root, "doctor")
    assert "cutover open_task — flipped to the plane" in d.stdout, d.stdout


def test_the_resolver_refuses_an_unreachable_plane(tmp_path):
    """report-back consumes this door with `2>/dev/null || true`: a refusal is
    an EMPTY stdout at rc 3 (the report degrades to id-less), never a stale id."""
    root, paths, _, _ = _scene(tmp_path)
    nofleet = _matcher(root, "--open-task", "w1")
    assert nofleet.returncode == 3 and nofleet.stdout == "" and "UNREACHABLE" in nofleet.stderr
    (root / "state" / "plane" / "plane.db").unlink()
    gone = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert gone.returncode == 3 and gone.stdout == "" and "UNREACHABLE" in gone.stderr


def test_a_peers_terminal_report_does_not_discharge_this_bots_idless_dispatch(tmp_path):
    """The guard is per bot: w2 finishing something after w1's unanswered
    id-less dispatch says nothing about w1 — the resolver keeps answering
    nothing for w1 until w1 itself reports."""
    root, paths, _, _ = _scene(tmp_path)
    ts = "2026-09-02T11:00:00Z"
    _live_dispatch(root, "7", "sha:" + "ef" * 8, ts=ts, expected_by="2026-09-02T12:00:00+00:00")
    _terminal(root, "3", "w2", "2026-09-02T11:30:00Z")
    r = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert r.returncode == 0 and r.stdout == ""
    assert _matcher(root, "--open-task", "w2", "--fleet", F).stdout == ""     # and w2 has nothing open now


def test_a_re_import_of_a_live_idless_row_adds_nothing(tmp_path):
    """The rationale, pinned: a ledger holding a live-emitted id-less row
    imports as NOTHING new — the row's stamped plane ids already match (the
    importer never keys dispatch rows by content; id-less rows are not
    attributable through the report ledger and are skipped, disclosed)."""
    from datetime import datetime, timezone
    from pathlib import Path
    from claudlobby.commands.plane import dispatch_ledger_path, report_ledger_path
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


def test_sha256_hex32_is_the_importers_content_key_and_fails_loudly_without_a_tool(tmp_path):
    """The bash key must equal `parity.content_key` byte for byte, and with no
    sha tool on PATH it must FAIL (empty + nonzero) rather than mint junk —
    the door then discloses and emits the communication only."""
    import hashlib
    import subprocess
    lib = REPO / "lib" / "lib-common.sh"
    line = '{"ts":"2026-09-02T10:00:00Z","task":"do \\"the\\" thing\\\\n","x":1}'
    ok = subprocess.run(["bash", "-c", f'source "{lib}"; sha256_hex32 "$1"', "_", line],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0 and ok.stdout.strip() == hashlib.sha256(line.encode()).hexdigest()[:32]
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("bash", "cut", "printf"):
        src = subprocess.run(["bash", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if src and src.startswith("/"):
            (bare / tool).symlink_to(src)
    no_tool = subprocess.run(["bash", "-c", f'source "{lib}"; sha256_hex32 "$1"', "_", line],
                             capture_output=True, text=True, timeout=30, env={"PATH": str(bare)})
    assert no_tool.returncode != 0 and no_tool.stdout.strip() == ""


def test_a_plane_mode_call_opens_the_plane_once(tmp_path):
    """The fold's efficiency claim, pinned at the CLI surface: one read-only
    open per invocation (the roster scan and the read share the session)."""
    root, paths, _, _ = _scene(tmp_path)
    tracer = tmp_path / "sitecustomize.py"
    tracer.write_text(
        "import sqlite3, os\n_real = sqlite3.connect\n"
        "def _c(*a, **k):\n    open(os.environ['CONNECT_LOG'], 'a').write('open\\n')\n    return _real(*a, **k)\n"
        "sqlite3.connect = _c\n")
    for args in (("--open", "w1"), ("--open-task", "w1"), ("--all", str(NOW_EPOCH)), ("--unassigned", str(NOW_EPOCH))):
        log = tmp_path / "connects.log"
        log.write_text("")
        r = _matcher(root, *args, "--fleet", F, PYTHONPATH=str(tmp_path), CONNECT_LOG=str(log))
        assert r.returncode == 0, (args, r.stderr)
        assert log.read_text().count("open") == 1, (args, log.read_text())
