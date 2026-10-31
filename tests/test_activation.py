"""Cold-host activation with real durable owners and a private native manager."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from claudlobby import activation, activation_state as state, bot_operations, runtime_admission as admission
from claudlobby.activation_handoffs import _BEGIN as _HANDOFF_BEGIN, _END as _HANDOFF_END
from claudlobby.activation_identity import read_selected_identity_bindings
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import job_units, planned_units, unit_family
from claudlobby.migration_apply import MigrationApplyError, apply_migration, read_migration
from claudlobby.plane.db import db_file
from claudlobby.plane.identity import resolve
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.queue_paths import spool_path
from claudlobby.releases import MANIFEST, release_path, seal_release
from claudlobby.supervision_inventory import FileSnapshot, InventoryError
from tests.test_releases import installed
from tests.test_migration_plan import _database, _event_request, _pending
from tests.test_task_audit import _insert
from tests.test_activation_units import _pause_all, _prepare as _prepare_pause, enrollment  # noqa: F401
from tests.test_activation_enrollment import unit_properties


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("boot")


class NativeHost:
    """Manager observations/actions only; real grants run harmless exec callbacks."""

    def __init__(self, root, release, plan, directory, package):
        self.root, self.release, self.plan, self.directory, self.package = root, release, plan, directory, package
        self.states, self.starts, self.ready, self.calls = {}, [], [], []
        self.search_path = None  # the catalog's directory rows, in order; None lists only `directory`
        self.bad_probe = False
        self.bad_enablement = False
        self.registry_failure = None
        self.registry = []
        self.external_result = 0
        self.fail_bot = None
        self.dead, self.quiet_unknown = set(), False
        self.ready_kind = "bridge-ready"
        self.by_name = {d.source.name: (d, item) for d, item in planned_units(plan, "Linux")}

    def read(self, function, *args):
        result = self.call(function, *args)
        if result.returncode:
            raise state.ActivationError(f"fixture native refusal: {function}")
        return result.stdout

    def call(self, function, *args, timeout=30):
        args = tuple(map(str, args))
        self.calls.append((function, args))
        output, rc = "", 0
        if function == "svc_inventory_catalog":
            output = "manager\tLinux\n" + "".join(
                f"directory\t{path}\n" for path in self.search_path or [self.directory])
            output += ''.join(f"installed\t{p.name}\n" for p in sorted(self.directory.iterdir())
                              if p.suffix in (".service", ".timer"))
            output += ''.join(f"loaded\t{name}\n" for name in sorted(self.states))
        elif function == "svc_activation_assert_external":
            assert args[-1] == str(os.getpid())
            rc = self.external_result
        elif function == "svc_inventory_state":
            output = self.states.get(args[1], "not-found not-found inactive")
        elif function == "svc_inventory_properties":
            # systemctl show for a unit the manager still has loaded: after a host
            # reset, every enabled candidate is loaded again at boot.
            output = unit_properties(self.directory, args[0], self.by_name[args[0]][0],
                                     active=self.states.get(args[0], "inactive").split()[-1],
                                     unit_file_state="enabled")
        elif function == "svc_activation_snapshot":
            output = self.states[args[1]]
            if self.bad_enablement:
                output = output.replace("enabled ", "enabled-runtime ")
        elif function == "svc_activation_bot_fence":
            output = "210\tRR_FENCE_" + Path(args[1]).name
        elif function == "svc_activation_bot_ready":
            bot = Path(args[1]).name
            if bot == self.fail_bot:
                rc = 3
            else:
                self.ready.append(bot)
                output = self.ready_kind
        elif function == "svc_activation_quiet":
            if self.quiet_unknown:
                rc = 3
            elif args[1] in self.dead or self.states.get(args[1], "inactive").split()[-1] != "active":
                output = "inactive\tno-cgroup-witness"
            else:
                output = "active\tcgroup-populated"
        elif function == "svc_bot_unit_owned_by":
            reader = subprocess.run([sys.executable, str(self.package.native / "bot-unit-owner.py"), *args],
                                    capture_output=True, text=True, timeout=30)
            rc = reader.returncode
        elif function == "svc_activation_start":
            file, target = Path(args[0]), args[1]
            assert file.is_file()
            record = state.read_activation(self.root, "cold")
            declaration, item = self.by_name[target]
            expected = {"ingest": "ingest_started", "bots": "bots_started", "producers": "producers_resumed"}
            assert record.body["pending"] == expected[item["phase"]]
            if file.suffix != ".timer":
                identity = admission.RuntimeIdentity(self.release.cli_path, self.release.native_path,
                                                      self.release.inputs.artifact_id)

                class ExecReached(Exception):
                    pass

                def execute(*_):
                    raise ExecReached

                with pytest.raises(ExecReached):
                    admission.run_unit(item["admission"]["argv"], identity=identity,
                                       environment=dict(declaration.environment), execer=execute)
            self.starts.append(target)
            wanted = "timers.target" if file.suffix == ".timer" else "default.target"
            link = self.directory / (wanted + ".wants") / target
            assert link.is_symlink() and link.readlink() == file and link.resolve() == file
            self.states[target] = "enabled loaded active"
            output = "start-requested"
        else:
            raise AssertionError(f"unexpected native operation: {function}")
        return subprocess.CompletedProcess([function, *args], rc, output if output.endswith("\n") else output + "\n", "")

    def probe(self, root):
        assert root == self.root and "claudlobby-plane-daemon.service" in self.starts
        return {"probe_version": 1, "root": str(root), "pid": os.getpid(),
                "release_id": self.release.release_id,
                "seal_sha256": "wrong" if self.bad_probe else self.release.seal_sha256,
                "artifact_id": self.release.inputs.artifact_id, "cli": str(self.release.cli_path),
                "runtime": self.release.compatibility.to_dict(),
                "sql_schema": self.release.compatibility.schema.write, "schema_state": "read"}

    def registry_scan(self, context):
        assert context.paths.root == self.root and context.paths.package == self.package
        assert context.paths.fleet_yaml == self.root / "fleet.yaml"
        assert context.fleet.manager == "manager" and set(context.fleet.bots) == {"manager", "worker"}
        record = state.read_activation(self.root, "cold")
        assert record.body["pending"] == "bots_started" and "ingest_started" in record.body["completed"]
        assert self.starts == ["claudlobby-plane-daemon.service"]
        assert not (self.directory / "com.example.manager.service").exists()
        assert (self.root / "runtime/bots/worker/settings.json").is_file()
        self.registry.append(context.fleet.name)
        if self.registry_failure == "off":
            return None
        if self.registry_failure not in ("incomplete", "uncommitted"):
            self.seed_registry(context)
        return {"scan_id": "private-scan", "complete": self.registry_failure != "incomplete",
                "recording": "uncommitted" if self.registry_failure == "uncommitted" else "committed",
                "attempted_chunks": 1, "committed_chunks": 1, "event_count": 4}

    def seed_registry(self, context):
        # Authored scanner output fixtures; the actual operation-context reader
        # consumes these real migrated SQL rows without a mocked identity lookup.
        host = ensure_host_uid(self.root / "state")
        with sqlite3.connect(db_file(self.root)) as conn:
            conn.row_factory = sqlite3.Row
            def identity(kind, alias, parent=None):
                uid = resolve(conn, kind, alias, now="2026-09-28T00:00:00Z", parent_uid=parent)
                conn.execute("UPDATE identity_registry SET provisional=0 WHERE uid=?", (uid,))
                return uid
            def snapshot(kind, alias, uid, host_uid=host):
                _insert(conn, "registry_snapshots", entity_type=kind, entity_alias=alias,
                        entity_uid=uid, fleet_uid=fleet, payload=json.dumps({"alias": alias}),
                        payload_hash="fixture", cause="generate", scan_id="private-scan")
                conn.execute("UPDATE registry_snapshots SET host_uid=? WHERE ingest_seq="
                             "(SELECT MAX(ingest_seq) FROM registry_snapshots)", (host_uid,))
            fleet = identity("fleet", context.fleet.name, host)
            snapshot("fleet", context.fleet.name, fleet)
            for bot in context.fleet.bots:
                alias = f"bot:{context.fleet.name}/{bot}"
                identity("actor", alias, fleet)
                uid = identity("bot_instance", alias, fleet)
                snapshot("bot", alias, uid)
                if bot == "worker" and self.registry_failure == "foreign-host":
                    snapshot("bot", alias, uid, "host_" + "f" * 32)


def test_timer_paired_service_is_published_but_not_started_by_either_activation_path():
    entries = ({"installed": "/user/scheduled.service", "service": None},
               {"installed": "/user/scheduled.timer", "service": "scheduled.service"},
               {"installed": "/user/added.service", "service": None},
               {"installed": "/user/fleet.job-failed@.service", "service": None})  # #2522
    startable, paired = activation._startable_producers(entries)
    assert paired == {"scheduled.service"}
    assert [Path(entry["installed"]).name for entry in startable] == [
        "scheduled.timer", "added.service"]


def test_resume_stage_boundary_requires_start_receipts_and_running_handoff_witness():
    def record(step, *, source="selected"):
        completed = list(state.STEPS[:state.STEPS.index(step)])
        return SimpleNamespace(status="activating", body={"completed": completed, "pending": step,
            "previous_selection": {"activation_id": "prior"} if source == "selected" else None,
            "intent": {"source_kind": "legacy-unsealed" if source == "legacy" else None,
                       "install_directory": "/private/native"}})
    assert activation.resumable_running_step(record("queues_classified")) == "queues_classified"
    assert activation.resumable_running_step(record("backup_saved", source="legacy")) == "backup_saved"
    assert activation.resumable_running_step(record("queues_classified", source="bootstrap")) == "queues_classified"
    assert activation.resumable_running_step(record("sessions_handed_off", source="bootstrap")) == "sessions_handed_off"
    assert activation.resumable_running_step(record("sessions_handed_off")) is None
    assert activation.resumable_running_step(record("ingest_started")) is None
    # Parking converges through its journal; a begun handoff needs per-bot results.
    assert activation.resumable_running_step(record("sessions_quiesced", source="legacy")) == "sessions_quiesced"
    witnessed = record("sessions_handed_off")
    witnessed.body["handoff_effects"] = {"old.service": "handed_off", "idle.service": "server_absent"}
    assert activation.resumable_running_step(witnessed) == "sessions_handed_off"
    witnessed.body["handoff_effects"]["unknown.service"] = None
    assert activation.resumable_running_step(witnessed) is None


def test_pre_effect_prepare_refusal_cancels_intent_without_starting(cold, monkeypatch):
    root, _, plan, host = cold
    def refuse(*args, **kwargs):
        raise state.ActivationError("fixture native snapshot drift")
    monkeypatch.setattr(activation.units, "prepare_unit_pause", refuse)
    with pytest.raises(state.ActivationError, match="fixture native snapshot drift"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    record = state.read_activation(root, "cold")
    assert record.status == "rolled_back" and record.body["completed"] == []
    assert host.starts == []


def test_bootstrap_resume_from_quiesced_queue_reuses_same_id(cold, monkeypatch):
    root, _, plan, host = cold
    original = activation.build_migration_manifest
    def interrupted(*_args, **_kwargs):
        raise state.ActivationError("fixture interrupted after empty native pause")
    monkeypatch.setattr(activation, "build_migration_manifest", interrupted)
    with pytest.raises(state.ActivationError, match="fixture interrupted"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    record = state.read_activation(root, "cold")
    assert record.body["pending"] == "queues_classified" and host.starts == []
    monkeypatch.setattr(activation, "build_migration_manifest", original)
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active" and resumed.activation_id == "cold"


def _job_holding_the_lock(root, seconds):
    """A timer job's hold on the activation lock (shared), let go after *seconds*."""
    fd = os.open(root / "state/activation.lock", os.O_RDONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    job = threading.Timer(seconds, os.close, (fd,))
    job.start()
    return job


def test_bootstrap_and_upgrade_wait_for_a_running_job_instead_of_refusing(cold, monkeypatch):
    root, source, source_plan, host = cold
    job = _job_holding_the_lock(root, 1.0)
    record = activation.bootstrap_activation(root, "cold", source_plan.plan_id, host.directory, adapter=host)
    job.join()
    assert record.status == "active"

    class PastTheLock(Exception):
        pass

    def enrollment(*_args, **_kwargs):
        raise PastTheLock  # the upgrade's first step under the lock

    monkeypatch.setattr(activation, "collect_enrollment", enrollment)
    plan = ConfigPlanBuilder(root, source.release_id, source.seal_sha256, ("example",), effects={}).seal()
    job = _job_holding_the_lock(root, 1.0)
    with pytest.raises(PastTheLock):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    job.join()


def test_resume_waits_for_a_running_job_instead_of_refusing(cold, monkeypatch):
    root, _, plan, host = cold
    original = activation.build_migration_manifest
    def interrupted(*_args, **_kwargs):
        raise state.ActivationError("fixture interrupted after empty native pause")
    monkeypatch.setattr(activation, "build_migration_manifest", interrupted)
    with pytest.raises(state.ActivationError, match="fixture interrupted"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(activation, "build_migration_manifest", original)
    job = _job_holding_the_lock(root, 1.0)
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    job.join()
    assert resumed.status == "active"


def test_legacy_pending_queue_blocks_before_activation_record_or_native_pause(cold):
    root, _, plan, host = cold
    connection = _database(root, version=11)
    connection.close()
    _pending(spool_path(root) / "undrained.json", [_event_request()])
    with pytest.raises(state.ActivationError, match="migration preview blocks activation before pause"):
        activation.adopt_existing_activation(root, "cutover", plan.plan_id, host.directory, adapter=host)
    assert not (root / "state/activations/cutover/activation.json").exists()
    assert not any(name == "svc_activation_pause" for name, _ in host.calls)
    assert host.starts == []


_RUNNING = (("ActiveState", "active"), ("LoadState", "loaded"), ("UnitFileState", "enabled"))


def _legacy_handoff_estate(root, installed_bots, *, assigned=True, stopped=(), properties=_RUNNING):
    """A legacy Plane with a current worker assignment, plus frozen old bot units.

    Old bot directories sit outside the candidate plan's reviewed destinations,
    so plan freshness is unchanged.
    """
    host, fleet = "host_" + "1" * 32, "fleet_" + "2" * 32
    actors = {"manager": "actor_" + "4" * 32, "worker": "actor_" + "5" * 32}
    connection = _database(root, version=11)
    try:
        for uid, kind, alias, parent in ((host, "host", "host", None), (fleet, "fleet", "example", host),
                                         *((uid, "actor", f"bot:example/{bot}", fleet)
                                           for bot, uid in actors.items())):
            connection.execute("INSERT INTO identity_registry (uid, kind, alias, parent_uid, provisional,"
                               " first_seen, last_seen) VALUES (?, ?, ?, ?, 0, 't', 't')",
                               (uid, kind, alias, parent))
        _insert(connection, "work_items", work_item_id="wi_" + "7" * 32, fleet_uid=fleet,
                title="Running work", created_by_uid=actors["manager"])
        if assigned:
            _insert(connection, "assignments", assignment_id="asg_" + "9" * 32, work_item_id="wi_" + "7" * 32,
                    fleet_uid=fleet, assignee_uid=actors["worker"], assigned_by_uid=actors["manager"])
    finally:
        connection.close()
    identity = root / "state/host-uid"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(host + "\n")
    identity.chmod(0o600)
    units = []
    for bot in (*installed_bots, *stopped):
        directory = root.resolve() / "legacy-runtime" / bot
        directory.mkdir(parents=True)
        # `bot stop` de-enrolls: the frozen declaration remains with no installed unit.
        units.append(SimpleNamespace(target=f"legacy.example.{bot}.service",
                                     installed=() if bot in stopped else (object(),),
                                     properties=(("LoadState", "not-found"),) if bot in stopped else properties,
                                     declaration=SimpleNamespace(scope="bot", fleet="example", bot=bot,
                                                                 working_directory=directory)))
    return units


def _adopt_legacy(root, plan, host, monkeypatch, units):
    inventory = SimpleNamespace(manager="Linux", catalog=f"manager\tLinux\ndirectory\t{host.directory}\n",
                                units=tuple(units))
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(activation, "legacy_linux_declarations", lambda _plan: ("reviewed-legacy",))
    monkeypatch.setattr(activation, "collect_enrollment", lambda *_, **__: inventory)
    return lambda: activation.adopt_existing_activation(root, "cutover", plan.plan_id, host.directory,
                                                       adapter=host)


def _no_activation_effect(root, host):
    assert not (root / "state/activations/cutover").exists()
    assert state.read_selection(root) is None
    assert {name for name, _ in host.calls} == {"svc_inventory_catalog"}  # no pause, handoff or start
    assert host.starts == []


def test_first_adoption_refuses_to_start_deliberately_stopped_bot_before_record(cold, monkeypatch):
    root, _, plan, host = cold
    # `bot stop` / spin-down removed the unit file; the frozen declaration remains.
    units = _legacy_handoff_estate(root, ("manager",), assigned=False, stopped=("worker",))
    adopt = _adopt_legacy(root, plan, host, monkeypatch, units)
    with pytest.raises(state.ActivationRefusal, match=r"deliberately stopped bots \(example/worker\)"):
        adopt()
    _no_activation_effect(root, host)


@pytest.mark.parametrize("prior", ["early_abort", "same_id_abort", "unfinished_abort",
                                   "unproven_abort", "cancelled_before_effects", "same_id_cancelled",
                                   "naked_rolled_back"])
def test_fresh_adoption_admits_only_a_verified_early_abort_prior_record(cold, monkeypatch, prior):
    from claudlobby import config_install
    root, _, plan, host = cold
    old = "cutover" if prior.startswith("same_id") else "aborted"
    with state.locked_activation(root) as store:
        store.prepare(old, plan, recovery_release_id=plan.release_id,
                      enrollment_digest="1" * 64, legacy_source=True)
        if prior in ("cancelled_before_effects", "same_id_cancelled", "naked_rolled_back"):
            # The owner's receipt names this unstarted configuration journal.
            config_install.prepare_config(plan, old)
            record = store.cancel_prepared(old)
            assert record.body["cancellation"]["journals"] == [old]
            if prior == "naked_rolled_back":
                del record.body["cancellation"]
                store._save(record)
        else:
            store.begin(old, "producers_paused")
            store.begin_adoption_abort(old, reason="early abort", release_id=plan.release_id,
                                       artifact_id="artifact", sql_user_version=12)
            if prior != "unfinished_abort":
                record = store.finish_adoption_abort(old, evidence_digest="3" * 64, resumed=[])
            if prior == "unproven_abort":
                record.body["adoption_abort"]["result"]["evidence"] = "unverified"
                store._save(record)
    before = state.read_activation(root, old).body
    # The deliberately stopped bot is the first refusal after the prior-record gate.
    units = _legacy_handoff_estate(root, ("manager",), assigned=False, stopped=("worker",))
    adopt = _adopt_legacy(root, plan, host, monkeypatch, units)
    if prior in ("early_abort", "cancelled_before_effects"):
        with pytest.raises(state.ActivationRefusal, match="deliberately stopped bots"):
            adopt()
        _no_activation_effect(root, host)
        # Past the gate, the next adoption's own record and journal coexist
        # with the old attempt's retained journals.
        with state.locked_activation(root) as store:
            fresh = store.prepare("fresh", plan, recovery_release_id=plan.release_id,
                                  enrollment_digest="1" * 64, legacy_source=True)
            config_install.prepare_config(plan, "fresh")
        assert fresh.status == "prepared" and fresh.body["intent"]["source_kind"] == "legacy-unsealed"
        assert config_install.read_config_install(root, "fresh").status == "prepared"
    else:
        with pytest.raises(state.ActivationError, match=f"existing activation {old} requires explicit repair"):
            adopt()
        assert host.calls == [] and state.read_selection(root) is None
    assert state.read_activation(root, old).body == before


@pytest.mark.parametrize("properties", [
    (("ActiveState", "inactive"), ("LoadState", "loaded"), ("UnitFileState", "enabled")),
    (("ActiveState", "inactive"), ("LoadState", "loaded"), ("UnitFileState", "disabled")),
    (("ActiveState", "inactive"), ("LoadState", "unloaded"), ("UnitFileState", "unchanged")),
], ids=["systemd-enabled-inactive", "systemd-disabled-inactive", "launchd-unloaded"])
def test_run_intent_leaves_installed_inactive_bots_and_timers_to_supervision(tmp_path, properties):
    # An installed unit file is still supervised: the old keepalive restarts it.
    root = tmp_path.resolve()
    context = SimpleNamespace(paths=SimpleNamespace(bot_runtime=lambda bot: root / bot),
                              fleet=SimpleNamespace(name="example", manager="worker",
                                                    bots={"worker": SimpleNamespace(autonomous_runner=None)}))
    bot = SimpleNamespace(installed=(object(),), properties=properties,
                          declaration=SimpleNamespace(scope="bot", fleet="example", bot="worker",
                                                      working_directory=root / "worker"))
    timer = SimpleNamespace(installed=(object(),), properties=properties,
                            declaration=SimpleNamespace(scope="fleet", fleet="example", bot=None,
                                                        working_directory=root))
    activation._refuse_unrecorded_run_intent(root, (bot, timer), (context,))


def test_first_adoption_refuses_unrecorded_legacy_runner_pause_without_touching_marker(cold, monkeypatch):
    root, _, plan, host = cold
    units = _legacy_handoff_estate(root, ("manager", "worker"), assigned=False)
    marker = root.resolve() / "legacy-runtime/worker" / activation.LEGACY_RUNNER_PAUSE
    marker.write_bytes(b"paused: needs-input\n")
    original = activation._roster

    def configured(*args):
        # The fixture manifest has no runner block; configure the worker's.
        rank, contexts = original(*args)
        return rank, tuple(SimpleNamespace(
            paths=context.paths,
            fleet=SimpleNamespace(name=context.fleet.name, manager=context.fleet.manager,
                                  bots={bot: SimpleNamespace(autonomous_runner=object() if bot == "worker" else None)
                                        for bot in context.fleet.bots}))
            for context in contexts)

    monkeypatch.setattr(activation, "_roster", configured)
    adopt = _adopt_legacy(root, plan, host, monkeypatch, units)
    with pytest.raises(state.ActivationRefusal, match=r"autonomous-runner\.paused markers .*\(example/worker\)"):
        adopt()
    _no_activation_effect(root, host)
    assert marker.read_bytes() == b"paused: needs-input\n"
    assert not (root / "state/fleet-state.json").exists()


def test_run_intent_accepts_recorded_pause_new_bots_and_omitted_bots(tmp_path):
    root = tmp_path.resolve()
    runtime = root / "runtime"
    (runtime / "worker").mkdir(parents=True)
    (runtime / "worker" / activation.LEGACY_RUNNER_PAUSE).write_text("paused\n")
    paths = SimpleNamespace(bot_runtime=lambda bot: runtime / bot)
    context = SimpleNamespace(paths=paths, fleet=SimpleNamespace(
        name="example", manager="manager",
        bots={"manager": SimpleNamespace(autonomous_runner=None),
              "worker": SimpleNamespace(autonomous_runner=object()),
              "added": SimpleNamespace(autonomous_runner=None)}))
    retired = SimpleNamespace(installed=(), properties=(),
                              declaration=SimpleNamespace(scope="bot", fleet="example", bot="retired",
                                                          working_directory=runtime / "retired"))
    (root / "state").mkdir()
    (root / "state/fleet-state.json").write_text(json.dumps({"bots": {"worker": {
        "fleet": "example", "autonomous_runner_pause": {"reason": "legacy marker", "since": "t", "actor": "op"}}}}))
    # A recorded pause keeps the runner ineligible; a new candidate bot has no old
    # unit, and an omitted old bot is retired rather than started.
    activation._refuse_unrecorded_run_intent(root, (retired,), (context,))


@pytest.mark.parametrize("installed_bots, appended, message", [
    (("manager",), False, "no reviewed old bot actor"),
    (("worker",), False, "old fleet manager has no installed handoff owner"),
    (("manager", "worker"), True, "existing canonical handoff section is unreadable"),
], ids=["stopped-worker", "uninstalled-manager", "damaged-section"])
def test_first_adoption_refuses_standing_handoff_blockers_before_record_or_pause(
        cold, monkeypatch, installed_bots, appended, message):
    root, _, plan, host = cold
    units = _legacy_handoff_estate(root, installed_bots)
    handoff = root.resolve() / "legacy-runtime" / installed_bots[0] / ".claude/session.md"
    if appended:
        handoff.parent.mkdir()
        # A section at the end, where the refresh writes it, that lost its status.
        handoff.write_bytes(b"old notes\n\n" + _HANDOFF_BEGIN + b"\n```json\n{}\n```\n"
                            + _HANDOFF_END + b"\n")
    before = handoff.read_bytes() if appended else None
    inventory = SimpleNamespace(manager="Linux", catalog=f"manager\tLinux\ndirectory\t{host.directory}\n",
                                units=tuple(units))
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(activation, "legacy_linux_declarations", lambda _plan: ("reviewed-legacy",))
    monkeypatch.setattr(activation, "collect_enrollment", lambda *_, **__: inventory)
    with pytest.raises(state.ActivationError, match=message):
        activation.adopt_existing_activation(root, "cutover", plan.plan_id, host.directory, adapter=host)
    assert not (root / "state/activations/cutover").exists()
    assert state.read_selection(root) is None
    assert {name for name, _ in host.calls} == {"svc_inventory_catalog"}  # no pause or handoff
    assert host.starts == []
    sessions = [path for path in (root.resolve() / "legacy-runtime").rglob("session.md")]
    assert sessions == ([handoff] if appended else [])
    if appended:
        assert handoff.read_bytes() == before


def _cold_case(installed, monkeypatch, tmp_path, *, fleet_job=False):
    """The cold host; ``fleet_job`` adds a fleet job whose service names the
    fleet's failure template, staged through the real manifest reader (#2522)."""
    root, inputs, paths, _, directory = installed
    shutil.copytree(Path(__file__).resolve().parents[1] / "claudlobby/plane/migrations",
                    directory / "package/plane/migrations")
    release = seal_release(root, inputs, paths)
    manifest = root / "fleet.yaml"
    manifest.write_text("fleet:\n  name: example\n  manager: manager\n  service_prefix: com.example\n"
                        "  bots:\n    worker: {expertise: [testing]}\n    manager: {expertise: [orchestration]}\n")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("example",),
                                effects={"fleet_manifests": {"example": str(manifest)}, "units": []})
    projects = root / "projects.yaml"
    from claudlobby.config import host_override_path
    for source in (host_override_path(), *(root / name for name in ("library", "voices", "templates"))):
        builder.input(source)
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
           "CLAUDLOBBY_RELEASE_ID": release.release_id, "CLAUDLOBBY_CLI": str(release.cli_path),
           "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
           "CLAUDLOBBY_LIBRARY_DIR": str(directory / "library"),
           "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id}
    # Never the host's shared /tmp sockets. Short: a socket under it must fit
    # macOS's 104-byte sun_path with pytest's directories in front.
    tmux_dir = tmp_path / "tmux"
    for stem, phase, bot in (("claudlobby-plane-daemon", "ingest", None),
                             ("com.example.worker", "bots", "worker"),
                             ("com.example.manager", "bots", "manager"),
                             ("claudlobby-keepalive", "producers", None)):
        working = root / "runtime/bots" / bot if bot else root
        destination = working if bot else root / "runtime/_host/timers"
        unit_env = {**env, **({"TMUX_TMPDIR": str(tmux_dir)} if bot else {})}
        command = ([str(release.native_path / "start-bot.sh"), str(working)] if bot else
                   [str(release.native_path / "keepalive-all.sh")] if phase == "producers" else
                   [str(release.native_path / "plane-daemon.sh")])
        argv = admission.wrap_unit_argv(unit_env, unit=stem, phase=phase,
                                        mode="oneshot" if phase == "producers" else "exec", argv=command)
        files = {stem + ".plist": (plistlib.dumps({"Label": stem, "WorkingDirectory": str(working),
                    "EnvironmentVariables": unit_env, "ProgramArguments": list(argv)}), 0o644),
                 stem + ".service": ((f"[Service]\nWorkingDirectory={working}\n"
                    + (f"Environment=TMUX_TMPDIR={tmux_dir}\n" if bot else "") +
                    f"ExecStart={admission.unit_systemd_command(argv)}\n" +
                    ("" if phase == "producers" else "[Install]\nWantedBy=default.target\n")).encode(), 0o644)}
        if phase == "producers":
            files[stem + ".timer"] = (f"[Timer]\nUnit={stem}.service\n[Install]\nWantedBy=timers.target\n".encode(), 0o644)
        builder.effects["units"].extend(unit_family(files, destination=destination,
            scope="bot" if bot else "host", phase=phase, release_id=release.release_id,
            fleet="example" if bot else None, bot=bot))
        builder.directory(destination)
        for name, (content, mode) in files.items():
            builder.file(destination / name, content, mode=mode)
        if bot:
            builder.file(working / "settings.json", b'{"permissions":{"deny":["Write"]}}\n')
    if fleet_job:
        timers, stem = root / "runtime/fleet/timers", "com.example.creds-check"
        argv = admission.wrap_unit_argv(env, unit=stem, phase="producers", mode="oneshot",
                                        argv=[str(release.native_path / "creds-check.sh"), "example"])
        identity = "".join(admission.unit_systemd_environment(key, value) + "\n" for key, value in env.items())
        files = {
            stem + ".plist": (plistlib.dumps({"Label": stem, "WorkingDirectory": str(root),
                              "EnvironmentVariables": env, "ProgramArguments": list(argv)}), 0o644),
            stem + ".service": ((f"[Unit]\nOnFailure=com.example.job-failed@%n.service\n[Service]\n"
                                 f"WorkingDirectory={root}\n{identity}"
                                 f"ExecStart={admission.unit_systemd_command(argv)}\n").encode(), 0o644),
            stem + ".timer": (f"[Timer]\nUnit={stem}.service\n[Install]\nWantedBy=timers.target\n".encode(), 0o644),
            "com.example.job-failed@.service": ((
                f"[Unit]\nDescription=claudlobby job-failed alert for %i (example)\n[Service]\nType=oneshot\n"
                f"WorkingDirectory={root}\n{identity}"
                f'ExecStart="{release.cli_path}" "--root" "{root}" "--fleet" "example" "fleet" "notify" '
                '"--level" "alert" "--event" "job_failed" "--message" "%i failed"\n').encode(), 0o644),
        }
        builder.effects["units"].extend(job_units(files, destination=timers, scope="fleet",
                                                  release_id=release.release_id, fleet="example"))
        builder.directory(timers)
        for name, (content, mode) in files.items():
            builder.file(timers / name, content, mode=mode)
    plan = builder.seal()
    package = SimpleNamespace(native=release.native_path, artifact_id=release.inputs.artifact_id)
    # The one directory the runtime reads units from; activation refuses any other (#2610).
    native_directory = Path.home() / ".config/systemd/user"
    native_directory.mkdir(parents=True, exist_ok=True)
    host = NativeHost(root, release, plan, native_directory, package)
    host.tmux_dir = tmux_dir
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(admission.RuntimeIdentity, "current", classmethod(lambda cls:
        admission.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(directory / paths.interpreter))
    monkeypatch.setattr(activation, "_probe", host.probe)
    # The scan owner separately proves assembly/commit semantics; this authored
    # output fixture checks ordering and the real operation-context SQL reader.
    monkeypatch.setattr(activation, "_registry_scan", host.registry_scan)
    return root, release, plan, host


@pytest.fixture
def cold(installed, monkeypatch, tmp_path):
    return _cold_case(installed, monkeypatch, tmp_path)


def test_backup_restore_preserves_work_and_activates_without_reviving_owner_access(cold, monkeypatch, tmp_path, capsys):
    from claudlobby.host_backup import create_backup, restore_backup, restored_bootstrap
    from claudlobby.plane.owner_source import bind_source, inspect_source, SourceNeedsBinding
    from claudlobby.plane.ids import read_host_uid
    from tests.test_task_audit import _task

    root, release, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    host_uid = read_host_uid(root / "state")
    with sqlite3.connect(db_file(root)) as conn:
        fleet = conn.execute("SELECT uid FROM identity_registry WHERE kind='fleet'").fetchone()[0]
        _task(conn, "wi_history", fleet=fleet)
        conn.execute("UPDATE work_items SET host_uid=?", (host_uid,))
        identities = conn.execute("SELECT * FROM identity_registry ORDER BY uid").fetchall()
    bind_source(root)
    (root / ".env").write_text("EXAMPLE_SECRET=excluded\n")
    (db_file(root).parent / "owner-access.db").write_bytes(b"excluded owner credentials")
    (root / "runtime/bots/worker/memory").mkdir()
    (root / "runtime/bots/worker/memory/private.md").write_text("separate owner backup needed")
    frozen = (root / "fleet.yaml").read_bytes()
    (root / "fleet.yaml").write_text("unactivated edit must not replace the selected configuration")
    bundle = tmp_path / "backup"
    manifest = create_backup(root, bundle)
    from claudlobby.__main__ import main
    assert main(["host", "backup", "inspect", str(bundle), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["command"] == "host.backup.inspect"
    assert not any(".env" in name or "owner-access" in name or "runtime" in name for name in manifest["files"])
    replacement = tmp_path / "r"  # Keep the existing activation socket under AF_UNIX's path limit.
    replacement.mkdir()
    restored = replacement / "data"
    restore_backup(bundle, restored, replacing_retired_host=True)
    assert (restored / "fleet.yaml").read_bytes() == frozen
    assert not (restored / ".env").exists() and not (restored / "runtime").exists()
    assert not (db_file(restored).parent / "owner-access.db").exists()
    with pytest.raises(SourceNeedsBinding):
        inspect_source(restored)
    assert restored_bootstrap(restored, release)
    # Reinstall the same fixture release at the new root and use the existing
    # activation owner. Only native manager observations are authored here.
    new_home = replacement / "home"  # a replacement host has its own home, and so its own empty unit directory
    new_home.mkdir()
    monkeypatch.setenv("HOME", str(new_home))
    candidate = installed.__wrapped__(replacement)
    new_root, new_release, new_plan, new_host = globals()["cold"].__wrapped__(candidate, monkeypatch, replacement)
    record = activation.bootstrap_activation(new_root, "cold", new_plan.plan_id, new_host.directory, adapter=new_host)
    assert record.status == "active" and read_migration(new_root, "cold")["manifest"]["database"]["initialize_empty"] is False
    assert read_host_uid(new_root / "state") == host_uid
    with sqlite3.connect(db_file(new_root)) as conn:
        assert conn.execute("SELECT title FROM work_items WHERE work_item_id='wi_history'").fetchone() == ("Historical work",)
        assert conn.execute("SELECT * FROM identity_registry ORDER BY uid").fetchall() == identities
    with pytest.raises(SourceNeedsBinding):
        inspect_source(new_root)


def test_backup_refuses_config_drift_tamper_and_existing_restore_target(cold, tmp_path):
    from claudlobby.host_backup import create_backup, inspect_backup, restore_backup
    root, _, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    (root / "library").mkdir()
    with pytest.raises(state.ActivationRefusal, match="configuration changed"):
        create_backup(root, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()
    (root / "library").rmdir()
    bundle = tmp_path / "backup"
    create_backup(root, bundle)
    with pytest.raises(state.ActivationRefusal, match="retired host"):
        restore_backup(bundle, tmp_path / "restored", replacing_retired_host=False)
    with pytest.raises(state.ActivationRefusal, match="new absolute"):
        restore_backup(bundle, root, replacing_retired_host=True)
    (bundle / "configuration/fleet.yaml").write_text("tampered")
    with pytest.raises(state.ActivationRefusal, match="digest"):
        inspect_backup(bundle)


def test_restored_activation_refuses_wrong_release_and_post_restore_writes(cold, tmp_path, capsys):
    from claudlobby.host_backup import create_backup, restore_backup, restored_bootstrap
    root, release, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    bundle, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(root, bundle)
    restore_backup(bundle, restored, replacing_retired_host=True)
    with pytest.raises(state.ActivationRefusal, match="original release"):
        restored_bootstrap(restored, SimpleNamespace(release_id="r-" + "0" * 64))
    with sqlite3.connect(db_file(restored)) as conn:
        conn.execute("CREATE TABLE unexpected_write(value)")
    with pytest.raises(state.ActivationRefusal, match="changed before activation"):
        restored_bootstrap(restored, release)
    from claudlobby.__main__ import main
    (restored / "state/restored-backup.json").write_text("corrupt")
    assert main(["--root", str(restored), "host", "status", "--json"]) == 6
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "unavailable"
    assert result["data"]["restored_backup"] == {"state": "unavailable"}


@pytest.mark.parametrize("linked", ["fleet.yaml", "projects.yaml"])
def test_backup_refuses_recorded_linked_configuration(tmp_path, linked):
    from claudlobby.host_backup import _source_files
    root = tmp_path / "host"
    root.mkdir()
    outside = tmp_path / "external.yaml"
    outside.write_text("fleet: {name: example}\n")
    for name in ("fleet.yaml", "projects.yaml"):
        if name == linked:
            (root / name).symlink_to(outside)
        else:
            (root / name).write_text("{}\n")
    builder = ConfigPlanBuilder(root, "r-" + "0" * 64, "1" * 64, ("example",), effects={})
    builder.effects["fleet_sources"] = {"example": {
        key: {"path": str(root / name), "sha256": builder.input_content(root / name)}
        for key, name in (("fleet", "fleet.yaml"), ("projects", "projects.yaml"))}}
    with pytest.raises(state.ActivationRefusal, match="linked fleet or project"):
        _source_files(builder.seal())


@pytest.mark.parametrize("ready_kind", ["bridge-ready", "session-ready"])
def test_cold_bootstrap_uses_real_sql_config_and_serial_starts_before_timers(cold, ready_kind):
    root, release, plan, host = cold
    host.ready_kind = ready_kind
    record = activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert record.status == "active" and tuple(record.body["completed"]) == state.STEPS
    assert host.starts == ["claudlobby-plane-daemon.service", "com.example.manager.service",
                           "com.example.worker.service", "claudlobby-keepalive.timer"]
    assert host.ready == ["manager", "worker", "manager", "worker"]
    assert host.registry == ["example"]
    assert "claudlobby-keepalive.service" not in host.starts
    assert (host.directory / "claudlobby-keepalive.service").is_file()
    assert {p.name for p in (host.directory / "default.target.wants").iterdir()} == set(host.starts[:3])
    assert {p.name for p in (host.directory / "timers.target.wants").iterdir()} == {host.starts[-1]}
    assert all(value == "enabled loaded active" for value in host.states.values())
    with sqlite3.connect(f"file:{db_file(root)}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == release.compatibility.schema.write
        assert connection.execute("SELECT COUNT(*) FROM registry_snapshots").fetchone()[0] == 3
    journal = read_migration(root, "cold")
    assert journal["manifest"]["database"]["initialize_empty"] is True
    assert Path(journal["backup"]["path"]).is_file()
    assert (root / "runtime/bots/worker/settings.json").read_bytes() == b'{"permissions":{"deny":["Write"]}}\n'
    assert state.read_selection(root)["release_id"] == release.release_id
    bindings = read_selected_identity_bindings(root, "example", package=host.package)
    assert bindings["manager"] == "manager" and bindings["manager_uid"] == bindings["bots"]["manager"]
    assert set(bindings["bots"]) == {"manager", "worker"}
    db_file(root).rename(root / "plane-db-removed")
    assert read_selected_identity_bindings(root, "example", package=host.package) == bindings
    before = list(host.starts)
    with pytest.raises(state.ActivationError, match="existing release selection"):
        activation.bootstrap_activation(root, "again", plan.plan_id, host.directory, adapter=host)
    assert host.starts == before


def test_bootstrap_publishes_the_fleet_failure_template_and_never_starts_or_names_it(
        installed, monkeypatch, tmp_path):
    """#2522: the template is published with the producers and the activation
    completes; only the job's timer starts, and no native call names the
    template, which systemd would refuse."""
    root, _, plan, host = _cold_case(installed, monkeypatch, tmp_path, fleet_job=True)
    name = "com.example.job-failed@.service"
    assert [d.source.name for d, _ in planned_units(plan, "Linux") if "job-failed" in d.source.name] == [name]
    record = activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert record.status == "active" and tuple(record.body["completed"]) == state.STEPS
    assert (host.directory / name).read_bytes() == (root / "runtime/fleet/timers" / name).read_bytes()
    assert set(host.starts[-2:]) == {"claudlobby-keepalive.timer", "com.example.creds-check.timer"}
    assert "com.example.creds-check.service" not in host.starts
    assert not any(name in arg for _, args in host.calls for arg in args)
    assert not list(host.directory.glob(f"*.wants/{name}"))


def test_upgrade_over_an_installed_template_passes_the_inventory_and_candidate_checks(
        installed, monkeypatch, tmp_path):
    """#2522: once an activation has installed the fleet's failure template, the
    next activation's inventory owns it, and the candidate's admission, placement
    and owner-service checks skip it without a native call. A template installed
    by hand whose command runs from under the data root still refuses."""
    root, release, source_plan, host = _cold_case(installed, monkeypatch, tmp_path, fleet_job=True)
    activation.bootstrap_activation(root, "cold", source_plan.plan_id, host.directory, adapter=host)
    name = "com.example.job-failed@.service"
    assert (host.directory / name).is_file()
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("example",),
                                effects={"units": source_plan.effects["units"]})
    for item in source_plan.effects["units"]:
        builder.file(Path(item["source"]), source_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()

    class ReachedRoster(Exception):
        pass

    def roster(*_):
        raise ReachedRoster  # past the inventory and every candidate check that sees units

    monkeypatch.setattr(activation, "_roster", roster)
    calls_before = len(host.calls)
    with pytest.raises(ReachedRoster):
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, host.directory, adapter=host)
    # The inventory reads the template's file owner (a file read, never systemd);
    # no native operation names it.
    assert {function for function, args in host.calls[calls_before:]
            if any(name in arg for arg in args)} == {"svc_bot_unit_owned_by"}
    assert not (root / "state/activations/upgrade").exists()
    (host.directory / "fleet-unit-failed@.service").write_text(
        "[Unit]\nDescription=raise a fleet alert that %i failed\n[Service]\nType=oneshot\n"
        f"ExecStart={root}/runtime/bots/manager/.cli/bin/claudlobby --root {root} fleet notify\n")
    with pytest.raises(InventoryError, match="fleet-unit-failed@.service: owned consumer is absent"):
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, host.directory, adapter=host)
    assert not (root / "state/activations/upgrade").exists()


def test_a_template_needs_no_quiescence_proof_and_hosts_no_caller():
    """#2522: no process runs as a template, so neither the quiescence proof nor
    the external-caller check names one; systemd would refuse the name."""
    calls = []

    class Recording:
        def call(self, function, *args, timeout=30):
            calls.append((function, args))
            return subprocess.CompletedProcess([function, *args], 0, "inactive\tno-cgroup-witness\n", "")

    template = {"target": "com.example.job-failed@.service",
                "installed": [{"path": "/user/com.example.job-failed@.service"}]}
    timer = {"target": "com.example.creds-check.timer",
             "installed": [{"path": "/user/com.example.creds-check.timer"}]}
    activation._legacy_quiet(Recording(), SimpleNamespace(units=lambda phase: (template, timer)), "producers", {})
    activation._assert_external_caller(Recording(), {"producers": (
        {"target": template["target"], "installed": template["installed"][0]["path"]},
        {"target": timer["target"], "installed": timer["installed"][0]["path"]})})
    assert [function for function, _ in calls] == ["svc_activation_quiet", "svc_activation_assert_external"]
    assert not any("job-failed@" in arg for _, args in calls for arg in map(str, args))


def test_require_runtime_directory_names_the_one_directory_for_each_native_manager():
    home = Path.home()
    systemd, agents = home / ".config/systemd/user", home / "Library/LaunchAgents"
    system_agents = Path("/Library/LaunchAgents")
    activation._require_runtime_directory("Linux", systemd, [home / ".config/systemd/user.control", systemd])
    activation._require_runtime_directory("Darwin", agents, [agents, system_agents])
    with pytest.raises(state.ActivationRefusal, match=re.escape(f"must be {agents},")):
        activation._require_runtime_directory("Darwin", system_agents, [agents, system_agents])
    with pytest.raises(state.ActivationRefusal, match="no directory the runtime reads"):
        activation._require_runtime_directory("FreeBSD", systemd, [systemd])


def test_bootstrap_refuses_a_listed_directory_the_runtime_does_not_read_and_the_rerun_succeeds(cold):
    """#2610: systemd lists ~/.config/systemd/user.control first. A unit there loads, but keepalive and
    svc_is_registered read only ~/.config/systemd/user, so activation names that one and changes nothing."""
    root, _, plan, host = cold
    runtime, control = Path.home() / ".config/systemd/user", Path.home() / ".config/systemd/user.control"
    assert host.directory == runtime
    host.search_path = [control, runtime]  # systemd 252's order
    with pytest.raises(state.ActivationRefusal) as refused:
        activation.bootstrap_activation(root, "cold", plan.plan_id, control, adapter=host)
    assert str(runtime) in str(refused.value)
    assert not list((root / "state/activations").glob("*/activation.json"))
    assert not control.exists() and not list(runtime.iterdir())
    assert host.starts == [] and not [name for name, _ in host.calls if name.startswith("svc_activation_")]
    # The CLI tells the operator to rerun the same command; with the directory the refusal names, it works.
    record = activation.bootstrap_activation(root, "cold", plan.plan_id, runtime, adapter=host)
    assert record.status == "active" and tuple(record.body["completed"]) == state.STEPS
    assert (runtime / "claudlobby-keepalive.service").is_file() and not control.exists()


def test_upgrade_refuses_a_listed_directory_the_runtime_does_not_read_and_changes_nothing(cold, monkeypatch):
    root, release, source_plan, host = cold
    runtime, control = Path.home() / ".config/systemd/user", Path.home() / ".config/systemd/user.control"
    activation.bootstrap_activation(root, "cold", source_plan.plan_id, runtime, adapter=host)
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("example",),
                                effects={"units": source_plan.effects["units"]})
    for item in source_plan.effects["units"]:
        builder.file(Path(item["source"]), source_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()
    host.search_path = [control, runtime]

    class ReachedRoster(Exception):
        pass

    def roster(*_):
        raise ReachedRoster  # past the install-directory check

    monkeypatch.setattr(activation, "_roster", roster)
    selection, installed_units, starts = state.read_selection(root), sorted(runtime.iterdir()), list(host.starts)
    calls_before = len(host.calls)
    with pytest.raises(state.ActivationRefusal) as refused:
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, control, adapter=host)
    assert str(runtime) in str(refused.value)
    assert state.read_selection(root) == selection and not (root / "state/activations/upgrade").exists()
    assert not control.exists() and sorted(runtime.iterdir()) == installed_units and host.starts == starts
    assert not [name for name, _ in host.calls[calls_before:] if name.startswith("svc_activation_")]
    # Only the directory differs: the same candidate, given the one the runtime reads, passes the check.
    with pytest.raises(ReachedRoster):
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, runtime, adapter=host)


def test_bootstrap_refuses_when_the_runtime_directory_is_not_on_the_native_search_path(cold, tmp_path):
    """With XDG_CONFIG_HOME set, systemd's whole list moves and ~/.config/systemd/user leaves it."""
    root, _, plan, host = cold
    runtime, xdg = Path.home() / ".config/systemd/user", tmp_path / "xdg-config/systemd"
    host.search_path = [xdg / "user.control", xdg / "user"]
    with pytest.raises(state.ActivationRefusal, match="is not on the native search path") as refused:
        activation.bootstrap_activation(root, "cold", plan.plan_id, runtime, adapter=host)
    assert str(runtime) in str(refused.value)
    assert not list((root / "state/activations").glob("*/activation.json")) and host.starts == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the systemd branch of svc_is_registered")
def test_a_unit_installed_where_the_runtime_reads_is_registered_for_the_real_shell_reader(cold, tmp_path):
    """The shell side of #2610: svc_is_registered, which keepalive and fleet-pulse use, sees every unit
    activation installed, and does not see one in user.control."""
    root, _, plan, host = cold
    runtime, control = Path.home() / ".config/systemd/user", Path.home() / ".config/systemd/user.control"
    activation.bootstrap_activation(root, "cold", plan.plan_id, runtime, adapter=host)
    labels = [unit.name[:-len(".service")] for unit in sorted(runtime.glob("*.service"))]
    assert labels, "activation installed no service unit"
    control.mkdir()
    (control / "com.example.elsewhere.service").write_text("[Service]\n")
    lib = Path(__file__).resolve().parent.parent / "claudlobby/_runtime_scripts"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
           "PLANE_EMIT_DISABLED": "1", "LANG": "C.UTF-8"}
    # lib-common.sh sets -e, so each answer is read in an `if`, not from `$?`.
    script = (f'. "{lib}/lib-common.sh"; bot="$1"; shift; for label in "$@"; do '
              'if svc_is_registered "$bot" "$label"; then echo "$label 0"; else echo "$label 1"; fi; done')
    run = subprocess.run(["bash", "-c", script, "_", str(tmp_path), *labels, "com.example.elsewhere"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    assert dict(line.rsplit(" ", 1) for line in run.stdout.splitlines()) == {
        **{label: "0" for label in labels}, "com.example.elsewhere": "1"}


@pytest.mark.parametrize("same_release", [False, True])
def test_upgrade_binds_applied_selected_plan_as_exact_source(cold, monkeypatch, same_release):
    root, source, source_plan, host = cold
    activation.bootstrap_activation(root, "cold", source_plan.plan_id, host.directory, adapter=host)
    # An active plan's original before-state is stale by construction. Its
    # retained generated unit bytes are the source enrollment authority.
    with pytest.raises(ValueError, match="generated destination changed"):
        source_plan.check_fresh()
    for platform in ("Darwin", "Linux"):
        declaration = next(d for d, _ in planned_units(source_plan, platform)
                           if d.bot == "manager" and d.source.suffix in (".plist", ".service"))
        assert "TMUX_TMPDIR" not in dict(declaration.environment)
        installed = FileSnapshot.read(declaration.source)
        environment = {**dict(declaration.environment), "TMUX_TMPDIR": str(host.tmux_dir)}
        unit = SimpleNamespace(declaration=declaration, installed=(installed,),
                               properties=(("Environment", " ".join(
                                   f"{key}={shlex.quote(value)}" for key, value in environment.items())),))
        assert activation._original_bot_tmpdir(unit) == str(host.tmux_dir)

    if same_release:
        candidate = source
        directory = source.directory
    else:
        inputs = replace(source.inputs, source_revision="c" * 40)
        directory = release_path(root, inputs.release_id)
        shutil.copytree(source.directory, directory)
        (directory / MANIFEST).unlink()
        (directory / source.paths.cli).write_text(f"#!{directory / source.paths.interpreter}\n")
        artifact = directory / source.paths.artifact
        metadata = json.loads(artifact.read_text())
        metadata["source_revision"] = inputs.source_revision
        artifact.write_text(json.dumps(metadata))
        candidate = seal_release(root, inputs, source.paths)
    plan = ConfigPlanBuilder(root, candidate.release_id, candidate.seal_sha256,
                             ("example",), effects={}).seal()
    package = SimpleNamespace(native=candidate.native_path, artifact_id=candidate.inputs.artifact_id)
    host.package = package
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(admission.RuntimeIdentity, "current", classmethod(lambda cls:
        admission.RuntimeIdentity(candidate.cli_path, candidate.native_path, candidate.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(directory / candidate.paths.interpreter))
    observed = []

    class SeenSource(Exception):
        pass

    def enrollment(root_arg, declarations, **kwargs):
        observed.append((root_arg, declarations, kwargs))
        raise SeenSource

    monkeypatch.setattr(activation, "collect_enrollment", enrollment)
    with pytest.raises(SeenSource):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert len(observed) == 1 and observed[0][0] == root
    assert observed[0][2]["legacy_source"] is False
    assert {declaration.release_id for declaration in observed[0][1]} == {source.release_id}
    assert {declaration.source.name for declaration in observed[0][1]} == {
        "claudlobby-plane-daemon.service", "com.example.manager.service",
        "com.example.worker.service", "claudlobby-keepalive.service",
        "claudlobby-keepalive.timer",
    }
    assert state.read_selection(root)["release_id"] == source.release_id
    assert not (root / "state/activations/upgrade").exists()


@pytest.fixture
def schema_upgrade(cold, monkeypatch):
    """A sealed schema-13 source and schema-14 target; native effects stay fake."""
    root, target, _, host = cold
    inputs = replace(target.inputs, source_revision="d" * 40)
    directory = release_path(root, inputs.release_id)
    shutil.copytree(target.directory, directory)
    (directory / MANIFEST).unlink()
    (directory / target.paths.cli).write_text(f"#!{directory / target.paths.interpreter}\n")
    artifact = directory / target.paths.artifact
    metadata = json.loads(artifact.read_text())
    metadata["source_revision"] = inputs.source_revision
    metadata["compatibility"]["schema"] = {"read": [13], "write": 13}
    artifact.write_text(json.dumps(metadata))
    for path in (directory / "package/plane/migrations").glob("*.sql"):
        if int(path.name[:4]) > 13:
            path.unlink()
    source = seal_release(root, inputs, target.paths)
    source_plan = ConfigPlanBuilder(root, source.release_id, source.seal_sha256, (),
                                   effects={"fleet_manifests": {}}).seal()
    plan = ConfigPlanBuilder(root, target.release_id, target.seal_sha256, (),
                            effects={"fleet_manifests": {}}).seal()
    with state.locked_activation(root) as store:
        store.prepare("prior", source_plan, recovery_release_id=source.release_id,
                      enrollment_digest="1" * 64)
        for step in state.STEPS:
            store.begin("prior", step)
            if step == "selection_switched":
                store.select("prior")
            else:
                store.complete("prior", step, evidence_digest="2" * 64)
    _database(root, version=13).close()
    # This coordinator boundary fixture supplies no native estate. It does not
    # model or authorize a zero-bot host setup; native rollout is separate.
    monkeypatch.setattr(activation, "current_declarations", lambda *_args: ())
    monkeypatch.setattr(activation, "planned_units", lambda *_args: ())
    monkeypatch.setattr(activation, "_roster", lambda *_args: ({}, ()))
    monkeypatch.setattr(activation, "_probe", lambda _root: None)
    return root, source, target, plan, host


def test_source_recovery_schema_upgrade_refuses_before_record_or_native_pause(schema_upgrade):
    root, source, target, plan, host = schema_upgrade
    before = db_file(root).read_bytes()
    with pytest.raises(state.ActivationRefusal, match="recovery release cannot read the post-migration.*14"):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert db_file(root).read_bytes() == before
    assert not (root / "state/activations/upgrade").exists()
    assert not any(name.startswith("svc_activation_") for name, _ in host.calls)
    assert state.read_selection(root)["release_id"] == source.release_id


def test_forward_recovery_checks_target_write_floor_before_record(schema_upgrade, monkeypatch):
    root, _, _, plan, host = schema_upgrade
    original = activation.build_migration_manifest
    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        rollback = deepcopy(result.rollback)
        rollback["after_target_write_versions"]["schema"] = 15
        return replace(result, rollback=rollback)
    monkeypatch.setattr(activation, "build_migration_manifest", preview)
    with pytest.raises(state.ActivationRefusal, match="post-target-write.*15"):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory,
                                      adapter=host, forward_recovery=True)
    assert not (root / "state/activations/upgrade").exists()
    assert not any(name.startswith("svc_activation_") for name, _ in host.calls)


def test_source_recovery_checks_target_writes_even_when_sql_is_readable(schema_upgrade, monkeypatch):
    root, _, _, plan, host = schema_upgrade
    original = activation.build_migration_manifest
    def preview(*args, **kwargs):
        result = original(*args, **kwargs)
        rollback = deepcopy(result.rollback)
        rollback["after_sql_versions"]["schema"] = 13
        return replace(result, rollback=rollback)
    monkeypatch.setattr(activation, "build_migration_manifest", preview)
    with pytest.raises(state.ActivationRefusal, match="post-target-write.*14"):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert not (root / "state/activations/upgrade").exists()
    assert not any(name.startswith("svc_activation_") for name, _ in host.calls)


def test_forward_recovery_migrates_on_same_id_resume_and_retains_backup(schema_upgrade, monkeypatch):
    root, source, target, plan, host = schema_upgrade
    manifest = activation.build_migration_manifest(root, source, target)
    activation._check_recovery_readability(manifest, target)
    with state.locked_activation(root) as store:
        store.prepare("upgrade", plan, source_release_id=source.release_id,
                      recovery_release_id=target.release_id, enrollment_digest="1" * 64,
                      install_directory=host.directory, forward_recovery=True)
        for step in state.STEPS[:state.STEPS.index("backup_saved")]:
            store.begin("upgrade", step)
            store.complete("upgrade", step, evidence_digest=(
                manifest.manifest_id[2:] if step == "queues_classified" else "2" * 64))
    class ResumeAdmitted(Exception):
        pass
    # Observe resume's immutable-choice gate before the native pause owner.
    monkeypatch.setattr(activation.units, "load_unit_pause",
                        lambda *_args: (_ for _ in ()).throw(ResumeAdmitted))
    before = state.read_activation(root, "upgrade")
    assert before.body["intent"]["forward_recovery"] is True
    assert before.body["intent"]["recovery_release_id"] == target.release_id
    assert before.body["intent"]["source_release_id"] == source.release_id
    assert before.body["completed"][-1] == "queues_classified"
    calls = list(host.calls)
    with pytest.raises(state.ActivationRefusal, match="cannot change the recorded recovery mode"):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory,
                                     adapter=host, forward_recovery=False)
    assert state.read_activation(root, "upgrade") == before and host.calls == calls
    with pytest.raises(ResumeAdmitted):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert state.read_activation(root, "upgrade") == before and host.calls == calls
    # Stop at the existing startup boundary: no native bot or resident starts.
    monkeypatch.setattr(activation, "_candidate_startup",
                        lambda _root, _store, identifier, *_args, **_kwargs:
                            state.read_activation(_root, identifier))
    with state.locked_activation(root) as store:
        activation.config_install.prepare_config(plan, "upgrade")
        resumed = activation._finish_running_activation(root, store, "upgrade", plan, target, source,
            None, (), (), {}, {}, (), (), {}, host.directory, host, host.package, legacy_source=False)
    assert resumed.body["intent"] == before.body["intent"]
    assert resumed.body["completed"][-3:] == ["migration_applied", "selection_switched", "configuration_applied"]
    journal = read_migration(root, "upgrade")
    assert journal["backup"]["user_version"] == 13
    assert Path(journal["backup"]["path"]).is_file()
    with sqlite3.connect(db_file(root)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
    selected = state.read_selection(root)
    assert selected["release_id"] == target.release_id
    with state.locked_activation(root) as store:
        with pytest.raises(state.ActivationError, match="old-source rollback is unsupported"):
            store.begin_rollback("upgrade")
        # The separate selection primitive still refuses the old configuration,
        # even if a caller has already entered a rollback journal by other means.
        refused = state.read_activation(root, "upgrade")
        refused.body.update(status="rolling_back", pending="selection_restored")
        store._save(refused)
        with pytest.raises(state.ActivationError, match="matching configuration; use forward repair"):
            store.restore_selection("upgrade")
    assert state.read_selection(root) == selected


@pytest.mark.parametrize("change", ["enable", "recovery-target"])
def test_resume_refuses_changed_recovery_before_any_native_call(schema_upgrade, monkeypatch, change):
    root, source, target, plan, host = schema_upgrade
    with state.locked_activation(root) as store:
        store.prepare("upgrade", plan, source_release_id=source.release_id,
                      recovery_release_id=target.release_id, enrollment_digest="1" * 64,
                      install_directory=host.directory, forward_recovery=True)
        record = state.read_activation(root, "upgrade")
        if change == "enable":
            record.body["intent"].pop("forward_recovery")
            record.body["intent"]["recovery_release_id"] = source.release_id
        else:
            record.body["intent"]["recovery_release_id"] = source.release_id
        store._save(record)
    before = state.read_activation(root, "upgrade")
    calls = list(host.calls)
    with pytest.raises(state.ActivationRefusal, match="recorded recovery"):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory,
                                     adapter=host, forward_recovery=True)
    assert state.read_activation(root, "upgrade") == before and host.calls == calls


def _forward_record_at_backup(root, source, target, plan, host, manifest_id="2" * 64):
    with state.locked_activation(root) as store:
        store.prepare("upgrade", plan, source_release_id=source.release_id,
                      recovery_release_id=target.release_id, enrollment_digest="1" * 64,
                      install_directory=host.directory, forward_recovery=True)
        for step in state.STEPS[:state.STEPS.index("backup_saved")]:
            store.begin("upgrade", step)
            store.complete("upgrade", step, evidence_digest=(
                manifest_id if step == "queues_classified" else "2" * 64))


@pytest.mark.parametrize("change", ["no-previous", "legacy-source", "other-source"])
def test_resume_refuses_forward_record_off_its_selected_source_before_any_native_call(
        schema_upgrade, monkeypatch, change):
    root, source, target, plan, host = schema_upgrade
    _forward_record_at_backup(root, source, target, plan, host)
    with state.locked_activation(root) as store:
        record = state.read_activation(root, "upgrade")
        if change == "no-previous":
            record.body["previous_selection"] = None
        elif change == "legacy-source":
            record.body["intent"]["source_kind"] = "legacy-unsealed"
        else:
            record.body["intent"]["source_release_id"] = target.release_id
        before = store._save(record)
    class ResumeAdmitted(Exception):
        pass
    # The pause owner is resume's first native step: reaching it admits the record.
    monkeypatch.setattr(activation.units, "load_unit_pause",
                        lambda *_args: (_ for _ in ()).throw(ResumeAdmitted))
    calls = list(host.calls)
    with pytest.raises(state.ActivationRefusal, match="recorded recovery release differs from its frozen recovery mode"):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert state.read_activation(root, "upgrade") == before and host.calls == calls


def test_resume_rechecks_recovery_readability_before_any_native_call(schema_upgrade, monkeypatch):
    root, source, target, plan, host = schema_upgrade
    _forward_record_at_backup(root, source, target, plan, host)
    class ResumeAdmitted(Exception):
        pass
    # This fixture has no native estate: the pause owner returns an empty Linux
    # enrollment, and the native quiet check that follows the preview raises.
    frozen = {"data_root": str(root), "legacy_source": False, "manager": "Linux", "units": [],
              "catalog": f"manager\tLinux\ndirectory\t{host.directory}\n"}
    monkeypatch.setattr(activation.units, "load_unit_pause", lambda *_args: SimpleNamespace(enrollment=frozen))
    monkeypatch.setattr(activation, "_legacy_quiet",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ResumeAdmitted))
    before, calls, data = state.read_activation(root, "upgrade"), list(host.calls), db_file(root).read_bytes()
    with pytest.raises(ResumeAdmitted):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    original = activation.build_migration_manifest
    def unreadable(*args, **kwargs):
        result = original(*args, **kwargs)
        rollback = deepcopy(result.rollback)
        rollback["after_target_write_versions"]["schema"] = 15
        return replace(result, rollback=rollback)
    monkeypatch.setattr(activation, "build_migration_manifest", unreadable)
    with pytest.raises(state.ActivationRefusal, match="post-target-write.*15"):
        activation.resume_activation(root, "upgrade", plan.plan_id, host.directory, adapter=host)
    assert state.read_activation(root, "upgrade") == before and host.calls == calls
    assert db_file(root).read_bytes() == data and read_migration(root, "upgrade") is None


def _same_compatibility_release(root, release, revision):
    """A second sealed release that reads and writes what RELEASE does."""
    inputs = replace(release.inputs, source_revision=revision)
    directory = release_path(root, inputs.release_id)
    shutil.copytree(release.directory, directory)
    (directory / MANIFEST).unlink()
    (directory / release.paths.cli).write_text(f"#!{directory / release.paths.interpreter}\n")
    artifact = directory / release.paths.artifact
    metadata = json.loads(artifact.read_text())
    metadata["source_revision"] = inputs.source_revision
    artifact.write_text(json.dumps(metadata))
    return seal_release(root, inputs, release.paths)


@pytest.mark.parametrize("change", ["not-boolean", "no-previous", "legacy-source", "other-recovery"])
def test_apply_refuses_forward_record_off_its_exact_target_before_backup(schema_upgrade, change):
    root, source, target, plan, host = schema_upgrade
    manifest = activation.build_migration_manifest(root, None if change == "legacy-source" else source, target)
    other = _same_compatibility_release(root, target, "e" * 40) if change == "other-recovery" else None
    _forward_record_at_backup(root, source, target, plan, host, manifest.manifest_id[2:])
    with state.locked_activation(root) as store:
        record = state.read_activation(root, "upgrade")
        intent = record.body["intent"]
        if change == "not-boolean":
            intent["forward_recovery"] = 1
        elif change == "no-previous":
            record.body["previous_selection"] = None
        elif change == "legacy-source":
            intent.update(source_kind="legacy-unsealed", source_release_id=None)
        else:
            intent["recovery_release_id"] = other.release_id
        before, data = store._save(record), db_file(root).read_bytes()
        with pytest.raises(MigrationApplyError, match="forward recovery differs from the exact selected upgrade target"):
            apply_migration(store, "upgrade", manifest)
    assert state.read_activation(root, "upgrade") == before
    assert db_file(root).read_bytes() == data and read_migration(root, "upgrade") is None


def test_forward_recovery_refuses_legacy_source_before_any_native_call(cold, monkeypatch):
    root, _, plan, host = cold
    _adopt_legacy(root, plan, host, monkeypatch, _legacy_handoff_estate(root, ("manager", "worker")))
    # No public door passes both; the running owner still refuses first.
    with pytest.raises(state.ActivationRefusal, match="supported only for selected release upgrades"):
        activation._running_activation(root, "cutover", plan.plan_id, host.directory,
                                       legacy_source=True, adapter=host, forward_recovery=True)
    assert not (root / "state/activations/cutover").exists() and host.calls == []


@pytest.mark.parametrize("choice", [1, "true"])
def test_forward_recovery_refuses_non_boolean_choice_before_any_native_call(schema_upgrade, choice):
    root, source, _, plan, host = schema_upgrade
    calls = list(host.calls)
    with pytest.raises(state.ActivationRefusal, match="supported only for selected release upgrades"):
        activation.upgrade_activation(root, "upgrade", plan.plan_id, host.directory,
                                      adapter=host, forward_recovery=choice)
    assert not (root / "state/activations/upgrade").exists() and host.calls == calls
    assert state.read_selection(root)["release_id"] == source.release_id


def test_upgrade_refuses_candidate_persistent_disabled_override_before_pause(cold, monkeypatch):
    root, release, source_plan, host = cold
    activation.bootstrap_activation(root, "cold", source_plan.plan_id, host.directory, adapter=host)
    item = next(item for item in source_plan.effects["units"]
                if Path(item["source"]).name == "claudlobby-plane-daemon.plist")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("example",), effects={"units": [item]})
    builder.file(Path(item["source"]), source_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()
    agents = Path.home() / "Library/LaunchAgents"  # the one directory the runtime reads on macOS (#2610)
    agents.mkdir(parents=True)
    catalog = (f"manager\tDarwin\ndomain\tgui/{os.getuid()}\ndirectory\t{agents}\n"
               "PID\tStatus\tLabel\n")
    target = f"gui/{os.getuid()}/claudlobby-plane-daemon"
    original_call = host.call

    def call(function, *args, timeout=30):
        if function == "svc_inventory_catalog":
            return subprocess.CompletedProcess([function], 0, catalog, "")
        if function == "svc_inventory_disabled":
            assert args == (f"gui/{os.getuid()}",)
            return subprocess.CompletedProcess([function], 0,
                '\n\tdisabled services = {\n\t\t"claudlobby-plane-daemon" => disabled\n\t}\n', "")
        return original_call(function, *args, timeout=timeout)

    monkeypatch.setattr(host, "call", call)
    inventory = SimpleNamespace(manager="Darwin", catalog=catalog, units=())
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(activation, "collect_enrollment", lambda *_, **__: inventory)
    before = state.read_selection(root)
    calls_before = len(host.calls)
    with pytest.raises(state.ActivationError, match="persistent disabled override") as failure:
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, agents, adapter=host)
    assert target in str(failure.value)
    assert isinstance(failure.value, activation.CandidateDisabledOverride)
    assert state.read_selection(root) == before
    assert not (root / "state/activations/upgrade").exists()
    assert not any(name == "svc_activation_pause" for name, _ in host.calls[calls_before:])


def test_same_release_upgrade_refuses_foreign_candidate_file_before_pause(cold, monkeypatch):
    root, release, selected_plan, host = cold
    activation.bootstrap_activation(root, "cold", selected_plan.plan_id, host.directory, adapter=host)
    item = next(item for item in selected_plan.effects["units"]
                if Path(item["source"]).name == "claudlobby-plane-daemon.plist")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("example",), effects={"units": [item]})
    builder.file(Path(item["source"]), selected_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()
    agents = Path.home() / "Library/LaunchAgents"  # the one directory the runtime reads on macOS (#2610)
    agents.mkdir(parents=True)
    foreign = agents / "claudlobby-plane-daemon.plist"
    foreign.write_bytes(b"unrelated installed LaunchAgent")
    original = foreign.read_bytes()
    catalog = (f"manager\tDarwin\ndomain\tgui/{os.getuid()}\ndirectory\t{agents}\n"
               f"installed\t{foreign.name}\nPID\tStatus\tLabel\n")
    original_call = host.call

    def call(function, *args, timeout=30):
        if function == "svc_inventory_catalog":
            return subprocess.CompletedProcess([function], 0, catalog, "")
        if function == "svc_inventory_disabled":
            return subprocess.CompletedProcess([function], 0, "\tdisabled services = {\n\t}\n", "")
        return original_call(function, *args, timeout=timeout)

    monkeypatch.setattr(host, "call", call)
    inventory = SimpleNamespace(manager="Darwin", catalog=catalog, units=())
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(activation, "collect_enrollment", lambda *_, **__: inventory)
    selection = state.read_selection(root)
    calls_before = len(host.calls)
    with pytest.raises(state.ActivationError, match="foreign candidate collision before activation"):
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, agents, adapter=host)
    assert state.read_selection(root) == selection
    assert not (root / "state/activations/upgrade").exists()
    assert foreign.read_bytes() == original
    assert not any(name == "svc_activation_pause" for name, _ in host.calls[calls_before:])


