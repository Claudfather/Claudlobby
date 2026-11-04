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



def _runtime_bash(script, *args):
    process = subprocess.Popen(["/bin/bash", "-c", script, "activation-fixture", *map(str, args)],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
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



def test_real_restart_policy_fence_is_verified_and_ready_uses_same_marker(tmp_path):
    bot = tmp_path / "bots/worker"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('RC_READY_TIMEOUT_S="001"\nBOT_SERVICE="fixture-worker"\n'
                                  'TELEGRAM_BOT_HANDLE="fixture_handle"\n')
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
    ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert ready.returncode == 0 and ready.stdout.strip() == "bridge-ready"
    missing = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", "absent-marker")
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
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('BOT_SERVICE="fixture-worker"\n' + extra_conf)
    log = bot / "logs/startup.log"
    log.parent.mkdir()
    log.write_text(accepted + "\n")  # an old boot cannot satisfy the new fence
    prefix = '''fixture_tmux() { [ "$*" = '-L fixture-worker has-session -t worker' ]; }
TMUX_BIN=fixture_tmux; export TMUX_BIN
. "$1/supervisor.sh"; shift; "$@"'''
    fence = _runtime_bash(prefix, NATIVE, "svc_activation_bot_fence", tmp_path, bot, "1")
    assert fence.returncode == 0, fence.stderr
    _, token = fence.stdout.strip().split("\t")
    assert _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token).returncode != 0
    with log.open("a") as out:
        out.write(rejected + "\n")
    assert _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token).returncode != 0
    with log.open("a") as out:
        out.write(accepted + "\n")
    ready = _runtime_bash(prefix, NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert ready.returncode == 0 and ready.stdout.strip() == readiness
    no_session = _runtime_bash(prefix.replace(
        "fixture_tmux() { [ \"$*\" = '-L fixture-worker has-session -t worker' ]; }",
        "fixture_tmux() { return 1; }",
    ), NATIVE, "svc_activation_bot_ready", tmp_path, bot, "0", token)
    assert no_session.returncode != 0



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
