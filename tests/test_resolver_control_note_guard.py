"""#1981: a control note holds the report resolver back again, and a worker can
say that a terminal report answers no dispatch.

Since #1491 a `query` / `cancel` / `compact` / `restart` note lands the
COMMUNICATION alone, with no assignment. The resolver's only guard keyed on the
bot's newest ASSIGNMENT (`answering_idless`), so a note no longer held it back:
the worker's id-less answer was stamped with its live task and closed it as
`completed` (ravi's #917 row, 2026-09-29 04:23:57Z).

The guard now reads the newest CONTROL NOTE sent to the bot: while it has no
id-less report from the bot after it, `head()` returns None. A report naming
one of the bot's own tasks, and a newer task, leave it standing: neither
answers a note, and a wrong completion is worse than an open row (the ruling
on #1984). A `--task` that links to none of the bot's tasks counts as id-less
and releases it. The note
fixture is the LIVE shape: the 04:20:32Z note carried a
`dispatch-log:sha:` ref, message_class `question`, command_type `query`, a
recipient alias AND `recipient_raw`, and no work item. The door's disclosed
fallback (no alias when it cannot resolve the worker's bot dir) is pinned too,
because that is the shape the armed harness below produces.

`--no-task` (or `--task -`) covers the notes the plane never sees: a raw
`dispatch.sh` send, or text typed into the pane.
"""

from __future__ import annotations

import pytest

from claudlobby.plane.emit_api import emit_batch
from claudlobby.plane.queries import TASK_STATUS_SQL
from tests.plane_fixtures import (F, _live_dispatch, _matcher, _report, _scene,
                                  _stdlib_readers, ro as _ro)
from tests.test_plane_door_e2e import _bash, _plane_row, _rows, armed  # noqa: F401

CONTROL = [("query", "question"), ("cancel", "raw_control"),
           ("compact", "raw_control"), ("restart", "raw_control")]


def _control_note(root, n, *, ts, bot="w1", cmd="query", cls="question", alias=True):
    """A control note as the live door lands it: one communication, nothing else."""
    payload = {"msg_id": f"msg_{'c' * 24}{n:0>8}", "sender": f"bot:{F}/mgr",
               "recipient_raw": bot, "message_class": cls, "command_type": cmd,
               "body": "a note"}
    if alias:
        payload["recipient"] = f"bot:{F}/{bot}"
    out = emit_batch(root, [{"event_type": "communication", "emitter": "dispatch-task",
                             "fleet": F, "source_ref": "dispatch-log:sha:" + "%032x" % n,
                             "occurred_at": ts, "payload": payload}])
    assert all(o.status == "committed" for o in out), out


def _ids(root, task_id):
    """(work_item_id, assignment_id) of a dispatched task, read off the plane."""
    with _ro(root) as conn:
        return tuple(conn.execute("SELECT work_item_id, assignment_id FROM assignments"
                                  " WHERE source_ref = ?", (f"dispatch-log:{task_id}",)).fetchone())


def _head(root, bot, at=None):
    pr = _stdlib_readers()
    with _ro(root) as conn:
        return pr.head(conn, F, bot, at)


# --- the guard, on the resolver itself ---------------------------------------

@pytest.mark.parametrize("cmd,cls", CONTROL)
def test_an_unanswered_control_note_makes_the_resolver_answer_nothing(tmp_path, cmd, cls):
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z", cmd=cmd, cls=cls)   # after w1's open t-2 (10:00)
    r = _matcher(root, "--open-task", "w1", "--fleet", F)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "", f"a {cmd} note let the resolver hand back {r.stdout.strip()!r}"
    assert _head(root, "w1") is None
    assert _head(root, "w2") == "t-3-cccc"                       # the guard is per bot


def test_an_id_less_report_after_the_note_releases_the_guard(tmp_path):
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z")
    _report(root, None, None, "2026-09-02T11:30:00Z", event=None, status="progress")
    assert _head(root, "w1") == "t-2-bbbb"


