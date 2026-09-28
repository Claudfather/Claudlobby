"""Cold-host activation only, through existing durable owners.

No upgrades, retries, rollback or native discovery fallback. A failed begun
step remains pending for explicit recovery; calling bootstrap again refuses.
The supplied root, reviewed plan and sealed executing release are mandatory.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import sys
import time

from .activation_state import ActivationError, ActivationRecord, locked_activation, read_activation, read_selection
from .activation_identity import identity_bindings_from_registry
from . import activation_enrollment as enrollment, activation_units as units, config_install
from .activation_runtime import start_unit
from .config_plan import path_state, read_plan
from .config_units import planned_units
from .migration_apply import apply_migration
from .migration_plan import build_migration_manifest
from .plane.db import db_file
from .releases import read_release
from .resources import get_resources
from .runtime_admission import RuntimeIdentity, validate_unit_admission
from .supervision_inventory import Adapter, _catalog, collect_enrollment


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
                if adapter.read("svc_activation_bot_ready", root, declaration.working_directory, "0", fence).strip() != "bridge-ready":
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
