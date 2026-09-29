"""Publish frozen candidate unit files and owned persistent enablement links.

Generated configuration is applied first; installed definitions are published
only at their explicit startup phase. Every enrolled declaration must carry the
shared admission owner's verified guard. This matters on Linux: publication in
a user config directory can take precedence over a parked runtime mask.

The coordinator owns zero-process proofs, native reload/unmask/start/readiness,
and stopping candidates before rollback. Publication never starts a unit.
ConfigInstall owns all file writes,
interruption recovery and exact removal; original restoration is UnitPause's.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path

from .activation_state import ActivationError, ActivationStore, read_activation, read_selection
from .activation_units import PHASES, journal_id as parking_id, load_unit_pause
from .config_install import apply_config, prepare_config, read_config_install, rollback_config
from .config_plan import ConfigPlan, ConfigPlanBuilder, path_state, read_plan
from .config_units import current_declarations, planned_units
from .releases import read_release
from .supervision_inventory import Adapter, _catalog, _environment, _properties


_OWNER = "activation-enrollment-v1"
_START = {"ingest": "ingest_started", "bots": "bots_started", "producers": "producers_resumed"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def journal_id(activation_id: str, phase: str) -> str:
    if phase not in (*PHASES, "directories"):
        raise ActivationError("unknown enrollment phase")
    return "enrollment-" + _digest([activation_id, phase])


def _record(store, activation_id, *, rollback=False):
    store.assert_locked()
    record = read_activation(store.root, activation_id)
    if rollback:
        if record.status != "rolling_back" or record.body["pending"] != "candidate_units_removed":
            raise ActivationError("candidate removal is not the admitted rollback step")
    elif record.status != "activating" or "configuration_applied" not in record.body["completed"]:
        raise ActivationError("candidate enrollment requires completed generated configuration")
    selected = {"schema": 1, "activation_id": activation_id,
                "release_id": record.body["intent"]["release_id"],
                "plan_id": record.body["intent"]["plan_id"]}
    if read_selection(store.root) != selected:
        raise ActivationError("candidate enrollment requires its exact selected release")
    return record


def _candidate(store, record, manager):
    plan = read_plan(store.root, record.body["intent"]["plan_id"])
    release = read_release(store.root, plan.release_id)
    if release.seal_sha256 != plan.release_seal:
        raise ActivationError("candidate release seal differs")
    declarations = current_declarations(plan, manager)
    units = planned_units(plan, manager)
    if len(declarations) != len(units):
        raise ActivationError("candidate declaration coverage differs")
    # Kept at the shared guard owner; no second command/parser predicate here.
    from .runtime_admission import validate_unit_admission
    for declaration, item in units:
        environment = dict(declaration.environment)
        if (type(item.get("enroll")) is not bool or item.get("phase") not in PHASES
                or declaration.scope not in ("host", "fleet", "bot")
                or declaration.scope != "host" and not declaration.fleet
                or declaration.scope == "bot" and not declaration.bot
                or environment.get("CLAUDLOBBY_ROOT") != str(store.root)
                or environment.get("CLAUDLOBBY_RELEASE_ID") != release.release_id
                or environment.get("CLAUDLOBBY_NATIVE_DIR") != str(release.native_path)
                or environment.get("CLAUDLOBBY_CLI") != str(release.cli_path)
                or environment.get("CLAUDLOBBY_ARTIFACT_ID") != release.inputs.artifact_id):
            raise ActivationError("candidate unit identity or phase is incomplete")
        if item["enroll"]:
            validate_unit_admission(release, declaration, item, plan.blob(item["sha256"]))
    return plan, units


def _catalog_now(adapter, enrollment):
    original = _catalog(enrollment["catalog"])
    current = _catalog(adapter.read("svc_inventory_catalog"))
    if current[:3] != original[:3]:
        raise ActivationError("native manager, domain or search paths changed")
    return current


def _target(manager, domain, source):
    name = Path(source).name
    return domain + "/" + name.removesuffix(".plist") if manager == "Darwin" else name


def _wanted_by(content, source, paired_services):
    """Only the compositor's shipped Install grammar has publication authority."""
    if source.suffix == ".plist":
        return None
    wanted, section, seen = [], "", False
    for raw in content.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section == "Install":
                if seen:
                    raise ActivationError("duplicate Install section is unsupported")
                seen = True
        elif section == "Install":
            key, separator, value = line.partition("=")
            if separator != "=" or key.strip() != "WantedBy":
                raise ActivationError("unsupported unit Install semantics")
            wanted.append(value.strip())
    expected = ([] if source.name in paired_services else
                ["timers.target" if source.suffix == ".timer" else "default.target"])
    if wanted != expected or not expected and seen:
        raise ActivationError("unit Install target differs from its persistent service/timer role")
    return expected[0] if expected else None


