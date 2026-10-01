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

from .activation_state import ActivationError, ActivationRefusal, ActivationStore, read_activation, read_selection
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


_TICKING = frozenset({"active", "activating", "inactive"})


def _same_enrollment(saved: str, observed: str, *, scheduled: bool) -> bool:
    """A timer-owned service may start, still run, or exit between snapshots.

    Its timer, not its own activity, is the enrollment state. Timers, resident
    services, bots and ingest stay exact; failed or other states refuse.
    """
    if not scheduled:
        return saved == observed
    # Checked even when equal: a failed or unknown activity is never restorable.
    before, after = saved.split(), observed.split()
    return (len(before) == len(after) == 3 and before[:2] == after[:2]
            and before[2] in _TICKING and after[2] in _TICKING)


def _scheduled_services(enrollment: dict, phases: dict) -> set[str]:
    """Producer services named by a frozen producer timer declaration.

    A launchd producer is its own schedule: its PID comes and goes with each
    tick, and its native pause/resume read only load state.
    """
    producers = set(phases["producers"])
    if enrollment["manager"] == "Darwin":
        return producers
    return {unit["declaration"]["service"] for unit in enrollment["units"]
            if unit["target"] in producers and unit["target"].endswith(".timer")
            and unit["declaration"]["service"] in producers}


def _native_saved(unit: dict, scheduled: set[str]) -> str:
    """The state the native owner pauses from and restores to.

    The frozen snapshot stays the raw record. A timer-owned service is restored
    inactive for its timer to trigger: never resent as one-shot work, and never
    an `activating` snapshot the native owner cannot restore.
    """
    saved = _saved(unit)
    if unit["target"] not in scheduled:
        return saved
    file_state, load, active = saved.split()
    if active not in _TICKING:
        raise ActivationError(f"timer-owned service has no restorable state: {unit['target']}")
    return f"{file_state} {load} inactive"


def _timers_last(units) -> list[dict]:
    # Resume a scheduled service before its timer can trigger it again.
    return sorted(units, key=lambda unit: unit["target"].endswith(".timer"))


def _file(unit: dict) -> str:
    return unit["installed"][0]["path"]


_PAUSE_TIMEOUT = 120  # a finite native stop (TimeoutStopSec=90s) plus helper overhead


def _call(adapter, function, *args, timeout=None):
    # Reads and other calls keep the adapter's default deadline.
    result = adapter.call(function, *args) if timeout is None else adapter.call(function, *args, timeout=timeout)
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
    scheduled = _scheduled_services(enrollment, membership)
    for unit in enrolled:
        if not _same_enrollment(_saved(unit),
                                _call(adapter, "svc_activation_snapshot", _file(unit), unit["target"]),
                                scheduled=unit["target"] in scheduled):
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


def load_unit_pause(store: ActivationStore, activation_id: str, *, terminal: bool = False) -> UnitPause:
    """Recover frozen membership, saved states and file backups via their owners.

    ``terminal`` is only for rechecking a verified early adoption abort.
    """
    store.assert_locked()
    record = read_activation(store.root, activation_id) if terminal else _record(store, activation_id)
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
    if (phase not in PHASES or record.body["pending"] != _PAUSE_STEPS[phase]
            or "adoption_abort" in record.body):
        raise ActivationError("unit pause phase is not the admitted activation step")
    pause = load_unit_pause(store, activation_id)
    adapter = adapter or Adapter()
    _check_empty(pause.enrollment, adapter)
    for unit in pause.enrollment["units"]:
        _check_source(unit["generated"])
    _external(adapter, pause)
    _darwin_check(adapter, pause.enrollment)
    scheduled = _scheduled_services(pause.enrollment, pause.phases)
    for unit in pause.units():
        # A previously parked node may be absent on retry. ConfigInstall proves
        # whether that absence belongs to its interrupted swap; never recreate
        # a missing file here or overwrite a changed enabled/loaded state.
        path = Path(_file(unit))
        if path.exists() or path.is_symlink():
            _check_source(unit["installed"][0])
            if not _same_enrollment(_saved(unit),
                                    _call(adapter, "svc_activation_snapshot", path, unit["target"]),
                                    scheduled=unit["target"] in scheduled):
                raise ActivationError(f"native enrollment changed before parking: {unit['target']}")
    identifier = journal_id(activation_id, phase)
    apply_config(store.root, identifier)  # sole filesystem writer; resumes partial swaps
    for unit in pause.units(phase):
        _call(adapter, "svc_activation_pause", _file(unit), unit["target"],
              _native_saved(unit, scheduled), str(os.getpid()), timeout=_PAUSE_TIMEOUT)
    _darwin_check(adapter, pause.enrollment)
    return UnitPhaseEvidence(phase, identifier, pause.plan(phase).plan_id,
                             tuple(unit["target"] for unit in pause.units(phase)), "paused")


def _producer_restored(saved: str, observed: str, *, scheduled: bool) -> bool:
    return _same_enrollment(saved, observed, scheduled=scheduled)


