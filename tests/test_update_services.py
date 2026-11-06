"""Focused filesystem recovery and no-repeat native effect contracts."""
import base64
from copy import deepcopy
import hashlib
import json
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


def _drifted_row(tmp_path, saved, **fields):
    row = intent(tmp_path)
    row.update(saved_native=saved, restore_native=saved, **fields)
    return row


@pytest.mark.parametrize("key,observed", [
    ("example.service", "enabled loaded inactive"), ("example.service", "enabled loaded activating"),
    ("example.timer", "enabled loaded inactive"), ("example.timer", "enabled loaded activating"),
    ("example.timer", "enabled loaded failed")])
def test_a_unit_that_only_gone_down_passes_the_stop_and_restores_as_frozen(tmp_path, monkeypatch, key, observed):
    row = _drifted_row(tmp_path, "enabled loaded active", target=key)
    store, native = Store(tmp_path, row), Native()
    declarations(monkeypatch, row)
    native.state = observed
    services.quiesce_consumer(store, key, native)
    pauses = [call for call in native.calls if call[0] == "svc_activation_pause"]
    assert len(pauses) == 1 and pauses[0][1][2] == "enabled loaded active"
    assert store.record["consumers"][key]["stop"] == "done"
    if key.endswith(".timer"):
        return  # a timer restored to running needs the held-start fixture of test_update_timer_catch_up.py
    services.restore_consumer(store, key, native, {})
    resumes = [call for call in native.calls if call[0] == "svc_activation_resume"]
    assert len(resumes) == 1 and resumes[0][1][2] == "enabled loaded active"
    assert native.state == "enabled loaded active"


def test_a_bot_that_has_gone_down_still_refuses_and_names_no_command(tmp_path):
    row = _drifted_row(tmp_path, "enabled loaded active", bot_binding=[str(tmp_path), "example", str(tmp_path)],
                       disposition="running")
    store, native = Store(tmp_path, row), Native()
    native.state, native.session = "enabled loaded inactive", "ready"
    with pytest.raises(UpdateError) as caught:
        services.quiesce_consumer(store, row["target"], native)
    text = str(caught.value)
    assert row["target"] in text and services._NO_BOT_START in text
    assert "host update continue" not in text and "systemctl" not in text
    assert not services._park_path(store, row).exists()
    assert not any(call[0] == "svc_activation_pause" for call in native.calls)


@pytest.mark.parametrize("saved,now,step", [
    ("enabled loaded active", "disabled loaded inactive", "Run systemctl --user enable {key}"),
    ("disabled loaded inactive", "enabled loaded inactive", "Run systemctl --user disable {key}"),
    ("enabled loaded active", "masked masked inactive", "Run systemctl --user unmask {key}"),
    ("enabled loaded active", "masked-runtime masked inactive", "Run systemctl --user unmask --runtime {key}"),
    ("enabled loaded active", "enabled loaded failed", "Run systemctl --user reset-failed {key}"),
    ("enabled-runtime loaded active", "disabled loaded active", 'Put {key} back to "enabled-runtime loaded active"'),
    ("enabled loaded active", "not-found not-found inactive", 'Put {key} back to "enabled loaded active"')],
    ids=["enable", "disable", "unmask", "unmask-runtime", "failed-service-does-not-pass", "no-command-runtime-enable",
         "no-command-not-found"])
def test_a_service_drift_names_the_unit_the_states_and_what_clears_it(tmp_path, saved, now, step):
    row = _drifted_row(tmp_path, saved)
    store, native = Store(tmp_path, row), Native()
    native.state = now
    key = row["target"]
    with pytest.raises(UpdateError) as caught:
        services.quiesce_consumer(store, key, native)
    assert str(caught.value) == (f'native state drifted before stop: {key} was "{saved}" when this update was '
                                 f'prepared and reads "{now}" now. {step.format(key=key)}, '
                                 'then run claudlobby host update continue')
    assert not any(call[0] == "svc_activation_pause" for call in native.calls)


@pytest.mark.parametrize("now", ["active", "activating"])
def test_a_unit_started_by_hand_is_told_to_stop(tmp_path, now):
    row = _drifted_row(tmp_path, "enabled loaded inactive")
    store, native = Store(tmp_path, row), Native()
    native.state = f"enabled loaded {now}"
    with pytest.raises(UpdateError) as caught:
        services.quiesce_consumer(store, row["target"], native)
    assert f"Run systemctl --user stop {row['target']}, then run claudlobby host update continue" in str(caught.value)


@pytest.mark.parametrize("now", ["unchanged unloaded inactive", "unchanged loaded failed"])
def test_a_launchd_drift_names_no_systemctl(tmp_path, monkeypatch, now):
    key = "gui/501/example"
    row = _drifted_row(tmp_path, "unchanged loaded active", manager="Darwin", target=key)
    store, native = Store(tmp_path, row), Native()
    native.state = now
    monkeypatch.setattr(services, "_darwin", lambda *_: None)
    with pytest.raises(UpdateError) as caught:
        services.quiesce_consumer(store, key, native)
    text = str(caught.value)
    assert f'Put {key} back to "unchanged loaded active", then run claudlobby host update continue' in text
    assert "systemctl" not in text


def test_native_state_matches_lets_only_scheduled_work_finish_between_observations(tmp_path):
    row = _drifted_row(tmp_path, "enabled loaded active")
    assert not services.native_state_matches(row, "enabled loaded inactive")
    assert services.native_state_matches(row, "enabled loaded active")
    assert services.native_state_matches({**row, "scheduled": True}, "enabled loaded inactive")


@pytest.mark.parametrize("frozen,observed,remedy", [
    ("stopped", "ready", "Stop its session, then run claudlobby host update continue"),
    ("running", "absent", services._NO_BOT_START)])
def test_bot_session_drift_names_both_states_and_what_clears_it(tmp_path, frozen, observed, remedy):
    row = intent(tmp_path)
    row.update(bot_binding=[str(tmp_path), "example", str(tmp_path)], disposition=frozen)
    store, native = Store(tmp_path, row), Native()
    native.session = observed
    with pytest.raises(UpdateError) as caught:
        services.quiesce_consumer(store, row["target"], native)
    now = "running" if observed == "ready" else "stopped"
    assert str(caught.value) == (f'bot session disposition drifted before stop: {row["target"]} was "{frozen}" '
                                 f'when this update was prepared and reads "{now}" now. {remedy}')


def test_adopted_clear_after_checkpoint_refuses_before_native_stop(tmp_path, monkeypatch):
    from claudlobby import native_session
    from claudlobby.isolation import transcript_slug
    from tests.test_native_session import Observation, SID, OTHER, provider_record

    bot, account, tmpdir = (tmp_path / part for part in ("bot", "account", "tmux"))
    (bot / "data").mkdir(parents=True)
    observation = Observation(tmp_path)
    provider = provider_record(bot, account, observation, SID)
    transcript = account / "projects" / transcript_slug(bot) / (SID + ".jsonl")
    transcript.parent.mkdir(parents=True); transcript.write_bytes(b"original legacy session\n")
    proof = native_session.checkpoint(bot, "example", tmpdir, observation,
                                      operation_id="original", account=account)
    row = intent(tmp_path)
    row.update(manager="Darwin", target="user/123/example", disposition="running",
               saved_native="unchanged loaded active", restore_native="unchanged loaded active",
               bot_binding=[str(bot), "example", str(tmpdir)])
    store, native = Store(tmp_path, row), Native()
    store.record["consumers"][row["target"]]["checkpoint"] = proof
    native.state, native.session = row["saved_native"], "ready"
    def call(function, *args, **kwargs):
        return (observation if function == "svc_bot_native_session" else native).call(function, *args, **kwargs)
    adapter = SimpleNamespace(package=observation.package, call=call)
    # The existing native ownership boundary is independent of session metadata.
    monkeypatch.setattr(services, "_darwin", lambda *_: None)
    provider_record(bot, account, observation, OTHER)
    original_receipt = (bot / "data" / native_session.NAME).read_bytes()
    with pytest.raises(native_session.NativeSessionError, match="session changed before stop"):
        services.quiesce_consumer(store, row["target"], adapter)
    progress = store.load()["consumers"][row["target"]]
    assert "stop" not in progress and "park" not in progress
    assert progress["checkpoint"] == proof
    assert (bot / "data" / native_session.NAME).read_bytes() == original_receipt
    assert Path(services._file(row)).read_bytes() == services._content(row["installed"][0])
    assert not any(name == "svc_activation_pause" for name, _ in native.calls)
    assert json.loads(provider.read_bytes())["sessionId"] == OTHER


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
    def __init__(self, directory, *others):
        super().__init__()
        self.directory = directory
        self.others = others
        self.state = "not-found not-found inactive"
        self.fail_start = False

    def read(self, function, *args):
        assert function == "svc_inventory_catalog"
        return "".join(["manager\tLinux\n", *(f"directory\t{item}\n" for item in (self.directory, *self.others))])

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


