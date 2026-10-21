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
from claudlobby.supervision_inventory import _catalog, EnrollmentInventory, collect_enrollment
from tests.test_activation_units import empty_enrollment, enrollment, RecordedAdapter, _complete, _pause_all
from tests.test_releases import installed, r


def unit_properties(directory, target, declaration, *, active="inactive", unit_file_state="disabled") -> str:
    """``systemctl show`` for a loaded candidate unit, as the inventory reads it."""
    properties = {"Id": target, "FragmentPath": str(directory / target),
                  "DropInPaths": "", "NeedDaemonReload": "no",
                  "LoadState": "loaded", "ActiveState": active, "UnitFileState": unit_file_state,
                  "TriggeredBy": "", "ExecStart": "recorded candidate command",
                  "WorkingDirectory": str(declaration.working_directory),
                  "Environment": shlex.join(f"{key}={value}" for key, value in declaration.environment),
                  "Triggers": declaration.service or ""}
    return ''.join(f"{key}={value}\n" for key, value in properties.items())


class Manager(RecordedAdapter):
    def __init__(self, inventory, plan):
        super().__init__(inventory)
        self.directories = _catalog(inventory.catalog)[2]
        self.directory = self.directories[0]
        self.declarations = {d.source.name: (d, item) for d, item in planned_units(plan, self.manager)}
        self.loaded_foreign = set()

    def call(self, function, *args, **kwargs):
        if function == "svc_inventory_catalog":
            text = f"manager\t{self.manager}\n" + "".join(f"directory\t{path}\n" for path in self.directories)
            if self.manager == "Darwin":
                text += "domain\tgui/501\nPID\tStatus\tLabel\n"
            else:
                files = sorted(self.directory.iterdir()) if self.directory.exists() else []
                text += ''.join(f"installed\t{p.name}\n" for p in files
                                if p.suffix in (".service", ".timer"))
                text += ''.join(f"loaded\t{name}\n" for name in sorted(set(self.states) | self.loaded_foreign))
        elif function == "svc_inventory_state":
            file, target = Path(args[0]), args[1]
            text = ("unchanged unloaded inactive" if self.manager == "Darwin" else
                    self.states.get(target, "disabled loaded inactive" if file.exists() or target in self.loaded_foreign
                                    else "not-found not-found inactive"))
        elif function == "svc_inventory_properties":
            text = unit_properties(self.directory, args[0], self.declarations[args[0]][0])
        else:
            return super().call(function, *args, **kwargs)
        self.calls.append((function, tuple(map(str, args))))
        return subprocess.CompletedProcess([function, *args], 0, text, "")


def _candidate_plan(installed, *, install_extra=""):
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
                 stem + ".service": ((f"[Service]\nWorkingDirectory={root}\nExecStart={runtime_admission.unit_systemd_command(argv)}\n" +
                     ("" if timed else "[Install]\nWantedBy=default.target\n" + install_extra)).encode(), 0o640)}
        if timed:
            files[stem + ".timer"] = (f"[Timer]\nUnit={stem}.service\n[Install]\nWantedBy=timers.target\n".encode(), 0o640)
        destination = root / "runtime/generated"
        items = unit_family(files, destination=destination, scope=scope, phase=phase,
                            release_id=candidate.release_id, fleet="alpha" if scope != "host" else None,
                            bot="member" if scope == "bot" else None, enroll=enroll)
        for name, (content, mode) in files.items():
            builder.file(destination / name, content, mode=mode)
        builder.effects["units"].extend(items)
    plan = builder.seal()
    return plan, old, candidate


@pytest.fixture
def case(enrollment, installed, monkeypatch, request):
    inventory, phases, _, _, foreign, wants = enrollment
    plan, old, candidate = _candidate_plan(installed, install_extra=getattr(request, "param", ""))
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


