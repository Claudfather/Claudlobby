"""Cold-host, first-adoption and selected-release activation through durable owners.

No automatic retry, rollback or native discovery fallback. A failed
begun step remains pending for explicit recovery. The supplied root, reviewed
plan and sealed executing release are mandatory.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

from .activation_state import (ACTIVATION_WAIT_S, START_STEPS, STEPS, ActivationError, ActivationRecord,
                               ActivationRefusal, CandidateDisabledOverride, _RUNNING_QUIESCE_STEPS, lock_busy,
                               handoff_request_started_ns, locked_activation, read_activation, read_selection,
                               resumable_running_step)
from .activation_identity import identity_bindings_from_registry
from . import activation_enrollment as enrollment, activation_units as units, config_install
from .activation_runtime import (RuntimeEvidence, RuntimeEvidenceError, assert_quiescent,
                                 observe_started_unit, start_unit)
from .config_plan import path_state, read_plan
from .config_units import current_declarations, planned_units
from .migration_apply import apply_migration, read_migration
from .migration_plan import MigrationManifest, build_migration_manifest
from .plane.db import db_file
from .releases import read_release
from .resources import get_resources
from .runtime_admission import RuntimeIdentity, validate_unit_admission
from .supervision_inventory import (Adapter, EnrolledUnit, FileSnapshot, InventoryError, UnitDeclaration,
                                    _catalog, _darwin_disabled, _darwin_source, _environment,
                                    collect_enrollment, is_unit_template, legacy_linux_declarations)


# A live preview can race normal writers; these facts need the authoritative
# quiesced pass. Every other blocker is a reason not to stop the old fleet.
_LIVE_PREVIEW_CHURN = frozenset({
    "database bytes changed during preview; repeat under quiescence",
    "receipt inventory changed during preview",
    "inflight spool claims require a quiesced ownership/recovery check",
})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _prepare_no_effect_journals(store, activation_id, plan, inventory, phases, adapter):
    """Cancel a prepared intent only when its journal owners prove zero effects."""
    try:
        config_install.prepare_config(plan, activation_id)
        return units.prepare_unit_pause(store, activation_id, inventory, phases, adapter=adapter)
    except Exception:
        try:
            store.cancel_prepared(activation_id)
        except Exception:
            pass  # Preserve the original refusal; an unproven record stays blocking.
        raise


def _bootstrap_plane(root: Path, release) -> bool:
    """Return whether the bootstrap owns restored data instead of an empty DB."""
    from .host_backup import restored_bootstrap
    if restored_bootstrap(root, release):
        return True
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
    return False


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


def _preflight_candidate_placement(inventory, candidates, install_directory: Path) -> None:
    """Refuse foreign candidate labels before preparing or pausing an activation.

    Enrollment repeats these checks after parking. Here the frozen original
    inventory is still installed, so only its exact paths and bytes may occupy
    a candidate label in any native search directory.
    """
    manager, domain, directories, _, loaded = _catalog(inventory.catalog)
    originals = {unit.target: unit for unit in inventory.units}
    for declaration, item in candidates:
        if not item["enroll"]:
            continue
        target = enrollment._target(manager, domain, declaration.source)
        prior = originals.get(target)
        owned = prior.installed[0] if prior and len(prior.installed) == 1 else None
        destination = Path(owned.path) if owned else install_directory / declaration.source.name
        if destination == declaration.source:
            raise ActivationError("generated source cannot also be an installed unit")
        for directory in directories:
            path = directory / destination.name
            if path_state(path)["node"]["kind"] == "absent":
                continue
            if owned is None or path != Path(owned.path):
                raise ActivationError(f"foreign candidate collision before activation: {path}")
            try:
                if FileSnapshot.read(path) != owned:
                    raise ActivationError(f"changed original candidate before activation: {path}")
            except (OSError, InventoryError) as exc:
                raise ActivationError(f"original candidate cannot be verified: {path}") from exc
        if destination.name in loaded and owned is None:
            raise ActivationError(f"foreign loaded candidate before activation: {target}")


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
    with locked_activation(root, wait=ACTIVATION_WAIT_S) as store:
        if read_selection(root) is not None:
            raise ActivationError("bootstrap refuses an existing release selection")
        for prior in (root / "state/activations").glob("*/activation.json"):
            record = read_activation(root, prior.parent.name)
            raise ActivationError(f"existing activation {record.activation_id} requires explicit recovery, not bootstrap")
        _bootstrap_plane(root, release)
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
            if item["enroll"] and not is_unit_template(declaration.source.name):
                start = validate_unit_admission(release, declaration, item, plan.blob(item["sha256"]))
                starts[str(declaration.source)] = (declaration, item, start)
                if item["phase"] == "bots" and os.path.lexists(declaration.working_directory):
                    raise ActivationError("bootstrap refuses pre-existing bot runtime directories")
        rank, _ = _roster(plan, declarations, package)
        from .plane.owner_service import preflight as owner_service_preflight
        owner_service_preflight(starts)
        if sum(item["phase"] == "ingest" and item["enroll"] for _, item in declarations) != 1:
            raise ActivationError("bootstrap requires exactly one declared ingest unit")
        # An empty original roster cannot supply the caller checks used by the
        # upgrade parking owner. Check exact candidate targets before preparing
        # SQL/config changes; hosted or unknown ancestry is not permission.
        for declaration, item in declarations:
            if (item["enroll"] and not is_unit_template(declaration.source.name)
                    and adapter.call("svc_activation_assert_external",
                    install_directory / declaration.source.name,
                    enrollment._target(manager, domain, declaration.source), str(os.getpid())).returncode):
                raise ActivationError("bootstrap caller is hosted or cannot be proved external; use an operator shell")
        store.prepare(activation_id, plan, recovery_release_id=release.release_id,
                      source_release_id=release.release_id, enrollment_digest=inventory.digest,
                      install_directory=install_directory)
        _prepare_no_effect_journals(store, activation_id, plan, inventory,
                                    {phase: [] for phase in units.PHASES}, adapter)
        for step, phase in (("producers_paused", "producers"), ("sessions_handed_off", None),
                            ("sessions_quiesced", "bots"), ("ingest_quiesced", "ingest")):
            store.begin(activation_id, step)
            _bootstrap_plane(root, release)
            evidence = (units.pause_phase(store, activation_id, phase, adapter=adapter).digest if phase else
                        _digest({"bootstrap_empty": True, "sessions": [], "handoffs": []}))
            store.complete(activation_id, step, evidence_digest=evidence)
        return _finish_bootstrap_activation(root, store, activation_id, plan, release,
            declarations, starts, rank, install_directory, adapter, package)


def _finish_bootstrap_activation(root, store, activation_id, plan, release,
                                 declarations, starts, rank, install_directory, adapter, package):
    record = read_activation(root, activation_id)
    completed = set(record.body["completed"])
    if "queues_classified" not in completed:
        store.begin(activation_id, "queues_classified")
        restored = _bootstrap_plane(root, release)
        migration = build_migration_manifest(root, release, release, initialize_empty=not restored)
        if migration.blockers or any(row["file_count"] != 0 for row in migration.queues.values()):
            raise ActivationError("bootstrap migration/queue proof is blocked")
        store.complete(activation_id, "queues_classified", evidence_digest=migration.manifest_id[2:])
    else:
        journal = read_migration(root, activation_id)
        if journal is not None:
            payload = dict(journal["manifest"])
            if payload.pop("schema", None) != 1:
                raise ActivationError("saved bootstrap migration manifest has an unknown schema")
            migration = MigrationManifest(**payload)
        else:
            restored = _bootstrap_plane(root, release)
            migration = build_migration_manifest(root, release, release, initialize_empty=not restored)
        if migration.manifest_id[2:] != record.body["evidence"]["queues_classified"]:
            raise ActivationError("bootstrap queues differ from their recorded migration manifest")
    if "migration_applied" not in completed:
        if "backup_saved" not in completed:
            store.begin(activation_id, "backup_saved")
        apply_migration(store, activation_id, migration)
    if "selection_switched" not in completed:
        store.begin(activation_id, "selection_switched")
        store.select(activation_id)
    if "configuration_applied" not in completed:
        store.begin(activation_id, "configuration_applied")
        applied = config_install.apply_config(root, activation_id)
        store.complete(activation_id, "configuration_applied", evidence_digest=_digest({
            "plan_id": applied.plan_id, "status": applied.status, "progress": applied.progress}))
    return _candidate_startup(root, store, activation_id, plan, release, declarations,
        starts, rank, install_directory, adapter, package, bootstrap=True)


def _original_bot_tmpdir(unit, *, allow_uninstalled=False) -> str:
    """Read the original bot's private socket directory from its frozen unit."""
    declaration = unit.declaration
    if not unit.installed and allow_uninstalled:
        # collect_enrollment bound these generated bytes to the selected plan.
        # No inherited TMPDIR or bot.conf fallback is an ownership witness.
        return _published_tmux_dir(declaration.source, unit.generated.content)
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


