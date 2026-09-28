"""Real task ingestion and exact retry proof on explicitly owned Plane roots."""

from dataclasses import replace
import json
import os
import signal
import sqlite3
import time
from uuid import uuid4

import pytest

from claudlobby import task_operations as tasks
from claudlobby.config import BotConfig, FleetConfig, ProjectConfig
from claudlobby.context import Context
from claudlobby.paths import Paths
from claudlobby.plane.db import connect, db_file
from claudlobby.plane.identity import resolve, resolve_party
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.request_receipts import ReceiptConflict, locked_request
from claudlobby.task_queries import WrongTaskReferenceError, show_task
from tests.package_fixtures import source_package
from tests.plane_setup import initialize_plane
from tests.test_task_state import _insert


@pytest.fixture
def estate(tmp_path):
    initialize_plane(tmp_path)
    host = ensure_host_uid(tmp_path / "state")
    conn = connect(db_file(tmp_path), synchronous="FULL")
    fleet_uid = resolve(conn, "fleet", "example", now="2026-09-28T00:00:00Z")
    actors = {bot: tasks.TaskActor(resolve_party(conn, "bot:example/" + bot,
                  now="2026-09-28T00:00:00Z", fleet_uid=fleet_uid), "bot:example/" + bot)
              for bot in ("manager", "worker", "other")}
    fleet = FleetConfig("example", "example", "manager",
                bots={bot: BotConfig(bot, bot, []) for bot in actors},
                projects={"shop": ProjectConfig("shop", "Shop", repos=["owner/shop"])})
    context = Context(Paths(root=tmp_path, package=source_package()), fleet, {})
    ctx = tasks.TaskOperationContext(context, host, fleet_uid, actors["manager"], actors)
    yield ctx, conn
    conn.close()


def _counts(conn):
    return tuple(conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                 for table in ("work_items", "assignments", "events", "communications"))


def _receipt(ctx, request_id):
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store:
        return store.load()


def test_duplicate_admission_is_one_unassigned_task_without_plaintext_receipt(estate):
    ctx, conn = estate
    request_id = str(uuid4())
    result = tasks.admit(ctx, request_id, title="Private authored title", body="Private authored body",
                         project_key="shop", repo="owner/shop")
    assert result.task.state == "queued" and result.assignment_id is None
    assert result.task.created_by_uid == ctx.caller.uid and result.task.fleet_uid == ctx.fleet_uid
    assert result.recording == "committed" and result.delivery == result.notification == "not_requested"
    again = tasks.admit(ctx, request_id, title="Private authored title", body="Private authored body",
                        project_key="shop", repo="owner/shop")
    assert again.replayed and again.task_id == result.task_id and again.task == result.task
    assert _counts(conn) == (1, 0, 0, 0)
    raw = (ctx.root / "state/requests" / ctx.fleet_uid / (request_id + ".json")).read_text()
    assert "Private authored" not in raw and json.loads(raw)["attempt"] == 1
    with pytest.raises(ReceiptConflict):
        tasks.admit(ctx, request_id, title="Changed title")
    assert _counts(conn) == (1, 0, 0, 0)


def test_competing_routing_and_exact_assignee_acceptance(estate):
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Route me", project_key="shop")
    changed_scope = replace(ctx, context=replace(ctx.context, fleet=replace(ctx.context.fleet, projects={})))
    with pytest.raises(tasks.TaskQueryError, match="project"):
        tasks.assign(changed_scope, str(uuid4()), task.task_id, bot_id="worker")
    routed = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker", expected_by="2026-10-01T00:00:00Z")
    assert routed.task.state == "assigned"
    assert routed.task.current_assignment.expected_by == "2026-10-01T00:00:00+00:00"
    with pytest.raises(tasks.TaskConflictError, match="not queued"):
        tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="other")
    with pytest.raises(tasks.TaskConflictError, match="assignee"):
        tasks.accept(ctx, str(uuid4()), routed.assignment_id)
    with pytest.raises(WrongTaskReferenceError):
        tasks.accept(ctx, str(uuid4()), task.task_id)
    worker = replace(ctx, caller=ctx.bots["worker"])
    rid = str(uuid4())
    accepted = tasks.accept(worker, rid, routed.assignment_id)
    assert accepted.task.state == "active"
    assert accepted.task.history[0].actor_uid == worker.caller.uid
    assert tasks.accept(worker, rid, routed.assignment_id).replayed
    with pytest.raises(tasks.TaskConflictError, match="already accepted"):
        tasks.accept(worker, str(uuid4()), routed.assignment_id)
    assert _counts(conn) == (1, 1, 1, 0)


