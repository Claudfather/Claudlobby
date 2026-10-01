"""Original-unit pause/recovery with private files and a recording adapter."""

from dataclasses import replace
import json
import os
from pathlib import Path
import plistlib
import subprocess

import pytest

from claudlobby import activation
from claudlobby import activation_state as state
from claudlobby import activation_units as units
from claudlobby import config_install
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.supervision_inventory import Adapter, EnrollmentInventory, EnrolledUnit, FileSnapshot, UnitDeclaration, collect_enrollment
from tests.test_releases import installed, r
from tests.test_supervision_inventory import Observations, observed_print


class RecordedAdapter:
    def __init__(self, inventory):
        self.manager = inventory.manager
        self.original = {unit.target: " ".join(dict(unit.properties)[key] for key in
                         ("UnitFileState", "LoadState", "ActiveState")) for unit in inventory.units}
        scheduled = {unit.declaration.service for unit in inventory.units
                     if unit.target.endswith(".timer") and unit.declaration.service}
        # A timer-owned service is paused from and restored to inactive.
        self.native = {target: " ".join(saved.split()[:2] + ["inactive"]) if target in scheduled else saved
                       for target, saved in self.original.items()}
        self.states = dict(self.original)
        self.files = {unit.target: unit.installed[0] for unit in inventory.units}
        self.calls = []
        self.refusal = {}
        self.pause_failure = None
        self.resume_failure = None
        self.overrides = {}
        self.hidden_masks = set()
        self.binding_changed = False

    def read(self, function, *args):
        result = self.call(function, *args)
        assert result.returncode == 0
        return result.stdout

    def call(self, function, *args, timeout=None):
        # Only the native pause waits for a finite stop; other calls keep the default.
        assert timeout == (120 if function == "svc_activation_pause" else None)
        if function == "svc_inventory_disabled":
            output = '\n\tdisabled services = {\n' + ''.join(f'\t\t"{key}" => {value}\n' for key, value in self.overrides.items()) + '\t}\n'
            return subprocess.CompletedProcess([function, *args], 0, output, "")
        if function == "svc_inventory_properties":
            target = args[0]
            source = plistlib.loads(self.files[target].content)
            output = observed_print(target, source, Path(self.files[target].path))
            if self.binding_changed:
                output = output.replace("\tprogram = ", "\tprogram = /foreign", 1)
            return subprocess.CompletedProcess([function, *args], 0, output, "")
        if function == "svc_activation_reload":
            assert not args
            self.calls.append((function, None))
            return subprocess.CompletedProcess([function], 0, "", "")
        file, target = Path(args[0]), args[1]
        self.calls.append((function, target))
        rc, output = 0, ""
        if function == "svc_activation_assert_external":
            assert args[2] == str(os.getpid())
            rc = self.refusal.get(target, 0)
        elif function in ("svc_activation_snapshot", "svc_inventory_state"):
            if function == "svc_activation_snapshot":
                assert file.is_file()
            output = self.states[target] + "\n"
        elif function == "svc_activation_pause":
            assert not file.exists() and not file.is_symlink(), "adapter saw unparked installed source"
            assert args[2] == self.native[target]
            if target == self.pause_failure:
                rc = 3
            else:
                self.states[target] = "unchanged unloaded inactive" if self.manager == "Darwin" else "masked-runtime masked inactive"
        elif function == "svc_activation_resume":
            assert FileSnapshot.read(file) == self.files[target], "resume happened before exact restoration"
            assert args[2] == self.native[target]
            if target == self.resume_failure:
                rc = 3
            else:
                self.states[target] = self.native[target]
        elif function == "svc_activation_clear_runtime_mask":
            # Abort restores from the native saved state; publication from its fresh snapshot.
            assert args[2] in (self.native[target], self.states[target]) and file.is_file()
            output = "removed\n" if target in self.hidden_masks else "absent\n"
            self.hidden_masks.discard(target)
        else:
            raise AssertionError(f"unexpected native operation: {function}")
        return subprocess.CompletedProcess([function, *args], rc, output, "recorded unknown" if rc else "")