def test_upgrade_handoff_roster_uses_frozen_selected_bots_after_authoring_change(cold):
    root, _, selected_plan, host = cold
    manifest = root / "fleet.yaml"
    manifest.write_text("fleet:\n  name: example\n  manager: manager\n"
                        "  service_prefix: com.example\n  bots:\n"
                        "    manager: {expertise: [orchestration]}\n")
    old_dirs = {("example", bot): root / "runtime/bots" / bot for bot in ("manager", "worker")}
    assert activation._source_handoff_roster(selected_plan, old_dirs, host.package) == {
        "example": ("manager", ("worker", "manager"))
    }


@pytest.mark.parametrize("installed_worker", [False, True])
def test_preserved_stopped_handoff_owners_keep_canonical_work_mapping(tmp_path, monkeypatch, installed_worker):
    from dataclasses import asdict
    from claudlobby import active_config
    from claudlobby.activation_handoffs import preflight_canonical_handoffs, persist_canonical_handoffs
    from claudlobby.task_audit import audit_tasks
    from tests.test_activation_handoffs import _fixture, _tree

    conn, expected_roster, directories, _, worker_handoff = _fixture(tmp_path)
    contexts = tuple(SimpleNamespace(fleet=SimpleNamespace(name=fleet, manager=manager,
        bots=dict.fromkeys(bots))) for fleet, (manager, bots) in expected_roster.items())
    source = SimpleNamespace(fleets=tuple(expected_roster))
    monkeypatch.setattr(active_config, "context_from_plan", lambda _, fleet, **kw:
                        next(context for context in contexts if context.fleet.name == fleet))
    units, witnesses = [], {}
    for (fleet, bot), directory in directories.items():
        declaration = SimpleNamespace(scope="bot", fleet=fleet, bot=bot,
            source=directory / f"{fleet}.{bot}.service", working_directory=directory)
        target = declaration.source.name
        installed = installed_worker and bot == "worker"
        units.append(SimpleNamespace(declaration=declaration, target=target,
                                     installed=(object(),) if installed else ()))
        if not installed:
            witnesses[target] = {"disposition": "stopped", "session": "server_absent",
                "source": str(declaration.source), "working_directory": str(directory),
                "installed": False, "native_active": "inactive"}
    try:
        roster, dirs, retained = activation._handoff_inputs(
            units, source, contexts, None, bot_states=witnesses)
        assert roster == expected_roster and dirs == directories
        before = _tree(tmp_path)
        preflight_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs, candidate_bots=retained)
        assert _tree(tmp_path) == before
        result = persist_canonical_handoffs(tmp_path, roster=roster, bot_dirs=dirs,
            candidate_bots=retained, expected_audit=asdict(audit_tasks(conn)))
        assert result["bots"] == len(directories) and result["active_assignments"] == 1
        assert b"assigned_to_this_bot" in worker_handoff.read_bytes()
    finally:
        conn.close()


