"""Concrete update ordering and interruption contracts, without native effects."""
from copy import deepcopy
import os
import shutil
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import update_operation as operation
from claudlobby import bot_operations
from claudlobby.git_update import UpdateError
from claudlobby.supervision_runtime import RuntimeEvidenceError


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
        "bot": {"target": "bot", "scheduled": False, "phase": "bots",
                "bot_binding": ["/data/bot", "bot", "/tmp/private"], "disposition": "running"},
        "owner": {"target": "owner.service", "scheduled": False, "phase": "producers"},
        "recorder": {"target": "recorder.service", "scheduled": False, "phase": "ingest"},
    }.items():
        row = {"manager": "Linux", "bot_binding": None, "disposition": "resident", **fields}
        rows[key] = {"intent": row}
    return {"phase": "quiesce", "request_id": "original", "config_fingerprint": "render",
            "previous_config": "previous", "candidate_config": {"fingerprint": "candidate"},
            "target": "target", "consumers": rows, "sql_schema": 14, "scope": _scope(rows)}


def _scope(rows):
    """A frozen scope binds each consumer's intent, keyed by its target."""
    return operation.consumer_scope([item["intent"] for item in rows.values()])


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
    operation.quiesce_operation(store, SessionAdapter())
    assert events == [("stop", "timer"), ("stop", "job"), ("checkpoint", "original"),
                      ("stop", "bot"), ("stop", "owner"), ("stop", "recorder")]
    operation.quiesce_operation(store, SessionAdapter())
    assert sum(event[0] == "checkpoint" for event in events) == 1


class SessionAdapter:
    """Answers the session observer with one fixed answer and leaves every other native call to the adapter it
    wraps, if any. Observer calls are recorded."""

    def __init__(self, answer="ready", inner=None):
        self.answer, self.inner, self.calls = answer, inner, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def call(self, function, *args, timeout=30):
        if function == "svc_bot_session_observe":
            self.calls.append((function, args))
            return SimpleNamespace(returncode=0, stdout=self.answer, stderr="")
        return self.inner.call(function, *args, timeout=timeout)


class FailingObserver:
    """A native observer call that fails."""

    def __init__(self):
        self.calls = []

    def call(self, function, *args, timeout=30):
        self.calls.append((function, args))
        return SimpleNamespace(returncode=3, stdout="", stderr="observer refused")