def test_stale_assignment_cannot_accept_its_replacement(estate):
    from claudlobby.plane.emit_api import emit_batch
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Replace me")
    original = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="worker")
    # Model the later return operation with the actual existing event codec.
    emit_batch(ctx.root, [{"event_type": "task", "fleet": ctx.context.fleet.name,
                "emitter": "claudlobby.tasks.v1", "payload": {"work_item_id": task.task_id,
                "assignment_id": original.assignment_id, "event": "returned_blocked"}}], require_commit=True)
    replacement = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="other")
    with pytest.raises(tasks.TaskConflictError, match="stale"):
        tasks.accept(replace(ctx, caller=ctx.bots["worker"]), str(uuid4()), original.assignment_id)
    current = show_task(conn, task.task_id, fleet_uid=ctx.fleet_uid)
    assert current.current_assignment.assignment_id == replacement.assignment_id
    assert current.state == "assigned" and len(current.history) == 1


def test_failed_conditional_recording_is_not_spooled_or_replayed_after_routing_changes(estate, monkeypatch):
    from claudlobby.plane import emit_api
    ctx, conn = estate
    task = tasks.admit(ctx, str(uuid4()), title="Conditional routing")
    original_emit = emit_api.emit_batch
    def unavailable(root, raws, *, require_commit):
        assert require_commit is True
        def broken_connection():
            error = sqlite3.OperationalError("unable to open database file")
            if hasattr(sqlite3, "SQLITE_CANTOPEN"):
                error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
            error.sqlite_errorname = "SQLITE_CANTOPEN"
            raise error
        return original_emit(root, raws, require_commit=True, conn_factory=broken_connection)
    failed_request = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(emit_api, "emit_batch", unavailable)
        with pytest.raises(tasks.TaskRecordingError) as raised:
            tasks.assign(ctx, failed_request, task.task_id, bot_id="worker")
    assert raised.value.recording == "unknown"
    pending = _receipt(ctx, failed_request)
    assert pending.stages[0].status == "unknown" and _counts(conn) == (1, 0, 0, 0)
    winner = tasks.assign(ctx, str(uuid4()), task.task_id, bot_id="other")
    with pytest.raises(tasks.TaskConflictError, match="not queued"):
        tasks.assign(ctx, failed_request, task.task_id, bot_id="worker")
    assert _receipt(ctx, failed_request).intent == pending.intent
    assert _counts(conn) == (1, 1, 0, 0)
    assert show_task(conn, task.task_id, fleet_uid=ctx.fleet_uid).current_assignment.assignment_id == winner.assignment_id
    assert not list((ctx.root / "state/plane").rglob("*.jsonl"))


def test_commit_before_receipt_outcome_reconciles_and_pruned_payload_refuses(estate, monkeypatch):
    from claudlobby.request_receipts import RequestStore
    ctx, conn = estate
    rid = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(RequestStore, "outcome", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("after commit")))
        with pytest.raises(OSError, match="after commit"):
            tasks.admit(ctx, rid, title="One durable task")
    prior = _receipt(ctx, rid)
    assert prior.stages[0].status == "unknown" and _counts(conn) == (1, 0, 0, 0)
    result = tasks.admit(ctx, rid, title="One durable task")
    assert result.replayed and result.task_id == prior.intent.task_id
    assert _receipt(ctx, rid).attempt == 1 and _counts(conn) == (1, 0, 0, 0)
    conn.execute("DELETE FROM work_items WHERE work_item_id=?", (result.task_id,))
    with pytest.raises(tasks.TaskRecordingError):
        tasks.admit(ctx, rid, title="One durable task")
    assert _counts(conn) == (0, 0, 0, 0)