@pytest.mark.parametrize("field,value", [
    ("disposition", "restart"), ("session", "unknown"), ("source", "/foreign.service"),
    ("working_directory", "/foreign"), ("installed", True), ("native_active", "active"),
])
def test_preserved_stopped_handoff_owner_requires_exact_witness(tmp_path, field, value):
    declaration = SimpleNamespace(scope="bot", fleet="example", bot="manager",
        source=tmp_path / "manager.service", working_directory=tmp_path / "manager")
    unit = SimpleNamespace(declaration=declaration, target="manager.service", installed=())
    witness = {"disposition": "stopped", "session": "server_absent",
        "source": str(declaration.source), "working_directory": str(declaration.working_directory),
        "installed": False, "native_active": "inactive"}
    witness[field] = value
    with pytest.raises(state.ActivationRefusal, match="exact preserved witness"):
        activation._handoff_inputs((unit,), object(), (), None,
                                   bot_states={unit.target: witness})
    # No preserve witness leaves the default stopped-owner refusal intact.
    assert activation._handoff_inputs((unit,), None, (), None) == ({}, {}, set())


def test_upgrade_parks_source_only_bot_by_selected_phase(cold):
    root, release, selected_plan, _ = cold
    retained = [item for item in selected_plan.effects["units"]
                if item["bot"] != "worker"]
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("example",), effects={"units": retained})
    for item in retained:
        builder.file(Path(item["source"]), selected_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()
    old = [SimpleNamespace(target=declaration.source.name, installed=(object(),))
           for declaration, item in planned_units(selected_plan, "Linux") if item["enroll"]]
    inventory = SimpleNamespace(manager="Linux", catalog="manager\tLinux\ndirectory\t/fixture\n", units=old)
    phases = activation._legacy_phase_membership(candidate, inventory, selected_plan)
    assert "com.example.worker.service" in phases["bots"]
    assert "com.example.manager.service" in phases["bots"]


@pytest.mark.parametrize("failure, pending", [("ingest", "ingest_started"), ("worker", "bots_started"),
                                             ("enablement", "ingest_started"), ("off", "bots_started"),
                                             ("incomplete", "bots_started"), ("uncommitted", "bots_started"),
                                             ("foreign-host", "bots_started")])
def test_failed_readiness_keeps_candidate_pending_and_never_starts_producers(cold, failure, pending):
    root, _, plan, host = cold
    host.bad_probe = failure == "ingest"
    host.bad_enablement = failure == "enablement"
    host.fail_bot = "worker" if failure == "worker" else None
    host.registry_failure = failure
    with pytest.raises(state.ActivationError) as failure_info:
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    if failure == "foreign-host":
        assert "identity binding failed" in str(failure_info.value)
        assert "foreign host registry binding" in str(failure_info.value.__cause__)
        assert not (host.directory / "com.example.manager.service").exists()
    record = state.read_activation(root, "cold")
    assert record.status == "activating" and record.body["pending"] == pending
    if failure == "worker":
        assert "identity_bindings" in record.body
        replacement = deepcopy(record.body["identity_bindings"])
        bot_ids = replacement["fleets"]["example"]["bots"]
        bot_ids["worker"] = next("actor_" + digit * 32 for digit in "012"
                                 if "actor_" + digit * 32 not in bot_ids.values())
        with state.locked_activation(root) as store:
            with pytest.raises(state.ActivationError, match="already recorded"):
                store.record_identity_bindings("cold", replacement, package=host.package)
    elif pending == "bots_started":
        assert "identity_bindings" not in record.body
    assert "producers_resumed" not in record.body["completed"]
    assert not (host.directory / "claudlobby-keepalive.timer").exists()
    assert read_migration(root, "cold")["result"] is not None
    if failure != "worker":
        assert host.starts == ["claudlobby-plane-daemon.service"]
    else:
        assert host.starts == ["claudlobby-plane-daemon.service", "com.example.manager.service", "com.example.worker.service"]


@pytest.mark.parametrize("failing", ["ingest", "worker"])
def test_bootstrap_resume_reconciles_started_unit_without_native_resend(cold, failing):
    root, _, plan, host = cold
    host.bad_probe = failing == "ingest"
    host.fail_bot = "worker" if failing == "worker" else None
    with pytest.raises(state.ActivationError):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    before = list(host.starts)
    pending = state.read_activation(root, "cold")
    assert pending.body["pending"] == ("ingest_started" if failing == "ingest" else "bots_started")
    assert any(effect["result"] is None for effect in pending.body["start_effects"].values())
    with pytest.raises(state.ActivationError):
        activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert host.starts == before  # An unready issued bot is never sent a second start.
    host.bad_probe = False
    host.fail_bot = None
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active"
    assert host.starts == ["claudlobby-plane-daemon.service", "com.example.manager.service",
                           "com.example.worker.service", "claudlobby-keepalive.timer"]
    assert tuple(resumed.body["completed"]) == state.STEPS


_WORKER_UNIT = "com.example.worker.service"


def _failed_worker_start(cold):
    root, _, plan, host = cold
    host.fail_bot = "worker"
    with pytest.raises(state.ActivationError):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    record = state.read_activation(root, "cold")
    assert record.body["pending"] == "bots_started"
    return record


@pytest.mark.parametrize("case", ["active", "unknown", "fleet", "bot", "succeeded", "reason",
                                  "multiline", "hosted", "ancestry-unknown", "missing", "locked"])
def test_start_repair_refuses_live_unknown_wrong_identity_and_recorded_success(cold, case):
    root, _, _, host = cold
    before = _failed_worker_start(cold).body
    host.quiet_unknown = case == "unknown"
    if case in ("hosted", "ancestry-unknown"):
        host.dead.add(_WORKER_UNIT)  # Otherwise repairable: only native ancestry refuses.
        host.external_result = 1 if case == "hosted" else 3
    fleet = "other" if case == "fleet" else "example"
    bot = "manager" if case == "succeeded" else "nobody" if case == "bot" else "worker"
    starts = list(host.starts)
    reason = " " if case == "reason" else "exited\nbefore bridge" if case == "multiline" else "exited"
    message = {"active": "unit is not verified inactive", "locked": "holds the lock",
               "missing": "could not verify the recorded activation"}.get(case)
    with state.locked_activation(root) if case == "locked" else nullcontext():
        with pytest.raises(state.ActivationRefusal, match=message):
            activation.repair_failed_bot_start(root, "absent" if case == "missing" else "cold",
                                               fleet=fleet, bot=bot, reason=reason, adapter=host)
    assert state.read_activation(root, "cold").body == before
    assert host.starts == starts


def test_start_repair_accepts_stale_refused_private_socket(cold):
    root, _, _, host = cold
    _failed_worker_start(cold)
    host.dead.add(_WORKER_UNIT)
    stale = host.tmux_dir / f"tmux-{os.getuid()}" / "com.example.worker"
    if len(str(stale)) > 100:
        pytest.skip("AF_UNIX path limit; point CLAUDLOBBY_TEST_TMPDIR at a shorter private directory")
    stale.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(stale))  # Bound, never listening: an unclean tmux exit's leftover.
    repaired = activation.repair_failed_bot_start(root, "cold", fleet="example", bot="worker",
                                                  reason="exited", adapter=host)
    assert repaired.body["start_repairs"][0]["dead_evidence"]["details"]["socket_state"] == "refused"
    assert stale.exists()  # Evidence is observed, never removed.


