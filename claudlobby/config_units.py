"""Bind staged native units to the configuration that actually rendered them.

The plist is the compositor's structured output for a unit family; its identity
and environment supply the declaration, not a second fleet-config parser.
Both platform variants are hashed. Enrollment still independently checks the
installed/effective definition before any activation effect.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib

from .config_plan import ConfigPlan, PlanError
from .activation_state import ActivationError
from .supervision_inventory import UnitDeclaration, is_unit_template
from .runtime_admission import parse_unit_argv, unit_systemd_environment


def unit_family(files: dict[str, tuple[bytes, int]], *, destination: Path,
                scope: str, phase: str, release_id: str, fleet: str | None = None,
                bot: str | None = None, enroll: bool = True) -> list[dict]:
    """Freeze one already rendered service, optionally with a Linux timer."""
    plists = [name for name in files if name.endswith(".plist")]
    if len(plists) != 1 or phase not in {"producers", "bots", "ingest"}:
        raise PlanError("unit family needs one structured definition and an explicit phase")
    plist = plists[0]
    stem = plist.removesuffix(".plist")
    if set(files) not in ({plist, stem + ".service"},
                          {plist, stem + ".service", stem + ".timer"}):
        raise PlanError("partial or unexpected staged unit family")
    try:
        definition = plistlib.loads(files[plist][0])
        environment = definition["EnvironmentVariables"]
        working_directory = definition["WorkingDirectory"]
        admission = parse_unit_argv(definition["ProgramArguments"])
        if admission.unit != stem or admission.phase != phase or admission.release_id != release_id:
            raise ValueError("unbound unit admission")
        if (definition["Label"] != stem or not Path(working_directory).is_absolute()
                or not isinstance(environment, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())
                or environment.get("CLAUDLOBBY_RELEASE_ID") != release_id):
            raise ValueError("unbound native identity")
    except (ValueError, TypeError, KeyError, ActivationError, plistlib.InvalidFileException) as exc:
        raise PlanError("staged unit is not bound to its candidate release") from exc
    # Native identity is public location metadata. Other environment values may
    # be credentials; they stay in the private staged bytes, never in summaries.
    # The one composed opt-in switch the scheduled host updater must compare
    # with its unit (host_update_operations) is carried too, including an empty
    # disarming assignment; an absent switch stays absent.
    identity = {key: environment[key] for key in (
        "CLAUDLOBBY_ROOT", "FLEET_ROOT", "CLAUDLOBBY_NATIVE_DIR",
        "CLAUDLOBBY_LIBRARY_DIR", "CLAUDLOBBY_CLI", "CLAUDLOBBY_ARTIFACT_ID",
        "CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED") if key in environment}
    return [{"source": str(destination / name), "scope": scope, "phase": phase,
             "release_id": release_id, "working_directory": working_directory,
             "environment": identity, "fleet": fleet, "bot": bot, "enroll": enroll,
             "admission": {"kind": "unit-start-v1", "unit": admission.unit,
                           "argv": list(admission.argv)},
             "service": stem + ".service" if name.endswith(".timer") else None,
             "platform": "Darwin" if name.endswith(".plist") else "Linux",
             "sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
            for name, (content, mode) in sorted(files.items())]


def template_units(templates: dict[str, tuple[bytes, int]], families: list[dict], *,
                   destination: Path, scope: str, release_id: str,
                   fleet: str | None) -> list[dict]:
    """Freeze the fleet's Linux failure template beside the job units naming it (#2522).

    A template has no plist and no admission: systemd starts its instances
    when a unit names it in OnFailure=, and activation only publishes and
    parks its file. It claims the identity every family beside it declares,
    which its own reviewed bytes must spell line for line.
    """
    identities = {json.dumps([item["working_directory"], item["environment"]], sort_keys=True)
                  for item in families}
    if scope != "fleet" or not fleet or len(identities) != 1:
        raise PlanError("a unit template needs one fleet identity from the job units naming it")
    working_directory, environment = families[0]["working_directory"], families[0]["environment"]
    result = []
    for name, (content, mode) in sorted(templates.items()):
        try:
            lines = set(content.decode("utf-8").splitlines())
        except UnicodeDecodeError as exc:
            raise PlanError("unit template is not UTF-8") from exc
        if (f"WorkingDirectory={working_directory}" not in lines
                or any(unit_systemd_environment(key, value) not in lines
                       for key, value in environment.items())):
            raise PlanError("unit template differs from its fleet identity")
        result.append({"source": str(destination / name), "scope": scope, "phase": "producers",
                       "release_id": release_id, "working_directory": working_directory,
                       "environment": dict(environment), "fleet": fleet, "bot": None,
                       "enroll": True, "admission": None, "service": None, "platform": "Linux",
                       "sha256": hashlib.sha256(content).hexdigest(), "mode": mode})
    return result


def job_units(files: dict[str, tuple[bytes, int]], *, destination: Path, scope: str,
              release_id: str, fleet: str | None = None,
              resident_phases: dict[str, str] | None = None) -> list[dict]:
    """Read complete compositor output, including its existing DORMANT owner.

    Resident phases explicitly name declared host jobs; no installed unit
    is classified by guessing from a filename. Timers are always producers.
    A systemd template (``prefix@.service``) is the one file without a plist.
    """
    resident_phases = resident_phases or {}
    dormant = {line.strip() for line in files.get("DORMANT", (b"", 0))[0].decode().splitlines()
               if line.strip() and not line.lstrip().startswith("#")}
    native = {name: item for name, item in files.items()
              if name.endswith((".plist", ".service", ".timer"))}
    templates = {name: native.pop(name) for name in sorted(native) if is_unit_template(name)}
    result = []
    for plist in sorted(name for name in native if name.endswith(".plist")):
        stem = plist.removesuffix(".plist")
        family = {name: native.pop(name) for name in
                  (plist, stem + ".service", stem + ".timer") if name in native}
        timed = stem + ".timer" in family
        result.extend(unit_family(family, destination=destination, scope=scope,
                                  phase="producers" if timed else resident_phases.get(stem, "producers"),
                                  release_id=release_id, fleet=fleet, enroll=stem not in dormant))
    if native:
        raise PlanError("staged service/timer has no complete structured unit family")
    if templates:
        result.extend(template_units(templates, result, destination=destination, scope=scope,
                                     release_id=release_id, fleet=fleet))
    return result


def planned_units(plan: ConfigPlan, platform: str) -> tuple[tuple[UnitDeclaration, dict], ...]:
    """Load exact proposed units; do not substitute them for running evidence.

    The activation installer consumes staged bytes. To inventory the current
    estate, pass the saved *current* plan and let collect_enrollment verify its
    actual generated files against the hashes here before considering them owned.
    """
    if platform not in {"Linux", "Darwin"}:
        raise PlanError("unsupported supervision platform")
    manifest = plan.effects.get("units")
    if not isinstance(manifest, list) or not manifest:
        raise PlanError("missing unit manifest is not deletion authority")
    changes = {change.target: change.after for change in plan.changes}
    result, seen = [], set()
    for item in manifest:
        if item["source"] in seen or item["release_id"] != plan.release_id:
            raise PlanError("ambiguous unit manifest identity")
        seen.add(item["source"])
        source = Path(item["source"])
        after = changes.get(str(source))
        if after is None:
            parent = changes.get(str(source.parent), {})
            after = parent.get("files", {}).get(source.name) if parent.get("kind") == "tree" else None
        if (after is None or after.get("sha256") != item["sha256"]
                or after.get("mode") != item["mode"]):
            raise PlanError("unit manifest differs from frozen configuration bytes")
        plan.blob(item["sha256"])
        if item["platform"] != platform:
            continue
        result.append((UnitDeclaration(source, item["scope"], Path(item["working_directory"]),
                                       item["release_id"], tuple(sorted(item["environment"].items())),
                                       item["fleet"], item["bot"], item["service"]), item))
    if not result:
        raise PlanError("unit manifest has no declarations for this platform")
    return tuple(result)


def current_declarations(plan: ConfigPlan, platform: str) -> tuple[UnitDeclaration, ...]:
    """Reject torn/stale current generated bytes before enrollment observation."""
    result = []
    for declaration, item in planned_units(plan, platform):
        source = declaration.source
        if (not source.is_file() or source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]
                or source.stat().st_mode & 0o777 != item["mode"]):
            raise PlanError(f"current generated unit differs from its saved plan: {source}")
        result.append(declaration)
    return tuple(result)