@pytest.fixture
def private_dir():
    """A short bot tmpdir (an AF_UNIX path stays under 108 bytes) holding tmux-<uid>/."""
    path = Path(tempfile.mkdtemp(prefix="sg", dir="/tmp"))
    (path / f"tmux-{os.getuid()}").mkdir(mode=0o700)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _bind(private_dir, listening):
    """Bind the bot's private tmux socket. A closed unlinked one is the file a dead server leaves."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(private_dir / f"tmux-{os.getuid()}" / "bot"))
    if listening:
        sock.listen(1)
        return sock
    sock.close()


def _bound_record(private_dir):
    """The bot's binding points at the test's private directory."""
    data = _gone_record()
    data["consumers"]["bot"]["intent"]["bot_binding"] = ["/data/bot", "bot", str(private_dir)]
    data["scope"] = _scope(data["consumers"])
    return data


def _gone_record(disposition="running"):
    """The frozen scope binds the bot intent."""
    data = record()
    data["consumers"]["bot"]["intent"]["disposition"] = disposition
    data["scope"] = _scope(data["consumers"])
    return data


def _quiesce_with(monkeypatch, store, adapter, checkpoint):
    events = []
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "previous"})
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    monkeypatch.setattr(bot_operations, "save_planned_checkpoint", checkpoint, raising=False)
    def quiet(store, key, adapter):
        events.append(key)
        current = store.load()
        current["consumers"][key]["stop"] = "done"
        store.save(current)
    monkeypatch.setattr(operation, "quiesce_consumer", quiet)
    return events, lambda: operation.quiesce_operation(store, adapter)


def _no_checkpoint(*_args, **_kwargs):
    pytest.fail("a bot whose session is gone must not be checkpointed")


def _bot_binding(private_dir):
    return [("/data/bot", "bot", str(private_dir))]


def test_a_bot_whose_server_exited_leaving_a_refusing_socket_is_marked_and_stopped_without_a_checkpoint(
        monkeypatch, private_dir):
    _bind(private_dir, listening=False)
    store, adapter = Store(_bound_record(private_dir)), SessionAdapter("unknown")
    events, run = _quiesce_with(monkeypatch, store, adapter, _no_checkpoint)
    run()
    assert store.load()["consumers"]["bot"]["session_gone"] is True
    assert events == ["timer", "job", "bot", "owner", "recorder"]
    assert [call[1] for call in adapter.calls] == _bot_binding(private_dir)


def test_a_bot_whose_socket_is_missing_is_marked_and_stopped_without_a_checkpoint(monkeypatch, private_dir):
    store, adapter = Store(_bound_record(private_dir)), SessionAdapter("absent")
    events, run = _quiesce_with(monkeypatch, store, adapter, _no_checkpoint)
    run()
    assert store.load()["consumers"]["bot"]["session_gone"] is True
    assert events == ["timer", "job", "bot", "owner", "recorder"]
    assert [call[1] for call in adapter.calls] == _bot_binding(private_dir)


def test_a_frozen_running_bot_with_a_ready_session_is_checkpointed_not_marked(monkeypatch, private_dir):
    server = _bind(private_dir, listening=True)
    try:
        store, adapter, taken = Store(_bound_record(private_dir)), SessionAdapter("ready"), []
        events, run = _quiesce_with(monkeypatch, store, adapter, lambda *a, **k: taken.append(1) or {"original": True})
        run()
        assert taken == [1] and "session_gone" not in store.load()["consumers"]["bot"]
    finally:
        server.close()


def test_a_server_that_still_accepts_keeps_its_checkpoint_attempt_and_is_not_marked(monkeypatch, private_dir):
    server = _bind(private_dir, listening=True)
    try:
        store, adapter = Store(_bound_record(private_dir)), SessionAdapter("unknown")
        def refuse(*_args, **_kwargs):
            raise bot_operations.BotLifecycleError("no live session")
        events, run = _quiesce_with(monkeypatch, store, adapter, refuse)
        with pytest.raises(bot_operations.BotLifecycleError, match="no live session"):
            run()
        assert "session_gone" not in store.load()["consumers"]["bot"]
        assert "bot" not in events
    finally:
        server.close()


def test_a_socket_path_that_is_a_regular_file_keeps_its_checkpoint_attempt_and_is_not_marked(
        monkeypatch, private_dir):
    (private_dir / f"tmux-{os.getuid()}" / "bot").write_text("not a socket")
    store, adapter = Store(_bound_record(private_dir)), SessionAdapter("unknown")
    def refuse(*_args, **_kwargs):
        raise bot_operations.BotLifecycleError("no live session")
    events, run = _quiesce_with(monkeypatch, store, adapter, refuse)
    with pytest.raises(bot_operations.BotLifecycleError, match="no live session"):
        run()
    assert "session_gone" not in store.load()["consumers"]["bot"]
    assert "bot" not in events


def test_a_failing_session_observer_raises_and_marks_nothing(monkeypatch, private_dir):
    store, adapter = Store(_bound_record(private_dir)), FailingObserver()
    events, run = _quiesce_with(monkeypatch, store, adapter, _no_checkpoint)
    with pytest.raises(RuntimeEvidenceError):
        run()
    assert "session_gone" not in store.load()["consumers"]["bot"]
    assert len(adapter.calls) == 1 and "bot" not in events


def test_a_bot_frozen_stopped_is_never_read_for_a_gone_session(monkeypatch):
    store, adapter = Store(_gone_record("stopped")), SessionAdapter("absent")
    events, run = _quiesce_with(monkeypatch, store, adapter, _no_checkpoint)
    run()
    assert adapter.calls == [] and "session_gone" not in store.load()["consumers"]["bot"]


def test_final_quiet_accepts_a_marked_bot_without_a_checkpoint_and_nothing_else(monkeypatch):
    def quiet_store(**fields):
        data = _gone_record()
        for item in data["consumers"].values():
            item["stop"] = "done"
        data["consumers"]["bot"].update(fields)
        return Store(data), data
    events = []
    monkeypatch.setattr(bot_operations, "verify_checkpoint_after_stop",
                        lambda directory, proof: events.append(("proof", proof)), raising=False)
    monkeypatch.setattr(operation, "observe_quiet", lambda store, key, adapter: events.append(("quiet", key)))
    monkeypatch.setattr(operation, "_schema", lambda *_: 14)
    store, data = quiet_store(session_gone=True)
    operation.assert_operation_quiet(store, None)
    assert {key for kind, key in events if kind == "quiet"} == set(data["consumers"])
    assert not [event for event in events if event[0] == "proof"]
    store, _ = quiet_store()
    with pytest.raises(UpdateError, match="running bot lacks its original checkpoint proof"):
        operation.assert_operation_quiet(store, None)
    events.clear()
    store, _ = quiet_store(session_gone=True, checkpoint={"original": True})
    operation.assert_operation_quiet(store, None)
    assert ("proof", {"original": True}) in events


def test_marking_a_gone_session_twice_reads_it_once(private_dir):
    _bind(private_dir, listening=False)
    store, adapter = Store(_bound_record(private_dir)), SessionAdapter("unknown")
    assert operation.mark_session_gone(store, "bot", adapter) is True
    assert operation.mark_session_gone(store, "bot", adapter) is True
    assert len(adapter.calls) == 1


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


def test_resume_before_stop_rechecks_original_native_identity_without_replaying(monkeypatch):
    data = record()
    for key in ("timer", "job"):
        data["consumers"][key]["stop"] = "done"
    data["consumers"]["bot"].update(checkpoint={"original": True}, checkpoint_operation="original")
    store, events = Store(data), []
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "previous"})
    def recheck(*_args, **kwargs):
        events.append(kwargs["operation_id"])
        raise bot_operations.BotLifecycleError("native process differs from retained identity")
    monkeypatch.setattr(bot_operations, "save_planned_checkpoint", recheck)
    monkeypatch.setattr(operation, "quiesce_consumer", lambda *_: pytest.fail("must not stop on changed native identity"))
    with pytest.raises(bot_operations.BotLifecycleError, match="native process differs"):
        operation.quiesce_operation(store, SessionAdapter())
    assert events == ["original"]
    assert store.load()["consumers"]["bot"].get("stop") is None


@pytest.mark.parametrize("disposition", ["running", "stopped"])
def test_update_adopts_running_session_but_leaves_stopped_bot_and_optional_handoff_alone(tmp_path, monkeypatch, disposition):
    from claudlobby import native_session as native
    from claudlobby.isolation import transcript_slug
    from tests.test_native_session import Observation, SID, provider_record

    bot, account, tmpdir = (tmp_path / part for part in ("bot", "account", "tmux"))
    (bot / "data").mkdir(parents=True)
    (bot / "data/.spawn").touch()
    optional = bot / "data/.handoff-request"
    optional.write_bytes(b'{"request_id":"unknown-original","state":"pending"}\n')
    retained = optional.read_bytes(), optional.stat().st_ino
    adapter = Observation(tmp_path)
    provider_record(bot, account, adapter, SID)
    transcript = account / "projects" / transcript_slug(bot) / (SID + ".jsonl")
    transcript.parent.mkdir(parents=True); transcript.write_bytes(b"retained older interactive context\n")
    monkeypatch.setattr(native, "_context", lambda *_: (object(), account))
    monkeypatch.setattr(native, "require_provider", lambda _: None)
    data = record()
    data["consumers"] = {"bot": data["consumers"]["bot"]}
    data["consumers"]["bot"]["intent"].update(bot_binding=[str(bot), "example.worker", str(tmpdir)],
                                              disposition=disposition)
    data["scope"] = _scope(data["consumers"])
    store = Store(data)
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "previous"})
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    stopped = []
    def quiet(store, key, adapter):
        current = store.load()
        if disposition == "running":
            proof = current["consumers"][key]["checkpoint"]
            assert proof["operation_id"] == "original" and proof["session"]["session_id"] == SID
            bot_operations.verify_checkpoint_after_stop(bot, proof)
        else:
            assert "checkpoint" not in current["consumers"][key] and native.read(bot) is None
        stopped.append(key)
        current["consumers"][key]["stop"] = "done"
        store.save(current)
    monkeypatch.setattr(operation, "quiesce_consumer", quiet)
    result = operation.quiesce_operation(store, SessionAdapter(inner=adapter))
    operation.quiesce_operation(store, SessionAdapter(inner=adapter))
    assert stopped == ["bot"] and result["request_id"] == "original"
    assert (optional.read_bytes(), optional.stat().st_ino) == retained
    assert bool(adapter.calls) == (disposition == "running")
    assert all(name == "svc_bot_native_session" for name, _ in adapter.calls)


@pytest.mark.parametrize("interrupt_second_entry", [False, True])
def test_publication_commits_config_before_restore(tmp_path, monkeypatch, interrupt_second_entry):
    import sys
    from claudlobby import installation as locations

    data = record()
    interpreter = Path(sys.executable).resolve()
    data.update(phase="environment", maintenance=True, git_complete=True,
                dependencies={"environment_complete": True}, host_interpreter=str(interpreter))
    store, events = Store(data), []
    store.installation = locations.Installation(tmp_path / "installation", tmp_path / "data")
    store.root = store.installation.data_root
    locations.write_installation(store.installation)
    store.installation.bin.mkdir()
    updater = store.installation.bin / "claudlobby-update"
    updater.write_bytes(b"previous updater")
    expected_launcher = (f"#!{interpreter}\n".encode()
                         + Path(locations.__file__).read_bytes())
    expected_updater = (f"#!{interpreter}\n".encode()
                        + Path(locations.__file__).with_name("git_update.py").read_bytes())
    monkeypatch.setattr(operation, "_schema", lambda *_: 14)
    monkeypatch.setattr(operation, "_head", lambda *_: "target")
    monkeypatch.setattr(operation, "read_rendered", lambda *_: "render")
    monkeypatch.setattr(operation, "apply_publication", lambda *_: events.append("files"))
    attempts = []
    def install_second(*args, **kwargs):
        assert store.installation.cli.read_bytes() == expected_launcher
        attempts.append(True)
        if interrupt_second_entry and len(attempts) == 1:
            raise OSError("interrupted second entry refresh")
        locations.install_updater(*args, **kwargs)
    monkeypatch.setattr(operation, "install_updater", install_second)
    def commit(*_args, **_kwargs):
        assert store.load()["phase"] == "commit"
        assert store.load()["maintenance"]
        assert store.installation.cli.read_bytes() == expected_launcher
        assert updater.read_bytes() == expected_updater
        events.append("config")
    monkeypatch.setattr(operation, "publish_current_config", commit)
    if interrupt_second_entry:
        with pytest.raises(OSError, match="second entry"):
            operation.publish_operation(store)
        assert store.load()["phase"] == "commit" and store.load()["maintenance"]
        assert events == ["files"] and updater.read_bytes() == b"previous updater"
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


@pytest.fixture
def initial_store(tmp_path):
    from claudlobby.installation import Installation
    data = record()
    data.update(initialize=True, previous_config=None, consumers={}, sql_schema=operation.SQL_SCHEMA_VERSION,
                candidate_config={"fingerprint": "initial", "identities": None,
                                  "supervision": {"manager": "Linux", "domain": "",
                                                  "directory": str(tmp_path / "units")}})
    store = Store(data)
    store.installation = Installation(tmp_path / "installation", tmp_path / "data")
    store.root = store.installation.data_root
    store.work = store.root / "state/update-work"
    store.work.mkdir(parents=True, mode=0o700)
    return store


def test_initialize_prepares_null_previous_without_creating_schema_or_identities(initial_store, monkeypatch):
    store, events = initial_store, []
    store.record = None
    rendered = SimpleNamespace(revision="target", fingerprint="render", fleets=("example",),
                               check_inputs=lambda: events.append("inputs"))
    context = SimpleNamespace(paths="frozen", fleet="example")
    package = SimpleNamespace()
    adapter = SimpleNamespace(package=package, read=lambda *_: "catalog")
    monkeypatch.setattr(operation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(operation, "_catalog", lambda *_: ("Linux", "", (), set(), {}))
    monkeypatch.setattr(operation, "context_from_plan", lambda *_args, **_kwargs: context)
    def preflight(contexts):
        assert list(contexts) == [context]
        events.append("preflight")
    monkeypatch.setattr(operation, "preflight_registry_scan", preflight)
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: pytest.fail("active read"))
    monkeypatch.setattr(operation, "read_registry_identity_bindings", lambda *_args, **_kwargs: pytest.fail("identity read"))
    def snapshot(render, installation, revision, *, identities, supervision):
        assert render is rendered and identities is None
        return {"fingerprint": "initial", "identities": None, "units": [], "supervision": supervision}
    monkeypatch.setattr(operation, "snapshot_from_plan", snapshot)
    def validate(previous, *_, baseline):
        assert baseline is None
        events.append(("candidate", previous))
    monkeypatch.setattr(operation, "validate_candidate_consumers", validate)
    def freeze(previous, _adapter, directory, *, candidate_snapshot, rendered, baseline):
        assert baseline is None
        assert previous is None and rendered.fingerprint == "render"
        assert candidate_snapshot["identities"] is None
        return []
    monkeypatch.setattr(operation, "freeze_service_intent", freeze)
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(
        stdout=f"SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n".encode()))
    def prepare(target, *, config_fingerprint, scope, request_id):
        store.record = {"phase": "prepare", "target": target, "config_fingerprint": config_fingerprint,
                        "request_id": request_id, "scope": scope}
        return store.load()
    store.prepare = prepare
    store.prepare_dependencies = lambda **_: events.append("dependencies")
    monkeypatch.setattr(operation, "prepare_publication", lambda *_: events.append("publication prepared"))
    result = operation.prepare_operation(store, rendered, adapter, store.root / "units", Path("/python"),
                                         "same-request", initialize=True)
    assert result["initialize"] is True and result["previous_config"] is None
    assert result["candidate_config"]["identities"] is None and result["consumers"] == {}
    assert result["phase"] == "quiesce"
    assert events == ["inputs", "preflight", ("candidate", None), "publication prepared", "dependencies"]
    assert not operation.db_file(store.root).exists()
    assert not (store.root / "state/config.json").exists()


@pytest.mark.parametrize("evidence", ["config.json", "selected-release.json", "activations", "migrations",
                                      "restored-backup.json", "restored-backup", "plane/plane.db", "plane/spool"])
def test_initialize_refuses_existing_active_or_recovery_state_without_overwrite(initial_store, evidence):
    store = initial_store
    path = store.root / "state" / evidence
    path.parent.mkdir(parents=True, exist_ok=True)
    if evidence in {"activations", "migrations", "restored-backup", "plane/spool"}:
        path.mkdir()
    else:
        path.write_bytes(b"original evidence")
    with pytest.raises((UpdateError, RuntimeError)):
        operation._fresh_root(store)
    assert path.is_dir() or path.read_bytes() == b"original evidence"
    assert not (store.root / "state/config.json").exists() or evidence == "config.json"


def test_existing_update_includes_added_native_consumer_in_original_scope(initial_store, monkeypatch):
    store = initial_store
    store.record = None
    placement = {"manager": "Linux", "domain": "", "directory": str(store.root / "units")}
    previous = {"fingerprint": "previous", "identities": {}, "units": [], "supervision": placement}
    candidate = {**previous, "fingerprint": "candidate", "units": [{"source": "new.service"}]}
    rendered = SimpleNamespace(revision="target", fingerprint="render", check_inputs=lambda: None)
    adapter = SimpleNamespace(package=None, read=lambda *_: "catalog")
    monkeypatch.setattr(operation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(operation, "_catalog", lambda *_: ("Linux", "", (), set(), {}))
    monkeypatch.setattr(operation, "read_current_config", lambda *_a, **_k: previous)
    monkeypatch.setattr(operation, "read_registry_identity_bindings", lambda *_a, **_k: {})
    monkeypatch.setattr(operation, "snapshot_from_plan", lambda *_a, **_k: candidate)
    monkeypatch.setattr(operation, "validate_candidate_consumers", lambda *_, baseline: None if baseline is None else pytest.fail("unexpected conversion baseline"))
    monkeypatch.setattr(operation, "_schema", lambda *_: operation.SQL_SCHEMA_VERSION)
    added = {"kind": "added", "target": "new.service", "phase": "producers"}
    def freeze(current, native, directory, *, candidate_snapshot, rendered, baseline):
        assert baseline is None
        assert current == previous and candidate_snapshot == candidate
        assert rendered.fingerprint == "render"
        return [added]
    monkeypatch.setattr(operation, "freeze_service_intent", freeze)
    def prepare(target, *, config_fingerprint, scope, request_id):
        store.record = {"phase": "prepare", "target": target, "config_fingerprint": config_fingerprint,
                        "request_id": request_id, "scope": scope}
        return store.load()
    store.prepare = prepare
    store.prepare_dependencies = lambda **_: None
    monkeypatch.setattr(operation, "prepare_publication", lambda *_: None)
    result = operation.prepare_operation(store, rendered, adapter, Path(placement["directory"]),
                                         Path("/python"), "same-request")
    assert result["previous_config"] == "previous"
    assert result["consumers"]["new.service"]["intent"] == added
    assert result["scope"] == operation.consumer_scope([added])


def test_initialize_preserves_canonical_owner_preparation(initial_store):
    from claudlobby.plane.owner_access import OwnerAccess
    from claudlobby.plane.ids import ensure_host_uid
    store = initial_store
    host = ensure_host_uid(store.root / "state")
    OwnerAccess.initialize(store.root, before_first_fleet=True)
    owner = store.root / "state/plane/owner-access.db"
    before = owner.read_bytes()
    operation._fresh_root(store)
    assert owner.read_bytes() == before
    assert (store.root / "state/host-uid").read_text().strip() == host
    assert not operation.db_file(store.root).exists()


def test_initialize_database_recovers_interrupted_no_overwrite_publish(initial_store, monkeypatch):
    from claudlobby.plane import migrations
    store = initial_store
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(
        stdout=f"SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n".encode()))
    calls, save = [], store.save
    real_migrate = migrations.migrate
    def migrate(conn):
        calls.append("migrate")
        return real_migrate(conn)
    monkeypatch.setattr(migrations, "migrate", migrate)
    def interrupt(record):
        if record.get("initialization", {}).get("database", {}).get("state") == "published":
            raise OSError("interrupted after link")
        save(record)
    store.save = interrupt
    with pytest.raises(OSError, match="after link"):
        operation._initialize_database(store)
    progress = store.load()["initialization"]["database"]
    assert progress["state"] == "ready"
    assert Path(progress["path"]).samefile(operation.db_file(store.root))
    store.save = save
    operation._initialize_database(store)
    assert calls == ["migrate"]
    assert store.load()["initialization"]["database"]["state"] == "published"
    assert not Path(progress["path"]).exists()
    conn = operation.connect_ro(operation.db_file(store.root))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == operation.SQL_SCHEMA_VERSION
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_initialize_database_does_not_adopt_foreign_final_file(initial_store, monkeypatch):
    store = initial_store
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(
        stdout=f"SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n".encode()))
    save = store.save
    def interrupt(record):
        save(record)
        if record.get("initialization", {}).get("database", {}).get("state") == "ready":
            raise OSError("before link")
    store.save = interrupt
    with pytest.raises(OSError, match="before link"):
        operation._initialize_database(store)
    foreign = operation.db_file(store.root)
    foreign.parent.mkdir(mode=0o700)
    foreign.write_bytes(b"foreign database")
    foreign.chmod(0o600)
    store.save = save
    with pytest.raises(UpdateError, match="owned staged file"):
        operation._initialize_database(store)
    assert foreign.read_bytes() == b"foreign database"


@pytest.mark.parametrize("observed", [False, True])
def test_initialize_unknown_registry_is_reobserved_without_new_scan(initial_store, monkeypatch, observed):
    from claudlobby import resources
    from claudlobby.activation_state import ActivationError
    from claudlobby.plane.registry_emit import RegistryScanError
    store = initial_store
    store.record["initialization"] = {}
    rendered = SimpleNamespace()
    package, calls = object(), []
    monkeypatch.setattr(resources, "get_resources", lambda **_: package)
    failure = RegistryScanError("unknown scan", {"scan_id": "original", "recording": "unknown"})
    def scan(*_args, **_kwargs):
        assert store.load()["initialization"]["registry"] == {"state": "pending"}
        calls.append("scan")
        raise failure
    monkeypatch.setattr(operation, "scan_registry", scan)
    with pytest.raises(RegistryScanError) as raised:
        operation._initialize_identities(store, rendered)
    assert raised.value is failure
    assert store.load()["initialization"]["registry"]["summary"] == failure.summary
    def read(*_args, **_kwargs):
        calls.append("read")
        if not observed:
            raise ActivationError("registry incomplete")
        return {"existing": "canonical IDs"}
    monkeypatch.setattr(operation, "read_registry_identity_bindings", read)
    def snapshot(*_args, identities, **_kwargs):
        return {**store.record["candidate_config"], "identities": identities, "fingerprint": "bound"}
    monkeypatch.setattr(operation, "snapshot_from_plan", snapshot)
    if observed:
        operation._initialize_identities(store, rendered)
        assert store.load()["candidate_config"]["identities"] == {"existing": "canonical IDs"}
        assert store.load()["initialization"]["registry"]["state"] == "complete"
        operation._initialize_identities(store, rendered)
    else:
        with pytest.raises(UpdateError, match="not resent"):
            operation._initialize_identities(store, rendered)
        assert store.load()["initialization"]["registry"]["state"] == "pending"
    assert calls == ["scan", "read"]


def test_initialize_quiet_has_no_current_snapshot_or_database(initial_store, monkeypatch):
    store = initial_store
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(
        stdout=f"SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n".encode()))
    monkeypatch.setattr(operation, "read_current_config", lambda *_args, **_kwargs: pytest.fail("active read"))
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    assert operation.quiesce_operation(store, None)["consumers"] == {}
    assert operation.assert_operation_quiet(store, None)["previous_config"] is None
    assert not operation.db_file(store.root).exists()


def test_initialize_publication_finishes_files_schema_ids_before_current_commit(initial_store, monkeypatch):
    store, events = initial_store, []
    store.record.update(phase="environment", maintenance=True, git_complete=True,
                        dependencies={"environment_complete": True}, host_interpreter="/python")
    monkeypatch.setattr(operation, "_head", lambda *_: "target")
    monkeypatch.setattr(operation, "_schema", lambda *_args, **_kwargs: operation.SQL_SCHEMA_VERSION)
    monkeypatch.setattr(operation, "read_rendered", lambda *_: "render")
    monkeypatch.setattr(operation, "apply_publication", lambda *_: events.append("files"))
    monkeypatch.setattr(operation, "_initialize_database", lambda *_: events.append("schema"))
    from claudlobby.plane import owner_source
    monkeypatch.setattr(owner_source, "complete_first_fleet_source", lambda *_: events.append("owner"))
    def identities(store, rendered):
        assert store.load()["maintenance"] is True and events == ["files", "schema", "owner"]
        events.append("identities")
        data = store.load()
        data["candidate_config"]["identities"] = {"actual": "registry IDs"}
        store.save(data)
    monkeypatch.setattr(operation, "_initialize_identities", identities)
    monkeypatch.setattr(operation, "install_launcher", lambda *_args, **_kwargs: events.append("launcher"))
    monkeypatch.setattr(operation, "install_updater", lambda *_args, **_kwargs: events.append("updater"))
    def commit(snapshot, *, installation, expected_previous):
        assert expected_previous is None and snapshot["identities"] == {"actual": "registry IDs"}
        assert store.load()["maintenance"] is True
        events.append("config")
    monkeypatch.setattr(operation, "publish_current_config", commit)
    result = operation.publish_operation(store)
    assert events == ["files", "schema", "owner", "identities", "launcher", "updater", "config"]
    assert result["phase"] == "restore" and result["config_committed"] and not result["maintenance"]


def test_initialize_added_bot_restore_intent_does_not_request_absent_session_checkpoint(initial_store, monkeypatch):
    store = initial_store
    added = record()["consumers"]["bot"]
    added["intent"]["kind"] = "added"
    store.record["consumers"] = {"bot": added}
    monkeypatch.setattr(operation, "_git", lambda *_: SimpleNamespace(
        stdout=f"SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n".encode()))
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    monkeypatch.setattr(bot_operations, "save_planned_checkpoint", lambda *_args, **_kwargs:
                        pytest.fail("added bot has no previous session"))
    monkeypatch.setattr(bot_operations, "verify_checkpoint_after_stop", lambda *_args, **_kwargs:
                        pytest.fail("added bot has no previous checkpoint"))
    def quiet(store, key, adapter):
        data = store.load()
        data["consumers"][key]["stop"] = "done"
        store.save(data)
    monkeypatch.setattr(operation, "quiesce_consumer", quiet)
    observed = []
    monkeypatch.setattr(operation, "observe_quiet", lambda store, key, adapter: observed.append(key))
    operation.quiesce_operation(store, None)
    result = operation.assert_operation_quiet(store, None)
    assert observed == ["bot"]
    assert result["consumers"]["bot"]["stop"] == "done"
    assert "checkpoint" not in result["consumers"]["bot"]


def test_owned_initialized_database_completes_existing_first_owner_before_registry(initial_store, monkeypatch):
    from claudlobby.plane import owner_source
    from claudlobby.plane.ids import ensure_host_uid, read_host_uid
    from claudlobby.plane.owner_access import OwnerAccess
    store = initial_store
    host = ensure_host_uid(store.root / 'state')
    access = OwnerAccess.initialize(store.root, before_first_fleet=True)
    monkeypatch.setattr(operation, '_git', lambda *_: SimpleNamespace(
        stdout=f'SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n'.encode()))
    operation._initialize_database(store)
    before = operation._initial_database(store, store.load())
    owner_source.complete_first_fleet_source(store.root)
    assert operation._initial_database(store, store.load()) == before
    assert owner_source.inspect_source(store.root) == host
    assert read_host_uid(store.root / 'state') == host and not access.first_fleet_pending()
    assert store.load()['candidate_config']['identities'] is None
    assert not (store.root / 'state/config.json').exists()


def test_publication_resumes_after_first_owner_retirement_without_rebinding(initial_store, monkeypatch):
    from claudlobby.plane import owner_source
    from claudlobby.plane.ids import ensure_host_uid
    from claudlobby.plane.owner_access import OwnerAccess, AccessUnavailable
    store = initial_store
    host = ensure_host_uid(store.root / 'state')
    access = OwnerAccess.initialize(store.root, before_first_fleet=True)
    store.record.update(phase='environment', maintenance=True, git_complete=True,
                        dependencies={'environment_complete': True}, host_interpreter='/python')
    monkeypatch.setattr(operation, '_git', lambda *_: SimpleNamespace(
        stdout=f'SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n'.encode()))
    monkeypatch.setattr(operation, '_head', lambda *_: 'target')
    monkeypatch.setattr(operation, 'read_rendered', lambda *_: 'render')
    monkeypatch.setattr(operation, 'apply_publication', lambda *_: None)
    monkeypatch.setattr(operation, 'install_launcher', lambda *_a, **_k: None)
    monkeypatch.setattr(operation, 'install_updater', lambda *_a, **_k: None)
    def identities(store, rendered):
        assert not access.first_fleet_pending() and owner_source.inspect_source(store.root) == host
        data = store.load()
        data['candidate_config']['identities'] = {'existing': 'ids'}
        store.save(data)
    monkeypatch.setattr(operation, '_initialize_identities', identities)
    def interrupted(*_a, **_k):
        raise OSError('interrupted before config commit')
    monkeypatch.setattr(operation, 'publish_current_config', interrupted)
    with pytest.raises(OSError, match='before config commit'):
        operation.publish_operation(store)
    assert store.load()['phase'] == 'commit' and not access.first_fleet_pending()
    assert not (store.root / 'state/config.json').exists()
    # The default fresh preparation validation stays strict.
    with pytest.raises(AccessUnavailable):
        access.validate_storage()
    monkeypatch.setattr(owner_source, 'bind_source', lambda *_: pytest.fail('must inspect retired binding'))
    commits = []
    monkeypatch.setattr(operation, 'publish_current_config', lambda snapshot, **_: commits.append(snapshot))
    result = operation.publish_operation(store)
    assert result['phase'] == 'restore' and result['config_committed']
    assert len(commits) == 1 and owner_source.inspect_source(store.root) == host


@pytest.mark.parametrize('defect', ['unbound', 'foreign'])
def test_committing_owned_database_refuses_retired_changed_source(initial_store, monkeypatch, defect):
    import sqlite3
    from claudlobby.plane import owner_source
    from claudlobby.plane.ids import ensure_host_uid
    from claudlobby.plane.owner_access import OwnerAccess
    store = initial_store
    ensure_host_uid(store.root / 'state')
    OwnerAccess.initialize(store.root, before_first_fleet=True)
    monkeypatch.setattr(operation, '_git', lambda *_: SimpleNamespace(
        stdout=f'SQL_SCHEMA_VERSION = {operation.SQL_SCHEMA_VERSION}\n'.encode()))
    operation._initialize_database(store)
    owner_source.complete_first_fleet_source(store.root)
    store.record['phase'] = 'commit'
    with sqlite3.connect(operation.db_file(store.root)) as conn:
        if defect == 'unbound':
            conn.execute('DROP TABLE owner_source_binding')
        else:
            conn.execute('UPDATE owner_source_binding SET host_uid=?', ('host_' + 'f' * 32,))
    monkeypatch.setattr(owner_source, 'bind_source', lambda *_: pytest.fail('must not re-attest'))
    with pytest.raises((owner_source.SourceNeedsBinding, owner_source.SourceDenied)):
        operation._fresh_root(store, store.load(), committed=True)


@pytest.mark.parametrize("changed", [False, True])
def test_conversion_last_check_reuses_original_evidence_and_native_scope(monkeypatch, changed):
    from claudlobby.update_services import consumer_scope
    data = record()
    original = [{"target": "original.timer", "saved_native": "enabled loaded inactive"}]
    data["scope"] = consumer_scope(original)
    data["native_install_directory"] = "/units"
    store = Store(data)
    baseline = object()
    calls = []
    monkeypatch.setattr(operation, "conversion_baseline", lambda s, package: calls.append("baseline") or baseline)
    rendered = SimpleNamespace(check_inputs=lambda: calls.append("inputs"))
    monkeypatch.setattr(operation, "read_rendered", lambda *_: rendered)
    def freeze(current, adapter, directory, **kwargs):
        assert current is None and directory == Path("/units")
        assert kwargs == {"candidate_snapshot": data["candidate_config"], "rendered": rendered, "baseline": baseline}
        calls.append("native")
        return [{**original[0], "saved_native": "disabled loaded inactive"}] if changed else original
    monkeypatch.setattr(operation, "freeze_service_intent", freeze)
    if changed:
        with pytest.raises(UpdateError, match="native intent changed"):
            operation.check_conversion_before_retirement(store, SimpleNamespace(package=object()))
    else:
        operation.check_conversion_before_retirement(store, SimpleNamespace(package=object()))
    assert calls == ["baseline", "inputs", "native"]
    assert store.history == []  # Inspection cannot replace the original operation.


@pytest.mark.parametrize("state", ["enabled loaded inactive", "disabled loaded inactive"])
def test_conversion_last_check_allows_scheduled_finish_but_preserves_enrollment(monkeypatch, state):
    from claudlobby.update_services import consumer_scope
    original = {"target": "job.service", "scheduled": True, "saved_native": "enabled loaded active",
                "restore_native": "enabled loaded inactive", "properties": [["UnitFileState", "enabled"],
                ["LoadState", "loaded"], ["ActiveState", "active"]]}
    data = record()
    data.update(scope=consumer_scope([original]), consumers={"job.service": {"intent": original}},
                native_install_directory="/units")
    store = Store(data)
    observed = deepcopy(original)
    observed["saved_native"] = state
    observed["properties"][-1][1] = "inactive"
    if state.startswith("disabled"):
        observed["properties"][0][1] = "disabled"
    monkeypatch.setattr(operation, "conversion_baseline", lambda *_: object())
    monkeypatch.setattr(operation, "read_rendered", lambda *_: SimpleNamespace(check_inputs=lambda: None))
    monkeypatch.setattr(operation, "freeze_service_intent", lambda *args, **kwargs: [observed])
    if state.startswith("disabled"):
        with pytest.raises(UpdateError, match="native intent changed"):
            operation.check_conversion_before_retirement(store, SimpleNamespace(package=object()))
    else:
        operation.check_conversion_before_retirement(store, SimpleNamespace(package=object()))
    assert store.history == []
    assert store.load()["consumers"]["job.service"]["intent"] == original


@pytest.mark.parametrize("owner", [None, "on", "off"])
def test_a_computer_starts_with_no_team(tmp_path, monkeypatch, owner):
    """Phase 1's proof: the first activation, without a team, dispatched by a run that holds a lease.

    The run that asks for the start holds the runtime reader lease, as every guide
    run does. The updater drains it before its cutover: nothing is committed
    while that run lives, and the start finishes once it has exited.
    """
    import sqlite3
    from claudlobby import git_update as updater
    from claudlobby.active_config import read_current_config
    from claudlobby.git_update import read_update
    from claudlobby.installation import _runtime_lock
    from claudlobby.plane.owner_source import inspect_source
    from tests.no_team_start import Case, default_on_jobs, job_name
    case = Case(tmp_path, monkeypatch, owner=owner)
    lease = _runtime_lock(case.installation)  # the guide's reader lease
    dispatched = updater.apply_or_continue(case.installation, target=case.target, initialization=case.scope)
    assert dispatched["job"] == {"dispatch": "started"}
    assert case.dispatches == [dispatched["update"]["request_id"]]
    assert dispatched["update"]["phase"] == "quiesce" and dispatched["update"]["initialization_scope"] == case.scope
    assert not operation.db_file(case.root).exists() and not (case.root / "state/config.json").exists()
    seen = []

    def guide_exits():
        record = read_update(case.installation)
        seen.append((record["phase"], record.get("runtime_waits", []), "quiesce" in case.trace,
                     operation.db_file(case.root).exists(), (case.root / "state/config.json").exists()))
        os.close(lease)
    result = case.run_job(at={3: guide_exits})
    # While the run that dispatched it lived, the updater polled at its drain and changed
    # nothing: at the third poll no gate had been taken and nothing was quiesced or written.
    assert seen == [("quiesce", [], False, False, False)]
    assert result["phase"] == "done" and result["health_errors"] == {}
    # It took the drain only after the lease closed at that poll, and the cutover found
    # the lock free. Counted in polls, not seconds: a fast runner records a 0.0 s wait.
    drain, cutover = result["runtime_waits"]
    assert drain["gate"] == "drain" and drain["outcome"] == "taken" and case.polls == 3
    assert cutover["gate"] == "cutover" and cutover["outcome"] == "taken"
    assert case.installation.preflight() == case.target
    # A bound plane.db that holds no team.
    assert inspect_source(case.root) == case.host_uid and not case.access.first_fleet_pending()
    connection = sqlite3.connect(operation.db_file(case.root))
    try:
        assert connection.execute("SELECT count(*) FROM identity_registry WHERE kind != 'host'").fetchone() == (0,)
    finally:
        connection.close()
    # A committed configuration that lists no team, and no identities.
    config = read_current_config(case.root, installation=case.installation, revision=case.target)
    assert config["fleets"] == {} and config["identities"] is None
    # Exactly the default-on jobs, and the lasting service only where the settings switch it on.
    expected = default_on_jobs() | ({"plane-owner"} if owner == "on" else set())
    assert {job_name(key) for key in result["consumers"]} == expected
    assert all(item["restore"] == "done" for item in result["consumers"].values())
    assert {path.name for path in case.directory.iterdir() if path.is_file()} == set(result["consumers"])


def test_an_ordinary_update_of_a_computer_started_with_no_team_reaches_done(tmp_path, monkeypatch):
    """The ground phase 2 stands on: a started computer with no team is updated from one revision to the next."""
    from claudlobby.active_config import read_current_config
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch)
    started = case.start()
    assert started["phase"] == "done" and started["health_errors"] == {}
    first = read_current_config(case.root, installation=case.installation, revision=case.target)
    later = case.add_target()
    calls = len(case.native.calls)
    seen = []
    real = operation.prepare_operation

    def prepare(store, rendered, *args, **kwargs):
        seen.append((rendered.fleets, kwargs["initialize"]))
        return real(store, rendered, *args, **kwargs)
    monkeypatch.setattr(operation, "prepare_operation", prepare)
    updated = case.update(later)
    assert updated["phase"] == "done" and updated["health_errors"] == {}
    assert updated["previous_config"] == first["fingerprint"] and not updated.get("initialize")
    assert seen == [((), False)]
    assert case.installation.preflight() == later
    second = read_current_config(case.root, installation=case.installation, revision=later)
    # A start and the ordinary update after it hold the same identities: None.
    assert first["identities"] is None and second["identities"] is None
    assert second["fleets"] == {} and second["units"] == first["units"]
    assert set(updated["consumers"]) == set(started["consumers"])
    assert all(item["restore"] == "done" for item in updated["consumers"].values())
    # Every unit the start installed was stopped for the cutover and brought back as it was.
    paused = {args[1] for name, args in case.native.calls[calls:] if name == "svc_activation_pause"}
    resumed = {args[1] for name, args in case.native.calls[calls:] if name == "svc_activation_resume"}
    assert paused == resumed == {key for key in updated["consumers"] if "@." not in key}
    assert all(case.native.states[key] == updated["consumers"][key]["intent"]["restore_native"] for key in resumed)
    assert operation.db_file(case.root).exists()


def test_a_computer_started_with_no_team_is_admitted_and_checked_like_any_other(tmp_path, monkeypatch):
    """Runtime admission and the operator check each read the empty configuration.

    Update authorization (`update_entry.main`'s authorize stage) is not run here: it
    checks its own process's source and interpreter. This checks what it reads after
    the operator check: the committed configuration's native placement.
    """
    from claudlobby import context, supervision_inventory
    from claudlobby.commands.operator_context import require_operator_context
    from claudlobby.command_result import CommandFailure
    from claudlobby.runtime_admission import RuntimeIdentity, mutation_admission
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch)
    case.start()
    package = case.package(case.target)
    identity = RuntimeIdentity(case.installation.cli, case.installation.native, "", case.installation, case.target)
    monkeypatch.setattr(RuntimeIdentity, "current", classmethod(lambda cls: identity))
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(case.installation.directory))
    with mutation_admission(case.root, identity=identity) as current:
        assert current.snapshot["fleets"] == {} and current.revision == case.target
    # The operator check finds the started computer's own units and asks each about its caller.
    monkeypatch.setattr(context, "resolve_paths", lambda root=None, **_: SimpleNamespace(package=package))
    installed = sorted(path for path in case.directory.iterdir() if path.is_file())
    monkeypatch.setattr(supervision_inventory, "collect_enrollment", lambda *_args, **_kwargs: SimpleNamespace(
        require_complete=lambda: SimpleNamespace(units=tuple(
            SimpleNamespace(target=path.name, installed=(SimpleNamespace(path=path),)) for path in installed))))
    before = len(case.native.calls)
    require_operator_context(case.root)
    asked = [args[1] for name, args in case.native.calls[before:] if name == "svc_activation_assert_external"]
    assert asked == [path.name for path in installed if "@." not in path.name] and asked
    monkeypatch.setenv("BOT_ID", "worker")
    with pytest.raises(CommandFailure, match="operator shell"):
        require_operator_context(case.root)
    monkeypatch.delenv("BOT_ID")
    # The native placement update authorization reads after that operator check.
    from claudlobby.active_config import read_current_config
    snapshot = read_current_config(case.root, installation=case.installation, revision=package.source_revision)
    assert snapshot["supervision"] == {"manager": "Linux", "domain": "", "directory": str(case.directory)}


import subprocess  # noqa: E402
import sys  # noqa: E402


# Phase 2, step 7: the proofs for the update actions of the preparation script.
#
# The computer is tests/no_team_start.py's, laid out where the preparation script
# lays one out (`Case(home=...)`), with a receipt at the revision it was started at
# and the source that receipt recorded holding a later commit. Every `host update`
# command runs with the update actions' own environment, built by the script's own
# `update_environment`, through the launcher's update route into the updater's
# `main`. The stand-ins are the fixture's: the native layer, the operator gate and
# the updater's job, whose stages run in this process.

#: The maintenance phases the updater enters. `migrate` is in its list of phases
#: but no stage sets it (publication goes from `publish` to `commit`), so the
#: fourth phase a job can stop in with maintenance on is `commit`.
MAINTENANCE = ("git", "environment", "publish", "commit")


def _script():
    from tests.test_host_install import payload
    return payload()


def _recorded_source(case, tmp_path, *, at=None):
    """The preparation's receipt at `at` (the installed revision), its source holding a later commit."""
    import json as _json
    from claudlobby import git_update as updater
    from tests.no_team_start import LOCK
    from tests.test_package_resources import _run
    at = at or updater._head(case.installation)
    remote = tmp_path / "recorded-source"
    _run(["git", "clone", "--quiet", "--no-hardlinks", case.installation.source, remote], tmp_path)
    _run(["git", "-C", remote, "config", "maintenance.auto", "false"], tmp_path)
    _run(["git", "-C", remote, "checkout", "--quiet", "-B", "later", case.target], tmp_path)
    (remote / "templates/candidate.txt").write_text("later bytes\n")
    _run(["git", "-C", remote, "add", "."], tmp_path)
    _run(["git", "-C", remote, "-c", "user.name=Example", "-c", "user.email=example@example.invalid",
          "commit", "--quiet", "-m", "Later source"], tmp_path)
    later = _run(["git", "-C", remote, "rev-parse", "HEAD"], tmp_path).strip()
    python = Path(sys.executable).resolve()
    receipt = {"schema": 1, "purpose": "installation-preparation-only",
               "installation": str(case.installation.directory), "data_root": str(case.root),
               "remote": str(remote), "ref": at, "interpreter": str(python),
               "interpreter_sha256": updater._digest(python.read_bytes()), "revision": at,
               "lock_sha256": updater._digest(LOCK), "environment_owned": True, "complete": True}
    path = case.installation.directory / "preparation.json"
    path.write_text(_json.dumps(receipt, sort_keys=True))
    path.chmod(0o600)
    return later, path.read_bytes()


def _as_the_script_runs(case, monkeypatch, *argv, without=()):
    """`host update <argv>` in the update actions' environment, through the launcher's update route."""
    import contextlib
    import io
    import json as _json
    from claudlobby import git_update as updater, installation as launcher
    environment = _script().update_environment(case.home)
    for key in without:
        environment.pop(key)
    arguments = ["--root", str(case.root), "host", "update", *argv]
    output = io.StringIO()
    with monkeypatch.context() as patch:
        for key in list(os.environ):
            patch.delenv(key)
        for key, value in environment.items():
            patch.setenv(key, value)
        assert launcher._update_route(arguments)
        assert launcher._bound_arguments(case.installation, arguments) == arguments
        patch.setattr(updater, "__file__", str(case.installation.bin / "claudlobby-update"))
        with contextlib.redirect_stdout(output):
            code = updater.main(arguments)
    return code, _json.loads(output.getvalue())


def _launcher(case, *argv):
    """An ordinary command through the real stable launcher, in the update actions' environment."""
    return subprocess.run([sys.executable, "-I", "-S", "-B", str(case.installation.cli), "--root", str(case.root),
                           *argv], env=_script().update_environment(case.home), capture_output=True, text=True,
                          timeout=120)


def _fail_once(monkeypatch, phase):
    """The updater's job stops once in `phase`, with maintenance on."""
    from claudlobby.git_update import UpdateStore
    owner, name = {"git": (UpdateStore, "cutover_git"), "environment": (UpdateStore, "cutover_environment"),
                   "publish": (operation, "apply_publication"),
                   "commit": (operation, "publish_current_config")}[phase]
    real, failed = getattr(owner, name), []

    def once(*args, **kwargs):
        if not failed:
            failed.append(phase)
            raise RuntimeError(f"fixture job failure in {phase}")
        return real(*args, **kwargs)
    monkeypatch.setattr(owner, name, once)
    return failed


def _measured(label, seconds):
    print(f"\nMEASURED {label}: {seconds:.3f} s")


def _owner_computer(tmp_path, monkeypatch, home):
    """tests/test_owner_computer_run.py's computer, laid out as the preparation script lays one out."""
    import functools
    from tests import test_owner_computer_run as computer_run
    from tests.no_team_start import Case
    monkeypatch.setattr(computer_run, "Case", functools.partial(Case, home=home))
    return computer_run.Computer(tmp_path, monkeypatch)


LASTING = "claudlobby-plane-owner.service"


@pytest.mark.parametrize("owner", ["on", "off"])
def test_a_started_computer_moves_to_a_named_revision(tmp_path, monkeypatch, owner):
    """`host update apply Y` from the update actions' process moves a started computer with no team to Y.

    The process that asks holds no lease and, in the real world, is outside every
    managed unit (the operator gate is the fixture's; the gate's own dependence on
    the environment is the next test's). With the settings switching the lasting
    service on, the computer is started by the computer run; with them saying
    `enroll: false`, by phase 1's start.
    """
    import time as _time
    from claudlobby.git_update import read_update
    from claudlobby.plane.owner_source import start_state
    from tests.no_team_start import Case
    home = tmp_path / "account"
    if owner == "on":
        computer = _owner_computer(tmp_path, monkeypatch, home)
        case = computer.case
        started = _time.monotonic()
        assert computer.run().data["state"] == "computer_starting"
        case.run_job()
        _measured("start (computer run to done)", _time.monotonic() - started)
    else:
        case = Case(tmp_path, monkeypatch, owner="off", home=home)
        case.start()
    assert start_state(case.root, case.installation) == "started"
    later, receipt = _recorded_source(case, tmp_path)
    database = operation.db_file(case.root).stat()
    calls = len(case.native.calls)
    started = _time.monotonic()
    code, dispatched = _as_the_script_runs(case, monkeypatch, "apply", later)
    before_dispatch = _time.monotonic() - started
    assert code == 0 and dispatched["job"] == {"dispatch": "started"}
    record = dispatched["update"]
    assert (record["target"], record["phase"], record["maintenance"]) == (later, "quiesce", False)
    assert case.dispatches[-1] == record["request_id"] and not record.get("initialization_scope")
    finished = case.run_job()
    whole = _time.monotonic() - started
    assert finished["phase"] == "done" and finished["health_errors"] == {}
    assert finished["request_id"] == record["request_id"]
    # The process that asked held no lease: the job took both gates at its first try.
    assert case.polls == 0 and [row["outcome"] for row in finished["runtime_waits"]] == ["taken", "taken"]
    assert case.installation.preflight() == later
    assert (case.installation.directory / "preparation.json").read_bytes() == receipt
    assert start_state(case.root, case.installation) == "started"
    after = operation.db_file(case.root).stat()
    assert (after.st_dev, after.st_ino) == (database.st_dev, database.st_ino)
    paused = {args[1] for name, args in case.native.calls[calls:] if name == "svc_activation_pause"}
    resumed = {args[1] for name, args in case.native.calls[calls:] if name == "svc_activation_resume"}
    if owner == "on":
        # The settings file reached the render: the lasting service was stopped and restored.
        assert LASTING in finished["consumers"] and LASTING in paused and LASTING in resumed
    else:
        assert LASTING not in finished["consumers"] and not (case.directory / LASTING).exists()
    _measured(f"update, the part of apply before dispatch (owner {owner})", before_dispatch)
    _measured(f"update, apply to done (owner {owner})", whole)
    if owner == "on":
        # A computer run at Y that names the finished record is admitted and asks nothing.
        from tests.test_owner_computer_run import HOST, lasting_service
        computer.revision = later
        computer.profile.receipt_revision = _json_loads(receipt)["ref"]
        with lasting_service(computer):
            again = computer.run(update_record=[record["request_id"], later])
        assert again.data == {"state": "computer_ready", "origin": HOST}
        assert computer.channel.cards == [] and read_update(case.installation)["request_id"] == record["request_id"]


def _json_loads(raw):
    import json as _json
    return _json.loads(raw)


def test_the_update_renders_this_computers_settings_through_the_built_environment(tmp_path, monkeypatch):
    """Without the settings variable the render takes the packaged default and drops the lasting service.

    The updater then refuses the update before maintenance (it never removes a
    consumer), and leaves its record at `prepare`. With the variable, the same
    computer's update renders the service and reaches `done`
    (`test_a_started_computer_moves_to_a_named_revision`).
    """
    from claudlobby.git_update import read_update
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, owner="on", home=tmp_path / "account")
    started = case.start()
    assert LASTING in started["consumers"]
    later, _ = _recorded_source(case, tmp_path)
    with pytest.raises(UpdateError, match="candidate removes native consumers"):
        _as_the_script_runs(case, monkeypatch, "apply", later, without=("CLAUDLOBBY_HOST_SYSTEM_YAML",))
    record = read_update(case.installation)
    assert (record["target"], record["phase"], record["maintenance"]) == (later, "prepare", False)
    assert record.get("job_dispatch") is None and case.installation.preflight() == case.target


