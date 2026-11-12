"""A bot's current task is its open assignment by state, then by its latest
transition, and more than one open assignment is said, never picked silently
(#2179).

`fleet status` and `utilization` read `current_task` from `read_fleet_work`.
It used to take the open assignment with the lowest task id, and task ids are
random, so an assignment recorded but never delivered could read as the work
in progress while the bot worked on another.
"""

from __future__ import annotations

import json

import pytest

from claudlobby import status as status_mod
from claudlobby.task_work_queries import BotWork, CurrentWork, read_fleet_work
from claudlobby.utilization import compute_bot_utilization
from tests.test_task_state import _insert, conn  # noqa: F401 — the migrated in-memory plane


def _bot(conn, name="alice"):
    conn.execute("INSERT INTO identity_registry (uid, kind, alias, provisional, first_seen,"
                 " last_seen) VALUES (?, 'actor', ?, 0, 't', 't')", (f"actor_{name}", f"bot:f/{name}"))


def _open(conn, tid, title, *events, aid=None):
    """One open assignment of alice's: its task, the assignment, then its events,
    each landing after the last (the Plane's ingest order is its time order)."""
    aid = aid or "asg_" + tid[3:]
    _insert(conn, "work_items", work_item_id=tid, title=title, created_by_uid="actor_alice")
    _insert(conn, "assignments", assignment_id=aid, work_item_id=tid,
            assignee_uid="actor_alice", assigned_by_uid="actor_manager")
    for event in events:
        _event(conn, tid, event, aid=aid)


def _event(conn, tid, event, aid=None):
    _insert(conn, "events", kind="task", work_item_id=tid, assignment_id=aid or "asg_" + tid[3:],
            event=event)


def _work(conn) -> BotWork:
    return read_fleet_work(conn, fleet_uid="fleet_a", fleet="f", bot_names=["alice"]).bots["alice"]


@pytest.mark.parametrize("active, assigned", [("wi_aaaa", "wi_bbbb"), ("wi_bbbb", "wi_aaaa")])
def test_an_active_assignment_is_current_whatever_the_task_ids(conn, active, assigned):  # noqa: F811
    """The live case: the bot works one assignment, and a second is recorded but
    not yet delivered. The undelivered one is newer, so state must win over recency."""
    _bot(conn)
    _open(conn, active, "The review in progress", "accepted")
    _open(conn, assigned, "Recorded, not delivered")
    work = _work(conn)
    assert work.current_task == "The review in progress"
    assert [(a.task_id, a.state) for a in work.assignments] == [(active, "active"), (assigned, "assigned")]
    assert work.open_assignments == 2


def test_active_then_blocked_then_assigned_against_the_id_order(conn):  # noqa: F811
    _bot(conn)
    _open(conn, "wi_cccc", "Active", "accepted")
    _open(conn, "wi_bbbb", "Blocked", "accepted", "blocked_waiting")
    _open(conn, "wi_aaaa", "Assigned")
    work = _work(conn)
    assert [a.state for a in work.assignments] == ["active", "blocked", "assigned"]
    assert work.current_task == "Active" and work.open_assignments == 3


@pytest.mark.parametrize("older, newer", [("wi_aaaa", "wi_bbbb"), ("wi_bbbb", "wi_aaaa")])
def test_two_assigned_ones_order_by_their_latest_transition(conn, older, newer):  # noqa: F811
    _bot(conn)
    _open(conn, older, "Assigned first")
    _open(conn, newer, "Assigned second")
    work = _work(conn)
    assert work.current_task == "Assigned second"
    assert [a.task_id for a in work.assignments] == [newer, older]
    assert work.open_assignments == 2


