"""Shared CLI helpers used by multiple command modules."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .. import dotenv
from ..config import load_fleet
from ..paths import Paths

log = logging.getLogger("claudlobby")


def _resolve_paths(args) -> Paths:
    fleet = getattr(args, "fleet", None)
    seed = getattr(args, "seed", False)
    if seed and fleet:
        log.error("--seed and --fleet are mutually exclusive")
        sys.exit(1)
    if seed:
        root = Path(args.root).resolve() if args.root else Paths.detect().root
        return Paths(root=root, seed=True)
    if args.root:
        root = Path(args.root).resolve()
        fleet_dir = (root / "local" / fleet) if fleet else None
        if fleet and fleet_dir and not fleet_dir.is_dir():
            log.error(
                "fleet overlay not found: %s — run `claudlobby new-fleet %s` to scaffold"
                " (or remove --fleet to use root mode)",
                fleet_dir,
                fleet,
            )
            sys.exit(1)
        return Paths(root=root, fleet_dir=fleet_dir)
    return Paths.detect(fleet=fleet)


def _load_env(paths: Paths) -> None:
    """Load .env file into os.environ (without overriding existing vars).
    Handles both `VAR=value` and `export VAR=value` formats — the latter is
    what env-migrate writes and what hand-edited .env files commonly use."""
    for k, v in dotenv.read(paths.env_file).items():
        if k not in os.environ:
            os.environ[k] = v


def _load_fleet_or_exit(paths: Paths) -> tuple["FleetConfig", dict]:
    """Wrap load_fleet() with user-friendly error messages on common failures.

    Returns ``(FleetConfig, merged_defaults)`` — the merged defaults dict
    produced by ``_merge_system_into_defaults()``.
    """
    import yaml

    try:
        return load_fleet(paths.fleet_yaml)
    except FileNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error("invalid YAML in %s: %s", paths.fleet_yaml, e)
        sys.exit(1)


def _validation_gate(fleet: "FleetConfig", paths: Paths, *, context: str) -> bool:
    """Run validate() and report; return True when composing may proceed.

    The shared load->validate->log gate for every command that composes
    outside `claudlobby generate` (new-bot --auto-generate, move-bot).
    Warnings are surfaced (not just errors) so did-you-mean hints reach
    the user on these paths too.
    """
    from ..validator import validate

    report = validate(fleet, paths)
    for warning in report.warnings:
        log.warning("%s", warning)
    if report.has_errors:
        for err in report.errors:
            log.error("%s", err)
        log.error("validation failed — fix the errors above, then %s", context)
        return False
    return True


def _parse_rename_map(entries: list[str]) -> dict[str, str]:
    """Parse --map entries ('fleet-bot=src-dir'). Raises ValueError on bad format."""
    rename_map: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"--map expects 'fleet-bot=src-dir', got: {entry}")
        fleet_bot, src_name = entry.split("=", 1)
        rename_map[fleet_bot.strip()] = src_name.strip()
    return rename_map


def _migration_preamble(
    args,
) -> tuple[Paths, "FleetConfig", Path, dict[str, str]]:
    """Shared preamble for migration commands.

    Calls sys.exit(1) on failure — callers can unpack the result directly.
    """
    paths = _resolve_paths(args)
    fleet, _md = _load_fleet_or_exit(paths)
    source_dir = Path(args.source).expanduser().resolve()

    if not source_dir.is_dir():
        log.error("source directory not found: %s", source_dir)
        sys.exit(1)

    try:
        rename_map = _parse_rename_map(args.map or [])
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)

    return paths, fleet, source_dir, rename_map


def _add_migration_args(parser) -> None:
    """Add the common --source, --map, --apply args shared by all migration commands."""
    parser.add_argument(
        "--source",
        required=True,
        help="Path to existing bot fleet dir (e.g. ~/my-bots)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        help="Rename a fleet bot to its legacy dir (e.g. --map clog=assistant). Repeatable.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default: dry-run preview only)",
    )
