"""Read-only enrollment evidence for coordinated activation, never enrollment.

Declarations must come from the reviewed current generated-config manifest,
not candidate renders substituted for the running release. Exact byte equality
binds argv and environment to that manifest; the native ownership reader remains
the sole parser of unit WorkingDirectory. A matching name is never ownership.

Unknown observations are blockers, not empty inventories. Darwin print and
print-disabled grammar is based on read-only macOS 26.1 observations, not a
stable Apple API. Exact identity fields are validated and unfamiliar managed
definition fields refuse completeness; native pause acceptance is separate.
No service mutations or filesystem writes occur here.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import stat
import subprocess

from .resources import PackageResources, get_resources


class InventoryError(ValueError):
    """Coverage or binding could not be proved; activation must not continue."""


class Adapter:
    """One selected-native invocation owner, also usable by activation controls.

    ``call`` returns CompletedProcess so callers retain the adapter's 0/1/3
    ownership and caller-membership meanings. Inventory never sources lib-common
    or bot.conf; cold bot readiness calls source their existing helper owner.
    Every call explicitly overrides an inherited native adapter directory.
    """

    _FUNCTIONS = frozenset({
        "svc_inventory_catalog", "svc_inventory_properties", "svc_inventory_disabled", "svc_inventory_state", "svc_bot_unit_owned_by",
        "svc_activation_snapshot", "svc_activation_assert_external",
        "svc_activation_pause", "svc_activation_resume",
        "svc_activation_start", "svc_activation_quiet", "svc_activation_bot_fence",
        "svc_activation_bot_ready", "svc_activation_handoff",
        "svc_activation_stop_private_server",
    })

    def __init__(self, package: PackageResources | None = None, *, runner=None):
        self.package = package if package is not None else get_resources()
        self.runner = runner if runner is not None else self._run

    @staticmethod
    def _run(command, *, timeout, env, capture_output, text):
        # A timed-out Bash must not leave its readiness poll/native child behind.
        # This never signals supervisor-owned services that the manager started.
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=text, env=env, start_new_session=True) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def call(self, function: str, *args: str | Path, timeout: float = 30):
        if function not in self._FUNCTIONS:
            raise InventoryError(f"unsupported private adapter function: {function}")
        native = self.package.native
        if not native.is_absolute() or not (native / "supervisor.sh").is_file():
            raise InventoryError("selected release has no native supervision adapter")
        env = dict(os.environ)
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        env["LC_ALL"] = "C"
        # Functions inherited through the environment must not replace uname or
        # the actual manager. Tests use the explicit runner seam instead.
        env = {key: value for key, value in env.items() if not key.startswith("BASH_FUNC_")}
        command = ['_SUPERVISOR_LIB_DIR="$1"; shift; _OS=$(uname -s) || exit 3;',
                   '. "$_SUPERVISOR_LIB_DIR/supervisor.sh" || exit 3; "$@"']
        return self.runner(["/bin/bash", "-c", " ".join(command), "supervision-inventory",
                            str(native), function, *(str(arg) for arg in args)],
                           capture_output=True, text=True, timeout=timeout, env=env)

    def read(self, function, *args) -> str:
        result = self.call(function, *args)
        if result.returncode:
            raise InventoryError(f"{function} failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout


@dataclass(frozen=True)
class UnitDeclaration:
    """One reviewed generated unit, including inactive/uninstalled declarations.

    ``environment`` is the exact expected identity subset (native_environment),
    as immutable key/value pairs. A timer names its service in ``service`` and
    inherits that service's binding. Scope is host, fleet or bot; fleet/bot IDs
    are declared metadata, never inferred from a unit's spelling.
    """

    source: Path
    scope: str
    working_directory: Path
    release_id: str
    environment: tuple[tuple[str, str], ...]
    fleet: str | None = None
    bot: str | None = None
    service: str | None = None


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    resolved: str
    mode: int
    link: str | None
    content: bytes
    sha256: str

    @classmethod
    def read(cls, path: Path):
        node = path.lstat()
        if not (stat.S_ISLNK(node.st_mode) or stat.S_ISREG(node.st_mode)):
            raise InventoryError(f"special installed/generated unit: {path}")
        resolved = path.resolve(strict=True)
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise InventoryError(f"unit does not resolve to a regular file: {path}")
        content = path.read_bytes()
        return cls(str(path), str(resolved), stat.S_IMODE(node.st_mode),
                   os.readlink(path) if path.is_symlink() else None,
                   content, hashlib.sha256(content).hexdigest())


@dataclass(frozen=True)
class EnrolledUnit:
    declaration: UnitDeclaration
    target: str
    generated: FileSnapshot
    installed: tuple[FileSnapshot, ...]
    properties: tuple[tuple[str, str], ...]


def _json_value(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class EnrollmentInventory:
    data_root: Path
    manager: str
    catalog: str
    units: tuple[EnrolledUnit, ...]
    observed_files: tuple[FileSnapshot, ...]
    foreign: tuple[str, ...]
    issues: tuple[str, ...]
    bootstrap_empty: bool = False
    legacy_source: bool = False

    def payload(self) -> dict:
        return _json_value(asdict(self))

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.payload(), sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()

    def require_complete(self):
        if self.issues or (not self.units and not self.bootstrap_empty):
            raise InventoryError("incomplete enrollment inventory: " + "; ".join(self.issues))
        if self.bootstrap_empty:
            if self.units or self.bootstrap_empty is not True:
                raise InventoryError("bootstrap inventory must have no original declarations")
            manager, _, _, _, _ = _catalog(self.catalog)
            if manager != self.manager or set(self.foreign) != {row.path for row in self.observed_files}:
                raise InventoryError("bootstrap inventory lacks complete foreign-file coverage")
        return self

    def candidate_only(self, recovery: EnrollmentInventory) -> tuple[EnrolledUnit, ...]:
        """Return exact owned candidates only; never unlink anything here."""
        self.require_complete()
        recovery.require_complete()
        if (self.data_root, self.manager) != (recovery.data_root, recovery.manager):
            raise InventoryError("candidate/recovery enrollment scopes differ")
        targets = {unit.target for unit in recovery.units}
        return tuple(unit for unit in self.units if unit.target not in targets and unit.installed)

    def check_files(self) -> None:
        """Recheck immediately before parking; file bytes alone are not liveness."""
        _, _, directories, _, _ = _catalog(self.catalog)
        present = {str(path) for directory in directories for path in _directory_files(directory)
                   if path.name.endswith(_SUFFIXES)}
        if present != {saved.path for saved in self.observed_files}:
            raise InventoryError("installed search-path contents changed")
        for saved in self.observed_files:
            if FileSnapshot.read(Path(saved.path)) != saved:
                raise InventoryError(f"enrollment file changed: {saved.path}")
        for unit in self.units:
            for saved in (unit.generated, *unit.installed):
                if FileSnapshot.read(Path(saved.path)) != saved:
                    raise InventoryError(f"enrollment file changed: {saved.path}")


_NAME = re.compile(r"[A-Za-z0-9_.@:-]+")
_SUFFIXES = (".service", ".timer", ".socket", ".path", ".plist")


def _directory_files(directory):
    try:
        return tuple(directory.iterdir())
    except FileNotFoundError:
        return ()  # An absent native search directory is observable; unreadable is not.


def _require_no_selection(data_root):
    from .activation_state import ActivationError, read_selection
    try:
        if read_selection(data_root) is not None:
            raise InventoryError("bootstrap requires no prior release selection")
    except (ActivationError, OSError) as exc:
        raise InventoryError(f"bootstrap selection cannot be proved absent: {exc}") from exc


def _catalog(text):
    manager, domain, directories, names, loaded = "", "", [], set(), {}
    launchd_rows = False
    for line in text.splitlines():
        fields = line.split("\t")
        if fields == ["PID", "Status", "Label"] or line.split() == ["PID", "Status", "Label"]:
            launchd_rows = True
            continue
        if launchd_rows:
            row = line.split()
            if len(row) != 3 or not re.fullmatch(r"-|[0-9]+", row[0]) or not re.fullmatch(r"-?[0-9]+", row[1]):
                raise InventoryError("unknown launchctl list row")
            key, value = "loaded", row[2] + ".plist"
        elif len(fields) == 2:
            key, value = fields
        else:
            raise InventoryError("malformed adapter catalog")
        if key == "manager" and not manager:
            manager = value
        elif key == "domain" and not domain:
            domain = value
        elif key == "directory" and Path(value).is_absolute():
            directories.append(Path(value))
        elif key in ("installed", "loaded") and _NAME.fullmatch(value):
            names.add(value)
            if key == "loaded":
                if value in loaded:
                    raise InventoryError("duplicate loaded identity")
                loaded[value] = ("active" if row[0] != "-" else "inactive") if launchd_rows else "unknown"
        else:
            raise InventoryError(f"unknown adapter catalog row: {line}")
    if not directories or manager not in ("Linux", "Darwin"):
        raise InventoryError("missing manager/search-path coverage")
    if manager == "Darwin" and (not launchd_rows or not re.fullmatch(r"(?:gui|user)/[0-9]+", domain)):
        raise InventoryError("unproved launchd domain")
    return manager, domain, directories, names, loaded


def _properties(text):
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in result:
            raise InventoryError("ambiguous native unit properties")
        result[key] = value
    required = {"Id", "LoadState", "ActiveState", "UnitFileState", "FragmentPath",
                "DropInPaths", "NeedDaemonReload", "Triggers", "TriggeredBy"}
    service_fields = {"WorkingDirectory", "Environment", "ExecStart"}
    allowed = required | service_fields
    if result.get("Id", "").endswith(".service"):
        required |= service_fields
    if not required <= result.keys() or not result.keys() <= allowed:
        raise InventoryError("missing/unknown native unit properties")
    return result


def _environment(text):
    result = {}
    for item in shlex.split(text):
        key, separator, value = item.partition("=")
        if not separator or key in result:
            raise InventoryError("ambiguous native environment")
        result[key] = value
    return result


def _darwin_disabled(text: str) -> dict[str, str]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "\tdisabled services = {" or lines[-1] != "\t}":
        raise InventoryError("unknown launchd disabled-override structure")
    result = {}
    for line in lines[1:-1]:
        match = re.fullmatch(r'\t\t"([A-Za-z0-9_.@:-]+)" => (enabled|disabled)', line)
        if not match or match[1] in result:
            raise InventoryError("unknown or duplicate launchd disabled override")
        result[match[1]] = match[2]
    return result


# Public field names observed on macOS 26.1 running and idle managed jobs. Only
# identity/launch fields below are interpreted; these diagnostic scalar names
# and coalition blocks are structurally checked, never treated as ownership.
_DARWIN_DIAGNOSTICS = {
    "active count", "stdout path", "stderr path", "asid", "minimum runtime",
    "exit timeout", "runs", "immediate reason", "forks", "execs", "initialized",
    "trampolined", "started suspended", "proxy started suspended", "checked allocations",
    "checked allocations reason", "checked allocations flags", "last exit code",
    "last terminating signal", "resource coalition", "jetsam coalition", "spawn type",
    "jetsam priority", "jetsam memory limit (active)", "jetsam memory limit (inactive)",
    "jetsamproperties category", "jetsam thread limit", "cpumon", "run interval", "properties",
    "event triggers", "event channels",
}


def _darwin_print(text: str, target: str) -> dict[str, str]:
    """Parse the observed tab-indented print projection without shell evaluation.

    Values/arguments containing controls or ambiguous surrounding whitespace are
    unsupported. Unknown diagnostic fields are retained by name: an unrelated
    job can be classified from its identity, but a managed match must refuse it.
    """
    lines = text.splitlines()
    if not lines or lines[0] != target + " = {" or lines[-1] != "}":
        raise InventoryError("launchd print target or outer structure differs")
    scalar, blocks = {}, {}
    index = 1
    while index < len(lines) - 1:
        line = lines[index]
        index += 1
        if not line:
            continue
        match = re.fullmatch(r"\t([^\t=]+) = (.*)", line)
        if not match or match[1] in scalar or match[1] in blocks:
            raise InventoryError("ambiguous launchd print field")
        key, value = match.groups()
        if value != "{":
            scalar[key] = value
            continue
        body, depth = [], 1
        while index < len(lines) - 1:
            inner = lines[index]
            index += 1
            if inner == "\t" * depth + "}":
                depth -= 1
                if depth == 0:
                    break
            elif inner.endswith((" = {", " => {")):
                depth += 1
            if inner and not inner.startswith("\t\t"):
                raise InventoryError("unknown launchd block indentation")
            body.append(inner)
        if depth:
            raise InventoryError("unterminated launchd print block")
        blocks[key] = body
    required = {"path", "type", "state", "program", "domain"}
    if not required <= scalar.keys() or not re.fullmatch(re.escape(target.rsplit("/", 1)[0]) + r" \[[0-9]+\]", scalar["domain"]):
        raise InventoryError("launchd print domain or execution identity is incomplete")
    if scalar["state"] == "running" and re.fullmatch(r"[0-9]+", scalar.get("pid", "")) and int(scalar["pid"]) > 1:
        active = "active"
    elif scalar["state"] == "not running" and "pid" not in scalar:
        active = "inactive"
    else:
        raise InventoryError("unknown or transitional launchd state")
    for key in ("path", "program", "working directory"):
        if key in scalar and (not Path(scalar[key]).is_absolute() or scalar[key].strip() != scalar[key]
                              or any(ord(c) < 32 for c in scalar[key])):
            raise InventoryError("unsupported launchd execution path")
    argv = []
    for line in blocks.get("arguments", []):
        if not line.startswith("\t\t") or not line[2:] or line[2:].strip() != line[2:] or any(ord(c) < 32 for c in line[2:]):
            raise InventoryError("unsupported launchd argument representation")
        argv.append(line[2:])
    environments = {}
    for block in ("inherited environment", "default environment", "environment"):
        values = {}
        for line in blocks.get(block, []):
            match = re.fullmatch(r"\t\t([A-Za-z_][A-Za-z0-9_]*) => (.*)", line)
            if (not match or match[1] in values or match[2].strip() != match[2]
                    or any(ord(c) < 32 for c in match[2])):
                raise InventoryError("unsupported launchd environment representation")
            values[match[1]] = match[2]
        environments[block] = values
    effective = {**environments["inherited environment"], **environments["default environment"], **environments["environment"]}
    known = required | {"working directory", "pid", "arguments", *environments} | _DARWIN_DIAGNOSTICS
    return {"Id": target.rsplit("/", 1)[1] + ".plist", "LoadState": "loaded", "ActiveState": active,
            "FragmentPath": scalar["path"], "WorkingDirectory": scalar.get("working directory", ""),
            "Program": scalar["program"], "Arguments": json.dumps(argv),
            "Environment": shlex.join(f"{key}={value}" for key, value in sorted(effective.items())),
            "ConfiguredEnvironment": json.dumps(environments["environment"], sort_keys=True),
            "Type": scalar["type"], "UnknownFields": json.dumps(sorted((scalar.keys() | blocks.keys()) - known))}


class _PlistKeys(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise InventoryError("duplicate reviewed plist key")
        super().__setitem__(key, value)


def _darwin_source(content: bytes) -> dict:
    """Read launch identity/policy, not the shared WorkingDirectory predicate."""
    try:
        source = plistlib.loads(content, dict_type=_PlistKeys)
    except (ValueError, TypeError, plistlib.InvalidFileException) as exc:
        raise InventoryError("unreadable plist launch definition") from exc
    if not isinstance(source, dict):
        raise InventoryError("plist launch definition is not an object")
    argv = source.get("ProgramArguments", [])
    env = source.get("EnvironmentVariables", {})
    program = source.get("Program", argv[0] if isinstance(argv, list) and argv else "")
    if (not isinstance(argv, list) or not all(isinstance(arg, str) and arg and arg.strip() == arg
            and not any(ord(c) < 32 for c in arg) for arg in argv)
            or not isinstance(program, str) or not Path(program).is_absolute()
            or not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items())
            or type(source.get("Disabled", False)) is not bool):
        raise InventoryError("unsupported plist program, arguments, environment or disabling")
    return {"label": source.get("Label"), "program": program, "arguments": argv, "environment": env,
            "disabled": source.get("Disabled", False), "has_directory": "WorkingDirectory" in source,
            "directory": source.get("WorkingDirectory")}


def _darwin_binding(name, content, installed_path, directory, environment, props, loaded, disabled):
    source = _darwin_source(content)
    if source["label"] != name.removesuffix(".plist") or any(source["environment"].get(key) != value for key, value in environment.items()):
        raise InventoryError("reviewed plist label or release identity differs")
    override = disabled.get(source["label"], "unset")
    if loaded:
        if not installed_path or not props:
            raise InventoryError("loaded consumer lacks installed source or effective binding")
        configured = json.loads(props["ConfiguredEnvironment"])
        automatic = {"OSLogRateLimit", "XPC_SERVICE_NAME"}
        if (props["FragmentPath"] != installed_path or props["Type"] != "LaunchAgent"
                or props["UnknownFields"] != "[]" or props["WorkingDirectory"] != directory
                or props["Program"] != source["program"] or json.loads(props["Arguments"]) != source["arguments"]
                or any(configured.get(key) != value for key, value in source["environment"].items())
                or set(configured) - source["environment"].keys() - automatic
                or configured.get("XPC_SERVICE_NAME", source["label"]) != source["label"]):
            raise InventoryError("loaded launchd definition differs from reviewed plist")
    else:
        props = {"LoadState": "unloaded", "ActiveState": "inactive"}
    # Override state is explicit, separate from the native adapter's restoration
    # token. validate_darwin_unit rechecks it around pause/restoration effects.
    props.update(UnitFileState="unchanged", DisabledOverride=override,
                 EnabledState=override if override != "unset" else "disabled" if source["disabled"] else "enabled")
    return props


def validate_darwin_unit(adapter, target: str, *, source: bytes, installed_path: str,
                        working_directory: str, environment: dict, original: dict,
                        require_original_load: bool = False) -> dict:
    """Recheck frozen binding/overrides during parking and exact restoration.

    The source may be parked, so read native state through the shared adapter;
    never infer that a failed print means unloaded. This performs no mutation.
    """
    state = adapter.read("svc_inventory_state", installed_path, target).split()
    if len(state) != 3 or state[0] != "unchanged" or state[1] not in ("loaded", "unloaded") or state[2] not in ("active", "inactive"):
        raise InventoryError("unknown launchd activation state")
    disabled = _darwin_disabled(adapter.read("svc_inventory_disabled", target.rsplit("/", 1)[0]))
    props = _darwin_print(adapter.read("svc_inventory_properties", target), target) if state[1] == "loaded" else {}
    props = _darwin_binding(target.rsplit("/", 1)[1] + ".plist", source, installed_path,
                            working_directory, environment, props, state[1] == "loaded", disabled)
    if (props["ActiveState"] != state[2]
            or any(props[key] != original.get(key) for key in ("DisabledOverride", "EnabledState"))
            or (require_original_load and props["LoadState"] != original.get("LoadState"))):
        raise InventoryError("launchd disabling, binding or load state changed")
    return props


def collect_enrollment(data_root: Path, declarations: tuple[UnitDeclaration, ...], *,
                       package: PackageResources | None = None, runner=None,
                       bootstrap_empty: bool = False, legacy_source: bool = False,
                       adapter=None) -> EnrollmentInventory:
    """Observe every installed/loaded candidate, preserving incomplete evidence.

    This accepts the current manifest, including intentionally uninstalled units;
    a missing generated source is always torn evidence. Calling this twice around
    the native reads detects ordinary source/list drift, not an atomic OS snapshot.
    Activation still must recheck under its host lock and prove quiescence.

    ``bootstrap_empty`` is a distinct no-prior-enrollment proof: declarations
    must be empty and the release selector absent. The same full catalog and
    installed-file checks must classify every observation before it is complete.
    Candidate declarations never stand in for a previous installed release.
    """
    data_root = data_root.resolve(strict=True)
    if (type(bootstrap_empty) is not bool or type(legacy_source) is not bool
            or bootstrap_empty and (declarations or legacy_source)):
        raise InventoryError("bootstrap inventory must have no original declarations")
    if not declarations and not bootstrap_empty:
        raise InventoryError("empty generated manifest is not deletion authority")
    if bootstrap_empty:
        _require_no_selection(data_root)
    adapter = adapter if adapter is not None else Adapter(package, runner=runner)
    expected = {}
    for declaration in declarations:
        name = declaration.source.name
        env = dict(declaration.environment)
        if (name in expected or not _NAME.fullmatch(name) or not name.endswith(_SUFFIXES)
                or not declaration.source.is_absolute() or not declaration.working_directory.is_absolute()
                or declaration.scope not in ("host", "fleet", "bot")
                or (declaration.release_id != "" if legacy_source else not declaration.release_id)
                or (declaration.scope in ("fleet", "bot") and not declaration.fleet)
                or (declaration.scope == "bot" and not declaration.bot)
                or len(env) != len(declaration.environment)
                or env.get("CLAUDLOBBY_ROOT") != str(data_root)
                or not legacy_source and not all(env.get(key) for key in (
                    "CLAUDLOBBY_NATIVE_DIR", "CLAUDLOBBY_LIBRARY_DIR", "CLAUDLOBBY_CLI",
                    "CLAUDLOBBY_ARTIFACT_ID", "FLEET_ROOT"))):
            raise InventoryError(f"ambiguous or incomplete declaration: {name}")
        expected[name] = declaration
    for declaration in declarations:
        if declaration.source.suffix == ".timer":
            service = expected.get(declaration.service)
            if (service is None or service.source.suffix != ".service"
                    or (service.scope, service.fleet, service.bot, service.environment, service.release_id)
                    != (declaration.scope, declaration.fleet, declaration.bot, declaration.environment, declaration.release_id)):
                raise InventoryError("timer has no matching declared service binding")
    catalog = adapter.read("svc_inventory_catalog")
    manager, domain, directories, names, loaded = _catalog(catalog)
    issues, foreign, installed, units = [], [], {}, []
    try:
        for directory in dict.fromkeys(directories):
            for path in sorted(_directory_files(directory)):
                if path.name.endswith(_SUFFIXES):
                    installed.setdefault(path.name, []).append(FileSnapshot.read(path))
                    names.add(path.name)
    except (OSError, RuntimeError, InventoryError) as exc:
        issues.append(f"installed search-path coverage failed: {exc}")
    properties = {}
    disabled = {}
    if manager == "Linux":
        for name in sorted(names | set(expected)):
            if not name.endswith(_SUFFIXES):
                continue
            try:
                props = _properties(adapter.read("svc_inventory_properties", name))
                if props["Id"] != name:
                    raise InventoryError("native identity is an alias")
                properties[name] = props
            except (InventoryError, OSError, subprocess.SubprocessError) as exc:
                issues.append(f"{name}: cannot observe effective definition: {exc}")
    else:
        disabled = _darwin_disabled(adapter.read("svc_inventory_disabled", domain))
        for name in sorted(loaded):
            try:
                properties[name] = _darwin_print(adapter.read("svc_inventory_properties", domain + "/" + name.removesuffix(".plist")),
                                                 domain + "/" + name.removesuffix(".plist"))
                if properties[name]["ActiveState"] != loaded[name]:
                    raise InventoryError("launchd list/print activity changed")
            except (InventoryError, OSError, subprocess.SubprocessError) as exc:
                issues.append(f"{name}: cannot observe effective definition: {exc}")
    owners = {data_root, *(item.working_directory.resolve() for item in declarations)}

    def related_properties(props):
        env = _environment(props.get("Environment", ""))
        cwd = props.get("WorkingDirectory", "")
        return (env.get("CLAUDLOBBY_ROOT") == str(data_root)
                or bool(cwd and (Path(cwd).resolve() in owners or Path(cwd).resolve().is_relative_to(data_root))))

    for name in sorted(names - set(expected)):
        props = properties.get(name, {})
        if bootstrap_empty and manager == "Linux" and name in loaded and props.get("LoadState") != "loaded":
            issues.append(f"{name}: loaded ownership is unknown")
        related = related_properties(props)
        anchors = {anchor for declaration in declarations for key, anchor in declaration.environment
                   if key in ("CLAUDLOBBY_ROOT", "CLAUDLOBBY_NATIVE_DIR", "CLAUDLOBBY_CLI")}
        if bootstrap_empty:
            anchors.add(str(data_root))
            related |= str(data_root) in props.get("ExecStart", "")
        related |= any(anchor in arg for anchor in anchors for arg in
                       [props.get("Program", ""), *json.loads(props.get("Arguments", "[]"))])
        related |= any(unit in expected or related_properties(properties.get(unit, {}))
                       for unit in props.get("Triggers", "").split())
        for saved in installed.get(name, ()):
            if not name.endswith((".service", ".plist")):
                # An undeclared activation source cannot be classified by a
                # filename; its observed Triggers must prove its destination.
                continue
            if bootstrap_empty and manager == "Linux":
                # A stale cached definition cannot prove the installed source
                # foreign. Bind its parsed directory to the effective one with
                # the existing ownership reader, including nested bot roots.
                if (props.get("FragmentPath") != saved.path or props.get("NeedDaemonReload") != "no"
                        or adapter.call("svc_bot_unit_owned_by", saved.path,
                                        props.get("WorkingDirectory", "")).returncode != 0):
                    issues.append(f"{name}: installed/effective ownership is unknown")
            if bootstrap_empty and manager == "Darwin":
                try:
                    source = _darwin_source(saved.content)
                    if source["has_directory"]:
                        # The shared ownership reader validates the raw plist
                        # directory before it can establish a nested root link.
                        if adapter.call("svc_bot_unit_owned_by", saved.path, source["directory"]).returncode != 0:
                            raise InventoryError("installed ownership is unknown")
                        related |= Path(source["directory"]).resolve().is_relative_to(data_root)
                    related |= (source["environment"].get("CLAUDLOBBY_ROOT") == str(data_root)
                                or any(anchor in arg for anchor in anchors
                                       for arg in [source["program"], *source["arguments"]]))
                except InventoryError as exc:
                    issues.append(f"{name}: {exc}")
            results = [adapter.call("svc_bot_unit_owned_by", saved.path, owner).returncode for owner in owners]
            if 0 in results:
                related = True
            elif any(code != 1 for code in results):
                try:
                    # Unrelated Apple/app plists commonly omit WorkingDirectory.
                    # Their explicit launch identity can rule out this release;
                    # a malformed directory is still unknown, never foreign.
                    source = _darwin_source(saved.content) if manager == "Darwin" else None
                    if source is None or source["has_directory"]:
                        raise InventoryError("installed ownership is unknown")
                    related |= (source["environment"].get("CLAUDLOBBY_ROOT") == str(data_root)
                                or any(anchor in arg for anchor in anchors
                                       for arg in [source["program"], *source["arguments"]]))
                except InventoryError:
                    issues.append(f"{name}: installed ownership is unknown")
        if related:
            issues.append(f"{name}: owned consumer is absent from generated manifest")
        elif (not _environment(props.get("Environment", "")).get("CLAUDLOBBY_ROOT")
              and any(anchor in props.get("ExecStart", "")
                      for declaration in declarations
                      for key, anchor in declaration.environment
                      if key in ("CLAUDLOBBY_NATIVE_DIR", "CLAUDLOBBY_CLI"))):
            issues.append(f"{name}: release consumer has no declared data-root binding")
        elif name in loaded and not installed.get(name) and manager == "Darwin" and not props:
            issues.append(f"{name}: loaded job has no observable installed binding")
        else:
            foreign.extend(saved.path for saved in installed.get(name, ()))
    for name, declaration in expected.items():
        generated = None
        sources = tuple(installed.get(name, ()))
        props = properties.get(name, {})
        try:
            generated = FileSnapshot.read(declaration.source)
            if len(sources) > 1:
                raise InventoryError("multiple installed definitions shadow one another")
            binding = expected[declaration.service] if declaration.service else declaration
            if adapter.call("svc_bot_unit_owned_by", binding.source, binding.working_directory).returncode != 0:
                raise InventoryError("generated ownership is foreign or unknown")
            for saved in sources:
                if saved.content != generated.content:
                    raise InventoryError("installed bytes differ from reviewed generated source")
                if not declaration.service and adapter.call("svc_bot_unit_owned_by", saved.path, declaration.working_directory).returncode != 0:
                    raise InventoryError("installed ownership is foreign or unknown")
            if manager == "Linux":
                if not props:
                    raise InventoryError("missing effective native properties")
                if sources:
                    if (props["FragmentPath"] != sources[0].path or props["LoadState"] != "loaded"
                            or props["DropInPaths"] or props["NeedDaemonReload"] != "no"):
                        raise InventoryError("effective definition is missing, masked, overridden or stale")
                    if props["ActiveState"] not in ("active", "inactive") or props["UnitFileState"] not in ("enabled", "enabled-runtime", "disabled", "static"):
                        raise InventoryError("unknown or transitional supervisor state")
                    if declaration.service:
                        if props["Triggers"].split() != [declaration.service] or not installed.get(declaration.service):
                            raise InventoryError("timer activates a different service")
                    elif (Path(props["WorkingDirectory"]).resolve() != declaration.working_directory.resolve()
                          or any(_environment(props["Environment"]).get(key) != value for key, value in declaration.environment)):
                        raise InventoryError("loaded data/fleet/release identity differs")
                elif name in loaded or props["LoadState"] != "not-found":
                    raise InventoryError("loaded consumer lacks installed source bytes")
            else:
                props = _darwin_binding(name, generated.content, sources[0].path if sources else None,
                                        str(declaration.working_directory), dict(declaration.environment),
                                        props, name in loaded, disabled)
        except (OSError, RuntimeError, InventoryError) as exc:
            issues.append(f"{name}: {exc}")
        if generated is not None:
            target = domain + "/" + name.removesuffix(".plist") if domain else name
            units.append(EnrolledUnit(declaration, target, generated, sources, tuple(sorted(props.items()))))
    observed_files = tuple(saved for name in sorted(installed) for saved in installed[name])
    result = EnrollmentInventory(data_root, manager, catalog, tuple(units), observed_files,
                                 tuple(sorted(foreign)), tuple(issues), bootstrap_empty, legacy_source)
    try:
        result.check_files()
        if adapter.read("svc_inventory_catalog") != catalog:
            raise InventoryError("installed/loaded catalog changed during inventory")
        if manager == "Darwin" and _darwin_disabled(adapter.read("svc_inventory_disabled", domain)) != disabled:
            raise InventoryError("launchd disabled overrides changed during inventory")
        if bootstrap_empty:
            _require_no_selection(data_root)
    except (InventoryError, OSError, subprocess.SubprocessError) as exc:
        result = EnrollmentInventory(data_root, manager, catalog, tuple(units), observed_files,
                                     tuple(sorted(foreign)), (*issues, str(exc)), bootstrap_empty, legacy_source)
    return result