@pytest.mark.parametrize("first, second", [("wi_aaaa", "wi_bbbb"), ("wi_bbbb", "wi_aaaa")])
def test_between_two_active_ones_the_latest_activity_wins(conn, first, second):  # noqa: F811
    """A transition is any of the assignment's events, not only its creation: the
    first accepted, then the second, then the first reported progress."""
    _bot(conn)
    _open(conn, first, "Accepted first, progress since", "accepted")
    _open(conn, second, "Accepted second", "accepted")
    _event(conn, first, "progress")
    work = _work(conn)
    assert work.current_task == "Accepted first, progress since"
    assert [a.task_id for a in work.assignments] == [first, second]


def test_one_open_assignment_counts_one_and_unavailable_work_counts_nothing(conn):  # noqa: F811
    _bot(conn)
    _open(conn, "wi_aaaa", "Only", "accepted")
    assert _work(conn).open_assignments == 1
    unknown = read_fleet_work(conn, fleet_uid="fleet_a", fleet="f", bot_names=["bob"]).bots["bob"]
    assert unknown.unavailable and unknown.current_task is None and unknown.open_assignments is None


def _two_open():
    return (CurrentWork("wi_aaaa", "asg_aaaa", "Review Claudlobby #2174 for a merge", "active"),
            CurrentWork("wi_bbbb", "asg_bbbb", "Review Claudlobby #2176", "assigned"))


def test_status_json_carries_the_count_beside_current_task():
    bs = status_mod.BotStatus(name="alice", state="working", current_task="Review Claudlobby #2174 for a merge",
                              open_assignments=2, work_assignments=_two_open())
    bot = json.loads(status_mod.format_json([bs], "f"))["bots"][0]
    assert bot["current_task"] == "Review Claudlobby #2174 for a merge"
    assert bot["open_assignments"] == 2
    assert [a["state"] for a in bot["work_assignments"]] == ["active", "assigned"]


def test_status_table_and_detail_say_when_more_than_one_is_open():
    bs = status_mod.BotStatus(name="alice", state="working",
                              current_task="Review Claudlobby #2174 for a merge and a long title past the column",
                              open_assignments=2, work_assignments=_two_open())
    table = status_mod._ANSI_RE.sub("", status_mod.format_table([bs], "f"))
    assert "(+1 open)" in table, "the count must survive the column's truncation"
    detail = status_mod._ANSI_RE.sub("", status_mod.format_bot_detail(bs))
    assert "(1 of 2 open)" in detail
    one = status_mod.BotStatus(name="bob", state="working", current_task="Only work", open_assignments=1,
                               work_assignments=_two_open()[:1])
    assert "open)" not in status_mod._ANSI_RE.sub("", status_mod.format_table([one], "f"))


def test_utilization_inherits_the_order_and_the_count():
    work = BotWork(assignments=_two_open())
    util = compute_bot_utilization("alice", [], work=work)
    assert util.current_task == "Review Claudlobby #2174 for a merge"
    assert util.open_assignments == 2


@pytest.mark.parametrize("a, b", [("wi_aaaa", "wi_bbbb"), ("wi_bbbb", "wi_aaaa")])
def test_an_escalation_is_not_the_bots_transition(conn, a, b):  # noqa: F811
    """An escalation carries the assignment id, but it says the task waits on a
    person, not that the bot moved to it, so it does not decide the current task."""
    _bot(conn)
    _open(conn, a, "Progress reported last", "accepted")
    _open(conn, b, "Escalated to a human", "accepted")
    _event(conn, a, "progress")
    _event(conn, b, "escalated")
    assert _work(conn).current_task == "Progress reported last"


@pytest.mark.parametrize("older, newer", [("wi_aaaa", "wi_bbbb"), ("wi_bbbb", "wi_aaaa")])
def test_a_nudge_on_the_older_assigned_row_does_not_make_it_current(conn, older, newer):  # noqa: F811
    """A nudge is the operator asking the manager to act: not the bot's transition."""
    _bot(conn)
    _open(conn, older, "Assigned first")
    _open(conn, newer, "Assigned second")
    _event(conn, older, "nudged")
    assert _work(conn).current_task == "Assigned second"
