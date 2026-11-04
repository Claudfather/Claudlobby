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
    monkeypatch.setattr(operation, "validate_candidate_consumers", lambda previous, *_:
                        events.append(("candidate", previous)))
    def freeze(previous, _adapter, directory, *, candidate_snapshot, rendered):
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
    monkeypatch.setattr(operation, "validate_candidate_consumers", lambda *_: None)
    monkeypatch.setattr(operation, "_schema", lambda *_: operation.SQL_SCHEMA_VERSION)
    added = {"kind": "added", "target": "new.service", "phase": "producers"}
    def freeze(current, native, directory, *, candidate_snapshot, rendered):
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