def test_legacy_adoption_resume_after_start_repair_reproves_old_bot_quiet(enrollment, monkeypatch):
    inventory, phases, plan, adapter, _, _ = enrollment
    # First adoption: unsealed original declarations, no prior selection or active record.
    inventory = replace(inventory, legacy_source=True, units=tuple(
        replace(unit, declaration=replace(unit.declaration, release_id="")) for unit in inventory.units))
    root = inventory.data_root
    directory = Path(inventory.catalog.split("directory\t", 1)[1].splitlines()[0])
    release = state.read_release(root, plan.release_id)
    package = type("Package", (), {"native": release.native_path, "artifact_id": release.inputs.artifact_id})()
    adapter.package = package
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(activation.RuntimeIdentity, "current", classmethod(lambda cls:
        activation.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(release.directory / release.paths.interpreter))
    bot = next(unit for unit in inventory.units if unit.declaration.scope == "bot")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("alpha",), effects={})
    builder.file(Path(bot.generated.path), bot.generated.content, mode=bot.generated.mode)
    candidate_plan = builder.seal()
    item = {"enroll": True, "phase": "bots", "sha256": bot.generated.sha256}
    monkeypatch.setattr(activation, "planned_units", lambda _plan, _manager: ((bot.declaration, item),))
    monkeypatch.setattr(activation, "validate_unit_admission", lambda *_args: object())
    monkeypatch.setattr(activation, "_roster", lambda *_args: ({}, ()))
    # This native-start witness fixture records migration as complete without
    # creating a database; its readability proof belongs to migration tests.
    monkeypatch.setattr(activation, "build_migration_manifest", lambda *_args:
                        SimpleNamespace(readability_blockers=lambda *_args, **_kwargs: ()))
    fence = {"ceiling": 210, "fence": "RR_FENCE_member"}
    with state.locked_activation(root) as store:
        assert state.read_selection(root) is None
        _prepare_pause(store, inventory, phases, candidate_plan, adapter, install_directory=directory)
        intent = state.read_activation(root, "cutover").body["intent"]
        assert intent["source_kind"] == "legacy-unsealed" and intent["source_release_id"] is None
        _pause_all(store, adapter)
        for step in ("backup_saved", "migration_applied", "selection_switched",
                     "configuration_applied", "ingest_started"):
            store.begin("cutover", step)
            if step == "selection_switched":
                store.select("cutover")
            else:
                store.complete("cutover", step, evidence_digest="a" * 64)
        store.begin("cutover", "bots_started")
        store.record_start_phase("cutover", phase="bots", publication_digest="b" * 64, registry=[])
        store.record_start_intent("cutover", phase="bots", source=str(bot.declaration.source),
                                  target=bot.target, sha256=bot.generated.sha256, fence=fence)
        # The journal owner's archive; the old bot is no longer exempt as a started candidate.
        archived = store.archive_failed_bot_start(
            "cutover", source=str(bot.declaration.source), target=bot.target,
            sha256=bot.generated.sha256, fence=fence, evidence={"details": {}, "digest": "c" * 64},
            reason="exited before bridge", repair_artifact=release.inputs.artifact_id).body["start_repairs"]
    accepting, quiet = [True], []
    monkeypatch.setattr(activation, "_legacy_bot_socket",
                        lambda *_args, **_kwargs: (root / "old.sock", accepting[0]))
    monkeypatch.setattr(activation, "assert_quiescent",
                        lambda _adapter, **kwargs: quiet.append(kwargs["target"]))
    monkeypatch.setattr(activation, "_finish_running_activation",
                        lambda *_args, **_kwargs: state.read_activation(root, "cutover"))
    calls_before = list(adapter.calls)
    with pytest.raises(state.ActivationError, match="old private bot server is present"):
        activation.resume_activation(root, "cutover", candidate_plan.plan_id, directory, adapter=adapter)
    accepting[0] = False
    resumed = activation.resume_activation(root, "cutover", candidate_plan.plan_id, directory, adapter=adapter)
    assert resumed.body["start_repairs"] == archived and resumed.body["start_effects"] == {}
    assert bot.target in quiet  # The repaired bot's old unit is proved quiet again.
    assert adapter.calls == calls_before  # No native start or handoff replay.


