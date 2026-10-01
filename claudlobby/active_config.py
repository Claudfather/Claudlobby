"""Operational fleet context from sealed activated inputs, never pending edits.

The caller holds mutation_admission through an operation. This reader neither
admits a mutation nor invents a live configuration for an incomplete activation.
Authoring/read-only declaration commands continue to use context.load_context.
"""

from pathlib import Path

from .activation_state import ActivationError, read_activation, read_selection
from .config_plan import ConfigPlan, PlanError, read_plan
from .context import BotNotFoundError, Context, resolve_paths
from .paths import Paths
from .resources import PackageResources, get_resources


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


def load_active_context(root: Path, fleet: str | None, *, bot: str | None = None,
                        package: PackageResources | None = None) -> Context:
    """Load recorded active scope; runtime availability remains unobserved."""
    package = package if package is not None else get_resources()
    selected = read_selection(root)
    if selected is None:
        raise ActivationError("host has no active configuration")
    record = read_activation(root, selected["activation_id"])
    if (record.status != "active" or record.body["intent"]["plan_id"] != selected["plan_id"]
            or record.body["intent"]["release_id"] != selected["release_id"]):
        raise ActivationError("selected configuration activation is incomplete")
    from .releases import read_release
    release = read_release(root, selected["release_id"], verify_files=False)
    if release.native_path != package.native or release.inputs.artifact_id != package.artifact_id:
        raise ActivationError("active configuration and executing package differ")
    plan = read_plan(root, selected["plan_id"])
    if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
        raise ActivationError("active configuration and selected release differ")
    if fleet is None:
        if len(plan.fleets) != 1:
            raise PlanError("select --fleet from the active fleets")
        fleet = plan.fleets[0]
    return context_from_plan(plan, fleet, bot=bot, package=package)


def resolve_active_context(*, root: Path | None = None, fleet: str | None = None,
                           bot: str | None = None,
                           package: PackageResources | None = None) -> Context:
    """Discover the host only; fleet location comes from the activated plan."""
    paths = resolve_paths(root=root, package=package)
    return load_active_context(paths.root, fleet, bot=bot, package=paths.package)
