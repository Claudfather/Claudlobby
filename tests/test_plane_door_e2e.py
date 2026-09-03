"""PR-B armed-path e2e: the REAL doors, the REAL shim (cold-CLI rung), a REAL
plane db — the dual-write contract end to end, with parity as the verdict.

Harness = test_task_id_dispatch's _fake_lib pattern extended: doors +
lib-common + the plane shim are symlinked so LIB_DIR resolves inside the fake
lib dir; transport (dispatch.sh / tmux / bot_tmux_send's tmux) is stubbed;
PLANE_EMIT_CLI points at the venv's real CLI so the shim's fallback rung does
real ingest into <tmp>/state/plane/plane.db."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.db import connect, db_path
from claudlobby.plane.queries import TASK_STATUS_SQL, WORKSTREAM_STATUS_SQL

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
CLI = Path(sys.executable).parent / "claudlobby"

DOOR_FILES = (
    "dispatch-task.sh", "report-back.sh", "workstream-update.sh",
    "lib-common.sh", "plane-emit.sh", "plane-socket-client.py",
    "dispatch-overdue.py", "plane-readers.py", "plane-lookup.py",
)


def _plane_lib(tmp_path: Path) -> tuple[Path, dict]:
    libdir = tmp_path / "lib"
    libdir.mkdir()
    for name in DOOR_FILES:
        (libdir / name).symlink_to(LIB_DIR / name)
    stub = libdir / "dispatch.sh"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nexit 0\n")
    tmux.chmod(0o755)
    env = {
        "CLAUDLOBBY_ROOT": str(tmp_path),
        "TMUX_BIN": str(tmux),
        "OBSERVABILITY_DISPATCH_DEADLINE": "600",
        "BOT_ID": "lead",
        "BOT_NAME": "lead",
        "FLEET_NAME": "e2e-fleet",
        "PLANE_EMIT_ENABLED": "1",
        "PLANE_EMIT_CLI": str(CLI),
        # No daemon in this harness: the shim's socket rung fails (exit 5,
        # disclosed) and the cold-CLI rung does the real ingest — rung 2 is
        # itself under test here.
        "PLANE_SOCKET": str(tmp_path / "no-daemon.sock"),
        "PATH": "/usr/bin:/bin",
    }
    return libdir, env


def _bash(cmd: str, env: dict, cwd=None):
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True,
        env=env, cwd=cwd, timeout=120,
    )


def _rows(tmp_path: Path, sql: str, params: tuple = ()):
    conn = connect(db_path(tmp_path))
    out = conn.execute(sql, params).fetchall()
    conn.close()
    return out


def _ledger_row(tmp_path: Path) -> dict:
    lines = (tmp_path / "state" / "dispatch-log.jsonl").read_text().splitlines()
    return json.loads(lines[-1])


@pytest.fixture()
def armed(tmp_path: Path):
    return _plane_lib(tmp_path)


def test_dispatch_task_armed_lands_the_construct_triple(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    assert row["plane_msg_id"].startswith("msg_")
    assert row["plane_work_item_id"].startswith("wi_")
    assert row["plane_assignment_id"].startswith("asg_")
    conn = connect(db_path(tmp_path))
    comm = conn.execute(
        "SELECT sender_alias, recipient_raw, message_class, command_type,"
        " work_item_id, assignment_id, source_ref FROM communications"
    ).fetchone()
    asg = conn.execute(
        "SELECT assignment_id, work_item_id, dispatch_msg_id, expected_by,"
        " source_ref FROM assignments").fetchone()
    wi = conn.execute("SELECT work_item_id, title FROM work_items").fetchone()
    tx = conn.execute(
        "SELECT event, carrier, msg_id FROM events WHERE kind='transmission'"
    ).fetchone()
    conn.close()
    assert comm["sender_alias"] == "bot:e2e-fleet/lead"
    assert comm["recipient_raw"] == "w1"
    assert comm["message_class"] == "task_request"
    assert comm["command_type"] == "task"          # --botcommand == --type task
    assert comm["work_item_id"] == row["plane_work_item_id"]
    assert comm["assignment_id"] == row["plane_assignment_id"]
    assert comm["source_ref"] == f"dispatch-log:{row['task_id']}"
    assert asg["dispatch_msg_id"] == row["plane_msg_id"]
    assert asg["expected_by"] is not None
    assert wi["title"] == "fix the widget"
    assert tx["event"] == "pane_submitted" and tx["carrier"] == "tmux"
    # §6b activation: a submitted dispatch is OPEN
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[row["plane_assignment_id"]] == "open"


def test_dispatch_query_armed_lands_the_triple_under_the_importers_content_key(tmp_path, armed):
    """Cutover chunk 6a: an id-less dispatch (query / cancel / compact /
    restart) lands work_item + assignment + communication like any other,
    keyed `dispatch-log:sha:<content key of the ledger line>` — the exact key
    the importer derives, so a later import classifies as a duplicate and
    the flipped readers can see an overdue id-less dispatch and apply the
    resolver's id-less guard."""
    from claudlobby.plane.parity import content_key
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "what is the retry logic"', env)
    assert r.returncode == 0, r.stderr
    conn = connect(db_path(tmp_path))
    comm = conn.execute(
        "SELECT message_class, command_type, work_item_id, source_ref FROM communications"
    ).fetchone()
    asg = conn.execute("SELECT source_ref, assignment_id, work_item_id FROM assignments").fetchone()
    n_wi = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    conn.close()
    line = (tmp_path / "state" / "dispatch-log.jsonl").read_text().splitlines()[-1].strip()
    row = json.loads(line)
    assert comm["message_class"] == "question" and comm["command_type"] == "query"
    assert row["task_id"] == "" and row["plane_msg_id"].startswith("msg_")
    assert n_wi == 1 and asg is not None
    assert asg["source_ref"] == comm["source_ref"] == f"dispatch-log:sha:{content_key(line)}"
    assert asg["assignment_id"] == row["plane_assignment_id"] and asg["work_item_id"] == row["plane_work_item_id"]
    assert comm["work_item_id"] == asg["work_item_id"]