def _legacy_bot_socket(unit, *, require_for_active=True, allow_uninstalled=False):
    """Observe only the private server selected by this old bot unit."""
    tmpdir = _original_bot_tmpdir(unit, allow_uninstalled=allow_uninstalled)
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


def _snapshot_bot_states(inventory, candidates, adapter):
    """Freeze only positively observed bot intent, under the activation lock."""
    expected = {(d.fleet, d.bot): d for d, item in candidates
                if d.scope == "bot" and item["enroll"]}
    originals = {(u.declaration.fleet, u.declaration.bot): u for u in inventory.units
                 if u.declaration.scope == "bot"}
    if not expected or set(expected) != set(originals):
        raise ActivationRefusal("preserving bot state requires the unchanged selected bot roster")
    result = {}
    for key, unit in sorted(originals.items()):
        old, candidate = unit.declaration, expected[key]
        if old.source != candidate.source or old.working_directory != candidate.working_directory:
            raise ActivationRefusal("preserving bot state requires unchanged bot placement and labels")
        properties = dict(unit.properties)
        if unit.installed:
            installed = unit.installed[0].path
            if FileSnapshot.read(Path(installed)) != unit.installed[0]:
                raise ActivationRefusal("bot installed definition changed before the first pause")
        else:
            placement = enrollment.selected_bot_entry(inventory.data_root, old.fleet, old.bot,
                                                      inventory.manager)
            if placement["target"] != unit.target or placement["source"] != str(old.source):
                raise ActivationRefusal("stopped bot has no exact selected native placement")
            installed = placement["installed"]
        observed = adapter.read("svc_inventory_state", installed, unit.target).strip().split()
        if len(observed) != 3 or observed[1] not in {"loaded", "unloaded", "not-found"}:
            raise ActivationRefusal("bot native enrollment is transient or unreadable")
        active = observed[2]
        if unit.installed and active != properties.get("ActiveState"):
            raise ActivationRefusal("bot native state changed during disposition observation")
        if active not in {"active", "inactive"}:
            raise ActivationRefusal("bot native state is transient or unreadable")
        path, present = _legacy_bot_socket(unit, require_for_active=False, allow_uninstalled=True)
        if present:
            if not unit.installed:
                raise ActivationRefusal("uninstalled bot has a live private server")
            observed = adapter.read("svc_bot_session_observe", old.working_directory,
                                    old.source.stem, _original_bot_tmpdir(unit),
                                    unit.installed[0].path, unit.target).strip()
            if observed != "ready":
                raise ActivationRefusal("live private bot session ownership or readiness is unknown")
        elif active == "active":
            raise ActivationRefusal("active bot native unit has no live private server")
        result[unit.target] = {"disposition": "restart" if present else "stopped",
            "source": str(old.source), "working_directory": str(old.working_directory),
            "socket_path": str(path), "tmux_tmpdir": _original_bot_tmpdir(unit, allow_uninstalled=True),
            "installed": bool(unit.installed), "installed_path": installed, "native_active": active,
            "session": "ready" if present else "server_absent"}
    return result


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
    """Use frozen selected identities even when authoring removes an old bot.

    Manager coverage belongs to the shared handoff owner, which checks it for
    both upgrade and first-adoption rosters.
    """
    from .active_config import context_from_plan
    roster = {}
    for fleet in source_plan.fleets:
        context = context_from_plan(source_plan, fleet, package=package)
        installed = tuple(bot for bot in context.fleet.bots if (fleet, bot) in bot_dirs)
        if installed:
            roster[fleet] = (context.fleet.manager, installed)
    if set(bot_dirs) != {(fleet, bot) for fleet, (_, bots) in roster.items() for bot in bots}:
        raise ActivationError("old bot handoff roster differs from selected configuration")
    return roster


def _handoff_refresh_time(root: Path, activation_id: str, now: datetime) -> datetime:
    """When the handoffs' references count as refreshed, from the activation
    records and never from a handoff file, which any session can edit (#2094).

    A retried step keeps the time its first attempt recorded. An activation
    within 12 h of the previous one's recorded refresh keeps that time. Otherwise,
    including after an activation that recorded none, it is now.
    """
    def parse(value):
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    body = read_activation(root, activation_id).body
    if body.get("handoff_refreshed"):
        return parse(body["handoff_refreshed"])
    previous = body.get("previous_selection")
    if previous:
        try:
            prior = read_activation(root, previous["activation_id"]).body.get("handoff_refreshed")
        except ActivationError:
            prior = None
        if prior and timedelta(0) <= now - parse(prior) < timedelta(hours=12):
            return parse(prior)
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _handoff_inputs(old_units, source_plan, contexts, package, *, bot_states=None):
    """Old handoff roster, exact bot directories and retained candidate bots.

    Shared by the live pre-record preflight and the authoritative quiesced
    write, so both judge the same frozen old units and reviewed configuration.
    A preserved, positively stopped selected bot retains its handoff ownership
    after native de-enrollment; default and adoption mapping remain installed-only.
    """
    bot_dirs = {}
    for unit in old_units:
        declaration = unit.declaration
        if declaration.scope != "bot":
            continue
        if not unit.installed:
            if source_plan is None or bot_states is None:
                continue
            witness = bot_states.get(unit.target, {})
            if (witness.get("disposition") != "stopped"
                    or witness.get("session") != "server_absent"
                    or witness.get("source") != str(declaration.source)
                    or witness.get("working_directory") != str(declaration.working_directory)
                    or witness.get("installed") is not False
                    or witness.get("native_active") != "inactive"):
                raise ActivationRefusal("stopped bot handoff owner lacks its exact preserved witness")
        bot_dirs[(declaration.fleet, declaration.bot)] = declaration.working_directory
    roster = (_source_handoff_roster(source_plan, bot_dirs, package) if source_plan is not None else
              {context.fleet.name: (context.fleet.manager,
                  tuple(bot for bot in context.fleet.bots
                        if (context.fleet.name, bot) in bot_dirs))
               for context in contexts
               if any(fleet == context.fleet.name for fleet, _ in bot_dirs)})
    candidate_bots = {(context.fleet.name, bot) for context in contexts for bot in context.fleet.bots}
    return roster, bot_dirs, candidate_bots


LEGACY_RUNNER_PAUSE = "autonomous-runner.paused"


def _refuse_unrecorded_run_intent(root: Path, old_units, contexts, *, bot_states=None) -> None:
    """Refuse, before any record, work the operator deliberately stopped or paused.

    Activation starts every candidate bot, and the new runner gate reads only
    recorded automation state. So a frozen old bot unit that is uninstalled,
    and a legacy runner pause marker without a recorded pause, both need an
    explicit operator decision first. This reads only; no marker or state is
    changed.

    Only an uninstalled unit is an explicit stop: ``bot stop`` and legacy
    spin-down disable and remove the unit file. An installed but inactive bot,
    whether enabled, disabled or unloaded, is still supervised, because the
    keepalive restarts any bot whose unit file exists, so it is not treated as
    stopped. Only bot-scope units are considered, so a timer's normal
    inactive state never counts.
    """
    from .automation_state import AutomationStateError, status
    candidates = {(context.fleet.name, bot): context for context in contexts for bot in context.fleet.bots}
    stopped, paused = [], []
    for unit in old_units:
        declaration = unit.declaration
        key = (declaration.fleet, declaration.bot)
        if declaration.scope != "bot" or key not in candidates:
            continue  # A bot the candidate omits is retired, never started.
        if not unit.installed and not (bot_states and bot_states.get(unit.target, {}).get("disposition") == "stopped"):
            stopped.append(f"{key[0]}/{key[1]}")
    for (fleet, bot), context in sorted(candidates.items()):
        if context.fleet.bots[bot].autonomous_runner is None:
            continue  # An unconfigured runner stays ineligible under the new gate.
        directories = {Path(unit.declaration.working_directory) for unit in old_units
                       if unit.declaration.scope == "bot"
                       and (unit.declaration.fleet, unit.declaration.bot) == (fleet, bot)}
        directories.add(Path(context.paths.bot_runtime(bot)))
        if not any(os.path.lexists(directory / LEGACY_RUNNER_PAUSE) for directory in directories):
            continue
        try:
            recorded = status(root, fleet, bot, configured=True)["paused"]
        except AutomationStateError:
            recorded = False  # Unverifiable state is not a recorded pause.
        if not recorded:
            paused.append(f"{fleet}/{bot}")
    reasons = []
    if stopped:
        reasons.append("activation would start deliberately stopped bots (" + ", ".join(stopped)
                       + "); remove them from the reviewed candidate fleet and re-run config plan, "
                       "or start them deliberately with the host's current supervisor, then retry")
    if paused:
        reasons.append(f"legacy {LEGACY_RUNNER_PAUSE} markers have no recorded automation pause ("
                       + ", ".join(paused) + "); the new runner gate would resume them. Remove "
                       "autonomous_runner from those bots in the reviewed candidate and re-plan "
                       "(record `bot automation pause` after activation before restoring it), "
                       "or remove the marker only if the runner may resume")
    if reasons:
        raise ActivationRefusal("run intent blocks activation before any record: " + "; ".join(reasons))