class StartingBotNative(Native):
    """launchd acknowledges bootstrap before the private bot session is ready."""
    def __init__(self, store, target, *, fail_ready=False):
        super().__init__()
        self.store, self.target, self.fail_ready = store, target, fail_ready
        self.state, self.session = "unchanged unloaded inactive", "unknown"
        self.ready_timeouts = []

    def call(self, function, *args, timeout=30):
        if function == "svc_activation_bot_fence":
            self.calls.append((function, args))
            return SimpleNamespace(returncode=0, stdout="210\tRR_FENCE_original", stderr="")
        if function == "svc_activation_resume":
            progress = self.store.load()["consumers"][self.target]
            assert progress["restore"] == "pending"
            assert progress["readiness"] == {"ceiling": 210, "token": "RR_FENCE_original"}
            self.calls.append((function, args))
            self.state = "unchanged loaded inactive"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if function == "svc_activation_bot_ready":
            self.calls.append((function, args))
            self.ready_timeouts.append(timeout)
            assert self.session == "unknown"
            if self.fail_ready:
                return SimpleNamespace(returncode=3, stdout="", stderr="readiness unverified")
            self.session = "ready"
            return SimpleNamespace(returncode=0, stdout="session-ready", stderr="")
        return super().call(function, *args, timeout=timeout)


@pytest.mark.parametrize("added", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_launchd_bot_waits_for_original_readiness_without_replaying_start(tmp_path, monkeypatch, added, interrupted):
    import plistlib
    row = added_intent(tmp_path, monkeypatch)[0] if added else intent(tmp_path)
    content = plistlib.dumps({"RunAtLoad": True, "ProgramArguments": ["/bin/true"]})
    source = Path(row["declaration"]["source"])
    source.write_bytes(content)
    row.update(manager="Darwin", target="user/123/example", scheduled=False,
               bot_binding=[str(tmp_path), "example", str(tmp_path / "tmux")], disposition="running")
    if added:
        row.update(run_at_load=True, candidate={"sha256": hashlib.sha256(content).hexdigest(),
            "mode": source.stat().st_mode & 0o777, "content": {"base64": base64.b64encode(content).decode()}})
        monkeypatch.setattr(services, "observe_added_absence", lambda *_: {"absent": True})
    else:
        row["saved_native"] = row["restore_native"] = "unchanged loaded active"
        declarations(monkeypatch, row)
        monkeypatch.setattr(services, "_darwin", lambda *_: None)
    store = Store(tmp_path, row)
    native = StartingBotNative(store, row["target"], fail_ready=interrupted)
    snapshot = {"data_root": str(tmp_path)}
    if interrupted:
        with pytest.raises(RuntimeError):
            services.restore_consumer(store, row["target"], native, snapshot)
        assert store.load()["consumers"][row["target"]]["restore"] == "pending"
        native.fail_ready = False
    result = services.restore_consumer(store, row["target"], native, snapshot)
    assert result["restore"] == "done"
    assert sum(name == "svc_activation_resume" for name, _ in native.calls) == 1
    assert sum(name == "svc_activation_bot_fence" for name, _ in native.calls) == 1
    assert not any(name == "svc_activation_start" for name, _ in native.calls)
    ready = [args for name, args in native.calls if name == "svc_activation_bot_ready"]
    assert ready == [(str(tmp_path), str(tmp_path), "210", "RR_FENCE_original", str(tmp_path / "tmux"))] * (2 if interrupted else 1)
    assert native.ready_timeouts == [240] * len(ready)


@pytest.mark.parametrize("linked", [False, True])
def test_freeze_new_consumer_binds_absence_and_persistent_enablement(tmp_path, linked):
    from claudlobby.supervision_inventory import UnitDeclaration
    directory, shared, link = _linked_search_list(tmp_path)
    others = (link, shared) if linked else ()
    source = tmp_path / "generated.service"
    content = b"[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n"
    item = {"sha256": hashlib.sha256(content).hexdigest(), "mode": 0o644,
            "phase": "required", "scope": "host", "working_directory": str(tmp_path),
            "fleet": None, "bot": None, "service": None, "enroll": True}
    snapshot = {"supervision": {"manager": "Linux", "domain": "", "directory": str(directory)}}
    declaration = UnitDeclaration(source, "host", tmp_path, "", (), None, None, None)
    native = AddedNative(directory, *others)
    row = services._freeze_added(snapshot, native, directory, declaration, item, content, set())
    assert row["kind"] == "added" and row["absence"]["absent"]
    assert row["search_directories"] == [str(path) for path in (directory, *others)]
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


def _linked_search_list(tmp_path):
    """A plain install directory, a directory outside it, and a link to that directory."""
    directory, shared, link = tmp_path / "native", tmp_path / "shared", tmp_path / "linked"
    directory.mkdir()
    shared.mkdir()
    link.symlink_to(shared)
    return directory, shared, link


def _absence_witnesses(directories, state, *, manager="Linux", domain=""):
    """What the manager answers to one absence check: its catalog, then the unit's state."""
    catalog = "".join([f"manager\t{manager}\n", f"domain\t{domain}\n" if domain else "",
                       *(f"directory\t{item}\n" for item in directories)])

    def call(function, *_args, **_kwargs):
        stdout = catalog if function == "svc_inventory_catalog" else state
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    return SimpleNamespace(call=call)


@pytest.mark.parametrize("manager,domain,target,name,state", [
    ("Linux", "", "example.service", "example.service", "not-found not-found inactive"),
    ("Darwin", "gui/501", "gui/501/example", "example.plist", "unchanged unloaded inactive"),
])
def test_independent_absence_accepts_a_link_on_the_search_list(tmp_path, manager, domain, target, name, state):
    from claudlobby.supervision_runtime import assert_native_absent
    directory, shared, link = _linked_search_list(tmp_path)
    directories = [directory, link, shared]
    evidence = assert_native_absent(_absence_witnesses(directories, state, manager=manager, domain=domain),
                                    manager=manager, domain=domain, target=target,
                                    installed_file=directory / name, search_directories=directories)
    assert evidence.details["absent"] is True


@pytest.mark.parametrize("unit,state,refusal", [
    ("file", "not-found not-found inactive", "already exists"),
    ("dangling link", "not-found not-found inactive", "already exists"),
    ("only the manager sees it", "disabled loaded inactive", "absence is unproved"),
])
def test_independent_absence_sees_a_unit_behind_a_linked_directory(tmp_path, unit, state, refusal):
    from claudlobby.supervision_runtime import assert_native_absent, RuntimeEvidenceError
    directory, shared, link = _linked_search_list(tmp_path)
    if unit == "file":
        (shared / "example.service").write_text("[Service]\nExecStart=/bin/true\n")
    elif unit == "dangling link":
        (shared / "example.service").symlink_to(tmp_path / "gone")
    directories = [directory, link]
    with pytest.raises(RuntimeEvidenceError, match=refusal):
        assert_native_absent(_absence_witnesses(directories, state), manager="Linux", domain="",
                             target="example.service", installed_file=directory / "example.service",
                             search_directories=directories)


@pytest.mark.parametrize("placement", ["link", "under a link", "relative entry"])
def test_independent_absence_refuses_a_bad_placement(tmp_path, placement):
    from claudlobby.supervision_runtime import assert_native_absent, RuntimeEvidenceError
    directory, shared, link = _linked_search_list(tmp_path)
    (shared / "user").mkdir()
    directories = {"link": [link], "under a link": [link / "user"],
                   "relative entry": [directory, Path("relative")]}[placement]
    native = _absence_witnesses(directories, "not-found not-found inactive")
    with pytest.raises(RuntimeEvidenceError, match="invalid frozen native placement"):
        assert_native_absent(native, manager="Linux", domain="", target="example.service",
                             installed_file=directories[0] / "example.service", search_directories=directories)


# Shared native contracts survive removal of the activation coordinator.
import signal
import socket
import subprocess
from claudlobby import supervision_runtime as runtime
from claudlobby.supervision_inventory import Adapter as NativeAdapter

NATIVE = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts"

def test_high_linux_boot_rung_extends_only_its_exact_start_budget():
    assert runtime.unit_start_budget(Path("worker.service"),
            b"[Service]\nExecStartPre=/bin/sleep 33\n") == 63
    assert runtime.unit_start_budget(Path("worker.service"), b"[Service]\n") == 30
    with pytest.raises(runtime.RuntimeEvidenceError, match="unsupported published boot delay"):
        runtime.unit_start_budget(Path("worker.service"), b"ExecStartPre=/bin/sh -c 'sleep 33'\n")



def test_a_unit_start_budget_counts_only_a_linux_units_boot_delay():
    """bot start and restart size their enroll call with it too (#2087)."""
    assert runtime.unit_start_budget(Path("worker.service"),
            b"[Service]\nExecStartPre=/bin/sleep 30\n") == 60
    assert runtime.unit_start_budget(Path("worker.plist"), b"ExecStartPre=/bin/sleep 30\n") == 30



def _runtime_bash(script, *args, env=None):
    process = subprocess.Popen(["/bin/bash", "-c", script, "activation-fixture", *map(str, args)],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=env)
    try:
        stdout, stderr = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=3)
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)



