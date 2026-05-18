"""claudlobby CLI entry point."""

from __future__ import annotations
import argparse
import logging
import os
import re
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

log = logging.getLogger("claudlobby")


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
        k = k[len("export ") :].strip()
    v = v.strip().strip('"').strip("'")
    return (k, v)


def _migration_preamble(
    args,
) -> tuple[Paths, "FleetConfig", Path, dict[str, str]]:
    """Shared preamble for migration commands.

    Calls sys.exit(1) on failure — callers can unpack the result directly.
    """
    paths = _resolve_paths(args)
    fleet = _load_fleet_or_exit(paths)
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


def _load_fleet_or_exit(paths: Paths) -> "FleetConfig":
    """Wrap load_fleet() with user-friendly error messages on common failures."""
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


def cmd_doctor(args) -> int:
    """Pre-flight fleet health diagnostic."""
    from .doctor import format_report, run_doctor

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)
    report = run_doctor(fleet, paths)
    print(format_report(report))
    return 1 if report.has_failures else 0


def cmd_validate(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)
    report = validate(fleet, paths)

    for e in report.errors:
        log.error("%s", e)
    for w in report.warnings:
        log.warning("%s", w)

    if args.strict and report.has_issues:
        log.error("--strict: warnings count as errors")
        return 1
    if report.has_errors:
        return 1
    if not report.has_issues:
        log.info("fleet.yaml OK (%d bots, %d teams)", len(fleet.bots), len(fleet.teams))
    return 0


def cmd_generate(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)
    report = validate(fleet, paths)

    if report.has_errors:
        log.error("validation errors — refusing to generate")
        for e in report.errors:
            log.error("%s", e)
        return 1
    if args.strict and report.warnings:
        log.error("--strict: warnings count as errors — refusing to generate")
        for w in report.warnings:
            log.warning("%s", w)
        return 1
    for w in report.warnings:
        log.warning("%s", w)

    if args.bot:
        bot = fleet.bots.get(args.bot)
        if not bot:
            log.error("bot '%s' not in fleet.yaml", args.bot)
            return 1
        out = compose_bot(bot, fleet, paths)
        log.info("composed %s → %s", args.bot, out)
    else:
        out = compose_fleet(fleet, paths)
        log.info("composed %d bots → %s", len(out), paths.runtime_bots)
    return 0


def cmd_list_library(args) -> int:
    paths = _resolve_paths(args)

    def _list_md(label: str, kind: str):
        """Walk overlay → base recursively. Display nested files as `dir/name`."""
        log.info("%s:", label)
        seen: dict[str, str] = {}  # rel_key (no .md) → "[overlay]" or "[base]"
        for d in paths.library_search_dirs(kind):
            if not d.is_dir():
                continue
            tag = (
                "[overlay]"
                if (paths.overlay_library and d == paths.overlay_library / kind)
                else "[base]"
            )
            for p in sorted(d.rglob("*.md")):
                if p.stem.lower().startswith("readme"):
                    continue
                rel_key = str(p.relative_to(d).with_suffix(""))
                if rel_key not in seen:
                    seen[rel_key] = tag
        for rel_key, tag in sorted(seen.items()):
            marker = " (override)" if tag == "[overlay]" else ""
            log.info("  %s%s", rel_key, marker)

    _list_md("Expertise", "expertise")

    log.info("MCP fragments (base only):")
    if paths.base_mcp.is_dir():
        for p in sorted(paths.base_mcp.glob("*.json")):
            log.info("  %s", p.stem)

    _list_md("Integrations", "integrations")
    _list_md("Protocols", "protocols")
    _list_md("Guardrails", "guardrails")
    _list_md("Resources", "resources")
    _list_md("Lessons", "lessons")
    _list_md("Post-actions", "post_actions")

    log.info("Skills:")
    seen_skills: dict[str, str] = {}  # rel_key → tag
    for d in paths.library_search_dirs("skills"):
        if not d.is_dir():
            continue
        tag = (
            "[overlay]"
            if (paths.overlay_library and d == paths.overlay_library / "skills")
            else "[base]"
        )
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
        log.info("  %s%s", rel_key, marker)

    log.info("Voices:")
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
            tag = (
                " (override)"
                if (paths.overlay_voices and p.is_relative_to(paths.overlay_voices))
                else ""
            )
        except ValueError:
            tag = ""
        log.info("  %s%s", p.relative_to(paths.root), tag)

    if paths.fleet_dir:
        log.info("[fleet overlay: %s]", paths.fleet_dir.relative_to(paths.root))
    else:
        log.info("[no fleet overlay — root mode. Use --fleet <name> for overlay mode.]")
    return 0


