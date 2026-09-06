"""The task loop's bash doors, through the REAL doors (chunk M-A, #1481).

`test_plane_door_e2e`'s harness: real `lib/` scripts, the real shim on its
cold-CLI rung, a real plane db under a throwaway root. What only running them
proves is that `task-act.sh` resolves the row the way `--supersedes` cannot
(it does not know the assignee), that a withdrawal actually leaves the
matcher's open set, and that an escalation does NOT.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudlobby.plane.queries import TASK_STATUS_SQL
from tests.conftest import load_lib_module
from tests.test_plane_door_e2e import (
    _bash, _plane_lib, _plane_row, _rows, _seed_assignment,
)

F = "e2e-fleet"


def _full_capture(root: Path) -> None:
    """The reason and the question are CONTENT, so a metadata-mode plane
    drops them at the door — the doors are what is under test here, not the
    capture policy."""
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _matcher(root, libdir, env, *args):
    return subprocess.run([sys.executable, str(libdir / "dispatch-overdue.py"), *args,
                           "--fleet", F, "--root", str(root)],
                          capture_output=True, text=True, env=env, timeout=120)


def _lookup(root, libdir, env, *args):
    return subprocess.run([sys.executable, str(libdir / "plane-lookup.py"),
                           "--root", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def _task_events(root, assignment_id):
    return [(r["event"], r["detail"]) for r in _rows(
        root, "SELECT event, detail FROM events WHERE kind='task'"
              " AND assignment_id = ? ORDER BY ingest_seq", (assignment_id,))]


# --- withdraw ---------------------------------------------------------------

def test_withdraw_closes_the_row_the_matcher_calls_open(tmp_path):
    """M2: a manager could end a row only by getting a report or
    re-dispatching with `--supersedes`; neither fits a send that never
    reached the bot. `cancelled` was already terminal on the plane, so the
    one thing to prove is that the door lands it on the RIGHT assignment."""
    libdir, env = _plane_lib(tmp_path)
    _full_capture(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "fix the widget"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    assert _matcher(tmp_path, libdir, env, "--open", "w1").stdout.strip()

    act = _bash(f'"{libdir}/task-act.sh" withdraw {row["task_id"]}'
                ' --reason "the broadcast never landed"', env)
    assert act.returncode == 0, act.stderr
    events = _task_events(tmp_path, row["plane_assignment_id"])
    assert [e for e, _ in events] == ["cancelled"]
    assert json.loads(events[0][1])["reason"] == "the broadcast never landed"
    assert json.loads(events[0][1])["by"] == "lead"
    statuses = {r[0]: r[1] for r in _rows(tmp_path, TASK_STATUS_SQL)}
    assert statuses[row["plane_assignment_id"]] == "cancelled"
    assert _matcher(tmp_path, libdir, env, "--open", "w1").stdout.strip() == ""


def test_withdraw_refuses_an_id_that_matches_two_open_assignments(tmp_path):
    """A task id is unique per dispatch, NOT across bots (#526 lets two
    fleets hold one bot name; a re-dispatch under one id is legal). The door
    cannot scope by assignee the way `--supersedes` does, so it REFUSES and
    names the candidates rather than cancelling the wrong worker's task."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "first"', env)
    assert r.returncode == 0, r.stderr
    mine = _plane_row(tmp_path)
    twin = _seed_assignment(tmp_path, task_id=mine["task_id"], bot="w2", tag="7")

    act = _bash(f'"{libdir}/task-act.sh" withdraw {mine["task_id"]} --reason "no"', env)
    assert act.returncode == 2, (act.returncode, act.stdout, act.stderr)
    assert "matches 2 open assignments" in act.stderr
    assert mine["plane_assignment_id"] in act.stderr and twin[2] in act.stderr
    assert _task_events(tmp_path, mine["plane_assignment_id"]) == []   # nothing acted


