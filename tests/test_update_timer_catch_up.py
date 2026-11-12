"""A timer the update restores to running never runs its job as catch-up (G18).

systemd 252.11 to 258.0 count a timer started again in the same user manager
from its previous start (upstream #31231), so every calendar time since then
runs its job the moment the restore starts it; a Persistent=true timer counts
from its last run, so a time missed while the update held it runs at once too.

The fake manager below keeps only what decides that. A start of a timer with a
calendar time due elapses at once, and so does OnActiveSec= from a loaded
drop-in; the elapse comes just after the start returns, at the restore's next
wait. Each elapse starts the service, whose loaded ExecCondition= commands run
first, for real, and skip the job on a nonzero exit. Drop-ins load only at a
reload, as in systemd; `systemctl unmask` reloads too, as in the adapter.
"""
import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

from claudlobby import update_services as services
from claudlobby.git_update import UpdateError

TIMER, SERVICE = "example.timer", "example.service"


class Store:
    def __init__(self, tmp_path, intent):
        self.work = tmp_path / "update-work"
        self.record = {"consumers": {
            intent["target"]: {"intent": intent},
            # Its service restored first, as the restore orders services before schedules.
            SERVICE: {"intent": {"manager": "Linux", "scheduled": True}, "restore": "done"},
        }, "scope": services.consumer_scope([intent]), "quiet": {}}

    def _assert_locked(self):
        pass

    def load(self):
        return deepcopy(self.record)

    def save(self, value):
        self.record = deepcopy(value)