@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
def test_native_exact_start_refuses_active_or_foreign_before_effect(tmp_path, manager):
    file = tmp_path / ("worker.service" if manager == "Linux" else "worker.plist")
    file.write_text("candidate bytes\n")
    target = "worker.service" if manager == "Linux" else "gui/501/worker"
    script = r'''
. "$1/supervisor.sh"
_OS="$2"; file="$3"; target="$4"; mode="$5"; trace="$6"; native_state="$7"
printf '%s' initial > "$native_state"
systemctl() {
    printf '%s\n' "$*" >> "$trace"
    case "$2" in
        show)
            phase=$(cat "$native_state"); load=loaded; active=inactive; enabled=disabled; fragment="$file"
            if [ "$mode" = refuse ]; then fragment=/foreign.service
            elif [ "$phase" = initial ]; then load=masked; enabled=masked-runtime; fragment=/dev/null
            elif [ "$phase" = started ]; then active=active; fi
            printf 'Id=%s\nLoadState=%s\nActiveState=%s\nSubState=dead\nUnitFileState=%s\nFragmentPath=%s\nControlGroup=\nMainPID=0\nControlPID=0\n' \
                "$target" "$load" "$active" "$enabled" "$fragment" ;;
        unmask) printf '%s' unmasked > "$native_state" ;;
        start) printf '%s' started > "$native_state" ;;
    esac
}
launchctl() {
    printf '%s\n' "$*" >> "$trace"
    case "$1" in
        manageruid) echo 501 ;; managername) echo Aqua ;;
        list)
            printf 'PID Status Label\n'
            if [ "$mode" = refuse ] || [ "$(cat "$native_state")" = started ]; then
                printf '123 0 worker\n'
            fi ;;
        bootstrap) printf '%s' started > "$native_state" ;;
    esac
}
svc_activation_start "$file" "$target"
'''
    trace, native_state = tmp_path / "calls", tmp_path / "state"
    refused = _runtime_bash(script, NATIVE, manager, file, target, "refuse", trace, native_state)
    assert refused.returncode == 3
    assert "start " not in trace.read_text() and "bootstrap " not in trace.read_text()
    trace.write_text("")
    started = _runtime_bash(script, NATIVE, manager, file, target, "allow", trace, native_state)
    assert started.returncode == 0, started.stderr
    assert started.stdout.strip() == "start-requested"
    calls = trace.read_text()
    if manager == "Linux":
        assert "--user unmask --runtime worker.service" in calls
        assert calls.index("unmask") < calls.index("--user start worker.service")
    else:
        assert f"bootstrap gui/501 {file}" in calls
    assert "enable" not in calls and "restart" not in calls and "install" not in calls
    assert file.read_text() == "candidate bytes\n"



def _native_confirm_cli(bot, tmpdir, *, observations=None):
    """Isolate bridge/tmux readiness while executing the real receipt verifier."""
    import sys
    from claudlobby import native_session
    from tests.test_native_session import Observation, retain_native_session
    (bot / "data").mkdir(exist_ok=True)
    adapter = Observation(bot.parent)
    retain_native_session(bot, "fixture-worker", Path(tmpdir), bot / "account", adapter)
    cli = bot / "fixture-cli"
    cli.write_text("#!" + sys.executable + "\n"
        "import sys\nfrom pathlib import Path\nfrom claudlobby.native_session import confirm, NativeSessionPending\n"
        "assert sys.argv[1] == '--root' and sys.argv[3:5] == ['_native-session','confirm']\n"
        "trace = " + repr(str(observations) if observations else None) + "\n"
        "def record(value):\n"
        "    if trace:\n"
        "        with Path(trace).open('a') as stream: stream.write(value + '\\n')\n"
        "record('attempt')\n"
        "try:\n    value = confirm(Path(sys.argv[5]))\n"
        "except NativeSessionPending:\n    record('pending')\n    raise SystemExit(75)\n"
        "assert value['label'] == sys.argv[6] and value['tmpdir'] == sys.argv[7]\n"
        "record('confirmed')\n")
    cli.chmod(0o700)
    with (bot / "bot.conf").open("a") as stream:
        stream.write("CLAUDLOBBY_CLI=" + str(cli) + "\n")
    return bot / "data" / native_session.NAME


def test_real_restart_policy_fence_is_verified_and_ready_uses_same_marker(tmp_path):
    bot = tmp_path / "bots/worker"
    bot_tmpdir = tmp_path / "private-tmux"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('RC_READY_TIMEOUT_S="001"\nBOT_SERVICE="fixture-worker"\n'
                                  f'TELEGRAM_BOT_HANDLE="fixture_handle"\nTMUX_TMPDIR="{bot_tmpdir}"\n')
    _native_confirm_cli(bot, bot_tmpdir)
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot)
    assert fence.returncode == 0, fence.stderr
    ceiling, token = fence.stdout.strip().split("\t")
    assert ceiling == "121"  # actual rr_bot_ceiling, including zero-padded config
    override = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "37")
    assert override.returncode == 0 and override.stdout.split("\t", 1)[0] == "37"
    assert _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "0").returncode != 0
    log = bot / "logs/startup.log"
    assert token in log.read_text()
    with log.open("a") as out:
        out.write("BRIDGE_READY fixture\n")
    ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir)
    assert ready.returncode == 0 and ready.stdout.strip() == "bridge-ready"
    missing = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", "absent-marker", bot_tmpdir)
    assert missing.returncode != 0
    log.unlink()
    log.mkdir()  # bridge_fence_write is best-effort; the new boundary must refuse
    failed = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot)
    assert failed.returncode != 0