@pytest.mark.parametrize("kind", ["update", "start"])
@pytest.mark.parametrize("phase", MAINTENANCE)
def test_a_failure_in_maintenance_is_continued(tmp_path, monkeypatch, kind, phase):
    """A job that stops in maintenance: every ordinary command and a guide run refused; continue finishes it."""
    from claudlobby import git_update as updater
    from claudlobby.git_update import read_update
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, home=tmp_path / "account")
    if kind == "update":
        case.start()
        later, _ = _recorded_source(case, tmp_path)
        _code, dispatched = _as_the_script_runs(case, monkeypatch, "apply", later)
        request = dispatched["update"]["request_id"]
    else:
        request = updater.apply_or_continue(case.installation, target=case.target,
                                            initialization=case.scope)["update"]["request_id"]
    failed = _fail_once(monkeypatch, phase)
    with pytest.raises(RuntimeError, match=f"failure in {phase}"):
        case.run_job()
    record = read_update(case.installation)
    assert failed == [phase]
    assert (record["request_id"], record["phase"], record["maintenance"]) == (request, phase, True)
    for argv in (["host", "status"], ["host", "owner", "setup", "--stdio-computer"]):
        refused = _launcher(case, *argv)
        assert refused.returncode == 6 and "maintenance is active" in refused.stderr, refused.stderr
    dispatches = len(case.dispatches)
    code, continued = _as_the_script_runs(case, monkeypatch, "continue", "--expected-request", request)
    assert code == 0 and continued["update"]["request_id"] == request
    assert case.dispatches[dispatches:] == [request]
    finished = case.run_job()
    assert (finished["request_id"], finished["phase"], finished["health_errors"]) == (request, "done", {})
    assert case.installation.preflight() == (later if kind == "update" else case.target)


