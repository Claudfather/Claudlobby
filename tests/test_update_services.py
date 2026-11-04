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
