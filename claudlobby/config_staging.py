"""Render reviewed configuration without writing generated runtime paths.

The selected candidate renders its own code/resources. Callers supply the
host's declared fleet Paths, including external/vault declarations; activation
must reconcile this explicit coverage with the actual enrolled consumers.
Source authoring (.env scaffolding included), enrollment, registry receipts and
release selection belong to their separate owners, never to this planner.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from . import composer as compose
from .config import host_override_path, load_fleet_snapshot
from .config_plan import ConfigPlan, ConfigPlanBuilder, PlanError
from .config_units import job_units, unit_family
from .context import Context
from .paths import Paths, _iter_fleet_dirs
from .releases import ReleaseManifest, read_release
from .resources import get_resources, selected_cli
from .runtime_admission import RESIDENT_UNIT_PHASES
from .validator import validate


def _snapshot(source: Path) -> dict[str, tuple[bytes, int]]:
    """Freeze authored files; reject cycles/special files rather than hanging."""
    files = {}

    def walk(directory: Path, prefix: str, ancestors: frozenset[Path]):
        resolved = directory.resolve(strict=True)
        if resolved in ancestors:
            raise PlanError(f"cyclic runtime asset directory: {directory}")
        for child in sorted(directory.iterdir()):
            name = prefix + child.name
            if child.is_dir():
                walk(child, name + "/", ancestors | {resolved})
            elif child.is_file():
                files[name] = (child.read_bytes(), child.stat().st_mode & 0o777)
            else:
                raise PlanError(f"unreadable or special runtime asset: {child}")

    if source.is_dir():
        walk(source, "", frozenset())
    return files


def _inputs(builder: ConfigPlanBuilder, paths: Paths) -> None:
    for path in (paths.fleet_yaml, paths.projects_yaml, paths.overlay_library,
                 paths.overlay_voices, paths.overlay_templates):
        builder.input(path)
    for tier in paths.env_tiers():
        if tier.path is not None:
            builder.input(tier.path)


def _access(builder, bot, fleet) -> None:
    fresh = compose.compose_access_json(bot, fleet)
    if fresh is None:
        return
    target = Path.home() / compose.telegram_channel_rel(bot.telegram.handle) / "access.json"
    builder.input(target)  # the merge reads runtime-owned pending/access fields
    if target.exists():
        existing = json.loads(target.read_text())
        if not isinstance(existing, dict):
            raise PlanError(f"channel access is not an object: {target}")
        fresh = compose.reconcile_access_content(existing, fresh, bot, fleet)
    builder.file(target, (json.dumps(fresh, indent=2) + "\n").encode(), mode=0o600)


def _bot(builder, context, bot, delay, cascade, log) -> None:
    paths, fleet = context.paths, context.fleet
    directory = paths.bot_runtime(bot.bot_id)
    paths.assert_writable(directory)
    for relative in ("", ".claude", ".cli", ".cli/bin", "memory", "projects",
                     "data", "data/events", "logs", "mounts"):
        builder.directory(directory / relative)
    rendered = compose.render_bot_files(bot, fleet, paths, boot_delay_s=delay, cascade=cascade)
    unit_name = f"{fleet.service_prefix}.{bot.bot_id}"
    family = {name: (rendered[name].content.encode(), rendered[name].mode)
              for name in (unit_name + ".service", unit_name + ".plist")}
    builder.effects["units"].extend(unit_family(
        family, destination=directory, scope="bot", phase="bots",
        release_id=builder.release_id, fleet=fleet.name, bot=bot.bot_id))
    tools = {}
    for name, artifact in rendered.items():
        if name.startswith("tools/"):
            if artifact is not None:
                tools[name.removeprefix("tools/")] = (artifact.content.encode(), artifact.mode)
        elif artifact is None:
            builder.remove(directory / name)
        else:
            builder.file(directory / name, artifact.content.encode(), mode=artifact.mode)
    builder.tree(directory / "tools", tools)
    builder.symlink(directory / ".cli/bin/claudlobby", selected_cli())

    skills = compose.resolve_effective_skills(
        bot, fleet, paths, is_manager=bot.bot_id in fleet.manager_bots())
    sources = compose.resolve_skill_sources(paths, skills, log)
    frozen = {}
    for leaf, source in sources.items():
        builder.input(source)
        frozen.update({f"{leaf}/{name}": item for name, item in _snapshot(source).items()})
    builder.tree(directory / ".claude/skills", frozen)

    mounts = directory / "mounts"
    if mounts.exists():
        for child in mounts.iterdir():
            if child.is_symlink() and child.name not in bot.mounts:
                builder.remove(child)
    for name, target in compose.resolve_mount_sources(bot, directory, log).items():
        builder.symlink(mounts / name, target)
    _access(builder, bot, fleet)


def stage_configuration(fleet_paths: list[Paths], release: ReleaseManifest,
                        *, log=lambda message: None) -> ConfigPlan:
    """Stage all supplied fleets and shared host artifacts as one immutable plan.

    No selected-bot shortcut: permissions, hooks, skills and host equipment must
    enter through the same coordinated activation. An empty fleet selection is
    refused, never interpreted as authority to remove an estate.
    """
    if not fleet_paths:
        raise PlanError("select the declared host fleets before planning configuration")
    root = fleet_paths[0].root
    current = read_release(root, release.release_id)
    package = get_resources()
    if (current != release or selected_cli().absolute() != release.cli_path
            or package.native != release.native_path
            or package.artifact_id != release.inputs.artifact_id):
        raise PlanError("run configuration planning with the sealed candidate release CLI")
    if any(paths.root != root or paths.package != package or paths.seed for paths in fleet_paths):
        raise PlanError("configuration fleets must share one data root and candidate package")
    manifests = {paths.fleet_yaml.resolve() for paths in fleet_paths}
    if len(manifests) != len(fleet_paths):
        raise PlanError("duplicate fleet declaration in host plan")
    # Local declarations are host-owned. Vault discovery alone does not establish
    # host ownership, so those are supplied explicitly by the enrollment owner.
    declared = {directory / "fleet.yaml" for directory in _iter_fleet_dirs(root / "local")
                if (directory / "fleet.yaml").is_file()}
    if (root / "fleet.yaml").is_file():
        declared.add(root / "fleet.yaml")
    missing = {path.resolve() for path in declared} - manifests
    if missing:
        raise PlanError("host plan omits local fleet declarations: "
                        + ", ".join(map(str, sorted(missing))))
    builder = ConfigPlanBuilder(root, release.release_id, release.seal_sha256, (), effects={})
    builder.input(host_override_path())
    builder.input(Path.home() / ".gitconfig")
    contexts = []
    source_digests = {}
    for paths in sorted(fleet_paths, key=lambda item: str(item.fleet_yaml)):
        _inputs(builder, paths)
        fleet_content, fleet_digest = builder.input_snapshot(paths.fleet_yaml)
        projects_content, projects_digest = builder.input_snapshot(paths.projects_yaml)
        if fleet_content is None:
            raise PlanError(f"fleet.yaml not found at {paths.fleet_yaml}")
        fleet, defaults = load_fleet_snapshot(paths.fleet_yaml, fleet_content, projects_content)
        if paths.fleet_name is not None and fleet.name != paths.fleet_name:
            raise PlanError(f"requested fleet {paths.fleet_name!r}, but frozen {paths.fleet_yaml} "
                            f"declares {fleet.name!r}")
        context = Context(paths, fleet, defaults)
        for source in compose.manifest_inputs(context.fleet, paths).values():
            builder.input(source)
        for bot_id in context.fleet.bots:
            for tier in paths.env_tiers(bot_id):
                if tier.path is not None:
                    builder.input(tier.path)
            bot = context.fleet.bots[bot_id]
            builder.input(compose.account_settings_path(bot, context.fleet, paths))
        report = validate(context.fleet, paths)
        if report.has_errors:
            raise PlanError("invalid fleet configuration: " + "; ".join(map(str, report.errors)))
        for warning in report.warnings:
            log(str(warning))
        contexts.append(context)
        source_digests[fleet.name] = {"fleet": fleet_digest, "projects": projects_digest}
    names = [context.fleet.name for context in contexts]
    prefixes = [context.fleet.service_prefix for context in contexts]
    if len(set(names)) != len(names) or len(set(prefixes)) != len(prefixes):
        raise PlanError("fleet names and service prefixes must be unique on the selected host")
    builder.fleets = tuple(sorted(names))
    builder.effects = {
        "fleet_manifests": {c.fleet.name: str(c.paths.fleet_yaml) for c in contexts},
        "fleet_sources": {c.fleet.name: {
            "fleet": {"path": str(c.paths.fleet_yaml), "sha256": source_digests[c.fleet.name]["fleet"]},
            "projects": {"path": str(c.paths.projects_yaml), "sha256": source_digests[c.fleet.name]["projects"]},
        } for c in contexts},
        "restart_bots": [f"{c.fleet.name}/{bot}" for c in contexts for bot in c.fleet.bots],
        "reload_supervision": True,
        "coverage": "declared fleets; activation must reconcile enrolled consumers",
        "units": [],
    }
    managers_total = sum(len(c.fleet.manager_bots()) for c in contexts)
    managers_before = workers_before = 0
    with tempfile.TemporaryDirectory(prefix="claudlobby-render-") as temporary:
        scratch = Path(temporary)
        for index, context in enumerate(contexts):
            fleet, paths = context.fleet, context.paths
            cascade = compose._bot_conf_cascade(paths, fleet, None)
            bases = (managers_before, managers_total + workers_before)
            for bot in fleet.bots.values():
                delay = compose.bot_boot_delay_s(bot, fleet, paths, bases=bases)
                _bot(builder, context, bot, delay, cascade, log)
            managers_before += len(fleet.manager_bots())
            workers_before += len(fleet.bots) - len(fleet.manager_bots())
            try:
                timers = compose.compose_fleet_timers(
                    fleet, paths, context.merged_defaults, output_dir=scratch / str(index),
                    require_complete=True)
            except compose.FleetTimerCompositionError as exc:
                raise PlanError(f"cannot stage {fleet.name} fleet timers: {exc}") from exc
            paths.assert_writable(paths.runtime_fleet / "timers")
            files = _snapshot(timers)
            builder.effects["units"].extend(job_units(
                files, destination=paths.runtime_fleet / "timers", scope="fleet",
                release_id=release.release_id, fleet=fleet.name))
            builder.tree(paths.runtime_fleet / "timers", files)
            if paths.shared_docs:
                for name in ("planning/active", "planning/completed", "decisions", "knowledge", "runbooks"):
                    builder.directory(paths.assert_writable(paths.shared_docs / name))
            provenance = compose.manifest_provenance(fleet, paths)
            builder.file(paths.runtime / "composed.json",
                         (json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode())
        host = scratch / "host"
        paths = contexts[0].paths
        timers = compose.compose_host_timers(paths, output_dir=host)
        paths.assert_writable(root / "runtime/_host/timers")
        files = _snapshot(timers)
        builder.effects["units"].extend(job_units(
            files, destination=root / "runtime/_host/timers", scope="host",
            release_id=release.release_id,
            # This is the declared host ingest service, not a pattern over
            # installed unit names. It must survive until the controlled drain.
            resident_phases=RESIDENT_UNIT_PHASES))
        builder.tree(root / "runtime/_host/timers", files)
        for render in (compose.compose_host_bot_handles, compose.compose_host_mention_allowlist):
            result = render(paths, output_dir=host, manifests=sorted(manifests))
            paths.assert_writable(root / "runtime/_host" / result.name)
            builder.file(root / "runtime/_host" / result.name, result.read_bytes())
    return builder.seal()
