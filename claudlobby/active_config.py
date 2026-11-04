"""Operational context from committed effective inputs, never pending edits.

The caller holds mutation_admission through an operation. This reader neither
admits a mutation nor treats consumer readiness as configuration authority.
Authoring/read-only declaration commands continue to use context.load_context.
"""

import json
import hashlib
import os
from pathlib import Path
import re
import tempfile

from .activation_state import ActivationError, read_activation, read_selection
from .config_plan import ConfigPlan, RenderedConfiguration, PlanError, read_plan
from .context import BotNotFoundError, Context, resolve_paths
from .paths import Paths
from .resources import PackageResources, get_resources
from .installation import Installation


def _snapshot_path(root: Path) -> Path:
    root = Path(root)
    path = root / "state/config.json"
    if (not root.is_absolute() or root.resolve() != root
            or path.parent.resolve() != path.parent or path.is_symlink()):
        raise PlanError("effective configuration location is redirected")
    return path


def _snapshot_context(root: Path, fleet: str, item: dict, *, bot: str | None,
                      package: PackageResources) -> Context:
    from .config import load_fleet_snapshot
    from yaml import YAMLError
    if (not isinstance(item, dict) or set(item) != {"manifest", "fleet", "projects"}
            or not isinstance(item["manifest"], str) or not isinstance(item["fleet"], str)
            or (item["projects"] is not None and not isinstance(item["projects"], str))):
        raise PlanError("effective configuration has invalid frozen inputs")
    manifest = Path(item["manifest"])
    if (not manifest.is_absolute() or manifest.name != "fleet.yaml"
            or ".." in manifest.parts or manifest.parent.resolve() != manifest.parent
            or manifest.is_relative_to(root / "state")
            or (manifest.is_relative_to(root) and "runtime" in manifest.relative_to(root).parts)
            or (package.installation is not None
                and manifest.is_relative_to(package.installation.directory))):
        raise PlanError("active fleet directory is redirected")
    try:
        config, defaults = load_fleet_snapshot(manifest, item["fleet"].encode(),
                                             item["projects"].encode() if item["projects"] is not None else None)
    except (ValueError, TypeError, AttributeError, KeyError, YAMLError) as exc:
        raise PlanError("effective configuration has invalid frozen inputs") from exc
    if config.name != fleet:
        raise PlanError("frozen configuration declares another fleet")
    if bot is not None and bot not in config.bots:
        raise BotNotFoundError(f"bot {bot!r} is not in the active fleet {fleet!r}")
    paths = Paths(root, package=package, fleet_dir=None if manifest.parent == root else manifest.parent)
    if paths.fleet_config_dir != manifest.parent:
        raise PlanError("active fleet directory is redirected")
    return Context(paths, config, defaults, bot)