def _team_preparation(case, kind, bound):
    """A team preparation left undispatched: a Preview, or (`bound`) a prepared team whose Launch never dispatched.

    The record's render is a no-team render, made the way a start makes one; only
    its scope names a team file (a first install's manifests, or phase 10's
    `addition_scope`). What `continue` decides reads that scope and the dispatch.
    """
    import json
    from claudlobby import git_update as updater
    from claudlobby.git_update import UpdateStore, read_update
    # Outside `local/`: a team file there that the no-team render does not name
    # is refused by every later render ("host plan omits local fleet declarations").
    team = case.root.parent / "authored-team/fleet.yaml"
    if kind == "first_team":
        team.parent.mkdir(parents=True)
        team.write_text("# a fixture team file\n")
        with UpdateStore(case.installation) as store:
            record = updater._prepare_update_locked(store, target=case.target, initialization=case.scope,
                                                    prepare_only=True)
            # The store refuses to change a frozen scope, so the record is written as the store writes it.
            record["initialization_scope"] = {**record["initialization_scope"], "manifests": [str(team)]}
            updater._write(store.state / "update.json",
                           json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    else:
        case.start()
        later = case.add_target()
        team.parent.mkdir(parents=True)
        team.write_text("# a fixture team file\n")
        with UpdateStore(case.installation) as store:
            record = updater._prepare_update_locked(store, target=later)
            record["addition_scope"] = {"manifests": [str(team)]}
            store.save(record)
    if bound:
        with UpdateStore(case.installation) as store:
            case._job_support(store)
    record = read_update(case.installation)
    assert record["phase"] == "quiesce" and record.get("job_dispatch") is None
    assert (record.get("updater_job") is not None) is bound
    return record


@pytest.mark.parametrize("bound", [False, True], ids=["preview", "prepared"])
@pytest.mark.parametrize("kind", ["first_team", "addition"])
def test_continue_never_dispatches_a_team_nobody_approved(tmp_path, monkeypatch, kind, bound):
    """`host update continue --expected-request` refuses an undispatched team preparation and changes nothing.

    Only its Launch card dispatches it. Once dispatched, a stage that fails is
    continued by the same command to `done`.
    """
    from claudlobby import git_update as updater
    from claudlobby.git_update import NotContinuable, read_update
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, home=tmp_path / "account")
    record = _team_preparation(case, kind, bound)
    path = case.root / "state/update.json"
    raw, dispatches = path.read_bytes(), list(case.dispatches)
    for _ in range(2):
        with pytest.raises(NotContinuable):
            _as_the_script_runs(case, monkeypatch, "continue", "--expected-request", record["request_id"])
        assert path.read_bytes() == raw and case.dispatches == dispatches
    # The Launch card: after its grant is reserved, the Launch dispatches the same record.
    launched = updater.apply_or_continue(case.installation, expected_request_id=record["request_id"])
    assert launched["job"] == {"dispatch": "started"} and case.dispatches[len(dispatches):] == [record["request_id"]]
    _fail_once(monkeypatch, "git")
    with pytest.raises(RuntimeError, match="failure in git"):
        case.run_job()
    assert read_update(case.installation)["phase"] == "git"
    code, continued = _as_the_script_runs(case, monkeypatch, "continue", "--expected-request", record["request_id"])
    assert code == 0 and continued["update"]["request_id"] == record["request_id"]
    finished = case.run_job()
    assert (finished["request_id"], finished["phase"], finished["health_errors"]) == (record["request_id"], "done", {})


