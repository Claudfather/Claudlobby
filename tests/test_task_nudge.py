"""`claudlobby task nudge` — the operator's act on ONE task (chunk M-A, #1481).

Two halves that must NOT fail symmetrically: the plane fact is written first
and a failed send never unwrites it (a nudge nobody delivered is still a nudge
the operator made, and the card carries it), while a nudge the plane refused
sends nothing at all — a delivered nudge with no trace is the one shape the
plane must never produce.

Everything here drives `cmd_task_nudge`, the real door, and reads the plane
back (the fold's F14): the earlier version asserted a private SQL constant,
which is the one thing that stays green while the door itself is wrong.
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
    def __init__(self, root, task_id, why="", as_who=None, assignment=None):
        self.root, self.fleet, self.seed = str(root), None, False
        self.task_id, self.why, self.as_who = task_id, why, as_who
        self.assignment = assignment


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


def _comms(root):
    conn = connect(db_path(root))
    rows = [dict(r) for r in conn.execute(
        "SELECT c.msg_id, c.sender_alias, c.recipient_alias, c.message_class,"
        " c.command_type, c.body, c.work_item_id, f.alias AS fleet"
        " FROM communications c"
        " LEFT JOIN identity_registry f ON f.uid = c.fleet_uid"
        " ORDER BY c.ingest_seq")]
    conn.close()
    return rows


def _transmissions(root):
    conn = connect(db_path(root))
    rows = [dict(r) for r in conn.execute(
        "SELECT msg_id, event, carrier, detail FROM events"
        " WHERE kind='transmission' ORDER BY ingest_seq")]
    conn.close()
    return rows


@pytest.fixture()
def sent(monkeypatch):
    """The manager send, captured rather than made: the CLI runs on the HOST,
    so its only send door is `lib/dispatch.sh` against a live tmux server."""
    calls = []

    def fake(paths, bot, message, fleet=None):
        calls.append((bot, message, fleet))
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
    bot, message, fleet = sent[0]
    assert bot == "erlich"
    assert fleet == F                     # F4: the ROW's fleet, never a guess
    assert message.startswith("NUDGE from chris: task " + tid)
    assert "port the parser" in message and "assignee ramanujan" in message
    for verb in ("chase", "supersede", "withdraw", "escalate"):
        assert verb in message
    assert f"task-act.sh withdraw {tid}" in message
    assert f"dispatch-task.sh --supersedes {tid}" in message


def test_the_ask_is_recorded_as_a_communication_and_a_submitted_transmission(tmp_path, sent, monkeypatch):
    """FOLD F3. The re-check reached the manager's pane and landed NOWHERE:
    the plane held a nudge with no trace of anyone being asked to act on it,
    so a manager who never answered was indistinguishable from one who was
    never asked. The ask is a communication (id-less — a task_request that
    opens no row) threaded under the work item, and the carrier fact follows
    the send."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    _full_capture(tmp_path)
    tid = _seed(tmp_path)
    assert task_cmd.cmd_task_nudge(
        _Args(tmp_path, tid, why="any movement?", as_who="chris")) == 0

    asks = [c for c in _comms(tmp_path) if c["message_class"] == "task_request"]
    assert len(asks) == 1
    ask = asks[0]
    assert ask["sender_alias"] == "human:chris"          # the telegram-in precedent
    assert ask["recipient_alias"] == f"bot:{F}/erlich"
    assert ask["command_type"] == "query"
    assert ask["work_item_id"] == WI                     # threads under the task
    assert ask["fleet"] == F
    assert "NUDGE from chris" in ask["body"]

    tx = _transmissions(tmp_path)
    assert [(t["event"], t["carrier"]) for t in tx] == [("pane_submitted", "tmux")]
    assert tx[0]["msg_id"] == ask["msg_id"]