@pytest.mark.parametrize("prepared_owner", [False, True])
def test_start_repair_archives_dead_attempt_then_resume_starts_it_once(cold, monkeypatch, prepared_owner):
    root, _, plan, host = cold
    if prepared_owner:
        from claudlobby.plane.owner_access import OwnerAccess
        ensure_host_uid(root / "state")
        OwnerAccess.initialize(root).prepare_first_fleet()
    failed = _failed_worker_start(cold).body
    source, attempt = next((s, e) for s, e in failed["start_effects"].items() if e["target"] == _WORKER_UNIT)
    others = {s: e for s, e in failed["start_effects"].items() if s != source}
    host.dead.add(_WORKER_UNIT)
    starts = list(host.starts)
    repaired = activation.repair_failed_bot_start(root, "cold", fleet="example", bot="worker",
                                                  reason="exited before bridge", adapter=host)
    assert host.starts == starts  # The repair itself starts nothing.
    archived = repaired.body["start_repairs"]
    assert len(archived) == 1 and archived[0]["attempt"] == attempt and archived[0]["source"] == source
    assert archived[0]["dead_evidence"]["details"]["native"] == "inactive\tno-cgroup-witness"
    assert archived[0]["dead_evidence"]["details"]["socket_state"] == "absent"
    assert Path(archived[0]["dead_evidence"]["details"]["socket_path"]).parent.parent == host.tmux_dir
    assert repaired.body["start_effects"] == others
    assert {k: v for k, v in repaired.body.items() if k not in ("start_effects", "start_repairs")} == {
        k: v for k, v in failed.items() if k != "start_effects"}
    with pytest.raises(state.ActivationRefusal):  # No intent left to repair until resume fences one.
        activation.repair_failed_bot_start(root, "cold", fleet="example", bot="worker",
                                           reason="again", adapter=host)
    host.fail_bot = None
    if prepared_owner:
        from claudlobby.plane import owner_source
        assert not OwnerAccess(root).first_fleet_pending()
        monkeypatch.setattr(owner_source, "bind_source", lambda root: pytest.fail("resume must not attest running recorder"))
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active"
    assert host.starts == starts + [_WORKER_UNIT, "claudlobby-keepalive.timer"]
    assert resumed.body["start_repairs"] == archived
    assert resumed.body["start_effects"][source]["result"] is not None
    assert all(resumed.body["start_effects"][s] == e for s, e in others.items() if e["phase"] != "producers")


def test_start_repair_interruption_keeps_history(cold, monkeypatch):
    root, _, plan, host = cold
    failed = _failed_worker_start(cold).body
    host.dead.add(_WORKER_UNIT)
    original_write = state._write

    def interrupted(path, value):
        raise OSError("interrupted before replace")

    monkeypatch.setattr(state, "_write", interrupted)
    with pytest.raises(OSError):
        activation.repair_failed_bot_start(root, "cold", fleet="example", bot="worker",
                                           reason="exited", adapter=host)
    assert state.read_activation(root, "cold").body == failed
    monkeypatch.setattr(state, "_write", original_write)
    archived = activation.repair_failed_bot_start(root, "cold", fleet="example", bot="worker",
                                                  reason="exited", adapter=host).body["start_repairs"]
    # The fresh start fails too: the archive survives beside the new unresolved intent.
    with pytest.raises(state.ActivationError):
        activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    record = state.read_activation(root, "cold").body
    assert record["start_repairs"] == archived and record["pending"] == "bots_started"
    fresh = [e for e in record["start_effects"].values() if e["target"] == _WORKER_UNIT]
    assert len(fresh) == 1 and fresh[0]["result"] is None


_PLANE_UNIT = "claudlobby-plane-daemon.service"
_MANAGER_UNIT = "com.example.manager.service"
_KEEPALIVE_TIMER = "claudlobby-keepalive.timer"


