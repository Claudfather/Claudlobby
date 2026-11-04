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
import re
import shlex

from .config_plan import ConfigPlan, PlanError
from .activation_state import ActivationError
from .supervision_inventory import UnitDeclaration, is_unit_template
from .runtime_admission import parse_unit_argv, unit_systemd_command, unit_systemd_environment


def _systemd_fields(content: bytes) -> dict:
    section, fields = None, {}
    for raw in content.decode().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError("unsupported generated native field")
        fields.setdefault((section, key), []).append(value)
    return fields


def _systemd_environment(fields: dict) -> dict:
    result = {}
    for value in fields.get(("Service", "Environment"), []):
        assignments = shlex.split(value)
        if len(assignments) != 1:
            raise ValueError("ambiguous generated native environment")
        key, separator, text = assignments[0].partition("=")
        text = text.replace("%%", "%")
        if (not separator or key in result
                or unit_systemd_environment(key, text) != "Environment=" + value):
            raise ValueError("unsupported generated native environment")
        result[key] = text
    return result


def _git_family(files, definition, *, destination, scope, phase, fleet, bot, enroll):
    from .installation import parse_git_unit_argv
    admission = parse_git_unit_argv(definition["ProgramArguments"])
    stem, directory = definition["Label"], definition["WorkingDirectory"]
    if (admission.unit != stem or admission.phase != phase
            or not Path(directory).is_absolute()):
        raise ValueError("unbound Git native family")
    service_fields = _systemd_fields(files[stem + ".service"][0])
    if (service_fields.get(("Service", "ExecStart")) != [unit_systemd_command(admission.argv)]
            or service_fields.get(("Service", "WorkingDirectory")) != [directory]):
        raise ValueError("systemd service bypasses its Git entry")
    linux_environment = _systemd_environment(service_fields)
    result = []
    for name, (content, mode) in sorted(files.items()):
        timer = name.endswith(".timer")
        if timer:
            fields = _systemd_fields(content)
            if fields.get(("Timer", "Unit"), [stem + ".service"]) != [stem + ".service"]:
                raise ValueError("timer names another native service")
        result.append({"source": str(destination / name), "scope": scope, "phase": phase,
                       "working_directory": directory,
                       "environment": definition["EnvironmentVariables"] if name.endswith(".plist") else linux_environment,
                       "fleet": fleet, "bot": bot, "enroll": enroll,
                       "admission": None if timer else {"kind": "git-native-v1", "unit": admission.unit,
                           "mode": admission.mode, "argv": list(admission.argv), "command": list(admission.command)},
                       "service": stem + ".service" if timer else None,
                       "platform": "Darwin" if name.endswith(".plist") else "Linux",
                       "sha256": hashlib.sha256(content).hexdigest(), "mode": mode})
    return result


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
    environment = {}
    try:
        definition = plistlib.loads(files[plist][0])
        environment = definition["EnvironmentVariables"]
        working_directory = definition["WorkingDirectory"]
        if "CLAUDLOBBY_INSTALLATION" in environment:
            return _git_family(files, definition, destination=destination, scope=scope,
                               phase=phase, fleet=fleet, bot=bot, enroll=enroll)
        admission = parse_unit_argv(definition["ProgramArguments"])
        if admission.unit != stem or admission.phase != phase or admission.release_id != release_id:
            raise ValueError("unbound unit admission")
        if (definition["Label"] != stem or not Path(working_directory).is_absolute()
                or not isinstance(environment, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items())
                or environment.get("CLAUDLOBBY_RELEASE_ID") != release_id):
            raise ValueError("unbound native identity")
    except (ValueError, TypeError, KeyError, ActivationError, plistlib.InvalidFileException) as exc:
        if isinstance(environment, dict) and "CLAUDLOBBY_INSTALLATION" in environment:
            raise PlanError("staged unit is not bound to its Git native entry") from exc
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
    git = bool(families) and "release_id" not in families[0]
    identity_keys = {"CLAUDLOBBY_ROOT", "FLEET_ROOT", "CLAUDLOBBY_INSTALLATION",
                     "CLAUDLOBBY_CLI", "CLAUDLOBBY_NATIVE_DIR", "CLAUDLOBBY_LIBRARY_DIR"}
    identities = {json.dumps([item["working_directory"],
                  {key: value for key, value in item["environment"].items() if key in identity_keys}
                  if git else item["environment"]], sort_keys=True) for item in families}
    if scope != "fleet" or not fleet or len(identities) != 1:
        raise PlanError("a unit template needs one fleet identity from the job units naming it")
    working_directory, environment = families[0]["working_directory"], families[0]["environment"]
    if git:
        # Linux-only templates inherit the actual Linux environment, not the
        # additional HOME/PATH values of the corresponding launchd definition.
        linux = next(item for item in families if item["platform"] == "Linux")
        working_directory, environment = linux["working_directory"], linux["environment"]
    result = []
    for name, (content, mode) in sorted(templates.items()):
        try:
            lines = set(content.decode("utf-8").splitlines())
            if git:
                fields = _systemd_fields(content)
                actual_environment = _systemd_environment(fields)
                if (fields.get(("Service", "WorkingDirectory")) != [working_directory]
                        or any(actual_environment.get(key) != value for key, value in environment.items()
                               if key in identity_keys)):
                    raise ValueError("unbound Git template")
                environment = actual_environment
        except UnicodeDecodeError as exc:
            raise PlanError("unit template is not UTF-8") from exc
        except ValueError as exc:
            raise PlanError("unit template differs from its Git identity") from exc
        if (f"WorkingDirectory={working_directory}" not in lines
                or any(unit_systemd_environment(key, value) not in lines
                       for key, value in environment.items())):
            raise PlanError("unit template differs from its fleet identity")
        result.append({"source": str(destination / name), "scope": scope, "phase": "producers",
                       **({} if git else {"release_id": release_id}), "working_directory": working_directory,
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


def validate_git_units(units: list[dict], installation) -> None:
    """Validate committed native bindings without a release or current source reads."""
    from .installation import parse_git_unit_argv
    fields = {"source", "scope", "phase", "working_directory", "environment", "fleet", "bot",
              "enroll", "admission", "service", "platform", "sha256", "mode"}
    if not isinstance(units, list):
        raise PlanError("effective configuration native units are invalid")
    seen = set()
    for item in units:
        if not isinstance(item, dict) or set(item) != fields:
            raise PlanError("effective configuration native unit fields are invalid")
        source, directory, env = item["source"], item["working_directory"], item["environment"]
        if (not isinstance(source, str) or not Path(source).is_absolute() or ".." in Path(source).parts
                or source in seen or not isinstance(directory, str) or not Path(directory).is_absolute()
                or ".." in Path(directory).parts or item["platform"] not in {"Linux", "Darwin"}
                or item["scope"] not in {"host", "fleet", "bot"}
                or item["phase"] not in {"bots", "ingest", "producers"}
                or (item["scope"] == "bot") != (item["phase"] == "bots")
                or type(item["enroll"]) is not bool
                or type(item["mode"]) is not int or item["mode"] & ~0o777
                or not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                or any(key in env for key in ("CLAUDLOBBY_RELEASE_ID", "CLAUDLOBBY_ARTIFACT_ID"))):
            raise PlanError("effective configuration native unit identity is invalid")
        seen.add(source)
        expected = {"CLAUDLOBBY_ROOT": str(installation.data_root),
                    "CLAUDLOBBY_INSTALLATION": str(installation.directory),
                    "CLAUDLOBBY_CLI": str(installation.cli), "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
                    "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
        if any(env.get(key) != value for key, value in expected.items()):
            raise PlanError("effective native unit belongs to another installation")
        if ((item["scope"] in {"fleet", "bot"} and not isinstance(item["fleet"], str))
                or item["scope"] == "host" and item["fleet"] is not None
                or (item["scope"] == "bot") != isinstance(item["bot"], str)):
            raise PlanError("effective native unit fleet/bot binding is invalid")
        suffix = ".plist" if item["platform"] == "Darwin" else ".service"
        admission = item["admission"]
        if Path(source).suffix == ".timer":
            if item["platform"] != "Linux" or admission is not None or item["service"] != Path(source).stem + ".service":
                raise PlanError("effective timer has an invalid service binding")
        elif is_unit_template(Path(source).name):
            if item["platform"] != "Linux" or admission is not None or item["service"] is not None:
                raise PlanError("effective native template is invalid")
        else:
            if (Path(source).suffix != suffix or item["service"] is not None
                    or not isinstance(admission, dict)
                    or set(admission) != {"kind", "unit", "mode", "argv", "command"}
                    or admission["kind"] != "git-native-v1"):
                raise PlanError("effective native unit has no Git entry")
            try:
                parsed = parse_git_unit_argv(admission["argv"])
            except ValueError as exc:
                raise PlanError("effective native unit has an invalid Git entry") from exc
            if (parsed.argv[0] != str(installation.cli) or parsed.root != installation.data_root
                    or parsed.unit != Path(source).stem or parsed.phase != item["phase"]
                    or parsed.unit != admission["unit"] or parsed.mode != admission["mode"]
                    or list(parsed.command) != admission["command"]):
                raise PlanError("effective native command differs from its Git entry")
    for timer in (item for item in units if item["service"] is not None):
        matches = [item for item in units if item["source"] == str(Path(timer["source"]).with_name(timer["service"]))]
        if (len(matches) != 1 or any(timer[key] != matches[0][key] for key in
                ("scope", "phase", "fleet", "bot", "working_directory", "environment", "enroll"))):
            raise PlanError("effective timer has no matching native service")


def current_git_declarations(snapshot: dict, platform: str) -> tuple[UnitDeclaration, ...]:
    """Inventory declarations from committed metadata and exact generated bytes."""
    from .installation import Installation
    if platform not in {"Linux", "Darwin"}:
        raise PlanError("unsupported supervision platform")
    installation = Installation(Path(snapshot["installation"]), Path(snapshot["data_root"]))
    validate_git_units(snapshot["units"], installation)
    result = []
    for item in snapshot["units"]:
        if item["platform"] != platform:
            continue
        source = Path(item["source"])
        if (not source.is_file() or source.is_symlink() or source.parent.resolve() != source.parent
                or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]
                or source.stat().st_mode & 0o777 != item["mode"]):
            raise PlanError(f"current generated unit differs from its committed configuration: {source}")
        result.append(UnitDeclaration(source, item["scope"], Path(item["working_directory"]), "",
                                     tuple(sorted(item["environment"].items())), item["fleet"], item["bot"], item["service"]))
    return tuple(result)


def staged_git_declarations(snapshot, rendered, platform):
    """Read exact pending unit bytes without requiring their live destination."""
    if platform not in {"Linux", "Darwin"}:
        raise PlanError("unsupported supervision platform")
    from .installation import Installation
    installation = Installation(Path(snapshot["installation"]), Path(snapshot["data_root"]))
    validate_git_units(snapshot["units"], installation)
    if (rendered.data_root != installation.data_root or rendered.revision != snapshot["revision"]
            or rendered.effects.get("units") != snapshot["units"]):
        raise PlanError("staged native declarations differ from candidate configuration")
    changes = {change.target: change.after for change in rendered.changes}
    result = []
    for item in snapshot["units"]:
        if item["platform"] != platform:
            continue
        source = Path(item["source"])
        after = changes.get(str(source))
        if after is None:
            parent = changes.get(str(source.parent), {})
            after = parent.get("files", {}).get(source.name) if parent.get("kind") == "tree" else None
        if after is None or any(after.get(key) != item[key] for key in ("sha256", "mode")):
            raise PlanError("staged native definition differs from candidate metadata")
        content = rendered.blob(item["sha256"])
        declaration = UnitDeclaration(source, item["scope"], Path(item["working_directory"]), "",
                                      tuple(sorted(item["environment"].items())), item["fleet"], item["bot"], item["service"])
        result.append((declaration, item, content))
    return tuple(result)
