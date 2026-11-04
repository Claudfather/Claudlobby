"""Concrete update ordering and interruption contracts, without native effects."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import update_operation as operation
from claudlobby import bot_operations
from claudlobby.git_update import UpdateError


class Store:
    root = Path("/data")
    installation = SimpleNamespace(data_root=root)

    def __init__(self, record):
        self.record = deepcopy(record)
        self.history = []

    def _assert_locked(self):
        pass

    def load(self):
        return deepcopy(self.record)

    def save(self, value):
        self.record = deepcopy(value)
        self.history.append(deepcopy(value))

    def verify_dependencies(self):
        return self.load()


def record():
    rows = {}
    for key, fields in {
        "timer": {"target": "job.timer", "scheduled": False, "phase": "producers"},
        "job": {"target": "job.service", "scheduled": True, "phase": "producers"},
        "bot": {"target": "bot.service", "scheduled": False, "phase": "bots",
                "bot_binding": ["/data/bot", "bot", "/tmp/private"], "disposition": "running"},
        "owner": {"target": "owner.service", "scheduled": False, "phase": "producers"},
        "recorder": {"target": "recorder.service", "scheduled": False, "phase": "ingest"},
    }.items():
        row = {"manager": "Linux", "bot_binding": None, "disposition": "resident", **fields}
        rows[key] = {"intent": row}
    return {"phase": "quiesce", "request_id": "original", "config_fingerprint": "render",
            "previous_config": "previous", "candidate_config": {"fingerprint": "candidate"},
            "target": "target", "consumers": rows, "sql_schema": 14}


def test_quiesce_schedules_then_one_checkpoint_then_bot_then_recorder(tmp_path, monkeypatch):
    store = Store(record())
    events = []
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "previous"})
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    def checkpoint(*_args, **kwargs):
        events.append(("checkpoint", kwargs["operation_id"]))
        return {"original": True}
    monkeypatch.setattr(bot_operations, "save_planned_checkpoint", checkpoint, raising=False)
    def quiet(store, key, adapter):
        events.append(("stop", key))
        current = store.load()
        current["consumers"][key]["stop"] = "done"
        store.save(current)
    monkeypatch.setattr(operation, "quiesce_consumer", quiet)
    operation.quiesce_operation(store, None)
    assert events == [("stop", "timer"), ("stop", "job"), ("checkpoint", "original"),
                      ("stop", "bot"), ("stop", "owner"), ("stop", "recorder")]
    operation.quiesce_operation(store, None)
    assert sum(event[0] == "checkpoint" for event in events) == 1


def test_final_quiet_verifies_original_proof_and_reobserves_every_consumer(monkeypatch):
    data = record()
    for item in data["consumers"].values():
        item["stop"] = "done"
    data["consumers"]["bot"]["checkpoint"] = {"original": True}
    store, events = Store(data), []
    monkeypatch.setattr(bot_operations, "verify_checkpoint_after_stop",
                        lambda directory, proof: events.append(("proof", proof)), raising=False)
    monkeypatch.setattr(operation, "observe_quiet", lambda store, key, adapter: events.append(("quiet", key)))
    monkeypatch.setattr(operation, "_schema", lambda *_: 14)
    operation.assert_operation_quiet(store, None)
    assert ("proof", {"original": True}) in events
    assert {key for kind, key in events if kind == "quiet"} == set(data["consumers"])


def test_resume_before_stop_rechecks_original_live_save_without_replaying(monkeypatch):
    data = record()
    for key in ("timer", "job"):
        data["consumers"][key]["stop"] = "done"
    data["consumers"]["bot"].update(checkpoint={"original": True}, checkpoint_operation="original")
    store, events = Store(data), []
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "previous"})
    def recheck(*_args, **kwargs):
        events.append(kwargs["operation_id"])
        raise bot_operations.BotLifecycleError("existing save is no longer fresh")
    monkeypatch.setattr(bot_operations, "save_planned_checkpoint", recheck)
    monkeypatch.setattr(operation, "quiesce_consumer", lambda *_: pytest.fail("must not stop on stale save"))
    with pytest.raises(bot_operations.BotLifecycleError, match="no longer fresh"):
        operation.quiesce_operation(store, None)
    assert events == ["original"]
    assert store.load()["consumers"]["bot"].get("stop") is None


def test_publication_commits_config_before_restore(monkeypatch):
    data = record()
    data.update(phase="environment", maintenance=True, git_complete=True,
                dependencies={"environment_complete": True})
    store, events = Store(data), []
    monkeypatch.setattr(operation, "_schema", lambda *_: 14)
    monkeypatch.setattr(operation, "_head", lambda *_: "target")
    monkeypatch.setattr(operation, "read_rendered", lambda *_: "render")
    monkeypatch.setattr(operation, "apply_publication", lambda *_: events.append("files"))
    def commit(*_args, **_kwargs):
        assert store.load()["phase"] == "commit"
        assert store.load()["maintenance"]
        events.append("config")
    monkeypatch.setattr(operation, "publish_current_config", commit)
    result = operation.publish_operation(store)
    assert events == ["files", "config"]
    assert result["phase"] == "restore"
    assert result["config_committed"] and not result["maintenance"]


def test_failed_consumer_keeps_committed_configuration_and_other_consumers_restore(monkeypatch):
    data = record()
    data.update(phase="restore", config_committed=True)
    store, events = Store(data), []
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: data["candidate_config"])
    def restore(store, key, *_):
        events.append(key)
        if key == "bot":
            raise UpdateError("bot readiness pending")
        current = store.load()
        current["consumers"][key]["restore"] = "done"
        store.save(current)
    monkeypatch.setattr(operation, "restore_consumer", restore)
    result = operation.restore_operation(store, None)
    assert events == ["recorder", "job", "owner", "bot", "timer"]
    assert result["config_committed"]
    assert result["health_errors"] == {"bot": "bot readiness pending"}
    assert result["phase"] == "done"


def test_schema_change_refuses_without_opening_or_initializing_db(monkeypatch):
    store = Store(record())
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(stdout=b"SQL_SCHEMA_VERSION = 15\n"))
    monkeypatch.setattr(operation, "db_file", lambda *_: SimpleNamespace(is_file=lambda: False))
    with pytest.raises(UpdateError, match="canonical migration"):
        operation._schema(store, "target")
