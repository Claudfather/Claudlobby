"""Cold-host activation with real durable owners and a private native manager."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import activation, activation_state as state, runtime_admission as admission
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import planned_units, unit_family
from claudlobby.migration_apply import read_migration
from claudlobby.plane.db import db_file
from claudlobby.plane.identity import resolve
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.releases import seal_release
from tests.test_releases import installed
from tests.test_task_audit import _insert


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
                output = "bridge-ready"
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
    for stem, phase, bot in (("claudlobby-plane-daemon", "ingest", None),
                             ("com.example.worker", "bots", "worker"),
                             ("com.example.manager", "bots", "manager"),
                             ("claudlobby-keepalive", "producers", None)):
        working = root / "runtime/bots" / bot if bot else root
        destination = working if bot else root / "runtime/_host/timers"
        command = ([str(release.native_path / "start-bot.sh"), str(working)] if bot else
                   [str(release.native_path / "keepalive-all.sh")] if phase == "producers" else
                   [str(release.cli_path), "plane", "daemon"])
        argv = admission.wrap_unit_argv(env, unit=stem, phase=phase,
                                        mode="oneshot" if phase == "producers" else "exec", argv=command)
        files = {stem + ".plist": (plistlib.dumps({"Label": stem, "WorkingDirectory": str(working),
                    "EnvironmentVariables": env, "ProgramArguments": list(argv)}), 0o644),
                 stem + ".service": ((f"[Service]\nWorkingDirectory={working}\n"
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
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(admission.RuntimeIdentity, "current", classmethod(lambda cls:
        admission.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(directory / paths.interpreter))
    monkeypatch.setattr(activation, "_probe", host.probe)
    # The scan owner separately proves assembly/commit semantics; this authored
    # output fixture checks ordering and the real operation-context SQL reader.
    monkeypatch.setattr(activation, "_registry_scan", host.registry_scan)
    return root, release, plan, host


def test_cold_bootstrap_uses_real_sql_config_and_serial_starts_before_timers(cold):
    root, release, plan, host = cold
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
    before = list(host.starts)
    with pytest.raises(state.ActivationError, match="existing release selection"):
        activation.bootstrap_activation(root, "again", plan.plan_id, host.directory, adapter=host)
    assert host.starts == before


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
    assert "producers_resumed" not in record.body["completed"]
    assert not (host.directory / "claudlobby-keepalive.timer").exists()
    assert read_migration(root, "cold")["result"] is not None
    if failure != "worker":
        assert host.starts == ["claudlobby-plane-daemon.service"]
    else:
        assert host.starts == ["claudlobby-plane-daemon.service", "com.example.manager.service", "com.example.worker.service"]


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
