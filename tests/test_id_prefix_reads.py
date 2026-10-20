"""The read doors take an ID prefix (#2380), against actual SQL and request files.

A prefix that names exactly one record the caller may read resolves; several are
listed and none is chosen; "0 candidates" is said only after a search by prefix.
The query owners resolve a prefix only when a read door asks (`prefix=True`, or
`resolve_message_reference` before a message read), so a mutation that reads a
task, an assignment or a reply's parent stays canonical. New names are reached
as module attributes, so each test fails on its own where the change is absent.
"""

from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from claudlobby import message_queries as mq
from claudlobby import reference_hints as hints
from claudlobby import request_queries as rq
from claudlobby import task_operations as tasks
from claudlobby import task_queries as tq
from claudlobby.commands import checkins
from claudlobby.plane.db import open_ro
from claudlobby.plane.migrations import migrate
from tests.plane_fixtures import plane_root
from tests.test_checkins_cli import F, _decision
from tests.test_message_queries import ACTORS, FLEETS, communication
from tests.test_message_queries import estate as message_estate  # noqa: F401 — real-SQL message fixture
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture
from tests.test_task_state import _assignment, _task


def _hex(head, fill):
    return head + fill * (32 - len(head))


TASK_AB1, TASK_AB2 = "wi_" + _hex("ab12cd34", "1"), "wi_" + _hex("ab12cd34", "2")
TASK_EF = "wi_" + _hex("ef56ab78", "3")
TASK_FOREIGN = "wi_" + _hex("ef56ab78", "4")
ASG_DA1, ASG_DA2 = "asg_" + _hex("da1585e0", "1"), "asg_" + _hex("da1585e0", "2")
ASG_9F = "asg_" + _hex("9f8e7d6c", "3")


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:", isolation_level=None)
    migrate(db)
    for tid in (TASK_AB1, TASK_AB2, TASK_EF):
        _task(db, tid)
    _task(db, TASK_FOREIGN, fleet_uid="fleet_b")
    _assignment(db, ASG_DA1, TASK_AB1)
    _assignment(db, ASG_DA2, TASK_AB2)
    _assignment(db, ASG_9F, TASK_EF)
    yield db
    db.close()


def test_a_unique_task_or_assignment_prefix_resolves_within_the_fleet(conn):
    # TASK_FOREIGN shares the prefix from another fleet and is no candidate.
    assert tq.show_task(conn, "wi_ef56ab78", fleet_uid="fleet_a", prefix=True).task_id == TASK_EF
    view = tq.show_assignment(conn, "asg_9f8e7d6c", fleet_uid="fleet_a", prefix=True)
    assert (view.assignment.assignment_id, view.task.task_id) == (ASG_9F, TASK_EF)
    assert tq.show_task(conn, TASK_EF[:-1], fleet_uid="fleet_a", prefix=True).task_id == TASK_EF
    assert tq.show_task(conn, TASK_EF, fleet_uid="fleet_a", prefix=True).task_id == TASK_EF


def test_an_ambiguous_task_or_assignment_prefix_lists_every_match_and_resolves_none(conn):
    with pytest.raises(tq.AmbiguousTaskReferenceError) as raised:
        tq.show_task(conn, "wi_ab12cd34", fleet_uid="fleet_a", prefix=True)
    hint = raised.value.hint
    assert hint.total_matches == 2 and hint.next_command is None
    assert [(c.task_id, c.assignment_id, c.command) for c in hint.candidates] == [
        (TASK_AB1, None, ("task", "show", TASK_AB1)), (TASK_AB2, None, ("task", "show", TASK_AB2))]
    with pytest.raises(tq.AmbiguousTaskReferenceError) as raised:
        tq.show_assignment(conn, "asg_da1585e0", fleet_uid="fleet_a", prefix=True)
    assert [(c.assignment_id, c.command) for c in raised.value.hint.candidates] == [
        (ASG_DA1, ("assignment", "show", ASG_DA1)), (ASG_DA2, ("assignment", "show", ASG_DA2))]


def test_a_prefix_with_no_match_is_searched_before_it_is_reported_missing(conn):
    for query, reference in ((tq.show_task, "wi_00000000"), (tq.show_assignment, "asg_00000000"),
                             (tq.show_task, "asg_00000000"), (tq.show_assignment, "wi_00000000")):
        with pytest.raises(tq.TaskPrefixNotFoundError) as raised:
            query(conn, reference, fleet_uid="fleet_a", prefix=True)
        assert raised.value.code == "not_found" and "starts with " + reference in str(raised.value)
        assert raised.value.hint.candidates == () and raised.value.hint.total_matches == 0
    # Another fleet's record is not found by its prefix either.
    with pytest.raises(tq.TaskPrefixNotFoundError):
        tq.show_task(conn, TASK_FOREIGN[:-1], fleet_uid="fleet_a", prefix=True)