def test_continue_serves_a_start_nothing_dispatched(tmp_path, monkeypatch):
    from claudlobby import git_update as updater
    from claudlobby.git_update import UpdateStore
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, home=tmp_path / "account")
    with UpdateStore(case.installation) as store:
        record = updater._prepare_update_locked(store, target=case.target, initialization=case.scope,
                                                prepare_only=True)
    assert record["phase"] == "quiesce" and record.get("job_dispatch") is None
    code, continued = _as_the_script_runs(case, monkeypatch, "continue", "--expected-request", record["request_id"])
    assert code == 0 and case.dispatches == [record["request_id"]]
    finished = case.run_job()
    assert (finished["request_id"], finished["phase"], finished["health_errors"]) == (record["request_id"], "done", {})


def test_a_done_record_with_health_errors_is_continued(tmp_path, monkeypatch):
    """A start whose restore fails for the lasting service is `done` with health errors: unfinished."""
    from claudlobby.git_update import UpdateError
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, owner="on", home=tmp_path / "account")
    real, failed = operation.restore_consumer, []

    def restore(store, key, *args, **kwargs):
        if key == LASTING and not failed:
            failed.append(key)
            raise RuntimeError("fixture: the lasting service did not start")
        return real(store, key, *args, **kwargs)
    monkeypatch.setattr(operation, "restore_consumer", restore)
    finished = case.start()
    request = finished["request_id"]
    assert finished["phase"] == "done" and finished["health_errors"][LASTING].startswith("fixture:")
    # Core's own rule: a schedule is restored only after every service is.
    assert all(key.endswith(".timer") and line == "restore services before new schedules"
               for key, line in finished["health_errors"].items() if key != LASTING)
    later, _ = _recorded_source(case, tmp_path)
    script = _script()
    code, status = _as_the_script_runs(case, monkeypatch, "status", "--json")
    shown = case.installation.preflight()
    update = script.summary(status["update"])
    assert (update["kind"], update["phase"], update["failed"]) == ("start", "done", True)
    assert script.update_refusal(update, shown, shown, later, True) == "update_in_progress"
    assert script.continue_refusal(update, request) is None
    # The updater's own refusal, which a client could not read.
    with pytest.raises(UpdateError, match="continue the existing frozen target"):
        _as_the_script_runs(case, monkeypatch, "apply", later)
    code, continued = _as_the_script_runs(case, monkeypatch, "continue", "--expected-request", request)
    assert code == 0 and continued["update"]["request_id"] == request
    again = case.run_job()
    assert (again["request_id"], again["phase"], again["health_errors"]) == (request, "done", {})
    _code, status = _as_the_script_runs(case, monkeypatch, "status", "--json")
    assert script.continue_refusal(script.summary(status["update"]), request) == "nothing_to_continue"