@pytest.fixture
def enrollment(installed, tmp_path):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    directory = tmp_path / "private-home/.config/systemd/user"
    directory.mkdir(parents=True)
    generated = root / "runtime/generated"
    generated.mkdir(parents=True)
    native_env = tuple({"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root / "local/alpha"),
                        "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
                        "CLAUDLOBBY_LIBRARY_DIR": str(release.directory / "library"),
                        "CLAUDLOBBY_CLI": str(release.cli_path),
                        "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id}.items())
    entries = []
    phases = {"producers": ["clock.timer", "scheduled.service"],
              "bots": ["member.service"], "ingest": ["collector.service"]}
    for name, scope in (("clock.timer", "fleet"), ("scheduled.service", "fleet"),
                        ("member.service", "bot"), ("collector.service", "host")):
        source = generated / name
        source.write_text(f"reviewed original {name}\n")
        source.chmod(0o640)
        target = directory / name
        if scope == "bot":
            target.symlink_to(os.path.relpath(source, target.parent))
        else:
            target.write_bytes(source.read_bytes())
            target.chmod(0o640)
        properties = {"UnitFileState": "disabled" if scope == "bot" else "enabled",
                      "LoadState": "loaded", "ActiveState": "inactive" if scope == "bot" else "active"}
        declaration = UnitDeclaration(source, scope, root, release.release_id, native_env,
                                      "alpha" if scope != "host" else None,
                                      "member" if scope == "bot" else None,
                                      "scheduled.service" if name.endswith(".timer") else None)
        entries.append(EnrolledUnit(declaration, name, FileSnapshot.read(source),
                                    (FileSnapshot.read(target),), tuple(properties.items())))
    foreign = directory / "foreign.service"
    foreign.write_text("unrelated unit remains untouched\n")
    wants = directory / "default.target.wants"
    wants.mkdir()
    (wants / "collector.service").symlink_to("../collector.service")
    timer_wants = directory / "timers.target.wants"
    timer_wants.mkdir()
    (timer_wants / "clock.timer").symlink_to("../clock.timer")
    catalog = f"manager\tLinux\ndirectory\t{directory}\n" + "".join(
        f"loaded\t{unit.target}\ninstalled\t{unit.target}\n" for unit in entries)
    inventory = EnrollmentInventory(root, "Linux", catalog, tuple(entries),
        tuple(unit.installed[0] for unit in entries) + (FileSnapshot.read(foreign),),
        (str(foreign),), ())
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, ("alpha",), effects={}).seal()
    return inventory, phases, plan, RecordedAdapter(inventory), foreign, wants


def _prepare(store, inventory, phases, plan, adapter, *, install_directory=None):
    store.prepare("cutover", plan, recovery_release_id=plan.release_id,
                  enrollment_digest=inventory.digest, legacy_source=inventory.legacy_source,
                  install_directory=install_directory)
    return units.prepare_unit_pause(store, "cutover", inventory, phases, adapter=adapter)


def test_prepared_snapshot_refusal_can_cancel_only_unstarted_journals(enrollment):
    inventory, phases, plan, adapter, _, _ = enrollment
    # A timer tick finished after the inventory: no file or load-state drift.
    adapter.states["scheduled.service"] = "enabled loaded inactive"
    with state.locked_activation(inventory.data_root) as store:
        store.prepare("cutover", plan, recovery_release_id=plan.release_id,
                      enrollment_digest=inventory.digest)
        config_install.prepare_config(plan, "cutover")
        # Another unit still has an actual ownership change, before any pause.
        adapter.states["member.service"] = "enabled loaded inactive"
        with pytest.raises(state.ActivationError, match="native enrollment changed"):
            units.prepare_unit_pause(store, "cutover", inventory, phases, adapter=adapter)
        assert store.cancel_prepared("cutover").status == "rolled_back"
    assert all(call[0] != "svc_activation_pause" for call in adapter.calls)


def test_producer_tick_churn_does_not_invalidate_frozen_enrollment(enrollment):
    inventory, phases, plan, adapter, _, _ = enrollment
    adapter.states["scheduled.service"] = "enabled loaded inactive"
    with state.locked_activation(inventory.data_root) as store:
        _prepare(store, inventory, phases, plan, adapter)
        assert state.read_activation(inventory.data_root, "cutover").status == "prepared"


@pytest.fixture
def empty_enrollment(installed, tmp_path):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    observations = tmp_path / "observations"
    observations.mkdir()
    obs = Observations(observations)
    obs.root = root
    obs.env = {"CLAUDLOBBY_ROOT": str(tmp_path / "foreign")}
    foreign = obs.add("foreign.service", working=tmp_path / "foreign", declared=False)
    adapter = Adapter(obs.package, runner=obs.runner)
    inventory = collect_enrollment(root, (), bootstrap_empty=True, adapter=adapter).require_complete()
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()
    return inventory, {phase: [] for phase in units.PHASES}, plan, adapter, foreign, obs


def _complete(store, step, evidence="a" * 64):
    store.begin("cutover", step)
    store.complete("cutover", step, evidence_digest=evidence)


def _pause_all(store, adapter):
    for phase, intermediate in (("producers", None), ("bots", "sessions_handed_off"),
                                ("ingest", None)):
        if intermediate:
            _complete(store, intermediate)
        step = {"producers": "producers_paused", "bots": "sessions_quiesced", "ingest": "ingest_quiesced"}[phase]
        store.begin("cutover", step)
        result = units.pause_phase(store, "cutover", phase, adapter=adapter)
        assert state.read_activation(store.root, "cutover").body["pending"] == step
        store.complete("cutover", step, evidence_digest=result.digest)
    _complete(store, "queues_classified")


def test_resume_same_id_reloads_frozen_pause_before_forward_work(enrollment, monkeypatch):
    inventory, phases, plan, adapter, _, _ = enrollment
    root = inventory.data_root
    directory = Path(inventory.catalog.split("directory\t", 1)[1].splitlines()[0])
    release = state.read_release(root, plan.release_id)
    package = type("Package", (), {"native": release.native_path,
                                    "artifact_id": release.inputs.artifact_id})()
    adapter.package = package
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(activation.RuntimeIdentity, "current", classmethod(lambda cls:
        activation.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(release.directory / release.paths.interpreter))
    # The previous selected activation is a recorded source. The interrupted
    # activation's parking plans and original snapshots are real journal owners.
    with state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id,
                      enrollment_digest=inventory.digest)
        for step in state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="a" * 64)
        _prepare(store, inventory, phases, plan, adapter, install_directory=directory)
        _pause_all(store, adapter)
    observed = []
    monkeypatch.setattr(activation, "_legacy_bot_socket", lambda *_args, **_kwargs: (root / "absent.sock", False))
    monkeypatch.setattr(activation, "_legacy_quiet", lambda _adapter, _pause, phase, _sockets, **_kwargs:
                        observed.append(("quiet", phase)))
    monkeypatch.setattr(activation, "_probe", lambda _root: None)
    monkeypatch.setattr(activation, "planned_units", lambda _plan, _manager: ())
    monkeypatch.setattr(activation, "_roster", lambda *_args: ({}, ()))
    def finish(_root, _store, identifier, _plan, _release, _source, _source_plan,
               old_units, *_args, **_kwargs):
        observed.append(("finish", identifier, len(old_units)))
        return state.read_activation(root, identifier)
    monkeypatch.setattr(activation, "_finish_running_activation", finish)
    before = list(adapter.calls)
    other = ConfigPlanBuilder(root, release.release_id, release.seal_sha256,
                              ("alpha",), effects={"different": True}).seal()
    with pytest.raises(state.ActivationError, match="different frozen plan"):
        activation.resume_activation(root, "cutover", other.plan_id,
                                     directory,
                                     adapter=adapter)
    assert adapter.calls == before and observed == []
    result = activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
    assert result.body["completed"][-1] == "queues_classified"
    assert observed == [("quiet", phase) for phase in units.PHASES] + [("finish", "cutover", 4)]
    assert adapter.calls == before