def test_withdraw_separates_nothing_open_from_a_plane_it_cannot_read(tmp_path):
    """source_state's rule at the act door: an id with nothing open is rc 2
    (a real answer, and it names the ambiguity — a CLOSED row answers empty
    too), while a plane that cannot be opened is rc 3. Collapsing them would
    let a wrong root read as "already handled"."""
    libdir, env = _plane_lib(tmp_path)
    dark = _bash(f'"{libdir}/task-act.sh" withdraw t-999999-beef --reason "x"', env)
    assert dark.returncode == 3 and "unreachable" in dark.stderr   # no db yet

    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"',
                 env).returncode == 0                              # the db exists now
    act = _bash(f'"{libdir}/task-act.sh" withdraw t-999999-beef --reason "x"', env)
    assert act.returncode == 2, (act.returncode, act.stderr)
    assert "no OPEN assignment carries" in act.stderr


def test_withdraw_without_a_reason_is_a_usage_error(tmp_path):
    """A withdrawal nobody can later explain is the shape of row the whole
    chunk exists to remove — required, never defaulted."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    act = _bash(f'"{libdir}/task-act.sh" withdraw {row["task_id"]}', env)
    assert act.returncode == 1 and "--reason" in act.stderr
    assert _task_events(tmp_path, row["plane_assignment_id"]) == []


def test_a_silenced_plane_refuses_the_act_rather_than_doing_it_unrecorded(tmp_path):
    """dispatch-task.sh sends anyway and discloses, because ITS mission is
    the send. An act whose whole content IS the record has nothing left to
    do, so it refuses at rc 3."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    act = _bash(f'"{libdir}/task-act.sh" withdraw {row["task_id"]} --reason "x"',
                {**env, "PLANE_EMIT_DISABLED": "1"})
    assert act.returncode == 3, (act.returncode, act.stderr)
    assert "nothing was done" in act.stderr
    assert _task_events(tmp_path, row["plane_assignment_id"]) == []


# --- escalate ---------------------------------------------------------------

def test_escalate_is_non_terminal_and_readable_by_the_watchdog(tmp_path):
    """M3, and the ruling that shapes it: `escalated` keeps the task OPEN
    while the human decides. So the matcher must still list it — and a
    dedicated read is the only way fleet-pulse can see it, which is what
    `plane-lookup --escalated` is for."""
    libdir, env = _plane_lib(tmp_path)
    _full_capture(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "port the parser"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)

    act = _bash(f'"{libdir}/task-act.sh" escalate {row["task_id"]}'
                ' "do we ship without the migration?"', env)
    assert act.returncode == 0, act.stderr
    events = _task_events(tmp_path, row["plane_assignment_id"])
    assert [e for e, _ in events] == ["escalated"]
    assert json.loads(events[0][1])["question"] == "do we ship without the migration?"

    # still open: the work is not lost while a human thinks
    assert row["task_id"] in _matcher(tmp_path, libdir, env, "--open", "w1").stdout

    out = _lookup(tmp_path, libdir, env, "--escalated", "--fleet", F)
    assert out.returncode == 0, out.stderr
    fields = out.stdout.strip().split("\t")
    assert fields[0] == row["plane_assignment_id"]
    assert fields[1] == row["task_id"]
    assert fields[2] == "lead"
    assert fields[4] == "do we ship without the migration?"


