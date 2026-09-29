"""Cold-host, first-adoption and selected-release activation through durable owners.

No automatic retry, rollback or native discovery fallback. A failed
begun step remains pending for explicit recovery. The supplied root, reviewed
plan and sealed executing release are mandatory.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import socket
import stat
import sys
import time

from .activation_state import ActivationError, ActivationRecord, locked_activation, read_activation, read_selection
from .activation_identity import identity_bindings_from_registry
from . import activation_enrollment as enrollment, activation_units as units, config_install
from .activation_runtime import BOT_READY_KINDS, assert_quiescent, start_unit
from .config_plan import path_state, read_plan
from .config_units import current_declarations, planned_units
from .migration_apply import apply_migration
from .migration_plan import build_migration_manifest
from .plane.db import db_file
from .releases import read_release
from .resources import get_resources
from .runtime_admission import RuntimeIdentity, validate_unit_admission
from .supervision_inventory import (Adapter, InventoryError, UnitDeclaration,
                                    _catalog, _darwin_disabled, _darwin_source, _environment,
                                    collect_enrollment)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _empty_plane(root: Path) -> None:
    # Bootstrap intentionally accepts less than upgrade: any existing Plane
    # content (DB, sidecar, socket/lock, spool, raw stage or unknown file) needs
    # the recovery/upgrade owner. db_file is the canonical noncreating path.
    directory = db_file(root).parent
    if directory.resolve() != directory:
        raise ActivationError("bootstrap refuses redirected Plane state")
    try:
        if directory.exists() and (not directory.is_dir() or next(directory.iterdir(), None) is not None):
            raise ActivationError("bootstrap requires absent Plane data and queues; use explicit recovery/upgrade")
    except OSError as exc:
        raise ActivationError("bootstrap cannot prove Plane state empty") from exc


def _probe(root: Path):
    # Lazy application dependency; this is the existing daemon's typed reader,
    # not a second socket protocol. Tests replace this native observation seam.
    from .plane.daemon import probe_daemon_info, socket_path
    return probe_daemon_info(socket_path(root))


def _ingest_ready(root, release, *, timeout=30) -> dict:
    expected = {"root": str(root), "release_id": release.release_id,
                "seal_sha256": release.seal_sha256, "artifact_id": release.inputs.artifact_id,
                "cli": str(release.cli_path), "runtime": release.compatibility.to_dict(),
                "sql_schema": release.compatibility.schema.write, "schema_state": "read"}
    deadline = time.monotonic() + timeout
    while True:
        info = _probe(root)
        if info is not None:
            if (any(info.get(key) != value for key, value in expected.items())
                    or info.get("probe_version") != 1 or type(info.get("pid")) is not int or info["pid"] <= 1):
                raise ActivationError("ingest readiness differs from the exact candidate release/schema")
            return info
        if time.monotonic() >= deadline:
            raise ActivationError("candidate ingest readiness timed out; activation remains pending")
        time.sleep(0.1)


def _roster(plan, declarations, package):
    from .active_config import context_from_plan
    sources = plan.effects.get("fleet_manifests")
    if not isinstance(sources, dict) or set(sources) != set(plan.fleets):
        raise ActivationError("bootstrap requires the staged fleet manifest coverage")
    rank, expected, contexts = {}, set(), []
    for name, source in sorted(sources.items()):
        manifest = Path(source)
        if (source not in plan.inputs or not manifest.is_absolute() or manifest.name != "fleet.yaml"
                or path_state(manifest, source=plan.inputs[source]["follow_links"]) != plan.inputs[source]["state"]):
            raise ActivationError("fleet source is not bound to the reviewed plan")
        context = context_from_plan(plan, name, package=package)
        contexts.append(context)
        fleet = context.fleet
        for bot in fleet.bots:
            key = (name, bot)
            rank[key] = (bot != fleet.manager, name, bot)
            expected.add(key)
    actual = [(d.fleet, d.bot) for d, item in declarations if item["enroll"] and item["phase"] == "bots"]
    if set(actual) != expected or len(actual) != len(expected):
        raise ActivationError("bootstrap bot unit coverage differs from the declared fleet roster")
    return rank, contexts


def _registry_scan(context):
    from .plane.registry_emit import run_generate_scan
    return run_generate_scan(context.paths, context.fleet, require_commit=True)


def _registry_ready(plan, declarations, package) -> list[dict]:
    from .operation_context import OperationContextError, bind_task_context
    # Re-read exact reviewed declarations, never a nearby/local discovery scan.
    _, contexts = _roster(plan, declarations, package)
    results = []
    for context in contexts:
        result = _registry_scan(context)
        if (not isinstance(result, dict) or result.get("complete") is not True
                or result.get("recording") != "committed"):
            raise ActivationError("candidate registry scan is disabled, incomplete or uncommitted; no bots started")
        try:
            bound = bind_task_context(context, origin=replace(context, bot_id=context.fleet.manager))
        except OperationContextError as exc:
            raise ActivationError("candidate registry identity binding failed; no bots started") from exc
        results.append({"fleet": context.fleet.name, "scan": result,
                        "bindings": {"host_uid": bound.host_uid, "fleet_uid": bound.fleet_uid,
                                     "manager_uid": bound.caller.uid,
                                     "bots": {name: actor.uid for name, actor in bound.bots.items()}}})
    return results


def _legacy_declarations(plan) -> tuple[UnitDeclaration, ...]:
    """Bind pre-release Darwin sources to the candidate's declared unit paths.

    Current generated bytes, installed bytes and launchd's loaded definition
    are then independently compared by collect_enrollment. A stale or extra
    related unit remains an inventory blocker, never an inferred deletion.
    """
    plan.check_fresh()
    result = []
    for declaration, _ in planned_units(plan, "Darwin"):
        source = declaration.source
        if not source.exists() and not source.is_symlink():
            continue  # A genuinely new candidate unit has no legacy owner.
        try:
            from .supervision_inventory import FileSnapshot
            old = _darwin_source(FileSnapshot.read(source).content)
        except (OSError, InventoryError) as exc:
            raise ActivationError("legacy generated unit cannot be read") from exc
        if (old["label"] != source.stem or old["directory"] != str(declaration.working_directory)
                or old["environment"].get("CLAUDLOBBY_ROOT") != str(plan.data_root)
                or declaration.scope == "bot" and not old["environment"].get("TMUX_TMPDIR")):
            raise ActivationError("legacy generated unit differs from the declared host owner")
        environment = {"CLAUDLOBBY_ROOT": str(plan.data_root)}
        if declaration.scope == "bot":
            environment["TMUX_TMPDIR"] = old["environment"]["TMUX_TMPDIR"]
        result.append(replace(declaration, release_id="", environment=tuple(sorted(environment.items()))))
    if not result:
        raise ActivationError("no reviewed legacy unit sources were found")
    plan.check_fresh()
    return tuple(result)


def bootstrap_activation(root: Path, activation_id: str, plan_id: str,
                         install_directory: Path, *, adapter: Adapter | None = None) -> ActivationRecord:
    """Activate a genuinely empty host once; failures require explicit recovery.

    Runs only from the candidate's sealed interpreter/package. The configuration
    and install directory are explicit, reviewed inputs. The adapter seam exists
    for native observation/action tests, not alternative ownership decisions.
    Scheduled producer services are published alongside their timers; Linux
    starts only the timer. A loaded Darwin schedule is not proof its first tick
    ran. Inner watchdog admission stays closed until the final active record.
    """
    root = Path(root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ActivationError("bootstrap requires an explicit existing absolute data root")
    root = root.resolve()
    plan = read_plan(root, plan_id)
    release = read_release(root, plan.release_id)
    package, identity = get_resources(), RuntimeIdentity.current()
    if (release.seal_sha256 != plan.release_seal
            or identity != RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)
            or package.native != release.native_path or package.artifact_id != release.inputs.artifact_id
            or Path(sys.executable).absolute() != release.directory / release.paths.interpreter):
        raise ActivationError("run bootstrap using the exact sealed candidate interpreter and package")
    adapter = adapter if adapter is not None else Adapter(package)
    if adapter.package.native != release.native_path:
        raise ActivationError("bootstrap adapter differs from the candidate release")
    with locked_activation(root) as store:
        if read_selection(root) is not None:
            raise ActivationError("bootstrap refuses an existing release selection")
        for prior in (root / "state/activations").glob("*/activation.json"):
            record = read_activation(root, prior.parent.name)
            raise ActivationError(f"existing activation {record.activation_id} requires explicit recovery, not bootstrap")
        _empty_plane(root)
        plan.check_fresh()
        inventory = collect_enrollment(root, (), bootstrap_empty=True, adapter=adapter).require_complete()
        install_directory = Path(install_directory)
        manager, domain, directories, _, _ = _catalog(inventory.catalog)
        if (not install_directory.is_absolute() or install_directory.resolve() != install_directory
                or install_directory not in directories):
            raise ActivationError("bootstrap install directory is not an observed native search path")
        declarations = planned_units(plan, inventory.manager)
        starts = {}
        for declaration, item in declarations:
            if item["enroll"]:
                start = validate_unit_admission(release, declaration, item, plan.blob(item["sha256"]))
                starts[str(declaration.source)] = (declaration, item, start)
                if item["phase"] == "bots" and os.path.lexists(declaration.working_directory):
                    raise ActivationError("bootstrap refuses pre-existing bot runtime directories")
        rank, _ = _roster(plan, declarations, package)
        if sum(item["phase"] == "ingest" and item["enroll"] for _, item in declarations) != 1:
            raise ActivationError("bootstrap requires exactly one declared ingest unit")
        # An empty original roster cannot supply the caller checks used by the
        # upgrade parking owner. Check exact candidate targets before preparing
        # SQL/config changes; hosted or unknown ancestry is not permission.
        for declaration, item in declarations:
            if item["enroll"] and adapter.call("svc_activation_assert_external",
                    install_directory / declaration.source.name,
                    enrollment._target(manager, domain, declaration.source), str(os.getpid())).returncode:
                raise ActivationError("bootstrap caller is hosted or cannot be proved external; use an operator shell")
        store.prepare(activation_id, plan, recovery_release_id=release.release_id,
                      source_release_id=release.release_id, enrollment_digest=inventory.digest)
        config_install.prepare_config(plan, activation_id)
        units.prepare_unit_pause(store, activation_id, inventory,
                                 {phase: [] for phase in units.PHASES}, adapter=adapter)
        for step, phase in (("producers_paused", "producers"), ("sessions_handed_off", None),
                            ("sessions_quiesced", "bots"), ("ingest_quiesced", "ingest")):
            store.begin(activation_id, step)
            _empty_plane(root)
            evidence = (units.pause_phase(store, activation_id, phase, adapter=adapter).digest if phase else
                        _digest({"bootstrap_empty": True, "sessions": [], "handoffs": []}))
            store.complete(activation_id, step, evidence_digest=evidence)
        store.begin(activation_id, "queues_classified")
        _empty_plane(root)
        migration = build_migration_manifest(root, release, release, initialize_empty=True)
        if migration.blockers or any(row["file_count"] != 0 for row in migration.queues.values()):
            raise ActivationError("bootstrap migration/queue proof is blocked")
        store.complete(activation_id, "queues_classified", evidence_digest=migration.manifest_id[2:])
        store.begin(activation_id, "backup_saved")
        apply_migration(store, activation_id, migration)  # owns backup + migration completion
        store.begin(activation_id, "selection_switched")
        store.select(activation_id)
        store.begin(activation_id, "configuration_applied")
        applied = config_install.apply_config(root, activation_id)
        store.complete(activation_id, "configuration_applied", evidence_digest=_digest({
            "plan_id": applied.plan_id, "status": applied.status, "progress": applied.progress}))
        enrollment.prepare_candidate_enrollment(store, activation_id, configuration_journal=activation_id,
                                               install_directory=install_directory, adapter=adapter)
        observations = {}
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started")):
            store.begin(activation_id, step)
            registry = _registry_ready(plan, declarations, package) if phase == "bots" else []
            if phase == "bots":
                bindings = identity_bindings_from_registry(plan, registry, package=package)
                store.record_identity_bindings(activation_id, bindings, package=package)
            publication = enrollment.install_candidate_units(store, activation_id, phase, adapter=adapter)
            entries = enrollment.candidate_entries(store, activation_id, phase)
            if phase == "bots":
                entries = sorted(entries, key=lambda entry: rank[(starts[entry["source"]][0].fleet,
                                                                  starts[entry["source"]][0].bot)])
            results = []
            for entry in entries:
                _, _, unit = starts[entry["source"]]
                result = start_unit(store, activation_id, installed_file=Path(entry["installed"]),
                                    target=entry["target"], unit=unit, sha256=entry["after"]["sha256"],
                                    adapter=adapter, readiness=(lambda: _ingest_ready(root, release))
                                    if phase == "ingest" else None)
                observations[entry["source"]] = result
                results.append(result.digest)
            enabled = enrollment.verify_candidate_enablement(store, activation_id, phase, adapter=adapter)
            store.complete(activation_id, step, evidence_digest=_digest([publication.digest, results, enabled, registry]))
        store.begin(activation_id, "verified")
        verified = {"ingest": _ingest_ready(root, release), "bots": []}
        for source, result in observations.items():
            declaration, _, unit = starts[source]
            if unit.phase == "bots":
                fence = result.details["readiness"]["fence"]
                kind = result.details["readiness"]["kind"]
                if (kind not in BOT_READY_KINDS or adapter.read("svc_activation_bot_ready", root,
                        declaration.working_directory, "0", fence).strip() != kind):
                    raise ActivationError("bot lost readiness before producer admission")
                verified["bots"].append(result.digest)
        store.complete(activation_id, "verified", evidence_digest=_digest(verified))
        store.begin(activation_id, "producers_resumed")
        publication = enrollment.install_candidate_units(store, activation_id, "producers", adapter=adapter)
        entries = enrollment.candidate_entries(store, activation_id, "producers")
        timer_services = {entry["service"] for entry in entries if entry["service"]}
        results = []
        for entry in entries:
            if Path(entry["installed"]).name in timer_services:
                continue  # publish the service; only its declared timer is started
            _, _, unit = starts[entry["source"]]
            result = start_unit(store, activation_id, installed_file=Path(entry["installed"]),
                                target=entry["target"], unit=unit, sha256=entry["after"]["sha256"], adapter=adapter)
            results.append(result.digest)
        enabled = enrollment.verify_candidate_enablement(store, activation_id, "producers", adapter=adapter)
        return store.complete(activation_id, "producers_resumed", evidence_digest=_digest({
            "publication": publication.digest, "native_starts": results,
            "enablement": enabled, "timer_services": sorted(timer_services),
            "watchdog_ticks": "admitted only after active", "user_manager_startup": "host prerequisite"}))


def _original_bot_tmpdir(unit) -> str:
    """Read the original bot's private socket directory from its frozen unit."""
    declaration = unit.declaration
    if len(unit.installed) != 1:
        raise ActivationError("original bot has no exact installed launch definition")
    if declaration.source.suffix == ".plist":
        original = _darwin_source(unit.installed[0].content)
        if (original["label"] != declaration.source.stem
                or original["directory"] != str(declaration.working_directory)):
            raise ActivationError("original bot launch definition differs from its declared owner")
        environment = original["environment"]
    elif declaration.source.suffix == ".service":
        environment = _environment(dict(unit.properties).get("Environment", ""))
    else:
        raise ActivationError("original bot has no supported launch definition")
    if any(environment.get(key) != value for key, value in declaration.environment):
        raise ActivationError("original bot launch environment differs from its declared owner")
    tmpdir = environment.get("TMUX_TMPDIR")
    if not isinstance(tmpdir, str) or not Path(tmpdir).is_absolute():
        raise ActivationError("original bot has no absolute private tmux directory")
    return tmpdir