@pytest.fixture(params=["Darwin", "Linux"])
def new_native_case(installed, tmp_path, monkeypatch, request):
    r.seal_release(installed[0], installed[1], installed[2])
    plan, old, candidate = _candidate_plan(installed)
    home = tmp_path / "private-home"
    home.mkdir()
    directory = home / ("Library/LaunchAgents" if request.param == "Darwin" else ".config/systemd/user")
    catalog = f"manager\t{request.param}\ndirectory\t{directory}\n"
    if request.param == "Darwin":
        catalog += "domain\tgui/501\nPID\tStatus\tLabel\n"
    inventory = EnrollmentInventory(plan.data_root, request.param, catalog, (), (), (), (), True)
    adapter = Manager(inventory, plan)
    inventory = collect_enrollment(plan.data_root, (), bootstrap_empty=True, adapter=adapter).require_complete()

    def validate(release, declaration, item, content):
        assert release.release_id == candidate.release_id
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
    monkeypatch.setattr(runtime_admission, "validate_unit_admission", validate)
    case = inventory, {phase: [] for phase in publish.PHASES}, plan, adapter, None, None, old, []
    return case, home


@pytest.mark.parametrize("existing_parent", [False, True])
@pytest.mark.parametrize("mutable_child", [False, True])
def test_native_phase_publication_has_one_shared_parent_owner(new_native_case, existing_parent, mutable_child):
    case, home = new_native_case
    inventory, _, plan, adapter, _, _, _, _ = case
    directory = adapter.directory
    if existing_parent:
        directory.mkdir(parents=True)
        (directory / "foreign-data").write_bytes(b"preserve existing data")
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=directory, adapter=adapter)
        phase_rows = [json.loads((config_install.read_config_install(store.root,
                      publish.journal_id("cutover", phase)).directory / "journal.json").read_text())["journal"]["rows"]
                      for phase in publish.PHASES]
        publication_order = ("ingest", "bots", "producers")
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started"), ("producers", "producers_resumed")):
            if phase == "producers":
                _complete(store, "verified")
            store.begin("cutover", step)
            publish.install_candidate_units(store, "cutover", phase, adapter=adapter)
            for entry in plans[0].effects["entries"]:
                installed_file = Path(entry["installed"])
                if publication_order.index(entry["phase"]) <= publication_order.index(phase):
                    assert installed_file.read_bytes() == plan.blob(entry["after"]["sha256"])
                    assert installed_file.stat().st_mode & 0o777 == entry["after"]["mode"]
                else:
                    assert not installed_file.exists()
            if phase != "producers":
                _complete_pending(store, step)
        assert not any(row["ensure"] for rows in phase_rows for row in rows)
        if mutable_child:
            (directory / "runtime-data").write_bytes(b"preserve new data")
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("candidate_units_removed")]:
            _complete(store, step)
        store.begin("cutover", "candidate_units_removed")
        publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert not any(Path(entry["installed"]).exists() for entry in plans[0].effects["entries"])
        directories = config_install.read_config_install(store.root, publish.journal_id("cutover", "directories"))
        assert directories.status == "rolled_back"
        if mutable_child:
            assert (directory / "runtime-data").read_bytes() == b"preserve new data"
            assert existing_parent or directory in directories.retained_directories
        if existing_parent:
            assert (directory / "foreign-data").read_bytes() == b"preserve existing data"
        elif not mutable_child:
            assert list(home.iterdir()) == []
        assert not any(call[0] in ("svc_activation_start", "svc_enroll", "svc_activation_resume") for call in adapter.calls)


def test_native_parent_publication_recovers_before_preparing_phase_journals(new_native_case, monkeypatch):
    case, _ = new_native_case
    inventory, _, _, adapter, _, _, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        original_replace = config_install._replace

        def interrupted(source, target):
            original_replace(source, target)
            if target == adapter.directory:
                raise InterruptedError("after native parent publication")

        with monkeypatch.context() as patch:
            patch.setattr(config_install, "_replace", interrupted)
            with pytest.raises(InterruptedError, match="native parent publication"):
                publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        directory_journal = config_install.read_config_install(store.root, publish.journal_id("cutover", "directories"))
        assert directory_journal.status == "applying"
        assert adapter.directory.is_dir() and not list(adapter.directory.iterdir())
        assert not any((store.root / "state/activations" / publish.journal_id("cutover", phase)).exists()
                       for phase in publish.PHASES)
        publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                             install_directory=adapter.directory, adapter=adapter)
        assert config_install.read_config_install(store.root, publish.journal_id("cutover", "directories")).status == "applied"
        assert all(config_install.read_config_install(store.root, publish.journal_id("cutover", phase)).status == "prepared"
                   for phase in publish.PHASES)