def _enablement(entry, wanted_by, prior):
    if wanted_by is None:
        return None
    installed = Path(entry["installed"])
    link = installed.parent / (wanted_by + ".wants") / installed.name
    if link.parent.resolve() != link.parent:
        raise ActivationError("enablement directory is redirected")
    before = path_state(link)
    node = before["node"]
    if node["kind"] != "absent":
        if (not entry["original"] or node["kind"] != "symlink"
                or before["resolved"] != str(installed)):
            raise ActivationError("unowned or misdirected native enablement link")
    elif entry["original"] and dict(prior["properties"]).get("UnitFileState") == "enabled":
        raise ActivationError("original persistent enablement link is missing")
    return {"path": str(link), "target": str(installed), "before": before,
            "create": not entry["original"], "wanted_by": wanted_by}


def _retired_enablement(enrollment, entries, pause, *, observe):
    """Bind only old persistent links whose exact owned targets retire."""
    if enrollment["manager"] != "Linux":
        return []
    retained = {entry["target"] for entry in entries}
    result = []
    for unit in enrollment["units"]:
        if not unit["installed"] or unit["target"] in retained:
            continue
        state = dict(unit["properties"]).get("UnitFileState")
        if state != "enabled":
            continue
        installed = Path(unit["installed"][0]["path"])
        wanted = "timers.target" if installed.suffix == ".timer" else "default.target"
        link = installed.parent / (wanted + ".wants") / installed.name
        if link.parent.resolve() != link.parent:
            raise ActivationError("retired enablement directory is redirected")
        phase = next((name for name in PHASES if unit["target"] in pause.phases[name]), None)
        if phase is None:
            raise ActivationError("retired native target has no frozen parking phase")
        entry = {"path": str(link), "target": str(installed), "phase": phase}
        if observe:
            before = path_state(link)
            if before["node"]["kind"] != "symlink" or before["resolved"] != str(installed):
                raise ActivationError("retired native enablement link is missing or foreign")
            entry["before"] = before
        result.append(entry)
    return result


def _check_enablement(entry, *, allow_candidate, require_applied=False):
    link = entry["enablement"]
    if link is None:
        return
    path = Path(link["path"])
    installed = Path(entry["installed"])
    if (link["wanted_by"] not in ("default.target", "timers.target")
            or path != installed.parent / (link["wanted_by"] + ".wants") / installed.name
            or link["target"] != str(installed)
            or type(link["create"]) is not bool or link["create"] != (not entry["original"])):
        raise ActivationError("frozen enablement target differs from its exact installed candidate")
    if path.parent.resolve() != path.parent:
        raise ActivationError("enablement directory is redirected")
    actual = path_state(path)
    applied = {"resolved": link["target"],
               "node": {"kind": "symlink", "target": link["target"]}}
    allowed = [link["before"]] if not require_applied or not link["create"] else []
    if link["create"] and allow_candidate:
        allowed.append(applied)
    if actual not in allowed:
        raise ActivationError("native enablement link changed outside this activation")


def _native_quiet(adapter, manager, file, target, *, candidate=None):
    result = adapter.call("svc_activation_assert_external", file, target, str(os.getpid()))
    if result.returncode:
        raise ActivationError(f"candidate caller is hosted or unknown: {target}")
    value = adapter.read("svc_inventory_state", file, target).strip().split()
    if (len(value) != 3 or value[2] != "inactive"
            or manager == "Darwin" and value != ["unchanged", "unloaded", "inactive"]
            or manager == "Linux" and value[1] not in ("masked", "not-found", "loaded")):
        raise ActivationError(f"candidate target is not quiescent: {target}")
    if manager == "Linux" and value[1] == "loaded":
        # A stopped candidate may remain cached until the coordinator reloads.
        # Require its actual fresh definition, not merely an inactive name.
        props = _properties(adapter.read("svc_inventory_properties", target))
        if (candidate is None or props.get("Id") != target
                or props.get("FragmentPath") != str(file) or props.get("DropInPaths") != ""
                or props.get("NeedDaemonReload") != "no" or props.get("LoadState") != "loaded"
                or props.get("ActiveState") != "inactive"):
            raise ActivationError(f"foreign or stale loaded candidate collision: {target}")
        if candidate["service"]:
            if props.get("Triggers", "").split() != [candidate["service"]]:
                raise ActivationError("candidate timer changed its service binding")
            return  # the separately declared service gets its own identity check
        if (props.get("WorkingDirectory") != candidate["working_directory"]
                or any(_environment(props.get("Environment", "")).get(key) != val
                       for key, val in candidate["environment"].items())):
            raise ActivationError(f"candidate loaded identity differs: {target}")