def test_a_task_sent_after_the_note_does_not_release_the_guard(tmp_path):
    """Gap order 2 on the resolver: a newer task is not an answer to the note."""
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z")
    _live_dispatch(root, "8", "t-8-eeee", ts="2026-09-02T11:30:00Z")
    assert _head(root, "w1") is None, "a newer task released the hold"


def test_a_report_naming_a_task_does_not_release_the_guard(tmp_path):
    """Gap order 1 on the resolver: a `--task` report on other work is not an
    answer to the note either."""
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z")
    _report(root, *_ids(root, "t-2-bbbb"), "2026-09-02T11:30:00Z", event="progress")
    assert _head(root, "w1") is None, "a report naming a task released the hold"


def test_a_note_after_an_answered_one_holds_again(tmp_path):
    """ANY unanswered note holds, so the NEWEST note decides: an id-less report
    after it is after every older one. The first note's answer does not cover
    the second."""
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z")
    _report(root, None, None, "2026-09-02T11:30:00Z", event=None, status="completed")
    assert _head(root, "w1") == "t-2-bbbb"                       # answered: the resolver is back
    _control_note(root, 2, ts="2026-09-02T13:00:00Z")
    assert _head(root, "w1") is None


def test_the_doors_fallback_shape_is_guarded_too(tmp_path):
    """The door records only `recipient_raw` when it cannot resolve the worker's
    bot dir; that note is still this bot's."""
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z", alias=False)
    assert _head(root, "w1") is None


def test_another_bots_note_or_report_changes_nothing_here(tmp_path):
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T12:30:00Z", bot="w2")
    assert _head(root, "w1") == "t-2-bbbb" and _head(root, "w2") is None
    _control_note(root, 2, ts="2026-09-02T13:00:00Z", bot="w1")
    _report(root, None, None, "2026-09-02T13:30:00Z", bot="w2", event=None, status="progress")
    assert _head(root, "w1") is None                             # w2 reporting does not answer w1's note
    assert _head(root, "w2") == "t-3-cccc"                       # and does answer its own


def test_the_guard_answers_as_of_an_instant(tmp_path):
    root, *_ = _scene(tmp_path)
    _control_note(root, 1, ts="2026-09-02T11:00:00Z")
    assert _head(root, "w1", "2026-09-02T10:30:00+00:00") == "t-2-bbbb"    # before the note
    assert _head(root, "w1", "2026-09-02T11:30:00+00:00") is None


# --- the doors, end to end: tonight's sequence --------------------------------

def _status(tmp_path, asg):
    return {r[0]: r[1] for r in _rows(tmp_path, TASK_STATUS_SQL)}[asg]


def test_without_a_note_an_idless_report_still_closes_the_task(tmp_path, armed):
    """The positive control: #835's auto-resolve is unchanged, so the check
    below can tell an open task from a closed one."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "done"', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, task["plane_assignment_id"]) == "completed"


def test_answering_a_query_note_no_longer_closes_the_live_task(tmp_path, armed):
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "a note"', env).returncode == 0
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "ack"', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, task["plane_assignment_id"]) == "open"


def test_a_task_report_between_the_note_and_its_answer_does_not_release_the_hold(tmp_path, armed):
    """Gap order 1, end to end: the note, then a `--task` progress report on the
    live task, then the id-less answer. The answer must not close the task."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "a note"', env).returncode == 0
    r = _bash(f'"{libdir}/report-back.sh" w1 progress "on it" --task {task["task_id"]}', env)
    assert r.returncode == 0, r.stderr
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "ack the note"', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, task["plane_assignment_id"]) == "progress"   # never `completed`