@pytest.mark.parametrize("collision", ["symlink", "file"])
def test_native_install_parent_cannot_redirect_or_replace_foreign_data(new_native_case, collision):
    case, home = new_native_case
    inventory, _, _, adapter, _, _, _, _ = case
    foreign = home / "foreign"
    foreign.mkdir()
    (foreign / "data").write_bytes(b"foreign bytes")
    adapter.directory.parent.mkdir(parents=True)
    if collision == "symlink":
        adapter.directory.symlink_to(foreign, target_is_directory=True)
    else:
        adapter.directory.write_bytes(b"foreign bytes")
    with state.locked_activation(inventory.data_root) as store:
        refusal = state.ActivationError if collision == "symlink" else NotADirectoryError
        with pytest.raises(refusal):
            prepared(case, store)
            publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                 install_directory=adapter.directory, adapter=adapter)
        assert not (store.root / "state/activations" / publish.journal_id("cutover", "directories")).exists()
    assert (foreign / "data").read_bytes() == b"foreign bytes"
    if collision == "symlink":
        assert adapter.directory.readlink() == foreign
    else:
        assert adapter.directory.read_bytes() == b"foreign bytes"


def test_directory_publication_refuses_an_unrelated_ensure(new_native_case):
    case, home = new_native_case
    inventory, _, plan, adapter, _, _, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        foreign = home / "unrelated"
        builder = ConfigPlanBuilder(store.root, plan.release_id, plan.release_seal, plan.fleets, effects={})
        builder.directory(foreign)
        with pytest.raises(state.ActivationError, match="unowned replacements"):
            publish._check_publication_changes(builder.seal(), plans[0].effects["entries"], "directories")
        assert not foreign.exists()


def test_prepared_native_unit_collision_is_never_adopted(new_native_case):
    case, _ = new_native_case
    inventory, _, _, adapter, _, _, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        entry = next(entry for entry in plans[0].effects["entries"] if entry["phase"] == "ingest")
        foreign = Path(entry["installed"])
        foreign.write_bytes(b"foreign bytes")
        store.begin("cutover", "ingest_started")
        with pytest.raises(state.ActivationError, match="foreign or changed candidate collision"):
            publish.install_candidate_units(store, "cutover", "ingest", adapter=adapter)
        assert foreign.read_bytes() == b"foreign bytes"
        assert config_install.read_config_install(store.root, publish.journal_id("cutover", "ingest")).status == "prepared"


def test_bootstrap_candidate_publishes_new_units_then_removes_only_its_files(empty_enrollment, installed, monkeypatch):
    inventory, phases, _, empty_adapter, foreign, _ = empty_enrollment
    plan, recovery, candidate = _candidate_plan(installed)
    adapter = Manager(inventory, plan)
    original = foreign.read_bytes()
    checks = []
    def validate(release, declaration, item, content):
        assert release.release_id == candidate.release_id
        assert hashlib.sha256(content).hexdigest() == item["sha256"]
        checks.append(declaration.source.name)
    monkeypatch.setattr(runtime_admission, "validate_unit_admission", validate)
    with state.locked_activation(inventory.data_root) as store:
        store.prepare("cutover", plan, recovery_release_id=recovery.release_id, enrollment_digest=inventory.digest)
        config_install.prepare_config(plan, "generated-config")
        pause = activation_units.prepare_unit_pause(store, "cutover", inventory, phases, adapter=empty_adapter)
        assert pause.plan("bots").effects["original_release_id"] is None
        _pause_all(store, empty_adapter)
        for step in ("backup_saved", "migration_applied"):
            _complete(store, step)
        store.begin("cutover", "selection_switched")
        store.select("cutover")
        store.begin("cutover", "configuration_applied")
        config_install.apply_config(store.root, "generated-config")
        _complete_pending(store, "configuration_applied")
        original_replace = config_install._replace
        interrupted_target = adapter.directory / "default.target.wants"
        def interrupted(source, destination):
            original_replace(source, destination)
            if destination == interrupted_target:
                raise InterruptedError("after owned publication rename")
        with monkeypatch.context() as patch:
            patch.setattr(config_install, "_replace", interrupted)
            with pytest.raises(InterruptedError):
                publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        assert all(not entry["original"] for entry in plans[0].effects["entries"])
        assert set(checks) == {d.source.name for d, item in planned_units(plan, "Linux") if item["enroll"]}
        store.begin("cutover", "ingest_started")
        interrupted_target = adapter.directory / "default.target.wants/collector.service"
        with monkeypatch.context() as patch:
            patch.setattr(config_install, "_replace", interrupted)
            with pytest.raises(InterruptedError):
                publish.install_candidate_units(store, "cutover", "ingest", adapter=adapter)
        assert interrupted_target.is_symlink()
        result = publish.install_candidate_units(store, "cutover", "ingest", adapter=adapter)
        assert result.targets == ("collector.service",)
        assert (adapter.directory / "collector.service").is_file()
        link = adapter.directory / "default.target.wants/collector.service"
        assert link.readlink() == adapter.directory / "collector.service"
        foreign_link = link.parent / "foreign.service"
        foreign_link.symlink_to("../foreign.service")
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("candidate_units_removed")]:
            _complete(store, step)
        store.begin("cutover", "candidate_units_removed")
        publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert not (adapter.directory / "collector.service").exists()
        assert not link.is_symlink()
        assert foreign_link.readlink() == Path("../foreign.service")
        assert foreign.read_bytes() == original
        assert not any(call[0] in ("svc_activation_resume", "svc_enroll") for call in adapter.calls)