@dataclass(frozen=True)
class EnrollmentPublication:
    phase: str
    journal_id: str
    plan_id: str
    targets: tuple[str, ...]
    operation: str

    @property
    def digest(self):
        return _digest(vars(self))


def _check_targets(adapter, enrollment, entries, *, allow_candidate):
    manager, domain, directories, _, loaded = _catalog_now(adapter, enrollment)
    for entry in entries:
        _check_enablement(entry, allow_candidate=allow_candidate)
        destination = Path(entry["installed"])
        if destination.parent not in directories or destination.parent.resolve() != destination.parent:
            raise ActivationError("candidate destination left verified search paths")
        candidate_exists = False
        for directory in directories:
            path = directory / destination.name
            state = path_state(path)["node"]
            if state["kind"] != "absent" and (not allow_candidate or path != destination or state != entry["after"]):
                raise ActivationError(f"foreign or changed candidate collision: {path}")
            candidate_exists |= path == destination and state == entry["after"]
        # A new unit cannot inherit an unrelated loaded definition. Original
        # targets were proved owned by the frozen enrollment, then parked.
        if destination.name in loaded and not entry["original"] and not allow_candidate:
            raise ActivationError(f"foreign loaded candidate collision: {entry['target']}")
        _native_quiet(adapter, manager, destination, entry["target"],
                      candidate=entry if candidate_exists else None)