def test_a_failed_send_records_the_failure_rather_than_claiming_delivery(tmp_path, monkeypatch, capsys):
    """The fact is written FIRST and a send failure never unwrites it: the
    nudge stands, shows on the card, and M4's timer will carry it. Loud (rc 1),
    not silent, and not a rollback — and the carrier fact is HONEST (F3): a
    `pane_submitted` on a send that returned 1 is exactly the fabrication that
    makes recording the ask worthless."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    monkeypatch.setattr(task_cmd, "send_to_bot",
                        lambda paths, bot, message, fleet=None: (1, "session not found"))
    tid = _seed(tmp_path)
    rc = task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris"))
    assert rc == 1
    assert [e for e, _, _ in _task_events(tmp_path)] == ["nudged"]
    tx = _transmissions(tmp_path)
    assert [t["event"] for t in tx] == ["failed"]
    assert "session not found" in (tx[0]["detail"] or "")
    # the ask itself is still recorded — the question was asked, not delivered
    assert [c["message_class"] for c in _comms(tmp_path)] == ["task_request"]
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
    refuses and names the candidates — `task-act.sh`'s rule, one door over —
    and (fold F5) names a REMEDY the caller can actually run."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    tid = _seed(tmp_path)
    _seed(tmp_path, task_id=tid, stem="c" * 32, bot="knuth", mgr="gilfoyle")
    assert task_cmd.cmd_task_nudge(_Args(tmp_path, tid, as_who="chris")) == 2
    assert sent == [] and _task_events(tmp_path) == []
    err = capsys.readouterr().err
    assert "matches 2 open assignments" in err
    assert ASG in err and "asg_" + "c" * 32 in err
    assert "--assignment <asg_id>" in err


def test_naming_the_assignment_resolves_the_ambiguity(tmp_path, sent, monkeypatch):
    """FOLD F5, the other half. `--assignment` NARROWS the task id's own open
    set rather than querying by assignment, so the row acted on provably
    carries the id the caller named."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    tid = _seed(tmp_path)
    twin = "asg_" + "c" * 32
    _seed(tmp_path, task_id=tid, stem="c" * 32, bot="knuth", mgr="gilfoyle")
    assert task_cmd.cmd_task_nudge(
        _Args(tmp_path, tid, as_who="chris", assignment=twin)) == 0
    assert [(e, a) for e, _, a in _task_events(tmp_path)] == [("nudged", twin)]
    assert sent[0][0] == "gilfoyle"          # THAT row's manager

    # ...and an assignment that is not one of this task's rows is not a match
    assert task_cmd.cmd_task_nudge(
        _Args(tmp_path, tid, as_who="chris", assignment="asg_" + "d" * 32)) == 2


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


def test_operator_text_is_collapsed_before_it_reaches_a_pane(tmp_path, sent, monkeypatch):
    """FOLD F9. `lib/dispatch.sh` sends through tmux `send-keys`, where a
    NEWLINE in the payload is a RETURN: a `why` containing one submitted
    everything before it and left the rest — here `/exit` — sitting at the
    manager's prompt (reproduced). Collapsed at the door, so no caller has to
    remember."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    _full_capture(tmp_path)
    tid = _seed(tmp_path, title="port the\nparser")
    assert task_cmd.cmd_task_nudge(
        _Args(tmp_path, tid, why="stalled?\n/exit", as_who="chris")) == 0
    _bot, message, _fleet = sent[0]
    assert "\n" not in message and "\t" not in message
    assert "stalled? /exit" in message
    assert json.loads(_task_events(tmp_path)[0][1])["reason"] == "stalled? /exit"


@pytest.mark.parametrize("who", ["chris\nrm -rf /", "bot:eng/erlich", "a" * 65, "", "  "])
def test_an_unusable_as_name_is_refused_before_anything_is_minted(tmp_path, sent, monkeypatch, who):
    """FOLD F9. `--as` mints `human:<who>` as a plane identity the registry
    keeps forever, so an arbitrary string is not free text: a newline, a
    forged `bot:` namespace or a whole sentence would each become an actor
    nobody can name again."""
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)
    tid = _seed(tmp_path)
    args = _Args(tmp_path, tid, as_who=who)
    if who.strip() == "":
        # empty/blank falls back to $USER — a real name, not a refusal
        monkeypatch.setenv("USER", "chris")
        assert task_cmd.cmd_task_nudge(args) == 0
        return
    assert task_cmd.cmd_task_nudge(args) == 2
    assert sent == [] and _task_events(tmp_path) == []