def test_declared_scope_and_missing_identity_refuse_before_recording(estate):
    ctx, conn = estate
    _insert(conn, "workstreams", workstream_id="foreign-stream", fleet_uid="fleet_" + "f" * 32,
            title="Other fleet", opened_by_uid=ctx.caller.uid)
    for arguments in ({"project_key": "missing"}, {"workstream_id": "foreign-stream"},
                      {"project_key": "shop", "repo": "elsewhere/repo"}):
        with pytest.raises(tasks.TaskQueryError):
            tasks.admit(ctx, str(uuid4()), title="Refuse", **arguments)
    with pytest.raises(tasks.TaskQueryError, match="declared member"):
        tasks.assign(ctx, str(uuid4()), "wi_" + "1" * 32, bot_id="outside")
    missing = replace(ctx, caller=tasks.TaskActor("actor_" + "f" * 32, "bot:example/missing"))
    before = conn.execute("SELECT count(*) FROM identity_registry").fetchone()[0]
    with pytest.raises(tasks.TaskConflictError, match="identity"):
        tasks.admit(missing, str(uuid4()), title="Do not mint")
    assert conn.execute("SELECT count(*) FROM identity_registry").fetchone()[0] == before
    assert _counts(conn) == (0, 0, 0, 0)


def test_withdraw_closes_queued_work_and_its_active_assignment_in_one_fact(estate):
    ctx, conn = estate
    for assigned in (False, True):
        admitted = tasks.admit(ctx, str(uuid4()), title="Withdraw this work")
        aid = None
        if assigned:
            routed = tasks.assign(ctx, str(uuid4()), admitted.task_id, bot_id="worker")
            aid = routed.assignment_id
            tasks.accept(replace(ctx, caller=ctx.bots["worker"]), str(uuid4()), aid)
        before = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        rid = str(uuid4())
        withdrawn = tasks.withdraw(ctx, rid, admitted.task_id, reason="No longer needed")
        assert withdrawn.task.state == "cancelled" and withdrawn.task.current_assignment is None
        assert withdrawn.assignment_id == aid
        terminal = withdrawn.task.terminal_event
        assert terminal.event == "cancelled" and terminal.assignment_id is None
        assert terminal.emitter == tasks.TASK_EMITTER
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == before + 1
        if assigned:
            old = withdrawn.task.assignments[0]
            assert old.assignment_id == aid and old.state == "closed"
            assert old.terminal_event.event_id == terminal.event_id
        assert tasks.withdraw(ctx, rid, admitted.task_id, reason="No longer needed").replayed
        with pytest.raises(tasks.TaskConflictError, match="terminal"):
            tasks.withdraw(ctx, str(uuid4()), admitted.task_id, reason="Again")
        with pytest.raises(tasks.TaskConflictError, match="current assignment"):
            tasks.reassign(ctx, str(uuid4()), admitted.task_id, bot_id="other", reason="Too late")
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == before + 1


def test_reassign_commit_before_receipt_update_replays_without_retargeting(estate, monkeypatch):
    from claudlobby.request_receipts import RequestStore
    ctx, conn = estate
    admitted = tasks.admit(ctx, str(uuid4()), title="Hand off")
    old = tasks.assign(ctx, str(uuid4()), admitted.task_id, bot_id="worker")
    rid = str(uuid4())
    with monkeypatch.context() as patch:
        patch.setattr(RequestStore, "outcome", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("after commit")))
        with pytest.raises(OSError, match="after commit"):
            tasks.reassign(ctx, rid, admitted.task_id, bot_id="other", reason="New specialist")
    pending = _receipt(ctx, rid)
    assert pending.stages[0].status == "unknown" and len(pending.intent.stages[0].facts) == 2
    current = show_task(conn, admitted.task_id, fleet_uid=ctx.fleet_uid)
    successor = current.current_assignment.assignment_id
    assert successor == pending.intent.assignment_id and successor != old.assignment_id
    assert current.state == "assigned" and current.assignments[0].state == "closed"
    closure = current.assignments[0].terminal_event
    assert closure.event == "reassigned" and closure.successor_id == successor
    assert closure.assignment_id == old.assignment_id and closure.emitter == tasks.TASK_EMITTER
    with pytest.raises(tasks.TaskConflictError, match="stale"):
        tasks.accept(replace(ctx, caller=ctx.bots["worker"]), str(uuid4()), old.assignment_id)
    tasks.accept(replace(ctx, caller=ctx.bots["other"]), str(uuid4()), successor)
    before = _counts(conn)
    replayed = tasks.reassign(ctx, rid, admitted.task_id, bot_id="other", reason="New specialist")
    assert replayed.replayed and replayed.assignment_id == successor and replayed.task.state == "active"
    assert _receipt(ctx, rid).attempt == 1 and _counts(conn) == before