def test_a_too_short_prefix_is_refused_never_reported_missing(conn):
    for query, reference in ((tq.show_task, "wi_ab12cd3"), (tq.show_assignment, "asg_da15"),
                             (tq.show_assignment, "asg_")):
        with pytest.raises(tq.ShortTaskPrefixError) as raised:
            query(conn, reference, fleet_uid="fleet_a", prefix=True)
        assert raised.value.code == "invalid_argument" and raised.value.hint is None


def test_a_wrong_kind_prefix_keeps_the_scoped_canonical_hint(conn):
    with pytest.raises(tq.WrongTaskReferenceError) as raised:
        tq.show_task(conn, "asg_9f8e7d6c", fleet_uid="fleet_a", prefix=True)
    assert raised.value.hint.next_command == ("task", "show", TASK_EF)
    with pytest.raises(tq.WrongTaskReferenceError) as raised:
        tq.show_assignment(conn, "wi_ef56ab78", fleet_uid="fleet_a", prefix=True)
    assert raised.value.hint.next_command == ("assignment", "show", ASG_9F)


def test_mutation_callers_still_read_canonical_ids_only(conn):
    for query, reference in ((tq.show_task, "wi_ef56ab78"), (tq.show_assignment, "asg_9f8e7d6c"),
                             (tq.show_assignment, "asg_da15")):
        with pytest.raises(tq.TaskNotFoundError) as raised:
            query(conn, reference, fleet_uid="fleet_a")
        assert type(raised.value) is tq.TaskNotFoundError


MSG_42A, MSG_42B = "msg_" + _hex("42664fe3", "1"), "msg_" + _hex("42664fe3", "2")
MSG_77 = "msg_" + _hex("77aa88bb", "3")
MSG_77_OTHER = "msg_" + _hex("77aa88bb", "5")


@pytest.fixture
def messages(message_estate):  # noqa: F811 — pytest fixture parameter
    ctx, conn = message_estate
    for message_id in (MSG_42A, MSG_42B, MSG_77):
        communication(conn, msg_id=message_id)
    # Same prefix as MSG_77, between two parties the receiver below is not.
    communication(conn, sender="bot:a/worker", recipient="bot:c/worker", msg_id=MSG_77_OTHER)
    receiver = replace(ctx, caller=ACTORS["bot:b/worker"], caller_fleet_uid=FLEETS["b"])
    return ctx, conn, receiver


def test_a_unique_readable_message_prefix_resolves_for_its_readers(messages):
    ctx, _, receiver = messages
    # MSG_77_OTHER shares the prefix, between two parties neither caller is.
    for caller in (receiver, ctx):
        assert mq.resolve_message_reference(caller, "msg_77aa88bb") == MSG_77
    assert mq.resolve_message_reference(receiver, MSG_77[:-1]) == MSG_77
    # A full ID comes back unread, as the readers below take it.
    assert mq.resolve_message_reference(receiver, "msg_" + "e" * 32) == "msg_" + "e" * 32


def test_an_ambiguous_message_prefix_lists_only_readable_matches(messages):
    ctx, _, receiver = messages
    with pytest.raises(mq.MessageAmbiguousError) as raised:
        mq.resolve_message_reference(receiver, "msg_42664fe3")
    hint = raised.value.hint
    assert raised.value.code == "ambiguous_reference" and hint.total_matches == 2
    assert [(c.id, c.command) for c in hint.candidates] == [
        (MSG_42A, ("message", "show", MSG_42A)), (MSG_42B, ("message", "show", MSG_42B))]
    # The operator may read both messages that start with msg_77aa88bb.
    operator = replace(ctx, caller=ACTORS["human:operator"], caller_fleet_uid=None)
    with pytest.raises(mq.MessageAmbiguousError) as raised:
        mq.resolve_message_reference(operator, "msg_77aa88bb")
    assert raised.value.hint.total_matches == 2


def test_a_message_prefix_with_no_readable_match_is_searched_before_not_found(messages):
    _, _, receiver = messages
    for reference in ("msg_00000000", MSG_77_OTHER[:-1]):
        with pytest.raises(mq.MessageNotFoundError) as raised:
            mq.resolve_message_reference(receiver, reference)
        assert "starts with " + reference in str(raised.value)
        assert raised.value.hint.prefix == reference and raised.value.hint.total_matches == 0


def test_message_prefixes_are_refused_when_short_and_by_the_canonical_readers(messages):
    _, _, receiver = messages
    for reference, words in (("msg_77aa", "at least 8"), ("display-id", "or a prefix of one")):
        with pytest.raises(mq.MessageQueryError, match=words) as raised:
            mq.resolve_message_reference(receiver, reference)
        assert raised.value.code == "invalid_argument"
    # show_message, receipt and wait_for_reply (a reply's parent among their
    # callers) take full IDs only; the read doors resolve a prefix before them.
    for read in (lambda: mq.show_message(receiver, "msg_77aa88bb"),
                 lambda: mq.receipt(receiver, "msg_77aa88bb"),
                 lambda: mq.wait_for_reply(receiver, "msg_77aa88bb", timeout=1)):
        with pytest.raises(mq.MessageQueryError, match="canonical") as raised:
            read()
        assert raised.value.code == "invalid_argument"