def _legacy_quiet(adapter, pause, phase, sockets, *, candidate_started=frozenset()):
    for unit in pause.units(phase):
        if is_unit_template(unit["target"]):
            continue  # no process runs as a template; its instances are its own units
        if unit["target"] in candidate_started:
            continue  # Exact saved candidate intent is reconciled by its readiness/native owner.
        assert_quiescent(adapter, installed_file=Path(unit["installed"][0]["path"]),
                         target=unit["target"], socket_path=sockets.get(unit["target"]))


def _oneshot_producer(phase: str, installed) -> bool:
    """A directly started producer service runs once: with no recorded result,
    whether it ran is unknown, so neither the resume nor a repair may start it again."""
    return phase == "producers" and Path(installed).suffix == ".service"


def _startable_producers(entries):
    """Publish paired services, but start only their declared timers.

    A unit template is published and never started: only OnFailure= starts
    its instances (#2522)."""
    timer_services = {entry["service"] for entry in entries if entry["service"]}
    return (tuple(entry for entry in entries
                  if Path(entry["installed"]).name not in timer_services
                  and not is_unit_template(Path(entry["installed"]).name)), timer_services)


def _require_preserved_handoff(bot_dir, *, not_before=None):
    """Use the public handoff owner's capture verifier, never the file's mtime alone."""
    from .bot_operations import BotLifecycleError, _fresh_self_handoff
    try:
        _fresh_self_handoff(bot_dir, not_before=not_before)
    except BotLifecycleError as exc:
        raise ActivationRefusal("preserve-state activation requires a fresh verified session handoff; "
                                "inspect the live session and saved handoff, then retry the same activation "
                                "with --resume; existing handoff attempts are never resent") from exc


def _bot_label(unit):
    """The fleet/bot a bot unit declares: the name an operator knows it by."""
    return f"{unit.declaration.fleet}/{unit.declaration.bot}"


def _handoff_request_time(unit, handed_off, old_bots):
    """Time a preserved handoff request on the clock that stamps its handoff file (#2528).

    A refusal names the bot and its handoff directory, and the bots this activation has
    already handed off (``handed_off``, their targets), whose handoffs a retry keeps (#2547).
    """
    from .bot_operations import _HANDOFF_FRESH_S, BotLifecycleError, _handoff_clock_ns
    bot_dir = unit.declaration.working_directory
    try:
        return _handoff_clock_ns(bot_dir)
    except BotLifecycleError as exc:
        names = [_bot_label(old_bots[target]) if target in old_bots else target for target in handed_off]
        earlier = (f"already handed off in this activation: {', '.join(names)}; a retry keeps those handoffs "
                   "and does not request them again, and refuses if one is more than "
                   f"{_HANDOFF_FRESH_S} s old when it stops the sessions" if names
                   else "no bot has been handed off in this activation")
        raise ActivationRefusal(f"preserve-state activation cannot time its handoff request to {_bot_label(unit)} "
                                f"in that bot's handoff directory {bot_dir / '.claude'}, so no handoff was "
                                "requested from it; inspect that directory, then retry the same activation "
                                f"with --resume; {earlier}") from exc


def _quiesce_running(root, store, activation_id, pause, old_units, adapter):
    """Run or resume the running-estate pause steps under one owner.

    Parking resumes through its ConfigInstall journal and the native pause
    converges on the frozen original state. A handoff injects into a live
    session, so each bot's intent is recorded first: a recorded result is never
    repeated and an intent without one refuses. A private server is stopped
    only while still observed and witnessed live at handoff.
    """
    record = read_activation(root, activation_id)
    completed = record.body["completed"]
    preserve = record.body["intent"].get("preserve_bot_state") is True
    old_bots = {unit.target: unit for unit in old_units
                if unit.installed and unit.declaration.scope == "bot"}
    if "producers_paused" not in completed:
        record = read_activation(root, activation_id)
        saved_states = record.body["intent"].get("bot_states")
        if saved_states is not None:
            # This also runs on a resumed first pause: pre-quiescence intent
            # stays frozen, and a changed bot never silently changes that map.
            frozen = pause.enrollment
            inventory = SimpleNamespace(units=old_units, catalog=frozen["catalog"],
                                        data_root=root, manager=frozen["manager"])
            candidates = planned_units(read_plan(root, record.body["intent"]["plan_id"]), frozen["manager"])
            if record.body["pending"] is None:
                _preflight_candidate_placement(inventory, candidates,
                                               Path(record.body["intent"]["install_directory"]))
            if _snapshot_bot_states(inventory, candidates, adapter) != saved_states:
                raise ActivationError("bot state changed before the first activation pause")
        store.begin(activation_id, "producers_paused")
        evidence = units.pause_phase(store, activation_id, "producers", adapter=adapter)
        _legacy_quiet(adapter, pause, "producers", {})
        store.complete(activation_id, "producers_paused", evidence_digest=evidence.digest)
    sockets = {target: _legacy_bot_socket(unit, require_for_active=False)[0]
               for target, unit in old_bots.items()}
    if "sessions_handed_off" not in completed:
        if read_activation(root, activation_id).body["pending"] is None:
            store.arm_handoff_evidence(activation_id)
        store.begin(activation_id, "sessions_handed_off")
        record = read_activation(root, activation_id)
        effects = record.body.get("handoff_effects")
        if not isinstance(effects, dict):
            raise ActivationError("legacy bot handoff has an unknown effect; inspect its session before repair")
        unknown = [target for target, result in effects.items() if result is None]
        if unknown and not preserve:
            raise ActivationError("legacy bot handoff has an unknown effect; inspect its session before repair")
        for target in unknown:
            requested = handoff_request_started_ns(record, target) if preserve else None
            if requested is None or target not in old_bots:
                raise ActivationRefusal("bot handoff has no bound capture witness; inspect its live session "
                                        "before operator recovery; an unknown handoff is never resent")
            _require_preserved_handoff(old_bots[target].declaration.working_directory, not_before=requested)
        for target in unknown:
            # Proof of a late save reconciles the recorded attempt without any native call.
            store.record_handoff(activation_id, target=target, result="handed_off")
        # The handoffs a refusal names, from this run or an earlier one: effects was read
        # before the loop, so the loop adds its own (#2547).
        handed_off = [target for target, result in effects.items() if result == "handed_off" or target in unknown]
        for target, unit in old_bots.items():
            if target in effects:
                continue  # Recorded before interruption; never inject twice.
            _, present = _legacy_bot_socket(unit)
            restart = preserve and record.body["intent"]["bot_states"][target]["disposition"] == "restart"
            if not present:
                if restart:
                    _require_preserved_handoff(unit.declaration.working_directory)
                store.record_handoff(activation_id, target=target, result="server_absent")
                continue
            if preserve and not restart:
                raise ActivationRefusal("preserved stopped bot acquired a live private server before handoff")
            requested = _handoff_request_time(unit, handed_off, old_bots) if preserve else None
            store.record_handoff(activation_id, target=target, result=None,
                                 **({"request_started_ns": requested} if preserve else {}))
            result = adapter.call("svc_activation_handoff", unit.declaration.working_directory,
                                  unit.declaration.source.stem, _original_bot_tmpdir(unit),
                                  *(("explicit",) if preserve else ()), timeout=45)
            if preserve:
                marker = result.stdout.strip().splitlines()[-1:] if result.stdout else []
                if result.returncode or marker not in (["handoff-saved"], ["handoff-skipped:recent"]):
                    raise ActivationRefusal("preserved bot handoff did not produce a verified save; "
                                            "inspect its live session and saved handoff, then retry the same activation "
                                            "with --resume; existing handoff attempts are never resent")
                _require_preserved_handoff(unit.declaration.working_directory,
                    not_before=requested if marker == ["handoff-saved"] else None)
            elif result.returncode:
                raise ActivationError("legacy bot handoff did not complete")
            store.record_handoff(activation_id, target=target, result="handed_off")
            handed_off.append(target)
        store.complete(activation_id, "sessions_handed_off", evidence_digest=_digest(
            {"old_bots": sorted(sockets), "handoff": (
                "explicit-save-and-fresh-capture-verified-or-stopped" if preserve else
                "existing-door-attempted-or-private-server-absent")}))
    if "sessions_quiesced" not in completed:
        if preserve:
            pending_pause = read_activation(root, activation_id).body["pending"] == "sessions_quiesced"
            for target, unit in old_bots.items():
                if record.body["intent"]["bot_states"][target]["disposition"] == "restart":
                    if pending_pause:
                        try:
                            assert_quiescent(adapter, installed_file=Path(unit.installed[0].path),
                                             target=target, socket_path=sockets[target])
                        except RuntimeEvidenceError:
                            pass  # A target still awaiting its stop needs a fresh capture.
                        else:
                            continue  # Its first stop was fenced by the durable begin below.
                    _require_preserved_handoff(unit.declaration.working_directory)
        store.begin(activation_id, "sessions_quiesced")
        # An older record has no witness; any server it left running refuses.
        effects = read_activation(root, activation_id).body.get("handoff_effects") or {}
        live_sockets = {target for target, result in effects.items() if result == "handed_off"}
        evidence = units.pause_phase(store, activation_id, "bots", adapter=adapter)
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
                                        _original_bot_tmpdir(original))
                if response.returncode:
                    raise ActivationError("legacy private bot server did not stop")
        _legacy_quiet(adapter, pause, "bots", sockets)
        store.complete(activation_id, "sessions_quiesced", evidence_digest=evidence.digest)
    if "ingest_quiesced" not in completed:
        store.begin(activation_id, "ingest_quiesced")
        evidence = units.pause_phase(store, activation_id, "ingest", adapter=adapter)
        _legacy_quiet(adapter, pause, "ingest", {})
        if _probe(root) is not None:
            raise ActivationError("old ingest still answers after native pause")
        store.complete(activation_id, "ingest_quiesced", evidence_digest=evidence.digest)
    return sockets