def prepare_candidate_enrollment(store: ActivationStore, activation_id: str, *,
                                 configuration_journal: str, install_directory: Path,
                                 adapter=None) -> tuple[ConfigPlan, ...]:
    """Freeze exact phase placements after config apply and all original pauses.

    New identities use the explicitly selected, observed install directory.
    Matching identities retain the exact original installed path and owner.
    One directory-only ConfigInstall journal ensures missing link parents before
    phase journals reserve their own files/links. No unit is enabled or started.
    """
    record = _record(store, activation_id)
    published = [store.root / "state/activations" / journal_id(activation_id, phase) / "config"
                 for phase in PHASES]
    if all(path.exists() for path in published):
        _, plans = _load(store, activation_id, record)
        if any(plan.effects["configuration_journal"] != configuration_journal
               or plan.effects["install_directory"] != str(Path(install_directory).absolute()) for plan in plans):
            raise ActivationError("prepared publication differs from requested configuration or placement")
        return plans
    pause = load_unit_pause(store, activation_id)
    enrollment = pause.enrollment
    candidate, units = _candidate(store, record, enrollment["manager"])
    config = read_config_install(store.root, configuration_journal)
    if config.plan_id != candidate.plan_id or config.status != "applied":
        raise ActivationError("generated configuration journal is not applied")
    for phase in PHASES:
        if read_config_install(store.root, parking_id(activation_id, phase)).status != "applied":
            raise ActivationError("original unit parking is incomplete")
    adapter = adapter or Adapter()
    manager, domain, directories, _, _ = _catalog_now(adapter, enrollment)
    install_directory = Path(install_directory).absolute()
    if install_directory not in directories or install_directory.resolve() != install_directory:
        raise ActivationError("new-unit directory is not a verified native search path")
    original = {unit["target"]: unit for unit in enrollment["units"]}
    for prior in original.values():
        if prior["installed"]:
            _native_quiet(adapter, manager, prior["installed"][0]["path"], prior["target"])
    entries = []
    paired_services = {declaration.service for declaration, item in units if item["enroll"] and declaration.service}
    for declaration, item in units:
        if not item["enroll"]:
            continue
        target = _target(manager, domain, declaration.source)
        prior = original.get(target)
        if prior is not None:
            saved = prior["declaration"]
            if any(saved[key] != (str(value) if isinstance(value, Path) else value)
                   for key, value in (("scope", declaration.scope), ("fleet", declaration.fleet),
                       ("bot", declaration.bot), ("working_directory", declaration.working_directory))):
                raise ActivationError("candidate label collides with a different original owner")
        installed = prior["installed"][0]["path"] if prior and prior["installed"] else str(install_directory / declaration.source.name)
        if installed == str(declaration.source):
            raise ActivationError("generated source cannot also be an installed unit")
        entries.append({"target": target, "installed": installed, "source": str(declaration.source),
                        "phase": item["phase"], "original": bool(prior and prior["installed"]),
                        "working_directory": str(declaration.working_directory),
                        "environment": dict(declaration.environment),
                        "service": declaration.service,
                        "after": {"kind": "file", "sha256": item["sha256"], "mode": item["mode"]}})
        wanted = _wanted_by(candidate.blob(item["sha256"]), declaration.source, paired_services)
        entries[-1]["enablement"] = _enablement(entries[-1], wanted, prior)
    if not entries:
        raise ActivationError("empty enrolled candidate manifest is not removal authority")
    retired = _retired_enablement(enrollment, entries, pause, observe=True)
    _check_targets(adapter, enrollment, entries, allow_candidate=False)
    common = {"owner": _OWNER, "activation_id": activation_id,
              "configuration_journal": configuration_journal, "candidate_plan": candidate.plan_id,
              "install_directory": str(install_directory),
              "enrollment_digest": record.body["intent"]["enrollment_digest"],
              "entries": entries, "retired_enablement": retired}
    directory_id = journal_id(activation_id, "directories")
    directory_path = store.root / "state/activations" / directory_id / "config"
    if directory_path.exists():
        directory_plan = read_plan(store.root, read_config_install(store.root, directory_id).plan_id)
        if directory_plan.effects != {**common, "phase": "directories"}:
            raise ActivationError("prepared publication directories differ")
    else:
        builder = ConfigPlanBuilder(store.root, candidate.release_id, candidate.release_seal,
                                    candidate.fleets, effects={**common, "phase": "directories"})
        for entry in entries:
            link = entry["enablement"]
            if link and link["create"]:
                builder.directory(Path(link["path"]).parent)
        directory_plan = builder.seal()
        prepare_config(directory_plan, directory_id)
    _check_publication_changes(directory_plan, entries, "directories")
    apply_config(store.root, directory_id)
    plans = []
    for phase in PHASES:
        builder = ConfigPlanBuilder(store.root, candidate.release_id, candidate.release_seal,
                                    candidate.fleets, effects={**common, "phase": phase})
        for entry in entries:
            builder.input(Path(entry["source"]))
            if entry["phase"] == phase:
                builder.file(Path(entry["installed"]), candidate.blob(entry["after"]["sha256"]), mode=entry["after"]["mode"])
                link = entry["enablement"]
                if link and link["create"]:
                    builder.symlink(Path(link["path"]), Path(link["target"]))
        for link in retired:
            if link["phase"] == phase:
                builder.remove(Path(link["path"]))
        plans.append(builder.seal())
    _check_targets(adapter, enrollment, entries, allow_candidate=False)
    for phase, plan in zip(PHASES, plans):
        prepare_config(plan, journal_id(activation_id, phase))
    return tuple(plans)


def _check_publication_changes(plan, entries, phase, retired=()):
    actual = {change.target: change.after for change in plan.changes}
    if phase == "directories":
        expected = {str(Path(entry["enablement"]["path"]).parent): {"kind": "directory", "mode": 0o755}
                    for entry in entries if entry["enablement"] and entry["enablement"]["create"]}
        valid = all(target in expected and node == expected[target] for target, node in actual.items())
    else:
        expected = {entry["installed"]: entry["after"] for entry in entries if entry["phase"] == phase}
        expected.update({entry["enablement"]["path"]: {"kind": "symlink", "target": entry["enablement"]["target"]}
                         for entry in entries if entry["phase"] == phase
                         and entry["enablement"] and entry["enablement"]["create"]})
        expected.update({link["path"]: {"kind": "absent"}
                         for link in retired if link["phase"] == phase})
        valid = actual == expected
    retired_before = {link["path"]: link["before"] for link in retired if link["phase"] == phase}
    if not valid or any((change.before != retired_before[change.target]
                         if change.target in retired_before else change.before["node"] != {"kind": "absent"})
                        for change in plan.changes):
        raise ActivationError("candidate publication contains unowned replacements")