@pytest.mark.parametrize(("extra_conf", "accepted", "rejected", "readiness"), [
    ("", "READY — non-channel bot, no poller to await",
     "BRIDGE_READY — Telegram poller up", "session-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\nEXPECT_NO_TOKEN=1\n',
     "BRIDGE_SKIP — no token by design (EXPECT_NO_TOKEN); canary/throwaway, no alert",
     "READY — non-channel bot, no poller to await", "session-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\nEXPECT_NO_TOKEN=1\n',
     "BRIDGE_READY — Telegram poller up",
     "READY — non-channel bot, no poller to await", "bridge-ready"),
    ('TELEGRAM_BOT_HANDLE="fixture_handle"\n',
     "BRIDGE_READY — Telegram poller up",
     "BRIDGE_SKIP — no token by design (EXPECT_NO_TOKEN); canary/throwaway, no alert",
     "bridge-ready"),
])
def test_native_bot_readiness_accepts_only_configured_post_fence_outcome(
    tmp_path, extra_conf, accepted, rejected, readiness
):
    bot = tmp_path / "bots/worker"
    bot_tmpdir = tmp_path / "private-tmux"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text(f'BOT_SERVICE="fixture-worker"\nTMUX_TMPDIR="{bot_tmpdir}"\n' + extra_conf)
    log = bot / "logs/startup.log"
    log.parent.mkdir()
    log.write_text(accepted + "\n")  # an old boot cannot satisfy the new fence
    _native_confirm_cli(bot, bot_tmpdir)
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "1")
    assert fence.returncode == 0, fence.stderr
    _, token = fence.stdout.strip().split("\t")
    assert _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir).returncode != 0
    with log.open("a") as out:
        out.write(rejected + "\n")
    assert _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir).returncode != 0
    with log.open("a") as out:
        out.write(accepted + "\n")
    ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir)
    assert ready.returncode == 0 and ready.stdout.strip() == readiness
    no_session = _runtime_bash(prefix.replace(
        "fixture_tmux() { [ \"$*\" = '-L fixture-worker has-session -t worker' ]; }",
        "fixture_tmux() { return 1; }",
    ), NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir)
    assert no_session.returncode != 0



def test_native_bot_readiness_uses_frozen_tmpdir_instead_of_operator_socket(tmp_path):
    import shutil
    import tempfile
    from tests.conftest import constructed_env

    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is required for the real private-session contract")
    # Keep the Unix socket short even when pytest's private checkout path is long.
    with tempfile.TemporaryDirectory(prefix="cf-ready-", dir="/tmp") as scratch:
        bot_tmpdir, operator_tmpdir = Path(scratch) / "bot", Path(scratch) / "operator"
        bot_tmpdir.mkdir(mode=0o700)
        operator_tmpdir.mkdir(mode=0o700)
        bot = tmp_path / "bots/worker"
        bot.mkdir(parents=True)
        (bot / "bot.conf").write_text(f'BOT_SERVICE="fixture-worker"\nTMUX_TMPDIR="{bot_tmpdir}"\n')
        _native_confirm_cli(bot, bot_tmpdir)
        env = constructed_env(HOME=tmp_path, TMUX_BIN=tmux, TMUX_TMPDIR=operator_tmpdir)
        bot_env = {**env, "TMUX_TMPDIR": str(bot_tmpdir)}
        prefix = '. "$1/supervisor.sh"; shift; "$@"'
        try:
            subprocess.run([tmux, "-f", "/dev/null", "-L", "fixture-worker", "new-session", "-d", "-s", "worker"],
                           env=bot_env, check=True, capture_output=True, timeout=5)
            fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "1", env=env)
            assert fence.returncode == 0, fence.stderr
            _, token = fence.stdout.strip().split("\t")
            with (bot / "logs/startup.log").open("a") as out:
                out.write("READY — non-channel bot, no poller to await\n")
            ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, bot_tmpdir, env=env)
            assert ready.returncode == 0, ready.stderr
            assert ready.stdout.strip() == "session-ready"
            wrong = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token, operator_tmpdir, env=env)
            assert wrong.returncode != 0
            assert not (operator_tmpdir / f"tmux-{os.getuid()}" / "fixture-worker").exists()
        finally:
            subprocess.run([tmux, "-L", "fixture-worker", "kill-server"], env=bot_env,
                           capture_output=True, timeout=5)


def test_adapter_timeout_reaps_its_poll_group_without_delayed_effect(tmp_path, monkeypatch, request):
    """#2493: the outcome never rests on a clock. The timeout starts only once the
    grandchild runs, its late write waits for a gate that opens only at the drain
    after the kill, and that drain returns only after every process holding the
    output pipes has exited, so a stalled parent or child cannot reorder them.
    #2507: a failing run stops at its own line and leaves no poller behind."""
    late, gate = tmp_path / "late", tmp_path / "gate"
    request.addfinalizer(gate.touch)  # a survivor of a failed run writes late and exits

    class Gated(subprocess.Popen):
        started = False

        def communicate(self, input=None, timeout=None):
            if timeout is None:
                gate.touch()  # the drain: a survivor of the kill writes late before it exits
            elif not self.started:  # once: a bounded drain must not wait on a survivor's pipe
                assert self.stdout.readline() == "ready\n"  # the grandchild is in the group
                self.started = True
            return super().communicate(input, timeout)

    monkeypatch.setattr(subprocess, "Popen", Gated)
    with pytest.raises(subprocess.TimeoutExpired):
        NativeAdapter._run(["/bin/bash", "-c", '( echo ready; until [ -e "$2" ]; do /bin/sleep 0.01; done; '
                            'echo late > "$1" ) & wait', "private-timeout", str(late), str(gate)],
                           timeout=0.05, env=dict(os.environ), capture_output=True, text=True)
    assert not late.exists()
    assert gate.exists()  # the drain ran, else no survivor could have written late



def test_quiet_settle_is_the_native_wait_and_widens_the_call_budget(tmp_path):
    """#2227: settle_s reaches svc_activation_quiet as its wait, inside a call
    budget that covers it; without it the native argv and budget are unchanged."""
    calls = []

    class Adapter:
        def call(self, function, *args, timeout=30):
            calls.append((function, args, timeout))
            return subprocess.CompletedProcess(args, 0, "inactive\tcgroup-empty\n", "")

    installed_file = tmp_path / "worker.service"
    args = dict(installed_file=installed_file, target="worker.service")
    runtime.assert_quiescent(Adapter(), **args)
    runtime.assert_quiescent(Adapter(), **args, settle_s=30)
    assert calls == [("svc_activation_quiet", (installed_file, "worker.service", ""), 30),
                     ("svc_activation_quiet", (installed_file, "worker.service", "", "30"), 60)]
    for bad in (-1, True, 2.5, "30"):
        with pytest.raises(runtime.RuntimeEvidenceError, match="settle"):
            runtime.assert_quiescent(Adapter(), **args, settle_s=bad)
    assert len(calls) == 2  # a refused settle reaches no native call