def test_running_resume_reconciles_receipted_candidate_instead_of_old_bot(enrollment, monkeypatch):
    inventory, phases, plan, adapter, _, _ = enrollment
    root = inventory.data_root
    directory = Path(inventory.catalog.split("directory\t", 1)[1].splitlines()[0])
    release = state.read_release(root, plan.release_id)
    package = type("Package", (), {"native": release.native_path,
                                    "artifact_id": release.inputs.artifact_id})()
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
    with state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id,
                      enrollment_digest=inventory.digest)
        for step in state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="a" * 64)
        _prepare(store, inventory, phases, candidate_plan, adapter, install_directory=directory)
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
                                  target=bot.target, sha256=bot.generated.sha256,
                                  fence={"ceiling": 210, "fence": "RR_FENCE_member"})
    quiet = []
    monkeypatch.setattr(activation, "_legacy_bot_socket",
                        lambda *_args, **_kwargs: (root / "candidate.sock", True))
    monkeypatch.setattr(activation, "assert_quiescent",
                        lambda _adapter, **kwargs: quiet.append(kwargs["target"]))
    monkeypatch.setattr(activation, "_finish_running_activation",
                        lambda *_args, **_kwargs: state.read_activation(root, "cutover"))
    calls_before = list(adapter.calls)
    resumed = activation.resume_activation(root, "cutover", candidate_plan.plan_id, directory, adapter=adapter)
    assert resumed.body["pending"] == "bots_started"
    assert bot.target not in quiet
    assert set(quiet) == set(phases["ingest"] + phases["producers"])
    assert adapter.calls == calls_before  # No native start or handoff replay.