def _load(store, activation_id, record):
    pause = load_unit_pause(store, activation_id)
    candidate, units = _candidate(store, record, pause.enrollment["manager"])
    paired_services = {declaration.service for declaration, item in units if item["enroll"] and declaration.service}
    wanted = {str(declaration.source): _wanted_by(candidate.blob(item["sha256"]), declaration.source, paired_services)
              for declaration, item in units if item["enroll"]}
    plans, shared = [], None
    for phase in ("directories", *PHASES):
        journal = read_config_install(store.root, journal_id(activation_id, phase))
        plan = read_plan(store.root, journal.plan_id)
        effects = plan.effects
        if (effects.get("owner") != _OWNER or effects.get("activation_id") != activation_id
                or effects.get("phase") != phase or effects.get("candidate_plan") != candidate.plan_id
                or effects.get("enrollment_digest") != record.body["intent"]["enrollment_digest"]
                or plan.release_id != candidate.release_id or plan.release_seal != candidate.release_seal):
            raise ActivationError("candidate publication differs from activation intent")
        common = {key: value for key, value in effects.items() if key != "phase"}
        if shared is not None and common != shared:
            raise ActivationError("candidate phase placements disagree")
        shared = common
        for entry in effects["entries"]:
            link = entry["enablement"]
            if entry["source"] not in wanted or (link["wanted_by"] if link else None) != wanted[entry["source"]]:
                raise ActivationError("frozen enablement differs from candidate Install semantics")
        retired = effects.get("retired_enablement", [])
        expected_retired = _retired_enablement(pause.enrollment, effects["entries"], pause, observe=False)
        if ([{key: item[key] for key in ("path", "target", "phase")} for item in retired]
                != expected_retired or any(item.get("before", {}).get("node", {}).get("kind") != "symlink"
                                           or item["before"].get("resolved") != item["target"] for item in retired)):
            raise ActivationError("retired enablement differs from frozen enrollment")
        _check_publication_changes(plan, effects["entries"], phase, retired)
        if phase != "directories":
            plans.append(plan)
    configuration = read_config_install(store.root, plans[0].effects["configuration_journal"])
    if configuration.plan_id != candidate.plan_id or configuration.status != "applied":
        raise ActivationError("candidate generated configuration is no longer applied")
    return pause.enrollment, tuple(plans)


def candidate_entries(store: ActivationStore, activation_id: str, phase: str) -> tuple[dict, ...]:
    """Read verified frozen native placements; never derive installed paths again."""
    if phase not in PHASES:
        raise ActivationError("unknown enrollment phase")
    _, plans = _load(store, activation_id, _record(store, activation_id))
    entries = plans[PHASES.index(phase)].effects["entries"]
    return tuple(json.loads(json.dumps(entry)) for entry in entries if entry["phase"] == phase)


def _selected_phase(root: Path, phase: str) -> tuple[ConfigPlan, tuple[dict, ...]]:
    """Read an active activation's frozen native placements for one phase."""
    if phase not in PHASES:
        raise ActivationError("unknown selected native phase")
    selected = read_selection(root)
    if selected is None:
        raise ActivationError("native operation requires a selected activation")
    record = read_activation(root, selected["activation_id"])
    if (record.status != "active"
            or record.body["intent"]["release_id"] != selected["release_id"]
            or record.body["intent"]["plan_id"] != selected["plan_id"]):
        raise ActivationError("selected native activation is incomplete")
    candidate = read_plan(root, selected["plan_id"])
    journal = read_config_install(root, journal_id(selected["activation_id"], phase))
    publication = read_plan(root, journal.plan_id)
    effects = publication.effects
    if (journal.status != "applied" or publication.release_id != selected["release_id"]
            or publication.release_seal != candidate.release_seal
            or effects.get("owner") != _OWNER
            or effects.get("activation_id") != selected["activation_id"]
            or effects.get("candidate_plan") != candidate.plan_id
            or effects.get("phase") != phase
            or effects.get("enrollment_digest") != record.body["intent"]["enrollment_digest"]):
        raise ActivationError("selected native publication differs from its activation")
    entries = effects.get("entries")
    if not isinstance(entries, list):
        raise ActivationError("selected native publication has no entries")
    _check_publication_changes(publication, entries, phase, effects.get("retired_enablement", []))
    return candidate, tuple(json.loads(json.dumps(entry)) for entry in entries if entry.get("phase") == phase)


