"""Data migration command: copy bot data dirs from a legacy setup into per-bot runtime data/."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ._helpers import _migration_preamble

log = logging.getLogger("claudlobby")

# Subdir names we never copy from a legacy bot dir — claudlobby-managed,
# build artifacts, dotfile noise, runtime junk.
_DATA_MIGRATE_AUTO_SKIP_DIRS: set[str] = {
    ".git",
    ".github",
    ".gitignore",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    ".claude",
    "memory",
    "logs",
}

PlanAction = Literal[
    "copy", "skip-git", "skip-git-container", "skip-exists", "skip-empty", "skip-mount"
]


def _contains_git_checkouts(path: Path, threshold: float = 0.5) -> bool:
    """True if at least `threshold` of immediate subdirs are git checkouts.
    Catches parent-of-repos dirs (e.g. ~/projects/) so we don't recursively
    cp -r several gigabytes of code that should live elsewhere or be
    symlinked. Cheap: one shallow iterdir + N is_dir checks."""
    try:
        subdirs = [c for c in path.iterdir() if c.is_dir() and not c.is_symlink()]
    except OSError:
        return False
    if not subdirs:
        return False
    git_count = sum(1 for c in subdirs if (c / ".git").is_dir())
    return (git_count / len(subdirs)) >= threshold


@dataclass
class _DataMigratePlanItem:
    bot: str
    src: Path
    dst: Path
    action: PlanAction
    size_mb: float


def _dir_size_mb(path: Path) -> float:
    """Approximate size of a directory tree in MB. Skips symlinks."""
    total = 0
    for entry in os.scandir(path):
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                total += int(_dir_size_mb(Path(entry.path)) * (1024 * 1024))
        except OSError:
            pass
    return total / (1024 * 1024)


def _human_size(mb: float) -> str:
    if mb < 1:
        return "<1M"
    if mb < 1024:
        return f"{mb:.0f}M"
    return f"{mb / 1024:.1f}G"


def cmd_data_migrate(args) -> int:
    """Copy bot data dirs from a legacy bot setup into per-bot runtime data/.

    Defaults to dry-run; pass --apply to actually copy. Auto-skips git
    checkouts (advises symlink instead), build artifacts, and claudlobby-
    managed files. Use --include / --exclude to override the auto-discovery.
    """
    paths, fleet, source_dir, rename_map = _migration_preamble(args)

    include_set = (
        {x.strip() for x in args.include.split(",") if x.strip()}
        if args.include
        else None
    )
    exclude_set = (
        {x.strip() for x in args.exclude.split(",") if x.strip()}
        if args.exclude
        else set()
    )

    # An --include name is an explicit override that bypasses both the
    # dotfile rule and the auto-skip set. Without --include, no override.
    def _user_overrode(name: str) -> bool:
        return include_set is not None and name in include_set

    plan: list[_DataMigratePlanItem] = []
    missing_sources: list[tuple[str, Path]] = []

    for fleet_bot_name in fleet.bots:
        src_name = rename_map.get(fleet_bot_name, fleet_bot_name)
        src_bot_path = source_dir / src_name
        if not src_bot_path.is_dir():
            missing_sources.append((fleet_bot_name, src_bot_path))
            continue

        bot_data_dir = paths.bot_runtime(fleet_bot_name) / "data"
        bot_cfg = fleet.bots[fleet_bot_name]
        mount_names = set(bot_cfg.mounts.keys())
        # Resolve mount targets so we can match source dirs by real path
        mount_targets = {
            Path(t).expanduser().resolve() for t in bot_cfg.mounts.values()
        }

        for child in sorted(src_bot_path.iterdir()):
            name = child.name
            if not child.is_dir():
                continue
            if name.startswith(".") and not _user_overrode(name):
                continue
            if name in _DATA_MIGRATE_AUTO_SKIP_DIRS and not _user_overrode(name):
                continue
            if name in exclude_set:
                continue
            if include_set is not None and name not in include_set:
                continue
            # Skip dirs that are handled by mounts (by name or by resolved path)
            if name in mount_names or child.resolve() in mount_targets:
                plan.append(
                    _DataMigratePlanItem(
                        fleet_bot_name, child, bot_data_dir / name, "skip-mount", 0.0
                    )
                )
                continue

            dst = bot_data_dir / name

            # Order: cheap skip-checks BEFORE the recursive size walk. Sizing
            # 4.7G of git-checkout files only to discard the result is a real
            # cost on the Pi.
            if (child / ".git").is_dir():
                plan.append(
                    _DataMigratePlanItem(fleet_bot_name, child, dst, "skip-git", 0.0)
                )
                continue
            if _contains_git_checkouts(child):
                plan.append(
                    _DataMigratePlanItem(
                        fleet_bot_name, child, dst, "skip-git-container", 0.0
                    )
                )
                continue
            if dst.exists():
                plan.append(
                    _DataMigratePlanItem(fleet_bot_name, child, dst, "skip-exists", 0.0)
                )
                continue

            size_mb = _dir_size_mb(child)
            if size_mb == 0:
                plan.append(
                    _DataMigratePlanItem(fleet_bot_name, child, dst, "skip-empty", 0.0)
                )
                continue

            plan.append(
                _DataMigratePlanItem(fleet_bot_name, child, dst, "copy", size_mb)
            )

        # Top-level files (not inside subdirectories)
        for child in sorted(src_bot_path.iterdir()):
            name = child.name
            if child.is_dir():
                continue
            if name.startswith(".") and not _user_overrode(name):
                continue
            if name in exclude_set:
                continue
            if include_set is not None and name not in include_set:
                continue

            dst = bot_data_dir / name

            if dst.exists():
                plan.append(
                    _DataMigratePlanItem(fleet_bot_name, child, dst, "skip-exists", 0.0)
                )
                continue

            try:
                size_mb = child.stat().st_size / (1024 * 1024)
            except OSError:
                size_mb = 0.0
            if size_mb == 0:
                plan.append(
                    _DataMigratePlanItem(fleet_bot_name, child, dst, "skip-empty", 0.0)
                )
                continue

            plan.append(
                _DataMigratePlanItem(fleet_bot_name, child, dst, "copy", size_mb)
            )

    log.info("=== data-migrate plan ===")
    log.info("source: %s", source_dir)
    log.info("fleet:  %s", fleet.name)
    if rename_map:
        log.info("rename map: %s", rename_map)
    if include_set is not None:
        log.info("include filter: %s", sorted(include_set))
    if exclude_set:
        log.info("exclude filter: %s", sorted(exclude_set))

    if missing_sources:
        log.warning("Source dirs missing (use --map fleet-bot=src-dir if renamed):")
        for fb, sp in missing_sources:
            log.warning("  %s → expected %s", fb, sp)

    by_bot: dict[str, list[_DataMigratePlanItem]] = {}
    for item in plan:
        by_bot.setdefault(item.bot, []).append(item)

    _SKIP_REASON: dict[PlanAction, str] = {
        "skip-git": "git checkout — symlink from new location instead",
        "skip-git-container": "contains git checkouts — symlink/move to ~/projects/ instead",
        "skip-exists": "destination already exists",
        "skip-empty": "empty",
        "skip-mount": "handled by mounts config — symlinked, not copied",
    }

    total_to_copy_mb = 0.0
    if by_bot:
        for fb in sorted(by_bot):
            log.info("  %s:", fb)
            for item in by_bot[fb]:
                sz_str = _human_size(item.size_mb)
                rel_src = item.src.relative_to(source_dir)
                rel_dst = (
                    item.dst.relative_to(paths.root)
                    if item.dst.is_relative_to(paths.root)
                    else item.dst
                )
                suffix = "/" if item.src.is_dir() else ""
                if item.action == "copy":
                    log.info(
                        "    COPY  %6s  %s%s  →  %s%s",
                        sz_str,
                        rel_src,
                        suffix,
                        rel_dst,
                        suffix,
                    )
                    total_to_copy_mb += item.size_mb
                else:
                    reason = _SKIP_REASON[item.action]
                    log.info(
                        "    SKIP  %6s  %s%s  (%s)", sz_str, rel_src, suffix, reason
                    )
        log.info("Total to copy: %s", _human_size(total_to_copy_mb))
    else:
        log.info(
            "(no data dirs found to migrate — try --include <name> if subdirs were auto-skipped)"
        )

    if not args.apply:
        log.info("(dry-run — pass --apply to copy)")
        return 0

    copied = 0
    for item in plan:
        if item.action != "copy":
            continue
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if item.src.is_file():
                shutil.copy2(item.src, item.dst)
            else:
                shutil.copytree(item.src, item.dst, symlinks=True)
            log.info("copied  %s  →  %s", item.src, item.dst)
            copied += 1
        except (OSError, shutil.Error) as e:
            log.error("FAILED  %s: %s", item.src, e)

    log.info("Applied: %d item(s) copied", copied)
    return 0