def test_quiet_known_socket_and_pid_are_required_not_inferred_from_inactive(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("qr")
    class Adapter:
        def call(self, *args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "inactive\tno-cgroup-witness\n", "")

    path = tmp_path / "tmux.sock"
    args = dict(installed_file=tmp_path / "worker.service", target="worker.service", socket_path=path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        with pytest.raises(runtime.RuntimeEvidenceError, match="still accepts"):
            runtime.assert_quiescent(Adapter(), **args)
    with pytest.raises(runtime.RuntimeEvidenceError, match="known process"):
        runtime.assert_quiescent(Adapter(), **args, known_pids=(os.getpid(),))
    observed = runtime.assert_quiescent(Adapter(), **args)
    assert observed.details["socket_state"] == "refused"
    assert observed.details["coverage"] == "named-unit-and-supplied-witnesses-only"
    assert path.exists()  # observation never unlinks a socket


@pytest.mark.parametrize("enroll", [False, True])
def test_conversion_preserves_dormant_rows_and_refuses_enrollment_toggle(enroll):
    row = {"platform": "Linux", "source": "/generated/example.timer", "enroll": enroll,
           "working_directory": "/root", "scope": "fleet", "fleet": "example", "bot": None}
    baseline = SimpleNamespace(manager="Linux", units=(row,))
    candidate = {"units": [deepcopy(row)]}
    services.validate_candidate_consumers(None, candidate, "Linux", baseline=baseline)
    candidate["units"][0]["enroll"] = not enroll
    with pytest.raises(UpdateError, match="preserve native enrollment policy"):
        services.validate_candidate_consumers(None, candidate, "Linux", baseline=baseline)


@pytest.mark.parametrize("defect", ["domain", "target"])
def test_conversion_refuses_cross_domain_or_retargeted_inventory_before_freezing(tmp_path, monkeypatch, defect):
    baseline = SimpleNamespace(root=tmp_path, manager="Darwin", domain="gui/123", install_directory=tmp_path,
                               declarations=(), release=object(),
                               placements=({"source": "/generated/example.plist", "target": "gui/123/example"},))
    monkeypatch.setattr(services.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(services, "_catalog", lambda _: ("Darwin", "user/123" if defect == "domain" else "gui/123", [], [], []))
    adapter = SimpleNamespace(read=lambda _: "catalog")
    calls = []
    def collect(*args, **kwargs):
        calls.append("inventory")
        return SimpleNamespace(require_complete=lambda: SimpleNamespace(payload=lambda: {"units": [
            {"declaration": {"source": "/generated/example.plist"}, "target": "user/123/example"}]}))
    monkeypatch.setattr(services, "collect_enrollment", collect)
    with pytest.raises(UpdateError, match="original native manager domain|historical native targets"):
        services.freeze_service_intent(None, adapter, tmp_path, baseline=baseline)
    assert calls == ([] if defect == "domain" else ["inventory"])


@pytest.mark.parametrize("interrupted", [False, True])
def test_darwin_bot_pause_binds_private_server_and_pending_retry_never_resends(tmp_path, monkeypatch, interrupted):
    row = intent(tmp_path)
    row.update(manager="Darwin", bot_binding=[str(tmp_path), "example", str(tmp_path)],
               disposition="running", saved_native="unchanged loaded inactive",
               restore_native="unchanged loaded inactive")
    store, native = Store(tmp_path, row), Native()
    native.state, native.session = "unchanged loaded inactive", "ready"
    monkeypatch.setattr(services, "_darwin", lambda *_: None)
    original = native.call
    def call(function, *args, **kwargs):
        if function == "svc_activation_pause":
            native.calls.append((function, args))
            assert store.load()["consumers"][row["target"]]["stop"] == "pending"
            assert args[4:] == ("bot-owned", *row["bot_binding"])
            native.state = "unchanged unloaded inactive"
            if interrupted:
                raise TimeoutError("private stop outcome unknown")
            native.session = "absent"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return original(function, *args, **kwargs)
    native.call = call
    def quiet(*_):
        if native.session != "absent":
            raise UpdateError("private socket still accepts")
    monkeypatch.setattr(services, "observe_quiet", quiet)
    if interrupted:
        from claudlobby.supervision_runtime import RuntimeEvidenceError
        with pytest.raises(RuntimeEvidenceError, match="native observation failed or timed out"):
            services.quiesce_consumer(store, row["target"], native)
        with pytest.raises(UpdateError, match="private socket"):
            services.quiesce_consumer(store, row["target"], native)
        native.session = "absent"  # Original effect subsequently observed complete.
    services.quiesce_consumer(store, row["target"], native)
    assert sum(name == "svc_activation_pause" for name, _ in native.calls) == 1
    assert store.load()["consumers"][row["target"]]["stop"] == "done"


@pytest.mark.parametrize("defect", [None, "label", "file", "private-stop-failure"])
def test_darwin_pause_shell_unloads_before_exact_private_stop(tmp_path, defect):
    import subprocess
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    trace = tmp_path / "trace"
    body = r'''
set -euo pipefail
. "$1"
_OS=Darwin
loaded=loaded
_svc_activation_saved() { :; }
svc_activation_assert_external() {
    printf 'external\n' >> "$TRACE"
    SVC_ACT_LOAD="$loaded"
    SVC_ACT_ACTIVE=inactive
    SVC_ACT_GROUP=""
}
_svc_activation_read() { SVC_ACT_LOAD="$loaded"; SVC_ACT_ACTIVE=inactive; }
launchctl() {
    [ "$1" = bootout ] && [ "$2" = gui/123/example ] || return 99
    printf 'bootout\n' >> "$TRACE"
    loaded=unloaded
}
svc_activation_stop_private_server() {
    [ "$loaded" = unloaded ] || return 98
    [ "$1" = /owned/bot ] && [ "$2" = example ] && [ "$3" = /owned/tmp ] && [ "$4" = retired ] || return 97
    printf 'private-stop\n' >> "$TRACE"
    [ "$DEFECT" != private-stop-failure ] || return 3
}
file=/owned/example.plist
label=example
[ "$DEFECT" != file ] || file=/owned/foreign.plist
[ "$DEFECT" != label ] || label=foreign
svc_activation_pause "$file" gui/123/example 'unchanged loaded inactive' 123 bot-owned /owned/bot "$label" /owned/tmp
'''
    outcome = subprocess.run(["/bin/bash", "-c", body, "pause-contract", str(script)],
                             env={"PATH": "/usr/bin:/bin", "TRACE": str(trace), "DEFECT": defect or ""},
                             capture_output=True, text=True, timeout=10)
    observed = trace.read_text().splitlines() if trace.exists() else []
    if defect in {"label", "file"}:
        assert outcome.returncode == 3 and observed == []
    else:
        assert observed == ["external", "bootout", "private-stop"], outcome.stderr
        assert outcome.returncode == (3 if defect else 0), outcome.stderr



@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
@pytest.mark.parametrize("endpoint", ["absent", "refused", "live", "regular", "symlink", "foreign", "permission"])
def test_deenrolled_bot_freeze_requires_positive_private_endpoint_quiet(tmp_path_factory, monkeypatch, endpoint, manager):
    import plistlib
    from types import SimpleNamespace
    directory = tmp_path_factory.mktemp("sq")
    socket_dir = directory / ("tmux-" + str(os.getuid()))
    socket_dir.mkdir()
    path = socket_dir / "b"
    content = plistlib.dumps({"Label": "b", "ProgramArguments": ["/bin/true"],
                             "EnvironmentVariables": {"TMUX_TMPDIR": str(directory)}})
    if manager == "Linux":
        content = ("[Service]\nEnvironment=TMUX_TMPDIR=" + str(directory) + "\n").encode()
    generated = directory / ("b.service" if manager == "Linux" else "b.plist")
    generated.write_bytes(content)
    absent_installed = directory / "native" / generated.name
    assert generated.exists() and not absent_installed.exists()
    row = {"target": "b.service" if manager == "Linux" else "gui/123/b", "installed": [], "properties": [
           ["UnitFileState", "not-found" if manager == "Linux" else "unchanged"],
           ["LoadState", "not-found" if manager == "Linux" else "unloaded"], ["ActiveState", "inactive"]],
           "generated": {"path": str(generated), "content": {"base64": base64.b64encode(content).decode()}},
           "declaration": {"source": str(generated), "scope": "bot", "service": None,
                           "working_directory": str(directory)}}
    monkeypatch.setattr(services.platform, "system", lambda: manager)
    monkeypatch.setattr(services, "current_git_declarations", lambda *_: ())
    inventory = SimpleNamespace(payload=lambda: {"units": [deepcopy(row)]})
    monkeypatch.setattr(services, "collect_enrollment", lambda *args, **kwargs: SimpleNamespace(require_complete=lambda: inventory))
    class Adapter:
        def call(self, function, *args, **kwargs):
            pytest.fail("inventory already proves native absence; generated files are not installed native targets")
    server = None
    if endpoint in {"refused", "live", "foreign"}:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        if endpoint == "live":
            server.listen(1)
        else:
            server.close()
    elif endpoint == "regular":
        path.write_text("foreign file")
    elif endpoint == "symlink":
        path.symlink_to(directory / "absent")
    if endpoint in {"foreign", "permission"}:
        original = Path.lstat
        def lstat(actual, *args, **kwargs):
            if actual == path:
                if endpoint == "permission":
                    raise PermissionError("unreadable endpoint")
                info = original(actual, *args, **kwargs)
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
            return original(actual, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", lstat)
    try:
        if endpoint in {"absent", "refused"}:
            rows = services.freeze_service_intent({"data_root": str(directory)}, Adapter(), directory)
            assert rows[0]["disposition"] == "uninstalled" and rows[0]["installed"] == []
        else:
            with pytest.raises(runtime.RuntimeEvidenceError, match="socket"):
                services.freeze_service_intent({"data_root": str(directory)}, Adapter(), directory)
        if endpoint != "absent" and endpoint != "permission":
            assert path.is_symlink() or path.exists()  # Proof never unlinks retained endpoints.
    finally:
        if server is not None:
            server.close()


@pytest.mark.parametrize("state", ["masked-runtime", "masked"])
def test_conversion_resume_preserves_observed_mask_without_start_or_unmask(tmp_path, state):
    """Pi's parked quiet bots stay down; the old restart intent grants no start."""
    unit = tmp_path / "worker.service"
    unit.write_text("[Service]\nExecStart=/bin/true\n")
    trace = tmp_path / "calls"
    script = r'''
. "$1/supervisor.sh"
_OS=Linux; file="$2"; target=worker.service; saved="$3 masked inactive"; trace="$4"
mask="$3"
systemctl() {
    printf '%s\n' "$*" >> "$trace"
    [ "$2" = show ] || return 3
    printf 'Id=worker.service\nLoadState=masked\nActiveState=inactive\nSubState=dead\nUnitFileState=%s\nFragmentPath=/dev/null\nControlGroup=\nMainPID=0\nControlPID=0\n' "$mask"
}
svc_activation_resume "$file" "$target" "$saved"
'''
    result = _runtime_bash(script, NATIVE, unit, state, trace)
    assert result.returncode == 0, result.stderr
    calls = trace.read_text().splitlines()
    assert calls and all(call.startswith("--user show ") for call in calls)
    assert unit.read_text() == "[Service]\nExecStart=/bin/true\n"


def _paused_bot(tmp_path, monkeypatch, active="inactive"):
    """Pi quiet/masked bot row, frozen by conversion: no unit file, its runtime mask kept."""
    row = intent(tmp_path)
    generated = tmp_path / "generated" / row["target"]
    generated.parent.mkdir()
    row["generated"].update(path=str(generated), resolved=str(generated))
    content = ("[Service]\nEnvironment=TMUX_TMPDIR=" + str(tmp_path / "tmux") + "\nExecStart=/bin/true\n").encode()
    generated.write_bytes(content)
    row["generated"].update(sha256=hashlib.sha256(content).hexdigest(), content={"base64": base64.b64encode(content).decode()})
    installed = Path(row["installed"][0]["path"])
    installed.unlink()
    mask = tmp_path / "runtime" / row["target"]
    mask.parent.mkdir()
    mask.symlink_to("/dev/null")
    mask_snapshot = {"path": str(mask), "resolved": "/dev/null", "link": "/dev/null", "mode": mask.lstat().st_mode & 0o777,
                     "sha256": hashlib.sha256(b"").hexdigest(), "content": {"base64": ""}}
    declaration = {**row["declaration"], "source": str(generated), "scope": "bot", "service": None,
                   "fleet": "example", "bot": "worker"}
    observed = {"target": row["target"], "installed": [], "generated": row["generated"], "declaration": declaration,
                "properties": [["UnitFileState", "masked-runtime"], ["LoadState", "masked"], ["ActiveState", active]]}
    parked = {str(installed): {"target": row["target"], "snapshot": row["installed"][0], "phase": "bots",
                              "properties": [["UnitFileState", "enabled"], ["LoadState", "loaded"], ["ActiveState", "active"]]}}
    baseline = SimpleNamespace(root=tmp_path, manager="Linux", domain="", install_directory=tmp_path,
        declarations=(SimpleNamespace(source=generated),), release=object(), parked=parked,
        placements=({"source": str(generated), "target": row["target"]},))
    inventory = SimpleNamespace(payload=lambda: {"units": [deepcopy(observed)], "observed_files": [deepcopy(mask_snapshot)]})
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(require_complete=lambda: inventory)
    monkeypatch.setattr(services, "collect_enrollment", collect)
    monkeypatch.setattr(services.platform, "system", lambda: "Linux")
    monkeypatch.setattr(services, "_catalog", lambda _: ("Linux", "", [], [], []))
    class QuietMask(Native):
        def call(self, function, *args, timeout=30):
            if function == "svc_activation_quiet":
                self.calls.append((function, args))
                return SimpleNamespace(returncode=0, stdout="inactive\tno-cgroup-witness", stderr="")
            return super().call(function, *args, timeout=timeout)
    adapter = QuietMask()
    adapter.read = lambda *_: "catalog"
    adapter.state = "masked-runtime masked " + active
    frozen, = services.freeze_service_intent(None, adapter, tmp_path, baseline=baseline)
    return SimpleNamespace(frozen=frozen, adapter=adapter, calls=calls, parked=parked, installed=installed,
                           mask=mask, mask_snapshot=mask_snapshot, generated=generated, content=content)


@pytest.mark.parametrize("active", ["inactive", "failed"])
def test_pending_conversion_freezes_actual_mask_and_never_publishes_or_starts_it(tmp_path, monkeypatch, active):
    """Pi quiet/masked rows retain physical absence despite old enabled intent."""
    case = _paused_bot(tmp_path, monkeypatch, active)
    frozen, adapter = case.frozen, case.adapter
    assert case.calls[0]["historical_parked"] == case.parked
    assert frozen["installed"] == [] and frozen["disposition"] == "stopped"
    assert frozen["saved_native"] == frozen["restore_native"] == adapter.state
    assert frozen["preserved_mask"]["snapshot"] == case.mask_snapshot and "legacy_parked" not in frozen
    store = Store(tmp_path, frozen)
    services.quiesce_consumer(store, frozen["target"], adapter)
    # It stays paused, and the restore names it rather than reporting it done (#2699).
    with pytest.raises(UpdateError, match=f"left bot example/worker paused: {frozen['target']} keeps its runtime mask"):
        services.restore_consumer(store, frozen["target"], adapter, {})
    assert store.record["consumers"][frozen["target"]]["restore"] == "done"
    assert {name for name, _ in adapter.calls} == {"svc_inventory_state", "svc_activation_quiet"}
    assert not case.installed.exists() and case.mask.is_symlink() and os.readlink(case.mask) == "/dev/null"
    assert case.generated.read_bytes() == case.content


class UnitStates(Native):
    """Native state per unit, so a paused bot and a timer restore side by side."""

    def __init__(self, states):
        super().__init__()
        self.states = dict(states)

    def call(self, function, *args, timeout=30):
        self.calls.append((function, args))
        output = ""
        if function == "svc_bot_session_observe":
            output = self.session
        elif function == "svc_activation_pause":
            self.states[args[1]] = "masked-runtime masked inactive"
        elif function == "svc_activation_resume":
            self.states[args[1]] = args[2]
        elif function in {"svc_activation_snapshot", "svc_inventory_state"}:
            output = self.states[args[1]]
        elif function == "svc_activation_quiet":
            output = "inactive\tno-cgroup-witness" if self.states[args[1]].endswith(" inactive") else "unknown"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def _at_restore(store, tmp_path, monkeypatch):
    """The update's configuration is committed, and its restore stage runs next."""
    from claudlobby import update_operation
    store.root, store.installation = tmp_path, None
    store.record.update(phase="restore", config_committed=True, target="target",
                        candidate_config={"fingerprint": "candidate"})
    monkeypatch.setattr(update_operation, "read_current_config", lambda *_args, **_kwargs: {"fingerprint": "candidate"})


def _paused_update(tmp_path, monkeypatch):
    """An update at its restore stage over the paused bot and a parked timer, which restores after it."""
    case = _paused_bot(tmp_path, monkeypatch)
    bot = case.frozen
    (tmp_path / "timer").mkdir()
    timer = intent(tmp_path / "timer")
    unit = Path(timer["installed"][0]["path"])
    unit.rename(unit.with_name("job.timer"))
    timer["target"] = "job.timer"
    timer["installed"][0].update(path=str(unit.with_name("job.timer")), resolved=str(unit.with_name("job.timer")))
    declarations(monkeypatch, timer)
    store = Store(tmp_path, bot)
    store.record["consumers"][timer["target"]] = {"intent": timer}
    store.record["scope"] = services.consumer_scope([bot, timer])
    adapter = UnitStates({bot["target"]: "masked-runtime masked inactive", timer["target"]: "disabled loaded inactive"})
    for key in (bot["target"], timer["target"]):
        services.quiesce_consumer(store, key, adapter)
    _at_restore(store, tmp_path, monkeypatch)
    return case, bot, timer, store, adapter


@pytest.mark.parametrize("decision", ["stop", "start"])
def test_a_bot_left_paused_keeps_the_update_pending_until_it_runs_or_its_stop_is_recorded(tmp_path, monkeypatch, decision):
    """#2699: the update reported done with four bots down. It names each one until someone decides."""
    from claudlobby import update_operation
    from claudlobby.stop_record import write_stop
    case, bot, timer, store, adapter = _paused_update(tmp_path, monkeypatch)

    first = update_operation.restore_operation(store, adapter)
    error = first["health_errors"][bot["target"]]
    assert set(first["health_errors"]) == {bot["target"]}
    assert error.startswith(f"this update left bot example/worker paused: {bot['target']} keeps its runtime mask")
    assert f"systemctl --user unmask --runtime {bot['target']}" in error
    assert "claudlobby --fleet example bot start worker" in error and "claudlobby --fleet example bot stop worker" in error
    # The decision does not hold the schedules back: the timer restores after the bot as before.
    assert first["consumers"][timer["target"]]["restore"] == "done" and adapter.states[timer["target"]] == "disabled loaded inactive"

    # Continuing without a decision changes nothing; a reboot that drops the mask leaves the bot down all the same.
    assert update_operation.restore_operation(store, adapter)["health_errors"] == first["health_errors"]
    case.mask.unlink()
    adapter.states[bot["target"]] = "not-found not-found inactive"
    rebooted = update_operation.restore_operation(store, adapter)["health_errors"]
    assert set(rebooted) == {bot["target"]}
    assert rebooted[bot["target"]].startswith(f"this update left bot example/worker down: {bot['target']} does not read as running")

    if decision == "stop":
        write_stop(tmp_path, by="operator", reason=None, request_id="00000000-0000-4000-8000-000000000001")
    else:
        # An active unit is not yet a running bot: its session must answer ready.
        case.installed.write_bytes(case.content)
        adapter.states[bot["target"]] = "enabled loaded active"
        starting = update_operation.restore_operation(store, adapter)["health_errors"]
        assert set(starting) == {bot["target"]} and "(native state: enabled loaded active, session absent)" in starting[bot["target"]]
        # The real observer answers unknown while the unit is active and its session not yet up: still down.
        adapter.session = "unknown"
        starting = update_operation.restore_operation(store, adapter)["health_errors"]
        assert set(starting) == {bot["target"]} and "(native state: enabled loaded active, session unknown)" in starting[bot["target"]]
        adapter.session = "ready"
    done = update_operation.restore_operation(store, adapter)
    assert done["phase"] == "done" and done["health_errors"] == {}
    assert done["consumers"][bot["target"]]["outcome"] == services._PAUSED + (
        "; the bot's stop is recorded" if decision == "stop" else "; the bot runs again")
    effects = {"svc_activation_pause", "svc_activation_resume", "svc_activation_start", "svc_activation_clear_runtime_mask"}
    assert not [name for name, args in adapter.calls if name in effects and args[1] == bot["target"]]


def test_a_restore_pass_cut_off_before_the_paused_bot_is_read_names_it_on_the_next_pass(tmp_path, monkeypatch):
    """A kill, a power cut or the restore child's time limit can stop a pass mid-read; the next pass reads the bot."""
    from claudlobby import update_operation
    _, bot, timer, store, adapter = _paused_update(tmp_path, monkeypatch)

    class Cut(BaseException):
        pass

    def call(function, *args, timeout=30, read=adapter.call):
        if store.record["consumers"][bot["target"]].get("restore") == "done":
            raise Cut(function)
        return read(function, *args, timeout=timeout)
    adapter.call = call
    with pytest.raises(Cut, match="svc_inventory_state"):
        update_operation.restore_operation(store, adapter)
    del adapter.call
    assert store.record["phase"] == "restore" and store.record["consumers"][bot["target"]]["restore"] == "done"
    resumed = update_operation.restore_operation(store, adapter)
    assert set(resumed["health_errors"]) == {bot["target"]}
    assert resumed["health_errors"][bot["target"]].startswith(f"this update left bot example/worker paused: {bot['target']}")
    assert resumed["consumers"][timer["target"]]["restore"] == "done"


@pytest.mark.parametrize("kind,previous,desired", [
    ("resident", "enabled loaded active", "enabled loaded active"),
    ("stopped", "disabled loaded inactive", "disabled loaded inactive"),
    ("timer", "enabled loaded active", "enabled loaded active"),
    ("oneshot", "enabled loaded active", "enabled loaded inactive"),
    ("preexisting_mask", "masked-runtime masked inactive", "masked-runtime masked inactive"),
])
def test_completed_old_producer_pause_restores_original_intent(tmp_path, monkeypatch, kind, previous, desired):
    """The completed producer pause created this mask; it did not change intent."""
    row = intent(tmp_path)
    if kind == "timer":
        original = Path(row["installed"][0]["path"])
        original.rename(original.with_suffix(".timer"))
        row["target"] = original.with_suffix(".timer").name
        row["installed"][0].update(path=str(original.with_suffix(".timer")), resolved=str(original.with_suffix(".timer")))
        # The restored intent only: the held start of a timer restored to
        # running has its own tests in test_update_timer_catch_up.py.
        monkeypatch.setattr(services, "_held_start", lambda *_: None)
    installed = Path(row["installed"][0]["path"])
    generated = tmp_path / "generated" / row["target"]
    generated.parent.mkdir()
    generated.write_bytes(services._content(row["generated"]))
    row["generated"].update(path=str(generated), resolved=str(generated))
    installed.unlink()
    mask = tmp_path / "runtime" / row["target"]
    mask.parent.mkdir(); mask.symlink_to("/dev/null")
    mask_snapshot = {"path": str(mask), "resolved": "/dev/null", "link": "/dev/null", "mode": mask.lstat().st_mode & 0o777,
                     "sha256": hashlib.sha256(b"").hexdigest(), "content": {"base64": ""}}
    journal = tmp_path / "old-parking" / "journal.json"
    blobs = journal.parent / "blobs"; blobs.mkdir(parents=True)
    (blobs / row["installed"][0]["sha256"]).write_bytes(services._content(row["installed"][0]))
    journal.write_text("retained old journal placeholder\n")
    declaration = {**row["declaration"], "source": str(generated), "service": None}
    properties = [[key, value] for key, value in zip(("UnitFileState", "LoadState", "ActiveState"), previous.split())]
    parked = {str(installed): {"target": row["target"], "snapshot": row["installed"][0], "phase": "producers",
                              "paused_native": previous, "properties": properties, "journal": str(journal)}}
    observed = {"target": row["target"], "installed": [], "generated": row["generated"], "declaration": declaration,
                "properties": [["UnitFileState", "masked-runtime"], ["LoadState", "masked"], ["ActiveState", "inactive"]]}
    rows = [observed]
    if kind == "oneshot":
        timer = generated.with_suffix(".timer")
        timer.write_text("[Timer]\n")
        rows.append({"target": timer.name, "installed": [], "generated": {},
                     "declaration": {**declaration, "source": str(timer), "service": row["target"]},
                     "properties": [["UnitFileState", "not-found"], ["LoadState", "not-found"], ["ActiveState", "inactive"]]})
    baseline = SimpleNamespace(root=tmp_path, manager="Linux", domain="", install_directory=tmp_path,
        declarations=tuple(SimpleNamespace(source=Path(item["declaration"]["source"])) for item in rows),
        release=object(), parked=parked, placements=())
    inventory = SimpleNamespace(payload=lambda: {"units": deepcopy(rows), "observed_files": [deepcopy(mask_snapshot)]})
    monkeypatch.setattr(services, "collect_enrollment", lambda *a, **k: SimpleNamespace(require_complete=lambda: inventory))
    monkeypatch.setattr(services.platform, "system", lambda: "Linux")
    monkeypatch.setattr(services, "_catalog", lambda _: ("Linux", "", [], [], []))
    class Paused(Native):
        def call(self, function, *args, timeout=30):
            if function == "svc_activation_quiet":
                self.calls.append((function, args))
                return SimpleNamespace(returncode=0, stdout="inactive\tno-cgroup-witness", stderr="")
            return super().call(function, *args, timeout=timeout)
    adapter = Paused(); adapter.read = lambda *_: "catalog"; adapter.state = "masked-runtime masked inactive"
    frozen = next(item for item in services.freeze_service_intent(None, adapter, tmp_path, baseline=baseline)
                  if item["target"] == row["target"])
    if kind == "preexisting_mask":
        assert frozen["preserved_mask"]["snapshot"] == mask_snapshot and frozen["installed"] == []
        assert frozen["restore_native"] == desired and not installed.exists()
        # Not a bot, so the restore keeps the mask as before: paused, and no health error (#2699).
        from claudlobby import update_operation
        frozen["phase"] = "producers"  # a real row's phase, from its candidate; the restore loop orders by it
        store = Store(tmp_path, frozen)
        _at_restore(store, tmp_path, monkeypatch)
        adapter.calls.clear()
        done = update_operation.restore_operation(store, adapter)
        assert done["phase"] == "done" and done["health_errors"] == {}
        assert done["consumers"][frozen["target"]]["restore"] == "done" and done["consumers"][frozen["target"]]["outcome"] == services._PAUSED
        assert {name for name, _ in adapter.calls} == {"svc_inventory_state", "svc_activation_quiet"}
        assert not installed.exists() and mask.is_symlink()
        return
    assert "preserved_mask" not in frozen and frozen["legacy_parked"] == parked[str(installed)]
    assert frozen["saved_native"] == "masked-runtime masked inactive" and frozen["restore_native"] == desired
    declarations(monkeypatch, frozen)
    store = Store(tmp_path, frozen)
    services.quiesce_consumer(store, frozen["target"], adapter)
    assert not installed.exists() and mask.is_symlink()
    assert not any(name == "svc_activation_pause" for name, _ in adapter.calls)
    services.restore_consumer(store, frozen["target"], adapter, {})
    assert installed.read_bytes() == generated.read_bytes()
    assert adapter.state == desired
    resumes = [args for name, args in adapter.calls if name == "svc_activation_resume"]
    assert len(resumes) == 1 and resumes[0][2] == desired


def test_native_readiness_waits_for_matching_sessionstart_without_second_start(tmp_path):
    import concurrent.futures
    import time
    from claudlobby import native_session
    from tests.test_native_session import Observation, session_start

    bot, tmpdir, observations = tmp_path / "bots/worker", tmp_path / "private-tmux", tmp_path / "confirmations"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text(f'BOT_SERVICE="fixture-worker"\nTMUX_TMPDIR="{tmpdir}"\n')
    receipt_path = _native_confirm_cli(bot, tmpdir, observations=observations)
    original = native_session.read(bot)
    assert native_session.launch(bot, "fixture-worker", tmpdir, bot / "account") == ("resume", original["session_id"])
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "5")
    assert fence.returncode == 0, fence.stderr
    _, token = fence.stdout.strip().split("\t")
    with (bot / "logs/startup.log").open("a") as log:
        log.write("READY — non-channel bot, no poller to await\n")

    def delayed_hook():
        deadline = time.monotonic() + 5
        while not observations.exists() or "pending" not in observations.read_text():
            assert time.monotonic() < deadline, "readiness never observed the original pending hook"
            time.sleep(0.01)
        native = Observation(tmp_path)
        native.pid += 1
        native_session.capture(bot, "fixture-worker", tmpdir, bot / "account", native,
            session_start(bot, bot / "account", original["session_id"], source="resume"), caller=123)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        hook = pool.submit(delayed_hook)
        ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "5", token, tmpdir)
        hook.result(timeout=6)
    assert ready.returncode == 0, ready.stderr
    assert ready.stdout.strip() == "session-ready"
    attempts = observations.read_text().splitlines()
    assert "pending" in attempts and attempts[-1] == "confirmed" and attempts.count("attempt") >= 2
    confirmed = native_session.confirm(bot, expected=original)
    assert confirmed["session_id"] == original["session_id"] and confirmed["process"] != original["process"]
    assert json.loads(receipt_path.read_text()) == confirmed


@pytest.mark.parametrize("state", ["pending-no-budget", "pending-spent-budget", "malformed"])
def test_native_readiness_never_retries_fatal_receipt_or_extends_original_ceiling(tmp_path, state):
    from claudlobby import native_session
    bot, tmpdir, observations = tmp_path / "bots/worker", tmp_path / "private-tmux", tmp_path / "confirmations"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text(f'BOT_SERVICE="fixture-worker"\nTMUX_TMPDIR="{tmpdir}"\n')
    receipt = _native_confirm_cli(bot, tmpdir, observations=observations)
    if state == "malformed":
        receipt.write_bytes(b"invalid original receipt")
    else:
        native_session.launch(bot, "fixture-worker", tmpdir, bot / "account")
    original = receipt.read_bytes()
    prefix = '''fixture_tmux() {
    [ "$*" = '-L fixture-worker has-session -t worker' ] || return 1
    [ "${SPEND_BUDGET:-0}" = 0 ] || SECONDS=$((SECONDS + 5))
}
sleep() { printf unexpected-poll > "$POLL_TRACE"; return 99; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    from tests.conftest import constructed_env
    poll = tmp_path / "poll"
    env = constructed_env(HOME=tmp_path, POLL_TRACE=poll,
                          SPEND_BUDGET="1" if state == "pending-spent-budget" else "0")
    fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "5", env=env)
    assert fence.returncode == 0, fence.stderr
    _, token = fence.stdout.strip().split("\t")
    with (bot / "logs/startup.log").open("a") as log:
        log.write("READY — non-channel bot, no poller to await\n")
    ceiling = "0" if state == "pending-no-budget" else "5"
    refused = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, ceiling, token, tmpdir, env=env)
    assert refused.returncode != 0
    assert observations.read_text().splitlines().count("attempt") == 1
    assert not poll.exists(), "fatal/past-ceiling confirmation was polled again"
    assert receipt.read_bytes() == original