def test_a_message_prefix_reads_by_a_range_seek_and_refuses_past_its_bound(messages):
    ctx, conn, _ = messages
    plan = conn.execute("EXPLAIN QUERY PLAN " + mq.MESSAGE_PREFIX_SQL,
                        ("msg_77aa88bb", "msg_77aa88bbg", 65)).fetchall()
    details = [row[3] for row in plan]
    assert len(details) == 1 and details[0].startswith("SEARCH communications USING"), details
    assert "(msg_id>? AND msg_id<?)" in details[0], details
    for n in range(hints.PREFIX_SCAN_LIMIT):
        communication(conn, msg_id="msg_5ca1ab1e" + f"{n:024x}")
    with pytest.raises(mq.MessageAmbiguousError) as raised:
        mq.resolve_message_reference(ctx, "msg_5ca1ab1e")
    assert raised.value.hint.total_matches == hints.PREFIX_SCAN_LIMIT
    assert len(raised.value.hint.candidates) == hints.PREFIX_HINT_LIMIT
    communication(conn, msg_id="msg_5ca1ab1e" + "f" * 24)
    with pytest.raises(mq.MessageAmbiguousError, match="more than 64") as raised:
        mq.resolve_message_reference(ctx, "msg_5ca1ab1e")
    assert raised.value.hint is None


REQUEST_A, REQUEST_B = "5e1ec7ed-0000-4000-8000-000000000001", "5e1ec7ed-0000-4000-8000-000000000002"
REQUEST_C = "a11ce000-1111-4111-8111-111111111111"


@pytest.fixture
def requests(estate):  # noqa: F811 — pytest fixture parameter
    ctx, _ = estate
    for request_id in (REQUEST_A, REQUEST_B, REQUEST_C):
        tasks.admit(ctx, request_id, title="Prefix " + request_id[:4])
    return ctx


def test_a_unique_request_prefix_resolves_and_an_ambiguous_one_lists_both(requests):
    ctx = requests
    for reference in ("a11ce000", "a11ce000-", REQUEST_C[:-1]):
        assert rq.read_request(ctx.root, ctx.fleet_uid, reference, prefix=True).request_id == REQUEST_C
    with pytest.raises(rq.RequestAmbiguousError) as raised:
        rq.read_request(ctx.root, ctx.fleet_uid, "5e1ec7ed", prefix=True)
    hint = raised.value.hint
    assert raised.value.code == "ambiguous_reference" and hint.total_matches == 2
    assert [(c.id, c.command) for c in hint.candidates] == [
        (REQUEST_A, ("request", "show", REQUEST_A)), (REQUEST_B, ("request", "show", REQUEST_B))]


def test_a_request_prefix_with_no_match_is_searched_and_nothing_is_created(requests, tmp_path):
    ctx = requests
    with pytest.raises(rq.RequestNotFoundError) as raised:
        rq.read_request(ctx.root, ctx.fleet_uid, "00000000", prefix=True)
    assert raised.value.hint.total_matches == 0 and "starts with 00000000" in str(raised.value)
    empty = (tmp_path / "empty-host").resolve()
    empty.mkdir()
    with pytest.raises(rq.RequestNotFoundError) as raised:
        rq.read_request(empty, ctx.fleet_uid, "a11ce000", prefix=True)
    assert raised.value.hint.total_matches == 0 and list(empty.iterdir()) == []


def test_request_prefixes_are_refused_when_short_or_not_asked_for(requests):
    ctx = requests
    with pytest.raises(rq.RequestInvalidArgumentError, match="at least its first 8"):
        rq.read_request(ctx.root, ctx.fleet_uid, "a11ce00", prefix=True)
    with pytest.raises(rq.RequestInvalidArgumentError, match="canonical UUID"):
        rq.read_request(ctx.root, ctx.fleet_uid, "a11ce000")


CK_BEEF1, CK_BEEF2 = "ck_" + _hex("beef0001", "1"), "ck_" + _hex("beef0001", "2")
CK_CAFE = "ck_" + _hex("cafe0002", "3")


def _checkin_refs(root, prefix):
    conn, reason = open_ro(root)
    assert conn is not None, reason
    with closing(conn):
        return [row["checkin_ref"] for row in checkins.collect_checkins(
            conn, F, since=None, bot=None, last=False, checkin_prefix=prefix, limit=65)]


def test_a_checkin_prefix_reads_only_the_refs_in_its_range(tmp_path):
    root = plane_root(tmp_path)
    for age, ck in enumerate((CK_BEEF1, CK_BEEF2, CK_CAFE)):
        _decision(root, "mgr", ck, age_h=age + 1)
    assert _checkin_refs(root, "ck_cafe0002") == ["checkin:" + CK_CAFE]
    assert _checkin_refs(root, "ck_beef0001") == ["checkin:" + CK_BEEF1, "checkin:" + CK_BEEF2]
    assert _checkin_refs(root, "ck_00000000") == []
    assert _checkin_refs(root, CK_CAFE[:-1]) == ["checkin:" + CK_CAFE]