def _legacy_bot_socket(unit, *, require_for_active=True):
    """Observe only the private server selected by this old bot unit."""
    tmpdir = _original_bot_tmpdir(unit)
    if (unit.declaration.bot is None
            or unit.declaration.working_directory.name != unit.declaration.bot):
        raise ActivationError("original bot has no exact private tmux target")
    socket_path = Path(tmpdir) / f"tmux-{os.getuid()}" / unit.declaration.source.stem
    try:
        info = socket_path.lstat()
    except FileNotFoundError:
        if require_for_active and dict(unit.properties).get("ActiveState") == "active":
            raise ActivationError("active legacy bot has no private tmux socket")
        return socket_path, False
    except OSError as exc:
        raise ActivationError("legacy private tmux socket cannot be observed") from exc
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ActivationError("legacy private tmux socket is foreign or not a socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(str(socket_path))
        except ConnectionRefusedError:
            if require_for_active and dict(unit.properties).get("ActiveState") == "active":
                raise ActivationError("active legacy bot has a refused private tmux socket")
            return socket_path, False
        except OSError as exc:
            raise ActivationError("legacy private tmux socket cannot be probed") from exc
    return socket_path, True


def _legacy_phase_membership(plan, inventory, source_plan=None):
    from .activation_enrollment import _target
    def phase_map(frozen):
        result = {}
        for declaration, item in planned_units(frozen, inventory.manager):
            if item["enroll"]:
                target = _target(inventory.manager, _catalog(inventory.catalog)[1], declaration.source)
                if target in result:
                    raise ActivationError("unit phase is ambiguous")
                result[target] = item["phase"]
        return result

    candidate_phases = phase_map(plan)
    phase_by_target = phase_map(source_plan) if source_plan is not None else candidate_phases
    for target, phase in phase_by_target.items():
        if target in candidate_phases and candidate_phases[target] != phase:
            raise ActivationError("candidate changes a retained native unit phase")
    phases = {phase: [] for phase in units.PHASES}
    for unit in inventory.units:
        if not unit.installed:
            continue
        phase = phase_by_target.get(unit.target)
        if phase not in phases:
            raise ActivationError("old owned unit has no frozen source phase")
        phases[phase].append(unit.target)
    if not phases["ingest"] or len(phases["ingest"]) != 1:
        raise ActivationError("first adoption requires one exact existing ingest unit")
    return phases


def _source_handoff_roster(source_plan, bot_dirs, package):
    """Use frozen selected identities even when authoring removes an old bot."""
    from .active_config import context_from_plan
    roster = {}
    for fleet in source_plan.fleets:
        context = context_from_plan(source_plan, fleet, package=package)
        installed = tuple(bot for bot in context.fleet.bots if (fleet, bot) in bot_dirs)
        if installed:
            if context.fleet.manager not in installed:
                raise ActivationError("old fleet manager has no installed handoff owner")
            roster[fleet] = (context.fleet.manager, installed)
    if set(bot_dirs) != {(fleet, bot) for fleet, (_, bots) in roster.items() for bot in bots}:
        raise ActivationError("old bot handoff roster differs from selected configuration")
    return roster


def _legacy_quiet(adapter, pause, phase, sockets):
    for unit in pause.units(phase):
        assert_quiescent(adapter, installed_file=Path(unit["installed"][0]["path"]),
                         target=unit["target"], socket_path=sockets.get(unit["target"]))


def _running_activation(root: Path, activation_id: str, plan_id: str,
                        install_directory: Path, *, legacy_source: bool,
                        adapter: Adapter | None = None) -> ActivationRecord:
    """Quiesce an enrolled estate, then install one reviewed sealed candidate.

    First adoption has no sealed recovery floor. An upgrade binds the selected
    active release and its saved configuration as the original owner. Both use
    the same durable pause, migration, publication and serial start sequence.
    """
    root = Path(root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ActivationError("activation requires an explicit existing absolute data root")
    root = root.resolve()
    plan = read_plan(root, plan_id)
    release = read_release(root, plan.release_id)
    package, identity = get_resources(), RuntimeIdentity.current()
    if (release.seal_sha256 != plan.release_seal
            or identity != RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)
            or package.native != release.native_path or package.artifact_id != release.inputs.artifact_id
            or Path(sys.executable).absolute() != release.directory / release.paths.interpreter):
        raise ActivationError("run activation using the exact sealed candidate interpreter and package")
    adapter = adapter if adapter is not None else Adapter(package)
    if adapter.package.native != release.native_path:
        raise ActivationError("activation adapter differs from the candidate release")
    with locked_activation(root) as store:
        selected = read_selection(root)
        if legacy_source and selected is not None:
            raise ActivationError("first adoption refuses an existing release selection")
        if not legacy_source and selected is None:
            raise ActivationError("upgrade requires an active selected release")
        for prior in (root / "state/activations").glob("*/activation.json"):
            record = read_activation(root, prior.parent.name)
            if record.status not in {"active", "rolled_back"}:
                raise ActivationError(f"existing activation {record.activation_id} requires explicit repair")
            if legacy_source:
                raise ActivationError(f"existing activation {record.activation_id} requires explicit repair")
        plan.check_fresh()
        source = source_plan = None
        if legacy_source:
            declarations = _legacy_declarations(plan)
        else:
            previous = read_activation(root, selected["activation_id"])
            if (previous.status != "active"
                    or previous.body["intent"]["release_id"] != selected["release_id"]
                    or previous.body["intent"]["plan_id"] != selected["plan_id"]
                    or (selected["release_id"] == release.release_id
                        and selected["plan_id"] == plan.plan_id)):
                raise ActivationError("selected activation is incomplete or candidate plan is already active")
            source = read_release(root, selected["release_id"])
            source_plan = read_plan(root, selected["plan_id"])
            if (source_plan.release_id != source.release_id
                    or source_plan.release_seal != source.seal_sha256):
                raise ActivationError("selected configuration differs from its sealed release")
            # A completed plan's before-state is necessarily stale after its
            # own apply. Bind its retained unit bytes to current generated
            # files instead; enrollment checks installed and loaded state.
            observed_manager = _catalog(adapter.read("svc_inventory_catalog"))[0]
            declarations = current_declarations(source_plan, observed_manager)
        inventory = collect_enrollment(root, declarations, legacy_source=legacy_source,
                                       adapter=adapter).require_complete()
        if legacy_source and inventory.manager != "Darwin":
            raise ActivationError("unsealed first adoption currently supports the observed Darwin host only")
        manager, domain, directories, _, _ = _catalog(inventory.catalog)
        install_directory = Path(install_directory)
        if (not install_directory.is_absolute() or install_directory.resolve() != install_directory
                or install_directory not in directories):
            raise ActivationError("install directory is not an observed native search path")
        candidates = planned_units(plan, manager)
        starts = {}
        for declaration, item in candidates:
            if item["enroll"]:
                starts[str(declaration.source)] = (declaration, item,
                    validate_unit_admission(release, declaration, item, plan.blob(item["sha256"])))
        candidate_targets = {enrollment._target(inventory.manager, _catalog(inventory.catalog)[1], declaration.source)
                             for declaration, item in candidates if item["enroll"]}
        if not legacy_source and manager == "Darwin":
            # The source inventory freezes old labels, but a newly enrolled
            # candidate label can also have a persistent launchd override.
            # Read that same native owner once before any activation record or
            # producer pause; launchd will not start a disabled candidate.
            disabled = _darwin_disabled(adapter.read("svc_inventory_disabled", domain))
            blocked = sorted(target for target in candidate_targets
                             if disabled.get(target.rsplit("/", 1)[-1]) == "disabled")
            if blocked:
                raise ActivationError("candidate launchd unit has a persistent disabled override: "
                                      + ", ".join(blocked)
                                      + "; review and explicitly enable the unit or unenroll it in config")
        retired_units = tuple(unit for unit in inventory.units
                              if unit.installed and unit.target not in candidate_targets)
        rank, contexts = _roster(plan, candidates, package)
        phases = _legacy_phase_membership(plan, inventory, source_plan)
        tmpdirs = {unit.target: _original_bot_tmpdir(unit) for unit in inventory.units
                   if unit.installed and unit.declaration.scope == "bot"}
        for unit in inventory.units:
            if unit.installed and adapter.call("svc_activation_assert_external",
                    unit.installed[0].path, unit.target, str(os.getpid())).returncode:
                raise ActivationError("activation caller is hosted or native ownership is unknown")
        store.prepare(activation_id, plan,
                      recovery_release_id=source.release_id if source else release.release_id,
                      source_release_id=source.release_id if source else None,
                      enrollment_digest=inventory.digest, legacy_source=legacy_source)
        config_install.prepare_config(plan, activation_id)
        pause = units.prepare_unit_pause(store, activation_id, inventory, phases, adapter=adapter)
        store.begin(activation_id, "producers_paused")
        evidence = units.pause_phase(store, activation_id, "producers", adapter=adapter)
        _legacy_quiet(adapter, pause, "producers", {})
        store.complete(activation_id, "producers_paused", evidence_digest=evidence.digest)
        store.begin(activation_id, "sessions_handed_off")
        sockets = {}
        live_sockets = set()
        for unit in inventory.units:
            if not unit.installed or unit.declaration.scope != "bot":
                continue
            socket_path, present = _legacy_bot_socket(unit)
            sockets[unit.target] = socket_path
            if present:
                live_sockets.add(unit.target)
            if present and adapter.call("svc_activation_handoff",
                                        unit.declaration.working_directory,
                                        unit.declaration.source.stem,
                                        tmpdirs[unit.target], timeout=45).returncode:
                raise ActivationError("legacy bot handoff did not complete")
        store.complete(activation_id, "sessions_handed_off", evidence_digest=_digest(
            {"old_bots": sorted(sockets), "handoff": "existing-door-attempted-or-private-server-absent"}))
        store.begin(activation_id, "sessions_quiesced")
        evidence = units.pause_phase(store, activation_id, "bots", adapter=adapter)
        old_bots = {unit.target: unit for unit in inventory.units
                    if unit.installed and unit.declaration.scope == "bot"}
        for unit in pause.units("bots"):
            socket_path = sockets.get(unit["target"])
            original = old_bots.get(unit["target"])
            if socket_path is None or original is None:
                raise ActivationError("parked bot has no retained private-server observation")
            observed_path, still_present = _legacy_bot_socket(
                original, require_for_active=False)
            if observed_path != socket_path or (still_present and unit["target"] not in live_sockets):
                raise ActivationError("unexpected private bot server after native pause")
            if still_present:
                declaration = unit["declaration"]
                response = adapter.call("svc_activation_stop_private_server",
                                        declaration["working_directory"],
                                        Path(declaration["source"]).stem,
                                        tmpdirs[unit["target"]])
                if response.returncode:
                    raise ActivationError("legacy private bot server did not stop")
        _legacy_quiet(adapter, pause, "bots", sockets)
        store.complete(activation_id, "sessions_quiesced", evidence_digest=evidence.digest)
        store.begin(activation_id, "ingest_quiesced")
        evidence = units.pause_phase(store, activation_id, "ingest", adapter=adapter)
        _legacy_quiet(adapter, pause, "ingest", {})
        if _probe(root) is not None:
            raise ActivationError("old ingest still answers after native pause")
        store.complete(activation_id, "ingest_quiesced", evidence_digest=evidence.digest)
        store.begin(activation_id, "queues_classified")
        migration = build_migration_manifest(root, source, release)
        if migration.blockers:
            raise ActivationError("data or pending queues block activation: " + "; ".join(migration.blockers))
        from .activation_handoffs import persist_canonical_handoffs
        bot_dirs = {(unit.declaration.fleet, unit.declaration.bot): unit.declaration.working_directory
                    for unit in inventory.units if unit.installed and unit.declaration.scope == "bot"}
        # Newly composed bots have no previous session or actor to hand off.
        # The handoff owner still checks every active historical assignment.
        roster = (_source_handoff_roster(source_plan, bot_dirs, package) if source_plan is not None else
                  {context.fleet.name: (context.fleet.manager,
                      tuple(bot for bot in context.fleet.bots
                            if (context.fleet.name, bot) in bot_dirs))
                   for context in contexts
                   if any(fleet == context.fleet.name for fleet, _ in bot_dirs)})
        persist_canonical_handoffs(root, roster=roster, bot_dirs=bot_dirs,
                                   expected_audit=migration.task_audit,
                                   candidate_bots={(context.fleet.name, bot)
                                                   for context in contexts for bot in context.fleet.bots})
        # migration_apply binds this evidence slot to the exact manifest ID.
        store.complete(activation_id, "queues_classified", evidence_digest=migration.manifest_id[2:])
        store.begin(activation_id, "backup_saved")
        apply_migration(store, activation_id, migration)
        store.begin(activation_id, "selection_switched")
        store.select(activation_id)
        store.begin(activation_id, "configuration_applied")
        applied = config_install.apply_config(root, activation_id)
        store.complete(activation_id, "configuration_applied", evidence_digest=_digest({
            "plan_id": applied.plan_id, "status": applied.status, "progress": applied.progress}))
        enrollment.prepare_candidate_enrollment(store, activation_id,
                configuration_journal=activation_id, install_directory=install_directory, adapter=adapter)
        observations = {}
        for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started")):
            store.begin(activation_id, step)
            registry = _registry_ready(plan, candidates, package) if phase == "bots" else []
            if phase == "bots":
                store.record_identity_bindings(activation_id,
                    identity_bindings_from_registry(plan, registry, package=package), package=package)
            publication = enrollment.install_candidate_units(store, activation_id, phase, adapter=adapter)
            entries = enrollment.candidate_entries(store, activation_id, phase)
            if phase == "bots":
                entries = sorted(entries, key=lambda entry: rank[(starts[entry["source"]][0].fleet,
                                                                  starts[entry["source"]][0].bot)])
            results = []
            for entry in entries:
                _, _, unit = starts[entry["source"]]
                result = start_unit(store, activation_id, installed_file=Path(entry["installed"]),
                                    target=entry["target"], unit=unit, sha256=entry["after"]["sha256"],
                                    adapter=adapter, readiness=(lambda: _ingest_ready(root, release))
                                    if phase == "ingest" else None)
                observations[entry["source"]] = result
                results.append(result.digest)
            enabled = enrollment.verify_candidate_enablement(store, activation_id, phase, adapter=adapter)
            store.complete(activation_id, step, evidence_digest=_digest([publication.digest, results, enabled, registry]))
        store.begin(activation_id, "verified")
        verified = {"ingest": _ingest_ready(root, release), "bots": []}
        for unit in retired_units:
            assert_quiescent(adapter, installed_file=Path(unit.installed[0].path),
                             target=unit.target, socket_path=sockets.get(unit.target))
        for source, result in observations.items():
            declaration, _, unit = starts[source]
            if unit.phase == "bots":
                fence = result.details["readiness"]["fence"]
                kind = result.details["readiness"]["kind"]
                if (kind not in BOT_READY_KINDS or adapter.read("svc_activation_bot_ready", root,
                        declaration.working_directory, "0", fence).strip() != kind):
                    raise ActivationError("candidate bot lost readiness before producer admission")
                verified["bots"].append(result.digest)
        store.complete(activation_id, "verified", evidence_digest=_digest(verified))
        store.begin(activation_id, "producers_resumed")
        publication = enrollment.install_candidate_units(store, activation_id, "producers", adapter=adapter)
        results = []
        for entry in enrollment.candidate_entries(store, activation_id, "producers"):
            _, _, unit = starts[entry["source"]]
            result = start_unit(store, activation_id, installed_file=Path(entry["installed"]),
                                target=entry["target"], unit=unit, sha256=entry["after"]["sha256"], adapter=adapter)
            results.append(result.digest)
        enabled = enrollment.verify_candidate_enablement(store, activation_id, "producers", adapter=adapter)
        return store.complete(activation_id, "producers_resumed", evidence_digest=_digest({
            "publication": publication.digest, "native_starts": results, "enablement": enabled,
            "legacy_source": legacy_source, "watchdog_ticks": "admitted only after active"}))


def adopt_existing_activation(root: Path, activation_id: str, plan_id: str,
                              install_directory: Path, *, adapter: Adapter | None = None) -> ActivationRecord:
    """First activation of an already running, unsealed Darwin estate."""
    return _running_activation(root, activation_id, plan_id, install_directory,
                               legacy_source=True, adapter=adapter)


def upgrade_activation(root: Path, activation_id: str, plan_id: str,
                       install_directory: Path, *, adapter: Adapter | None = None) -> ActivationRecord:
    """Upgrade an active selected release with that seal as the recovery floor."""
    return _running_activation(root, activation_id, plan_id, install_directory,
                               legacy_source=False, adapter=adapter)