def test_reassign_rolls_back_both_facts_and_failed_retry_cannot_close_a_replacement(estate):
    ctx, conn = estate
    admitted = tasks.admit(ctx, str(uuid4()), title="Atomic handoff")
    old = tasks.assign(ctx, str(uuid4()), admitted.task_id, bot_id="worker")
    # Fail the second write inside the real ingest transaction, after the
    # predecessor closure. The trigger and all rows belong to this fixture.
    conn.execute("CREATE TRIGGER reject_successor BEFORE INSERT ON assignments "
                 "BEGIN SELECT RAISE(ABORT, 'owned successor failure'); END")
    rid = str(uuid4())
    before = _counts(conn)
    with pytest.raises(tasks.TaskRecordingError):
        tasks.reassign(ctx, rid, admitted.task_id, bot_id="other", reason="Move work")
    pending = _receipt(ctx, rid)
    assert pending.stages[0].status == "unknown" and _counts(conn) == before
    current = show_task(conn, admitted.task_id, fleet_uid=ctx.fleet_uid)
    assert current.current_assignment.assignment_id == old.assignment_id and current.history == ()
    for fact in pending.intent.stages[0].facts:
        assert conn.execute("SELECT 1 FROM ingest_ledger WHERE event_id=?", (fact.event_id,)).fetchone() is None
    conn.execute("DROP TRIGGER reject_successor")
    winner = tasks.reassign(ctx, str(uuid4()), admitted.task_id, bot_id="manager", reason="Manager takes it")
    before = _counts(conn)
    with pytest.raises(ReceiptConflict):
        tasks.reassign(ctx, rid, admitted.task_id, bot_id="other", reason="Move work")
    assert _receipt(ctx, rid).intent == pending.intent and _counts(conn) == before
    assert show_task(conn, admitted.task_id, fleet_uid=ctx.fleet_uid).current_assignment.assignment_id == winner.assignment_id
    assert not list((ctx.root / "state/plane").rglob("*.jsonl"))


def test_reassign_without_current_and_foreign_bot_mutations_refuse_before_preparation(estate):
    ctx, conn = estate
    tid = "wi_" + "b" * 32
    _insert(conn, "work_items", fleet_uid=ctx.fleet_uid, work_item_id=tid,
            title="Queued", created_by_uid=ctx.caller.uid)
    with pytest.raises(tasks.TaskConflictError, match="current assignment"):
        tasks.reassign(ctx, str(uuid4()), tid, bot_id="worker", reason="No predecessor")
    foreign = replace(ctx, caller_fleet_uid="fleet_" + "f" * 32)
    with pytest.raises(tasks.TaskConflictError, match="origin fleet"):
        tasks.reassign(foreign, str(uuid4()), tid, bot_id="worker", reason="Wrong fleet")
    with pytest.raises(tasks.TaskConflictError, match="origin fleet"):
        tasks.withdraw(foreign, str(uuid4()), tid, reason="Wrong fleet")
    assert _counts(conn) == (1, 0, 0, 0)
    assert not list((ctx.root / "state/requests").rglob("*.json"))


def test_task_flock_excludes_a_different_request_in_an_independent_process(estate):
    ctx, _ = estate
    task_id = "wi_" + "a" * 32
    with locked_request(ctx.root, ctx.fleet_uid, str(uuid4())) as first:
        with tasks._locked_task(first, task_id) as check:
            check()
            child = os.fork()
            if child == 0:
                try:
                    with locked_request(ctx.root, ctx.fleet_uid, str(uuid4())) as second:
                        with pytest.raises(tasks.TaskConflictError, match="already being changed"):
                            with tasks._locked_task(second, task_id):
                                pass
                except BaseException:
                    os._exit(1)
                os._exit(0)
            deadline = time.monotonic() + 5
            reaped = False
            try:
                while time.monotonic() < deadline:
                    pid, status = os.waitpid(child, os.WNOHANG)
                    if pid:
                        reaped = True
                        assert status == 0
                        break
                    time.sleep(0.02)
                else:
                    pytest.fail("owned lock contender did not finish")
            finally:
                if not reaped:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(child, 0)