def test_a_later_report_clears_the_escalation(tmp_path):
    """The escalation holds only while it is the assignment's NEWEST task
    event, so the worker reporting — or the manager withdrawing — ends it
    with no second door to remember."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "port the parser"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/task-act.sh" escalate {row["task_id"]} "which repo?"',
                 env).returncode == 0
    assert _lookup(tmp_path, libdir, env, "--escalated", "--fleet", F).stdout.strip()

    rb = _bash(f'"{libdir}/report-back.sh" w1 progress "on it" --progress 20'
               f' --task {row["task_id"]}', env)
    assert rb.returncode == 0, rb.stderr
    after = _lookup(tmp_path, libdir, env, "--escalated", "--fleet", F)
    assert after.returncode == 0 and after.stdout.strip() == ""


def test_the_escalated_read_refuses_an_unreachable_plane(tmp_path):
    """source_state's rule: unreachable is not empty. A watchdog that read
    'nothing escalated' off a plane it could not open would go dark in
    silence, which is the exact class #1014 named."""
    libdir, env = _plane_lib(tmp_path)
    out = _lookup(tmp_path / "nowhere", libdir, env, "--escalated", "--fleet", F)
    assert out.returncode == 3 and out.stdout == ""
    assert "unreachable" in out.stderr


def test_escalate_without_a_question_is_a_usage_error(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    act = _bash(f'"{libdir}/task-act.sh" escalate {row["task_id"]}', env)
    assert act.returncode == 1 and "needs the question" in act.stderr


def test_an_unknown_verb_is_refused_with_the_usage(tmp_path):
    libdir, env = _plane_lib(tmp_path)
    act = _bash(f'"{libdir}/task-act.sh" delete t-1 --reason x', env)
    assert act.returncode == 1 and "task-act.sh withdraw" in act.stderr


# --- the terminal vocabulary the withdraw door needs -------------------------

def test_cancelled_is_terminal_in_every_matcher_vocabulary():
    """`cancelled` has been terminal for the plane's OPEN set since Phase 1,
    but the two LEGACY-status sets the matcher and brief read did not carry
    it — so a withdrawn row left `--open` while every status render called it
    live. One vocabulary, or the doors disagree about the same fact."""
    import importlib.util

    lib = Path(__file__).resolve().parent.parent / "lib"
    spec = importlib.util.spec_from_file_location("dov", lib / "dispatch-overdue.py")
    dov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dov)
    spec = importlib.util.spec_from_file_location("pr", lib / "plane-readers.py")
    pr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr)

    assert "cancelled" in dov._TERMINAL
    assert "cancelled" in pr.TERMINAL_STATUSES
    assert dov._TERMINAL == set(pr.TERMINAL_STATUSES)
    assert pr.LEGACY_STATUS["cancelled"] == "cancelled"
    # ...and the legacy names still map to what they always did
    assert pr.LEGACY_STATUS["returned_blocked"] == "blocked"


# --- M1: a deadline by default ----------------------------------------------

def test_the_door_and_the_composer_agree_on_the_default_deadline(tmp_path):
    """M1: with no composed value the door falls back to its own literal, and
    the two must be the SAME number or a bot.conf composed before M-A pages on
    a different clock than one composed after. 24h, in seconds."""
    from claudlobby.composer import DEFAULT_DISPATCH_DEADLINE_S

    libdir, env = _plane_lib(tmp_path)
    env = {k: v for k, v in env.items() if k != "OBSERVABILITY_DISPATCH_DEADLINE"}
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "no composed deadline"', env)
    assert r.returncode == 0, r.stderr
    row = _plane_row(tmp_path)
    dispatched = _rows(tmp_path, "SELECT occurred_at FROM assignments"
                                 " ORDER BY ingest_seq DESC LIMIT 1")[0][0]
    from datetime import datetime
    # `occurred_at` is the INGEST stamp and `expected_by` was computed from the
    # door's own `date +%s` a moment earlier, so the span runs SHORT by however
    # long the shim's cold-CLI rung took -- 2.2s on a loaded host, measured. The
    # tolerance is one-sided and generous because the claim under test is which
    # DEFAULT was used (86400 vs the old 1800), not clock precision.
    span = (datetime.fromisoformat(row["expected_by"])
            - datetime.fromisoformat(dispatched)).total_seconds()
    assert DEFAULT_DISPATCH_DEADLINE_S - 300 <= span <= DEFAULT_DISPATCH_DEADLINE_S + 5, \
        (span, row["expected_by"])


