"""`claudlobby task recheck` — the task loop's re-check trigger (chunk M-B, #1481).

M4 is the estate's first CLOCK-driven reaction to a plane fact: every N hours,
each manager is handed the rows of theirs that stopped moving, with the four
verbs, and is asked to say what it did with each.

Everything here drives `cmd_task_recheck`, the real door, and reads the plane
back (M-A's F14 lesson: a test that asserts a private SQL constant is the one
thing that stays green while the door itself is wrong). The send is the only
seam replaced — the CLI runs on the HOST, so its send door is `lib/dispatch.sh`
against a live tmux server.

The two properties the whole chunk rests on:

  * the DEBOUNCE IS A PLANE READ. A row is skipped because a communication
    stamped `task-recheck:<assignment_id>` names it, not because a state file
    says so — so the window survives a host that lost its state dir, and a row
    the plane never recorded an ask for comes back rather than going quiet.
  * a row is asked about ONLY by the manager who dispatched it (`assigned_by`),
    and never by a fleet default.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claudlobby.commands import task as task_cmd
from claudlobby.plane.db import connect, db_path
from claudlobby.plane.emit_api import emit_batch

F = "recheck-fleet"
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def root(tmp_path: Path) -> Path:
    """A claudlobby root carrying the REAL matcher and the REAL stdlib readers
    in lib/ (test_brief.py's fixture, for its reason): the door reads the plane
    through the INSTALL's own doors, and a stub would sever exactly the shared
    definition of "open" this chunk depends on."""
    (tmp_path / "lib").mkdir()
    for name in ("dispatch-overdue.py", "plane-readers.py", "plane-lookup.py"):
        shutil.copy(REPO_ROOT / "lib" / name, tmp_path / "lib" / name)
    return tmp_path


class _Args:
    def __init__(self, root, *, fleet=None, max_age_h=48.0, repeat_h=24.0,
                 dry_run=False):
        self.root, self.fleet, self.seed = str(root), None, False
        self.recheck_fleet = fleet or F
        self.max_age_h, self.repeat_h, self.dry_run = max_age_h, repeat_h, dry_run


def _full_capture(root) -> None:
    d = root / "state" / "plane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.json").write_text('{"*": "full"}')


def _iso(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ago(hours: float) -> str:
    from datetime import datetime, timedelta, timezone
    return _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


def _ahead(hours: float) -> str:
    from datetime import datetime, timedelta, timezone
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def _stem(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


def _seed_row(root, *, task_id, bot="ramanujan", mgr="erlich", fleet=F,
              title="port the parser", dispatched, expected_by=None):
    """One dispatched row on the plane, at the instants given."""
    stem = _stem(task_id)
    wi, asg = "wi_" + stem, "asg_" + stem
    base = {"emitter": "t", "fleet": fleet, "source_ref": f"dispatch-log:{task_id}",
            "occurred_at": dispatched}
    payload = {"assignment_id": asg, "work_item_id": wi,
               "assignee": f"bot:{fleet}/{bot}", "assigned_by": f"bot:{fleet}/{mgr}"}
    if expected_by:
        payload["expected_by"] = expected_by
    emit_batch(root, [
        {**base, "event_type": "work_item",
         "payload": {"work_item_id": wi, "title": title,
                     "created_by": f"bot:{fleet}/{mgr}"}},
        {**base, "event_type": "assignment", "payload": payload},
    ])
    return asg


def _seed_task_event(root, *, task_id, event, at, fleet=F, actor="bot:x/y", **detail):
    stem = _stem(task_id)
    emit_batch(root, [{
        "event_type": "task", "emitter": "t", "fleet": fleet,
        "source_ref": f"dispatch-log:{task_id}", "occurred_at": at,
        "payload": {"work_item_id": "wi_" + stem, "assignment_id": "asg_" + stem,
                    "event": event, "actor": actor, **detail},
    }])


def _comms(root):
    conn = connect(db_path(root))
    rows = [dict(r) for r in conn.execute(
        "SELECT c.msg_id, c.source_ref, c.sender_alias, c.recipient_alias,"
        " c.message_class, c.command_type, c.assignment_id, c.body"
        " FROM communications c ORDER BY c.ingest_seq")]
    conn.close()
    return rows


def _transmissions(root):
    conn = connect(db_path(root))
    rows = [dict(r) for r in conn.execute(
        "SELECT msg_id, event FROM events WHERE kind='transmission'"
        " ORDER BY ingest_seq")]
    conn.close()
    return rows


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def fake(paths, bot, message, fleet=None):
        calls.append((bot, message, fleet))
        return 0, ""

    monkeypatch.setattr(task_cmd, "send_to_bot", fake)
    return calls


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.delenv("PLANE_EMIT_DISABLED", raising=False)


# --- what the re-check sends -------------------------------------------------


def test_a_stale_row_reaches_its_own_manager_with_the_menu(tmp_path, sent):
    _full_capture(tmp_path)
    _seed_row(tmp_path, task_id="t-stale", dispatched=_ago(30),
              expected_by=_ago(6))
    # ...and a healthy row of the SAME manager: dispatched an hour ago, due
    # tomorrow. It must not be named — a re-check that lists everything open
    # is the wall of text a manager stops reading.
    _seed_row(tmp_path, task_id="t-fresh", dispatched=_ago(1),
              expected_by=_ahead(23), title="still running")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 1
    bot, message, fleet = sent[0]
    assert bot == "erlich"                       # assigned_by, the row's own manager
    assert fleet == F
    assert "t-stale" in message and "t-fresh" not in message
    assert "port the parser" in message and "assignee ramanujan" in message
    assert "deadline passed" in message
    for verb in task_cmd.TASK_VERBS:
        assert verb in message
    # the exact commands, not advice
    assert "task-act.sh withdraw <task-id> --reason" in message
    assert "dispatch-task.sh --supersedes <task-id>" in message
    assert "dispatch-task.sh --type query" in message
    assert "\n" not in message                   # tmux reads a newline as RETURN


def test_each_manager_gets_its_own_message(tmp_path, sent):
    _seed_row(tmp_path, task_id="t-a", mgr="erlich", dispatched=_ago(30),
              expected_by=_ago(6))
    _seed_row(tmp_path, task_id="t-b", mgr="gilfoyle", bot="dinesh",
              dispatched=_ago(30), expected_by=_ago(6))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert sorted(b for b, _, _ in sent) == ["erlich", "gilfoyle"]
    by_bot = {b: m for b, m, _ in sent}
    assert "t-a" in by_bot["erlich"] and "t-b" not in by_bot["erlich"]
    assert "t-b" in by_bot["gilfoyle"] and "t-a" not in by_bot["gilfoyle"]


def test_the_row_facts_the_manager_needs_ride_the_line(tmp_path, sent):
    """The menu is only useful over the facts: is anyone waiting on the human,
    has anyone poked it, has it moved at all."""
    _full_capture(tmp_path)
    _seed_row(tmp_path, task_id="t-facts", dispatched=_ago(30), expected_by=_ago(6))
    _seed_task_event(tmp_path, task_id="t-facts", event="progress", at=_ago(20),
                     actor=f"bot:{F}/ramanujan")
    _seed_task_event(tmp_path, task_id="t-facts", event="escalated", at=_ago(3),
                     actor=f"bot:{F}/erlich", by="erlich",
                     question="do we ship without the migration")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    message = sent[0][1]
    assert "ESCALATED by erlich" in message
    assert "do we ship without the migration" in message
    assert "last progress 20h ago" in message


def test_an_escalation_survives_a_later_nudge_on_the_line(tmp_path, sent):
    """The M-A fold's F1, on this surface: a nudge is an ASK, not an ANSWER.
    Read through one window the nudge would displace the raise and the line
    would stop saying the human owes an answer."""
    _full_capture(tmp_path)
    _seed_row(tmp_path, task_id="t-both", dispatched=_ago(30), expected_by=_ago(6))
    _seed_task_event(tmp_path, task_id="t-both", event="escalated", at=_ago(5),
                     actor=f"bot:{F}/erlich", by="erlich", question="which repo")
    _seed_task_event(tmp_path, task_id="t-both", event="nudged", at=_ago(1),
                     actor="human:chris", by="chris", reason="any movement")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    message = sent[0][1]
    assert "ESCALATED by erlich" in message and "which repo" in message
    assert "nudged by chris" in message


# --- what the re-check records (and how the debounce reads it) ---------------


def test_the_ask_is_recorded_per_row_stamped_and_transmitted(tmp_path, sent):
    _full_capture(tmp_path)
    asg = _seed_row(tmp_path, task_id="t-rec", dispatched=_ago(30), expected_by=_ago(6))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    asks = [c for c in _comms(tmp_path) if c["message_class"] == "task_request"]
    assert len(asks) == 1
    ask = asks[0]
    assert ask["source_ref"] == f"task-recheck:{asg}"      # THE debounce stamp
    assert ask["sender_alias"] == "system:task-recheck"    # machinery, not a human
    assert ask["recipient_alias"] == f"bot:{F}/erlich"
    assert ask["command_type"] == "query"                  # asks, never mints a task
    assert ask["assignment_id"] == asg                     # threads under the row
    assert "t-rec" in ask["body"]
    tx = _transmissions(tmp_path)
    assert [t["event"] for t in tx] == ["pane_submitted"]
    assert tx[0]["msg_id"] == ask["msg_id"]


def test_a_failed_send_is_recorded_as_failed_never_as_submitted(tmp_path, monkeypatch):
    _full_capture(tmp_path)
    _seed_row(tmp_path, task_id="t-down", dispatched=_ago(30), expected_by=_ago(6))
    monkeypatch.setattr(task_cmd, "send_to_bot",
                        lambda *a, **k: (1, "session not found"))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 1
    tx = _transmissions(tmp_path)
    assert [t["event"] for t in tx] == ["failed"]
    # ...and the ask still stands, so the row is not re-asked into the void
    assert len([c for c in _comms(tmp_path)
                if c["message_class"] == "task_request"]) == 1


def test_a_second_run_inside_the_repeat_window_sends_nothing(tmp_path, sent, capsys):
    _seed_row(tmp_path, task_id="t-twice", dispatched=_ago(30), expected_by=_ago(6))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 1
    capsys.readouterr()
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 1                                  # the row was already named
    assert "re-checked inside the last 24h" in capsys.readouterr().out


def test_the_window_expiring_lets_the_row_through_again(tmp_path, sent):
    _seed_row(tmp_path, task_id="t-again", dispatched=_ago(30), expected_by=_ago(6))
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    # a window shorter than the ask is old — the stamp is an INSTANT on the
    # plane, so the window is arithmetic over it, not a file's mtime
    assert task_cmd.cmd_task_recheck(_Args(tmp_path, repeat_h=0)) == 0
    assert len(sent) == 2


def test_the_window_holds_only_the_rows_it_named(tmp_path, sent):
    """A per-manager debounce would make a NEW stale row wait out the window
    behind a row that was already asked about."""
    _seed_row(tmp_path, task_id="t-old", dispatched=_ago(30), expected_by=_ago(6))
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    _seed_row(tmp_path, task_id="t-new", dispatched=_ago(30), expected_by=_ago(6),
              title="the second one")
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 2
    assert "t-new" in sent[1][1] and "t-old" not in sent[1][1]


def test_the_cap_stamps_only_the_rows_it_named(tmp_path, sent, monkeypatch):
    """The cap holds rows over; it must not silently mark them re-checked, or
    a manager with a long tail would never hear about the tail."""
    monkeypatch.setattr(task_cmd, "RECHECK_MAX_ROWS", 2)
    for i in range(4):
        _seed_row(tmp_path, task_id=f"t-cap{i}", dispatched=_ago(30 + i),
                  expected_by=_ago(6))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 1
    assert sent[0][1].count("] task t-cap") == 2
    assert "+2 more of yours are stale" in sent[0][1]
    assert len([c for c in _comms(tmp_path)
                if c["message_class"] == "task_request"]) == 2
    # the held-over rows lead the next run
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 2
    assert sent[1][1].count("] task t-cap") == 2


# --- which rows qualify ------------------------------------------------------


def test_a_deadline_less_row_qualifies_on_age_alone(tmp_path, sent):
    """Every id-less dispatch and every row dispatched before M-A carries no
    deadline; without the age arm they would never be re-checked at all."""
    _seed_row(tmp_path, task_id="t-noddl", dispatched=_ago(60))
    _seed_row(tmp_path, task_id="t-young", dispatched=_ago(2), title="young")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert "t-noddl" in sent[0][1] and "t-young" not in sent[0][1]


def test_a_closed_row_is_never_re_checked(tmp_path, sent, capsys):
    _seed_row(tmp_path, task_id="t-done", dispatched=_ago(30), expected_by=_ago(6))
    _seed_task_event(tmp_path, task_id="t-done", event="completed", at=_ago(1),
                     actor=f"bot:{F}/ramanujan")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert sent == []
    assert "nothing sent" in capsys.readouterr().out


def test_a_withdrawn_row_is_never_re_checked(tmp_path, sent):
    """M-A's `cancelled` is terminal for every reader — including this one."""
    _seed_row(tmp_path, task_id="t-gone", dispatched=_ago(30), expected_by=_ago(6))
    _seed_task_event(tmp_path, task_id="t-gone", event="cancelled", at=_ago(1),
                     actor=f"bot:{F}/erlich", by="erlich", reason="never landed")

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert sent == []


def test_the_scope_is_the_dispatching_fleet_not_the_assignee(tmp_path, sent):
    """`escalated_rows`'s rule, and for its reason: the row belongs to whoever
    dispatched it. 44.6% of dispatch traffic is cross-fleet, so scoping by the
    assignee's fleet would hand a manager's rows to another fleet's timer and
    hide them from their own."""
    _seed_row(tmp_path, task_id="t-cross", bot="outsider", mgr="erlich",
              dispatched=_ago(30), expected_by=_ago(6), title="ours, their bot")
    # ...and a row ANOTHER fleet dispatched (its own manager owes the re-check)
    _seed_row(tmp_path, task_id="t-theirs", fleet="other-fleet", mgr="stranger",
              bot="ramanujan", dispatched=_ago(30), expected_by=_ago(6))

    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 0
    assert len(sent) == 1
    assert sent[0][0] == "erlich"
    assert "t-cross" in sent[0][1] and "t-theirs" not in sent[0][1]


# --- refusals ----------------------------------------------------------------


def test_plane_emit_disabled_refuses_and_sends_nothing(tmp_path, sent, monkeypatch):
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    _seed_row(tmp_path, task_id="t-silent", dispatched=_ago(30), expected_by=_ago(6))
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 3
    assert sent == []


def test_an_unreachable_plane_refuses_rather_than_reporting_a_quiet_fleet(
        tmp_path, sent, capsys):
    assert task_cmd.cmd_task_recheck(_Args(tmp_path)) == 3
    assert sent == []
    assert "unreachable, not empty" in capsys.readouterr().err


def test_a_fleet_the_plane_never_saw_refuses(tmp_path, sent, capsys):
    _seed_row(tmp_path, task_id="t-x", dispatched=_ago(30), expected_by=_ago(6))
    assert task_cmd.cmd_task_recheck(_Args(tmp_path, fleet="no-such-fleet")) == 3
    assert sent == []


def test_dry_run_records_nothing_and_sends_nothing(tmp_path, sent, capsys):
    _seed_row(tmp_path, task_id="t-dry", dispatched=_ago(30), expected_by=_ago(6))
    assert task_cmd.cmd_task_recheck(_Args(tmp_path, dry_run=True)) == 0
    assert sent == []
    assert [c for c in _comms(tmp_path) if c["message_class"] == "task_request"] == []
    out = capsys.readouterr().out
    assert "[dry-run] erlich: 1 row(s)" in out and "t-dry" in out


# --- the one stamp -----------------------------------------------------------


def test_the_stamp_prefix_is_the_readers_own(tmp_path):
    """The emitter is the package and the reader is a stdlib script a bash door
    shells; they cannot share a constant, so they are TWINS and this is the
    pin. A fork does not crash — it silently disables the debounce and re-asks
    a manager every sweep."""
    from tests.conftest import load_lib_module

    assert (task_cmd.RECHECK_REF_PREFIX
            == load_lib_module("plane-readers").RECHECK_REF_PREFIX)


def test_a_row_with_no_nameable_manager_is_disclosed_never_reassigned():
    """`assigned_by` is the plane's own fact about who owns a row. A fleet
    default would send the re-check to someone who never dispatched it — the
    #526 shape, and worse here because the ask carries four destructive verbs.
    Driven at the grouping seam: ingest requires `assigned_by`, so the plane
    cannot be made to produce this row, and the branch would otherwise be
    unreachable code claiming to handle it."""
    ownerless = {"assignment_id": "asg_" + "d" * 32, "assigned_by": None,
                 "task_id": "t-orphan"}
    by, orphans = task_cmd._group_by_manager([ownerless])
    assert by == {} and orphans == [ownerless]


def test_a_row_whose_dispatch_instant_is_unreadable_is_not_due():
    """Every phrase the line makes is a clock claim about the row. With no
    readable instant none of them is true, so the row is left alone rather than
    described wrongly — the `_age` = "age unknown" rule, one level up."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    assert not task_cmd.row_is_due({"occurred_at": "not-an-instant"},
                                   now=now, max_age_s=1)
    assert not task_cmd.row_is_due({"occurred_at": None}, now=now, max_age_s=1)
