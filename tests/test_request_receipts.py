"""Operational receipts retain uncertainty without replaying a transport."""

from dataclasses import replace
import json
import os
import stat
from uuid import uuid4

import pytest

from claudlobby import request_receipts as rr


@pytest.fixture
def receipt_case(tmp_path):
    intent = rr.RequestIntent(
        "message.send", 1, "host_" + "1" * 32, "fleet_" + "2" * 32,
        "actor_" + "3" * 32, "actor_" + "4" * 32,
        rr.semantic_digest({"body": b"SECRET-private-body", "token": "SECRET-token"}),
        (rr.StagePlan("recording", (rr.ExpectedFact("ev_" + "5" * 32, "communication", "6" * 64),)),
         rr.StagePlan("delivery")), message_id="msg_" + "7" * 32)
    return tmp_path, str(uuid4()), intent


def test_interrupted_recording_and_send_reload_unknown_without_automatic_retry(receipt_case, monkeypatch):
    root, ident, intent = receipt_case
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        with monkeypatch.context() as patch:
            patch.setattr(rr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("receipt unavailable")))
            with pytest.raises(OSError):
                store.prepare(intent)
        assert store.load() is None and not store.path.exists()
        store.prepare(intent)
        store.begin_attempt()
        store.stage(0)
        store.outcome(0, "committed")
        before = store.path.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr(rr.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("interrupted before rename")))
            with pytest.raises(OSError):
                store.stage(1)
        assert store.path.read_bytes() == before
        assert not list(store.path.parent.glob("*.tmp"))
        with monkeypatch.context() as patch:
            patch.setattr(rr, "_sync", lambda *_: (_ for _ in ()).throw(OSError("interrupted after rename")))
            with pytest.raises(OSError):
                store.stage(1)
    with pytest.raises(rr.ReceiptError, match="lock"):
        store.load()
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        saved = store.prepare(intent)
        assert [s.status for s in saved.stages] == ["committed", "unknown"]
        assert saved.attempt == 1
        store.begin_attempt()
        with pytest.raises(rr.ReceiptConflict):
            store.stage(0)
        with pytest.raises(rr.ReceiptConflict):
            store.stage(1)
        store.stage(1, retry_uncertain=True)
        store.outcome(1, "submitted")
        store.outcome(1, "received")
        assert store.load().stages[1] == rr.StageOutcome("received", 2)
        with pytest.raises(rr.ReceiptConflict):
            store.outcome(0, "unrecorded")


@pytest.mark.parametrize(("operation", "families"), [
    ("message.send", ("communication", "transmission")),
    ("task.assign", ("work_item", "assignment", "task")),
])
def test_uuid_conflicts_and_private_digest_only_storage(receipt_case, operation, families):
    root, ident, intent = receipt_case
    intent = replace(intent, operation=operation,
                     task_id="wi_" + "b" * 32 if operation == "task.assign" else None,
                     assignment_id="asg_" + "c" * 32 if operation == "task.assign" else None,
                     stages=(rr.StagePlan("recording", tuple(
                         rr.ExpectedFact(f"ev_{index:032x}", family, "6" * 64)
                         for index, family in enumerate(families, 1))),))
    assert rr.semantic_digest({"body": b"SECRET-private-body", "token": "SECRET-token"},
                              presentation={"json": True, "retry_uncertain": True}) == intent.semantic_sha256
    assert rr.semantic_digest({"body": b"changed"}) != intent.semantic_sha256
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.load() is None  # no inference about a prior best-effort send
        saved = store.prepare(intent)
        assert store.prepare(intent) == saved
        assert tuple(f.family for f in store.load().intent.stages[0].facts) == families
        for table in ("events", "communications", "work_items", "assignments", "metric_samples", "registry_snapshots"):
            invalid = replace(intent, stages=(rr.StagePlan("recording", (
                replace(intent.stages[0].facts[0], family=table),)),))
            with pytest.raises(rr.ReceiptError, match="expected fact"):
                store.prepare(invalid)
        for changed in (replace(intent, semantic_sha256="8" * 64),
                        replace(intent, recipient_uid="actor_" + "9" * 32),
                        replace(intent, message_id="msg_" + "a" * 32)):
            with pytest.raises(rr.ReceiptConflict):
                store.prepare(changed)
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert b"SECRET" not in store.path.read_bytes()
        raw = json.loads(store.path.read_bytes())
        raw["format_version"] = 2
        store.path.write_text(json.dumps(raw))
        with pytest.raises(rr.ReceiptError):
            store.load()
        assert json.loads(store.path.read_bytes())["format_version"] == 2


def test_flock_excludes_independent_process_and_rejects_inherited_store(receipt_case):
    root, ident, intent = receipt_case
    # fork runs only Python against this owned temp tree, with no native service.
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        store.prepare(intent)
        pid = os.fork()
        if pid == 0:
            try:
                with pytest.raises(rr.ReceiptError, match="lock"):
                    store.load()
                with pytest.raises(rr.ReceiptBusy):
                    with rr.locked_request(root, intent.fleet_uid, ident):
                        pass
            except BaseException:
                os._exit(1)
            os._exit(0)
        assert os.waitpid(pid, 0)[1] == 0
    with rr.locked_request(root, intent.fleet_uid, ident) as store:
        assert store.load().intent == intent