def _completed_adoption_abort(root: Path, record: ActivationRecord) -> bool:
    """A verified early first-adoption abort left no candidate effect behind.

    Only its owner's terminal receipt qualifies; a fresh adoption still
    re-enrolls, re-plans and re-previews SQL from scratch under a new ID.
    """
    body = record.body
    abort = body.get("adoption_abort")
    result = abort.get("result") if isinstance(abort, dict) else None
    return (record.status == "rolled_back"
            and body["intent"].get("source_kind") == "legacy-unsealed"
            and body["intent"].get("source_release_id") is None
            and body["previous_selection"] is None
            and body["completed"] == [] and body["pending"] is None and body["evidence"] == {}
            and body.get("forward") == {"status": "activating", "completed": [],
                                        "pending": "producers_paused", "evidence": {}}
            and isinstance(result, dict)
            and isinstance(result.get("evidence"), str)
            and re.fullmatch(r"[0-9a-f]{64}", result["evidence"]) is not None
            and not body.get("handoff_effects") and not body.get("start_effects")
            and not body.get("start_phases") and "identity_bindings" not in body
            and read_migration(root, record.activation_id) is None)


def _cancelled_before_effects(root: Path, record: ActivationRecord) -> bool:
    """cancel_prepared's receipt for a first adoption, its journals still unstarted.

    The owner proved zero effects when it wrote the receipt; this read-only
    recheck only confirms the named journals were not started since.
    """
    body = record.body
    cancellation = body.get("cancellation")
    if not (record.status == "rolled_back"
            and body["intent"].get("source_kind") == "legacy-unsealed"
            and body["intent"].get("source_release_id") is None
            and body["previous_selection"] is None
            and isinstance(cancellation, dict)
            and cancellation.get("kind") == "prepared-before-effects"
            and cancellation.get("selection_sha256") == _digest(None)
            and isinstance(cancellation.get("journals"), list)
            and body["completed"] == [] and body["pending"] is None and body["evidence"] == {}
            and not body.get("handoff_effects") and not body.get("start_effects")
            and not body.get("start_phases") and "identity_bindings" not in body
            and "forward" not in body and "adoption_abort" not in body
            and read_migration(root, record.activation_id) is None):
        return False
    try:
        journals = [config_install.read_config_install(root, identifier)
                    for identifier in cancellation["journals"]]
    except (config_install.ConfigInstallError, TypeError, ValueError):
        return False
    return all(journal.status == "prepared" and all(row == "pending" for row in journal.progress)
               for journal in journals)


def _check_recovery_readability(manifest, recovery) -> None:
    for after_writes, label in ((False, "post-migration"), (True, "post-target-write")):
        blockers = manifest.readability_blockers(recovery.compatibility,
                                                after_target_writes=after_writes)
        if blockers:
            raise ActivationRefusal(f"recovery release cannot read the {label} state: " + "; ".join(blockers))


def _recorded_recovery(root, record, release, *, forward_recovery=None):
    intent = record.body["intent"]
    mode = intent.get("forward_recovery", False)
    if type(mode) is not bool or (forward_recovery is not None and type(forward_recovery) is not bool):
        raise ActivationRefusal("invalid recorded recovery mode")
    if forward_recovery is not None and forward_recovery != mode:
        raise ActivationRefusal("resume cannot change the recorded recovery mode")
    previous = record.body["previous_selection"]
    expected = release.release_id if mode or previous is None else intent["source_release_id"]
    if (intent["recovery_release_id"] != expected
            or mode and (previous is None or intent.get("source_kind") == "legacy-unsealed"
                         or intent["source_release_id"] != previous["release_id"])):
        raise ActivationRefusal("recorded recovery release differs from its frozen recovery mode")
    return read_release(root, expected)


def _running_activation(root: Path, activation_id: str, plan_id: str,
                        install_directory: Path, *, legacy_source: bool,
                        adapter: Adapter | None = None, preserve_bot_state=False,
                        forward_recovery=False) -> ActivationRecord:
    """Quiesce an enrolled estate, then install one reviewed sealed candidate.

    First adoption has no sealed recovery floor. An upgrade binds the selected
    active release and its saved configuration as the original owner. Both use
    the same durable pause, migration, publication and serial start sequence.
    """
    if preserve_bot_state and legacy_source:
        raise ActivationRefusal("preserving bot state is supported only for selected release upgrades")
    if type(forward_recovery) is not bool or forward_recovery and legacy_source:
        raise ActivationRefusal("forward recovery is supported only for selected release upgrades")
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
    with locked_activation(root, wait=ACTIVATION_WAIT_S) as store:
        selected = read_selection(root)
        if legacy_source and selected is not None:
            raise ActivationError("first adoption refuses an existing release selection")
        if not legacy_source and selected is None:
            raise ActivationError("upgrade requires an active selected release")
        for prior in (root / "state/activations").glob("*/activation.json"):
            record = read_activation(root, prior.parent.name)
            if record.status not in {"active", "rolled_back"}:
                raise ActivationError(f"existing activation {record.activation_id} requires explicit repair")
            if legacy_source and (record.activation_id == activation_id
                                  or not (_completed_adoption_abort(root, record)
                                          or _cancelled_before_effects(root, record))):
                raise ActivationError(f"existing activation {record.activation_id} requires explicit repair")
        plan.check_fresh()
        source = source_plan = None
        if not legacy_source:
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
        # The migration owner reads live state; SQLite may create empty WAL/SHM
        # bookkeeping sidecars. Refuse standing blockers before any activation record or
        # native pause, then repeat the entire proof after ingest is quiesced.
        preview = build_migration_manifest(root, source, release)
        standing = tuple(blocker for blocker in preview.blockers
                         if blocker not in _LIVE_PREVIEW_CHURN)
        if standing:
            raise ActivationRefusal("migration preview blocks activation before pause: " + "; ".join(standing))
        recovery = release if forward_recovery or source is None else source
        _check_recovery_readability(preview, recovery)
        if legacy_source:
            observed_manager = _catalog(adapter.read("svc_inventory_catalog"))[0]
            declarations = (legacy_linux_declarations(plan) if observed_manager == "Linux" else
                            _legacy_declarations(plan) if observed_manager == "Darwin" else
                            ())
            if not declarations:
                raise ActivationError("no reviewed legacy unit sources were found")
        inventory = collect_enrollment(root, declarations, legacy_source=legacy_source,
                                       adapter=adapter).require_complete()
        if legacy_source and inventory.manager != observed_manager:
            raise ActivationError("legacy native manager changed during enrollment")
        manager, domain, directories, _, _ = _catalog(inventory.catalog)
        install_directory = Path(install_directory)
        if (not install_directory.is_absolute() or install_directory.resolve() != install_directory
                or install_directory not in directories):
            raise ActivationError("install directory is not an observed native search path")
        candidates = planned_units(plan, manager)
        bot_states = _snapshot_bot_states(inventory, candidates, adapter) if preserve_bot_state else None
        starts = {}
        for declaration, item in candidates:
            if item["enroll"] and not is_unit_template(declaration.source.name):
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
                             if disabled.get(target.rsplit("/", 1)[-1]) == "disabled"
                             and (not bot_states or bot_states.get(target, {}).get("disposition") != "stopped"))
            if blocked:
                raise CandidateDisabledOverride(tuple(blocked))
        _preflight_candidate_placement(inventory, candidates, install_directory)
        from .plane.owner_service import preflight as owner_service_preflight, managed_listeners
        owner_service_preflight(starts, managed=managed_listeners(source_plan, inventory))
        retired_units = tuple(unit for unit in inventory.units
                              if unit.installed and unit.target not in candidate_targets
                              and not is_unit_template(unit.target))
        rank, contexts = _roster(plan, candidates, package)
        # Standing handoff refusals (manager coverage, stopped or uninstalled
        # assignees, retired owners, unparsable sections) must not wait for a
        # quiesced fleet. This live read proves only those conditions; the
        # quiesced write rereads its own audit and remains authoritative.
        from .activation_handoffs import preflight_canonical_handoffs
        roster, bot_dirs, candidate_bots = _handoff_inputs(
            inventory.units, source_plan, contexts, package, bot_states=bot_states)
        preflight_canonical_handoffs(root, roster=roster, bot_dirs=bot_dirs,
                                     candidate_bots=candidate_bots)
        _refuse_unrecorded_run_intent(root, inventory.units, contexts, bot_states=bot_states)
        phases = _legacy_phase_membership(plan, inventory, source_plan)
        for unit in inventory.units:
            if unit.installed and unit.declaration.scope == "bot":
                _original_bot_tmpdir(unit)  # Refuse an unknown private server before any record.
        for unit in inventory.units:
            if (unit.installed and not is_unit_template(unit.target)
                    and adapter.call("svc_activation_assert_external",
                                     unit.installed[0].path, unit.target, str(os.getpid())).returncode):
                raise ActivationError("activation caller is hosted or native ownership is unknown")
        store.prepare(activation_id, plan,
                      recovery_release_id=recovery.release_id, forward_recovery=forward_recovery,
                      source_release_id=source.release_id if source else None,
                      enrollment_digest=inventory.digest, legacy_source=legacy_source,
                      install_directory=install_directory, bot_states=bot_states)
        pause = _prepare_no_effect_journals(store, activation_id, plan, inventory, phases, adapter)
        sockets = _quiesce_running(root, store, activation_id, pause, inventory.units, adapter)
        return _finish_running_activation(
            root, store, activation_id, plan, release, source, source_plan,
            inventory.units, candidates, starts, rank, contexts, retired_units, sockets,
            install_directory, adapter, package, legacy_source=legacy_source)


