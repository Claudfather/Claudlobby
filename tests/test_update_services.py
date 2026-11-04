"""Focused filesystem recovery and no-repeat native effect contracts."""
import base64
from copy import deepcopy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import update_services as services
from claudlobby.git_update import UpdateError


class Store:
    def __init__(self, tmp_path, intent):
        self.work = tmp_path / "update-work"
        self.record = {"consumers": {intent["target"]: {"intent": intent}},
                       "scope": services.consumer_scope([intent]), "quiet": {}}
        self.history = []

    def _assert_locked(self):
        pass

    def load(self):
        return deepcopy(self.record)

    def save(self, value):
        self.record = deepcopy(value)
        self.history.append(deepcopy(value))


class Native:
    def __init__(self):
        self.calls = []
        self.state = "disabled loaded inactive"
        self.fail_pause = False
        self.fail_resume = False
        self.session = "absent"

    def call(self, function, *args, timeout=30):
        self.calls.append((function, args))
        if function == "svc_activation_pause":
            if self.fail_pause:
                raise TimeoutError()
            self.state = "masked-runtime masked inactive"
        if function == "svc_activation_resume":
            if self.fail_resume:
                raise TimeoutError()
            self.state = args[2]
        output = self.state if function in {"svc_activation_snapshot", "svc_inventory_state"} else ""
        if function == "svc_activation_quiet":
            output = "inactive\tcgroup-empty" if self.state.endswith(" inactive") else "unknown"
        if function == "svc_bot_session_observe":
            output = self.session
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def intent(tmp_path):
    generated = tmp_path / "generated.service"
    installed = tmp_path / "example.service"
    generated.write_bytes(b"[Service]\nExecStart=/bin/true\n")
    installed.write_bytes(generated.read_bytes())
    def snapshot(path):
        return {"path": str(path), "resolved": str(path), "mode": path.stat().st_mode & 0o777,
                "link": None, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content": {"base64": base64.b64encode(path.read_bytes()).decode()}}
    return {"target": installed.name, "manager": "Linux", "installed": [snapshot(installed)],
            "generated": snapshot(generated), "scheduled": False, "bot_binding": None,
            "disposition": "resident", "saved_native": "disabled loaded inactive",
            "restore_native": "disabled loaded inactive", "properties": [],
            "declaration": {"source": str(generated), "working_directory": str(tmp_path),
                            "scope": "host", "environment": []}}


def declarations(monkeypatch, row):
    monkeypatch.setattr(services, "current_git_declarations", lambda *_: [SimpleNamespace(
        source=Path(row["declaration"]["source"]), working_directory=Path(row["declaration"]["working_directory"]))])


def test_parks_exact_file_before_pause_and_restores_disabled_inactive(tmp_path, monkeypatch):
    row = intent(tmp_path)
    store, native = Store(tmp_path, row), Native()
    declarations(monkeypatch, row)
    services.quiesce_consumer(store, row["target"], native)
    assert not Path(services._file(row)).exists()
    assert services._park_path(store, row).read_bytes() == Path(row["generated"]["path"]).read_bytes()
    assert store.record["consumers"][row["target"]]["stop"] == "done"
    services.restore_consumer(store, row["target"], native, {})
    assert Path(services._file(row)).is_file()
    assert native.state == "disabled loaded inactive"
    assert store.record["consumers"][row["target"]]["restore"] == "done"


def test_unknown_stop_is_observed_without_resending(tmp_path):
    row = intent(tmp_path)
    store, native = Store(tmp_path, row), Native()
    native.fail_pause = True
    with pytest.raises(Exception):
        services.quiesce_consumer(store, row["target"], native)
    native.state = "masked-runtime masked inactive"
    services.quiesce_consumer(store, row["target"], native)
    assert sum(call[0] == "svc_activation_pause" for call in native.calls) == 1


@pytest.mark.parametrize("frozen,observed", [("stopped", "ready"), ("running", "absent")])
def test_bot_session_drift_refuses_before_parking_or_stop(tmp_path, frozen, observed):
    row = intent(tmp_path)
    row.update(bot_binding=[str(tmp_path), "example", str(tmp_path)], disposition=frozen)
    store, native = Store(tmp_path, row), Native()
    native.session = observed
    with pytest.raises(UpdateError, match="session disposition drifted"):
        services.quiesce_consumer(store, row["target"], native)
    assert Path(services._file(row)).is_file()
    assert not services._park_path(store, row).exists()
    assert not any(call[0] == "svc_activation_pause" for call in native.calls)


def test_unknown_start_never_replays_resume(tmp_path, monkeypatch):
    row = intent(tmp_path)
    row["saved_native"] = row["restore_native"] = "enabled loaded active"
    store, native = Store(tmp_path, row), Native()
    native.state = row["saved_native"]
    declarations(monkeypatch, row)
    services.quiesce_consumer(store, row["target"], native)
    native.fail_resume = True
    with pytest.raises(Exception):
        services.restore_consumer(store, row["target"], native, {})
    with pytest.raises(UpdateError, match="pending"):
        services.restore_consumer(store, row["target"], native, {})
    assert sum(call[0] == "svc_activation_resume" for call in native.calls) == 1
    native.state = row["restore_native"]
    services.restore_consumer(store, row["target"], native, {})


def test_parked_bytes_drift_refuses(tmp_path):
    row = intent(tmp_path)
    store, native = Store(tmp_path, row), Native()
    services.quiesce_consumer(store, row["target"], native)
    services._park_path(store, row).write_bytes(b"foreign")
    with pytest.raises(UpdateError, match="parked"):
        services.quiesce_consumer(store, row["target"], native)


def test_schedules_restore_after_residents(tmp_path, monkeypatch):
    row = intent(tmp_path)
    row["target"] = "example.timer"
    store, native = Store(tmp_path, row), Native()
    store.record["consumers"]["other.service"] = {"intent": {"manager": "Linux", "scheduled": False}}
    with pytest.raises(UpdateError, match="before schedules"):
        services.restore_consumer(store, row["target"], native, {})


def test_publishes_changed_committed_native_definition(tmp_path, monkeypatch):
    row = intent(tmp_path)
    store, native = Store(tmp_path, row), Native()
    declarations(monkeypatch, row)
    services.quiesce_consumer(store, row["target"], native)
    new_content = b"[Service]\nExecStart=/bin/false\n"
    Path(row["generated"]["path"]).write_bytes(new_content)
    services.restore_consumer(store, row["target"], native, {})
    assert Path(services._file(row)).read_bytes() == new_content
    assert services._park_path(store, row).read_bytes() == services._content(row["installed"][0])


def test_literal_relative_link_survives_parking(tmp_path, monkeypatch):
    row = intent(tmp_path)
    installed = Path(services._file(row))
    installed.unlink()
    installed.symlink_to(Path(row["generated"]["path"]).name)
    row["installed"][0]["link"] = os.readlink(installed)
    row["installed"][0]["resolved"] = str(installed.resolve())
    row["installed"][0]["mode"] = installed.lstat().st_mode & 0o777
    store, native = Store(tmp_path, row), Native()
    declarations(monkeypatch, row)
    services.quiesce_consumer(store, row["target"], native)
    services.restore_consumer(store, row["target"], native, {})
    assert installed.is_symlink()
    assert os.readlink(installed) == Path(row["generated"]["path"]).name


def test_stopped_launchd_publishes_but_defers_native_load(tmp_path, monkeypatch):
    row = intent(tmp_path)
    row.update(manager="Darwin", defer_native_load=True,
               saved_native="unchanged loaded inactive", restore_native="unchanged loaded inactive")
    store, native = Store(tmp_path, row), Native()
    declarations(monkeypatch, row)
    monkeypatch.setattr(services, "_darwin", lambda *_: None)
    native.state = "unchanged unloaded inactive"
    import plistlib
    candidate = plistlib.dumps({"RunAtLoad": True, "ProgramArguments": ["/bin/true"]})
    Path(row["generated"]["path"]).write_bytes(candidate)
    result = services.restore_consumer(store, row["target"], native, {})
    assert Path(services._file(row)).read_bytes() == candidate
    assert result["outcome"] == "stopped, deferred native load"
    assert not any(call[0] in {"svc_activation_resume", "svc_activation_start"} for call in native.calls)


def test_candidate_consumer_placement_change_refuses_before_effects():
    old = {"installation": "/installation", "data_root": "/data", "units": [
        {"platform": "Linux", "source": "/data/a.service", "working_directory": "/data",
         "scope": "host", "fleet": None, "bot": None, "service": None}]}
    services.validate_candidate_consumers(old, deepcopy(old), "Linux")
    changed = deepcopy(old)
    changed["units"][0]["working_directory"] = "/foreign"
    with pytest.raises(UpdateError, match="relocates"):
        services.validate_candidate_consumers(old, changed, "Linux")


class AddedNative(Native):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.state = "not-found not-found inactive"
        self.fail_start = False

    def read(self, function, *args):
        assert function == "svc_inventory_catalog"
        return f"manager\tLinux\ndirectory\t{self.directory}\n"

    def call(self, function, *args, timeout=30):
        if function == "svc_inventory_catalog":
            return SimpleNamespace(returncode=0, stdout=self.read(function), stderr="")
        if function == "svc_activation_start":
            self.calls.append((function, args))
            if self.fail_start:
                raise TimeoutError()
            self.state = "enabled loaded active"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if function == "svc_activation_reload":
            self.state = "disabled loaded inactive"
        return super().call(function, *args, timeout=timeout)


def added_intent(tmp_path, monkeypatch, *, timer_owned=False):
    source = tmp_path / "generated" / "example.service"
    source.parent.mkdir()
    source.write_bytes(b"[Service]\nExecStart=/bin/true\n")
    directory = tmp_path / "native"
    directory.mkdir()
    row = {"kind": "added", "target": source.name, "manager": "Linux", "domain": "", "installed": [],
           "phase": "required", "scheduled": timer_owned, "bot_binding": None,
           "disposition": "resident", "destination": str(directory / source.name),
           "search_directories": [str(directory)],
           "declaration": {"source": str(source), "working_directory": str(tmp_path),
                           "environment": [], "scope": "host"},
           "candidate": {"sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "mode": source.stat().st_mode & 0o777,
                         "content": {"base64": base64.b64encode(source.read_bytes()).decode()}},
           "enabled": True, "enroll": True, "run_at_load": False}
    monkeypatch.setattr(services, "current_git_declarations", lambda *_: [SimpleNamespace(
        source=source, working_directory=tmp_path, environment=())])
    monkeypatch.setattr(services, "collect_enrollment", lambda *a, **kw: SimpleNamespace(require_complete=lambda: None))
    return row, AddedNative(directory)


def test_added_timer_owned_service_is_published_without_off_schedule_start(tmp_path, monkeypatch):
    row, native = added_intent(tmp_path, monkeypatch, timer_owned=True)
    store = Store(tmp_path, row)
    services.quiesce_consumer(store, row["target"], native)
    services.observe_quiet(store, row["target"], native)
    services.restore_consumer(store, row["target"], native, {"data_root": str(tmp_path)})
    assert Path(row["destination"]).read_bytes() == Path(row["declaration"]["source"]).read_bytes()
    assert store.record["consumers"][row["target"]]["restore"] == "done"
    assert not any(name in {"svc_activation_start", "svc_activation_resume", "svc_activation_pause"}
                   for name, _ in native.calls)


def test_added_unknown_start_observes_without_repeating_or_recreating(tmp_path, monkeypatch):
    row, native = added_intent(tmp_path, monkeypatch)
    store = Store(tmp_path, row)
    native.fail_start = True
    with pytest.raises(Exception):
        services.restore_consumer(store, row["target"], native, {"data_root": str(tmp_path)})
    with pytest.raises(UpdateError, match="pending"):
        services.restore_consumer(store, row["target"], native, {"data_root": str(tmp_path)})
    assert sum(name == "svc_activation_start" for name, _ in native.calls) == 1
    Path(row["destination"]).unlink()
    with pytest.raises(UpdateError, match="disappeared"):
        services.restore_consumer(store, row["target"], native, {"data_root": str(tmp_path)})
    assert not Path(row["destination"]).exists()


def test_added_collision_refuses_without_replacing_foreign_file(tmp_path, monkeypatch):
    row, native = added_intent(tmp_path, monkeypatch)
    installed = Path(row["destination"])
    installed.write_bytes(b"foreign")
    with pytest.raises(UpdateError, match="already exists"):
        services.quiesce_consumer(Store(tmp_path, row), row["target"], native)
    assert installed.read_bytes() == b"foreign"
    assert native.calls == []


def test_candidate_initial_and_added_bindings_allow_but_removal_refuses():
    item = {"platform": "Linux", "source": "/generated/example.service",
            "working_directory": "/root", "scope": "host"}
    candidate = {"installation": "/install", "data_root": "/root", "units": [item]}
    services.validate_candidate_consumers(None, candidate, "Linux")
    services.validate_candidate_consumers({**candidate, "units": []}, candidate, "Linux")
    with pytest.raises(UpdateError, match="removal is unsupported"):
        services.validate_candidate_consumers(candidate, {**candidate, "units": []}, "Linux")


def test_new_darwin_schedule_loads_without_kickstart(tmp_path, monkeypatch):
    import plistlib
    row, native = added_intent(tmp_path, monkeypatch)
    source = Path(row["declaration"]["source"])
    content = plistlib.dumps({"Label": "example", "ProgramArguments": ["/bin/true"],
                             "StartInterval": 60})
    source.write_bytes(content)
    row.update(manager="Darwin", target="user/123/example", scheduled=True,
               candidate={"sha256": hashlib.sha256(content).hexdigest(),
                          "mode": source.stat().st_mode & 0o777,
                          "content": {"base64": base64.b64encode(content).decode()}})
    native.state = "unchanged unloaded inactive"
    monkeypatch.setattr(services, "observe_added_absence", lambda *_: {"absent": True})
    store = Store(tmp_path, row)
    services.restore_consumer(store, row["target"], native, {"data_root": str(tmp_path)})
    assert native.state == "unchanged loaded inactive"
    assert sum(name == "svc_activation_resume" for name, _ in native.calls) == 1
    assert not any(name == "svc_activation_start" for name, _ in native.calls)


def test_freeze_new_consumer_binds_absence_and_persistent_enablement(tmp_path):
    from claudlobby.supervision_inventory import UnitDeclaration
    directory = tmp_path / "native"
    directory.mkdir()
    source = tmp_path / "generated.service"
    content = b"[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n"
    item = {"sha256": hashlib.sha256(content).hexdigest(), "mode": 0o644,
            "phase": "required", "scope": "host", "working_directory": str(tmp_path),
            "fleet": None, "bot": None, "service": None, "enroll": True}
    snapshot = {"supervision": {"manager": "Linux", "domain": "", "directory": str(directory)}}
    declaration = UnitDeclaration(source, "host", tmp_path, "", (), None, None, None)
    native = AddedNative(directory)
    row = services._freeze_added(snapshot, native, directory, declaration, item, content, set())
    assert row["kind"] == "added" and row["absence"]["absent"]
    assert "saved_native" not in row and row["installed"] == []
    assert row["enablement"]["path"] == str(directory / "default.target.wants" / source.name)
    assert not Path(row["destination"]).exists()


@pytest.mark.parametrize("drift", ["domain", "directory", "template"])
def test_independent_absence_rejects_changed_native_witnesses(tmp_path, drift):
    from claudlobby.supervision_runtime import assert_native_absent, RuntimeEvidenceError
    directory = tmp_path / "native"
    directory.mkdir()
    target = "example@.service" if drift == "template" else "example.service"
    lines = f"manager\tLinux\ndirectory\t{directory}\n"
    if drift == "directory":
        lines += f"directory\t{tmp_path / 'other'}\n"
    if drift == "template":
        lines += "loaded\texample@worker.service\n"
    native = SimpleNamespace(call=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=lines, stderr=""))
    with pytest.raises(RuntimeEvidenceError):
        assert_native_absent(native, manager="Linux", domain="user/123" if drift == "domain" else "",
                             target=target, installed_file=directory / target, search_directories=[directory])