def test_zero_is_an_open_ended_dispatch_on_either_door(tmp_path):
    """`0` must mint NO deadline. "now + 0" would be overdue in the second it
    was sent — the loudest possible reading of "no deadline please"."""
    libdir, env = _plane_lib(tmp_path)
    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "open ended"',
              {**env, "OBSERVABILITY_DISPATCH_DEADLINE": "0"})
    assert r.returncode == 0, r.stderr
    assert _plane_row(tmp_path)["expected_by"] is None

    r = _bash(f'"{libdir}/dispatch-task.sh" --botcommand --deadline-min 0 w1 "also open"', env)
    assert r.returncode == 0, r.stderr
    assert _plane_row(tmp_path)["expected_by"] is None

    # ...and an open-ended row is never overdue, however long it sits
    late = _matcher(tmp_path, libdir, env, "--all", "9999999999")
    assert late.returncode == 0 and late.stdout == "", late.stdout


# --- migration 0010: the widened CHECK ---------------------------------------

def test_migration_0010_widens_the_task_check_without_losing_a_row(tmp_path):
    """SQLite cannot ALTER a CHECK, so widening the task vocabulary is a table
    REBUILD. What that has to preserve is everything: the rows, the schema's
    own refusals, and every index (a silently-dropped partial index turns the
    per-assignment seek the §14 read gate depends on into a table scan)."""
    import sqlite3

    from claudlobby.plane.db import connect
    from claudlobby.plane.migrations import (
        SCHEMA_USER_VERSION, _migration_files, migrate)

    conn = connect(tmp_path / "old.db")
    conn.isolation_level = None
    for number, sql in _migration_files():          # stop one short of 0010
        if number >= 10:
            break
        conn.executescript(sql)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    before_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'")}

    conn.execute("INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                 " VALUES ('ev_' || ?, 'task', '2026-01-01T00:00:00+00:00')", ("a" * 32,))
    seq = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
        " ingested_at, host_uid, emitter, kind, event, work_item_id)"
        " VALUES (?, 'ev_' || ?, '2', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00', 'host_' || ?, 't', 'task', 'progress', 'wi_' || ?)",
        (seq, "a" * 32, "0" * 32, "b" * 32))
    # the pre-0010 schema REFUSES the new tokens — the defect the migration fixes
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (?, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task', 'escalated', 'wi')",
            (seq + 1, "c" * 32))

    assert migrate(conn) == SCHEMA_USER_VERSION == 10
    assert [r[0] for r in conn.execute("SELECT event FROM events")] == ["progress"]
    after_idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'")}
    assert after_idx == before_idx

    # ...and the LIVE migrated schema accepts EVERY token the contract names
    # (fold F13): the two new ones are the point, but a vocabulary pinned only
    # at its newest members is a gate that stops watching the rest.
    from claudlobby.plane.contracts import TASK_EVENTS

    assert {"escalated", "nudged"} <= set(TASK_EVENTS)
    for i, token in enumerate(TASK_EVENTS):
        conn.execute(
            "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
            " VALUES ('ev_' || ?, 'task', 'x')", (f"{i:032x}",))
        s = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (?, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task', ?, 'wi')",
            (s, f"{i:032x}", token))
    # ...and a dead token stays dead: the rebuild widened the list, not the gate
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (ingest_seq, event_id, schema_version, occurred_at,"
            " ingested_at, host_uid, emitter, kind, event, work_item_id)"
            " VALUES (99, 'ev_' || ?, '2', 'x', 'x', 'h', 't', 'task',"
            " 'receiver_acknowledged', 'wi')", ("d" * 32,))
    conn.close()


# --- the M-A fold, through the real doors (#1481) ----------------------------

