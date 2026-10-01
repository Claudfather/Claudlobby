"""Cold-host activation with real durable owners and a private native manager."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import activation, activation_state as state, runtime_admission as admission
from claudlobby.activation_handoffs import _BEGIN as _HANDOFF_BEGIN, _END as _HANDOFF_END
from claudlobby.activation_identity import read_selected_identity_bindings
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import planned_units, unit_family
from claudlobby.migration_apply import read_migration
from claudlobby.plane.db import db_file
from claudlobby.plane.identity import resolve
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.queue_paths import spool_path
from claudlobby.releases import MANIFEST, release_path, seal_release
from claudlobby.supervision_inventory import FileSnapshot
from tests.test_releases import installed
from tests.test_migration_plan import _database, _event_request, _pending
from tests.test_task_audit import _insert
from tests.test_activation_units import _pause_all, _prepare as _prepare_pause, enrollment  # noqa: F401


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp("boot")


class NativeHost:
    """Manager observations/actions only; real grants run harmless exec callbacks."""

    def __init__(self, root, release, plan, directory, package):
        self.root, self.release, self.plan, self.directory, self.package = root, release, plan, directory, package
        self.states, self.starts, self.ready, self.calls = {}, [], [], []
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
            output = f"manager\tLinux\ndirectory\t{self.directory}\n"
            output += ''.join(f"installed\t{p.name}\n" for p in sorted(self.directory.iterdir())
                              if p.suffix in (".service", ".timer"))
            output += ''.join(f"loaded\t{name}\n" for name in sorted(self.states))
        elif function == "svc_activation_assert_external":
            assert args[-1] == str(os.getpid())
            rc = self.external_result
        elif function == "svc_inventory_state":
            output = self.states.get(args[1], "not-found not-found inactive")
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
               {"installed": "/user/added.service", "service": None})
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
    (("manager", "worker"), True, "existing canonical handoff section is malformed"),
], ids=["stopped-worker", "uninstalled-manager", "appended-section"])
def test_first_adoption_refuses_standing_handoff_blockers_before_record_or_pause(
        cold, monkeypatch, installed_bots, appended, message):
    root, _, plan, host = cold
    units = _legacy_handoff_estate(root, installed_bots)
    handoff = root.resolve() / "legacy-runtime" / installed_bots[0] / ".claude/session.md"
    if appended:
        handoff.parent.mkdir()
        handoff.write_bytes(b"old notes\n\n" + _HANDOFF_BEGIN + b"\n```json\n{}\n```\n"
                            + _HANDOFF_END + b"\n\n## Later notes\n")
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


@pytest.fixture
def cold(installed, monkeypatch, tmp_path):
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
    builder.effects["fleet_sources"] = {"example": {
        "fleet": {"path": str(manifest), "sha256": builder.input_content(manifest)},
        "projects": {"path": str(projects), "sha256": builder.input_content(projects)},
    }}
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
           "CLAUDLOBBY_RELEASE_ID": release.release_id, "CLAUDLOBBY_CLI": str(release.cli_path),
           "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
           "CLAUDLOBBY_LIBRARY_DIR": str(directory / "library"),
           "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id}
    tmux_dir = tmp_path / "private-tmux"  # Never the host's shared /tmp sockets.
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
    plan = builder.seal()
    package = SimpleNamespace(native=release.native_path, artifact_id=release.inputs.artifact_id)
    native_directory = tmp_path / "private-native-user"
    native_directory.mkdir()
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


def test_upgrade_refuses_candidate_persistent_disabled_override_before_pause(cold, monkeypatch):
    root, release, source_plan, host = cold
    activation.bootstrap_activation(root, "cold", source_plan.plan_id, host.directory, adapter=host)
    item = next(item for item in source_plan.effects["units"]
                if Path(item["source"]).name == "claudlobby-plane-daemon.plist")
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                                ("example",), effects={"units": [item]})
    builder.file(Path(item["source"]), source_plan.blob(item["sha256"]), mode=item["mode"])
    candidate = builder.seal()
    catalog = (f"manager\tDarwin\ndomain\tgui/{os.getuid()}\ndirectory\t{host.directory}\n"
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
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, host.directory, adapter=host)
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
    foreign = host.directory / "claudlobby-plane-daemon.plist"
    foreign.write_bytes(b"unrelated installed LaunchAgent")
    original = foreign.read_bytes()
    catalog = (f"manager\tDarwin\ndomain\tgui/{os.getuid()}\ndirectory\t{host.directory}\n"
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
        activation.upgrade_activation(root, "upgrade", candidate.plan_id, host.directory, adapter=host)
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
        pytest.skip("AF_UNIX path limit; run with a short private TMPDIR")
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


def test_start_repair_archives_dead_attempt_then_resume_starts_it_once(cold):
    root, _, plan, host = cold
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

    # The old bot's session appended notes after a previous canonical section.
    member_dir = next(unit.declaration.working_directory for unit in inventory.units
                      if unit.declaration.scope == "bot")
    handoff = member_dir / ".claude/session.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    notes = b"---\nlast_updated: 2020-01-01T00:00:00Z\n---\n\n## Next Steps\n- held note\n"
    malformed = notes + b"\n\n" + _HANDOFF_BEGIN + b"\n```json\n{}\n```\n" + _HANDOFF_END + b"\n\n## Later\n"
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
    manifest = SimpleNamespace(blockers=(), task_audit=task_audit, manifest_id="m-" + "c" * 64)
    monkeypatch.setattr(activation, "build_migration_manifest", lambda *_args, **_kwargs: manifest)

    class MigrationReached(Exception):
        pass

    def migration(_store, identifier, applied):
        assert identifier == "cutover" and applied is manifest
        raise MigrationReached

    monkeypatch.setattr(activation, "apply_migration", migration)
    calls = list(adapter.calls)

    with pytest.raises(state.ActivationError, match="existing canonical handoff section is malformed"):
        activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
    refused = state.read_activation(root, "cutover")
    assert refused.status == "activating" and refused.body["pending"] == "queues_classified"
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
    assert task.encode() in repaired and assignment.encode() in repaired
    assert adapter.calls == calls  # no native pause, handoff or start replay
