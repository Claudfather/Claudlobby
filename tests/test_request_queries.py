"""Request inspection observes retained receipts and exact facts without effects."""

from dataclasses import replace
import sqlite3
from uuid import uuid4

import pytest

from claudlobby import request_queries as queries, request_receipts as receipts
from claudlobby import task_operations as tasks
from claudlobby.plane.db import db_file
from tests.plane_setup import initialize_plane
from tests.test_task_operations import estate  # noqa: F401 — private real-Plane fixture


def test_absent_request_is_not_found_without_creating_any_path(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    root = root.resolve()
    with pytest.raises(queries.RequestNotFoundError) as error:
        queries.read_request(root, "fleet_" + "1" * 32, str(uuid4()))
    assert error.value.code == "not_found"
    assert "cannot prove" in error.value.hint and "earlier best-effort message" in error.value.hint
    with pytest.raises(queries.RequestInvalidArgumentError):
        queries.read_request(root, "fleet_" + "1" * 32, "not-a-uuid")
    assert list(root.iterdir()) == []


def test_real_task_receipt_has_separate_exact_conflict_and_pruned_proof(estate):  # noqa: F811
    ctx, conn = estate
    request_id = str(uuid4())
    task = tasks.admit(ctx, request_id, title="Private request")
    receipt_path = ctx.root / "state/requests" / ctx.fleet_uid / f"{request_id}.json"
    saved = receipt_path.read_bytes()
    before_changes = conn.total_changes
    view = queries.read_request(ctx.root, ctx.fleet_uid, request_id)
    assert view.request_id == request_id and view.operation == "task.admit"
    assert view.operation_version == view.format_version == 1
    assert (view.host_uid, view.fleet_uid, view.caller_uid) == (
        ctx.host_uid, ctx.fleet_uid, ctx.caller.uid)
    assert view.task_id == task.task_id and view.assignment_id is None
    assert len(view.semantic_sha256) == 64 and view.observed_attempt == 1
    assert len(view.stages) == 1
    assert view.stages[0].recorded_status == "committed"
    assert view.stages[0].proof.status == "committed" and view.stages[0].proof.matched == 1
    assert view.transmissions == () and conn.total_changes == before_changes
    assert receipt_path.read_bytes() == saved

    event_id = view.stages[0].expected_facts[0].event_id
    conn.execute("UPDATE work_items SET title='Changed' WHERE event_id=?", (event_id,))
    altered = queries.read_request(ctx.root, ctx.fleet_uid, request_id)
    assert altered.stages[0].recorded_status == "committed"
    assert altered.stages[0].proof.status == "conflict"
    conn.execute("DELETE FROM work_items WHERE event_id=?", (event_id,))
    pruned = queries.read_request(ctx.root, ctx.fleet_uid, request_id)
    assert pruned.stages[0].recorded_status == "committed"
    assert pruned.stages[0].proof.status == "unknown"
    assert receipt_path.read_bytes() == saved


def _message_intent(root):
    fleet_uid = "fleet_" + "2" * 32
    return receipts.RequestIntent(
        "message.send", 1, "host_" + "1" * 32, fleet_uid,
        "actor_" + "3" * 32, "actor_" + "4" * 32, "5" * 64,
        (receipts.StagePlan("recording", (receipts.ExpectedFact(
            "ev_" + "6" * 32, "communication", "7" * 64,
            ("emitter", "event_id", "fleet_uid", "host_uid")),)),
         receipts.StagePlan("delivery")),
        message_id="msg_" + "8" * 32,
        route=receipts.MessageRouteBinding(
            "activation-1", "plan-1", "release-1", fleet_uid, fleet_uid,
            "bot:example/manager", "bot:example/worker",
            "actor_" + "9" * 32, "bot:example/manager",
            receipts.NativeDestination(str(root), "example", "example.worker", "worker", "/tmp"),
            receipts.NativeDestination(str(root), "example", "example.manager", "manager", "/tmp"),
        ),
    )


def test_submitted_and_unobserved_sends_remain_distinct_from_recording(tmp_path):
    root = tmp_path.resolve()
    initialize_plane(root)
    intent = _message_intent(root)
    submitted_id, unobserved_id = str(uuid4()), str(uuid4())
    with receipts.locked_request(root, intent.fleet_uid, submitted_id) as store:
        store.prepare(intent)
        store.begin_message_attempt("ev_" + "a" * 32)
        store.stage(0)
        store.observe_message_transport(1, receipts.TransportObservation(
            "submitted", native_returncode=0))
        store.prepare_message_transmission(1, receipts.ExpectedFact(
            "ev_" + "a" * 32, "transmission", "d" * 64,
            ("emitter", "event_id", "fleet_uid", "host_uid")))
        store.stage_message_transmission(1)
    with receipts.locked_request(root, intent.fleet_uid, unobserved_id) as store:
        store.prepare(replace(intent, message_id="msg_" + "b" * 32))
        store.begin_message_attempt("ev_" + "c" * 32)
        store.stage(0)
    with sqlite3.connect(db_file(root)) as conn:
        before = (conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0],
                  conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    submitted = queries.read_request(root, intent.fleet_uid, submitted_id)
    unobserved = queries.read_request(root, intent.fleet_uid, unobserved_id)
    assert submitted.stages[0].proof.status == "unrecorded"
    assert submitted.stages[1].proof is None
    assert submitted.stages[1].recorded_status == "submitted"
    assert submitted.transmissions[0].transport.status == "submitted"
    assert submitted.transmissions[0].recorded_status == "unknown"
    assert submitted.transmissions[0].proof.status == "unrecorded"
    assert unobserved.stages[1].recorded_status == "unknown"
    assert unobserved.transmissions[0].transport is None
    assert unobserved.transmissions[0].proof is None

    db_file(root).rename(root / "plane-offline")
    offline = queries.read_request(root, intent.fleet_uid, submitted_id)
    assert offline.stages[0].proof.status == "unknown"
    assert offline.stages[1].recorded_status == "submitted"
    assert offline.transmissions[0].transport.status == "submitted"
    assert offline.transmissions[0].proof.status == "unknown"
    assert not db_file(root).exists()
    with sqlite3.connect(root / "plane-offline") as conn:
        assert before == (conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0],
                          conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])


def test_unsafe_receipt_node_and_oversize_refuse_without_repair(estate):  # noqa: F811
    ctx, _ = estate
    request_id = str(uuid4())
    tasks.admit(ctx, request_id, title="Owned private request")
    path = ctx.root / "state/requests" / ctx.fleet_uid / f"{request_id}.json"
    saved = path.read_bytes()
    target = path.with_name("saved.json")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(queries.RequestQueryError, match="redirected"):
        queries.read_request(ctx.root, ctx.fleet_uid, request_id)
    path.unlink()
    target.rename(path)
    path.write_bytes(b"{" + b" " * queries.MAX_RECEIPT_BYTES + b"}")
    with pytest.raises(queries.RequestQueryError, match="read limit"):
        queries.read_request(ctx.root, ctx.fleet_uid, request_id)
    assert path.stat().st_size > len(saved)