def _finish_running_activation(root, store, activation_id, plan, release, source, source_plan,
                               old_units, candidates, starts, rank, contexts, retired_units, sockets,
                               install_directory, adapter, package, *, legacy_source):
    """Continue from recorded quiescence, reconciling any exact candidate starts."""
    record = read_activation(root, activation_id)
    completed = set(record.body["completed"])
    if "queues_classified" not in completed:
        store.begin(activation_id, "queues_classified")
        migration = build_migration_manifest(root, source, release)
        if migration.blockers:
            raise ActivationRefusal("data or pending queues block activation: " + "; ".join(migration.blockers))
        from .activation_handoffs import persist_canonical_handoffs
        roster, bot_dirs, candidate_bots = _handoff_inputs(
            old_units, source_plan, contexts, package,
            bot_states=record.body["intent"].get("bot_states"))
        refreshed = _handoff_refresh_time(root, activation_id, datetime.now(timezone.utc))
        store.record_handoff_refresh(activation_id, refreshed.strftime("%Y-%m-%dT%H:%M:%SZ"))
        persist_canonical_handoffs(root, roster=roster, bot_dirs=bot_dirs,
                                   expected_audit=migration.task_audit,
                                   candidate_bots=candidate_bots, refreshed_at=refreshed)
        store.complete(activation_id, "queues_classified", evidence_digest=migration.manifest_id[2:])
    else:
        journal = read_migration(root, activation_id)
        if journal is not None:
            payload = dict(journal["manifest"])
            if payload.pop("schema", None) != 1:
                raise ActivationError("saved migration manifest has an unknown schema")
            migration = MigrationManifest(**payload)
        else:
            migration = build_migration_manifest(root, source, release)
        if migration.manifest_id[2:] != record.body["evidence"]["queues_classified"]:
            raise ActivationError("classified queues differ from their recorded migration manifest")
    if "migration_applied" not in completed:
        if "backup_saved" not in completed:
            store.begin(activation_id, "backup_saved")
        apply_migration(store, activation_id, migration)
    if "selection_switched" not in completed:
        store.begin(activation_id, "selection_switched")
        store.select(activation_id)
    if "configuration_applied" not in completed:
        store.begin(activation_id, "configuration_applied")
        applied = config_install.apply_config(root, activation_id)
        store.complete(activation_id, "configuration_applied", evidence_digest=_digest({
            "plan_id": applied.plan_id, "status": applied.status, "progress": applied.progress}))
    return _candidate_startup(root, store, activation_id, plan, release, candidates,
        starts, rank, install_directory, adapter, package, bootstrap=False,
        retired_units=retired_units, sockets=sockets, legacy_source=legacy_source)


def _candidate_startup(root, store, activation_id, plan, release, candidates, starts,
                       rank, install_directory, adapter, package, *, bootstrap,
                       retired_units=(), sockets=None, legacy_source=False):
    """Serial candidate starts with durable per-unit intent and read-only replay."""
    sockets = sockets or {}
    enrollment.prepare_candidate_enrollment(store, activation_id,
        configuration_journal=activation_id, install_directory=install_directory, adapter=adapter)
    record = read_activation(root, activation_id)
    if record.body["completed"] == list(STEPS[:STEPS.index("ingest_started")]) and record.body["pending"] is None:
        store.arm_start_evidence(activation_id)
    observations = {}
    for phase, step in (("ingest", "ingest_started"), ("bots", "bots_started"),
                        ("producers", "producers_resumed")):
        record = read_activation(root, activation_id)
        completed = step in record.body["completed"]
        if phase == "producers":
            _verify_candidate_startup(root, store, activation_id, release, starts,
                                      observations, retired_units, sockets, adapter)
            record = read_activation(root, activation_id)
        if not completed:
            store.begin(activation_id, step)
        record = read_activation(root, activation_id)
        phases = record.body.get("start_phases")
        effects = record.body.get("start_effects")
        if not isinstance(phases, dict) or not isinstance(effects, dict):
            raise ActivationError("candidate start lacks durable phase/effect evidence")
        context = phases.get(phase)
        prior_effects = {source: effect for source, effect in effects.items() if effect["phase"] == phase}
        if context is None:
            if completed or prior_effects:
                raise ActivationError("candidate start preceded its durable phase evidence")
            registry = _registry_ready(plan, candidates, package) if phase == "bots" else []
            if phase == "bots":
                store.record_identity_bindings(activation_id,
                    identity_bindings_from_registry(plan, registry, package=package), package=package)
            publication = enrollment.install_candidate_units(store, activation_id, phase, adapter=adapter)
            store.record_start_phase(activation_id, phase=phase,
                                     publication_digest=publication.digest, registry=registry)
            publication_digest = publication.digest
        else:
            registry = context["registry"]
            publication_digest = context["publication"]
            if phase == "bots" and "identity_bindings" not in record.body:
                raise ActivationError("candidate bots lack recorded identity bindings")
            if not completed:
                publication = enrollment.install_candidate_units(store, activation_id, phase,
                    adapter=adapter, already_started=bool(prior_effects))
                if publication.digest != publication_digest:
                    raise ActivationError("candidate publication evidence changed")
        entries = tuple(entry for entry in enrollment.candidate_entries(store, activation_id, phase)
                        if entry.get("publish", True))
        if phase == "bots":
            entries = sorted(entries, key=lambda entry: rank[(starts[entry["source"]][0].fleet,
                                                              starts[entry["source"]][0].bot)])
        startable, timer_services = _startable_producers(entries) if phase == "producers" else (entries, set())
        if set(prior_effects) - {entry["source"] for entry in startable}:
            raise ActivationError("candidate start receipt names a foreign unit")
        results = []
        for entry in startable:
            source = entry["source"]
            _, _, unit = starts[source]
            if source not in prior_effects:
                from .plane.owner_service import preflight as owner_service_preflight
                owner_service_preflight({source: starts[source]})
            fence = prior_effects[source]["fence"] if source in prior_effects else None
            readiness = (lambda: _ingest_ready(root, release)) if phase == "ingest" else None
            if source in prior_effects:
                effect = prior_effects[source]
                if effect["target"] != entry["target"] or effect["sha256"] != entry["after"]["sha256"]:
                    raise ActivationError("candidate start receipt differs from frozen publication")
                observed = observe_started_unit(store, installed_file=Path(entry["installed"]),
                    target=entry["target"], unit=unit, sha256=entry["after"]["sha256"],
                    adapter=adapter, fence=fence, readiness=readiness)
                saved = effect["result"]
                if saved is None:
                    if _oneshot_producer(phase, entry["installed"]):
                        raise ActivationError("one-shot producer start has unknown effect; inspect its native/journal witness")
                    store.record_start_result(activation_id, source=source, target=entry["target"],
                                              details=observed.details, digest=observed.digest)
                    result = observed
                else:
                    result = RuntimeEvidence("started", entry["target"], saved["details"])
                    if (result.digest != saved["digest"]
                            or result.details.get("readiness") != observed.details.get("readiness")):
                        raise ActivationError("candidate start result digest changed")
            else:
                if completed:
                    raise ActivationError("completed candidate phase lacks a start receipt")
                result = start_unit(store, activation_id, installed_file=Path(entry["installed"]),
                    target=entry["target"], unit=unit, sha256=entry["after"]["sha256"],
                    adapter=adapter, readiness=readiness,
                    before_start=lambda saved_fence, entry=entry, source=source:
                        store.record_start_intent(activation_id, phase=phase, source=source,
                            target=entry["target"], sha256=entry["after"]["sha256"], fence=saved_fence))
                store.record_start_result(activation_id, source=source, target=entry["target"],
                                          details=result.details, digest=result.digest)
            observations[source] = result
            results.append(result.digest)
        enabled = enrollment.verify_candidate_enablement(store, activation_id, phase, adapter=adapter)
        phase_digest = _digest([publication_digest, results, enabled, registry]) if phase != "producers" else _digest({
            "publication": publication_digest, "native_starts": results,
            "enablement": enabled, "timer_services": sorted(timer_services),
            "watchdog_ticks": "admitted only after active", **(
                {"user_manager_startup": "host prerequisite"} if bootstrap else {"legacy_source": legacy_source})})
        if completed:
            if record.body["evidence"][step] != phase_digest:
                raise ActivationError("completed candidate phase evidence changed")
        elif phase == "producers":
            return store.complete(activation_id, step, evidence_digest=phase_digest)
        else:
            store.complete(activation_id, step, evidence_digest=phase_digest)
    raise ActivationError("candidate startup did not reach producer completion")


