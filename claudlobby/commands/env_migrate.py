"""Environment migration command: extract secrets from legacy bot setups into tiered .env files."""

from __future__ import annotations

import json as _json
import logging
import re
from pathlib import Path

from .. import dotenv
from ..paths import Paths
from ._helpers import _migration_preamble

log = logging.getLogger("claudlobby")


def _discover_legacy_bot_dirs(source_dir: Path) -> dict[str, Path]:
    """Find legacy bot dirs (subdirs containing .mcp.json or bot.conf)."""
    bot_dir_map: dict[str, Path] = {}
    for child in source_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / ".mcp.json").is_file() or (child / "bot.conf").is_file():
            bot_dir_map[child.name] = child
    return bot_dir_map


def _extract_secrets(
    source_dir: Path, bot_dir_map: dict[str, Path]
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    """Gather secrets from ~/.env, per-bot bot.conf, and per-bot .mcp.json.

    Returns (env_from_home, bot_conf_secrets, mcp_values).
    """
    home_env = Path.home() / ".env"
    env_from_home = dotenv.read(home_env)

    SECRET_LIKE = re.compile(r"(TOKEN|KEY|SECRET|URL|HANDLE|CHAT_ID|PASSWORD|API)")
    bot_conf_secrets: dict[str, dict[str, str]] = {}
    for src_name, bot_path in bot_dir_map.items():
        conf_path = bot_path / "bot.conf"
        scope = {
            k: v
            for k, v in dotenv.read(conf_path).items()
            if v and not v.startswith("$") and SECRET_LIKE.search(k)
        }
        if scope:
            bot_conf_secrets[src_name] = scope

    mcp_values: dict[str, str] = {}
    for src_name, bot_path in bot_dir_map.items():
        mcp_path = bot_path / ".mcp.json"
        if not mcp_path.is_file():
            continue
        try:
            mcp_data = _json.loads(mcp_path.read_text())
        except _json.JSONDecodeError as e:
            log.warning("failed to parse %s: %s", mcp_path, e)
            continue
        for _, srv in mcp_data.get("mcpServers", {}).items():
            for k, v in srv.get("env", {}).items():
                if v and not str(v).startswith("${"):
                    mcp_values[k] = str(v)

    return env_from_home, bot_conf_secrets, mcp_values


def _resolve_fleet_vars(
    fleet: "FleetConfig",
    paths: Paths,
    env_from_home: dict[str, str],
    mcp_values: dict[str, str],
) -> dict[str, str]:
    """Determine fleet-level vars from MCP fragment placeholders.

    Resolves values from ~/.env first, then .mcp.json fallback.
    """
    needed: set[str] = set()
    seen_mcp: set[str] = set()
    for bot in fleet.bots.values():
        for mcp_entry in bot.mcp:
            if mcp_entry.name in seen_mcp:
                continue
            seen_mcp.add(mcp_entry.name)
            frag_path = paths.find_library_file("mcp", mcp_entry.name, ".json")
            if frag_path:
                for var in re.findall(
                    r"\$\{([A-Z_][A-Z0-9_]*)\}", frag_path.read_text()
                ):
                    needed.add(var)

    fleet_vars: dict[str, str] = {}
    for var in needed:
        if var in env_from_home:
            fleet_vars[var] = env_from_home[var]
        elif var in mcp_values:
            fleet_vars[var] = mcp_values[var]
    return fleet_vars


def _resolve_bot_vars(
    fleet: "FleetConfig",
    source_dir: Path,
    rename_map: dict[str, str],
    bot_dir_map: dict[str, Path],
    bot_conf_secrets: dict[str, dict[str, str]],
    env_from_home: dict[str, str],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Resolve per-bot vars (Telegram tokens etc.) scoped to source bot.conf.

    Returns (bot_vars, warnings).
    """
    bot_vars: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    for fleet_bot_name, bot in fleet.bots.items():
        if not (bot.telegram and bot.telegram.token_env):
            continue
        token_env = bot.telegram.token_env
        src_name = rename_map.get(fleet_bot_name, fleet_bot_name)
        if src_name not in bot_dir_map:
            warnings.append(
                f"  WARN: fleet bot '{fleet_bot_name}' has no source dir at "
                f"'{source_dir / src_name}' (use --map {fleet_bot_name}=<dir> if renamed)"
            )
            continue
        src_secrets = bot_conf_secrets.get(src_name, {})
        if token_env in src_secrets:
            bot_vars[fleet_bot_name] = {token_env: src_secrets[token_env]}
        elif token_env in env_from_home:
            warnings.append(
                f"  WARN: '{token_env}' for '{fleet_bot_name}' not found in "
                f"{src_name}/bot.conf — falling back to ~/.env (likely wrong "
                f"if multiple bots share the var name)"
            )
            bot_vars[fleet_bot_name] = {token_env: env_from_home[token_env]}
        else:
            warnings.append(
                f"  WARN: '{token_env}' for '{fleet_bot_name}' not found in any source"
            )
    return bot_vars, warnings


def _apply_env_migration(
    fleet: "FleetConfig",
    paths: Paths,
    source_dir: Path,
    fleet_vars: dict[str, str],
    bot_vars: dict[str, dict[str, str]],
) -> int:
    """Write fleet and per-bot .env files. Returns exit code."""
    fleet_env_path = (
        paths.fleet_dir / ".env" if paths.fleet_dir else paths.root / ".env"
    )

    if fleet_vars:
        merged = dotenv.merge_into(fleet_env_path, fleet_vars)
        fleet_env_path.parent.mkdir(parents=True, exist_ok=True)
        fleet_env_path.write_text(
            dotenv.format_file(
                f"# Fleet secrets for {fleet.name} (migrated from {source_dir})",
                merged,
            )
        )
        fleet_env_path.chmod(0o600)
        log.info(
            "wrote %s (%d migrated, %d total)",
            fleet_env_path,
            len(fleet_vars),
            len(merged),
        )

    bot_count = 0
    for fleet_bot_name, vars_dict in bot_vars.items():
        bot_env_path = paths.bot_runtime(fleet_bot_name) / ".env"
        if not bot_env_path.parent.is_dir():
            log.error(
                "SKIP %s: runtime dir missing — run `claudlobby generate` first",
                fleet_bot_name,
            )
            continue
        merged = dotenv.merge_into(bot_env_path, vars_dict)
        bot_env_path.write_text(
            dotenv.format_file(
                f"# Bot secrets for {fleet_bot_name} (migrated; pre-existing keys preserved)",
                merged,
            )
        )
        bot_env_path.chmod(0o600)
        bot_count += 1
        log.info(
            "wrote %s (%d migrated, %d total)",
            bot_env_path,
            len(vars_dict),
            len(merged),
        )

    log.info("Applied: %d fleet vars, %d bot .env files", len(fleet_vars), bot_count)
    return 0


def cmd_env_migrate(args) -> int:
    """Extract secrets from an existing bot setup into tiered .env files.

    Defaults to dry-run; pass --apply to write files. Per-bot tokens are
    scoped to their source bot.conf — no cross-wiring across bots that share
    a token_env name. On --apply, existing .env content is preserved
    (merged), so this won't wipe hand-edited keys the migrator didn't find.
    """
    paths, fleet, source_dir, rename_map = _migration_preamble(args)

    bot_dir_map = _discover_legacy_bot_dirs(source_dir)
    if not bot_dir_map:
        log.error(
            "no legacy bot dirs found under %s (looking for subdirs with .mcp.json or bot.conf)",
            source_dir,
        )
        return 1

    env_from_home, bot_conf_secrets, mcp_values = _extract_secrets(
        source_dir, bot_dir_map
    )
    fleet_vars = _resolve_fleet_vars(fleet, paths, env_from_home, mcp_values)
    bot_vars, bot_warnings = _resolve_bot_vars(
        fleet, source_dir, rename_map, bot_dir_map, bot_conf_secrets, env_from_home
    )

    # --- Plan output ---
    fleet_env_path = (
        paths.fleet_dir / ".env" if paths.fleet_dir else paths.root / ".env"
    )

    def _redact(v: str) -> str:
        if len(v) <= 8:
            return "***"
        return f"{v[:4]}…{v[-2:]}"

    log.info("=== env-migrate plan ===")
    log.info("source: %s", source_dir)
    log.info("fleet:  %s", fleet.name)
    log.info("discovered legacy bot dirs: %s", sorted(bot_dir_map.keys()))
    if rename_map:
        log.info("rename map: %s", rename_map)

    if fleet_vars:
        log.info("FLEET-LEVEL (%d vars) → %s", len(fleet_vars), fleet_env_path)
        for k in sorted(fleet_vars):
            log.info("  %s=%s", k, _redact(fleet_vars[k]))
    else:
        log.info("FLEET-LEVEL: (no fleet-shared vars to migrate)")

    if bot_vars:
        log.info("BOT-LEVEL (%d bot .env files):", len(bot_vars))
        for fleet_bot_name in sorted(bot_vars):
            bot_env_path = paths.bot_runtime(fleet_bot_name) / ".env"
            log.info("  %s → %s", fleet_bot_name, bot_env_path)
            for k in sorted(bot_vars[fleet_bot_name]):
                log.info("    %s=%s", k, _redact(bot_vars[fleet_bot_name][k]))
    else:
        log.info("BOT-LEVEL: (no per-bot tokens resolved)")

    if bot_warnings:
        for w in bot_warnings:
            log.warning("%s", w)

    if not args.apply:
        log.info("(dry-run — pass --apply to write these files)")
        return 0

    return _apply_env_migration(fleet, paths, source_dir, fleet_vars, bot_vars)