class Manager:
    """A user manager, as far as one restored timer and its service go."""

    def __init__(self, runtime, *, due):
        self.runtime = runtime
        self.due = due              # a calendar time fell after its previous start (or last run)
        self.on_active = True       # a loaded OnActiveSec= elapses once, at the start
        self.state = {TIMER: "masked-runtime masked inactive", SERVICE: "static loaded inactive"}
        self.loaded = {TIMER: [], SERVICE: []}
        self.runs, self.calls, self.timer_drop_ins_at_start = [], [], []
        self.pending = False  # an elapse the manager has yet to dispatch
        self.cut = None  # "before-the-start" or "after-the-start": the resume call fails there

    def _reload(self):
        for unit in self.loaded:
            directory = self.runtime / "systemd" / "user" / f"{unit}.d"
            self.loaded[unit] = sorted(str(path) for path in directory.glob("*.conf"))

    def _values(self, unit, key):
        values = []
        for path in self.loaded[unit]:
            for line in Path(path).read_text().splitlines():
                name, _, value = line.partition("=")
                if name == key:
                    values = [] if value == "" else [*values, value]
        return values

    def tick(self):
        """Time passes: an elapse due since the start is dispatched."""
        if self.pending:
            self.pending = self.due = False  # this elapse is now its last trigger
            self.elapse()

    def elapse(self):
        """The timer elapses: its service starts, and each ExecCondition= runs first."""
        for command in self._values(SERVICE, "ExecCondition"):
            if subprocess.run(shlex.split(command), env={}, timeout=10).returncode:
                return  # skipped: the job does not run
        self.runs.append(SERVICE)

    def call(self, function, *args, timeout=30):
        self.calls.append(function)
        output = ""
        if function == "svc_activation_reload":
            self._reload()
        elif function == "svc_activation_resume":
            if self.cut == "before-the-start":
                raise TimeoutError("cut off before the start")
            _, target, desired = args
            self._reload()  # systemctl unmask --runtime reloads the manager
            self.state[target] = desired
            if target == TIMER and desired.endswith(" active"):
                self.timer_drop_ins_at_start.append([Path(path).read_text() for path in self.loaded[TIMER]])
                self.pending = self.due or (self.on_active and bool(self._values(TIMER, "OnActiveSec")))
            if self.cut == "after-the-start":
                raise TimeoutError("cut off after the start")
        elif function == "svc_activation_snapshot":
            output = self.state[args[1]]
        elif function == "svc_inventory_properties":
            unit = args[0]
            file_state, load, active = self.state[unit].split()
            output = "\n".join((f"Id={unit}", f"LoadState={load}", f"ActiveState={active}",
                                f"UnitFileState={file_state}", "FragmentPath=", "NeedDaemonReload=no",
                                "DropInPaths=" + ("" if load == "masked" else " ".join(self.loaded[unit])),
                                "Triggers=", "TriggeredBy="))
        else:
            raise AssertionError(f"unexpected native call {function}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def restore(tmp_path, monkeypatch, *, due, desired="enabled loaded active"):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    generated, installed = tmp_path / "generated" / TIMER, tmp_path / "units" / TIMER
    for path in (generated, installed):
        path.parent.mkdir()
        path.write_bytes(b"[Timer]\nOnCalendar=*-*-* 18:30:00\n")

    def snapshot(path):
        return {"path": str(path), "resolved": str(path), "mode": path.stat().st_mode & 0o777,
                "link": None, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content": {"base64": base64.b64encode(path.read_bytes()).decode()}}
    row = {"target": TIMER, "manager": "Linux", "installed": [snapshot(installed)],
           "generated": snapshot(generated), "scheduled": False, "bot_binding": None,
           "disposition": "resident", "saved_native": desired, "restore_native": desired,
           "properties": [], "declaration": {"source": str(generated), "working_directory": str(tmp_path),
                                             "scope": "host", "environment": [], "service": SERVICE}}
    monkeypatch.setattr(services, "current_git_declarations", lambda *_: [SimpleNamespace(
        source=generated, working_directory=tmp_path)])
    manager = Manager(runtime, due=due)
    monkeypatch.setattr(time, "sleep", lambda _: manager.tick())
    return Store(tmp_path, row), manager


def nothing_held(tmp_path, manager):
    return (manager.loaded == {TIMER: [], SERVICE: []}
            and not [path for path in (tmp_path / "run").rglob("*") if path.is_file()])


@pytest.mark.parametrize("due", [True, False], ids=["a-time-since-its-previous-start", "nothing-due"])
def test_a_timer_restored_to_running_never_runs_its_job_as_catch_up(tmp_path, monkeypatch, due):
    store, manager = restore(tmp_path, monkeypatch, due=due)
    services.restore_consumer(store, TIMER, manager, {})
    manager.tick()  # time passes after the restore returns
    assert manager.runs == []
    assert manager.state[TIMER] == "enabled loaded active"
    assert store.record["consumers"][TIMER]["restore"] == "done"
    # The start went out held: one elapse at once, without the unit's accuracy
    # window or random delay, which its service skipped.
    assert manager.timer_drop_ins_at_start == [["[Timer]\nOnActiveSec=1us\nAccuracySec=1us\nRandomizedDelaySec=0\n"]]
    # Nothing of the hold is left, so the next scheduled time runs the job.
    assert nothing_held(tmp_path, manager)
    manager.elapse()
    assert manager.runs == [SERVICE]


def test_a_held_start_whose_elapse_never_comes_keeps_its_service_held_and_says_so(tmp_path, monkeypatch):
    store, manager = restore(tmp_path, monkeypatch, due=False)
    manager.on_active = False  # nothing elapses at the start, so nothing proves the hold was used
    monkeypatch.setattr(services, "_HELD_START_WAIT_S", 0)
    with pytest.raises(UpdateError, match="has not taken its held start"):
        services.restore_consumer(store, TIMER, manager, {})
    with pytest.raises(UpdateError, match="has not taken its held start"):
        services.restore_consumer(store, TIMER, manager, {})
    assert manager.loaded[SERVICE] and manager.runs == []
    manager.elapse()  # its next scheduled time is skipped, not run, and proves the hold
    assert manager.runs == []
    services.restore_consumer(store, TIMER, manager, {})
    assert manager.calls.count("svc_activation_resume") == 1
    assert store.record["consumers"][TIMER]["restore"] == "done"
    assert nothing_held(tmp_path, manager)
    manager.elapse()
    assert manager.runs == [SERVICE]


@pytest.mark.parametrize("cut", ["after-the-start", "before-the-start"])
def test_a_restore_cut_off_at_its_held_start_releases_it_without_starting_again(tmp_path, monkeypatch, cut):
    store, manager = restore(tmp_path, monkeypatch, due=True)
    manager.cut = cut
    with pytest.raises(RuntimeError, match="native observation failed"):
        services.restore_consumer(store, TIMER, manager, {})
    manager.cut = None
    manager.tick()
    if cut == "after-the-start":
        services.restore_consumer(store, TIMER, manager, {})
        assert store.record["consumers"][TIMER]["restore"] == "done"
    else:
        # A start that may have gone out is observed on retry, never resent;
        # with the timer still stopped, nothing can elapse, so the hold goes.
        with pytest.raises(UpdateError, match="observation only on retry"):
            services.restore_consumer(store, TIMER, manager, {})
    manager.tick()
    assert manager.calls.count("svc_activation_resume") == 1
    assert manager.runs == []
    assert nothing_held(tmp_path, manager)


def test_a_timer_restored_stopped_takes_no_hold(tmp_path, monkeypatch):
    store, manager = restore(tmp_path, monkeypatch, due=True, desired="enabled loaded inactive")
    services.restore_consumer(store, TIMER, manager, {})
    manager.tick()
    assert manager.runs == [] and "svc_activation_reload" not in manager.calls
    assert manager.timer_drop_ins_at_start == []
    assert store.record["consumers"][TIMER]["restore"] == "done"
    assert nothing_held(tmp_path, manager)


def test_a_timer_that_names_no_exact_service_is_refused_before_any_start(tmp_path, monkeypatch):
    store, manager = restore(tmp_path, monkeypatch, due=True)
    intent = store.record["consumers"][TIMER]["intent"]
    intent["declaration"]["service"] = "example.service; true"
    store.record["scope"] = services.consumer_scope([intent])
    with pytest.raises(UpdateError, match="names no exact service"):
        services.restore_consumer(store, TIMER, manager, {})
    assert "svc_activation_resume" not in manager.calls and manager.runs == []