@pytest.mark.parametrize("stage", ["untouched", "mid-cards"])
def test_a_prepared_computer_that_is_not_started_refuses(tmp_path, monkeypatch, stage):
    """No committed configuration: `host update apply Y` is refused at authorization, before any fetch.

    Known from code (update_entry.py authorizes against the committed
    configuration). Fork S2-R's lean decides what the product says: `needs_reset`.
    The fixture's authorization reads the same committed configuration.
    """
    from claudlobby.config_plan import PlanError
    from claudlobby.git_update import read_update
    from tests.no_team_start import Case
    case = Case(tmp_path, monkeypatch, home=tmp_path / "account")
    if stage == "untouched":
        for path in case.access.path.parent.glob(case.access.path.name + "*"):
            path.unlink()
        (case.root / "state/host-uid").unlink()
    later, receipt = _recorded_source(case, tmp_path)
    with pytest.raises(PlanError, match="effective configuration is missing"):
        _as_the_script_runs(case, monkeypatch, "apply", later)
    assert read_update(case.installation) is None and case.installation.preflight() == case.before
    assert (case.installation.directory / "preparation.json").read_bytes() == receipt
    assert not (case.installation.source / ".git/FETCH_HEAD").exists()
    assert _script().update_refusal(None, case.before, case.before, later, False) == "needs_reset"