def abort_early_adoption(store: ActivationStore, activation_id: str, *, reason: str, release,
                         sql_user_version: int, adapter=None) -> UnitPhaseEvidence:
    """Explicit operator abort of a Linux first adoption stopped in producers_paused.

    Restores only the producer parking journal and producers' saved native
    states. Bots, ingest, selection and SQL are untouched, and later parking
    journals must be unstarted. Any refusal or unknown leaves the durable abort
    marker, so forward activation stays refused; an explicit rerun reconciles.
    """
    store.assert_locked()
    record = read_activation(store.root, activation_id)
    terminal = record.status == "rolled_back"
    if terminal:
        # Only the verified receipt of this sole first adoption may be rechecked.
        from .activation import _completed_adoption_abort
        others = {path.parent.name for path in (store.root / "state/activations").glob("*/activation.json")}
        if (not _completed_adoption_abort(store.root, record) or others != {activation_id}
                or read_selection(store.root) is not None
                or record.body["adoption_abort"]["sql_user_version"] != sql_user_version):
            raise ActivationRefusal("terminal recheck requires the sole verified early abort and its SQL precondition")
    else:
        record = _record(store, activation_id)
    pause = load_unit_pause(store, activation_id, terminal=terminal)
    if pause.enrollment["manager"] != "Linux" or not pause.enrollment.get("legacy_source"):
        raise ActivationRefusal("early abort supports only a Linux legacy enrollment")
    for phase in ("bots", "ingest"):
        journal = read_config_install(store.root, journal_id(activation_id, phase))
        if journal.status != "prepared" or any(row != "pending" for row in journal.progress):
            raise ActivationRefusal("a later parking phase has begun; early abort is not admitted")
    from .migration_apply import read_migration
    if read_migration(store.root, activation_id) is not None:
        raise ActivationRefusal("activation has a migration journal; early abort is not admitted")
    identifier = journal_id(activation_id, "producers")
    if terminal and read_config_install(store.root, identifier).status != "rolled_back":
        raise ActivationRefusal("terminal recheck requires the restored producer journal")
    adapter = adapter or Adapter()
    if not terminal:
        # Ineligible records refuse before any native read.
        store.check_adoption_abort(activation_id, sql_user_version=sql_user_version)
    if not terminal and "adoption_abort" not in record.body:
        # First attempt: read-only proofs before the durable marker, so a
        # changed source or hosted caller leaves the record unchanged.
        for unit in pause.enrollment["units"]:
            _check_source(unit["generated"])
        for unit in pause.units():
            if Path(_file(unit)).exists() or Path(_file(unit)).is_symlink():
                _check_source(unit["installed"][0])
        _external(adapter, pause)
    if not terminal:
        store.begin_adoption_abort(activation_id, reason=reason, release_id=release.release_id,
                                   artifact_id=release.inputs.artifact_id,
                                   sql_user_version=sql_user_version)
    for unit in pause.enrollment["units"]:
        _check_source(unit["generated"])
    if read_config_install(store.root, identifier).status in ("rolling_back", "rolled_back"):
        # A prior attempt restored bytes; expose them before frozen-placement reads.
        _call(adapter, "svc_activation_reload")
    _external(adapter, pause)
    if not terminal:
        rollback_config(store.root, identifier)
    for unit in pause.units():
        _check_source(unit["installed"][0])
    if not terminal:
        _call(adapter, "svc_activation_reload")
    resumed, cleared = [], []
    scheduled = _scheduled_services(pause.enrollment, pause.phases)
    for unit in _timers_last(pause.units("producers")):
        target, saved, native = unit["target"], _saved(unit), _native_saved(unit, scheduled)
        ticks = target in scheduled
        if not _producer_restored(saved, _call(adapter, "svc_activation_snapshot", _file(unit), target),
                                  scheduled=ticks):
            if terminal:  # a recheck never resumes or starts
                raise ActivationError(f"restored native state differs: {target}")
            _call(adapter, "svc_activation_resume", _file(unit), target, native)
            resumed.append(target)
            if not _producer_restored(saved, _call(adapter, "svc_activation_snapshot", _file(unit), target),
                                      scheduled=ticks):
                raise ActivationError(f"restored native state differs: {target}")
        # A restored higher-priority file can hide a surviving runtime mask.
        if (dict(unit["properties"])["LoadState"] == "loaded"
                and _call(adapter, "svc_activation_clear_runtime_mask", _file(unit), target, native) == "removed"):
            cleared.append(target)
    evidence = UnitPhaseEvidence("producers", identifier, pause.plan("producers").plan_id,
                                 tuple(unit["target"] for unit in pause.units("producers")),
                                 "rechecked" if terminal else "aborted")
    if terminal:
        store.record_adoption_abort_recheck(activation_id, reason=reason, release_id=release.release_id,
                                            artifact_id=release.inputs.artifact_id,
                                            evidence_digest=evidence.digest, cleared=cleared)
    else:
        store.finish_adoption_abort(record.activation_id, evidence_digest=evidence.digest,
                                    resumed=resumed, cleared=cleared)
    return evidence


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
    scheduled = _scheduled_services(pause.enrollment, pause.phases)
    for unit in _timers_last(pause.units(phase)):
        _check_source(unit["installed"][0])
        _darwin_check(adapter, pause.enrollment, {unit["target"]})
        _call(adapter, "svc_activation_resume", _file(unit), unit["target"], _native_saved(unit, scheduled))
        _darwin_check(adapter, pause.enrollment, {unit["target"]}, original_load=True)
        if not _same_enrollment(_saved(unit), _call(adapter, "svc_activation_snapshot", _file(unit), unit["target"]),
                                scheduled=unit["target"] in scheduled):
            raise ActivationError(f"restored native state differs: {unit['target']}")
    return UnitPhaseEvidence(phase, identifier, pause.plan(phase).plan_id,
                             tuple(unit["target"] for unit in pause.units(phase)), "restored")