def _after_reset(host, monkeypatch, targets):
    """What a reset leaves once the operator stops and resets each candidate:
    loaded but inactive, and the plane socket down."""
    for target in targets:
        host.states[target] = "enabled loaded inactive"
    monkeypatch.setattr(activation, "_plane_state", lambda _root: "down")


def _host_reset(cold, monkeypatch):
    """A watchdog reset mid bots_started: every started candidate process is gone.

    The plane daemon and the manager started with results, the worker's start
    has none, and afterwards nothing runs and the plane socket is down.
    """
    stranded = _failed_worker_start(cold).body
    assert {e["target"]: e["result"] is not None for e in stranded["start_effects"].values()} == {
        _PLANE_UNIT: True, _MANAGER_UNIT: True, _WORKER_UNIT: False}
    _after_reset(cold[3], monkeypatch, (_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT))
    return stranded


def _rewrite(root, change):
    """Edit the record body in place, as a corrupt or partial record would read."""
    with state.locked_activation(root) as store:
        record = state.read_activation(root, "cold")
        change(store, record.body)
        store._save(record)
    return state.read_activation(root, "cold").body


def test_host_reset_repair_archives_every_dead_start_then_resume_starts_them_once(cold, monkeypatch):
    root, _, plan, host = cold
    stranded = _host_reset(cold, monkeypatch)
    starts = list(host.starts)
    repaired = activation.repair_starts_after_host_reset(root, "cold", reason="watchdog reset", adapter=host)
    assert host.starts == starts  # The repair itself starts nothing.
    assert repaired.archived_targets == tuple(sorted([_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT]))
    body = repaired.record.body
    assert body["pending"] == "ingest_started" and body["start_effects"] == {}
    assert body["completed"] == list(state.STEPS[:state.STEPS.index("ingest_started")])
    assert not {"ingest_started", "bots_started"} & set(body["evidence"])
    archived = body["start_repairs"]
    assert sorted(r["attempt"]["target"] for r in archived) == list(repaired.archived_targets)
    assert all(r["kind"] == "host_reset" and r["reason"] == "watchdog reset" for r in archived)
    assert all(r["host_boot"] == activation._host_boot() for r in archived)
    assert all(r["attempt"] == stranded["start_effects"][r["source"]] for r in archived)
    assert all(r["dead_evidence"]["details"]["native"] == "inactive\tno-cgroup-witness" for r in archived)
    bots = [r for r in archived if r["attempt"]["phase"] == "bots"]
    assert all(r["dead_evidence"]["details"]["socket_state"] == "absent" for r in bots)
    # Everything else the resume needs survives: selection, publication, identity.
    kept = ("intent", "previous_selection", "start_phases", "identity_bindings")
    assert all(body[key] == stranded[key] for key in kept)
    with pytest.raises(state.ActivationRefusal, match="no candidate start is recorded"):
        activation.repair_starts_after_host_reset(root, "cold", reason="again", adapter=host)
    host.fail_bot = None
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active" and tuple(resumed.body["completed"]) == state.STEPS
    assert host.starts == starts + [_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT, _KEEPALIVE_TIMER]
    assert resumed.body["start_repairs"] == archived
    assert all(e["result"] is not None for e in resumed.body["start_effects"].values())


def _stray_fence(_store, body):
    effects = body["start_effects"]
    plane = next(source for source, effect in effects.items() if effect["phase"] == "ingest")
    effects[plane]["fence"] = {"ceiling": 210, "fence": "RR_FENCE_stray"}


def _oneshot_producer(store, body):
    service = next(entry for entry in activation.enrollment.candidate_entries(store, "cold", "producers")
                   if entry["target"] == "claudlobby-keepalive.service")
    body["start_effects"][service["source"]] = {"phase": "producers", "target": service["target"],
                                                "sha256": service["after"]["sha256"], "fence": None,
                                                "result": None}


def _unrecorded_worker(_store, body):
    # The reset landed before the worker's start intent was durable.
    effects = body["start_effects"]
    del effects[next(s for s, effect in effects.items() if effect["target"] == _WORKER_UNIT)]


@pytest.mark.parametrize("case, message", [
    ("serving", "plane socket is serving, not down"),
    ("hung", "plane socket is unanswered, not down"),
    ("live-bot", f"not quiet.*not quiescent: {_MANAGER_UNIT}"),
    ("live-plane", f"not quiet.*not quiescent: {_PLANE_UNIT}"),
    ("two-live", f"not quiescent: {_MANAGER_UNIT}; candidate target is not quiescent: {_PLANE_UNIT}"),
    ("unrecorded", f"not quiet.*not quiescent: {_WORKER_UNIT}"),
    ("hosted", "hosted or cannot be proved external"),
    ("reason", "short reason on one printable line"),
    ("locked", "holds the lock"),
    ("stray-fence", f"recorded start of {_PLANE_UNIT} differs from frozen publication"),
    ("oneshot", "one-shot producer start of claudlobby-keepalive.service has an unknown effect"),
])
def test_host_reset_repair_refuses_unless_every_start_is_proved_dead(cold, monkeypatch, case, message):
    root, _, _, host = cold
    stranded = _host_reset(cold, monkeypatch)
    if case in ("serving", "hung"):
        plane = "serving" if case == "serving" else "unanswered"
        monkeypatch.setattr(activation, "_plane_state", lambda _root: plane)
    elif case in ("live-bot", "live-plane", "two-live"):
        live = {"live-bot": (_MANAGER_UNIT,), "live-plane": (_PLANE_UNIT,)}.get(case, (_PLANE_UNIT, _MANAGER_UNIT))
        for unit in live:
            host.states[unit] = "enabled loaded active"
    elif case == "unrecorded":
        stranded = _rewrite(root, _unrecorded_worker)
        host.states[_WORKER_UNIT] = "enabled loaded failed"  # Looping on admission refusals.
    elif case == "hosted":
        host.external_result = 1
    elif case in ("stray-fence", "oneshot"):
        stranded = _rewrite(root, _stray_fence if case == "stray-fence" else _oneshot_producer)
    starts = list(host.starts)
    reason = "two\nlines" if case == "reason" else "watchdog reset"
    lock = (state.locked_activation(root) if case == "locked" else nullcontext())
    with lock, pytest.raises(state.ActivationRefusal, match=message):
        activation.repair_starts_after_host_reset(root, "cold", reason=reason, adapter=host)
    assert state.read_activation(root, "cold").body == stranded  # Nothing changed.
    assert host.starts == starts