def cmd_diff(args) -> int:
    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)
    if args.bot:
        sys.stdout.write(diff_bot(args.bot, fleet, paths))
    else:
        for name in fleet.bots:
            sys.stdout.write(diff_bot(name, fleet, paths))
    return 0


def cmd_promote(args) -> int:
    paths = _resolve_paths(args)
    fleet = _load_fleet_or_exit(paths)
    sys.stdout.write(promote_bot(args.bot, fleet, paths))
    return 0


def cmd_status(args) -> int:
    """Fleet health dashboard — live snapshot from tmux, systemd, fleet-state."""
    from .status import (
        collect_fleet_status,
        format_bot_detail,
        format_json,
        format_table,
    )

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)

    bot_filter = getattr(args, "bot", None)
    use_json = getattr(args, "json", False)

    statuses = collect_fleet_status(fleet, paths)

    if bot_filter:
        matches = [bs for bs in statuses if bs.name == bot_filter]
        if not matches:
            log.error("bot %r not found in fleet %r", bot_filter, fleet.name)
            return 1
        if use_json:
            sys.stdout.write(format_json(matches, fleet.name))
        else:
            sys.stdout.write(format_bot_detail(matches[0]))
        return 0

    if use_json:
        sys.stdout.write(format_json(statuses, fleet.name))
    else:
        sys.stdout.write(format_table(statuses, fleet.name))
    return 0


def cmd_report_back(args) -> int:
    """Query the report-back ledger — human-readable table of bot work events."""
    import json as _json
    from datetime import datetime, timedelta, timezone

    paths = _resolve_paths(args)

    # Resolve ledger path (mirrors shell logic in report-back.sh)
    if paths.fleet_dir:
        ledger = paths.runtime / "report-back.jsonl"
    else:
        ledger = paths.root / "runtime" / "fleet" / "report-back.jsonl"

    if not ledger.is_file():
        log.info("no report-back ledger found at %s", ledger)
        return 0

    # Parse --since into a cutoff timestamp
    cutoff = None
    if args.since:
        raw = args.since.strip()
        now = datetime.now(timezone.utc)
        if raw.endswith("h"):
            cutoff = now - timedelta(hours=int(raw[:-1]))
        elif raw.endswith("d"):
            cutoff = now - timedelta(days=int(raw[:-1]))
        elif raw.endswith("m"):
            cutoff = now - timedelta(minutes=int(raw[:-1]))
        else:
            try:
                cutoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                log.error(
                    "cannot parse --since '%s' (use e.g. 24h, 7d, 30m, or ISO date)",
                    raw,
                )
                return 1

    # Read and filter entries
    entries = []
    for line in ledger.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if args.bot and entry.get("bot") != args.bot:
            continue
        if args.status and entry.get("status") != args.status:
            continue
        if cutoff:
            try:
                ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except (KeyError, ValueError):
                continue
        entries.append(entry)

    if not entries:
        log.info("no matching events")
        return 0

    if args.json:
        for e in entries:
            print(_json.dumps(e))
        return 0

    # Table output
    print(f"{'TIMESTAMP':<22} {'BOT':<12} {'STATUS':<10} {'SUMMARY':<50} {'PR'}")
    print("-" * 110)
    for e in entries:
        ts = e.get("ts", "")[:19]
        bot = e.get("bot", "")[:11]
        status = e.get("status", "")[:9]
        summary = e.get("summary", "")[:49]
        pr = e.get("pr_url", "")
        print(f"{ts:<22} {bot:<12} {status:<10} {summary:<50} {pr}")

    print(f"\n{len(entries)} event(s)")
    return 0


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
    import re
    import json as _json

    home_env = Path.home() / ".env"
    env_from_home: dict[str, str] = {}
    if home_env.is_file():
        for line in home_env.read_text().splitlines():
            parsed = _parse_env_line(line)
            if parsed:
                env_from_home[parsed[0]] = parsed[1]

    SECRET_LIKE = re.compile(r"(TOKEN|KEY|SECRET|URL|HANDLE|CHAT_ID|PASSWORD|API)")
    bot_conf_secrets: dict[str, dict[str, str]] = {}
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
    import re

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
    import shutil

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