@pytest.mark.parametrize("interrupt", ["producers", "handoff", "sessions", "ingest", "unknown"])
def test_running_resume_before_selection_reparks_without_repeating_handoff(enrollment, monkeypatch, interrupt):
    inventory, phases, plan, adapter, _, _ = enrollment
    root = inventory.data_root
    directory = Path(inventory.catalog.split("directory\t", 1)[1].splitlines()[0])
    release = state.read_release(root, plan.release_id)
    package = type("Package", (), {"native": release.native_path,
                                    "artifact_id": release.inputs.artifact_id})()
    adapter.package = package
    monkeypatch.setattr(activation, "get_resources", lambda: package)
    monkeypatch.setattr(activation.RuntimeIdentity, "current", classmethod(lambda cls:
        activation.RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)))
    monkeypatch.setattr(activation.sys, "executable", str(release.directory / release.paths.interpreter))
    # One old bot's exact private server; only the native stop owner ends it.
    live = {"member.service": True}
    recorded = adapter.call

    def call(function, *args, timeout=None):
        if function not in ("svc_activation_handoff", "svc_activation_stop_private_server"):
            return recorded(function, *args, timeout=timeout)
        adapter.calls.append((function, args[1]))
        if function == "svc_activation_stop_private_server":
            live["member.service"] = False
        return subprocess.CompletedProcess([function, *args], 0, "", "")

    adapter.call = call
    monkeypatch.setattr(activation, "_original_bot_tmpdir", lambda _unit: "/private-fixture-tmux")
    monkeypatch.setattr(activation, "_legacy_bot_socket", lambda unit, **_kwargs:
                        (root / f"{unit.target}.sock", live[unit.target]))
    monkeypatch.setattr(activation, "_legacy_quiet", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(activation, "_probe", lambda _root: None)
    monkeypatch.setattr(activation, "planned_units", lambda _plan, _manager: ())
    monkeypatch.setattr(activation, "_roster", lambda *_args: ({}, ()))
    monkeypatch.setattr(activation, "_finish_running_activation",
                        lambda _root, _store, identifier, *_args, **_kwargs: state.read_activation(root, identifier))
    with state.locked_activation(root) as store:
        store.prepare("previous", plan, recovery_release_id=release.release_id,
                      enrollment_digest=inventory.digest)
        for step in state.STEPS:
            store.begin("previous", step)
            if step == "selection_switched":
                store.select("previous")
            else:
                store.complete("previous", step, evidence_digest="a" * 64)
        _prepare(store, inventory, phases, plan, adapter, install_directory=directory)
        # Interrupted running estate: each crash leaves its begun step pending.
        store.begin("cutover", "producers_paused")
        producers = units.pause_phase(store, "cutover", "producers", adapter=adapter)
        if interrupt != "producers":
            store.complete("cutover", "producers_paused", evidence_digest=producers.digest)
            store.arm_handoff_evidence("cutover")
            store.begin("cutover", "sessions_handed_off")
            store.record_handoff("cutover", target="member.service", result=None)
            if interrupt != "unknown":
                store.record_handoff("cutover", target="member.service", result="handed_off")
        if interrupt in ("sessions", "ingest"):
            store.complete("cutover", "sessions_handed_off", evidence_digest="a" * 64)
            store.begin("cutover", "sessions_quiesced")
            bots = units.pause_phase(store, "cutover", "bots", adapter=adapter)
            if interrupt == "ingest":
                live["member.service"] = False
                store.complete("cutover", "sessions_quiesced", evidence_digest=bots.digest)
                store.begin("cutover", "ingest_quiesced")
    before = len(adapter.calls)
    if interrupt == "unknown":
        # The handoff keystrokes may or may not have reached the session.
        assert activation.resumable_running_step(state.read_activation(root, "cutover")) is None
        with pytest.raises(state.ActivationError, match="lacks a durable handoff"):
            activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
        assert adapter.calls[before:] == [] and live["member.service"]
        assert state.read_activation(root, "cutover").body["pending"] == "sessions_handed_off"
        return
    resumed = activation.resume_activation(root, "cutover", plan.plan_id, directory, adapter=adapter)
    assert resumed.body["completed"] == list(state.STEPS[:4]) and resumed.body["pending"] is None
    assert resumed.body["handoff_effects"] == {"member.service": "handed_off"}
    effects = [call for call in adapter.calls[before:]
               if call[0] in ("svc_activation_handoff", "svc_activation_stop_private_server")]
    assert effects == ([("svc_activation_handoff", "member")] if interrupt == "producers" else []) + (
        [("svc_activation_stop_private_server", "member")] if interrupt != "ingest" else [])
    assert not live["member.service"]
    assert all(adapter.states[unit.target].startswith("masked") for unit in inventory.units)
    assert state.read_selection(root)["activation_id"] == "previous"


def test_phases_park_exact_nodes_then_restore_original_state_and_links(enrollment):
    inventory, phases, plan, adapter, foreign, wants = enrollment
    before_foreign = foreign.read_bytes()
    with state.locked_activation(inventory.data_root) as store:
        prepared = _prepare(store, inventory, phases, plan, adapter)
        assert units.load_unit_pause(store, "cutover") == prepared
        inventory.check_files()  # prepare never removes an installed node
        with pytest.raises(state.ActivationError, match="not the admitted"):
            units.pause_phase(store, "cutover", "bots", adapter=adapter)
        _pause_all(store, adapter)
        assert [target for function, target in adapter.calls if function == "svc_activation_pause"] == [
            "clock.timer", "scheduled.service", "member.service", "collector.service"]
        assert all(not Path(unit.installed[0].path).exists() for unit in inventory.units)
        assert foreign.read_bytes() == before_foreign
        assert os.readlink(wants / "collector.service") == "../collector.service"
        with pytest.raises(state.ActivationError, match="not the admitted rollback"):
            units.restore_phase(store, "cutover", "ingest", adapter=adapter)
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("selection_restored")]:
            _complete(store, step)
        store.begin("cutover", "selection_restored")
        store.restore_selection("cutover")
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started"),
                            ("producers", "producers_resumed")):
            if phase == "producers":
                _complete(store, "verified")
            store.begin("cutover", step)
            if phase == "ingest":
                adapter.resume_failure = "collector.service"
                with pytest.raises(state.ActivationError, match="refused.*3"):
                    units.restore_phase(store, "cutover", phase, adapter=adapter)
                assert config_install.read_config_install(store.root, units.journal_id("cutover", phase)).status == "rolled_back"
                assert state.read_activation(store.root, "cutover").body["pending"] == step
                adapter.resume_failure = None
            result = units.restore_phase(store, "cutover", phase, adapter=adapter)
            store.complete("cutover", step, evidence_digest=result.digest)
        assert state.read_activation(store.root, "cutover").status == "rolled_back"
    inventory.check_files()
    # Disabled/inactive bot stays so; the timer-owned service waits for its timer.
    assert adapter.states == {**adapter.original, "scheduled.service": "enabled loaded inactive"}
    assert foreign.read_bytes() == before_foreign
    assert os.readlink(wants / "collector.service") == "../collector.service"