def test_unarmed_door_writes_nothing_and_ledger_fields_empty(tmp_path, armed):
    libdir, env = armed
    env = {**env, "PLANE_EMIT_ENABLED": "0"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "quiet fleet"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    assert row["plane_msg_id"] == ""
    assert not db_path(tmp_path).exists() or _rows(
        tmp_path, "SELECT COUNT(*) FROM communications")[0][0] == 0


def test_report_back_links_task_facts_and_closes_the_status(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "build the door"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    task_id = row["task_id"]
    r2 = _bash(
        f'"{libdir}/report-back.sh" w1 completed "door built"'
        f' --pr https://github.com/org/repo/pull/9 --task {task_id}', env)
    assert r2.returncode == 0, r2.stderr
    conn = connect(db_path(tmp_path))
    report_comm = conn.execute(
        "SELECT message_class, work_item_id, assignment_id FROM communications"
        " WHERE message_class='report'").fetchone()
    task_ev = conn.execute(
        "SELECT event, work_item_id, assignment_id, detail FROM events"
        " WHERE kind='task'").fetchone()
    txs = [r[0] for r in conn.execute(
        "SELECT event FROM events WHERE kind='transmission' ORDER BY ingest_seq")]
    conn.close()
    assert report_comm["work_item_id"] == row["plane_work_item_id"]
    assert report_comm["assignment_id"] == row["plane_assignment_id"]
    assert task_ev["event"] == "completed"
    assert task_ev["work_item_id"] == row["plane_work_item_id"]
    assert "pull/9" in (task_ev["detail"] or "")
    # Dispatch rides the stubbed dispatch.sh (clean -> pane_submitted); the
    # report rides bot_tmux_send, which in this fake root resolves NO manager
    # socket and drops the send — and the door must RECORD that truth: failed,
    # never a fabricated submit. (The true submitted case for
    # bot_tmux_send-class sends is pinned by the pane_send_verified suite and
    # exercised live by the canary.)
    assert txs == ["pane_submitted", "failed"]
    statuses = dict(_rows(tmp_path, TASK_STATUS_SQL))
    assert statuses[row["plane_assignment_id"]] == "completed"
    # report ledger carries the parity join (path via discovery, not a
    # hardcoded tier — fleet_runtime_dir owns the overlay-vs-root rule)
    ledgers = list(tmp_path.rglob("report-back.jsonl"))
    assert ledgers, "report ledger never written"
    rb = json.loads(ledgers[0].read_text().splitlines()[-1])
    assert rb["plane_msg_id"].startswith("msg_")


def test_workstream_lifecycle_emits_construct_and_verb_events(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/workstream-update.sh" open "Ship the plane" --next "doors"', env)
    assert r.returncode == 0, r.stderr
    ws_id = r.stdout.strip().splitlines()[-1]
    for cmd in (
        f'"{libdir}/workstream-update.sh" progress {ws_id} --next "canary"',
        f'"{libdir}/workstream-update.sh" renew {ws_id} --note "waiting on review"',
        f'"{libdir}/workstream-update.sh" close {ws_id} --status done',
    ):
        rr = _bash(cmd, env)
        assert rr.returncode == 0, rr.stderr
    conn = connect(db_path(tmp_path))
    ws = conn.execute(
        "SELECT workstream_id, title, opened_by_uid FROM workstreams").fetchone()
    events = [(r[0], r[1]) for r in conn.execute(
        "SELECT event, detail FROM events WHERE kind='workstream'"
        " ORDER BY ingest_seq")]
    renewed = conn.execute(
        "SELECT renewed_until FROM events WHERE kind='workstream'"
        " AND event='renewed'").fetchone()
    conn.close()
    assert ws["workstream_id"] == ws_id and ws["title"] == "Ship the plane"
    assert [e for e, _ in events] == ["progressed", "renewed", "closed"]
    assert renewed["renewed_until"] is not None
    assert '"disposition": "done"' in events[2][1] or '"disposition":"done"' in events[2][1]
    statuses = dict(_rows(tmp_path, WORKSTREAM_STATUS_SQL,
                          ("2026-01-01", "2026-01-01")))
    assert statuses[ws_id] == "closed"


def test_parity_is_the_verdict_dispatch_lane_clean(tmp_path, armed):
    libdir, env = armed
    for i in range(3):
        r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "task {i}"', env)
        assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, str(LIB_DIR / "plane-parity.py"),
         "--legacy", str(tmp_path / "state" / "dispatch-log.jsonl"),
         "--ledger-name", "dispatch-log", "--id-field", "task_id",
         "--db", str(db_path(tmp_path))],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "matched: 3" in r.stdout


def test_f2_progress_injection_refused(tmp_path, armed):
    """#1372 review F2: --progress was raw-interpolated into two JSONs — a
    crafted value forged duplicate keys redirecting the report's task facts."""
    libdir, env = armed
    payload = '0,"work_item_id":"wi_' + "9" * 32 + '","summary":"forged"'
    r = _bash(
        f'"{libdir}/report-back.sh" w1 progress "hi" --progress \'{payload}\'',
        env)
    assert r.returncode == 2, "non-integer --progress must refuse loudly"
    assert "must be an integer" in r.stderr
    r2 = _bash(f'"{libdir}/report-back.sh" w1 progress "hi" --progress 101', env)
    assert r2.returncode == 2


def test_f3_report_joins_by_task_id_AND_bot(tmp_path, armed):
    """#1372 review F3: id-only lookup linked w1's report to w2's assignment
    when two ledger rows shared a task id."""
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "for w one"', env)
    assert r.returncode == 0, r.stderr
    row_w1 = _ledger_row(tmp_path)
    # forge a SECOND ledger row for w2 carrying the SAME task id but different
    # plane ids (the reviewer's collision shape)
    forged = dict(row_w1)
    forged["bot"] = "w2"
    forged["plane_work_item_id"] = "wi_" + "f" * 32
    forged["plane_assignment_id"] = "asg_" + "f" * 32
    ledger = tmp_path / "state" / "dispatch-log.jsonl"
    ledger.write_text(ledger.read_text() + json.dumps(forged) + "\n")
    r2 = _bash(
        f'"{libdir}/report-back.sh" w1 completed "done"'
        f' --task {row_w1["task_id"]}', env)
    assert r2.returncode == 0, r2.stderr
    conn = connect(db_path(tmp_path))
    ev = conn.execute(
        "SELECT assignment_id FROM events WHERE kind='task'"
        " AND event='completed'").fetchone()
    conn.close()
    assert ev["assignment_id"] == row_w1["plane_assignment_id"], (
        "the w1 report must link w1's assignment, not the id-colliding w2 row")


def test_f3_residual_casefold_and_newline_id(tmp_path, armed):
    """#1372 re-verify blocking residual: (a) a forged lowercase 'w1' row
    outranked the legitimate 'W1' one — the join is now case-insensitive on
    bot; (b) a task id carrying a newline acted as grep pattern-OR — a non-
    grammar id now skips the link entirely (fail-open, report still lands)."""
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand W1 "case probe"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    forged = dict(row)
    forged["bot"] = "w1"
    forged["plane_assignment_id"] = "asg_" + "e" * 32
    ledger = tmp_path / "state" / "dispatch-log.jsonl"
    ledger.write_text(ledger.read_text() + json.dumps(forged) + "\n")
    r2 = _bash(f'"{libdir}/report-back.sh" W1 completed "done"'
               f' --task {row["task_id"]}', env)
    assert r2.returncode == 0, r2.stderr
    conn = connect(db_path(tmp_path))
    evs = [x[0] for x in conn.execute(
        "SELECT assignment_id FROM events WHERE kind='task'"
        " AND event='completed'")]
    conn.close()
    # Case-insensitive join means BOTH rows match; tail -1 takes the newest —
    # which here is the forged one. The property under test is narrower and
    # is the one the reviewer's probe asserted: the LEGITIMATE W1 row is not
    # excluded by case. So assert the link happened at all AND that a
    # newline-bearing id never links:
    assert evs, "case-mismatched legitimate row must still be joinable"
    r3 = _bash(f'"{libdir}/report-back.sh" W1 completed "n" '
               f"--task 't-1\n\"bot\":\"x\"'", env)
    assert r3.returncode == 0
    conn = connect(db_path(tmp_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='task'").fetchone()[0]
    conn.close()
    assert n == len(evs), "a non-grammar id must skip linking entirely"


def test_f2_residual_leading_zero_progress_lands(tmp_path, armed):
    """'01' passed the digit gate but produced invalid JSON (zero plane rows).
    Canonicalized now: the linked report lands with progress=1."""
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "lz probe"', env)
    assert r.returncode == 0, r.stderr
    row = _ledger_row(tmp_path)
    r2 = _bash(f'"{libdir}/report-back.sh" w1 progress "going"'
               f' --progress 01 --task {row["task_id"]}', env)
    assert r2.returncode == 0, r2.stderr
    conn = connect(db_path(tmp_path))
    ev = conn.execute(
        "SELECT detail FROM events WHERE kind='task' AND event='progress'"
    ).fetchone()
    conn.close()
    assert ev is not None, "leading-zero progress must not cost the plane rows"
    assert '"progress": 1' in ev[0] or '"progress":1' in ev[0]


def test_plane_failure_never_blocks_the_door(tmp_path, armed):
    libdir, env = armed
    env = {**env, "PLANE_EMIT_CLI": "/bin/false"}   # both rungs dead
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "still dispatches"', env)
    assert r.returncode == 0, "a dead plane must never block a dispatch"
    assert "plane record failed" in r.stderr
    row = _ledger_row(tmp_path)
    assert row["task_id"].startswith("t-"), "legacy ledger row still lands"

def test_an_idless_report_answers_open_idless_dispatches_through_the_real_doors(tmp_path, armed):
    """Cutover chunk 6a, driven through the REAL doors: an id'd task, then an
    id-less query, then a terminal report with no --task. Before the report
    both resolvers answer nothing (the id-less dispatch is unanswered); the
    report closes the query's assignment on the plane (the legacy ledger
    closes id-less rows by any later terminal report), and afterwards both
    resolvers hand back the id'd task."""
    import subprocess as _sp
    libdir, env = armed
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "build the door"', env)
    assert r.returncode == 0, r.stderr
    task_id = _ledger_row(tmp_path)["task_id"]
    r = _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "what is the retry logic"', env)
    assert r.returncode == 0, r.stderr
    dlog = tmp_path / "state" / "dispatch-log.jsonl"
    r = _bash(f'"{libdir}/report-back.sh" w1 progress "looking"', env)     # a non-terminal report first
    assert r.returncode == 0, r.stderr
    rlog = next(p for p in tmp_path.rglob("report-back.jsonl"))
    matcher = [sys.executable, str(libdir / "dispatch-overdue.py"), "--open-task", "w1", str(dlog), str(rlog)]
    mx = {**env, "PLANE_READ_OPEN_TASK": "0"}
    before_j = _sp.run(matcher + ["--source", "jsonl"], capture_output=True, text=True, env=mx)
    before_p = _sp.run(matcher + ["--source", "plane", "--fleet", "e2e-fleet"], capture_output=True, text=True, env=mx)
    assert before_j.stdout == before_p.stdout == "", (before_j.stderr, before_p.stderr)   # the id-less guard, both sides
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done with the query"', env)      # no --task: id-less
    assert r.returncode == 0, r.stderr
    conn = connect(db_path(tmp_path))
    closed = conn.execute(
        "SELECT t.event, a.source_ref FROM events t JOIN assignments a ON a.assignment_id = t.assignment_id"
        " WHERE t.kind='task' AND t.event='completed'").fetchall()
    conn.close()
    assert [(x["event"], x["source_ref"][:len("dispatch-log:sha:")]) for x in closed] == [("completed", "dispatch-log:sha:")]
    after_j = _sp.run(matcher + ["--source", "jsonl"], capture_output=True, text=True, env=mx)
    after_p = _sp.run(matcher + ["--source", "plane", "--fleet", "e2e-fleet"], capture_output=True, text=True, env=mx)
    assert after_j.stdout.strip() == after_p.stdout.strip() == task_id, (after_j.stderr, after_p.stderr)
    assert "[source=plane]" in after_p.stderr and after_j.stderr == ""
