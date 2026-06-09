"""Memory migration command: copy memory files from ~/.claude/projects/ to per-bot memory dirs."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ._helpers import _load_fleet_or_exit, _resolve_paths

log = logging.getLogger("claudlobby")


def cmd_memory_migrate(args) -> int:
    """Migrate memory files from an existing bot setup to claudlobby per-bot memory dirs."""
    paths = _resolve_paths(args)
    fleet = _load_fleet_or_exit(paths)
    claude_projects = Path.home() / ".claude" / "projects"

    if not claude_projects.is_dir():
        log.error("no ~/.claude/projects/ directory found")
        return 1

    # Build a mapping of source memory dirs → fleet bot names
    # The user provides --map pairs like "<source-pattern>:<bot-name>" or we auto-detect
    bot_map: dict[str, str] = {}
    if args.map:
        for pair in args.map:
            src, dst = pair.split(":", 1)
            bot_map[src.strip()] = dst.strip()

    # Scan ~/.claude/projects/ for memory dirs
    migrated = 0
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        memory_dir = project_dir / "memory"
        if not memory_dir.is_dir():
            continue

        # Try to match this project to a fleet bot
        dir_name = project_dir.name  # e.g. "-home-user-projects-my-bot"
        target_bot = None

        # Check explicit map first
        for src_pattern, dst_bot in bot_map.items():
            if src_pattern in dir_name:
                target_bot = dst_bot
                break

        if target_bot is None:
            # Auto-detect: check if any fleet bot name appears in the dir name
            for bot_name in fleet.bots:
                # Match on bot name or common variations
                if bot_name in dir_name.replace("-", ""):
                    target_bot = bot_name
                    break

        if target_bot is None:
            memory_files = list(memory_dir.glob("*.md"))
            if memory_files:
                log.info(
                    "  SKIP %s (%d files) — no matching bot in fleet",
                    project_dir.name,
                    len(memory_files),
                )
                log.info(
                    "        use --map '%s:<bot-name>' to map it", project_dir.name
                )
            continue

        if target_bot not in fleet.bots:
            log.error(
                "  SKIP %s — mapped to '%s' but not in fleet",
                project_dir.name,
                target_bot,
            )
            continue

        # Copy memory files to bot's memory dir
        dest_memory = paths.bot_runtime(target_bot) / "memory"
        dest_memory.mkdir(parents=True, exist_ok=True)

        file_count = 0
        for src_file in memory_dir.glob("*.md"):
            dest_file = dest_memory / src_file.name
            if dest_file.exists() and not args.force:
                log.info(
                    "  SKIP %s/%s — already exists (use --force to overwrite)",
                    target_bot,
                    src_file.name,
                )
                continue
            shutil.copy2(src_file, dest_file)
            file_count += 1

        if file_count > 0:
            log.info(
                "%s → %s: %d memory files", project_dir.name, target_bot, file_count
            )
            migrated += 1

    if migrated == 0:
        log.warning(
            "No memory files migrated. Check --map mappings or run with --force."
        )
        return 1

    log.info("Migrated memory for %d bot(s).", migrated)
    log.info("Memories are now in local/<fleet>/runtime/bots/<bot>/memory/")
    return 0
