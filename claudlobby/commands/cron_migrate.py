"""Cron migration command: rewrite cron entries from legacy bot-fleet paths to claudlobby's layout."""

from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..paths import Paths
from ._helpers import _migration_preamble

log = logging.getLogger("claudlobby")


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
) -> list[_BotCronCtx]:
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


@dataclass
class CronRewriteResult:
    """Result of rewriting a crontab string."""

    new_text: str
    rewrites: list[tuple[int, str, str, str]]  # (line_no, old, new, bot)
    unmatched_legacy: list[tuple[int, str]]  # (line_no, line)


def rewrite_crontab(
    crontab_text: str,
    bot_ctxs: list[_BotCronCtx],
    legacy_root: str,
) -> CronRewriteResult:
    """Pure function: rewrite legacy paths in a crontab string.

    Takes crontab text, bot contexts, and legacy root path. Returns a
    CronRewriteResult with the rewritten text, rewrite details, and
    unmatched legacy lines. No subprocess calls — testable in isolation.
    """
    new_lines: list[str] = []
    rewrites: list[tuple[int, str, str, str]] = []
    unmatched_legacy: list[tuple[int, str]] = []

    for i, line in enumerate(crontab_text.splitlines(), 1):
        replaced, matched_bot = _rewrite_cron_line(line, bot_ctxs)
        new_lines.append(replaced)
        if matched_bot:
            rewrites.append((i, line, replaced, matched_bot))
        elif legacy_root in line and not line.lstrip().startswith("#"):
            unmatched_legacy.append((i, line))

    new_text = "\n".join(new_lines) + (
        "\n" if new_lines and not new_lines[-1].endswith("\n") else ""
    )
    return CronRewriteResult(new_text, rewrites, unmatched_legacy)


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
    import os

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

    # Rewrite lines using pure function
    result = rewrite_crontab(current_crontab, bot_ctxs, str(source_dir))

    # Plan output
    log.info("=== cron-migrate plan ===")
    log.info("source: %s", source_dir)
    log.info("fleet:  %s", fleet.name)
    if rename_map:
        log.info("rename map: %s", rename_map)

    if not result.rewrites and not result.unmatched_legacy:
        log.info("(no crontab lines reference the source prefix — nothing to migrate)")
        return 0

    broken_paths = _verify_cron_paths(result.rewrites, paths.root)

    if result.rewrites:
        by_bot: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for line_no, old, new, bot in result.rewrites:
            by_bot[bot].append((line_no, old, new))
        for bot in sorted(by_bot):
            log.info("  %s: %d line(s) to rewrite", bot, len(by_bot[bot]))
            for line_no, old, new in by_bot[bot]:
                log.info("    line %d:", line_no)
                log.info("      − %s", old)
                log.info("      + %s", new)
        log.info(
            "Total: %d lines rewritten across %d bot(s)",
            len(result.rewrites),
            len(by_bot),
        )

    if broken_paths:
        log.warning(
            "%d rewritten path(s) don't exist at destination — verify before applying:",
            len(broken_paths),
        )
        for line_no, bot, path in broken_paths:
            log.warning("    line %d (%s): %s", line_no, bot, path)

    if result.unmatched_legacy:
        log.warning(
            "%d line(s) reference %s but no bot subpath — operator must handle:",
            len(result.unmatched_legacy),
            str(source_dir),
        )
        for line_no, line in result.unmatched_legacy:
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

    install = subprocess.run(
        ["crontab", "-"], input=result.new_text, text=True, capture_output=True
    )
    if install.returncode != 0:
        log.error("`crontab -` install failed: %s", install.stderr.strip())
        log.error("your old crontab is preserved as %s", backup_path)
        return 1
    log.info("installed new crontab (%d lines rewritten)", len(result.rewrites))
    return 0