def test_proven_empty_bootstrap_parking_and_restore_are_frozen_noops(empty_enrollment):
    inventory, phases, plan, adapter, foreign, obs = empty_enrollment
    original = foreign.read_bytes()
    with state.locked_activation(inventory.data_root) as store:
        prepared = _prepare(store, inventory, phases, plan, adapter)
        assert prepared.enrollment["bootstrap_empty"] is True and prepared.units() == ()
        assert all(p.effects["original_release_id"] is None and p.changes == () for p in prepared.plans)
        assert all(p.release_id == plan.release_id for p in prepared.plans)  # real recovery carrier only
        assert state.read_selection(store.root) is None
        late = obs.add("late.service", working=store.root, declared=False)
        store.begin("cutover", "producers_paused")
        with pytest.raises(state.ActivationError, match="owned consumer"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert late.is_file()  # changed evidence never becomes removal authority
        late.unlink()
        del obs.properties[late.name]
        _pause_all(store, adapter)
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("selection_restored")]:
            _complete(store, step)
        store.begin("cutover", "selection_restored")
        store.restore_selection("cutover")
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started"),
                            ("producers", "producers_resumed")):
            if phase == "producers":
                _complete(store, "verified")
            store.begin("cutover", step)
            evidence = units.restore_phase(store, "cutover", phase, adapter=adapter)
            assert evidence.targets == ()
            store.complete("cutover", step, evidence_digest=evidence.digest)
        assert state.read_activation(store.root, "cutover").status == "rolled_back"
    assert foreign.read_bytes() == original and state.read_selection(inventory.data_root) is None
    assert not any(function.startswith("svc_activation_") for function, _ in obs.calls)


def test_empty_bootstrap_parking_ignores_verified_foreign_launchd_pid_churn(installed, tmp_path):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    (tmp_path / "observations").mkdir()
    obs = Observations(tmp_path / "observations")
    obs.root = root
    obs.manager = "Darwin"
    obs.env = {"CLAUDLOBBY_ROOT": str(tmp_path / "foreign")}
    obs.add("com.fixture.foreign.plist", working=tmp_path / "foreign", declared=False)
    obs.launchd["com.apple.mdworker.plist"] = (
        "gui/501/com.apple.mdworker = {\n\tpath = (submitted by launchd)\n"
        "\ttype = Submitted\n\tstate = running\n"
        "\tprogram = /System/Library/Frameworks/CoreServices.framework/mdworker\n"
        "\tdomain = gui/501 [1]\n\tpid = 710\n}\n")
    adapter = Adapter(obs.package, runner=obs.runner)
    inventory = collect_enrollment(root, (), bootstrap_empty=True, adapter=adapter).require_complete()
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()

    old_catalog = obs.catalog
    obs.catalog = lambda: old_catalog().replace("710\t0\tcom.apple.mdworker", "712\t0\tcom.apple.mdworker")
    obs.launchd["com.apple.mdworker.plist"] = obs.launchd["com.apple.mdworker.plist"].replace(
        "pid = 710", "pid = 712")
    with state.locked_activation(root) as store:
        prepared = _prepare(store, inventory, {phase: [] for phase in units.PHASES}, plan, adapter)
        assert prepared.units() == ()
        assert prepared.enrollment["catalog"] != obs.catalog()


@pytest.mark.parametrize("fault", ["missing", "overlap", "digest", "self-hosted", "unknown"])
def test_invalid_coverage_or_late_caller_refuses_before_any_parking(enrollment, fault):
    inventory, phases, plan, adapter, _, _ = enrollment
    with state.locked_activation(inventory.data_root) as store:
        store.prepare("cutover", plan, recovery_release_id=plan.release_id,
                      enrollment_digest="0" * 64 if fault == "digest" else inventory.digest)
        if fault == "missing":
            phases["bots"] = []
        elif fault == "overlap":
            phases["ingest"].append(phases["bots"][0])
        elif fault in ("self-hosted", "unknown"):
            adapter.refusal["collector.service"] = 1 if fault == "self-hosted" else 3
        with pytest.raises(state.ActivationError):
            units.prepare_unit_pause(store, "cutover", inventory, phases, adapter=adapter)
        inventory.check_files()
        assert not list((store.root / "state/activations").glob("units-*/config"))
        assert not any(function in ("svc_activation_pause", "svc_activation_resume") for function, _ in adapter.calls)