@dataclass
class _BotCronCtx:
    """Per-bot context for cron path resolution."""

    legacy_prefix: str
    bot_name: str
    data_dir: Path
    search_dirs: list[Path]


def _build_cron_contexts(
    fleet: "FleetConfig",
    source_dir: Path,
    rename_map: dict[str, str],
    paths: Paths,
) -> list["_BotCronCtx"]:
    """Build per-bot cron contexts sorted longest-prefix-first."""
    ctxs: list[_BotCronCtx] = []
    for fleet_bot in fleet.bots:
        src_name = rename_map.get(fleet_bot, fleet_bot)
        legacy_prefix = str(source_dir / src_name)
        data_dir = paths.bot_runtime(fleet_bot) / "data"
        projects_dir = paths.bot_runtime(fleet_bot) / "projects"
        search_dirs = [
            paths.lib,
            paths.lib / "personal",
            data_dir / "scripts",
            data_dir,
            projects_dir,
        ]
        ctxs.append(_BotCronCtx(legacy_prefix, fleet_bot, data_dir, search_dirs))
    ctxs.sort(key=lambda c: len(c.legacy_prefix), reverse=True)
    return ctxs


def _resolve_cron_path(legacy_path: str, ctx: _BotCronCtx) -> str:
    """Resolve a single legacy absolute path to its claudlobby location.

    Strips the legacy prefix, searches each search dir for the relative path.
    Falls back to naive data/ prefix (verify pass will flag it).
    """
    if not legacy_path.startswith(ctx.legacy_prefix):
        return legacy_path
    rel = legacy_path[len(ctx.legacy_prefix) :].lstrip("/")
    if not rel:
        return str(ctx.data_dir)

    for search_dir in ctx.search_dirs:
        candidate = search_dir / rel
        if candidate.exists():
            return str(candidate)

    basename = Path(rel).name
    if basename != rel:
        for search_dir in ctx.search_dirs:
            candidate = search_dir / basename
            if candidate.exists():
                return str(candidate)

    return str(ctx.data_dir / rel)


def _rewrite_cron_line(
    line: str, bot_ctxs: list[_BotCronCtx]
) -> tuple[str, str | None]:
    """Rewrite all legacy paths in a cron line. Returns (new_line, bot_name)."""
    import re

    for ctx in bot_ctxs:
        if ctx.legacy_prefix not in line:
            continue
        result = line
        for m in reversed(
            list(re.finditer(re.escape(ctx.legacy_prefix) + r"[/\w._-]*", line))
        ):
            old_path = m.group(0)
            new_path = _resolve_cron_path(old_path, ctx)
            result = result[: m.start()] + new_path + result[m.end() :]
        return result, ctx.bot_name
    return line, None


