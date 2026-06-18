"""Move-bot command: move a bot between fleets — copy state, re-enroll service."""

from __future__ import annotations

import json as _json
import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from ..composer import compose_bot
from ..paths import Paths, tmux_socket_for_bot
from ..validator import validate
from ._helpers import _load_env, _load_fleet_or_exit

log = logging.getLogger("claudlobby")


def cmd_move_bot(args) -> int:
    """Move a bot between fleets — copies state, re-enrolls service."""
    target_fleet_name: str = args.to
    bot_name: str = args.bot
    apply: bool = args.apply
    cleanup: bool = args.cleanup_source
    force: bool = args.force

    # --- Resolve claudlobby root ---
    root = Path(args.root).resolve() if args.root else Paths.detect().root
    local_dir = root / "local"
    if not local_dir.is_dir():
        log.error("no local/ directory at %s — nothing to scan", root)
        return 1

    # --- Auto-detect source fleet ---
    source_fleet_name = getattr(args, "from_fleet", None)
    if source_fleet_name:
        src_bot_dir = local_dir / source_fleet_name / "runtime" / "bots" / bot_name
        if not src_bot_dir.is_dir():
            log.error(
                "bot '%s' not found in fleet '%s' at %s",
                bot_name,
                source_fleet_name,
                src_bot_dir,
            )
            return 1
    else:
        candidates = []
        for fleet_dir in sorted(local_dir.iterdir()):
            if not fleet_dir.is_dir():
                continue
            bot_dir = fleet_dir / "runtime" / "bots" / bot_name
            if bot_dir.is_dir() and (bot_dir / "bot.conf").is_file():
                candidates.append((fleet_dir.name, bot_dir))
        if not candidates:
            log.error("bot '%s' not found in any fleet under %s", bot_name, local_dir)
            return 1
        if len(candidates) > 1:
            fleets = ", ".join(c[0] for c in candidates)
            log.error(
                "bot '%s' exists in multiple fleets (%s) — use --from to disambiguate",
                bot_name,
                fleets,
            )
            return 1
        source_fleet_name, src_bot_dir = candidates[0]

    if source_fleet_name == target_fleet_name:
        log.error("source and target fleet are the same (%s)", source_fleet_name)
        return 1

    # --- Verify target fleet has the bot stanza ---
    target_fleet_dir = local_dir / target_fleet_name
    if not target_fleet_dir.is_dir():
        log.error(
            "target fleet '%s' not found at %s", target_fleet_name, target_fleet_dir
        )
        return 1
    target_fleet_yaml = target_fleet_dir / "fleet.yaml"
    if not target_fleet_yaml.is_file():
        log.error("no fleet.yaml in target fleet '%s'", target_fleet_name)
        return 1
    target_fleet, _md = _load_fleet_or_exit(
        Paths(root=root, fleet_dir=target_fleet_dir)
    )
    if bot_name not in target_fleet.bots:
        log.error(
            "bot '%s' not in target fleet.yaml — add the stanza first, then retry",
            bot_name,
        )
        return 1

    # --- Pre-flight: check for WIP ---
    projects_dir = src_bot_dir / "projects"
    if projects_dir.is_dir():
        for repo in sorted(projects_dir.iterdir()):
            if not (repo / ".git").is_dir():
                continue
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip():
                log.error(
                    "bot '%s' has uncommitted WIP in %s — commit or stash first",
                    bot_name,
                    repo,
                )
                return 1

    # --- Pre-flight: check tmux session activity ---
    if apply and not force:
        # The bot runs on its own tmux server (-L <socket>); resolve it from the
        # source bot.conf, falling back to the default socket for an
        # un-regenerated bot that predates per-bot sockets.
        socket = tmux_socket_for_bot(src_bot_dir)
        _tmux = ["tmux", "-L", socket] if socket else ["tmux"]
        tmux_check = subprocess.run(
            [*_tmux, "has-session", "-t", bot_name],
            capture_output=True,
        )
        if tmux_check.returncode == 0:
            # Session exists — capture what it's doing
            pane_content = subprocess.run(
                [*_tmux, "capture-pane", "-t", bot_name, "-p", "-l", "5"],
                capture_output=True,
                text=True,
            )
            session_info = (
                pane_content.stdout.strip()
                if pane_content.returncode == 0
                else "(could not read pane)"
            )
            log.warning(
                "bot '%s' has an active tmux session — stopping it "
                "mid-task will lose in-flight context",
                bot_name,
            )
            print(f"\n  ⚠ Active tmux session for '{bot_name}':")
            for line in session_info.splitlines()[-5:]:
                print(f"    {line}")
            print("\n  Pass --force to override.\n")
            return 1

    # --- Determine service file paths ---
    is_linux = platform.system() == "Linux"
    is_macos = platform.system() == "Darwin"
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    launchd_dir = Path.home() / "Library" / "LaunchAgents"

    # Read source service_prefix from bot.conf
    src_bot_service_label = None
    for line in (src_bot_dir / "bot.conf").read_text().splitlines():
        if line.startswith("BOT_SERVICE="):
            src_bot_service_label = line.split("=", 1)[1].strip().strip("'\"")
            break
    if not src_bot_service_label:
        src_bot_service_label = f"claudlobby.{bot_name}"

    if is_linux:
        old_unit = systemd_dir / f"{bot_name}.service"
    elif is_macos:
        old_unit = launchd_dir / f"{src_bot_service_label}.plist"
    else:
        old_unit = None

    # --- Collect what will be moved ---
    src_env = src_bot_dir / ".env"
    src_memory = src_bot_dir / "memory"
    target_bot_dir = target_fleet_dir / "runtime" / "bots" / bot_name

    # Telegram access.json
    tg_handle = bot_name
    for line in (src_bot_dir / "bot.conf").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("TELEGRAM_STATE_DIR="):
            # Extract handle from path like $HOME/.claude/channels/telegram-<handle>
            m = re.search(r"telegram-([a-zA-Z0-9_-]+)", stripped)
            if m:
                tg_handle = m.group(1)
            break
    access_json_path = (
        Path.home() / ".claude" / "channels" / f"telegram-{tg_handle}" / "access.json"
    )

    # --- Print plan ---
    print(f"\n=== Move bot '{bot_name}' ===\n")
    print(f"  Source fleet:  {source_fleet_name}")
    print(f"  Target fleet:  {target_fleet_name}")
    print(f"  Source dir:    {src_bot_dir}")
    print(f"  Target dir:    {target_bot_dir}")
    print()

    steps = []
    if src_env.is_file():
        steps.append(f"Copy .env secrets → {target_bot_dir / '.env'}")
    if src_memory.is_dir() and any(src_memory.iterdir()):
        steps.append(
            f"Copy memory/ ({sum(1 for _ in src_memory.rglob('*') if _.is_file())} files)"
        )
    if old_unit and old_unit.is_file():
        steps.append(f"Stop + disable service ({old_unit.name})")
        steps.append(f"Delete old service file: {old_unit}")
    steps.append(f"Regenerate target bot (claudlobby generate --bot {bot_name})")
    if access_json_path.is_file() and target_fleet.telegram_group_chat_id:
        steps.append(
            f"Update access.json group chat → {target_fleet.telegram_group_chat_id}"
        )
    steps.append(f"Re-enroll via spin-up-bot.sh from {target_bot_dir}")
    if cleanup:
        steps.append(f"Remove source bot dir: {src_bot_dir}")

    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    print()

    if not apply:
        print("Dry run — pass --apply to execute.\n")
        return 0

    # --- Pre-apply validation (before any mutation) ---
    target_paths = Paths(root=root, fleet_dir=target_fleet_dir)
    _load_env(target_paths)
    report = validate(target_fleet, target_paths)
    if report.has_errors:
        log.error("target fleet has validation errors — aborting before any changes")
        for e in report.errors:
            log.error("  %s", e)
        return 1

    # --- Execute ---

    # 1. Copy .env
    if src_env.is_file():
        target_bot_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_env, target_bot_dir / ".env")
        # Preserve restrictive permissions
        (target_bot_dir / ".env").chmod(0o600)
        log.info("copied .env → %s", target_bot_dir / ".env")

    # 2. Copy memory/ (temp-dir-then-rename for rollback safety)
    if src_memory.is_dir() and any(src_memory.iterdir()):
        target_bot_dir.mkdir(parents=True, exist_ok=True)
        target_memory = target_bot_dir / "memory"
        tmp_memory = target_bot_dir / ".memory_tmp"
        if tmp_memory.exists():
            shutil.rmtree(tmp_memory)
        shutil.copytree(src_memory, tmp_memory)
        if target_memory.exists():
            shutil.rmtree(target_memory)
        tmp_memory.rename(target_memory)
        log.info(
            "copied memory/ (%d files)",
            sum(1 for _ in target_memory.rglob("*") if _.is_file()),
        )

    # 3. Stop + disable old service
    if old_unit and old_unit.is_file():
        if is_linux:
            for cmd in [
                ["systemctl", "--user", "stop", f"{bot_name}.service"],
                ["systemctl", "--user", "disable", f"{bot_name}.service"],
            ]:
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    log.warning(
                        "%s exited %d: %s",
                        " ".join(cmd),
                        r.returncode,
                        r.stderr.strip(),
                    )
            old_unit.unlink()
            r = subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                log.warning(
                    "daemon-reload exited %d: %s", r.returncode, r.stderr.strip()
                )
            log.info("stopped + removed systemd unit %s", old_unit.name)
        elif is_macos:
            r = subprocess.run(
                [
                    "launchctl",
                    "bootout",
                    f"gui/{os.getuid()}/{src_bot_service_label}",
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                log.warning(
                    "launchctl bootout exited %d: %s", r.returncode, r.stderr.strip()
                )
            old_unit.unlink()
            log.info("stopped + removed launchd plist %s", old_unit.name)

    # 4. Regenerate target bot (validation already passed above)
    compose_bot(target_fleet.bots[bot_name], target_fleet, target_paths)
    log.info("regenerated bot → %s", target_bot_dir)

    # 5. Update access.json group chat
    if access_json_path.is_file() and target_fleet.telegram_group_chat_id:
        try:
            access = _json.loads(access_json_path.read_text())
            old_groups = access.get("groups", {})
            # Replace all group entries with the target fleet's group
            new_groups = {
                target_fleet.telegram_group_chat_id: {
                    "requireMention": True,
                    "allowFrom": [],
                }
            }
            # Preserve allowFrom from old group if there was exactly one
            if len(old_groups) == 1:
                old_entry = next(iter(old_groups.values()))
                if isinstance(old_entry, dict):
                    new_groups[target_fleet.telegram_group_chat_id][
                        "requireMention"
                    ] = old_entry.get("requireMention", True)
                    new_groups[target_fleet.telegram_group_chat_id]["allowFrom"] = (
                        old_entry.get("allowFrom", [])
                    )
            access["groups"] = new_groups
            access_json_path.write_text(_json.dumps(access, indent=2) + "\n")
            log.info(
                "updated access.json group → %s", target_fleet.telegram_group_chat_id
            )
        except (_json.JSONDecodeError, OSError) as e:
            log.error("could not update access.json: %s", e)
            print(
                "\nINCOMPLETE MIGRATION: access.json update failed."
                "\nThe bot will boot but Telegram routing "
                "may point to the wrong group."
                f"\nFix manually: {access_json_path}\n"
            )
            return 1

    # 6. Re-enroll via spin-up-bot
    spin_up = root / "lib" / "spin-up-bot.sh"
    if spin_up.is_file():
        result = subprocess.run(
            [str(spin_up), str(target_bot_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("re-enrolled via spin-up-bot.sh")
        else:
            log.error(
                "spin-up-bot.sh exited %d — enroll manually:\n  %s %s",
                result.returncode,
                spin_up,
                target_bot_dir,
            )
            if result.stderr.strip():
                log.error("  stderr: %s", result.stderr.strip())
            print(
                f"\nINCOMPLETE MIGRATION: bot '{bot_name}' moved "
                "but enrollment failed."
                f"\nRun manually:  {spin_up} {target_bot_dir}\n"
            )
            return 1
    else:
        log.error("spin-up-bot.sh not found at %s — enroll manually", spin_up)
        print(
            f"\nINCOMPLETE MIGRATION: bot '{bot_name}' moved "
            "but not enrolled."
            f"\nExpected: {spin_up}\n"
        )
        return 1

    # 7. Cleanup source
    if cleanup:
        shutil.rmtree(src_bot_dir)
        log.info("removed source bot dir: %s", src_bot_dir)

    print(
        f"\nDone. Bot '{bot_name}' moved from '{source_fleet_name}' → '{target_fleet_name}'.\n"
    )
    return 0