def test_a_newer_task_between_the_note_and_its_answer_does_not_release_the_hold(tmp_path, armed):
    """Gap order 2, end to end: the note, then a newer task, then the id-less
    answer. The answer must close neither task."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    first = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "a note"', env).returncode == 0
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "newer work"', env).returncode == 0
    newer = _plane_row(tmp_path)
    assert newer["task_id"] != first["task_id"]
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "ack the note"', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, first["plane_assignment_id"]) == "open"
    assert _status(tmp_path, newer["plane_assignment_id"]) == "open"


def test_the_resolver_fires_again_once_the_note_has_its_id_less_answer(tmp_path, armed):
    """The positive control for both gap orders: once no note is outstanding,
    an id-less report resolves as #835 always did. A hold that never released
    would pass the two tests above and fail this one."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "a note"', env).returncode == 0
    assert _bash(f'"{libdir}/report-back.sh" w1 completed "ack the note"', env).returncode == 0
    assert _status(tmp_path, task["plane_assignment_id"]) == "open"
    assert _bash(f'"{libdir}/report-back.sh" w1 completed "real work done"', env).returncode == 0
    assert _status(tmp_path, task["plane_assignment_id"]) == "completed"


def test_a_note_answer_still_closes_an_open_idless_row(tmp_path, armed):
    """The guard holds back the #835 RESOLUTION only. A raw-text send stays on
    its documented bot+time contract: any terminal report closes it."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" w1 "raw work"', env).returncode == 0
    raw = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" --type query w1 "a note"', env).returncode == 0
    assert _bash(f'"{libdir}/report-back.sh" w1 completed "ack"', env).returncode == 0
    assert _status(tmp_path, raw["plane_assignment_id"]) == "completed"


# --- the opt-out ---------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--no-task", "--task -"])
def test_no_task_holds_the_resolver_back_for_a_note_the_plane_never_saw(tmp_path, armed, flag):
    """A raw dispatch.sh send records no dispatch communication (only its
    send-health events), so the guard cannot see it and the resolver would
    stamp the live task onto the answer. `--no-task` is the worker saying so."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch.sh" w1 "a raw note"', env).returncode == 0
    head = _bash(f'python3 "{libdir}/dispatch-overdue.py" --open-task w1'
                 f' --fleet e2e-fleet --root "{tmp_path}"', env)
    assert head.stdout.strip() == task["task_id"], head    # without --no-task this report closes it
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "ack the raw note" {flag}', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, task["plane_assignment_id"]) == "open"


@pytest.mark.parametrize("flag", ["--no-task", "--task -"])
def test_a_report_that_answers_no_dispatch_closes_nothing(tmp_path, armed, flag):
    """`--no-task` closes no raw id-less row either: a report that answers no
    dispatch answers none of them, and its marker says why nothing closed."""
    libdir, env = armed
    assert _bash(f'"{libdir}/dispatch-task.sh" --botcommand w1 "real work"', env).returncode == 0
    task = _plane_row(tmp_path)
    assert _bash(f'"{libdir}/dispatch-task.sh" w1 "raw work"', env).returncode == 0
    raw = _plane_row(tmp_path)
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "ack" {flag}', env)
    assert r.returncode == 0, r.stderr
    assert _status(tmp_path, task["plane_assignment_id"]) == "open"
    assert _status(tmp_path, raw["plane_assignment_id"]) == "open"
    marks = _rows(tmp_path, "SELECT json_extract(detail, '$.status'), json_extract(detail, '$.no_task')"
                            " FROM events WHERE kind = 'system' AND event = 'report_status'")
    assert [tuple(m) for m in marks] == [("completed", 1)]       # the status still lands, and says why nothing closed


def test_no_task_beside_a_task_id_is_refused(tmp_path, armed):
    libdir, env = armed
    r = _bash(f'"{libdir}/report-back.sh" w1 completed "x" --no-task --task t-1-aaaa', env)
    assert r.returncode == 2 and "--no-task" in r.stderr
    db = tmp_path / "state" / "plane" / "plane.db"
    assert not db.exists() or _rows(tmp_path, "SELECT count(*) FROM communications")[0][0] == 0