def test_a_nudge_does_not_erase_an_escalation_on_the_read(tmp_path):
    """FOLD F1, the bash half. `--escalated` is fleet-pulse's only window onto
    a raise that changes no status, and a `nudged` event displaced it: the
    question vanished from the read for good. A nudge is an ask, not an
    answer — only a real act clears an escalation."""
    libdir, env = _plane_lib(tmp_path)
    _full_capture(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "port it"',
                 env).returncode == 0
    row = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/task-act.sh" escalate {row["task_id"]} "which repo?"',
                 env).returncode == 0

    from claudlobby.plane.emit_api import emit_batch
    emit_batch(tmp_path, [{
        "event_type": "task", "emitter": "t", "fleet": F,
        "payload": {"work_item_id": row["plane_work_item_id"],
                    "assignment_id": row["plane_assignment_id"],
                    "event": "nudged", "actor": "human:chris", "by": "chris"}}])

    after = _lookup(tmp_path, libdir, env, "--escalated", "--fleet", F)
    assert after.returncode == 0, after.stderr
    fields = after.stdout.strip().split("\t")
    assert fields[1] == row["task_id"] and fields[4] == "which repo?"
    # ...and a real act still clears it
    assert _bash(f'"{libdir}/report-back.sh" w1 progress "on it" --progress 20'
                 f' --task {row["task_id"]}', env).returncode == 0
    assert _lookup(tmp_path, libdir, env, "--escalated", "--fleet", F).stdout.strip() == ""


def test_an_ambiguous_id_is_answered_by_naming_the_assignment(tmp_path):
    """FOLD F5. The refusal used to say "name the assignment on the plane, or
    close the duplicates first" while NO door took an assignment — a remedy
    the caller could not carry out, leaving "close the other manager's row" as
    the only exit. `--assignment` is that door, and the refusal prints the ids
    to paste into it."""
    libdir, env = _plane_lib(tmp_path)
    _full_capture(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "first"',
                 env).returncode == 0
    mine = _plane_row(tmp_path)
    twin = _seed_assignment(tmp_path, task_id=mine["task_id"], bot="w2", tag="7")

    refused = _bash(f'"{libdir}/task-act.sh" withdraw {mine["task_id"]} --reason "no"', env)
    assert refused.returncode == 2
    assert "--assignment <asg_id>" in refused.stderr
    assert mine["plane_assignment_id"] in refused.stderr and twin[2] in refused.stderr

    # the row named is the OLDER one, which is deliberately NOT the head of
    # the newest-first list: a "narrowing" that simply took the first row
    # would pass on the other choice and prove nothing
    listed = [ln.split()[1] for ln in _lookup(
        tmp_path, libdir, env, f"--task-id={mine['task_id']}", "--all-open"
    ).stdout.strip().splitlines()]
    assert listed[0] == twin[2] and listed[1] == mine["plane_assignment_id"]

    named = _bash(f'"{libdir}/task-act.sh" withdraw {mine["task_id"]} --reason "the dead one"'
                  f' --assignment {mine["plane_assignment_id"]}', env)
    assert named.returncode == 0, named.stderr
    assert [e for e, _ in _task_events(tmp_path, mine["plane_assignment_id"])] == ["cancelled"]
    assert _task_events(tmp_path, twin[2]) == []          # the other row untouched
    # ...and escalate takes it too, without swallowing it into the question
    esc = _bash(f'"{libdir}/task-act.sh" escalate {mine["task_id"]}'
                f' --assignment {twin[2]} "which one is live?"', env)
    assert esc.returncode == 0, esc.stderr
    events = _task_events(tmp_path, twin[2])
    assert [e for e, _ in events] == ["escalated"]
    assert json.loads(events[0][1])["question"] == "which one is live?"