def test_host_reset_repair_reopens_a_failed_verification(cold, monkeypatch):
    """A reset after bots_started leaves verified next with nothing pending."""
    root, _, plan, host = cold

    def lost(*_args, **_kwargs):
        raise state.ActivationError("candidate bot lost readiness before producer admission")

    verify = activation._verify_candidate_startup
    monkeypatch.setattr(activation, "_verify_candidate_startup", lost)
    with pytest.raises(state.ActivationError, match="lost readiness"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(activation, "_verify_candidate_startup", verify)
    stopped = state.read_activation(root, "cold")
    assert activation.resumable_running_step(stopped) == "verified"
    assert stopped.body["pending"] in (None, "verified")
    _after_reset(host, monkeypatch, (_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT))
    repaired = activation.repair_starts_after_host_reset(root, "cold", reason="reset", adapter=host)
    assert repaired.record.body["pending"] == "ingest_started"
    assert "bots_started" not in repaired.record.body["completed"]
    assert len(repaired.archived_targets) == 3
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active"


def test_host_reset_repair_recovers_a_reset_during_producers_resumed(cold, monkeypatch):
    """The producer timer started, then the host reset before producers_resumed completed."""
    root, _, plan, host = cold
    verify = activation.enrollment.verify_candidate_enablement

    def reset_during_producers(store, activation_id, phase, *, adapter):
        if phase == "producers":
            raise state.ActivationError("watchdog reset")
        return verify(store, activation_id, phase, adapter=adapter)

    monkeypatch.setattr(activation.enrollment, "verify_candidate_enablement", reset_during_producers)
    with pytest.raises(state.ActivationError, match="watchdog reset"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    monkeypatch.setattr(activation.enrollment, "verify_candidate_enablement", verify)
    stopped = state.read_activation(root, "cold").body
    assert stopped["pending"] == "producers_resumed" and "producers" in stopped["start_phases"]
    timer = next(e for e in stopped["start_effects"].values() if e["target"] == _KEEPALIVE_TIMER)
    assert timer["result"] is not None
    everything = (_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT, _KEEPALIVE_TIMER)
    _after_reset(host, monkeypatch, everything[:-1])
    host.states[_KEEPALIVE_TIMER] = "enabled loaded active"  # Enabled timers start again at boot.
    with pytest.raises(state.ActivationRefusal, match=f"not quiescent: {_KEEPALIVE_TIMER}"):
        activation.repair_starts_after_host_reset(root, "cold", reason="reset", adapter=host)
    assert state.read_activation(root, "cold").body == stopped
    _after_reset(host, monkeypatch, everything)  # The operator stops the timer too.
    starts = list(host.starts)
    repaired = activation.repair_starts_after_host_reset(root, "cold", reason="reset", adapter=host)
    assert repaired.archived_targets == tuple(sorted(everything))
    assert repaired.record.body["pending"] == "ingest_started"
    assert repaired.record.body["start_phases"] == stopped["start_phases"]
    resumed = activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert resumed.status == "active" and tuple(resumed.body["completed"]) == state.STEPS
    assert host.starts == starts + [_PLANE_UNIT, _MANAGER_UNIT, _WORKER_UNIT, _KEEPALIVE_TIMER]


@pytest.mark.parametrize("change", ["partial", "mismatched", "abort-marker", "no-install-directory"])
def test_host_reset_archive_takes_exactly_the_recorded_starts_of_a_resumable_record(cold, monkeypatch, change):
    root = cold[0]
    stranded = _host_reset(cold, monkeypatch)
    effects = stranded["start_effects"]
    attempts, evidence = effects, {source: {} for source in effects}
    if change == "partial":
        attempts = dict(list(effects.items())[:-1])
        evidence = {source: {} for source in attempts}
    elif change == "mismatched":
        evidence = dict(list(evidence.items())[:-1])
    elif change == "abort-marker":
        stranded = _rewrite(root, lambda _store, body: body.update(adoption_abort={"reason": "fixture"}))
    else:
        stranded = _rewrite(root, lambda _store, body: body["intent"].pop("install_directory"))
    with state.locked_activation(root) as store, pytest.raises(state.ActivationRefusal, match="not the admitted set"):
        store.archive_starts_after_host_reset("cold", attempts=attempts, evidence=evidence,
                                              reason="reset", repair_artifact="a", host_boot=None)
    assert state.read_activation(root, "cold").body == stranded


def test_host_boot_reads_the_kernel_or_sysctl_and_records_none_when_unreadable(monkeypatch):
    linux = activation._host_boot()
    assert linux is None or set(linux) == {"boot_id", "booted_at"}
    answer = ["8C1F2B6A-0D0E-4C55-9C1E-2D7B9E1A0F33\n"]

    def sysctl(argv, **_kwargs):
        assert argv[-1] == "kern.bootsessionuuid"
        if answer[0] is None:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, answer[0], "")

    monkeypatch.setattr(activation.sys, "platform", "darwin")
    monkeypatch.setattr(activation.subprocess, "run", sysctl)
    assert activation._host_boot() == {"boot_id": "8C1F2B6A-0D0E-4C55-9C1E-2D7B9E1A0F33", "booted_at": None}
    for unreadable in ("\n", None):
        answer[0] = unreadable
        assert activation._host_boot() is None


def test_selected_bindings_refuse_loss_foreign_scope_and_ambiguous_actors(cold):
    root, _, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    original = state.read_activation(root, "cold").body
    journal = root / "state/activations/cold/activation.json"

    def replace_body(body):
        # Keep the outer journal hash valid to exercise binding validation.
        state._write(journal, {"record": body, "sha256": state._digest(body)})

    missing = deepcopy(original)
    del missing["identity_bindings"]
    replace_body(missing)
    with pytest.raises(state.ActivationError, match="differ from the selected plan"):
        read_selected_identity_bindings(root, "example", package=host.package)

    foreign = deepcopy(original)
    actual_host = foreign["identity_bindings"]["host_uid"]
    foreign["identity_bindings"]["host_uid"] = next(
        "host_" + digit * 32 for digit in "01" if "host_" + digit * 32 != actual_host)
    replace_body(foreign)
    with pytest.raises(state.ActivationError, match="another host"):
        read_selected_identity_bindings(root, "example", package=host.package)

    foreign_fleet = deepcopy(original)
    foreign_fleet["identity_bindings"]["fleets"]["other"] = (
        foreign_fleet["identity_bindings"]["fleets"].pop("example"))
    replace_body(foreign_fleet)
    with pytest.raises(state.ActivationError, match="differ from the selected plan"):
        read_selected_identity_bindings(root, "example", package=host.package)

    ambiguous = deepcopy(original)
    ambiguous["identity_bindings"]["fleets"]["example"]["bots"]["worker"] = (
        ambiguous["identity_bindings"]["fleets"]["example"]["bots"]["manager"])
    replace_body(ambiguous)
    with pytest.raises(state.ActivationError, match="ambiguous"):
        read_selected_identity_bindings(root, "example", package=host.package)


def test_old_data_and_wrong_executing_interpreter_refuse_before_preparation(cold, monkeypatch):
    root, release, plan, host = cold
    queue = db_file(root).parent / "staged"
    queue.mkdir(parents=True)
    (queue / "pending.batch").write_bytes(b"operator-owned pending data\n")
    with pytest.raises(state.ActivationError, match="absent Plane"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert not db_file(root).exists() and not host.starts
    assert not list((root / "state/activations").glob("*/activation.json"))
    (queue / "pending.batch").unlink()
    queue.rmdir()
    monkeypatch.setattr(activation.sys, "executable", "/wrong/interpreter")
    with pytest.raises(state.ActivationError, match="sealed candidate interpreter"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert not db_file(root).exists() and host.calls == []


@pytest.mark.parametrize("caller_result", [1, 3], ids=["self-hosted", "unknown-ancestry"])
def test_candidate_caller_refuses_before_sql_config_or_activation_prepare(cold, caller_result):
    root, _, plan, host = cold
    host.external_result = caller_result
    with pytest.raises(state.ActivationError, match="caller is hosted or cannot be proved external"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert not db_file(root).exists() and not (root / "runtime/bots").exists()
    assert not list((root / "state/activations").glob("*/activation.json"))
    assert host.starts == [] and list(host.directory.iterdir()) == []


def test_quiesced_handoff_refusal_repairs_forward_with_same_activation_id(enrollment, monkeypatch):
    """Post-pause, pre-migration handoff refusal: repair the input, resume the same ID.

    Real activation, unit-pause and canonical-handoff journals; the native
    boundary and migration manifest are stubs. Nothing is replayed natively.
    """
    from dataclasses import asdict
    from claudlobby import activation_units as units
    from claudlobby.plane.migrations import migrate
    from claudlobby.task_audit import audit_tasks

    inventory, phases, plan, adapter, _, _ = enrollment
    root = inventory.data_root
    directory = Path(inventory.catalog.split("directory\t", 1)[1].splitlines()[0])
    release = state.read_release(root, plan.release_id)
    package = SimpleNamespace(native=release.native_path, artifact_id=release.inputs.artifact_id)
    adapter.package = package
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(activation.RuntimeIdentity, "current", classmethod(lambda cls:
        activation.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(release.directory / release.paths.interpreter))

    # Seeded Plane: the old member bot holds one current assignment.
    host, fleet, member = "host_" + "1" * 32, "fleet_" + "2" * 32, "actor_" + "4" * 32
    task, assignment = "wi_" + "7" * 32, "asg_" + "9" * 32
    db_file(root).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file(root), isolation_level=None)
    try:
        migrate(connection)
        for uid, kind, alias, parent in ((host, "host", "host", None), (fleet, "fleet", "alpha", host),
                                         (member, "actor", "bot:alpha/member", fleet)):
            connection.execute("INSERT INTO identity_registry (uid, kind, alias, parent_uid, provisional,"
                               " first_seen, last_seen) VALUES (?, ?, ?, ?, 0, 't', 't')",
                               (uid, kind, alias, parent))
        _insert(connection, "work_items", work_item_id=task, fleet_uid=fleet,
                title="Held work", created_by_uid=member)
        _insert(connection, "assignments", assignment_id=assignment, work_item_id=task,
                fleet_uid=fleet, assignee_uid=member, assigned_by_uid=member)
        connection.row_factory = sqlite3.Row
        task_audit = asdict(audit_tasks(connection))
    finally:
        connection.close()
    identity = root / "state/host-uid"
    identity.write_text(host + "\n")
    identity.chmod(0o600)

    # The old bot's handoff ends in a previous canonical section that is damaged.
    member_dir = next(unit.declaration.working_directory for unit in inventory.units
                      if unit.declaration.scope == "bot")
    handoff = member_dir / ".claude/session.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    notes = b"---\nlast_updated: 2020-01-01T00:00:00Z\n---\n\n## Next Steps\n- held note\n"
    malformed = notes + b"\n\n" + _HANDOFF_BEGIN + b"\n```json\n{}\n```\n" + _HANDOFF_END + b"\n"
    handoff.write_bytes(malformed)

    with state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id,
                      enrollment_digest=inventory.digest)
        for step in state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="a" * 64)
        _prepare_pause(store, inventory, phases, plan, adapter, install_directory=directory)
        for phase, step in (("producers", "producers_paused"), (None, "sessions_handed_off"),
                            ("bots", "sessions_quiesced"), ("ingest", "ingest_quiesced")):
            store.begin("cutover", step)
            evidence = units.pause_phase(store, "cutover", phase, adapter=adapter).digest if phase else "a" * 64
            store.complete("cutover", step, evidence_digest=evidence)

    # Stub native/quiescence observations and the candidate roster; the
    # migration manifest is a stub bound to the seeded audit.
    monkeypatch.setattr(activation, "_legacy_bot_socket", lambda *_args, **_kwargs: (root / "absent.sock", False))
    monkeypatch.setattr(activation, "_legacy_quiet", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_probe", lambda _root: None)
    monkeypatch.setattr(activation, "planned_units", lambda _plan, _manager: ())
    contexts = (SimpleNamespace(fleet=SimpleNamespace(name="alpha", manager="member", bots=("member",))),)
    monkeypatch.setattr(activation, "_roster", lambda *_args: ({}, contexts))
    monkeypatch.setattr(activation, "_source_handoff_roster",
                        lambda *_args: {"alpha": ("member", ("member",))})
    manifest = SimpleNamespace(blockers=(), task_audit=task_audit, manifest_id="m-" + "c" * 64,
                               readability_blockers=lambda *_args, **_kwargs: ())
    monkeypatch.setattr(activation, "build_migration_manifest", lambda *_args, **_kwargs: manifest)

    class MigrationReached(Exception):
        pass

    def migration(_store, identifier, applied):
        assert identifier == "cutover" and applied is manifest
        raise MigrationReached

    monkeypatch.setattr(activation, "apply_migration", migration)
    calls = list(adapter.calls)

    with pytest.raises(state.ActivationError, match="existing canonical handoff section is unreadable"):
        activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
    refused = state.read_activation(root, "cutover")
    assert refused.status == "activating" and refused.body["pending"] == "queues_classified"
    recorded = refused.body["handoff_refreshed"]  # recorded before any handoff was written
    assert activation.resumable_running_step(refused) == "queues_classified"
    assert handoff.read_bytes() == malformed

    # Operator repair: keep the session notes, drop only the stale section.
    handoff.write_bytes(notes)
    with pytest.raises(MigrationReached):
        activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
    resumed = state.read_activation(root, "cutover")
    assert "queues_classified" in resumed.body["completed"]
    assert resumed.body["pending"] == "backup_saved"
    assert state.read_selection(root)["activation_id"] == "previous"
    repaired = handoff.read_bytes()
    assert notes in repaired and repaired.rstrip().endswith(_HANDOFF_END)
    # The repaired step reuses the time its refused attempt recorded (#2094).
    assert resumed.body["handoff_refreshed"] == recorded
    assert f"\nreferences_refreshed: {recorded}\n".encode() in repaired.split(b"\n---\n", 1)[0] + b"\n"
    assert task.encode() in repaired and assignment.encode() in repaired
    assert adapter.calls == calls  # no native pause, handoff or start replay


@pytest.mark.parametrize("has_unit,live,native,session,expected", [
    (True, True, "inactive", "ready", "restart"),
    (True, False, "inactive", "unknown", "stopped"),
    (False, False, "inactive", "unknown", "stopped"),
    (False, True, "inactive", "ready", "uninstalled bot"),
    (True, True, "inactive", "unknown", "ownership or readiness"),
    (True, False, "active", "ready", "no live private server"),
    (True, False, "activating", "ready", "transient or unreadable"),
])
def test_preserved_upgrade_disposition_requires_exact_live_or_absent_server(
        cold, monkeypatch, has_unit, live, native, session, expected):
    root, _, plan, host = cold
    # Use the selected definition's explicit TMPDIR, independently of ambient
    # environment and native wrapper activity. This fixture does not prove tmux.
    declaration, item = next((d, i) for d, i in planned_units(plan, "Linux") if d.bot == "worker")
    content = plan.blob(item["sha256"])
    declaration.source.parent.mkdir(parents=True)
    declaration.source.write_bytes(content)
    generated = FileSnapshot.read(declaration.source)
    environment = {**dict(declaration.environment), "TMUX_TMPDIR": str(host.tmux_dir)}
    unit = SimpleNamespace(declaration=declaration, target=declaration.source.name,
        generated=generated, installed=(generated,) if has_unit else (),
        properties=(("Environment", shlex.join(f"{k}={v}" for k, v in environment.items())),
                    ("ActiveState", native)))
    inventory = SimpleNamespace(units=(unit,), data_root=root, manager="Linux")
    monkeypatch.setattr(activation.enrollment, "selected_bot_entry", lambda *_: {
        "target": unit.target, "source": str(declaration.source),
        "installed": str(host.directory / declaration.source.name)})
    adapter = SimpleNamespace(read=lambda function, *_:
        f"disabled {'loaded' if has_unit else 'not-found'} {native}" if function == "svc_inventory_state" else session)
    socket_path = host.tmux_dir / f"tmux-{os.getuid()}" / declaration.source.stem
    socket_path.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        if live:
            server.bind(str(socket_path))
            server.listen(8)
        if expected in {"restart", "stopped"}:
            snapshot = activation._snapshot_bot_states(inventory, ((declaration, item),), adapter)
            assert snapshot[unit.target]["disposition"] == expected
            assert snapshot[unit.target]["socket_path"] == str(socket_path)
            assert snapshot == activation._snapshot_bot_states(inventory, ((declaration, item),), adapter)
        else:
            with pytest.raises(state.ActivationRefusal, match=expected):
                activation._snapshot_bot_states(inventory, ((declaration, item),), adapter)
    assert not (root / "state/activations").exists()


def test_preserved_upgrade_requires_same_roster_and_explicit_frozen_tmpdir(tmp_path):
    inventory = SimpleNamespace(units=())
    declaration = SimpleNamespace(scope="bot", fleet="example", bot="worker")
    with pytest.raises(state.ActivationRefusal, match="unchanged selected bot roster"):
        activation._snapshot_bot_states(inventory, ((declaration, {"enroll": True}),), None)
    unit = SimpleNamespace(installed=(), declaration=SimpleNamespace(source=tmp_path / "worker.service"),
                           generated=SimpleNamespace(content=b"[Service]\n"))
    with pytest.raises(state.ActivationRefusal, match="absolute private tmux"):
        activation._original_bot_tmpdir(unit, allow_uninstalled=True)


@pytest.mark.parametrize("pending", [None, "producers_paused"])
def test_preserved_disposition_is_rechecked_before_first_pause_without_replacing_intent(monkeypatch, tmp_path, pending):
    saved = {"example.worker": {"disposition": "restart"}}
    record = SimpleNamespace(body={"completed": [], "pending": pending, "intent": {
        "plan_id": "frozen", "install_directory": str(tmp_path), "bot_states": saved}})
    pause = SimpleNamespace(enrollment={"catalog": "frozen catalog", "manager": "Darwin"})
    calls = []
    store = SimpleNamespace(begin=lambda *args: calls.append(args))
    monkeypatch.setattr(activation, "read_activation", lambda *_: record)
    monkeypatch.setattr(activation, "read_plan", lambda *_: object())
    monkeypatch.setattr(activation, "planned_units", lambda *_: ())
    def placement(*_):
        assert pending is None, "a partial producer pause must use its owned journal proof"
    monkeypatch.setattr(activation, "_preflight_candidate_placement", placement)
    monkeypatch.setattr(activation, "_snapshot_bot_states", lambda *_: {
        "example.worker": {"disposition": "stopped"}})
    with pytest.raises(state.ActivationError, match="changed before the first activation pause"):
        activation._quiesce_running(tmp_path, store, "upgrade", pause, (), object())
    assert calls == [] and record.body["intent"]["bot_states"] is saved
    assert saved["example.worker"]["disposition"] == "restart"


def test_preserved_stopped_witness_exempts_only_stop_guard_and_keeps_runner_pause_guard(tmp_path):
    root = tmp_path.resolve()
    directory = root / "worker"
    directory.mkdir()
    runner = SimpleNamespace(autonomous_runner=None)
    context = SimpleNamespace(paths=SimpleNamespace(bot_runtime=lambda _: directory),
        fleet=SimpleNamespace(name="example", bots={"worker": runner}))
    unit = SimpleNamespace(target="example.worker", installed=(), declaration=SimpleNamespace(
        scope="bot", fleet="example", bot="worker", working_directory=directory))
    with pytest.raises(state.ActivationRefusal, match="deliberately stopped"):
        activation._refuse_unrecorded_run_intent(root, (unit,), (context,))
    snapshot = {unit.target: {"disposition": "stopped", "session": "server_absent"}}
    activation._refuse_unrecorded_run_intent(root, (unit,), (context,), bot_states=snapshot)
    (directory / activation.LEGACY_RUNNER_PAUSE).write_text("paused\n")
    runner.autonomous_runner = object()
    with pytest.raises(state.ActivationRefusal, match="markers have no recorded automation pause"):
        activation._refuse_unrecorded_run_intent(root, (unit,), (context,), bot_states=snapshot)


@pytest.mark.parametrize("native", ["masked-runtime masked inactive", "disabled loaded inactive"])
def test_preserved_stopped_bot_must_be_native_absent_before_producers(monkeypatch, tmp_path, native):
    target = "example.worker.service"
    entry = {"target": target, "installed": str(tmp_path / target), "publish": False}
    record = SimpleNamespace(body={"completed": ["bots_started"], "intent": {
        "bot_states": {target: {"disposition": "stopped"}}}})
    monkeypatch.setattr(activation, "read_activation", lambda *_: record)
    monkeypatch.setattr(activation, "_ingest_ready", lambda *_: {})
    monkeypatch.setattr(activation.enrollment, "candidate_entries", lambda *_: (entry,))
    monkeypatch.setattr(activation.enrollment, "_check_targets", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation.units, "load_unit_pause", lambda *_: SimpleNamespace(enrollment={}))
    calls = []
    adapter = SimpleNamespace(read=lambda *_: native)
    store = SimpleNamespace(begin=lambda *_: calls.append("producer admission"))
    with pytest.raises(state.ActivationError, match="remains natively enrolled or masked"):
        activation._verify_candidate_startup(tmp_path, store, "upgrade", None, {}, {}, (), {}, adapter)
    assert calls == []


def _preserve_capture(bot_dir, *, kind="capture"):
    from datetime import datetime, timezone
    from claudlobby.activation_handoffs import _refresh_envelope
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    directory = bot_dir / ".claude"
    directory.mkdir(parents=True, exist_ok=True)
    capture = (f"---\ncwd: {bot_dir}\nlast_updated: {stamp}\n"
               f"references_refreshed: {stamp}\nschema_version: 2\n---\n\n"
               "## Decisions\n- Acceptance observation saved.\n").encode()
    # Grounded in the canary's clauDNA write: real capture frontmatter/State
    # precedes an older canonical reference shell, whose timestamp is separate.
    shell = _refresh_envelope(bot_dir, stamp) + b"\ncanonical references\n"
    if kind == "reference-only":
        content = shell
    elif kind == "stale-capture":
        content = capture.replace(f"last_updated: {stamp}".encode(), b"last_updated: 2000-01-01T00:00:00Z")
    elif kind == "shell-below":
        content = capture + b"\n" + shell
    elif kind == "envelope-above":
        content = _refresh_envelope(bot_dir, stamp) + capture
    else:
        content = capture
    (directory / "session.md").write_bytes(content)


@pytest.mark.parametrize("kind", ["reference-only", "stale-capture", "capture", "shell-below", "envelope-above"])
def test_preserved_handoff_uses_capture_timestamp_not_reference_refresh_or_mtime(tmp_path, kind):
    _preserve_capture(tmp_path, kind=kind)
    if kind in {"reference-only", "stale-capture"}:
        with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
            activation._require_preserved_handoff(tmp_path)
    else:
        activation._require_preserved_handoff(tmp_path)


@pytest.fixture
def preserved_handoff_case(tmp_path, monkeypatch):
    target = "example.worker.plist"
    bot_dir = tmp_path / "worker"
    bot_dir.mkdir()
    unit = SimpleNamespace(target=target, installed=(SimpleNamespace(path=tmp_path / target),), declaration=SimpleNamespace(
        scope="bot", source=bot_dir / target, working_directory=bot_dir, fleet="example", bot="worker"))
    body = {"completed": ["producers_paused"], "pending": None, "handoff_effects": {},
            "intent": {"preserve_bot_state": True, "install_directory": str(tmp_path),
                       "bot_states": {target: {"disposition": "restart"}}},
            "previous_selection": {"activation_id": "previous"}}
    record = SimpleNamespace(body=body, status="activating")
    calls, steps, on_call = [], [], [None]
    class BotPauseReached(Exception):
        pass
    def begin(_activation, step):
        steps.append(step)
        if step == "sessions_quiesced":
            raise BotPauseReached
        body["pending"] = step
    def complete(_activation, step, **_):
        body["completed"].append(step)
        body["pending"] = None
    def handoff(_activation, *, target, result, request_started_ns=None):
        if request_started_ns is not None:
            body.setdefault("handoff_requests", {})[target] = {"started_at_ns": request_started_ns}
        body["handoff_effects"][target] = result
    store = SimpleNamespace(begin=begin, complete=complete, record_handoff=handoff,
                            arm_handoff_evidence=lambda *_: None)
    present = [True]
    monkeypatch.setattr(activation, "read_activation", lambda *_: record)
    monkeypatch.setattr(activation, "_legacy_bot_socket", lambda *_args, **_kwargs: (tmp_path / "socket", present[0]))
    monkeypatch.setattr(activation, "_original_bot_tmpdir", lambda *_: str(tmp_path))
    outcome = [0, "handoff-saved"]
    def call(function, *args, **kwargs):
        assert body["handoff_effects"][target] is None  # Fence before the send.
        calls.append((function, args, kwargs))
        if on_call[0] is not None:
            on_call[0]()
        return subprocess.CompletedProcess([function], outcome[0], outcome[1] + "\n", "")
    adapter = SimpleNamespace(call=call)
    def run():
        return activation._quiesce_running(tmp_path, store, "upgrade", None, (unit,), adapter)
    return SimpleNamespace(run=run, body=body, record=record, calls=calls, steps=steps,
                           outcome=outcome, bot_dir=bot_dir, target=target,
                           present=present, on_call=on_call, BotPauseReached=BotPauseReached)


@pytest.mark.parametrize("marker", ["handoff-saved", "handoff-skipped:recent"])
def test_preserved_activation_requires_explicit_marker_and_verified_capture(preserved_handoff_case, marker):
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir, kind="shell-below")
    case.outcome[1] = marker
    if marker == "handoff-saved":
        case.on_call[0] = lambda: _preserve_capture(case.bot_dir, kind="shell-below")
    with pytest.raises(case.BotPauseReached):
        case.run()
    assert case.body["handoff_effects"][case.target] == "handed_off"
    assert case.calls[0][1][-1] == "explicit"


@pytest.mark.parametrize("marker,rc,kind", [
    ("handoff-timeout", 0, "capture"),
    ("handoff-skipped:capability", 0, "capture"),
    ("handoff-skipped:no-session", 0, "capture"),
    ("unrecognized", 0, "capture"),
    ("handoff-saved", 3, "capture"),
    ("handoff-skipped:recent", 0, "reference-only"),
    ("handoff-saved", 0, "stale-capture"),
])
def test_preserved_activation_refuses_unverified_handoff_and_never_resends_unknown(
        preserved_handoff_case, marker, rc, kind):
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir, kind=kind)
    case.outcome[:] = [rc, marker]
    with pytest.raises(state.ActivationRefusal, match="inspect.*session.*handoff"):
        case.run()
    assert case.body["handoff_effects"][case.target] is None
    assert "sessions_quiesced" not in case.steps
    assert activation.resumable_running_step(case.record) == "sessions_handed_off"
    # A late UI-approved capture reconciles only the original request: no send.
    _preserve_capture(case.bot_dir, kind="shell-below")
    case.outcome[:] = [0, "handoff-saved"]
    with pytest.raises(case.BotPauseReached):
        case.run()
    assert case.body["handoff_effects"][case.target] == "handed_off"
    assert len(case.calls) == 1


# How far a file's mtime can trail time.time_ns() (#2528): 7.4 ms was the most measured on a
# 250 Hz Pi, whose kernel stamps files from a clock that advances once a tick (4 ms), and a
# filesystem that keeps whole seconds can trail it by up to one.
FILE_CLOCK_LAGS = [pytest.param(7_500_000, id="7.5ms"), pytest.param(1_000_000_000, id="1s")]


@pytest.mark.parametrize("flow", ["request", "resume"])
@pytest.mark.parametrize("lag_ns", FILE_CLOCK_LAGS)
def test_preserved_handoff_request_is_timed_on_the_handoff_files_clock(
        preserved_handoff_case, monkeypatch, lag_ns, flow):
    """#2528: the request is timed on the clock that stamps the handoff file, so a handoff
    written after it never reads as older, however far that clock trails time.time_ns().
    time.time_ns() runs lag_ns ahead here, which is how such a file clock looks from the
    process, whatever this host's own clocks do. Both callers: the request verified at
    once, and a resume verifying a late save against the recorded request."""
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir, kind="shell-below")
    process_clock = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: process_clock() + lag_ns)
    if flow == "request":
        case.on_call[0] = lambda: _preserve_capture(case.bot_dir, kind="shell-below")
    else:
        case.outcome[1] = "handoff-timeout"
        with pytest.raises(state.ActivationRefusal, match="did not produce a verified save"):
            case.run()
        _preserve_capture(case.bot_dir, kind="shell-below")
        case.outcome[1] = "handoff-saved"
    with pytest.raises(case.BotPauseReached):
        case.run()
    assert case.body["handoff_effects"][case.target] == "handed_off"
    assert len(case.calls) == 1


