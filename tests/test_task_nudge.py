"""`claudlobby task nudge` — the operator's act on ONE task (chunk M-A, #1481).

Two halves that must NOT fail symmetrically: the plane fact is written first
and a failed send never unwrites it (a nudge nobody delivered is still a nudge
the operator made, and the card carries it), while a nudge the plane refused
sends nothing at all — a delivered nudge with no trace is the one shape the
plane must never produce.
"""

from __future__ import annotations

import json

import pytest

from claudlobby.commands import task as task_cmd
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

F = "nudge-fleet"
STEM = "b" * 32
ASG, WI, MSG = "asg_" + STEM, "wi_" + STEM, "msg_" + STEM


class _Args:
    def __init__(self, root, task_id, why="", as_who=None):
        self.root, self.fleet, self.seed = str(root), None, False
        self.task_id, self.why, self.as_who = task_id, why, as_who


def _full_capture(root) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _seed(root, *, task_id="t-1757000000-ab12", stem=STEM, bot="ramanujan",
          mgr="erlich", title="port the parser"):
    base = {"emitter": "t", "fleet": F, "source_ref": f"dispatch-log:{task_id}"}
    emit_batch(root, [
        {**base, "event_type": "work_item",
         "payload": {"work_item_id": "wi_" + stem, "title": title,
                     "created_by": f"bot:{F}/{mgr}"}},
        {**base, "event_type": "assignment",
         "payload": {"assignment_id": "asg_" + stem, "work_item_id": "wi_" + stem,
                     "assignee": f"bot:{F}/{bot}", "assigned_by": f"bot:{F}/{mgr}",
                     "dispatch_msg_id": "msg_" + stem}},
    ])
    return task_id


def _task_events(root):
    conn = connect(db_path(root))
    rows = [(r["event"], r["detail"], r["assignment_id"]) for r in conn.execute(
        "SELECT event, detail, assignment_id FROM events WHERE kind='task'"
        " ORDER BY ingest_seq")]
    conn.close()
    return rows


@pytest.fixture()
def sent(monkeypatch):
    """The manager send, captured rather than made: the CLI runs on the HOST,
    so its only send door is `lib/dispatch.sh` against a live tmux server."""
    calls = []

    def fake(paths, bot, message):
        calls.append((bot, message))
        return 0, ""

    monkeypatch.setattr(task_cmd, "send_to_bot", fake)
    return calls


def test_a_nudge_records_the_fact_and_asks_the_tasks_manager(tmp_path, sent, monkeypatch):
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    _full_capture(tmp_path)
    tid = _seed(tmp_path)
    rc = task_cmd.cmd_task_nudge(_Args(tmp_path, tid, why="any movement?", as_who="chris"))
    assert rc == 0

    events = _task_events(tmp_path)
    assert [e for e, _, _ in events] == ["nudged"]
    detail = json.loads(events[0][1])
    assert detail["by"] == "chris" and detail["reason"] == "any movement?"
    assert events[0][2] == ASG

    # the actor is a FIRST-CLASS human, not the manager it happens to reach
    conn = connect(db_path(tmp_path))
    alias = conn.execute(
        "SELECT i.alias FROM events e JOIN identity_registry i ON i.uid = e.actor_uid"
        " WHERE e.kind='task' AND e.event='nudged'").fetchone()[0]
    conn.close()
    assert alias == "human:chris"

    # ...and the re-check goes to the task's OWN manager (the plane's
    # assigned_by), carrying the four verbs and the row's facts
    assert len(sent) == 1
    bot, message = sent[0]
    assert bot == "erlich"
    assert message.startswith("NUDGE from chris: task " + tid)
    assert "port the parser" in message and "assignee ramanujan" in message
    for verb in ("chase", "supersede", "withdraw", "escalate"):
        assert verb in message
    assert f"task-act.sh withdraw {tid}" in message
    assert f"dispatch-task.sh --supersedes {tid}" in message