def test_all_open_honours_the_assignee_it_accepts(tmp_path):
    """FOLD F5. `--assignee` was declared on the parser and read by only the
    plain mode, so a caller that thought it had disambiguated got the whole
    ambiguous set back — worse than not offering the flag."""
    libdir, env = _plane_lib(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "first"',
                 env).returncode == 0
    mine = _plane_row(tmp_path)
    twin = _seed_assignment(tmp_path, task_id=mine["task_id"], bot="w2", tag="7")

    both = _lookup(tmp_path, libdir, env, f"--task-id={mine['task_id']}", "--all-open")
    assert len(both.stdout.strip().splitlines()) == 2
    one = _lookup(tmp_path, libdir, env, f"--task-id={mine['task_id']}", "--all-open",
                  "--assignee", f"bot:{F}/W2")            # case-insensitive, as the grep was
    assert one.returncode == 0
    lines = one.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].split()[1] == twin[2]


def test_the_act_is_stamped_with_the_rows_fleet_not_the_actors(tmp_path):
    """FOLD F4. 44.6% of dispatch traffic is cross-fleet, and the door stamped
    `FLEET_NAME` — the MANAGER's fleet. A withdrawal filed under the actor's
    fleet is invisible to every fleet-scoped read of the task it retired."""
    libdir, env = _plane_lib(tmp_path)
    _full_capture(tmp_path)
    tid = "t-1481-crossfleet"
    _seed_assignment(tmp_path, task_id=tid, bot="w9", tag="8")
    act = _bash(f'"{libdir}/task-act.sh" withdraw {tid} --reason "wrong fleet"',
                {**env, "FLEET_NAME": "some-other-fleet", "BOT_ID": "lead"})
    assert act.returncode == 0, act.stderr
    fleet = _rows(tmp_path, "SELECT f.alias FROM events e"
                            " LEFT JOIN identity_registry f ON f.uid = e.fleet_uid"
                            " WHERE e.kind='task' AND e.event='cancelled'")[0][0]
    assert fleet == F                       # the ROW's fleet
    # ...while WHO acted is still the actor's own alias, on the actor's fleet
    actor = _rows(tmp_path, "SELECT i.alias FROM events e"
                            " JOIN identity_registry i ON i.uid = e.actor_uid"
                            " WHERE e.kind='task' AND e.event='cancelled'")[0][0]
    assert actor == "bot:some-other-fleet/lead"


def test_a_leading_dash_id_is_a_value_and_a_bad_call_is_not_an_unreachable_plane(tmp_path):
    """FOLD F10, both halves. A task id a human typed can start with `-`, and
    passed as a separate word argparse reads it as a flag; and reporting every
    nonzero lookup rc as "unreachable" sends the caller to check the database
    for what is a typo in the call."""
    libdir, env = _plane_lib(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"',
                 env).returncode == 0                  # the db exists
    dash = _bash(f'"{libdir}/task-act.sh" withdraw -weird-id --reason "x"', env)
    assert dash.returncode == 2, (dash.returncode, dash.stderr)
    assert "no OPEN assignment carries" in dash.stderr   # an ANSWER, not a usage crash

    # ...and a plane that is genuinely absent is still rc 3, the other answer
    dark = _bash(f'"{libdir}/task-act.sh" withdraw t-nope --reason "x"',
                 {**env, "CLAUDLOBBY_ROOT": str(tmp_path / "nowhere")})
    assert dark.returncode == 3 and "unreachable" in dark.stderr


def test_the_stdlib_open_by_task_sql_is_byte_identical_to_the_package():
    """FOLD F6: "the open assignments carrying this task id" was written three
    times. One definition, one stdlib twin, pinned like `OPEN_SQL`."""
    from claudlobby.plane.queries import OPEN_BY_TASK_REF_SQL

    assert load_lib_module("plane-readers").TASK_OPEN_SQL == OPEN_BY_TASK_REF_SQL


def test_the_escalation_window_is_the_same_tuple_on_both_sides():
    """FOLD F1: the package's arms and the stdlib read must ignore the SAME
    tokens, or the card and fleet-pulse go quiet on different rules."""
    from claudlobby.plane.queries import ESCALATION_IGNORED

    assert load_lib_module("plane-readers").ESCALATION_IGNORED == ESCALATION_IGNORED
    assert "nudged" in ESCALATION_IGNORED


