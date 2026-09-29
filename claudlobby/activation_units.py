"""Pause and restore original enrollment under the existing activation lock.

The coordinator assigns every enrolled unit to producers, bots or ingest and
admits each phase. This module does not hand off sessions, prove zero writers,
complete activation steps, install candidate units or start candidate processes.
ConfigPlan/ConfigInstall own every parked file and recovery write. Their frozen
effects retain the enrollment and native snapshots; there is no second journal.

Restoring higher-priority installed files can defeat runtime masks before the
adapter resumes them. The durable activation/start-admission guard must remain
in force throughout restoration and reboot. Native evidence, including complete
Darwin inventory, remains a prerequisite rather than an inferred success.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from .activation_state import ActivationError, ActivationStore, read_activation, read_selection
from .config_install import apply_config, prepare_config, read_config_install, rollback_config
from .config_plan import ConfigPlan, ConfigPlanBuilder, read_plan
from .releases import read_release
from .supervision_inventory import (
    Adapter, EnrollmentInventory, FileSnapshot, InventoryError, _catalog, collect_enrollment, validate_darwin_unit,
)


PHASES = ("producers", "bots", "ingest")
_PAUSE_STEPS = dict(zip(PHASES, ("producers_paused", "sessions_quiesced", "ingest_quiesced")))
_RESTORE_STEPS = dict(zip(PHASES, ("producers_resumed", "bots_started", "ingest_started")))
_OWNER = "activation-units-v1"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def journal_id(activation_id: str, phase: str) -> str:
    """Stable, bounded ConfigInstall identity; never another journal layout."""
    if phase not in PHASES:
        raise ActivationError(f"unknown unit phase: {phase}")
    return "units-" + _digest([activation_id, phase])


def _record(store: ActivationStore, activation_id: str):
    store.assert_locked()
    record = read_activation(store.root, activation_id)
    if record.status in ("active", "rolled_back"):
        raise ActivationError("activation is already terminal")
    return record


def _membership(enrollment: dict, phases: dict) -> dict[str, list[str]]:
    if set(phases) != set(PHASES):
        raise ActivationError("explicit producers, bots and ingest membership is required")
    owned = {unit["target"] for unit in enrollment["units"]
             if unit["installed"] or dict(unit["properties"]).get("LoadState") == "loaded"}
    if any(len(unit["installed"]) != 1 for unit in enrollment["units"] if unit["target"] in owned):
        raise ActivationError("every enrolled unit needs exactly one verified installed node")
    result = {phase: sorted(phases[phase]) for phase in PHASES}
    targets = [target for phase in PHASES for target in result[phase]]
    if len(targets) != len(set(targets)) or set(targets) != owned:
        raise ActivationError("phase membership must be exhaustive and disjoint over enrolled units")
    return result


def _saved(unit: dict) -> str:
    properties = dict(unit["properties"])
    try:
        return " ".join(properties[key] for key in ("UnitFileState", "LoadState", "ActiveState"))
    except KeyError as exc:
        raise ActivationError("incomplete saved native state") from exc


def _file(unit: dict) -> str:
    return unit["installed"][0]["path"]


def _call(adapter, function, *args):
    result = adapter.call(function, *args)
    if result.returncode:
        raise ActivationError(f"{function} refused ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _check_source(saved: dict) -> None:
    actual = FileSnapshot.read(Path(saved["path"]))
    if (actual.resolved != saved["resolved"] or actual.mode != saved["mode"]
            or actual.link != saved["link"] or actual.sha256 != saved["sha256"]
            or base64.b64encode(actual.content).decode("ascii") != saved["content"]["base64"]):
        raise ActivationError(f"saved unit source changed: {saved['path']}")


@dataclass(frozen=True)
class UnitPause:
    activation_id: str
    plans: tuple[ConfigPlan, ...]

    @property
    def enrollment(self) -> dict:
        return self.plans[0].effects["enrollment"]

    @property
    def phases(self) -> dict:
        return self.plans[0].effects["phases"]

    def units(self, phase: str | None = None) -> tuple[dict, ...]:
        targets = set(self.phases[phase]) if phase else {target for values in self.phases.values() for target in values}
        return tuple(unit for unit in self.enrollment["units"] if unit["target"] in targets)

    def plan(self, phase: str) -> ConfigPlan:
        return self.plans[PHASES.index(phase)]


@dataclass(frozen=True)
class UnitPhaseEvidence:
    phase: str
    journal_id: str
    plan_id: str
    targets: tuple[str, ...]
    operation: str

    @property
    def digest(self) -> str:
        return _digest(vars(self))


def _external(adapter, pause: UnitPause) -> None:
    # All units, including later phases, must be safe before the first parking
    # write. Explicit coordinator PID keeps subprocess ancestry unambiguous.
    for unit in pause.units():
        _call(adapter, "svc_activation_assert_external", _file(unit), unit["target"], str(os.getpid()))


def _darwin_check(adapter, enrollment, targets=None, *, original_load=False):
    if enrollment["manager"] != "Darwin":
        return
    for unit in enrollment["units"]:
        if not unit["installed"] or (targets is not None and unit["target"] not in targets):
            continue
        declaration = unit["declaration"]
        try:
            # First adoption freezes the installed plist as the original
            # launch definition; its generated source remains a separate
            # checked snapshot and may differ from the installed bytes.
            source = unit["installed"][0] if enrollment.get("legacy_source") else unit["generated"]
            validate_darwin_unit(
                adapter, unit["target"], source=base64.b64decode(source["content"]["base64"], validate=True),
                installed_path=_file(unit), working_directory=declaration["working_directory"],
                environment=dict(declaration["environment"]), original=dict(unit["properties"]),
                require_original_load=original_load,
            )
        except InventoryError as exc:
            raise ActivationError(f"Darwin enrollment drift: {exc}") from exc


def _original_release(record, enrollment):
    if enrollment.get("legacy_source"):
        if (record.body["intent"].get("source_kind") != "legacy-unsealed"
                or record.body["previous_selection"] is not None
                or any(unit["declaration"]["release_id"] for unit in enrollment["units"])
                or not enrollment["units"]):
            raise ActivationError("legacy enrollment claims a sealed prior release")
        return None
    if enrollment.get("bootstrap_empty"):
        if (enrollment["bootstrap_empty"] is not True or enrollment["units"]
                or enrollment["issues"] or record.body["previous_selection"] is not None):
            raise ActivationError("bootstrap parking requires proven empty original enrollment and no prior selection")
        return None
    release_ids = {unit["declaration"]["release_id"] for unit in enrollment["units"]}
    if len(release_ids) != 1:
        raise ActivationError("original enrollment mixes releases")
    return next(iter(release_ids))


def _check_empty(enrollment, adapter):
    if not enrollment.get("bootstrap_empty"):
        return
    try:
        observed = collect_enrollment(Path(enrollment["data_root"]), (), bootstrap_empty=True,
                                      adapter=adapter).require_complete()
        # The full re-inventory still refuses an unknown or newly owned unit.
        # Foreign launchd jobs can start, exit or change PID between the frozen
        # observation and parking; their catalog rows are not enrollment state.
        before = _catalog(enrollment["catalog"])
        after = _catalog(observed.catalog)
        if (str(observed.data_root) != enrollment["data_root"]
                or observed.manager != enrollment["manager"]
                or before[:3] != after[:3]
                or observed.units):
            raise InventoryError("bootstrap enrollment scope changed since its frozen observation")
        if observed.payload()["observed_files"] != enrollment["observed_files"]:
            raise InventoryError("bootstrap installed sources changed since their frozen observation")
    except (InventoryError, OSError) as exc:
        raise ActivationError(f"empty original enrollment cannot be reverified: {exc}") from exc


def prepare_unit_pause(store: ActivationStore, activation_id: str,
                       inventory: EnrollmentInventory, phases: dict, *, adapter=None) -> UnitPause:
    """Freeze/prepare all parking phases before effects; use load after parking.

    An interrupted preparation can repeat while files remain unchanged. After
    any parking effect, recover from the deterministic ConfigInstall journals.
    """
    record = _record(store, activation_id)
    if record.status != "prepared":
        raise ActivationError("prepare unit parking before beginning activation; recover existing plans otherwise")
    inventory.require_complete()
    if inventory.data_root != store.root or inventory.digest != record.body["intent"]["enrollment_digest"]:
        raise ActivationError("enrollment does not match the persisted activation intent")
    enrollment = inventory.payload()
    membership = _membership(enrollment, phases)
    original_release_id = _original_release(record, enrollment)
    # ConfigPlan always binds a real release seal. Empty bootstrap plans use
    # the explicit recovery release only as their zero-change storage carrier;
    # original_release_id remains None, never a fabricated prior installation.
    release = read_release(store.root, original_release_id or record.body["intent"]["recovery_release_id"])
    if not inventory.legacy_source:
        for unit in inventory.units:
            env = dict(unit.declaration.environment)
            if (env.get("CLAUDLOBBY_NATIVE_DIR") != str(release.native_path)
                    or env.get("CLAUDLOBBY_CLI") != str(release.cli_path)
                    or env.get("CLAUDLOBBY_ARTIFACT_ID") != release.inputs.artifact_id):
                raise ActivationError("enrollment identity differs from its sealed original release")
    previous = record.body["previous_selection"]
    if previous is not None and previous["release_id"] != release.release_id:
        raise ActivationError("enrollment differs from the saved selected release")
    inventory.check_files()
    adapter = adapter or Adapter()
    _check_empty(enrollment, adapter)
    enrolled = tuple(unit for unit in enrollment["units"] if unit["installed"])
    # Finish every external-caller check before preparing any parking write.
    for unit in enrolled:
        _call(adapter, "svc_activation_assert_external", _file(unit), unit["target"], str(os.getpid()))
    for unit in enrolled:
        if _call(adapter, "svc_activation_snapshot", _file(unit), unit["target"]) != _saved(unit):
            raise ActivationError(f"native enrollment changed: {unit['target']}")
    _darwin_check(adapter, enrollment, original_load=True)
    plans = []
    for phase in PHASES:
        effects = {"owner": _OWNER, "activation_id": activation_id,
                   "enrollment_digest": inventory.digest, "enrollment": enrollment,
                   "phases": membership, "phase": phase, "original_release_id": original_release_id}
        builder = ConfigPlanBuilder(store.root, release.release_id, release.seal_sha256,
                                    tuple(sorted({unit.declaration.fleet for unit in inventory.units
                                                  if unit.declaration.fleet})), effects=effects)
        for unit in inventory.units:
            builder.input(Path(unit.generated.path))
        for unit in enrolled:
            if unit["target"] in membership[phase]:
                builder.remove(Path(_file(unit)))
        plans.append(builder.seal())
    inventory.check_files()
    _check_empty(enrollment, adapter)
    for phase, plan in zip(PHASES, plans):
        prepare_config(plan, journal_id(activation_id, phase))
    return load_unit_pause(store, activation_id)


def load_unit_pause(store: ActivationStore, activation_id: str) -> UnitPause:
    """Recover frozen membership, saved states and file backups via their owners."""
    record = _record(store, activation_id)
    plans = []
    shared = None
    for phase in PHASES:
        journal = read_config_install(store.root, journal_id(activation_id, phase))
        plan = read_plan(store.root, journal.plan_id)
        effects = plan.effects
        if (effects.get("owner") != _OWNER or effects.get("activation_id") != activation_id
                or effects.get("phase") != phase
                or effects.get("enrollment_digest") != record.body["intent"]["enrollment_digest"]
                or _digest(effects.get("enrollment")) != effects.get("enrollment_digest")):
            raise ActivationError("parking plan differs from persisted activation enrollment")
        common = {key: value for key, value in effects.items() if key != "phase"}
        if shared is not None and common != shared:
            raise ActivationError("parking phases disagree about frozen enrollment")
        shared = common
        membership = _membership(effects["enrollment"], effects["phases"])
        if membership != effects["phases"]:
            raise ActivationError("parking membership changed")
        original_release_id = _original_release(record, effects["enrollment"])
        if effects.get("original_release_id") != original_release_id or plan.release_id != (
                original_release_id or record.body["intent"]["recovery_release_id"]):
            raise ActivationError("parking plan differs from its original enrollment or recovery carrier")
        targets = {_file(unit) for unit in effects["enrollment"]["units"]
                   if unit["target"] in membership[phase]}
        if ({change.target for change in plan.changes} != targets
                or any(change.after != {"kind": "absent"} for change in plan.changes)):
            raise ActivationError("parking plan contains effects outside exact enrolled nodes")
        plans.append(plan)
    release = read_release(store.root, plans[0].release_id)
    if any(plan.release_id != release.release_id or plan.release_seal != release.seal_sha256 for plan in plans):
        raise ActivationError("parking plans disagree about their sealed release carrier")
    return UnitPause(activation_id, tuple(plans))


def pause_phase(store: ActivationStore, activation_id: str, phase: str, *, adapter=None) -> UnitPhaseEvidence:
    """Park exact phase files, then disarm through the shared native adapter.

    Returning evidence does not complete sessions_quiesced: detached sessions
    and producer/writer processes still require the coordinator's own proof.
    """
    record = _record(store, activation_id)
    if phase not in PHASES or record.body["pending"] != _PAUSE_STEPS[phase]:
        raise ActivationError("unit pause phase is not the admitted activation step")
    pause = load_unit_pause(store, activation_id)
    adapter = adapter or Adapter()
    _check_empty(pause.enrollment, adapter)
    for unit in pause.enrollment["units"]:
        _check_source(unit["generated"])
    _external(adapter, pause)
    _darwin_check(adapter, pause.enrollment)
    for unit in pause.units():
        # A previously parked node may be absent on retry. ConfigInstall proves
        # whether that absence belongs to its interrupted swap; never recreate
        # a missing file here or overwrite a changed enabled/loaded state.
        path = Path(_file(unit))
        if path.exists() or path.is_symlink():
            _check_source(unit["installed"][0])
            if _call(adapter, "svc_activation_snapshot", path, unit["target"]) != _saved(unit):
                raise ActivationError(f"native enrollment changed before parking: {unit['target']}")
    identifier = journal_id(activation_id, phase)
    apply_config(store.root, identifier)  # sole filesystem writer; resumes partial swaps
    for unit in pause.units(phase):
        _call(adapter, "svc_activation_pause", _file(unit), unit["target"], _saved(unit), str(os.getpid()))
    _darwin_check(adapter, pause.enrollment)
    return UnitPhaseEvidence(phase, identifier, pause.plan(phase).plan_id,
                             tuple(unit["target"] for unit in pause.units(phase)), "paused")


def restore_phase(store: ActivationStore, activation_id: str, phase: str, *, adapter=None) -> UnitPhaseEvidence:
    """Restore the original enrollment only, after coordinator rollback gates.

    Configuration and prior release selection must already be restored. Normal
    candidate startup is a separate operation; it cannot reuse these snapshots.
    """
    record = _record(store, activation_id)
    if (phase not in PHASES or record.status != "rolling_back"
            or record.body["pending"] != _RESTORE_STEPS[phase]
            or "selection_restored" not in record.body["completed"]
            or read_selection(store.root) != record.body["previous_selection"]):
        raise ActivationError("original unit restoration is not the admitted rollback step")
    pause = load_unit_pause(store, activation_id)
    adapter = adapter or Adapter()
    _check_empty(pause.enrollment, adapter)
    for unit in pause.enrollment["units"]:
        _check_source(unit["generated"])
    _external(adapter, pause)
    _darwin_check(adapter, pause.enrollment)
    identifier = journal_id(activation_id, phase)
    rollback_config(store.root, identifier)
    for unit in pause.units(phase):
        _check_source(unit["installed"][0])
        _darwin_check(adapter, pause.enrollment, {unit["target"]})
        _call(adapter, "svc_activation_resume", _file(unit), unit["target"], _saved(unit))
        _darwin_check(adapter, pause.enrollment, {unit["target"]}, original_load=True)
        if _call(adapter, "svc_activation_snapshot", _file(unit), unit["target"]) != _saved(unit):
            raise ActivationError(f"restored native state differs: {unit['target']}")
    return UnitPhaseEvidence(phase, identifier, pause.plan(phase).plan_id,
                             tuple(unit["target"] for unit in pause.units(phase)), "restored")
