"""Resolve package, data and requested fleet/bot scope through existing owners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import Paths
from .resources import PackageResources, get_resources, selected_cli

if TYPE_CHECKING:
    from .config import FleetConfig


@dataclass(frozen=True)
class Context:
    paths: Paths
    fleet: FleetConfig
    merged_defaults: dict
    bot_id: str | None = None


def native_environment(paths: Paths) -> dict[str, str]:
    """Resolved locations passed to native children and generated supervision.

    Native hot paths consume this context without starting Python or guessing
    mutable storage from their installed script directory.
    """
    return {
        "CLAUDLOBBY_ROOT": str(paths.root),
        "FLEET_ROOT": str(paths.fleet_config_dir),
        "CLAUDLOBBY_NATIVE_DIR": str(paths.lib),
        "CLAUDLOBBY_LIBRARY_DIR": str(paths.base_library),
        "CLAUDLOBBY_CLI": str(selected_cli()),
        "CLAUDLOBBY_ARTIFACT_ID": paths.package.artifact_id,
    }


def resolve_paths(
    *, root: Path | None = None, fleet: str | None = None, seed: bool = False,
    package: PackageResources | None = None,
) -> Paths:
    """Resolve host/fleet paths without requiring a fleet for host commands.

    Production always reads the installed package. Explicit injection is for
    callers such as isolated test/source harnesses, never a discovery fallback.
    """
    return Paths.detect(root, fleet, seed=seed,
                        package=package if package is not None else get_resources())


def load_context(
    paths: Paths, *, fleet: str | None = None, bot: str | None = None,
) -> Context:
    """Load with the config owner and refuse a requested identity mismatch."""
    from .config import load_fleet

    config, merged_defaults = load_fleet(paths.fleet_yaml, projects_yaml=paths.projects_yaml)
    requested = fleet if fleet is not None else paths.fleet_name
    if requested is not None and config.name != requested:
        raise ValueError(
            f"requested fleet {requested!r}, but {paths.fleet_yaml} declares "
            f"{config.name!r}; select the correct --root and --fleet"
        )
    if bot is not None and bot not in config.bots:
        raise ValueError(f"bot {bot!r} is not declared in fleet {config.name!r}")
    return Context(paths, config, merged_defaults, bot)


def resolve_context(
    *, root: Path | None = None, fleet: str | None = None, bot: str | None = None,
    seed: bool = False, package: PackageResources | None = None,
) -> Context:
    """Resolve and validate one requested fleet/bot scope."""
    paths = resolve_paths(root=root, fleet=fleet, seed=seed, package=package)
    return load_context(paths, fleet=fleet, bot=bot)