@pytest.mark.parametrize("bad", ["abc", "12-34", "-600", "12.5", ""])
def test_a_non_integer_deadline_is_refused_loudly_on_both_doors(tmp_path, bad):
    """FOLD F8. `$(( abc * 60 ))` under `set -u` is an unbound-variable fault
    inside the ERR trap: the dispatch exited 0 having sent NOTHING and
    recorded nothing — a silent total loss from a typo. `12-34` is the second
    half: the old class `*[!0-9-]*` let a dash through into `-le`, which is
    itself an error."""
    libdir, env = _plane_lib(tmp_path)
    if bad:
        flag = _bash(f'"{libdir}/dispatch-task.sh" --botcommand --deadline-min {bad}'
                     ' w1 "a task"', env)
        assert flag.returncode == 1, (flag.returncode, flag.stdout, flag.stderr)
        assert "--deadline-min must be a non-negative integer" in flag.stderr
    composed = _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "a task"',
                     {**env, "OBSERVABILITY_DISPATCH_DEADLINE": bad})
    if bad == "":
        # an EMPTY composed value is the unset case, not a typo: `:-` supplies
        # the door's own default and the dispatch goes through
        assert composed.returncode == 0, composed.stderr
        return
    assert composed.returncode == 1, (composed.returncode, composed.stderr)
    assert "OBSERVABILITY_DISPATCH_DEADLINE must be a non-negative integer" in composed.stderr
    assert not _rows(tmp_path, "SELECT 1 FROM assignments")   # nothing sent, nothing recorded


def test_a_migrator_that_lost_the_race_waits_instead_of_raising(tmp_path):
    """FOLD F13. The raced-migrator branch reads `user_version` the instant
    the script failed — right when the winner has COMMITTED, wrong when it is
    still running. Every earlier migration is milliseconds, so the loser's
    `BEGIN IMMEDIATE` always found the lock free or waited out a blink; 0010
    rebuilds `events` and holds it for SECONDS on a real plane, long enough to
    exceed `busy_timeout` and raise on a benign race. Taking the lock ourselves
    blocks until the winner commits, which is the proof the re-read needs."""
    import sqlite3

    from claudlobby.plane import migrations as m
    from claudlobby.plane.db import connect

    # the wait itself, against a real idle db: it answers the live version and
    # leaves NO transaction open (a held write lock here would wedge the door)
    conn = connect(tmp_path / "p.db")
    conn.isolation_level = None
    assert m.migrate(conn) == m.SCHEMA_USER_VERSION
    assert m._version_after_writer(conn) == m.SCHEMA_USER_VERSION
    assert not conn.in_transaction
    conn.close()

    # ...and the branch that uses it, driven deterministically: a script that
    # fails locked while `user_version` still reads BEHIND, and a winner that
    # commits while we wait for the lock.
    class _Losing:
        """The narrow slice of a connection `migrate` touches."""

        isolation_level = None
        in_transaction = False

        def __init__(self):
            self.versions = [0]          # nothing applied yet

        def execute(self, sql, *a):
            if "user_version" in sql:
                cur = sqlite3.connect(":memory:").execute(
                    "SELECT ?", (self.versions[-1],))
                return cur
            return None

        def executescript(self, sql):
            # the winner is mid-rebuild and still holds the write lock
            raise sqlite3.OperationalError("database is locked")

    losing = _Losing()
    waited = []

    def _winner_committed(conn):
        waited.append(True)
        return m.SCHEMA_USER_VERSION      # it finished while we blocked

    original = m._version_after_writer
    m._version_after_writer = _winner_committed
    try:
        assert m.migrate(losing) == m.SCHEMA_USER_VERSION
    finally:
        m._version_after_writer = original
    assert waited, "the loser never waited for the winner — it raised instead"
