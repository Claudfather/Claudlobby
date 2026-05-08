"""claudlobby CLI entry point."""
from __future__ import annotations
import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import __version__
from . import dotenv
from .config import load_fleet
from .composer import compose_bot, compose_fleet
from .diff import diff_bot, promote_bot
from .paths import Paths
from .validator import validate


def _parse_rename_map(entries: list[str]) -> dict[str, str]:
    """Parse --map entries ('fleet-bot=src-dir'). Raises ValueError on bad format."""
    rename_map: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"--map expects 'fleet-bot=src-dir', got: {entry}")
        fleet_bot, src_name = entry.split("=", 1)
        rename_map[fleet_bot.strip()] = src_name.strip()
    return rename_map


def _parse_env_line(line: str) -> tuple[str, str] | None:
    """Parse a single env line ('VAR=value' or 'export VAR=value').
    Returns (key, value) or None for blank/comment/invalid lines."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    k = k.strip()
    if k.startswith("export "):
        k = k[len("export "):].strip()
    v = v.strip().strip('"').strip("'")
    return (k, v)


def _resolve_paths(args) -> Paths:
    fleet = getattr(args, "fleet", None)
    if args.root:
        root = Path(args.root).resolve()
        fleet_dir = (root / "local" / fleet) if fleet else None
        if fleet and fleet_dir and not fleet_dir.is_dir():
            print(f"ERROR: fleet overlay not found: {fleet_dir}", file=sys.stderr)
            print(f"       Run `claudlobby new-fleet {fleet}` to scaffold (or remove --fleet to use root mode).", file=sys.stderr)
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


def cmd_validate(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = load_fleet(paths.fleet_yaml)
    report = validate(fleet, paths)

    if report.has_errors:
        print(f"errors ({len(report.errors)}):", file=sys.stderr)
        for e in report.errors:
            print(f"  ERROR: {e}", file=sys.stderr)
    if report.warnings:
        print(f"warnings ({len(report.warnings)}):")
        for w in report.warnings:
            print(f"  WARN: {w}")

    if args.strict and report.has_issues:
        print("--strict: warnings count as errors → fail", file=sys.stderr)
        return 1
    if report.has_errors:
        return 1
    if not report.has_issues:
        print(f"fleet.yaml OK ({len(fleet.bots)} bots, {len(fleet.teams)} teams)")
    return 0


def cmd_generate(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = load_fleet(paths.fleet_yaml)
    report = validate(fleet, paths)

    if report.has_errors:
        print(f"validation errors ({len(report.errors)}) — refusing to generate:", file=sys.stderr)
        for e in report.errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1
    if args.strict and report.warnings:
        print("--strict: warnings count as errors → refusing to generate", file=sys.stderr)
        for w in report.warnings:
            print(f"  WARN: {w}", file=sys.stderr)
        return 1
    if report.warnings:
        for w in report.warnings:
            print(f"  WARN: {w}", file=sys.stderr)

    if args.bot:
        bot = fleet.bots.get(args.bot)
        if not bot:
            print(f"bot '{args.bot}' not in fleet.yaml", file=sys.stderr)
            return 1
        out = compose_bot(bot, fleet, paths)
        print(f"composed {args.bot} → {out}")
    else:
        out = compose_fleet(fleet, paths)
        print(f"composed {len(out)} bots → {paths.runtime_bots}")
    return 0


def cmd_list_library(args) -> int:
    paths = _resolve_paths(args)

    def _list_md(label: str, kind: str):
        """Walk overlay → base recursively. Display nested files as `dir/name`."""
        print(f"\n{label}:")
        seen: dict[str, str] = {}  # rel_key (no .md) → "[overlay]" or "[base]"
        for d in paths.library_search_dirs(kind):
            if not d.is_dir():
                continue
            tag = "[overlay]" if (paths.overlay_library and d == paths.overlay_library / kind) else "[base]"
            for p in sorted(d.rglob("*.md")):
                if p.stem.lower().startswith("readme"):
                    continue
                rel_key = str(p.relative_to(d).with_suffix(""))
                if rel_key not in seen:
                    seen[rel_key] = tag
        for rel_key, tag in sorted(seen.items()):
            marker = " (override)" if tag == "[overlay]" else ""
            print(f"  {rel_key}{marker}")

    _list_md("Expertise", "expertise")

    print("\nMCP fragments (base only):")
    if paths.mcp.is_dir():
        for p in sorted(paths.mcp.glob("*.json")):
            print(f"  {p.stem}")

    _list_md("Integrations", "integrations")
    _list_md("Protocols", "protocols")
    _list_md("Guardrails", "guardrails")
    _list_md("Resources", "resources")
    _list_md("Lessons", "lessons")
    _list_md("Post-actions", "post_actions")

    print("\nSkills:")
    seen_skills: dict[str, str] = {}  # rel_key → tag
    for d in paths.library_search_dirs("skills"):
        if not d.is_dir():
            continue
        tag = "[overlay]" if (paths.overlay_library and d == paths.overlay_library / "skills") else "[base]"
        for sub in sorted(d.rglob("*")):
            if not sub.is_dir():
                continue
            if not (sub / "SKILL.md").is_file():
                continue
            rel_key = str(sub.relative_to(d))
            if rel_key not in seen_skills:
                seen_skills[rel_key] = tag
    for rel_key, tag in sorted(seen_skills.items()):
        marker = " (override)" if tag == "[overlay]" else ""
        print(f"  {rel_key}{marker}")

    print("\nVoices:")
    seen_voices: dict[str, Path] = {}
    if paths.overlay_voices and paths.overlay_voices.is_dir():
        for p in sorted(paths.overlay_voices.rglob("*.md")):
            seen_voices[p.name] = p
    if paths.base_voices.is_dir():
        for p in sorted(paths.base_voices.rglob("*.md")):
            seen_voices.setdefault(p.name, p)
    for name in sorted(seen_voices):
        p = seen_voices[name]
        try:
            tag = " (override)" if (paths.overlay_voices and p.is_relative_to(paths.overlay_voices)) else ""
        except ValueError:
            tag = ""
        print(f"  {p.relative_to(paths.root)}{tag}")

    if paths.fleet_dir:
        print(f"\n[fleet overlay: {paths.fleet_dir.relative_to(paths.root)}]")
    else:
        print(f"\n[no fleet overlay — root mode. Use --fleet <name> for overlay mode.]")
    return 0


def cmd_diff(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = load_fleet(paths.fleet_yaml)
    if args.bot:
        sys.stdout.write(diff_bot(args.bot, fleet, paths))
    else:
        for name in fleet.bots:
            sys.stdout.write(diff_bot(name, fleet, paths))
    return 0


def cmd_promote(args) -> int:
    paths = _resolve_paths(args)
    fleet = load_fleet(paths.fleet_yaml)
    sys.stdout.write(promote_bot(args.bot, fleet, paths))
    return 0


def cmd_status(args) -> int:
    """Stub: placeholder for fleet health dashboard."""
    paths = _resolve_paths(args)
    print(f"claudlobby status — placeholder")
    print(f"  root:    {paths.root}")
    print(f"  runtime: {paths.runtime_bots}")
    print("  (full status dashboard wiring tmux + service health: TODO)")
    return 0


def cmd_env_migrate(args) -> int:
    """Extract secrets from an existing bot setup into tiered .env files.

    Defaults to dry-run; pass --apply to write files. Per-bot tokens are
    scoped to their source bot.conf — no cross-wiring across bots that share
    a token_env name. On --apply, existing .env content is preserved
    (merged), so this won't wipe hand-edited keys the migrator didn't find.
    """
    import re
    import json as _json

    paths = _resolve_paths(args)
    fleet = load_fleet(paths.fleet_yaml)
    source_dir = Path(args.source).expanduser().resolve()

    if not source_dir.is_dir():
        print(f"ERROR: source directory not found: {source_dir}", file=sys.stderr)
        return 1

    # --- Parse --map flags: fleet_bot=src_dir (e.g. clog=assistant) ---
    try:
        rename_map = _parse_rename_map(args.map or [])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # --- Discover legacy bot dirs (any subdir containing .mcp.json or bot.conf) ---
    bot_dir_map: dict[str, Path] = {}
    for child in source_dir.iterdir():
        if not child.is_dir():
            continue
        if (child / ".mcp.json").is_file() or (child / "bot.conf").is_file():
            bot_dir_map[child.name] = child

    if not bot_dir_map:
        print(f"ERROR: no legacy bot dirs found under {source_dir} (looking for subdirs with .mcp.json or bot.conf)", file=sys.stderr)
        return 1

    # --- Source 1: ~/.env (host shell namespace; used as fallback) ---
    home_env = Path.home() / ".env"
    env_from_home: dict[str, str] = {}
    if home_env.is_file():
        for line in home_env.read_text().splitlines():
            parsed = _parse_env_line(line)
            if parsed:
                env_from_home[parsed[0]] = parsed[1]

    # --- Source 2: per-bot bot.conf (Telegram + other secrets, scoped per source dir) ---
    SECRET_LIKE = re.compile(r"(TOKEN|KEY|SECRET|URL|HANDLE|CHAT_ID|PASSWORD|API)")
    bot_conf_secrets: dict[str, dict[str, str]] = {}  # src_dir_name → {var → value}
    for src_name, bot_path in bot_dir_map.items():
        conf_path = bot_path / "bot.conf"
        if not conf_path.is_file():
            continue
        scope: dict[str, str] = {}
        for line in conf_path.read_text().splitlines():
            parsed = _parse_env_line(line)
            if not parsed:
                continue
            k, v = parsed
            if not v or v.startswith("$"):
                continue
            if SECRET_LIKE.search(k):
                scope[k] = v
        if scope:
            bot_conf_secrets[src_name] = scope

    # --- Source 3: per-bot .mcp.json env values (resolved tokens leak through here) ---
    mcp_values: dict[str, str] = {}
    for src_name, bot_path in bot_dir_map.items():
        mcp_path = bot_path / ".mcp.json"
        if not mcp_path.is_file():
            continue
        try:
            mcp_data = _json.loads(mcp_path.read_text())
        except _json.JSONDecodeError as e:
            print(f"WARN: failed to parse {mcp_path}: {e}", file=sys.stderr)
            continue
        for _, srv in mcp_data.get("mcpServers", {}).items():
            for k, v in srv.get("env", {}).items():
                if v and not str(v).startswith("${"):
                    mcp_values[k] = str(v)

    # --- Determine fleet-level vars needed (scan all referenced MCP fragments) ---
    needed_fleet_vars: set[str] = set()
    seen_mcp: set[str] = set()
    for bot in fleet.bots.values():
        for mcp_entry in bot.mcp:
            mcp_name = mcp_entry.name  # post-#19 multi-instance: McpEntry has .name
            if mcp_name in seen_mcp:
                continue
            seen_mcp.add(mcp_name)
            frag_path = paths.find_library_file("mcp", mcp_name, ".json")
            if frag_path:
                for var in re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", frag_path.read_text()):
                    needed_fleet_vars.add(var)

    # Resolve fleet vars: prefer ~/.env, fall back to mcp values if home didn't have it
    fleet_vars: dict[str, str] = {}
    for var in needed_fleet_vars:
        if var in env_from_home:
            fleet_vars[var] = env_from_home[var]
        elif var in mcp_values:
            fleet_vars[var] = mcp_values[var]

    # --- Resolve per-bot vars (Telegram tokens etc.) — scoped to source bot.conf ---
    bot_vars: dict[str, dict[str, str]] = {}
    bot_warnings: list[str] = []
    for fleet_bot_name, bot in fleet.bots.items():
        if not (bot.telegram and bot.telegram.token_env):
            continue
        token_env = bot.telegram.token_env
        # Find legacy source dir: --map override, then identity match
        src_name = rename_map.get(fleet_bot_name, fleet_bot_name)
        if src_name not in bot_dir_map:
            bot_warnings.append(
                f"  WARN: fleet bot '{fleet_bot_name}' has no source dir at "
                f"'{source_dir / src_name}' (use --map {fleet_bot_name}=<dir> if renamed)"
            )
            continue
        src_secrets = bot_conf_secrets.get(src_name, {})
        if token_env in src_secrets:
            bot_vars[fleet_bot_name] = {token_env: src_secrets[token_env]}
        elif token_env in env_from_home:
            bot_warnings.append(
                f"  WARN: '{token_env}' for '{fleet_bot_name}' not found in "
                f"{src_name}/bot.conf — falling back to ~/.env (likely wrong "
                f"if multiple bots share the var name)"
            )
            bot_vars[fleet_bot_name] = {token_env: env_from_home[token_env]}
        else:
            bot_warnings.append(
                f"  WARN: '{token_env}' for '{fleet_bot_name}' not found in any source"
            )

    # --- Plan output (always print; --apply to commit) ---
    fleet_env_path = paths.fleet_dir / ".env" if paths.fleet_dir else paths.root / ".env"

    def _redact(v: str) -> str:
        if len(v) <= 8:
            return "***"
        return f"{v[:4]}…{v[-2:]}"

    print(f"\n=== env-migrate plan ===")
    print(f"source: {source_dir}")
    print(f"fleet:  {fleet.name}")
    print(f"discovered legacy bot dirs: {sorted(bot_dir_map.keys())}")
    if rename_map:
        print(f"rename map: {rename_map}")
    print()

    if fleet_vars:
        print(f"FLEET-LEVEL ({len(fleet_vars)} vars) → {fleet_env_path}")
        for k in sorted(fleet_vars):
            print(f"  {k}={_redact(fleet_vars[k])}")
    else:
        print("FLEET-LEVEL: (no fleet-shared vars to migrate)")
    print()

    if bot_vars:
        print(f"BOT-LEVEL ({len(bot_vars)} bot .env files):")
        for fleet_bot_name in sorted(bot_vars):
            bot_env_path = paths.bot_runtime(fleet_bot_name) / ".env"
            print(f"  {fleet_bot_name} → {bot_env_path}")
            for k in sorted(bot_vars[fleet_bot_name]):
                print(f"    {k}={_redact(bot_vars[fleet_bot_name][k])}")
    else:
        print("BOT-LEVEL: (no per-bot tokens resolved)")
    print()

    if bot_warnings:
        print("Warnings:")
        for w in bot_warnings:
            print(w)
        print()

    if not args.apply:
        print("(dry-run — pass --apply to write these files)")
        return 0

    # --- Apply ---
    # Merge with any existing .env content rather than overwrite. Without
    # this, a bot's existing hand-edited .env (e.g. SLACK_TOKEN that the
    # migration didn't discover) gets wiped. New values win on conflict.
    if fleet_vars:
        merged = dotenv.merge_into(fleet_env_path, fleet_vars)
        fleet_env_path.parent.mkdir(parents=True, exist_ok=True)
        fleet_env_path.write_text(dotenv.format_file(
            f"# Fleet secrets for {fleet.name} (migrated from {source_dir})",
            merged,
        ))
        new_keys = sorted(set(fleet_vars) - (set(merged) - set(fleet_vars)))
        print(f"wrote {fleet_env_path} ({len(fleet_vars)} migrated, {len(merged)} total)")

    bot_count = 0
    for fleet_bot_name, vars_dict in bot_vars.items():
        bot_env_path = paths.bot_runtime(fleet_bot_name) / ".env"
        if not bot_env_path.parent.is_dir():
            print(f"SKIP {fleet_bot_name}: runtime dir missing — run `claudlobby generate` first", file=sys.stderr)
            continue
        merged = dotenv.merge_into(bot_env_path, vars_dict)
        bot_env_path.write_text(dotenv.format_file(
            f"# Bot secrets for {fleet_bot_name} (migrated; pre-existing keys preserved)",
            merged,
        ))
        bot_count += 1
        print(f"wrote {bot_env_path} ({len(vars_dict)} migrated, {len(merged)} total)")

    print(f"\nApplied: {len(fleet_vars)} fleet vars, {bot_count} bot .env files")
    return 0


# Subdir names we never copy from a legacy bot dir — claudlobby-managed,
# build artifacts, dotfile noise, runtime junk.
_DATA_MIGRATE_AUTO_SKIP_DIRS: set[str] = {
    ".git", ".github", ".gitignore", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "venv", ".venv", "dist", "build",
    ".claude", "memory", "logs",
}

PlanAction = Literal["copy", "skip-git", "skip-git-container", "skip-exists", "skip-empty", "skip-mount"]


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
    import shutil

    paths = _resolve_paths(args)
    fleet = load_fleet(paths.fleet_yaml)
    source_dir = Path(args.source).expanduser().resolve()

    if not source_dir.is_dir():
        print(f"ERROR: source directory not found: {source_dir}", file=sys.stderr)
        return 1

    try:
        rename_map = _parse_rename_map(args.map or [])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

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
            Path(t).expanduser().resolve()
            for t in bot_cfg.mounts.values()
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
                plan.append(_DataMigratePlanItem(fleet_bot_name, child, bot_data_dir / name, "skip-mount", 0.0))
                continue

            dst = bot_data_dir / name

            # Order: cheap skip-checks BEFORE the recursive size walk. Sizing
            # 4.7G of git-checkout files only to discard the result is a real
            # cost on the Pi.
            if (child / ".git").is_dir():
                plan.append(_DataMigratePlanItem(fleet_bot_name, child, dst, "skip-git", 0.0))
                continue
            if _contains_git_checkouts(child):
                plan.append(_DataMigratePlanItem(fleet_bot_name, child, dst, "skip-git-container", 0.0))
                continue
            if dst.exists():
                plan.append(_DataMigratePlanItem(fleet_bot_name, child, dst, "skip-exists", 0.0))
                continue

            size_mb = _dir_size_mb(child)
            if size_mb == 0:
                plan.append(_DataMigratePlanItem(fleet_bot_name, child, dst, "skip-empty", 0.0))
                continue

            plan.append(_DataMigratePlanItem(fleet_bot_name, child, dst, "copy", size_mb))

    print(f"\n=== data-migrate plan ===")
    print(f"source: {source_dir}")
    print(f"fleet:  {fleet.name}")
    if rename_map:
        print(f"rename map: {rename_map}")
    if include_set is not None:
        print(f"include filter: {sorted(include_set)}")
    if exclude_set:
        print(f"exclude filter: {sorted(exclude_set)}")
    print()

    if missing_sources:
        print("Source dirs missing (use --map fleet-bot=src-dir if renamed):")
        for fb, sp in missing_sources:
            print(f"  {fb} → expected {sp}")
        print()

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
            print(f"  {fb}:")
            for item in by_bot[fb]:
                sz_str = _human_size(item.size_mb)
                rel_src = item.src.relative_to(source_dir)
                rel_dst = (
                    item.dst.relative_to(paths.root)
                    if item.dst.is_relative_to(paths.root)
                    else item.dst
                )
                if item.action == "copy":
                    print(f"    COPY  {sz_str:>6}  {rel_src}/  →  {rel_dst}/")
                    total_to_copy_mb += item.size_mb
                else:
                    reason = _SKIP_REASON[item.action]
                    print(f"    SKIP  {sz_str:>6}  {rel_src}/  ({reason})")
        print()
        print(f"Total to copy: {_human_size(total_to_copy_mb)}")
    else:
        print("(no data dirs found to migrate — try --include <name> if subdirs were auto-skipped)")

    if not args.apply:
        print("\n(dry-run — pass --apply to copy)")
        return 0

    print()
    copied = 0
    for item in plan:
        if item.action != "copy":
            continue
        item.dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(item.src, item.dst, symlinks=True)
            print(f"copied  {item.src}  →  {item.dst}")
            copied += 1
        except (OSError, shutil.Error) as e:
            print(f"FAILED  {item.src}: {e}", file=sys.stderr)

    print(f"\nApplied: {copied} dirs copied")
    return 0


def cmd_cron_migrate(args) -> int:
    """Rewrite cron entries from a legacy bot-fleet path layout to claudlobby's.

    Defaults to dry-run; pass --apply to install the new crontab. Backs up
    the existing crontab to ~/crontab-backup-<ts>.txt before writing.

    Substitution rule per (fleet_bot, src_dir) pair:
      <source>/<src_dir>  →  <bot_runtime>/data
    e.g. with --source ~/pi-fleet --map clog=assistant:
      /home/<u>/pi-fleet/assistant/finances/x.py
        → /home/<u>/claudlobby/local/<fleet>/runtime/bots/clog/data/finances/x.py

    Lines that don't reference the source prefix pass through unchanged.
    """
    import subprocess
    from collections import defaultdict
    from datetime import datetime

    paths = _resolve_paths(args)
    fleet = load_fleet(paths.fleet_yaml)
    source_dir = Path(args.source).expanduser().resolve()

    if not source_dir.is_dir():
        print(f"ERROR: source directory not found: {source_dir}", file=sys.stderr)
        return 1

    try:
        rename_map = _parse_rename_map(args.map or [])
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Build per-bot context: legacy prefix, bot data dir, search dirs for resolution.
    import re

    @dataclass
    class _BotCronCtx:
        legacy_prefix: str
        bot_name: str
        data_dir: Path        # <bot_runtime>/data
        search_dirs: list[Path]  # ordered: lib/, data/scripts/, data/

    bot_ctxs: list[_BotCronCtx] = []
    for fleet_bot in fleet.bots:
        src_name = rename_map.get(fleet_bot, fleet_bot)
        legacy_prefix = str(source_dir / src_name)
        data_dir = paths.bot_runtime(fleet_bot) / "data"
        projects_dir = paths.bot_runtime(fleet_bot) / "projects"
        search_dirs = [
            paths.lib,                      # package-level shared scripts
            paths.lib / "personal",         # package-level personal scripts
            data_dir / "scripts",           # bot-specific scripts
            data_dir,                       # bot data (finances/, etc.)
            projects_dir,                   # git checkouts the bot works in
        ]
        bot_ctxs.append(_BotCronCtx(legacy_prefix, fleet_bot, data_dir, search_dirs))
    # Sort longest prefix first so child paths win
    bot_ctxs.sort(key=lambda c: len(c.legacy_prefix), reverse=True)

    def _resolve_path(legacy_path: str, ctx: _BotCronCtx) -> str:
        """Resolve a single legacy absolute path to its claudlobby location.

        Strategy: strip the legacy prefix to get the relative path, then
        check each search dir for the file/dir. If found, return the resolved
        path. If not found, fall back to the naive data/ prefix (will be
        caught by the verify pass).
        """
        if not legacy_path.startswith(ctx.legacy_prefix):
            return legacy_path
        rel = legacy_path[len(ctx.legacy_prefix):].lstrip("/")
        if not rel:
            return str(ctx.data_dir)

        # For paths like "finances/portfolio-snapshot.py", try each search dir
        for search_dir in ctx.search_dirs:
            candidate = search_dir / rel
            if candidate.exists():
                return str(candidate)

        # For top-level files (e.g., "briefing-cron.sh"), also try just the filename
        basename = Path(rel).name
        if basename != rel:  # only if rel has subdirs
            for search_dir in ctx.search_dirs:
                candidate = search_dir / basename
                if candidate.exists():
                    return str(candidate)

        # Fallback: naive prefix substitution (verify pass will flag it)
        return str(ctx.data_dir / rel)

    def _rewrite_line(line: str) -> tuple[str, str | None]:
        """Rewrite all legacy paths in a cron line. Returns (new_line, bot_name)."""
        for ctx in bot_ctxs:
            if ctx.legacy_prefix not in line:
                continue
            # Replace each occurrence of a legacy path individually
            result = line
            for m in reversed(list(re.finditer(re.escape(ctx.legacy_prefix) + r'[/\w._-]*', line))):
                old_path = m.group(0)
                new_path = _resolve_path(old_path, ctx)
                result = result[:m.start()] + new_path + result[m.end():]
            return result, ctx.bot_name
        return line, None

    # Read current user crontab
    proc = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "no crontab" in stderr:
            print(f"no current crontab for {os.environ.get('USER', 'this user')} — nothing to migrate", file=sys.stderr)
            return 0
        print(f"ERROR: `crontab -l` failed: {proc.stderr.strip()}", file=sys.stderr)
        return 1
    current_crontab = proc.stdout

    # Plan: per-line, resolve paths intelligently
    new_lines: list[str] = []
    rewrites: list[tuple[int, str, str, str]] = []
    unmatched_legacy: list[tuple[int, str]] = []
    legacy_root = str(source_dir)
    for i, line in enumerate(current_crontab.splitlines(), 1):
        replaced, matched_bot = _rewrite_line(line)
        new_lines.append(replaced)
        if matched_bot:
            rewrites.append((i, line, replaced, matched_bot))
        elif legacy_root in line and not line.lstrip().startswith("#"):
            unmatched_legacy.append((i, line))

    # Plan output
    print(f"\n=== cron-migrate plan ===")
    print(f"source: {source_dir}")
    print(f"fleet:  {fleet.name}")
    if rename_map:
        print(f"rename map: {rename_map}")
    print()

    if not rewrites and not unmatched_legacy:
        print("(no crontab lines reference the source prefix — nothing to migrate)")
        return 0

    # Verify pass: extract absolute paths from rewritten lines and check existence.
    # A cron line can reference multiple paths (script path, log path, args).
    import re
    broken_paths: list[tuple[int, str, str]] = []  # (line_no, bot, missing_path)
    for line_no, _old, new, bot in rewrites:
        # Extract all absolute paths from the rewritten line
        for m in re.finditer(r'(/\S+)', new):
            candidate = m.group(1)
            # Skip env-sourcing paths (. /home/.env), pipes, and redirections
            if candidate.startswith("/usr/") or candidate.startswith("/bin/"):
                continue
            # Strip trailing redirection chars
            candidate = candidate.rstrip(";\"'")
            p = Path(candidate)
            if p.suffix or p.name.endswith(".log"):
                # It's a file reference — check parent dir exists and file exists
                if not p.exists() and not p.parent.exists():
                    broken_paths.append((line_no, bot, candidate))
                elif not p.exists() and p.suffix in (".sh", ".py"):
                    broken_paths.append((line_no, bot, candidate))
            elif not p.exists() and str(p).startswith(str(paths.root)):
                # Directory reference under claudlobby that doesn't exist
                broken_paths.append((line_no, bot, candidate))

    if rewrites:
        by_bot: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for line_no, old, new, bot in rewrites:
            by_bot[bot].append((line_no, old, new))
        for bot in sorted(by_bot):
            print(f"  {bot}: {len(by_bot[bot])} line(s) to rewrite")
            for line_no, old, new in by_bot[bot]:
                print(f"    line {line_no}:")
                print(f"      − {old}")
                print(f"      + {new}")
        print()
        print(f"Total: {len(rewrites)} lines rewritten across {len(by_bot)} bot(s)")

    if broken_paths:
        print()
        print(f"⚠ {len(broken_paths)} rewritten path(s) don't exist at destination — verify before applying:")
        for line_no, bot, path in broken_paths:
            print(f"    line {line_no} ({bot}): {path}")

    if unmatched_legacy:
        print()
        print(f"⚠ {len(unmatched_legacy)} line(s) reference {legacy_root} but no bot subpath — operator must handle:")
        for line_no, line in unmatched_legacy:
            print(f"    line {line_no}: {line}")

    if not args.apply:
        print("\n(dry-run — pass --apply to install the new crontab)")
        return 0

    # Apply: backup current crontab, install rewritten one
    backup_path = Path.home() / f"crontab-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    backup_path.write_text(current_crontab)
    print(f"\nbackup: {backup_path}")

    new_crontab = "\n".join(new_lines) + ("\n" if new_lines and not new_lines[-1].endswith("\n") else "")
    install = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if install.returncode != 0:
        print(f"ERROR: `crontab -` install failed: {install.stderr.strip()}", file=sys.stderr)
        print(f"  your old crontab is preserved as {backup_path}", file=sys.stderr)
        return 1
    print(f"installed new crontab ({len(rewrites)} lines rewritten)")
    return 0


def cmd_memory_migrate(args) -> int:
    """Migrate memory files from an existing bot setup to claudlobby per-bot memory dirs."""
    import shutil

    paths = _resolve_paths(args)
    fleet = load_fleet(paths.fleet_yaml)
    claude_projects = Path.home() / ".claude" / "projects"

    if not claude_projects.is_dir():
        print(f"ERROR: no ~/.claude/projects/ directory found", file=sys.stderr)
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
                print(f"  SKIP {project_dir.name} ({len(memory_files)} files) — no matching bot in fleet")
                print(f"        use --map '{project_dir.name}:<bot-name>' to map it")
            continue

        if target_bot not in fleet.bots:
            print(f"  SKIP {project_dir.name} — mapped to '{target_bot}' but not in fleet", file=sys.stderr)
            continue

        # Copy memory files to bot's memory dir
        dest_memory = paths.bot_runtime(target_bot) / "memory"
        dest_memory.mkdir(parents=True, exist_ok=True)

        file_count = 0
        for src_file in memory_dir.glob("*.md"):
            dest_file = dest_memory / src_file.name
            if dest_file.exists() and not args.force:
                print(f"  SKIP {target_bot}/{src_file.name} — already exists (use --force to overwrite)")
                continue
            shutil.copy2(src_file, dest_file)
            file_count += 1

        if file_count > 0:
            print(f"  {project_dir.name} → {target_bot}: {file_count} memory files")
            migrated += 1

    if migrated == 0:
        print("\nNo memory files migrated. Check --map mappings or run with --force.")
        return 1

    print(f"\nMigrated memory for {migrated} bot(s).")
    print("Memories are now in local/<fleet>/runtime/bots/<bot>/memory/")
    return 0


def cmd_new_bot(args) -> int:
    """Interactive (or flag-driven) bot creation."""
    from .newbot import (
        NewBotInputs,
        insert_bot_stanza,
        interactive_collect,
        materialize_voice,
        render_stanza,
        write_token_to_env,
    )
    paths = _resolve_paths(args)

    # Pick mode. If --name given without --interactive, run non-interactive.
    if args.interactive or not args.name:
        inp = interactive_collect(paths)
    else:
        # Non-interactive: build from flags.
        def _csv(s: str | None) -> list[str] | None:
            if not s:
                return None
            return [x.strip() for x in s.split(",") if x.strip()]

        inp = NewBotInputs(
            name=args.name,
            expertise=_csv(args.expertise) or [],
            voice=args.voice,
            mission=args.mission,
            model=args.model,
            effort=args.effort,
            account=args.account,
            mcp=_csv(args.mcp),
            skills=_csv(args.skills),
            guardrails=_csv(args.guardrails),
            protocols=_csv(args.protocols),
            resources=_csv(args.resources),
            lessons=_csv(args.lessons),
            integrations=_csv(args.integrations),
            remote_control=False if args.no_remote_control else None,
            dangerously_skip_permissions=False if args.no_dangerously_skip_permissions else None,
            extra_flags=_csv(args.extra_flags),
            scope_org=args.scope_org,
            scope_repos=_csv(args.scope_repos),
            scope_snowflake_targets=_csv(args.scope_snowflake_targets),
            team=args.team,
            telegram_handle=args.telegram_handle,
            token_env=args.token_env or (f"TELEGRAM_TOKEN_{args.name.upper().replace('-', '_')}" if args.name else None),
            require_mention=args.require_mention if args.require_mention is not None else True,
            chat_id=args.chat_id,
            startup_prompt=args.startup_prompt,
        )
        # If --voice-text passed, write it now.
        if args.voice_text:
            inp.voice = materialize_voice(paths, inp.name, None, args.voice_text)

    # Validation: required fields
    if not inp.name:
        print("ERROR: --name is required", file=sys.stderr)
        return 1
    if not inp.expertise:
        print("ERROR: at least one --expertise is required", file=sys.stderr)
        return 1

    # Render the stanza
    stanza = render_stanza(inp)

    print("\n=== Stanza to be added to fleet.yaml ===\n")
    print(stanza)

    if inp.team:
        print(f"  → will also be added to team '{inp.team}'.workers")

    if args.dry_run:
        print("\n--dry-run: no changes written. Stanza above would be inserted into fleet.yaml.")
        return 0

    # Confirm
    if not args.yes:
        ans = input("\nWrite to fleet.yaml? [Y/n]: ").strip().lower()
        if ans and ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Backup fleet.yaml
    backup = paths.fleet_yaml.with_suffix(".yaml.bak")
    backup.write_text(paths.fleet_yaml.read_text())
    print(f"  ✓ Backup: {backup}")

    # Insert
    new_text = insert_bot_stanza(paths.fleet_yaml, stanza, team=inp.team)
    paths.fleet_yaml.write_text(new_text)
    print(f"  ✓ Updated {paths.fleet_yaml}")

    # Auto-generate
    if args.auto_generate:
        print(f"\n=== Running `claudlobby generate --bot {inp.name}` ===\n")
        from .composer import compose_bot
        from .config import load_fleet
        fleet = load_fleet(paths.fleet_yaml)
        bot = fleet.bots.get(inp.name)
        if bot is None:
            print(f"ERROR: bot '{inp.name}' not found in fleet.yaml after insertion", file=sys.stderr)
            return 1
        out_dir = compose_bot(bot, fleet, paths)
        print(f"  ✓ Composed to {out_dir}")

    # Next steps
    print("\n=== Next steps ===")
    print(f"  1. Review {paths.fleet_yaml}")
    if inp.token_env:
        env_set = (paths.env_file.is_file() and inp.token_env in paths.env_file.read_text())
        if env_set:
            print(f"  2. Token already in .env ✓")
        else:
            print(f"  2. Add {inp.token_env}=<your-token> to {paths.env_file}")
    print(f"  3. Run: claudlobby validate")
    if not args.auto_generate:
        print(f"  4. Run: claudlobby generate --bot {inp.name}")
    print(f"  5. Install service:")
    print(f"     # Linux: sudo ln -sf {paths.bot_runtime(inp.name)}/{inp.name}.service /etc/systemd/system/")
    print(f"     #        sudo systemctl daemon-reload && sudo systemctl enable --now {inp.name}.service")
    print(f"     # macOS: ln -sf {paths.bot_runtime(inp.name)}/{inp.name}.plist ~/Library/LaunchAgents/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudlobby",
        description="Compositor for Claude Code agent fleets.",
    )
    parser.add_argument("--root", help="Path to claudlobby repo root (auto-detected by default)")
    parser.add_argument(
        "--fleet",
        help="Fleet overlay name (uses local/<fleet>/ for fleet.yaml, library overlay, voices overlay, runtime/). "
             "If omitted, runs in root mode (fleet.yaml at repo root).",
    )
    parser.add_argument("-V", "--version", action="version", version=f"claudlobby {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="Validate fleet.yaml against library/")
    pv.add_argument("--strict", action="store_true", help="Fail on warnings")
    pv.set_defaults(func=cmd_validate)

    pg = sub.add_parser("generate", help="Compose runtime/bots/ from fleet.yaml + library/")
    pg.add_argument("--bot", help="Generate only one bot")
    pg.add_argument("--strict", action="store_true", help="Refuse to generate on warnings")
    pg.set_defaults(func=cmd_generate)

    pl = sub.add_parser("list-library", help="List available personas, skills, mcp, guardrails, protocols, voices")
    pl.set_defaults(func=cmd_list_library)

    pd = sub.add_parser("diff", help="Show drift between runtime/bots/<bot>/ and what generate would produce")
    pd.add_argument("--bot", help="Diff only one bot (default: all)")
    pd.set_defaults(func=cmd_diff)

    pp = sub.add_parser("promote", help="Promote runtime drift back to library/")
    pp.add_argument("bot", help="Bot name")
    pp.set_defaults(func=cmd_promote)

    ps = sub.add_parser("status", help="Fleet health dashboard (stub)")
    ps.set_defaults(func=cmd_status)

    pe = sub.add_parser("env-migrate", help="Extract secrets from an existing bot setup into tiered .env files (dry-run by default)")
    pe.add_argument("--source", required=True, help="Path to existing bot fleet dir (e.g. ~/my-bots)")
    pe.add_argument("--map", action="append", default=[], help="Rename a fleet bot to its legacy dir (e.g. --map clog=assistant). Repeatable.")
    pe.add_argument("--apply", action="store_true", help="Write the .env files (default: dry-run preview only)")
    pe.set_defaults(func=cmd_env_migrate)

    pdm = sub.add_parser(
        "data-migrate",
        help="Copy bot data dirs from a legacy bot setup into per-bot runtime data/ (dry-run by default)",
    )
    pdm.add_argument("--source", required=True, help="Path to existing bot fleet dir (e.g. ~/my-bots)")
    pdm.add_argument(
        "--map",
        action="append",
        default=[],
        help="Rename a fleet bot to its legacy dir (e.g. --map clog=assistant). Repeatable.",
    )
    pdm.add_argument(
        "--include",
        help="Comma-separated subdir names to include (overrides auto-discovery — useful to force-copy a default-skipped dir like 'logs')",
    )
    pdm.add_argument(
        "--exclude",
        help="Comma-separated subdir names to skip (e.g. 'personal-projects,work-projects' to keep big git checkouts in place)",
    )
    pdm.add_argument("--apply", action="store_true", help="Actually copy the dirs (default: dry-run preview only)")
    pdm.set_defaults(func=cmd_data_migrate)

    pcm = sub.add_parser(
        "cron-migrate",
        help="Rewrite cron entries from a legacy bot-fleet path layout to claudlobby's (dry-run by default)",
    )
    pcm.add_argument("--source", required=True, help="Path to existing bot fleet dir (e.g. ~/my-bots)")
    pcm.add_argument(
        "--map",
        action="append",
        default=[],
        help="Rename a fleet bot to its legacy dir (e.g. --map clog=assistant). Repeatable.",
    )
    pcm.add_argument("--apply", action="store_true", help="Install the rewritten crontab (default: dry-run preview only)")
    pcm.set_defaults(func=cmd_cron_migrate)

    pm = sub.add_parser("memory-migrate", help="Copy memory files from ~/.claude/projects/ to per-bot memory dirs")
    pm.add_argument("--map", nargs="*", help="Source-to-bot mappings (e.g. 'project-name-pattern:bot-name')")
    pm.add_argument("--force", action="store_true", help="Overwrite existing memory files")
    pm.set_defaults(func=cmd_memory_migrate)

    pn = sub.add_parser(
        "new-bot",
        help="Interactive bot creation (or flag-driven for scripts/skills)",
    )
    pn.add_argument("--name", help="Bot name (lowercase, e.g. 'eng-1')")
    pn.add_argument("--expertise", help="Comma-separated expertise areas (required)")
    pn.add_argument("--voice", help="Path to voice file (e.g. voices/erlich-bachman.md)")
    pn.add_argument("--voice-text", help="Inline voice description (creates voices/<name>.md)")
    pn.add_argument("--mission", help="One-paragraph charter")
    pn.add_argument("--model", help="opus / sonnet / haiku")
    pn.add_argument("--effort", help="max / default")
    pn.add_argument("--account", help="Account key from fleet.accounts (e.g. work)")
    pn.add_argument("--mcp", help="Comma-separated MCP fragments")
    pn.add_argument("--skills", help="Comma-separated skills")
    pn.add_argument("--guardrails", help="Comma-separated guardrails")
    pn.add_argument("--protocols", help="Comma-separated protocols")
    pn.add_argument("--resources", help="Comma-separated resources")
    pn.add_argument("--lessons", help="Comma-separated lessons")
    pn.add_argument("--integrations", help="Comma-separated integrations (auto-paired with mcp by default)")
    pn.add_argument("--no-remote-control", action="store_true", help="Disable --remote-control flag")
    pn.add_argument("--no-dangerously-skip-permissions", action="store_true", help="Disable --dangerously-skip-permissions")
    pn.add_argument("--extra-flags", help="Comma-separated extra claude CLI flags")
    pn.add_argument("--scope-org", help="GitHub org for scope")
    pn.add_argument("--scope-repos", help="Comma-separated repos for scope")
    pn.add_argument("--scope-snowflake-targets", help="Comma-separated Snowflake targets")
    pn.add_argument("--team", help="Add bot to this team's workers list")
    pn.add_argument("--telegram-handle", help="Bot @-handle (without @)")
    pn.add_argument("--token-env", help="Env var name holding the Telegram token (defaults to TELEGRAM_TOKEN_<NAME>)")
    pn.add_argument("--require-mention", type=lambda v: v.lower() in ("true", "yes", "1"),
                    default=None, help="true/false — Telegram requireMention")
    pn.add_argument("--chat-id", help="Override default group chat_id")
    pn.add_argument("--startup-prompt", help="Custom startup prompt")
    pn.add_argument("--interactive", action="store_true", help="Force interactive mode even if flags provided")
    pn.add_argument("--dry-run", action="store_true", help="Show stanza but don't write")
    pn.add_argument("--yes", "-y", action="store_true", help="Skip confirm-before-write")
    pn.add_argument("--auto-generate", action="store_true", help="Run `claudlobby generate --bot <name>` after writing")
    pn.set_defaults(func=cmd_new_bot)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