def _verify_candidate_startup(root, store, activation_id, release, starts,
                              observations, retired_units, sockets, adapter):
    record = read_activation(root, activation_id)
    if "bots_started" not in record.body["completed"]:
        raise ActivationError("candidate bots have not completed startup")
    verified = {"ingest": _ingest_ready(root, release), "bots": []}
    stopped = record.body["intent"].get("bot_states", {})
    placements = {entry["target"]: entry for entry in enrollment.candidate_entries(store, activation_id, "bots")}
    for target, saved in stopped.items():
        if saved["disposition"] != "stopped":
            continue
        entry = placements[target]
        if entry.get("publish", True) or os.path.lexists(entry["installed"]):
            raise ActivationError("preserved stopped bot was enrolled before producer admission")
        enrollment._check_targets(adapter, units.load_unit_pause(store, activation_id).enrollment,
                                  (entry,), allow_candidate=False)
        native = adapter.read("svc_inventory_state", entry["installed"], target).strip()
        expected = "not-found not-found inactive" if Path(entry["installed"]).suffix == ".service" else "unchanged unloaded inactive"
        if native != expected:
            raise ActivationError("preserved stopped bot remains natively enrolled or masked")
        # The native quiet reader supports absent files; the socket witness is
        # independently reprobed through its original frozen selected definition.
        originals = units.load_unit_pause(store, activation_id).enrollment["units"]
        original = next(_frozen_unit(row) for row in originals if row["target"] == target)
        path, present = _legacy_bot_socket(original, require_for_active=False, allow_uninstalled=True)
        if present or str(path) != saved["socket_path"]:
            raise ActivationError("preserved stopped bot has a live or changed private server")
    for original in retired_units:
        assert_quiescent(adapter, installed_file=Path(original.installed[0].path),
                         target=original.target, socket_path=sockets.get(original.target))
    for source, result in observations.items():
        declaration, _, unit = starts[source]
        if unit.phase == "bots":
            fence = result.details["readiness"]["fence"]
            kind = result.details["readiness"]["kind"]
            if adapter.read("svc_activation_bot_ready", root,
                    declaration.working_directory, "0", fence).strip() != kind:
                raise ActivationError("candidate bot lost readiness before producer admission")
            verified["bots"].append(result.digest)
    digest = _digest(verified)
    if "verified" in record.body["completed"]:
        if record.body["evidence"]["verified"] != digest:
            raise ActivationError("candidate verification evidence changed")
    else:
        store.begin(activation_id, "verified")
        store.complete(activation_id, "verified", evidence_digest=digest)


def _published_tmux_dir(installed: Path, content: bytes) -> str:
    """The private tmux directory named by the frozen published unit bytes."""
    if installed.suffix == ".plist":
        environment = _darwin_source(content)["environment"]
    else:
        try:
            lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ActivationRefusal("published bot unit is not UTF-8") from exc
        environment = _environment(" ".join(line.strip().split("=", 1)[1] for line in lines
                                            if line.strip().startswith("Environment=")))
    tmpdir = environment.get("TMUX_TMPDIR")
    if not isinstance(tmpdir, str) or not Path(tmpdir).is_absolute():
        raise ActivationRefusal("published bot unit has no absolute private tmux directory")
    return tmpdir