def selected_phase_entries(root: Path, phase: str) -> tuple[dict, ...]:
    """Return frozen placements after the shared selected-phase validation."""
    return _selected_phase(root, phase)[1]


def selected_bot_entry(root: Path, fleet: str, bot: str, platform: str) -> dict:
    """Read one active bot's frozen native placement, including after a stop.

    The activation publication journal, rather than the current native search
    path, is the placement owner: a deliberately stopped bot has no installed
    file from which a start could rediscover its destination.
    """
    candidate, entries = _selected_phase(root, "bots")
    declarations = [(declaration, item) for declaration, item in planned_units(candidate, platform)
                    if declaration.scope == "bot" and declaration.fleet == fleet
                    and declaration.bot == bot and item["enroll"]]
    if len(declarations) != 1:
        raise ActivationError("selected bot has no unique frozen unit")
    declaration, item = declarations[0]
    selected_entries = [entry for entry in entries if entry.get("source") == str(declaration.source)]
    if len(selected_entries) != 1:
        raise ActivationError("selected bot has no unique native placement")
    entry = selected_entries[0]
    if (entry["after"] != {"kind": "file", "sha256": item["sha256"], "mode": item["mode"]}
            or entry["working_directory"] != str(declaration.working_directory)
            or entry["environment"] != dict(declaration.environment)
            or Path(entry["installed"]).name != Path(entry["source"]).name
            or not Path(entry["installed"]).is_absolute()):
        raise ActivationError("selected bot native placement differs from its frozen unit")
    return json.loads(json.dumps(entry))


def verify_candidate_enablement(store: ActivationStore, activation_id: str, phase: str, *, adapter=None) -> str:
    """Verify owned links and effective persistent state after the native reload.

    This proves user-manager enablement, not OS user-manager startup or linger.
    Original links are preserved; paired oneshot services have no enablement.
    """
    adapter = adapter or Adapter()
    observations = []
    for entry in candidate_entries(store, activation_id, phase):
        _check_enablement(entry, allow_candidate=True, require_applied=True)
        link = entry["enablement"]
        if link and link["create"]:
            snapshot = adapter.read("svc_activation_snapshot", entry["installed"], entry["target"]).strip().split()
            if len(snapshot) != 3 or snapshot[:2] != ["enabled", "loaded"]:
                raise ActivationError("new native unit is not persistently enabled")
            observations.append([link["path"], link["target"], snapshot])
    return _digest(observations)


def install_candidate_units(store: ActivationStore, activation_id: str, phase: str, *, adapter=None) -> EnrollmentPublication:
    """Publish one phase's guarded bytes; caller still owns native activation."""
    record = _record(store, activation_id)
    if phase not in PHASES or record.body["pending"] != _START[phase]:
        raise ActivationError("candidate publication is not the admitted start phase")
    enrollment, plans = _load(store, activation_id, record)
    adapter = adapter or Adapter()
    plan = plans[PHASES.index(phase)]
    entries = [entry for entry in plan.effects["entries"] if entry["phase"] == phase]
    _check_targets(adapter, enrollment, entries, allow_candidate=True)
    apply_config(store.root, journal_id(activation_id, phase))
    # No daemon-reload here. Native state reconciliation/start is a separate
    # coordinator effect; this evidence claims exact installed files/links only.
    return EnrollmentPublication(phase, journal_id(activation_id, phase), plan.plan_id,
                                 tuple(entry["target"] for entry in entries), "published")


def remove_candidate_units(store: ActivationStore, activation_id: str, *, adapter=None) -> tuple[EnrollmentPublication, ...]:
    """Remove exact candidate nodes after coordinator rollback quiescence.

    Matching replacements also return to the parked absence; only UnitPause
    may restore original bytes/links. No prefix walk or foreign cleanup exists.
    """
    record = _record(store, activation_id, rollback=True)
    enrollment, plans = _load(store, activation_id, record)
    adapter = adapter or Adapter()
    _check_targets(adapter, enrollment, plans[0].effects["entries"], allow_candidate=True)
    results = []
    for phase, plan in reversed(tuple(zip(PHASES, plans))):
        rollback_config(store.root, journal_id(activation_id, phase))
        entries = [entry for entry in plan.effects["entries"] if entry["phase"] == phase]
        results.append(EnrollmentPublication(phase, journal_id(activation_id, phase), plan.plan_id,
                                             tuple(entry["target"] for entry in entries), "removed"))
    rollback_config(store.root, journal_id(activation_id, "directories"))
    return tuple(results)
