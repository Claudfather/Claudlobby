"""#1981: a control note holds the private resolver back again.

Since #1491 a `query` / `cancel` / `compact` / `restart` note lands the
COMMUNICATION alone, with no assignment. The resolver's only guard keyed on the
bot's newest ASSIGNMENT (`answering_idless`), so a note no longer held it back:
the worker's id-less answer was stamped with its live task and closed it as
`completed` (ravi's #917 row, 2026-09-29 04:23:57Z).

The guard now reads the newest CONTROL NOTE sent to the bot: while it has no
id-less report from the bot after it, `head()` returns None. A report naming
one of the bot's own tasks, and a newer task, leave it standing: neither
answers a note, and a wrong completion is worse than an open row (the ruling
on #1984). The note fixture is the LIVE shape: the 04:20:32Z note carried a
`dispatch-log:sha:` ref, message_class `question`, command_type `query`, a
recipient alias AND `recipient_raw`, and no work item. The door's disclosed
fallback (no alias) is pinned too.

The public CLI resolves nothing: a linked report names its task through
`task report`, and `fleet reports submit` is the explicitly unlinked report
(#1984's `--no-task`), so no report door auto-closes work. The retired
`report-back.sh` end-to-end cases stay retired with it; the unlinked report's
no-task-effect contract is pinned in `tests/test_message_write_cli.py`.
"""

from __future__ import annotations

import pytest

from claudlobby.plane.emit_api import emit_batch
from tests.plane_fixtures import (F, _live_dispatch, _matcher, _report, _scene,
                                  _stdlib_readers, ro as _ro)

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
    """Gap order 1 on the resolver: a linked report on other work is not an
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
    """A note recorded with only `recipient_raw` is still this bot's."""
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