def _validate_snapshot(snapshot: dict, installation: Installation, revision: str | None = None) -> None:
    if (not isinstance(snapshot, dict) or set(snapshot) != {
            "schema", "data_root", "installation", "source", "revision", "fleets", "owned_outputs", "units", "fingerprint"}
            or type(snapshot["schema"]) is not int or snapshot["schema"] != 1
            or snapshot["data_root"] != str(installation.data_root)
            or snapshot["installation"] != str(installation.directory)
            or snapshot["source"] != str(installation.source)
            or not isinstance(snapshot["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", snapshot["revision"])
            or (revision is not None and snapshot["revision"] != revision)):
        raise PlanError("effective configuration installation/source provenance differs")
    if snapshot["fingerprint"] != _fingerprint(snapshot):
        raise PlanError("effective configuration fingerprint differs")
    if not isinstance(snapshot["fleets"], dict) or not snapshot["fleets"]:
        raise PlanError("effective configuration has no frozen fleets")
    # The same parser used by declaration commands verifies every frozen fleet.
    package = PackageResources(installation.source / "library", installation.source / "library/voices",
                               installation.source / "templates", installation.source, installation.native,
                               installation.source / "claudlobby/system.yaml", "", snapshot["revision"], "",
                               installation)
    contexts = []
    for name, item in snapshot["fleets"].items():
        if not isinstance(name, str) or not name:
            raise PlanError("effective configuration has an invalid fleet name")
        contexts.append(_snapshot_context(installation.data_root, name, item, bot=None, package=package))
    outputs = snapshot["owned_outputs"]
    if not isinstance(outputs, list):
        raise PlanError("effective configuration ownership is invalid")
    seen = set()
    for output in outputs:
        if (not isinstance(output, dict) or set(output) != {"target", "state"}
                or not isinstance(output["target"], str)
                or not Path(output["target"]).is_absolute() or output["target"] in seen
                or not _valid_output_state(output["state"])):
            raise PlanError("effective configuration ownership is invalid")
        seen.add(output["target"])
        target = Path(output["target"])
        if (".." in target.parts or target.parent.resolve() != target.parent
                or target.is_relative_to(installation.directory)
                or not _owned_output(target, output["state"], contexts)):
            raise PlanError("effective configuration ownership escapes generated destinations")
    from .config_units import validate_git_units
    validate_git_units(snapshot["units"], installation)
    by_fleet = {context.fleet.name: context for context in contexts}
    for unit in snapshot["units"]:
        source = Path(unit["source"])
        context = by_fleet.get(unit["fleet"])
        if unit["scope"] == "host":
            directory, working = installation.data_root / "runtime/_host/timers", installation.data_root
        else:
            if context is None or (unit["bot"] is not None and unit["bot"] not in context.fleet.bots):
                raise PlanError("effective native unit has no declared fleet/bot")
            directory = (context.paths.bot_runtime(unit["bot"]) if unit["scope"] == "bot"
                         else context.paths.runtime_fleet / "timers")
            working = directory if unit["scope"] == "bot" else installation.data_root
            if unit["environment"].get("FLEET_ROOT") != str(context.paths.fleet_config_dir):
                raise PlanError("effective native unit has another fleet root")
        if source.parent != directory or unit["working_directory"] != str(working):
            raise PlanError("effective native unit escapes its declared owner")
        states = []
        for output in outputs:
            target, state = Path(output["target"]), output["state"]
            if source == target and state["kind"] == "file":
                states.append(state)
            elif source.is_relative_to(target) and state["kind"] == "tree":
                item = state["files"].get(str(source.relative_to(target)))
                if item is not None:
                    states.append({"kind": "file", **item})
        if states != [{"kind": "file", "mode": unit["mode"], "sha256": unit["sha256"]}]:
            raise PlanError("effective native unit differs from its owned generated bytes")


def _owned_output(target: Path, state: dict, contexts: list[Context]) -> bool:
    """Ownership never covers bot-authored trees, secrets or the host state store."""
    kind = state["kind"]
    host = contexts[0].paths.root / "runtime/_host"
    if target.is_relative_to(host / "timers") or target in {
            host / "mention-allowlist", host / "bot-handles"}:
        return True
    for context in contexts:
        paths, fleet = context.paths, context.fleet
        if target == paths.runtime / "composed.json" or target.is_relative_to(paths.runtime_fleet / "timers"):
            return True
        if paths.shared_docs is not None and kind == "directory" and target in {
                paths.shared_docs / name for name in
                ("planning/active", "planning/completed", "decisions", "knowledge", "runbooks")}:
            return True
        for bot_id, bot in fleet.bots.items():
            directory = paths.bot_runtime(bot_id)
            if kind == "directory" and target in {directory / name for name in
                    ("", ".claude", ".cli", ".cli/bin", "memory", "projects", "data", "data/events", "logs", "mounts")}:
                return True  # Ensure these directories; never own their children.
            if target.is_relative_to(directory):
                relative = target.relative_to(directory)
                if str(relative) in {"CLAUDE.md", ".mcp.json", "bot.conf", ".gitconfig",
                        ".gitconfig-github-app-id", ".claude/settings.local.json", ".cli/bin/claudlobby",
                        f"{fleet.service_prefix}.{bot_id}.service", f"{fleet.service_prefix}.{bot_id}.plist"}:
                    return True
                if relative.parts and relative.parts[0] in {"tools", "mounts"}:
                    return True
                if relative.is_relative_to(".claude/skills"):
                    return True
            if bot.telegram is not None and bot.telegram.handle:
                from .composer import telegram_channel_rel
                if target == Path.home() / telegram_channel_rel(bot.telegram.handle) / "access.json":
                    return kind == "file"
    return False


def _valid_output_state(state) -> bool:
    if not isinstance(state, dict):
        return False
    kind = state.get("kind")
    fields = {"absent": {"kind"}, "directory": {"kind", "mode"},
              "file": {"kind", "mode", "sha256"}, "symlink": {"kind", "target"},
              "tree": {"kind", "mode", "files"}}
    if kind not in fields or set(state) != fields[kind]:
        return False
    if kind == "symlink":
        return isinstance(state["target"], str) and bool(state["target"])
    if kind != "absent" and (type(state["mode"]) is not int or state["mode"] & ~0o777):
        return False
    if kind == "file":
        return isinstance(state["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", state["sha256"]) is not None
    if kind == "tree":
        if not isinstance(state["files"], dict):
            return False
        for name, item in state["files"].items():
            if (not isinstance(name, str) or not name or name == "."
                    or Path(name).is_absolute() or ".." in Path(name).parts
                    or str(Path(name)) != name or "\\" in name
                    or not isinstance(item, dict) or set(item) != {"mode", "sha256"}
                    or not _valid_output_state({"kind": "file", **item})):
                return False
    return True


def _fingerprint(snapshot: dict) -> str:
    content = {key: value for key, value in snapshot.items() if key != "fingerprint"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot_from_plan(plan: ConfigPlan | RenderedConfiguration, installation: Installation, revision: str) -> dict:
    """Freeze effective inputs and output ownership, independent of startup progress."""
    if plan.data_root != installation.data_root or getattr(plan, "revision", revision) != revision:
        raise PlanError("configuration plan belongs to another data root")
    sources = plan.effects.get("fleet_sources")
    if not isinstance(sources, dict) or set(sources) != set(plan.fleets):
        raise PlanError("active plan has no complete frozen fleet configuration")
    fleets = {}
    for name in plan.fleets:
        item = sources[name]
        if not isinstance(item, dict) or set(item) != {"fleet", "projects"}:
            raise PlanError("active fleet source fields are incomplete")
        manifest, content = plan.frozen_input(item["fleet"], required=True)
        projects, project_content = plan.frozen_input(item["projects"])
        if (projects != manifest.parent / "projects.yaml"
                or plan.effects.get("fleet_manifests", {}).get(name) != str(manifest)):
            raise PlanError("active configuration source locations disagree")
        fleets[name] = {"manifest": str(manifest), "fleet": content.decode(),
                        "projects": project_content.decode() if project_content is not None else None}
    snapshot = {"schema": 1, "data_root": str(installation.data_root),
                "installation": str(installation.directory), "source": str(installation.source),
                "revision": revision, "fleets": fleets,
                "units": plan.effects.get("units", []),
                "owned_outputs": [{"target": change.target, "state": change.after} for change in plan.changes]}
    # Detach mutable plan dictionaries before returning the committed document.
    snapshot = json.loads(json.dumps(snapshot))
    snapshot["fingerprint"] = _fingerprint(snapshot)
    _validate_snapshot(snapshot, installation, revision)
    return snapshot


def publish_current_config(snapshot: dict, *, installation: Installation,
                           expected_previous: str | None) -> None:
    """Atomically commit once before starting consumers; caller owns the update lock.

    Repeating an identical commit is a no-op. This writes no startup status and
    does not admit an update or prove generated files have been published.
    """
    _validate_snapshot(snapshot, installation)
    path = _snapshot_path(installation.data_root)
    content = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    _snapshot_path(installation.data_root)
    if path.exists():
        previous = read_current_config(installation.data_root, installation=installation)
        if previous["fingerprint"] == snapshot["fingerprint"]:
            return
        if previous["fingerprint"] != expected_previous:
            raise PlanError("effective configuration changed before commit")
    elif expected_previous is not None:
        raise PlanError("previous effective configuration is missing")
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_current_config(root: Path, *, installation: Installation, revision: str | None = None) -> dict:
    """Read only committed effective inputs; never repair from authored sources."""
    if Path(root) != installation.data_root:
        raise PlanError("effective configuration belongs to another data root")
    try:
        snapshot = json.loads(_snapshot_path(root).read_bytes())
        _validate_snapshot(snapshot, installation, revision)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, PlanError):
            raise
        raise PlanError("effective configuration is missing or invalid") from exc


def context_from_plan(plan: ConfigPlan, fleet: str, *, bot: str | None = None,
                      package: PackageResources) -> Context:
    """Use the shared FleetConfig parser over exact content-addressed inputs."""
    from .config import load_fleet_snapshot
    sources = plan.effects.get("fleet_sources")
    if not isinstance(sources, dict) or set(sources) != set(plan.fleets) or fleet not in sources:
        raise PlanError("active plan has no complete frozen fleet configuration")
    item = sources[fleet]
    if not isinstance(item, dict) or set(item) != {"fleet", "projects"}:
        raise PlanError("active fleet source fields are incomplete")
    manifest, content = plan.frozen_input(item["fleet"], required=True)
    projects, project_content = plan.frozen_input(item["projects"])
    if (manifest.name != "fleet.yaml" or projects != manifest.parent / "projects.yaml"
            or plan.effects.get("fleet_manifests", {}).get(fleet) != str(manifest)):
        raise PlanError("active configuration source locations disagree")
    config, defaults = load_fleet_snapshot(manifest, content, project_content)
    if config.name != fleet:
        raise PlanError("frozen configuration declares another fleet")
    if bot is not None and bot not in config.bots:
        raise BotNotFoundError(f"bot {bot!r} is not in the active fleet {fleet!r}")
    fleet_dir = None if manifest.parent == plan.data_root else manifest.parent
    if fleet_dir is not None:
        try:
            if fleet_dir.resolve() != fleet_dir:
                raise PlanError("active fleet directory is redirected")
        except (OSError, RuntimeError) as exc:
            raise PlanError("cannot resolve active fleet directory") from exc
    paths = Paths(plan.data_root, package=package, fleet_dir=fleet_dir)
    if paths.fleet_config_dir != manifest.parent:
        raise PlanError("active fleet directory is redirected")
    return Context(paths, config, defaults, bot)


def _selected_activation(root: Path):
    """Observe the matching selected journal; this grants no admission."""
    selected = read_selection(root)
    if selected is None:
        raise ActivationError("host has no active configuration")
    record = read_activation(root, selected["activation_id"])
    if (record.body["intent"]["plan_id"] != selected["plan_id"]
            or record.body["intent"]["release_id"] != selected["release_id"]):
        raise ActivationError("selected configuration activation is incomplete")
    return selected, record


def _plan_for_selection(root: Path, selected: dict, package: PackageResources) -> ConfigPlan:
    """Validate one selected package and frozen plan without authoring fallback."""
    from .releases import read_release
    release = read_release(root, selected["release_id"], verify_files=False)
    if release.native_path != package.native or release.inputs.artifact_id != package.artifact_id:
        raise ActivationError("active configuration and executing package differ")
    plan = read_plan(root, selected["plan_id"])
    if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
        raise ActivationError("active configuration and selected release differ")
    return plan


def _selected_plan(root: Path, package: PackageResources):
    """Observe sealed selection inputs; this grants no operational admission."""
    selected, record = _selected_activation(root)
    return selected, record, _plan_for_selection(root, selected, package)


def _selected_context(plan: ConfigPlan, fleet: str | None, *, bot: str | None,
                      package: PackageResources) -> Context:
    if fleet is None:
        if len(plan.fleets) != 1:
            raise PlanError("select --fleet from the active fleets")
        fleet = plan.fleets[0]
    return context_from_plan(plan, fleet, bot=bot, package=package)


def _load_legacy_active_context(root: Path, fleet: str | None, *, bot: str | None,
                                package: PackageResources) -> Context:
    """Temporary sealed-runtime reader; remove with the old activation engine."""
    selected, record = _selected_activation(root)
    if record.status != "active":
        raise ActivationError("selected configuration activation is incomplete")
    plan = _plan_for_selection(root, selected, package)
    return _selected_context(plan, fleet, bot=bot, package=package)


def load_active_context(root: Path, fleet: str | None, *, bot: str | None = None,
                        package: PackageResources | None = None) -> Context:
    """Load recorded active scope; runtime availability remains unobserved."""
    package = package if package is not None else get_resources()
    if package.installation is None:
        return _load_legacy_active_context(root, fleet, bot=bot, package=package)
    if package.source_revision is None:
        raise PlanError("executing Git source revision is unavailable")
    snapshot = read_current_config(root, installation=package.installation, revision=package.source_revision)
    if fleet is None:
        if len(snapshot["fleets"]) != 1:
            raise PlanError("select --fleet from the active fleets")
        fleet = next(iter(snapshot["fleets"]))
    if fleet not in snapshot["fleets"]:
        raise PlanError("effective configuration has no selected frozen fleet")
    return _snapshot_context(root, fleet, snapshot["fleets"][fleet], bot=bot, package=package)


def resolve_active_context(*, root: Path | None = None, fleet: str | None = None,
                           bot: str | None = None,
                           package: PackageResources | None = None) -> Context:
    """Discover the host only; fleet location comes from the activated plan."""
    paths = resolve_paths(root=root, package=package)
    return load_active_context(paths.root, fleet, bot=bot, package=paths.package)