def test_interrupted_parking_and_partial_native_pause_resume_from_existing_owners(enrollment, monkeypatch):
    inventory, phases, plan, adapter, _, _ = enrollment
    root = inventory.data_root
    with state.locked_activation(root) as store:
        prepared = _prepare(store, inventory, phases, plan, adapter)
        store.begin("cutover", "producers_paused")
        original_replace = config_install._replace
        first = Path(inventory.units[0].installed[0].path)

        def crash_after_parking(source, target):
            original_replace(source, target)
            if source == first:
                raise InterruptedError("power loss after owned node was parked")

        with monkeypatch.context() as patch:
            patch.setattr(config_install, "_replace", crash_after_parking)
            with pytest.raises(InterruptedError, match="power loss"):
                units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert not first.exists()
        assert not any(function == "svc_activation_pause" for function, _ in adapter.calls)
    with state.locked_activation(root) as store:
        assert units.load_unit_pause(store, "cutover") == prepared
        adapter.pause_failure = "scheduled.service"
        with pytest.raises(state.ActivationError, match="refused.*3"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert adapter.states["clock.timer"] == "masked-runtime masked inactive"
        assert state.read_activation(root, "cutover").body["pending"] == "producers_paused"
        adapter.pause_failure = None
        result = units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert result.plan_id == prepared.plan("producers").plan_id
        assert config_install.read_config_install(root, result.journal_id).status == "applied"
        store.complete("cutover", "producers_paused", evidence_digest=result.digest)
        # Later-phase source remains intact throughout the interrupted producer pause.
        assert Path(inventory.units[2].installed[0].path).is_file()
        assert Path(inventory.units[3].installed[0].path).is_file()


def _legacy(inventory):
    return replace(inventory, legacy_source=True, units=tuple(
        replace(unit, declaration=replace(unit.declaration, release_id=None)) for unit in inventory.units))


def test_legacy_early_abort_restores_only_producer_pause_after_verified_retry(enrollment):
    inventory, phases, plan, adapter, _, _ = enrollment
    inventory = _legacy(inventory)
    root = inventory.data_root
    release = state.read_release(root, plan.release_id)

    def abort(store, reason, sql=12):
        return units.abort_early_adoption(store, "cutover", reason=reason, release=release,
                                          sql_user_version=sql, adapter=adapter)

    with state.locked_activation(root) as store:
        _prepare(store, inventory, phases, plan, adapter)
        store.begin("cutover", "producers_paused")
        # First producer masked, then the coordinator stopped on the next one.
        adapter.pause_failure = "scheduled.service"
        with pytest.raises(state.ActivationError, match="refused.*3"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert adapter.states["clock.timer"] == "masked-runtime masked inactive"
        assert not Path(inventory.units[0].installed[0].path).exists()
        adapter.pause_failure = None
        adapter.resume_failure = "clock.timer"
        with pytest.raises(state.ActivationError, match="svc_activation_resume refused"):
            abort(store, "masked reader refused")
        record = state.read_activation(root, "cutover")
        assert record.status == "activating" and record.body["adoption_abort"]["result"] is None
        # A half-restored host can neither continue forward nor change its SQL precondition.
        with pytest.raises(state.ActivationError, match="out of order"):
            store.begin("cutover", "producers_paused")
        with pytest.raises(state.ActivationError, match="not the admitted"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        with pytest.raises(state.ActivationRefusal, match="SQL precondition"):
            abort(store, "retry", sql=13)
        adapter.resume_failure = None
        adapter.calls.clear()
        # The timer-owned service ticked off meanwhile: its frozen timer declares
        # it, so that is restored state. A resident service gets no such tolerance.
        adapter.states["scheduled.service"] = "enabled loaded inactive"
        pause = units.load_unit_pause(store, "cutover")
        assert units._scheduled_services(pause.enrollment, pause.phases) == {"scheduled.service"}
        assert units._producer_restored("enabled loaded active", "enabled loaded inactive", scheduled=True)
        assert not units._producer_restored("enabled loaded active", "enabled loaded inactive", scheduled=False)
        assert not units._producer_restored("enabled loaded failed", "enabled loaded failed", scheduled=True)
        evidence = abort(store, "retry")
        assert evidence.operation == "aborted" and evidence.targets == ("clock.timer", "scheduled.service")
        # Retry reloads before reading restored bytes, then once after restoration;
        # the untouched producer is not restarted and no bot or ingest resumes.
        assert [call for call in adapter.calls if call[0] in ("svc_activation_reload", "svc_activation_resume")] == [
            ("svc_activation_reload", None), ("svc_activation_reload", None),
            ("svc_activation_resume", "clock.timer")]
        assert adapter.states == {**adapter.original, "scheduled.service": "enabled loaded inactive"}
        assert all(FileSnapshot.read(Path(unit.installed[0].path)) == unit.installed[0] for unit in inventory.units)
        for phase in ("bots", "ingest"):
            assert config_install.read_config_install(root, units.journal_id("cutover", phase)).status == "prepared"
        record = state.read_activation(root, "cutover")
        assert record.status == "rolled_back" and record.body["completed"] == []
        assert record.body["forward"] == {"status": "activating", "completed": [],
                                          "pending": "producers_paused", "evidence": {}}
        recorded = record.body["adoption_abort"]
        assert recorded["sql_user_version"] == 12 and recorded["release_id"] == release.release_id
        assert [item["reason"] for item in recorded["attempts"]] == ["masked reader refused", "retry"]
        assert recorded["result"] == {"evidence": evidence.digest, "resumed": ["clock.timer"], "cleared": []}
        assert state.read_selection(root) is None
        # Residue: the restored higher-priority file hides a surviving runtime mask,
        # so the snapshot matches. A terminal recheck clears only that mask, never
        # resumes or starts, and appends history beside the unchanged result.
        adapter.hidden_masks.add("scheduled.service")
        adapter.calls.clear()
        with pytest.raises(state.ActivationRefusal, match="SQL precondition"):
            abort(store, "residue", sql=13)
        recheck = abort(store, "residue")
        assert recheck.operation == "rechecked" and adapter.hidden_masks == set()
        assert not any(function == "svc_activation_resume" for function, _ in adapter.calls)
        rechecked = state.read_activation(root, "cutover").body["adoption_abort"]
        assert rechecked["result"] == recorded["result"] and rechecked["attempts"] == recorded["attempts"]
        assert [(item["reason"], item["cleared"]) for item in rechecked["rechecks"]] == [
            ("residue", ["scheduled.service"])]
        adapter.states["clock.timer"] = "enabled loaded inactive"  # drift is refused, not restarted
        with pytest.raises(state.ActivationError, match="restored native state differs: clock.timer"):
            abort(store, "drift")
        assert not any(function == "svc_activation_resume" for function, _ in adapter.calls)


@pytest.mark.parametrize("case", ["sealed_identity", "handed_off", "selected"])
def test_early_abort_refuses_wrong_identity_or_progressed_adoption(enrollment, case):
    inventory, phases, plan, adapter, _, _ = enrollment
    if case != "sealed_identity":
        inventory = _legacy(inventory)
    root = inventory.data_root
    release = state.read_release(root, plan.release_id)
    with state.locked_activation(root) as store:
        _prepare(store, inventory, phases, plan, adapter)
        store.begin("cutover", "producers_paused")
        if case == "handed_off":
            result = units.pause_phase(store, "cutover", "producers", adapter=adapter)
            store.complete("cutover", "producers_paused", evidence_digest=result.digest)
            store.begin("cutover", "sessions_handed_off")
        if case == "selected":
            (root / "state/selected-release.json").write_text(json.dumps(
                {"schema": 1, "activation_id": "other", "release_id": plan.release_id, "plan_id": plan.plan_id}))
        before, calls = state.read_activation(root, "cutover").body, len(adapter.calls)
        with pytest.raises(state.ActivationRefusal):
            units.abort_early_adoption(store, "cutover", reason="r", release=release,
                                       sql_user_version=12, adapter=adapter)
        assert state.read_activation(root, "cutover").body == before
        assert adapter.calls[calls:] == []


@pytest.mark.parametrize("frozen_active", ["active", "activating"])
def test_timer_owned_oneshot_mid_run_parks_and_restores_without_resend(enrollment, frozen_active):
    inventory, phases, plan, adapter, _, _ = enrollment
    # The inventory itself may be taken while the timer-owned oneshot executes.
    inventory = _legacy(replace(inventory, units=tuple(
        replace(unit, properties=tuple((key, frozen_active if key == "ActiveState" else value)
                                       for key, value in unit.properties))
        if unit.target == "scheduled.service" else unit for unit in inventory.units)))
    root = inventory.data_root
    release = state.read_release(root, plan.release_id)
    with state.locked_activation(root) as store:
        # The timer-owned oneshot is executing at the preparation snapshot.
        adapter.states["scheduled.service"] = "enabled loaded activating"
        _prepare(store, inventory, phases, plan, adapter)
        store.begin("cutover", "producers_paused")
        # The same transition on a timer or an ingest service is drift, before parking.
        for target in ("clock.timer", "collector.service"):
            adapter.states[target] = "enabled loaded activating"
            with pytest.raises(state.ActivationError, match=f"changed before parking: {target}"):
                units.pause_phase(store, "cutover", "producers", adapter=adapter)
            adapter.states[target] = adapter.original[target]
        adapter.states["scheduled.service"] = "enabled loaded failed"
        with pytest.raises(state.ActivationError, match="changed before parking: scheduled.service"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        inventory.check_files()
        adapter.states["scheduled.service"] = "enabled loaded activating"
        units.pause_phase(store, "cutover", "producers", adapter=adapter)
        assert adapter.states["scheduled.service"] == "masked-runtime masked inactive"
        adapter.calls.clear()
        units.abort_early_adoption(store, "cutover", reason="probe ran mid-snapshot", release=release,
                                   sql_user_version=12, adapter=adapter)
    # The service is restored before its timer, inactive: its next tick, not a resend, runs it.
    assert [target for function, target in adapter.calls if function == "svc_activation_resume"] == [
        "scheduled.service", "clock.timer"]
    assert adapter.states == {**adapter.original, "scheduled.service": "enabled loaded inactive"}
    assert state.read_activation(root, "cutover").status == "rolled_back"
    # The frozen enrollment keeps the raw observed state; only the native argument differs.
    journal = config_install.read_config_install(root, units.journal_id("cutover", "producers"))
    frozen = state.read_plan(root, journal.plan_id).effects["enrollment"]["units"]
    assert dict(next(unit for unit in frozen if unit["target"] == "scheduled.service")["properties"])[
        "ActiveState"] == frozen_active


def test_fresh_caller_and_native_state_are_rechecked_before_effects(enrollment):
    inventory, phases, plan, adapter, _, _ = enrollment
    with state.locked_activation(inventory.data_root) as store:
        _prepare(store, inventory, phases, plan, adapter)
        store.begin("cutover", "producers_paused")
        adapter.refusal["collector.service"] = 3
        with pytest.raises(state.ActivationError, match="refused"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        inventory.check_files()
        adapter.refusal.clear()
        adapter.states["member.service"] = "enabled loaded active"
        with pytest.raises(state.ActivationError, match="changed before parking"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        inventory.check_files()


@pytest.mark.parametrize("legacy_source", [False, True])
def test_darwin_override_and_effective_binding_drift_refuse_before_park_or_restore(
    installed, tmp_path, legacy_source
):
    root, inputs, paths, _, _ = installed
    release = r.seal_release(root, inputs, paths)
    env = {"CLAUDLOBBY_ROOT": str(root), "FLEET_ROOT": str(root),
           "CLAUDLOBBY_NATIVE_DIR": str(release.native_path), "CLAUDLOBBY_CLI": str(release.cli_path),
           "CLAUDLOBBY_ARTIFACT_ID": release.inputs.artifact_id}
    source = root / "original.plist"
    source.write_bytes(plistlib.dumps({"Label": "fixture", "ProgramArguments": ["/bin/sleep", "60"],
                                      "WorkingDirectory": str(root), "EnvironmentVariables": env}))
    directory = tmp_path / "LaunchAgents"
    directory.mkdir()
    target = directory / "fixture.plist"
    target.write_bytes(source.read_bytes())
    if legacy_source:
        source.write_bytes(plistlib.dumps({"Label": "fixture", "ProgramArguments": ["/bin/false"],
                                          "WorkingDirectory": str(root), "EnvironmentVariables": env}))
        assert source.read_bytes() != target.read_bytes()
    declaration = UnitDeclaration(source, "host", root,
                                  "" if legacy_source else release.release_id, tuple(env.items()))
    props = {"UnitFileState": "unchanged", "LoadState": "loaded", "ActiveState": "active",
             "DisabledOverride": "unset", "EnabledState": "enabled"}
    entry = EnrolledUnit(declaration, "gui/501/fixture", FileSnapshot.read(source),
                         (FileSnapshot.read(target),), tuple(props.items()))
    catalog = f"manager\tDarwin\ndomain\tgui/501\ndirectory\t{directory}\nPID\tStatus\tLabel\n710\t0\tfixture\n"
    inventory = EnrollmentInventory(root, "Darwin", catalog, (entry,), entry.installed,
                                    (), (), legacy_source=legacy_source)
    adapter = RecordedAdapter(inventory)
    plan = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={}).seal()
    phases = {"producers": [entry.target], "bots": [], "ingest": []}
    with state.locked_activation(root) as store:
        prepared = _prepare(store, inventory, phases, plan, adapter)
        if legacy_source:
            frozen = prepared.enrollment["units"][0]
            assert frozen["generated"]["sha256"] != frozen["installed"][0]["sha256"]
        store.begin("cutover", "producers_paused")
        adapter.overrides["fixture"] = "enabled"  # same effective value, different persisted override
        with pytest.raises(state.ActivationError, match="Darwin enrollment drift"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        inventory.check_files()
        adapter.overrides.clear()
        adapter.binding_changed = True
        with pytest.raises(state.ActivationError, match="Darwin enrollment drift"):
            units.pause_phase(store, "cutover", "producers", adapter=adapter)
        inventory.check_files()
        adapter.binding_changed = False
        paused = units.pause_phase(store, "cutover", "producers", adapter=adapter)
        store.complete("cutover", "producers_paused", evidence_digest=paused.digest)
        if legacy_source:
            assert not target.exists(), "legacy pause must park the actual installed plist"
            return  # first adoption has no sealed prior release to roll back to
        store.begin_rollback("cutover")
        for step in state.ROLLBACK_STEPS[:state.ROLLBACK_STEPS.index("selection_restored")]:
            _complete(store, step)
        store.begin("cutover", "selection_restored")
        store.restore_selection("cutover")
        for step in ("ingest_started", "bots_started", "verified"):
            _complete(store, step)
        store.begin("cutover", "producers_resumed")
        adapter.overrides["fixture"] = "disabled"
        with pytest.raises(state.ActivationError, match="Darwin enrollment drift"):
            units.restore_phase(store, "cutover", "producers", adapter=adapter)
        assert not target.exists(), "override drift restored files before refusal"
        adapter.overrides.clear()
        restored = units.restore_phase(store, "cutover", "producers", adapter=adapter)
        assert restored.operation == "restored"
        inventory.check_files()