def test_original_pause_runtime_mask_is_owned_only_for_exact_original_target(
        enrollment, installed, monkeypatch, tmp_path):
    inventory, phases, _, _, foreign, wants = enrollment
    masks = tmp_path / "private-runtime/systemd/user"
    masks.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(masks.parent.parent))
    config = _catalog(inventory.catalog)[2][0]
    # The runtime directory is a lower-priority verified search path.
    inventory = replace(inventory, catalog=inventory.catalog.replace(
        f"directory\t{config}\n", f"directory\t{config}\ndirectory\t{masks}\n", 1))
    plan, old, _ = _candidate_plan(installed)
    adapter = Manager(inventory, plan)
    monkeypatch.setattr(runtime_admission, "validate_unit_admission", lambda *_: None, raising=False)
    case = (inventory, phases, plan, adapter, foreign, wants, old, [])
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        mask = masks / "scheduled.service"

        def refused(match):
            with pytest.raises(state.ActivationError, match=match):
                publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
            assert not (store.root / "state/activations" / publish.journal_id("cutover", "directories")).exists()

        mask.symlink_to(config / "scheduled.service")  # a wrong link is never the pause's mask
        refused("foreign or changed candidate collision")
        mask.unlink()
        (masks / "added.service").symlink_to("/dev/null")  # a new identity has no original pause
        refused("foreign or changed candidate collision")
        (masks / "added.service").unlink()
        # The pause's own mask on the exact original, still masked and inactive.
        mask.symlink_to("/dev/null")
        assert adapter.states["scheduled.service"] == "masked-runtime masked inactive"
        plans = publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        assert len(plans) == 3 and mask.is_symlink()  # preparation leaves it untouched
        assert not any(call[0] in ("svc_activation_reload", "svc_activation_clear_runtime_mask")
                       for call in adapter.calls)
        recorded = adapter.call

        def native(function, *args, **kwargs):
            if function == "svc_activation_reload":
                # The published file now supersedes the mask: a loaded, inactive paired service.
                adapter.states["scheduled.service"] = "static loaded inactive"
            elif function == "svc_activation_clear_runtime_mask":
                assert args[1] == "scheduled.service"
                (masks / args[1]).unlink()  # the native owner's exact unmask
            return recorded(function, *args, **kwargs)

        adapter.call = native
        for step, phase in (("ingest_started", "ingest"), ("bots_started", "bots")):
            store.begin("cutover", step)
            publish.install_candidate_units(store, "cutover", phase, adapter=adapter)
            _complete_pending(store, step)
            assert mask.is_symlink()  # another phase's mask stays until its own publication
        _complete(store, "verified")
        store.begin("cutover", "producers_resumed")
        publish.install_candidate_units(store, "cutover", "producers", adapter=adapter)
        # The paired service is never started directly; publication removed its mask.
        assert not mask.exists() and not mask.is_symlink()
        assert [call[0] for call in adapter.calls if call[0] in (
            "svc_activation_reload", "svc_activation_clear_runtime_mask")] == [
            "svc_activation_reload", "svc_activation_clear_runtime_mask"]
    assert not any(call[0] == "svc_activation_start" for call in adapter.calls)


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
        added_link = adapter.directory / "default.target.wants/added.service"
        timer_link = adapter.directory / "timers.target.wants/scheduled.timer"
        assert added_link.readlink() == adapter.directory / "added.service"
        assert timer_link.readlink() == adapter.directory / "scheduled.timer"
        assert not (wants / "member.service").is_symlink()  # preserve its original disabled state
        assert not (wants / "scheduled.service").is_symlink()  # paired oneshot remains static
        assert (adapter.directory / "member.service").stat().st_mode & 0o777 == 0o640
        assert not (adapter.directory / "timers.target.wants/clock.timer").is_symlink()
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
        added_link.unlink()
        added_link.symlink_to(foreign)
        with pytest.raises(state.ActivationError, match="enablement link changed"):
            publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert added_link.resolve() == foreign
        added_link.unlink()
        added_link.symlink_to(changed)
        removed = publish.remove_candidate_units(store, "cutover", adapter=adapter)
        assert len(removed) == 3
        assert publish.remove_candidate_units(store, "cutover", adapter=adapter) == removed
        for declaration, item in planned_units(plan, "Linux"):
            assert not (adapter.directory / declaration.source.name).exists()
        assert foreign.read_bytes() == foreign_bytes
        assert (wants / "collector.service").readlink() == wanted_link
        assert not added_link.is_symlink() and not timer_link.is_symlink()
        assert (timer_link.parent / "clock.timer").readlink() == Path("../clock.timer")
        assert not any(call[0] in ("svc_activation_resume", "svc_enroll") for call in adapter.calls)