def test_a_failed_send_keeps_the_record_and_says_so(tmp_path, monkeypatch, capsys):
    """The fact is written FIRST and a send failure never unwrites it: the
    nudge stands, shows on the card, and M4's timer will carry it. Loud (rc 1),
    not silent, and not a rollback."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    monkeypatch.setattr(task_cmd, "send_to_bot",
                        lambda paths, bot, message: (1, "session not found"))
    tid = _seed(tmp_path)
    rc = task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris"))
    assert rc == 1
    assert [e for e, _, _ in _task_events(tmp_path)] == ["nudged"]
    err = capsys.readouterr().err
    assert "did NOT reach erlich" in err and "stands on the plane" in err


def test_a_silenced_plane_refuses_and_sends_nothing(tmp_path, sent, monkeypatch, capsys):
    """A delivered nudge with no record is the one asymmetry the plane must
    never produce — so the silenced case refuses before the send, not after."""
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    tid = _seed(tmp_path)
    assert task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris")) == 3
    assert sent == []
    assert "nothing was sent" in capsys.readouterr().err


def test_an_id_matching_two_open_assignments_is_refused_by_name(tmp_path, sent, monkeypatch, capsys):
    """A wrong nudge sends a manager to chase the wrong worker, so the door
    refuses and names the candidates — `task-act.sh`'s rule, one door over."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    tid = _seed(tmp_path)
    _seed(tmp_path, task_id=tid, stem="c" * 32, bot="knuth", mgr="gilfoyle")
    assert task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris")) == 2
    assert sent == [] and _task_events(tmp_path) == []
    err = capsys.readouterr().err
    assert "matches 2 open assignments" in err
    assert ASG in err and "asg_" + "c" * 32 in err


def test_a_closed_row_and_an_unreachable_plane_are_different_answers(tmp_path, sent, monkeypatch):
    """unreachable ≠ empty (source_state): rc 3 for a plane that cannot be
    opened, rc 2 for an id with nothing open — the second is an answer."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    assert task_cmd.cmd_task_nudge(_Args(tmp_path, "t-1", as_who="chris")) == 3

    tid = _seed(tmp_path)
    emit_batch(tmp_path, [{
        "event_type": "task", "emitter": "t", "fleet": F,
        "payload": {"work_item_id": WI, "assignment_id": ASG, "event": "completed"}}])
    assert task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris")) == 2
    assert sent == []


def test_the_task_names_its_own_manager_and_the_contract_guarantees_one(tmp_path):
    """The nudge reaches the row's OWN manager (`assigned_by`), never the
    fleet's composed MANAGER_TMUX — and it needs no fallback to that, because
    `assigned_by` is REQUIRED on the Assignment contract. Pinned, since a
    fallback would be unreachable code claiming to handle a case the wire
    model forbids."""
    from claudlobby.plane.contracts import ContractViolation

    _seed(tmp_path, mgr="gilfoyle")
    conn = connect(db_path(tmp_path))
    row = conn.execute(task_cmd.OPEN_BY_TASK_SQL,
                       ("dispatch-log:t-1757000000-ab12",)).fetchone()
    conn.close()
    assert task_cmd.manager_of(row) == "gilfoyle"

    with pytest.raises(ContractViolation):
        emit_batch(tmp_path, [{
            "event_type": "assignment", "emitter": "t", "fleet": F,
            "payload": {"assignment_id": "asg_" + "d" * 32, "work_item_id": WI,
                        "assignee": f"bot:{F}/ramanujan",
                        "dispatch_msg_id": MSG}}])


def test_an_unparseable_instant_reads_age_unknown_never_zero(tmp_path):
    """`0s old` on a stale row would say the task was just dispatched — the
    fabricated-number class. Absence is said as absence."""
    assert task_cmd._age(None) == "age unknown"
    assert task_cmd._age("not-an-instant") == "age unknown"
