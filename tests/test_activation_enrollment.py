"""Private native files plus recorded manager state; no native service calls."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess

import pytest

from claudlobby import activation_enrollment as publish, activation_state as state
from claudlobby import activation_units, config_install, runtime_admission
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import planned_units, unit_family
from tests.test_activation_units import enrollment, RecordedAdapter, _complete, _pause_all
from tests.test_releases import installed, r


class Manager(RecordedAdapter):
    def __init__(self, inventory, plan):
        super().__init__(inventory)
        self.directory = Path(inventory.units[0].installed[0].path).parent
        self.declarations = {d.source.name: (d, item) for d, item in planned_units(plan, "Linux")}
        self.loaded_foreign = set()

    def call(self, function, *args):
        if function == "svc_inventory_catalog":
            text = f"manager\tLinux\ndirectory\t{self.directory}\n"
            text += ''.join(f"installed\t{p.name}\n" for p in sorted(self.directory.glob('*.service')))
            text += ''.join(f"loaded\t{name}\n" for name in sorted(set(self.states) | self.loaded_foreign))
        elif function == "svc_inventory_state":
            file, target = Path(args[0]), args[1]
            text = self.states.get(target, "disabled loaded inactive" if file.exists() or target in self.loaded_foreign
                                   else "not-found not-found inactive")
        elif function == "svc_inventory_properties":
            target = args[0]
            declaration, metadata = self.declarations[target]
            properties = {"Id": target, "FragmentPath": str(self.directory / target),
                          "DropInPaths": "", "NeedDaemonReload": "no",
                          "LoadState": "loaded", "ActiveState": "inactive", "UnitFileState": "disabled",
                          "TriggeredBy": "", "ExecStart": "recorded candidate command",
                          "WorkingDirectory": str(declaration.working_directory),
                          "Environment": shlex.join(f"{key}={value}" for key, value in declaration.environment),
                          "Triggers": declaration.service or ""}
            text = ''.join(f"{key}={value}\n" for key, value in properties.items())
        else:
            return super().call(function, *args)
        self.calls.append((function, tuple(map(str, args))))
        return subprocess.CompletedProcess([function, *args], 0, text, "")


@pytest.fixture
def case(enrollment, installed, monkeypatch):
    inventory, phases, _, _, foreign, wants = enrollment
    root, inputs, paths, _, original_dir = installed
    old = r.read_release(root, inputs.release_id)
    candidate_inputs = replace(inputs, source_revision="c" * 40)
    candidate_dir = r.release_path(root, candidate_inputs.release_id)
    shutil.copytree(original_dir, candidate_dir)
    (candidate_dir / r.MANIFEST).unlink()
    artifact = candidate_dir / paths.artifact
    metadata = json.loads(artifact.read_text())
    metadata["source_revision"] = candidate_inputs.source_revision
    artifact.write_text(json.dumps(metadata))
    (candidate_dir / paths.cli).write_text(f"#!{candidate_dir / paths.interpreter}\n")
    candidate = r.seal_release(root, candidate_inputs, paths)
    builder = ConfigPlanBuilder(root, candidate.release_id, candidate.seal_sha256, ("alpha",), effects={"units": []})
    for stem, scope, phase, timed, enroll in (
            ("collector", "host", "ingest", False, True),
            ("member", "bot", "bots", False, True),
            ("scheduled", "fleet", "producers", True, True),
            ("added", "host", "producers", False, True),
            ("dormant", "host", "producers", False, False)):
        env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
               "CLAUDLOBBY_RELEASE_ID": candidate.release_id,
               "CLAUDLOBBY_NATIVE_DIR": str(candidate.native_path),
               "CLAUDLOBBY_LIBRARY_DIR": str(candidate.directory / "library"),
               "CLAUDLOBBY_CLI": str(candidate.cli_path), "CLAUDLOBBY_ARTIFACT_ID": candidate.inputs.artifact_id}
        argv = runtime_admission.wrap_unit_argv(env, unit=stem, phase=phase,
                    mode="oneshot" if timed else "exec", argv=[str(candidate.cli_path), "fixture"])
        files = {stem + ".plist": (plistlib.dumps({"Label": stem, "WorkingDirectory": str(root),
                    "EnvironmentVariables": env, "ProgramArguments": list(argv)}), 0o640),
                 stem + ".service": (f"[Service]\nWorkingDirectory={root}\nExecStart={runtime_admission.unit_systemd_command(argv)}\n".encode(), 0o640)}
        if timed:
            files[stem + ".timer"] = (f"[Timer]\nUnit={stem}.service\n".encode(), 0o640)
        destination = root / "runtime/generated"
        items = unit_family(files, destination=destination, scope=scope, phase=phase,
                            release_id=candidate.release_id, fleet="alpha" if scope != "host" else None,
                            bot="member" if scope == "bot" else None, enroll=enroll)
        for name, (content, mode) in files.items():
            builder.file(destination / name, content, mode=mode)
        builder.effects["units"].extend(items)
    plan = builder.seal()
    adapter = Manager(inventory, plan)
    guard_checks = []

    # The shared guard parser/seal owner has separate integration coverage.
    # This test isolates the filesystem coordinator's mandatory collaborator:
    # every exact enrolled definition must be accepted before publication.
    def validate(release, declaration, item, content):
        assert release.release_id == candidate.release_id
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        guard_checks.append(declaration.source.name)
    monkeypatch.setattr(runtime_admission, "validate_unit_admission", validate, raising=False)
    return inventory, phases, plan, adapter, foreign, wants, old, guard_checks


def prepared(case, store):
    inventory, phases, plan, adapter, _, _, old, _ = case
    store.prepare("cutover", plan, recovery_release_id=old.release_id, enrollment_digest=inventory.digest)
    config_install.prepare_config(plan, "generated-config")
    activation_units.prepare_unit_pause(store, "cutover", inventory, phases, adapter=adapter)
    _pause_all(store, adapter)
    for step in ("backup_saved", "migration_applied"):
        _complete(store, step)
    store.begin("cutover", "selection_switched")
    store.select("cutover")
    store.begin("cutover", "configuration_applied")
    config_install.apply_config(store.root, "generated-config")
    _complete_pending(store, "configuration_applied")


def _complete_pending(store, step):
    store.complete("cutover", step, evidence_digest="b" * 64)


def test_phase_publication_retry_and_owned_cleanup_preserve_foreign(case, monkeypatch):
    inventory, _, plan, adapter, foreign, wants, _, checks = case
    foreign_bytes = foreign.read_bytes()
    wanted_link = (wants / "collector.service").readlink()
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        assert len(plans) == 3
        assert set(checks) == {declaration.source.name for declaration, item in planned_units(plan, "Linux") if item["enroll"]}
        with pytest.raises(state.ActivationError, match="admitted start phase"):
            publish.install_candidate_units(store, "cutover", "bots", adapter=adapter)
        assert not (adapter.directory / "added.service").exists()
        assert not (adapter.directory / "member.service").exists()
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started"), ("producers", "producers_resumed")):
            if phase == "producers":
                _complete(store, "verified")
            store.begin("cutover", step)
            if phase == "ingest":
                original_replace = config_install._replace
                target = adapter.directory / "collector.service"
                def interrupted(source, destination):
                    original_replace(source, destination)
                    if destination == target:
                        raise InterruptedError("after candidate file publication")
                with monkeypatch.context() as patch:
                    patch.setattr(config_install, "_replace", interrupted)
                    with pytest.raises(InterruptedError):
                        publish.install_candidate_units(store, "cutover", phase, adapter=adapter)
                assert target.read_bytes() == (inventory.data_root / "runtime/generated/collector.service").read_bytes()
                assert publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                    install_directory=adapter.directory, adapter=adapter) == plans
            result = publish.install_candidate_units(store, "cutover", phase, adapter=adapter)
            assert result.operation == "published"
            if phase != "producers":
                assert not (adapter.directory / "added.service").exists()
                _complete_pending(store, step)
        assert not (adapter.directory / "dormant.service").exists()
        assert (adapter.directory / "member.service").stat().st_mode & 0o777 == 0o640
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("candidate_units_removed")]:
            _complete(store, step)
        store.begin("cutover", "candidate_units_removed")
        changed = adapter.directory / "added.service"
        original = changed.read_bytes()
        changed.write_bytes(b"foreign replacement")
        with pytest.raises(state.ActivationError, match="changed candidate collision"):
            publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert (adapter.directory / "member.service").exists()
        changed.write_bytes(original)
        removed = publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert len(removed) == 3
        assert publish.remove_candidate_units(store, "cutover", adapter=adapter) == removed
        for declaration, item in planned_units(plan, "Linux"):
            assert not (adapter.directory / declaration.source.name).exists()
        assert foreign.read_bytes() == foreign_bytes
        assert (wants / "collector.service").readlink() == wanted_link
        assert not any(call[0] in ("svc_activation_resume", "svc_enroll") for call in adapter.calls)


def test_guard_and_foreign_loaded_collision_refuse_before_publication(case, monkeypatch):
    inventory, _, _, adapter, _, _, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        def unguarded(*args):
            raise state.ActivationError("unguarded candidate")
        with monkeypatch.context() as patch:
            patch.setattr(runtime_admission, "validate_unit_admission", unguarded)
            with pytest.raises(state.ActivationError, match="unguarded"):
                publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        adapter.loaded_foreign.add("added.service")
        with pytest.raises(state.ActivationError, match="foreign loaded candidate"):
            publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                 install_directory=adapter.directory, adapter=adapter)
        assert not (adapter.directory / "collector.service").exists()
        assert not list((store.root / "state/activations").glob("enrollment-*/config"))