def _guarded_repair(what: str, body, root, reason, adapter, *args):
    """Check one repair's operator inputs, then run its ``body``.

    ``body(root, package, adapter, reason, writing, *args)`` appends to
    ``writing`` just before its single journal write. Every failure before that
    is an ``ActivationRefusal`` (nothing changed); a failure after it keeps its
    own type, because the atomic replace may already have landed.
    """
    writing = []
    try:
        root = Path(root).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ActivationRefusal(f"{what} requires an explicit existing absolute data root")
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 500
                or not reason.isprintable()):
            raise ActivationRefusal(f"{what} requires a short reason on one printable line")
        package = get_resources()
        return body(root.resolve(), package, adapter if adapter is not None else Adapter(package),
                    reason.strip(), writing, *args)
    except ActivationRefusal:
        raise
    except (ActivationError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        if writing:
            raise
        if lock_busy(str(exc)):
            raise ActivationRefusal(str(exc)) from exc
        raise ActivationRefusal(f"{what} could not verify the recorded activation "
                                f"({type(exc).__name__})") from exc


def _selected_candidate(root, activation_id, intent):
    """The activation's frozen plan and sealed release, which must be the host's selection."""
    plan = read_plan(root, intent["plan_id"])
    release = read_release(root, intent["release_id"])
    if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
        raise ActivationRefusal("activation plan differs from its sealed release")
    if read_selection(root) != {"schema": 1, "activation_id": activation_id,
                                "release_id": release.release_id, "plan_id": plan.plan_id}:
        raise ActivationRefusal("host selection is not this activation's candidate")
    return plan


def _intent_matches(effect, entry, item) -> bool:
    """A recorded start is exactly its frozen publication, with its phase's fence shape."""
    fenced = isinstance(effect.get("fence"), dict) if effect["phase"] == "bots" else effect.get("fence") is None
    return (fenced and effect["phase"] == item["phase"] == entry["phase"]
            and effect["target"] == entry["target"]
            and effect["sha256"] == entry["after"]["sha256"] == item["sha256"])


def repair_failed_bot_start(root: Path, activation_id: str, *, fleet: str, bot: str,
                            reason: str, adapter: Adapter | None = None) -> ActivationRecord:
    """Archive one verified-dead unresolved candidate bot start; never start anything.

    May run from a newer CLI than the target's sealed release: this executable
    only reads the target's frozen plan/release and observes the named unit
    through its own adapter. It does not alter the seal, readiness or any other
    receipt. The sealed candidate's explicit ``--resume`` performs the fresh start.

    Every failure before the single journal write is an ``ActivationRefusal``
    (nothing changed); a failure after entering the write keeps its own type,
    because the atomic replace may already have landed.
    """
    return _guarded_repair("start repair", _repair_failed_bot_start, root, reason, adapter,
                           activation_id, fleet, bot)


def _repair_failed_bot_start(root, package, adapter, reason, writing, activation_id, fleet, bot):
    with locked_activation(root) as store:
        record = read_activation(root, activation_id)
        if record.body["pending"] != "bots_started" or resumable_running_step(record) != "bots_started":
            raise ActivationRefusal("start repair requires a pending bots_started activation")
        plan = _selected_candidate(root, activation_id, record.body["intent"])
        manager = _catalog(units.load_unit_pause(store, activation_id).enrollment["catalog"])[0]
        matches = [(declaration, item) for declaration, item in planned_units(plan, manager)
                   if item["enroll"] and item["phase"] == "bots"
                   and declaration.fleet == fleet and declaration.bot == bot]
        if len(matches) != 1:
            raise ActivationRefusal("named fleet/bot is not exactly one candidate bot unit")
        declaration, item = matches[0]
        source = str(declaration.source)
        placements = enrollment.candidate_placements(store, activation_id)[1]
        entries = [entry for entry in placements["bots"] if entry["source"] == source]
        effect = record.body["start_effects"].get(source)
        if len(entries) != 1 or effect is None:
            raise ActivationRefusal("named bot has no recorded candidate start intent")
        if effect["result"] is not None:
            raise ActivationRefusal("named bot has a durable start result; nothing to repair")
        entry = entries[0]
        if not _intent_matches(effect, entry, item):
            raise ActivationRefusal("recorded start intent differs from frozen publication")
        _assert_external_caller(adapter, placements)
        dead = _verified_dead_start(adapter, declaration, entry, effect, "named bot")
        writing.append(True)
        return store.archive_failed_bot_start(
            activation_id, source=source, target=entry["target"], sha256=effect["sha256"],
            fence=effect["fence"], evidence={"details": dead.details, "digest": dead.digest},
            reason=reason, repair_artifact=package.artifact_id)


def _assert_external_caller(adapter, placements: dict) -> None:
    """Native ancestry over this activation's recorded placements, as activation
    itself checks; ordinary inventory cannot classify a partial start."""
    for entries in placements.values():
        for placement in entries:
            if is_unit_template(placement["target"]):
                continue
            if adapter.call("svc_activation_assert_external", placement["installed"],
                            placement["target"], str(os.getpid())).returncode:
                raise ActivationRefusal("repair caller is hosted or cannot be proved external; "
                                        "use an operator shell")


def _verified_dead_start(adapter, declaration, entry, effect, who: str) -> RuntimeEvidence:
    """Prove one recorded candidate start's unit inactive, with matching frozen bytes.

    A bot's private tmux server must also be absent or refuse connections.
    ``who`` names the start in each refusal.
    """
    installed = Path(entry["installed"])
    try:
        content = installed.read_bytes()
        source_bytes = declaration.source.read_bytes()
    except OSError as exc:
        raise ActivationRefusal(f"{who}: source or installed unit is unreadable") from exc
    if (hashlib.sha256(content).hexdigest() != effect["sha256"]
            or hashlib.sha256(source_bytes).hexdigest() != effect["sha256"]):
        raise ActivationRefusal(f"{who}: source or installed unit differs from the frozen start")
    bot = effect["phase"] == "bots"
    socket_path = (Path(_published_tmux_dir(installed, content)) / f"tmux-{os.getuid()}"
                   / declaration.source.stem) if bot else None
    try:
        dead = assert_quiescent(adapter, installed_file=installed, target=entry["target"],
                                socket_path=socket_path)
    except ActivationError as exc:
        if "socket" in getattr(exc, "detail", ""):
            raise ActivationRefusal(f"{who}'s private tmux server still accepts or cannot be "
                                    "observed; inspect that server before repair") from exc
        raise ActivationRefusal(f"{who}'s unit is not verified inactive; if it is active "
                                "(exited), failed or restarting, explicitly stop that exact verified "
                                "unit (and reset-failed it), then retry") from exc
    if bot and dead.details["socket_state"] not in {"absent", "refused"}:
        raise ActivationRefusal(f"{who}'s private tmux server is not verified absent")
    return dead


def _host_boot() -> dict | None:
    """This boot's identity, recorded beside a host-reset repair for the audit trail.

    Linux reads the kernel's boot id and boot time; macOS asks ``sysctl`` for
    the boot session UUID only. Anything unreadable records ``None``.
    """
    try:
        if sys.platform == "darwin":
            # Only the UUID: one parser of kern.boottime already exists (lib-common.sh).
            boot_id = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True,
                                     text=True, timeout=5, check=True).stdout.strip()
            return {"boot_id": boot_id, "booted_at": None} if boot_id else None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
    except (OSError, subprocess.SubprocessError, StopIteration, ValueError, IndexError):
        return None
    return {"boot_id": boot_id, "booted_at": datetime.fromtimestamp(btime, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")}


def _plane_state(root: Path) -> str:
    """The plane socket's handshake state; only "down" (no socket, or refused) is dead.

    A listener that is slow, unreachable or answers garbage is not proof the
    daemon is gone. Tests replace this native observation seam.
    """
    from .plane.daemon import probe_daemon_state, socket_path
    return probe_daemon_state(socket_path(root))


@dataclass(frozen=True)
class HostResetRepair:
    """The repaired record and the targets this one write archived."""
    record: ActivationRecord
    archived_targets: tuple[str, ...]


def repair_starts_after_host_reset(root: Path, activation_id: str, *, reason: str,
                                   adapter: Adapter | None = None) -> HostResetRepair:
    """Archive every verified-dead candidate start after a host reset; never start anything.

    A host that resets while a start step is next (``ingest_started``,
    ``bots_started``, ``verified`` or ``producers_resumed``) loses every
    started candidate at once, and the resume only re-observes recorded
    starts, so it cannot continue. This proves each recorded start dead (its
    unit inactive, a bot's private tmux server gone, the plane not answering)
    and every published candidate quiet, as the resume's republish will
    require, then archives the starts and reopens ``ingest_started`` in one
    write. The sealed candidate's ``--resume`` then starts every candidate
    fresh. Like ``repair-start`` it may run from a newer CLI: it only reads the
    frozen plan/release and observes units through its own adapter.

    A one-shot producer service started with no recorded result is refused,
    as the resume refuses it: its effect is unknown, so it needs inspection.

    Every failure before the single journal write is an ``ActivationRefusal``
    (nothing changed); a failure after entering the write keeps its own type,
    because the atomic replace may already have landed.
    """
    return _guarded_repair("host-reset repair", _repair_starts_after_host_reset, root, reason, adapter,
                           activation_id)


def _repair_starts_after_host_reset(root, package, adapter, reason, writing, activation_id):
    with locked_activation(root) as store:
        record = read_activation(root, activation_id)
        if resumable_running_step(record) not in START_STEPS:
            raise ActivationRefusal("host-reset repair requires an activation stopped at one of "
                                    + ", ".join(START_STEPS))
        plan = _selected_candidate(root, activation_id, record.body["intent"])
        effects = record.body["start_effects"]
        if not effects:
            raise ActivationRefusal("no candidate start is recorded; resume directly")
        manager = _catalog(units.load_unit_pause(store, activation_id).enrollment["catalog"])[0]
        planned = {str(declaration.source): (declaration, item)
                   for declaration, item in planned_units(plan, manager) if item["enroll"]}
        frozen, placements = enrollment.candidate_placements(store, activation_id)
        entries = {entry["source"]: entry for phase in placements.values() for entry in phase}
        for source, effect in sorted(effects.items()):
            if source not in planned or source not in entries:
                raise ActivationRefusal(f"recorded start {source} is not a frozen candidate unit")
            if not _intent_matches(effect, entries[source], planned[source][1]):
                raise ActivationRefusal(f"recorded start of {entries[source]['target']} differs "
                                        "from frozen publication")
            if effect["result"] is None and _oneshot_producer(effect["phase"], entries[source]["installed"]):
                raise ActivationRefusal(f"one-shot producer start of {effect['target']} has an unknown "
                                        "effect; inspect its native/journal witness before repair")
        _assert_external_caller(adapter, placements)
        # Every unit the resume will republish must be quiet, recorded start or
        # not: a published candidate that systemd started at boot loops on
        # admission refusals, and the resume would refuse it after this write.
        unquiet = enrollment.unquiet_candidates(adapter, frozen, [
            entry for phase, phase_entries in placements.items()
            if phase in record.body["start_phases"] for entry in phase_entries])
        if unquiet:
            raise ActivationRefusal("candidate units are not quiet; stop and reset each one (a "
                                    "collision needs inspection), then retry: " + "; ".join(unquiet))
        evidence = {}
        for source, effect in sorted(effects.items()):
            entry = entries[source]
            dead = _verified_dead_start(adapter, planned[source][0], entry, effect, entry["target"])
            evidence[source] = {"details": dead.details, "digest": dead.digest}
        try:
            plane = _plane_state(root)
        except Exception as exc:
            raise ActivationRefusal("the plane daemon's socket cannot be observed; inspect it "
                                    "before repair") from exc
        if plane != "down":
            raise ActivationRefusal(f"the plane socket is {plane}, not down; stop whatever listens "
                                    "on it, or this is not a host reset")
        writing.append(True)
        repaired = store.archive_starts_after_host_reset(
            activation_id, attempts=effects, evidence=evidence, reason=reason,
            repair_artifact=package.artifact_id, host_boot=_host_boot())
        return HostResetRepair(repaired, tuple(sorted(effect["target"] for effect in effects.values())))


def _frozen_unit(row: dict) -> EnrolledUnit:
    """Rehydrate only the journal owner's verified original unit observation."""
    declaration = row["declaration"]
    def snapshot(value):
        return FileSnapshot(value["path"], value["resolved"], value["mode"], value["link"],
                            base64.b64decode(value["content"]["base64"], validate=True), value["sha256"])
    return EnrolledUnit(
        UnitDeclaration(Path(declaration["source"]), declaration["scope"],
                        Path(declaration["working_directory"]), declaration["release_id"],
                        tuple(tuple(pair) for pair in declaration["environment"]),
                        declaration["fleet"], declaration["bot"], declaration["service"]),
        row["target"], snapshot(row["generated"]),
        tuple(snapshot(value) for value in row["installed"]),
        tuple(tuple(pair) for pair in row["properties"]),
    )


def resume_activation(root: Path, activation_id: str, plan_id: str,
                      install_directory: Path, *, adapter: Adapter | None = None,
                      preserve_bot_state=False, forward_recovery=None) -> ActivationRecord:
    """Fix forward one recorded activation, without replaying uncertain sends."""
    root = Path(root).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ActivationError("resume requires an explicit existing absolute data root")
    root = root.resolve()
    plan = read_plan(root, plan_id)
    release = read_release(root, plan.release_id)
    package, identity = get_resources(), RuntimeIdentity.current()
    if (release.seal_sha256 != plan.release_seal
            or identity != RuntimeIdentity(release.cli_path, release.native_path, release.inputs.artifact_id)
            or package.native != release.native_path or package.artifact_id != release.inputs.artifact_id
            or Path(sys.executable).absolute() != release.directory / release.paths.interpreter):
        raise ActivationError("run resume using the exact sealed candidate interpreter and package")
    adapter = adapter if adapter is not None else Adapter(package)
    if adapter.package.native != release.native_path:
        raise ActivationError("resume adapter differs from the candidate release")
    with locked_activation(root, wait=ACTIVATION_WAIT_S) as store:
        record = read_activation(root, activation_id)
        if (record.body["intent"]["plan_id"] != plan_id
                or record.body["intent"]["release_id"] != release.release_id):
            raise ActivationError("resume ID belongs to a different frozen plan or release")
        if preserve_bot_state and record.body["intent"].get("preserve_bot_state") is not True:
            raise ActivationRefusal("resume cannot change the recorded bot-state policy")
        recovery = _recorded_recovery(root, record, release, forward_recovery=forward_recovery)
        step = resumable_running_step(record)
        if step is None:
            raise ActivationError("pending activation stage lacks a durable handoff or candidate-start witness; "
                                  "inspect the recorded step and repair forward with its native/journal owner")
        for prior in (root / "state/activations").glob("*/activation.json"):
            if prior.parent.name != activation_id and read_activation(root, prior.parent.name).status not in {
                    "active", "rolled_back"}:
                raise ActivationError("another unfinished activation owns this host")
        pause = units.load_unit_pause(store, activation_id)
        frozen = pause.enrollment
        bootstrap = frozen.get("bootstrap_empty") is True
        if (frozen["data_root"] != str(root)
                or frozen.get("legacy_source") is not
                (record.body["intent"].get("source_kind") == "legacy-unsealed")
                or bootstrap and (frozen["units"] or record.body["previous_selection"] is not None)):
            raise ActivationError("resume enrollment differs from frozen activation source")
        manager, domain, directories, _, _ = _catalog(frozen["catalog"])
        if frozen["manager"] != manager:
            raise ActivationError("frozen native manager differs from its catalog")
        install_directory = Path(install_directory)
        if (not install_directory.is_absolute() or install_directory.resolve() != install_directory
                or install_directory not in directories
                or record.body["intent"].get("install_directory") != str(install_directory)):
            raise ActivationError("resume install directory differs from frozen activation intent")
        selected = read_selection(root)
        candidate_selection = {"schema": 1, "activation_id": activation_id,
                               "release_id": release.release_id, "plan_id": plan_id}
        expected = candidate_selection if "selection_switched" in record.body["completed"] else record.body["previous_selection"]
        if selected != expected:
            # A crash during the selector may have written its atomic file.
            if step != "selection_switched" or selected != candidate_selection:
                raise ActivationError("current selection differs from this activation's frozen intent")
        source_id = record.body["intent"]["source_release_id"]
        legacy_source = record.body["intent"].get("source_kind") == "legacy-unsealed"
        if (legacy_source != (source_id is None)
                or bootstrap and source_id != release.release_id):
            raise ActivationError("resume source identity differs from frozen enrollment")
        source = read_release(root, source_id) if source_id and not bootstrap else None
        if source_id and not bootstrap and record.body["previous_selection"] is None:
            raise ActivationError("resume source selection is missing")
        source_plan = (read_plan(root, record.body["previous_selection"]["plan_id"])
                       if source_id and not bootstrap else None)
        if source_plan is not None and (source_plan.release_id != source.release_id
                                        or source_plan.release_seal != source.seal_sha256):
            raise ActivationError("resume source plan differs from its selected release")
        if not bootstrap:
            journal = read_migration(root, activation_id)
            if journal is not None:
                payload = dict(journal["manifest"])
                if payload.pop("schema", None) != 1:
                    raise ActivationError("saved migration manifest has an unknown schema")
                preview = MigrationManifest(**payload)
            else:
                preview = build_migration_manifest(root, source, release)
            _check_recovery_readability(preview, recovery)
        candidates = planned_units(plan, manager)
        starts = {}
        candidate_targets = {}
        for declaration, item in candidates:
            if item["enroll"]:
                source_path = str(declaration.source)
                if not is_unit_template(declaration.source.name):
                    starts[source_path] = (declaration, item,
                        validate_unit_admission(release, declaration, item, plan.blob(item["sha256"])))
                candidate_targets[source_path] = enrollment._target(manager, domain, declaration.source)
        candidate_started = set()
        if step in START_STEPS:
            for source_path, effect in record.body["start_effects"].items():
                candidate = starts.get(source_path)
                if (candidate is None or effect.get("phase") != candidate[1]["phase"]
                        or effect.get("target") != candidate_targets[source_path]
                        or effect.get("sha256") != candidate[1]["sha256"]):
                    raise ActivationError("candidate start intent differs from frozen plan target")
                candidate_started.add(effect["target"])
        old_units = tuple(_frozen_unit(row) for row in frozen["units"])
        if not bootstrap and step in _RUNNING_QUIESCE_STEPS:
            _quiesce_running(root, store, activation_id, pause, old_units, adapter)
        sockets = {}
        for unit in old_units:
            if unit.installed and unit.declaration.scope == "bot":
                socket_path, present = _legacy_bot_socket(unit, require_for_active=False)
                if present and unit.target not in candidate_started:
                    raise ActivationError("old private bot server is present after recorded quiescence")
                sockets[unit.target] = socket_path
        if bootstrap:
            for next_step, phase in (("producers_paused", "producers"),
                                     ("sessions_handed_off", None),
                                     ("sessions_quiesced", "bots"),
                                     ("ingest_quiesced", "ingest")):
                if next_step in record.body["completed"]:
                    continue
                store.begin(activation_id, next_step)
                _bootstrap_plane(root, release)
                evidence = (units.pause_phase(store, activation_id, phase, adapter=adapter).digest if phase else
                            _digest({"bootstrap_empty": True, "sessions": [], "handoffs": []}))
                store.complete(activation_id, next_step, evidence_digest=evidence)
        else:
            for phase in units.PHASES:
                _legacy_quiet(adapter, pause, phase, sockets, candidate_started=candidate_started)
        if not bootstrap and STEPS.index(step) < STEPS.index("ingest_started") and _probe(root) is not None:
            raise ActivationError("old ingest still answers; resume requires a quiesced writer")
        rank, contexts = _roster(plan, candidates, package)
        targets = set(candidate_targets.values())
        retired = tuple(unit for unit in old_units if unit.installed and unit.target not in targets
                        and not is_unit_template(unit.target))
        if bootstrap:
            return _finish_bootstrap_activation(root, store, activation_id, plan, release,
                candidates, starts, rank, install_directory, adapter, package)
        return _finish_running_activation(
            root, store, activation_id, plan, release, source, source_plan,
            old_units, candidates, starts, rank, contexts, retired, sockets,
            install_directory, adapter, package, legacy_source=legacy_source)


def adopt_existing_activation(root: Path, activation_id: str, plan_id: str,
                              install_directory: Path, *, adapter: Adapter | None = None) -> ActivationRecord:
    """First activation of an already running, unsealed native estate."""
    return _running_activation(root, activation_id, plan_id, install_directory,
                               legacy_source=True, adapter=adapter)


def upgrade_activation(root: Path, activation_id: str, plan_id: str,
                       install_directory: Path, *, adapter: Adapter | None = None,
                       preserve_bot_state=False, forward_recovery=False) -> ActivationRecord:
    """Upgrade an active selected release with that seal as the recovery floor."""
    return _running_activation(root, activation_id, plan_id, install_directory,
                               legacy_source=False, adapter=adapter, preserve_bot_state=preserve_bot_state,
                               forward_recovery=forward_recovery)