@pytest.mark.parametrize("offset_ns,accepted", [
    pytest.param(0, True, id="at-the-request"),
    pytest.param(-1, False, id="one-nanosecond-before"),
    pytest.param(-1_000_000_000, False, id="one-second-before"),
])
def test_preserved_handoff_written_before_its_request_is_still_refused(
        preserved_handoff_case, offset_ns, accepted):
    """#2528 control: timing the request on the file's clock adds no slack. A handoff whose
    file reads as written before the recorded request is refused for that, and one written
    at it is accepted."""
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir, kind="shell-below")

    def save():
        _preserve_capture(case.bot_dir, kind="shell-below")
        written = case.body["handoff_requests"][case.target]["started_at_ns"] + offset_ns
        os.utime(case.bot_dir / ".claude/session.md", ns=(written, written))
    case.on_call[0] = save
    if accepted:
        with pytest.raises(case.BotPauseReached):
            case.run()
        assert case.body["handoff_effects"][case.target] == "handed_off"
    else:
        with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff") as refused:
            case.run()
        assert str(refused.value.__cause__.__cause__) == "handoff predates its request"
        assert case.body["handoff_effects"][case.target] is None


def test_preserved_handoff_request_that_cannot_be_timed_refuses_before_it_is_sent(preserved_handoff_case):
    """#2528: the request is timed in the bot's own handoff directory before the fence, so a
    bot without one refuses with nothing recorded and nothing sent."""
    case = preserved_handoff_case
    with pytest.raises(state.ActivationRefusal, match="no handoff was requested"):
        case.run()
    assert case.body["handoff_effects"] == {}
    assert case.calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_preserved_handoff_request_in_an_unwritable_handoff_directory_refuses_before_it_is_sent(
        preserved_handoff_case):
    """#2533: a bot whose .claude/ exists but takes no new file cannot have its request timed
    there, so the activation refuses on the stamp's own BotLifecycleError, with nothing recorded
    and nothing sent. A stamp moved out of that directory would take no notice of the mode, and
    this activation would go on to send."""
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir, kind="shell-below")
    handoff_dir = case.bot_dir / ".claude"
    handoff_dir.chmod(0o500)
    try:
        with pytest.raises(state.ActivationRefusal, match="cannot time its handoff request") as refused:
            case.run()
    finally:
        handoff_dir.chmod(0o700)
    assert isinstance(refused.value.__cause__, bot_operations.BotLifecycleError)
    assert case.body["handoff_effects"] == {}
    assert case.calls == []

def _preserved_estate(tmp_path, monkeypatch, names):
    """Bots handed off one after another in one preserve-state activation (#2547).

    Each read_activation parses the record afresh from sorted-key JSON, as the store does, so the
    handoff loop sees only what it read before it began, and a retry reads the recorded handoffs
    back in name order. preserved_handoff_case shares one record, which would let a refusal built
    from that read pass here.
    """
    units, dirs, states = [], {}, {}
    for name in names:
        target = f"example.{name}.plist"
        bot_dir = tmp_path / name
        _preserve_capture(bot_dir, kind="shell-below")
        units.append(SimpleNamespace(target=target, installed=(SimpleNamespace(path=tmp_path / target),),
                                     declaration=SimpleNamespace(scope="bot", source=bot_dir / target,
                                                                 working_directory=bot_dir, fleet="example",
                                                                 bot=name)))
        dirs[name] = bot_dir
        states[target] = {"disposition": "restart"}
    body = {"completed": ["producers_paused"], "pending": None, "handoff_effects": {},
            "intent": {"preserve_bot_state": True, "install_directory": str(tmp_path), "bot_states": states},
            "previous_selection": {"activation_id": "previous"}}
    calls, outcome = [], ["handoff-saved"]

    class BotPauseReached(Exception):
        pass

    def begin(_activation, step):
        if step == "sessions_quiesced":
            raise BotPauseReached
        body["pending"] = step

    def complete(_activation, step, **_):
        body["completed"].append(step)
        body["pending"] = None

    def handoff(_activation, *, target, result, request_started_ns=None):
        if request_started_ns is not None:
            body.setdefault("handoff_requests", {})[target] = {"started_at_ns": request_started_ns}
        body["handoff_effects"][target] = result

    store = SimpleNamespace(begin=begin, complete=complete, record_handoff=handoff,
                            arm_handoff_evidence=lambda *_: None)
    monkeypatch.setattr(activation, "read_activation",
                        lambda *_: SimpleNamespace(body=json.loads(json.dumps(body, sort_keys=True)),
                                                   status="activating"))
    monkeypatch.setattr(activation, "_legacy_bot_socket", lambda *_args, **_kwargs: (tmp_path / "socket", True))
    monkeypatch.setattr(activation, "_original_bot_tmpdir", lambda *_: str(tmp_path))

    def call(function, bot_dir, *_args, **_kwargs):
        assert function == "svc_activation_handoff"
        assert body["handoff_effects"][f"example.{bot_dir.name}.plist"] is None  # Fence before the send.
        calls.append(bot_dir.name)
        if outcome[0] == "handoff-saved":
            _preserve_capture(bot_dir, kind="shell-below")
        return subprocess.CompletedProcess([function], 0, outcome[0] + "\n", "")

    def run():
        return activation._quiesce_running(tmp_path, store, "upgrade", None, tuple(units),
                                           SimpleNamespace(call=call))
    return SimpleNamespace(run=run, body=body, calls=calls, dirs=dirs, outcome=outcome,
                           BotPauseReached=BotPauseReached)


def _refused_while_unwritable(case, name):
    handoff_dir = case.dirs[name] / ".claude"
    handoff_dir.chmod(0o500)
    try:
        with pytest.raises(state.ActivationRefusal, match="cannot time its handoff request") as refused:
            case.run()
    finally:
        handoff_dir.chmod(0o700)
    return str(refused.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_preserved_handoff_refusal_names_the_bot_and_the_bots_already_handed_off(tmp_path, monkeypatch):
    """#2547: the second bot's handoff directory takes no new file. The refusal names that bot and
    the directory, and says the first bot was handed off and that a retry keeps its handoff."""
    case = _preserved_estate(tmp_path, monkeypatch, ["alpha", "bravo"])
    assert _refused_while_unwritable(case, "bravo") == (
        "preserve-state activation cannot time its handoff request to example/bravo in that bot's handoff "
        f"directory {case.dirs['bravo'] / '.claude'}, so no handoff was requested from it; inspect that "
        "directory, then retry the same activation with --resume; already handed off in this activation: "
        "example/alpha; a retry keeps those handoffs and does not request them again, and refuses if one is "
        "more than 300 s old when it stops the sessions")
    assert case.calls == ["alpha"]
    assert case.body["handoff_effects"] == {"example.alpha.plist": "handed_off"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_preserved_handoff_refusal_says_when_no_bot_was_handed_off(tmp_path, monkeypatch):
    """#2547: when the first bot's directory refuses, nothing was handed off, and the refusal says so."""
    case = _preserved_estate(tmp_path, monkeypatch, ["alpha", "bravo"])
    message = _refused_while_unwritable(case, "alpha")
    assert f"to example/alpha in that bot's handoff directory {case.dirs['alpha'] / '.claude'}," in message
    assert message.endswith("--resume; no bot has been handed off in this activation")
    assert "example/bravo" not in message
    assert case.calls == []
    assert case.body["handoff_effects"] == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_a_retry_that_refuses_again_still_names_the_bots_handed_off_before_it(tmp_path, monkeypatch):
    """#2547: the list includes what an earlier run of the same activation recorded. Once the
    directory takes files, the retry sends only the handoff still owed."""
    case = _preserved_estate(tmp_path, monkeypatch, ["alpha", "bravo"])
    for _ in range(2):
        assert "already handed off in this activation: example/alpha;" in _refused_while_unwritable(case, "bravo")
    assert case.calls == ["alpha"]
    with pytest.raises(case.BotPauseReached):
        case.run()
    assert case.calls == ["alpha", "bravo"]
    assert case.body["handoff_effects"] == {"example.alpha.plist": "handed_off", "example.bravo.plist": "handed_off"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_a_retry_names_the_bots_handed_off_in_the_order_it_requested_them(tmp_path, monkeypatch):
    """#2547: the bots are declared out of name order, and the store reads the record back with
    sorted keys. Both runs name the handed-off bots in the order their handoffs were requested."""
    case = _preserved_estate(tmp_path, monkeypatch, ["bravo", "alpha", "charlie"])
    for _ in range(2):
        assert ("already handed off in this activation: example/bravo, example/alpha;"
                in _refused_while_unwritable(case, "charlie"))
    assert case.calls == ["bravo", "alpha"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
def test_a_late_save_reconciled_by_the_retry_is_named_as_handed_off(tmp_path, monkeypatch):
    """#2547: a request recorded with no result, whose save the retry verifies, counts as handed off."""
    case = _preserved_estate(tmp_path, monkeypatch, ["alpha", "bravo"])
    case.outcome[0] = "handoff-timeout"
    with pytest.raises(state.ActivationRefusal, match="did not produce a verified save"):
        case.run()
    assert case.body["handoff_effects"] == {"example.alpha.plist": None}
    _preserve_capture(case.dirs["alpha"], kind="shell-below")  # The save lands after the door gave up.
    assert "already handed off in this activation: example/alpha;" in _refused_while_unwritable(case, "bravo")
    assert case.calls == ["alpha"]
    assert case.body["handoff_effects"] == {"example.alpha.plist": "handed_off"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a mode-0500 directory")
@pytest.mark.parametrize("age_s,kept", [pytest.param(250, True, id="250s"), pytest.param(301, False, id="301s")])
def test_a_handoff_the_retry_keeps_must_still_be_at_most_300_s_old_when_the_sessions_stop(
        tmp_path, monkeypatch, age_s, kept):
    """#2547 control for the refusal's last clause: the retry does not request a kept handoff again,
    and one more than 300 s old when the activation stops the sessions refuses it."""
    from datetime import datetime, timedelta, timezone
    case = _preserved_estate(tmp_path, monkeypatch, ["alpha", "bravo"])
    _refused_while_unwritable(case, "bravo")
    capture = case.dirs["alpha"] / ".claude/session.md"
    then = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    capture.write_text(re.sub(r"last_updated: \S+", f"last_updated: {then:%Y-%m-%dT%H:%M:%SZ}",
                              capture.read_text(), count=1))
    os.utime(capture, (then.timestamp(), then.timestamp()))
    if kept:
        with pytest.raises(case.BotPauseReached):
            case.run()
    else:
        with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
            case.run()
    assert case.calls == ["alpha", "bravo"]


def test_preserved_handoff_saved_in_the_requests_own_file_clock_tick_stays_held(preserved_handoff_case, monkeypatch):
    """#2528: a capture saved just before the request can share the file clock's reading at
    the request (one 4 ms tick at 250 Hz). The request waits for the clock's next reading,
    so a resume still finds that capture older than the request and holds it."""
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir)
    saved = (case.bot_dir / ".claude/session.md").stat().st_mtime_ns
    readings = iter([saved, saved, saved + 4_000_000])
    monkeypatch.setattr(bot_operations, "_file_clock_ns", lambda _directory: next(readings))
    case.outcome[:] = [3, "handoff-timeout"]
    with pytest.raises(state.ActivationRefusal, match="did not produce a verified save"):
        case.run()
    assert case.body["handoff_requests"][case.target]["started_at_ns"] == saved + 4_000_000
    with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff") as refused:
        case.run()
    assert str(refused.value.__cause__.__cause__) == "handoff predates its request"
    assert case.body["handoff_effects"][case.target] is None and len(case.calls) == 1


@pytest.mark.parametrize("fresh", [False, True])
def test_witnessed_running_bot_now_absent_requires_saved_capture(preserved_handoff_case, fresh):
    case = preserved_handoff_case
    case.present[0] = False
    if fresh:
        _preserve_capture(case.bot_dir)
        with pytest.raises(case.BotPauseReached):
            case.run()
        assert case.body["handoff_effects"][case.target] == "server_absent"
    else:
        with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
            case.run()
        assert case.body["handoff_effects"] == {}
    assert case.calls == []


def test_default_activation_keeps_best_effort_handoff_semantics(preserved_handoff_case):
    case = preserved_handoff_case
    case.body["intent"].pop("preserve_bot_state")
    case.outcome[:] = [0, "Handoff timed out after 30s"]
    with pytest.raises(case.BotPauseReached):
        case.run()
    assert case.body["handoff_effects"][case.target] == "handed_off"
    assert len(case.calls[0][1]) == 3


@pytest.mark.parametrize("kind", ["reference-only", "stale-capture", "pre-attempt"])
def test_preserved_unknown_handoff_stays_held_without_request_bound_capture(preserved_handoff_case, kind):
    case = preserved_handoff_case
    _preserve_capture(case.bot_dir)
    case.outcome[:] = [3, "handoff-timeout"]
    with pytest.raises(state.ActivationRefusal):
        case.run()
    if kind != "pre-attempt":
        _preserve_capture(case.bot_dir, kind=kind)
    with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
        case.run()
    assert case.body["handoff_effects"][case.target] is None and len(case.calls) == 1
    assert "sessions_quiesced" not in case.steps


@pytest.mark.parametrize("preserve,witness", [(False, True), (True, False)])
def test_unbound_unknown_handoff_remains_nonresumable(preserved_handoff_case, preserve, witness):
    case = preserved_handoff_case
    case.body["handoff_effects"][case.target] = None
    case.body["pending"] = "sessions_handed_off"
    if not preserve:
        case.body["intent"].pop("preserve_bot_state")
    if witness:
        case.body["handoff_requests"] = {case.target: {"started_at_ns": 1}}
    _preserve_capture(case.bot_dir)
    assert activation.resumable_running_step(case.record) is None
    with pytest.raises(state.ActivationError, match="unknown effect|no bound capture witness"):
        case.run()
    assert case.calls == []


def test_delayed_known_handoff_rechecks_fresh_capture_before_native_pause(preserved_handoff_case):
    case = preserved_handoff_case
    case.body["completed"].append("sessions_handed_off")
    case.body["handoff_effects"][case.target] = "handed_off"
    _preserve_capture(case.bot_dir, kind="stale-capture")
    with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
        case.run()
    assert case.steps == [] and case.calls == []


@pytest.mark.parametrize("quiet,fresh", [(True, False), (False, False), (False, True)])
def test_interrupted_bot_pause_reuses_capture_only_for_verified_quiet_target(
        preserved_handoff_case, monkeypatch, quiet, fresh):
    case = preserved_handoff_case
    case.body["completed"].append("sessions_handed_off")
    case.body["pending"] = "sessions_quiesced"
    case.body["handoff_effects"][case.target] = "handed_off"
    _preserve_capture(case.bot_dir, kind="capture" if fresh else "stale-capture")
    observations = []
    def observed(_adapter, **kwargs):
        observations.append(kwargs)
        if not quiet:
            raise activation.RuntimeEvidenceError("quiescence", case.target, "still running")
        return activation.RuntimeEvidence("quiescent", case.target, {})
    monkeypatch.setattr(activation, "assert_quiescent", observed)
    if quiet or fresh:
        with pytest.raises(case.BotPauseReached):
            case.run()
        assert case.steps == ["sessions_quiesced"]
    else:
        with pytest.raises(state.ActivationRefusal, match="fresh verified session handoff"):
            case.run()
        assert case.steps == []
    assert observations == [{"installed_file": case.bot_dir.parent / case.target,
                             "target": case.target, "socket_path": case.bot_dir.parent / "socket"}]
    assert case.calls == []


def test_first_activation_preserves_paired_owner_and_attests_only_new_recorder(cold):
    from claudlobby.plane.owner_access import OwnerAccess, PrincipalRef
    from claudlobby.plane.owner_source import inspect_source
    root, release, plan, host = cold
    ensure_host_uid(root / "state")
    store = OwnerAccess.initialize(root)
    store.prepare_first_fleet()
    principal = PrincipalRef("tailscale:controlplane.tailscale.com", "1001")
    challenge = store.begin_pairing(principal)
    owner = store.confirm_pairing(challenge.token, expected_principal=principal)
    store.workspace().initialize(expected_owner=owner)
    assert not db_file(root).exists()
    record = activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert record.status == "active"
    assert store.current_grant() == owner
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT active FROM first_fleet_preparation").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM grants").fetchone() == (1,)
    assert inspect_source(root) == owner.host_uid
    store.retire_first_fleet()  # Explicit bootstrap resume does not rearm preparation.
    assert store.current_grant() == owner


def test_ordinary_owner_initialized_after_ingest_requires_explicit_binding_on_resume(cold, monkeypatch):
    from claudlobby.plane.owner_access import OwnerAccess
    from claudlobby.plane import owner_source
    root, _, plan, host = cold
    _failed_worker_start(cold)
    store = OwnerAccess.initialize(root)
    assert not store.first_fleet_pending()
    starts = host.starts[:]
    with pytest.raises(state.ActivationError, match="explicit recovery"):
        activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert host.starts == starts
    owner_source.bind_source(root)
    host.fail_bot = None
    monkeypatch.setattr(owner_source, "bind_source", lambda root: pytest.fail("ordinary authority must not auto-bind"))
    assert activation.resume_activation(root, "cold", plan.plan_id, host.directory, adapter=host).status == "active"
    assert host.starts == starts + ["claudlobby-keepalive.timer"]


@pytest.mark.parametrize("operation", ["bind_source", "retire_first_fleet"])
def test_first_activation_owner_hook_errors_keep_activation_contract(cold, monkeypatch, operation):
    from claudlobby.plane.owner_access import AccessUnavailable, OwnerAccess
    from claudlobby.plane import owner_source
    root, _, plan, host = cold
    ensure_host_uid(root / "state")
    OwnerAccess.initialize(root, before_first_fleet=True)

    def unavailable(*args):
        raise AccessUnavailable("synthetic authority interruption")

    monkeypatch.setattr(owner_source if operation == "bind_source" else OwnerAccess, operation, unavailable)
    with pytest.raises(state.ActivationError, match="explicit recovery"):
        activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    assert not host.starts
