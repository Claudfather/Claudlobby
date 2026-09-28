"""Resolve package, data and requested fleet/bot scope through existing owners."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .paths import Paths
from .resources import PackageResources, get_resources, selected_cli

if TYPE_CHECKING:
    from .config import FleetConfig


class BotNotFoundError(ValueError):
    """The selected fleet has no declaration for the requested bot."""


@dataclass(frozen=True)
class Context:
    paths: Paths
    fleet: FleetConfig
    merged_defaults: dict
    bot_id: str | None = None


def generated_selectors(*, fleet: str | None = None, bot: str | None = None,
                        include_bot: bool = False, seed: bool = False) -> tuple[str | None, str | None]:
    """Explicit selectors precede generated session defaults for scoped reads.

    Host-wide preparation deliberately does not call this helper. A present
    empty generated selector refuses; it must not quietly select the root fleet
    or another identity. BOT_ID is authoritative when present, including when
    malformed; BOT_NAME serves only older generated contexts that lack BOT_ID.
    The existing path resolver and config loader validate the selected names.
    """
    def first(*names):
        for name in names:
            if name in os.environ:
                value = os.environ[name]
                if not value.strip() or Path(value).name != value or value in (".", ".."):
                    raise ValueError(f"invalid generated selector {name}; supply an explicit selector")
                return value
        return None

    if not seed:
        if fleet is None:
            fleet = first("FLEET_NAME", "CLAUDLOBBY_FLEET")
        if include_bot and bot is None:
            bot = first("BOT_ID", "BOT_NAME")
    return fleet, bot


def native_environment(paths: Paths) -> dict[str, str]:
    """Resolved locations passed to native children and generated supervision.

    Native hot paths consume this context without starting Python or guessing
    mutable storage from their installed script directory.
    """
    cli = selected_cli()
    result = {
        "CLAUDLOBBY_ROOT": str(paths.root),
        "FLEET_ROOT": str(paths.fleet_config_dir),
        "CLAUDLOBBY_NATIVE_DIR": str(paths.lib),
        "CLAUDLOBBY_LIBRARY_DIR": str(paths.base_library),
        "CLAUDLOBBY_CLI": str(cli),
        "CLAUDLOBBY_ARTIFACT_ID": paths.package.artifact_id,
    }
    # Composition can run from an explicit development package, but a sealed
    # candidate binds generated callers to its complete release (dependencies
    # included), not merely the source/resource artifact identity.
    if cli.is_relative_to(paths.release_store):
        from .releases import read_release
        release_id = cli.relative_to(paths.release_store).parts[0]
        release = read_release(paths.root, release_id, verify_files=False)
        if (release.cli_path != cli or release.native_path != paths.lib
                or release.inputs.artifact_id != paths.package.artifact_id):
            raise ValueError("selected CLI and package do not belong to the same sealed release")
        result["CLAUDLOBBY_RELEASE_ID"] = release_id
    return result


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
        raise BotNotFoundError(f"bot {bot!r} is not declared in fleet {config.name!r}")
    return Context(paths, config, merged_defaults, bot)


def resolve_context(
    *, root: Path | None = None, fleet: str | None = None, bot: str | None = None,
    seed: bool = False, package: PackageResources | None = None,
) -> Context:
    """Resolve and validate one requested fleet/bot scope."""
    paths = resolve_paths(root=root, fleet=fleet, seed=seed, package=package)
    return load_context(paths, fleet=fleet, bot=bot)