def test_omitted_fleet_timer_retires_only_its_owned_enablement(case):
    inventory, _, _, adapter, _, _, _, _ = case
    timer_link = adapter.directory / "timers.target.wants/clock.timer"
    foreign_link = timer_link.parent / "foreign.timer"
    foreign_link.symlink_to("../foreign.timer")
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        plans = publish.prepare_candidate_enrollment(
            store, "cutover", configuration_journal="generated-config",
            install_directory=adapter.directory, adapter=adapter)
        assert timer_link.is_symlink()
        assert any(change.target == str(timer_link) and change.after == {"kind": "absent"}
                   for change in plans[0].changes)
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started")):
            store.begin("cutover", step)
            publish.install_candidate_units(store, "cutover", phase, adapter=adapter)
            _complete_pending(store, step)
        _complete(store, "verified")
        store.begin("cutover", "producers_resumed")
        publish.install_candidate_units(store, "cutover", "producers", adapter=adapter)
        assert not timer_link.exists() and not timer_link.is_symlink()
        assert foreign_link.readlink() == Path("../foreign.timer")


def test_guard_and_foreign_loaded_collision_refuse_before_publication(case, monkeypatch):
    inventory, _, _, adapter, foreign, wants, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        def unguarded(*args):
            raise state.ActivationError("unguarded candidate")
        with monkeypatch.context() as patch:
            patch.setattr(runtime_admission, "validate_unit_admission", unguarded)
            with pytest.raises(state.ActivationError, match="unguarded"):
                publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                     install_directory=adapter.directory, adapter=adapter)
        link = wants / "collector.service"
        old_target = link.readlink()
        link.unlink()
        link.symlink_to(foreign)
        with pytest.raises(state.ActivationError, match="misdirected native enablement"):
            publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                 install_directory=adapter.directory, adapter=adapter)
        assert link.resolve() == foreign
        link.unlink()
        link.symlink_to(old_target)
        adapter.loaded_foreign.add("added.service")
        with pytest.raises(state.ActivationError, match="foreign loaded candidate"):
            publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                 install_directory=adapter.directory, adapter=adapter)
        assert not (adapter.directory / "collector.service").exists()
        assert not list((store.root / "state/activations").glob("enrollment-*/config"))


@pytest.mark.parametrize("case", ["Also=foreign.service\n"], indirect=True)
def test_unsupported_install_semantics_refuse_before_publication(case):
    inventory, _, _, adapter, _, _, _, _ = case
    with state.locked_activation(inventory.data_root) as store:
        prepared(case, store)
        with pytest.raises(state.ActivationError, match="unsupported unit Install"):
            publish.prepare_candidate_enrollment(store, "cutover", configuration_journal="generated-config",
                                                 install_directory=adapter.directory, adapter=adapter)
        assert not (adapter.directory / "collector.service").exists()
        assert not list((store.root / "state/activations").glob("enrollment-*/config"))