def _verify_cron_paths(
    rewrites: list[tuple[int, str, str, str]], root: Path
) -> list[tuple[int, str, str]]:
    """Check rewritten paths exist. Returns list of (line_no, bot, missing_path)."""
    import re

    broken: list[tuple[int, str, str]] = []
    for line_no, _old, new, bot in rewrites:
        for m in re.finditer(r"(/\S+)", new):
            candidate = m.group(1)
            if candidate.startswith("/usr/") or candidate.startswith("/bin/"):
                continue
            candidate = candidate.rstrip(";\"'")
            p = Path(candidate)
            if p.suffix or p.name.endswith(".log"):
                if not p.exists() and not p.parent.exists():
                    broken.append((line_no, bot, candidate))
                elif not p.exists() and p.suffix in (".sh", ".py"):
                    broken.append((line_no, bot, candidate))
            elif not p.exists() and str(p).startswith(str(root)):
                broken.append((line_no, bot, candidate))
    return broken


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

    paths, fleet, source_dir, rename_map = _migration_preamble(args)
    bot_ctxs = _build_cron_contexts(fleet, source_dir, rename_map, paths)

    # Read current user crontab
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        log.error("crontab command not found — is cron installed?")
        return 1
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "no crontab" in stderr:
            log.warning(
                "no current crontab for %s — nothing to migrate",
                os.environ.get("USER", "this user"),
            )
            return 0
        log.error("`crontab -l` failed: %s", proc.stderr.strip())
        return 1
    current_crontab = proc.stdout

    # Rewrite lines
    new_lines: list[str] = []
    rewrites: list[tuple[int, str, str, str]] = []
    unmatched_legacy: list[tuple[int, str]] = []
    legacy_root = str(source_dir)
    for i, line in enumerate(current_crontab.splitlines(), 1):
        replaced, matched_bot = _rewrite_cron_line(line, bot_ctxs)
        new_lines.append(replaced)
        if matched_bot:
            rewrites.append((i, line, replaced, matched_bot))
        elif legacy_root in line and not line.lstrip().startswith("#"):
            unmatched_legacy.append((i, line))

    # Plan output
    log.info("=== cron-migrate plan ===")
    log.info("source: %s", source_dir)
    log.info("fleet:  %s", fleet.name)
    if rename_map:
        log.info("rename map: %s", rename_map)

    if not rewrites and not unmatched_legacy:
        log.info("(no crontab lines reference the source prefix — nothing to migrate)")
        return 0

    broken_paths = _verify_cron_paths(rewrites, paths.root)

    if rewrites:
        by_bot: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for line_no, old, new, bot in rewrites:
            by_bot[bot].append((line_no, old, new))
        for bot in sorted(by_bot):
            log.info("  %s: %d line(s) to rewrite", bot, len(by_bot[bot]))
            for line_no, old, new in by_bot[bot]:
                log.info("    line %d:", line_no)
                log.info("      − %s", old)
                log.info("      + %s", new)
        log.info(
            "Total: %d lines rewritten across %d bot(s)", len(rewrites), len(by_bot)
        )

    if broken_paths:
        log.warning(
            "%d rewritten path(s) don't exist at destination — verify before applying:",
            len(broken_paths),
        )
        for line_no, bot, path in broken_paths:
            log.warning("    line %d (%s): %s", line_no, bot, path)

    if unmatched_legacy:
        log.warning(
            "%d line(s) reference %s but no bot subpath — operator must handle:",
            len(unmatched_legacy),
            legacy_root,
        )
        for line_no, line in unmatched_legacy:
            log.warning("    line %d: %s", line_no, line)

    if not args.apply:
        log.info("(dry-run — pass --apply to install the new crontab)")
        return 0

    # Apply: backup current crontab, install rewritten one
    backup_path = (
        Path.home() / f"crontab-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    )
    backup_path.write_text(current_crontab)
    log.info("backup: %s", backup_path)

    new_crontab = "\n".join(new_lines) + (
        "\n" if new_lines and not new_lines[-1].endswith("\n") else ""
    )
    install = subprocess.run(
        ["crontab", "-"], input=new_crontab, text=True, capture_output=True
    )
    if install.returncode != 0:
        log.error("`crontab -` install failed: %s", install.stderr.strip())
        log.error("your old crontab is preserved as %s", backup_path)
        return 1
    log.info("installed new crontab (%d lines rewritten)", len(rewrites))
    return 0


def cmd_memory_migrate(args) -> int:
    """Migrate memory files from an existing bot setup to claudlobby per-bot memory dirs."""
    import shutil

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


def cmd_warm_cache(args) -> int:
    """Pre-download npx packages referenced by MCP fragments.

    Scans all MCP fragments used by the fleet and runs `npx -y <pkg> --help`
    for each unique npx-based package. This ensures ~/.npm/_npx/ is warm
    before bot startup, avoiding 30-60s cold-download delays on Pi hardware.
    """
    import json as _json
    import subprocess

    paths = _resolve_paths(args)
    _load_env(paths)
    fleet = _load_fleet_or_exit(paths)

    # Collect unique npx packages from all bot MCP configs
    npx_packages: set[str] = set()
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                continue
            try:
                frag = _json.loads(frag_path.read_text())
            except _json.JSONDecodeError:
                continue
            for k, v in frag.items():
                if k.startswith("_") or not isinstance(v, dict):
                    continue
                if v.get("command") == "npx" and "args" in v:
                    args_list = v["args"]
                    # Extract package name: first arg after "-y"
                    for i, a in enumerate(args_list):
                        if a == "-y" and i + 1 < len(args_list):
                            npx_packages.add(args_list[i + 1])
                            break

    if not npx_packages:
        log.info("no npx-based MCP packages found in fleet")
        return 0

    log.info("warming npx cache for %d packages:", len(npx_packages))
    failed = []
    for pkg in sorted(npx_packages):
        log.info("  %s", pkg)
        if args.dry_run:
            continue
        # Use --help or a quick-exit to trigger download without running the server
        try:
            subprocess.run(
                ["npx", "-y", pkg, "--help"],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            log.warning("  timeout warming %s (120s) — may still have cached", pkg)
        except FileNotFoundError:
            log.error("npx not found — install Node.js first")
            return 1
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("  failed to warm %s: %s", pkg, e)
            failed.append(pkg)

    if args.dry_run:
        log.info("(dry run — no downloads)")
    elif failed:
        log.warning("%d packages failed to warm: %s", len(failed), ", ".join(failed))
    else:
        log.info("cache warm complete")
    return 0


_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def cmd_new_skill(args) -> int:
    """Interactive (or flag-driven) skill scaffolding."""
    from .newskill import interactive_collect, render_skill

    paths = _resolve_paths(args)

    if args.interactive or not args.name:
        name, description, argument_hint = interactive_collect()
    else:
        name = args.name
        description = args.description or ""
        argument_hint = args.argument_hint
        if not description:
            log.error("--description is required in non-interactive mode")
            return 1

    if not _SLUG_RE.match(name):
        log.error(
            "name must be lowercase, start with a letter, only [a-z0-9_-]: %r", name
        )
        return 1

    # Determine target directory (overlay if fleet, else base)
    if paths.overlay_library:
        skill_dir = paths.overlay_library / "skills" / name
    else:
        skill_dir = paths.base_skills / name

    if skill_dir.exists():
        log.error("skill already exists: %s", skill_dir)
        return 1

    content = render_skill(name, description, argument_hint)

    if args.dry_run:
        print(f"\n=== Would create {skill_dir}/SKILL.md ===\n")
        print(content)
        return 0

    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    log.info("created %s/SKILL.md", skill_dir)
    log.info("next: edit %s/SKILL.md to flesh out the skill behavior", skill_dir)
    return 0


def cmd_new_guardrail(args) -> int:
    """Interactive (or flag-driven) guardrail scaffolding."""
    from .newguardrail import interactive_collect, render_guardrail

    paths = _resolve_paths(args)

    if args.interactive or not args.name:
        name, title, description = interactive_collect()
    else:
        name = args.name
        title = args.title or name.replace("-", " ").title()
        description = args.description or ""
        if not description:
            log.error("--description is required in non-interactive mode")
            return 1

    if not _SLUG_RE.match(name):
        log.error(
            "name must be lowercase, start with a letter, only [a-z0-9_-]: %r", name
        )
        return 1

    # Determine target directory (overlay if fleet, else base)
    if paths.overlay_library:
        guardrail_path = paths.overlay_library / "guardrails" / f"{name}.md"
    else:
        guardrail_path = paths.base_guardrails / f"{name}.md"

    if guardrail_path.exists():
        log.error("guardrail already exists: %s", guardrail_path)
        return 1

    content = render_guardrail(name, title, description)

    if args.dry_run:
        print(f"\n=== Would create {guardrail_path} ===\n")
        print(content)
        return 0

    guardrail_path.parent.mkdir(parents=True, exist_ok=True)
    guardrail_path.write_text(content)
    log.info("created %s", guardrail_path)
    log.info("next: edit %s to add specific rules and examples", guardrail_path)
    return 0


def cmd_new_bot(args) -> int:
    """Interactive (or flag-driven) bot creation."""
    from .newbot import (
        NewBotInputs,
        insert_bot_stanza,
        interactive_collect,
        materialize_voice,
        render_stanza,
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
            dangerously_skip_permissions=False
            if args.no_dangerously_skip_permissions
            else None,
            extra_flags=_csv(args.extra_flags),
            scope_org=args.scope_org,
            scope_repos=_csv(args.scope_repos),
            scope_snowflake_targets=_csv(args.scope_snowflake_targets),
            team=args.team,
            telegram_handle=args.telegram_handle,
            token_env=args.token_env
            or (
                f"TELEGRAM_TOKEN_{args.name.upper().replace('-', '_')}"
                if args.name
                else None
            ),
            require_mention=args.require_mention
            if args.require_mention is not None
            else True,
            chat_id=args.chat_id,
            startup_prompt=args.startup_prompt,
        )
        # If --voice-text passed, write it now.
        if args.voice_text:
            inp.voice = materialize_voice(paths, inp.name, None, args.voice_text)

    # Validation: required fields
    if not inp.name:
        log.error("--name is required")
        return 1
    if not inp.expertise:
        log.error("at least one --expertise is required")
        return 1

    # Render the stanza
    stanza = render_stanza(inp)

    print("\n=== Stanza to be added to fleet.yaml ===\n")
    print(stanza)

    if inp.team:
        log.info("  → will also be added to team '%s'.workers", inp.team)

    if args.dry_run:
        log.info(
            "--dry-run: no changes written. Stanza above would be inserted into fleet.yaml."
        )
        return 0

    # Confirm
    if not args.yes:
        ans = input("\nWrite to fleet.yaml? [Y/n]: ").strip().lower()
        if ans and ans not in ("y", "yes"):
            log.info("Aborted.")
            return 1

    # Backup fleet.yaml
    backup = paths.fleet_yaml.with_suffix(".yaml.bak")
    backup.write_text(paths.fleet_yaml.read_text())
    log.info("  ✓ Backup: %s", backup)

    # Insert
    new_text = insert_bot_stanza(paths.fleet_yaml, stanza, team=inp.team)
    paths.fleet_yaml.write_text(new_text)
    log.info("  ✓ Updated %s", paths.fleet_yaml)

    # Auto-generate
    if args.auto_generate:
        log.info("=== Running `claudlobby generate --bot %s` ===", inp.name)
        from .composer import compose_bot
        from .config import load_fleet

        fleet = load_fleet(paths.fleet_yaml)
        bot = fleet.bots.get(inp.name)
        if bot is None:
            log.error("bot '%s' not found in fleet.yaml after insertion", inp.name)
            return 1
        out_dir = compose_bot(bot, fleet, paths)
        log.info("  ✓ Composed to %s", out_dir)

    # Next steps
    log.info("=== Next steps ===")
    log.info("  1. Review %s", paths.fleet_yaml)
    if inp.token_env:
        env_set = (
            paths.env_file.is_file() and inp.token_env in paths.env_file.read_text()
        )
        if env_set:
            log.info("  2. Token already in .env ✓")
        else:
            log.info("  2. Add %s=<your-token> to %s", inp.token_env, paths.env_file)
    log.info("  3. Run: claudlobby validate")
    if not args.auto_generate:
        log.info("  4. Run: claudlobby generate --bot %s", inp.name)
    log.info("  5. Install service:")
    log.info(
        "     # Linux: sudo ln -sf %s/%s.service /etc/systemd/system/",
        paths.bot_runtime(inp.name),
        inp.name,
    )
    log.info(
        "     #        sudo systemctl daemon-reload && sudo systemctl enable --now %s.service",
        inp.name,
    )
    log.info(
        "     # macOS: ln -sf %s/%s.plist ~/Library/LaunchAgents/",
        paths.bot_runtime(inp.name),
        inp.name,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudlobby",
        description="Compositor for Claude Code agent fleets.",
    )
    parser.add_argument(
        "--root", help="Path to claudlobby repo root (auto-detected by default)"
    )
    parser.add_argument(
        "--fleet",
        help="Fleet overlay name (uses local/<fleet>/ for fleet.yaml, library overlay, voices overlay, runtime/). "
        "If omitted, runs in root mode (fleet.yaml at repo root).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Operate on the built-in seed fleet (fleet.yaml.seed). "
        "Mutually exclusive with --fleet.",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"claudlobby {__version__}"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="Validate fleet.yaml against library/")
    pv.add_argument("--strict", action="store_true", help="Fail on warnings")
    pv.set_defaults(func=cmd_validate)

    pdr = sub.add_parser(
        "doctor",
        help="Pre-flight fleet health diagnostic (env, MCP, services, creds)",
    )
    pdr.set_defaults(func=cmd_doctor)

    pg = sub.add_parser(
        "generate", help="Compose runtime/bots/ from fleet.yaml + library/"
    )
    pg.add_argument("--bot", help="Generate only one bot")
    pg.add_argument(
        "--strict", action="store_true", help="Refuse to generate on warnings"
    )
    pg.set_defaults(func=cmd_generate)

    pl = sub.add_parser(
        "list-library",
        help="List available personas, skills, mcp, guardrails, protocols, voices",
    )
    pl.set_defaults(func=cmd_list_library)

    pd = sub.add_parser(
        "diff",
        help="Show drift between runtime/bots/<bot>/ and what generate would produce",
    )
    pd.add_argument("--bot", help="Diff only one bot (default: all)")
    pd.set_defaults(func=cmd_diff)

    pp = sub.add_parser("promote", help="Promote runtime drift back to library/")
    pp.add_argument("bot", help="Bot name")
    pp.set_defaults(func=cmd_promote)

    ps = sub.add_parser("status", help="Fleet health dashboard")
    ps.add_argument("--bot", help="Show detailed status for one bot")
    ps.add_argument(
        "--json", action="store_true", dest="json", help="JSON output for scripting"
    )
    ps.set_defaults(func=cmd_status)

    pe = sub.add_parser(
        "env-migrate",
        help="Extract secrets from an existing bot setup into tiered .env files (dry-run by default)",
    )
    _add_migration_args(pe)
    pe.set_defaults(func=cmd_env_migrate)

    pdm = sub.add_parser(
        "data-migrate",
        help="Copy bot data dirs from a legacy bot setup into per-bot runtime data/ (dry-run by default)",
    )
    _add_migration_args(pdm)
    pdm.add_argument(
        "--include",
        help="Comma-separated subdir names to include (overrides auto-discovery — useful to force-copy a default-skipped dir like 'logs')",
    )
    pdm.add_argument(
        "--exclude",
        help="Comma-separated subdir names to skip (e.g. 'personal-projects,work-projects' to keep big git checkouts in place)",
    )
    pdm.set_defaults(func=cmd_data_migrate)

    pcm = sub.add_parser(
        "cron-migrate",
        help="Rewrite cron entries from a legacy bot-fleet path layout to claudlobby's (dry-run by default)",
    )
    _add_migration_args(pcm)
    pcm.set_defaults(func=cmd_cron_migrate)

    pm = sub.add_parser(
        "memory-migrate",
        help="Copy memory files from ~/.claude/projects/ to per-bot memory dirs",
    )
    pm.add_argument(
        "--map",
        nargs="*",
        help="Source-to-bot mappings (e.g. 'project-name-pattern:bot-name')",
    )
    pm.add_argument(
        "--force", action="store_true", help="Overwrite existing memory files"
    )
    pm.set_defaults(func=cmd_memory_migrate)

    pn = sub.add_parser(
        "new-bot",
        help="Interactive bot creation (or flag-driven for scripts/skills)",
    )
    pn.add_argument("--name", help="Bot name (lowercase, e.g. 'eng-1')")
    pn.add_argument("--expertise", help="Comma-separated expertise areas (required)")
    pn.add_argument(
        "--voice", help="Path to voice file (e.g. voices/erlich-bachman.md)"
    )
    pn.add_argument(
        "--voice-text", help="Inline voice description (creates voices/<name>.md)"
    )
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
    pn.add_argument(
        "--integrations",
        help="Comma-separated integrations (auto-paired with mcp by default)",
    )
    pn.add_argument(
        "--no-remote-control", action="store_true", help="Disable --remote-control flag"
    )
    pn.add_argument(
        "--no-dangerously-skip-permissions",
        action="store_true",
        help="Disable --dangerously-skip-permissions",
    )
    pn.add_argument("--extra-flags", help="Comma-separated extra claude CLI flags")
    pn.add_argument("--scope-org", help="GitHub org for scope")
    pn.add_argument("--scope-repos", help="Comma-separated repos for scope")
    pn.add_argument(
        "--scope-snowflake-targets", help="Comma-separated Snowflake targets"
    )
    pn.add_argument("--team", help="Add bot to this team's workers list")
    pn.add_argument("--telegram-handle", help="Bot @-handle (without @)")
    pn.add_argument(
        "--token-env",
        help="Env var name holding the Telegram token (defaults to TELEGRAM_TOKEN_<NAME>)",
    )
    pn.add_argument(
        "--require-mention",
        type=lambda v: v.lower() in ("true", "yes", "1"),
        default=None,
        help="true/false — Telegram requireMention",
    )
    pn.add_argument("--chat-id", help="Override default group chat_id")
    pn.add_argument("--startup-prompt", help="Custom startup prompt")
    pn.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive mode even if flags provided",
    )
    pn.add_argument(
        "--dry-run", action="store_true", help="Show stanza but don't write"
    )
    pn.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirm-before-write"
    )
    pn.add_argument(
        "--auto-generate",
        action="store_true",
        help="Run `claudlobby generate --bot <name>` after writing",
    )
    pn.set_defaults(func=cmd_new_bot)

    pns = sub.add_parser(
        "new-skill",
        help="Scaffold a new skill directory with SKILL.md template",
    )
    pns.add_argument("--name", help="Skill name (lowercase, e.g. 'deploy-status')")
    pns.add_argument("--description", help="One-line description of the skill")
    pns.add_argument(
        "--argument-hint",
        help="Argument hint (e.g. '<task> [--repo <repo>]')",
    )
    pns.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive mode even if flags provided",
    )
    pns.add_argument(
        "--dry-run", action="store_true", help="Show output but don't write"
    )
    pns.set_defaults(func=cmd_new_skill)

    png = sub.add_parser(
        "new-guardrail",
        help="Scaffold a new guardrail file with frontmatter template",
    )
    png.add_argument("--name", help="Guardrail slug (lowercase, e.g. 'no-push-main')")
    png.add_argument("--title", help="Human-readable title")
    png.add_argument("--description", help="One-line description of the rule")
    png.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive mode even if flags provided",
    )
    png.add_argument(
        "--dry-run", action="store_true", help="Show output but don't write"
    )
    png.set_defaults(func=cmd_new_guardrail)

    pw = sub.add_parser(
        "warm-cache",
        help="Pre-download npx packages for all MCP servers in fleet",
    )
    pw.add_argument(
        "--dry-run",
        action="store_true",
        help="Show packages that would be warmed without downloading",
    )
    pw.set_defaults(func=cmd_warm_cache)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
