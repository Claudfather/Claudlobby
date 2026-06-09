"""claudlobby CLI entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .commands._helpers import _add_migration_args
from .commands.core import (
    cmd_diff,
    cmd_doctor,
    cmd_generate,
    cmd_list_library,
    cmd_promote,
    cmd_report_back,
    cmd_status,
    cmd_uptime,
    cmd_validate,
    cmd_warm_cache,
)
from .commands.cron_migrate import cmd_cron_migrate
from .commands.data_migrate import cmd_data_migrate
from .commands.env_migrate import cmd_env_migrate
from .commands.memory_migrate import cmd_memory_migrate
from .commands.move_bot import cmd_move_bot
from .commands.scaffolding import cmd_new_bot, cmd_new_guardrail, cmd_new_skill

log = logging.getLogger("claudlobby")


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

    prb = sub.add_parser(
        "report-back",
        help="Query the report-back ledger (bot work events)",
    )
    prb.add_argument("--bot", help="Filter by bot name")
    prb.add_argument(
        "--status", help="Filter by status (completed/progress/blocked/failed)"
    )
    prb.add_argument(
        "--since",
        help="Show events since (e.g. 24h, 7d, 30m, or ISO timestamp)",
    )
    prb.add_argument(
        "--json", action="store_true", help="Output raw JSONL instead of table"
    )
    prb.set_defaults(func=cmd_report_back)

    pu = sub.add_parser(
        "uptime",
        help="Per-bot uptime, MTBR, and restart-rate from keepalive logs",
    )
    pu.add_argument("--bot", help="Show metrics for one bot only")
    pu.add_argument(
        "--window",
        choices=["24h", "7d", "30d"],
        help="Time window (default: show all three in JSON, 24h for table)",
    )
    pu.add_argument("--json", action="store_true", dest="json", help="JSON output")
    pu.set_defaults(func=cmd_uptime)

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

    pmb = sub.add_parser(
        "move-bot",
        help="Move a bot between fleets (copy state, re-enroll service)",
    )
    pmb.add_argument("bot", help="Bot name to move")
    pmb.add_argument("--to", required=True, help="Target fleet name")
    pmb.add_argument(
        "--from",
        dest="from_fleet",
        help="Source fleet name (auto-detected if omitted)",
    )
    pmb.add_argument(
        "--apply",
        action="store_true",
        help="Execute the move (default: dry-run preview)",
    )
    pmb.add_argument(
        "--cleanup-source",
        action="store_true",
        help="Remove source bot directory after move",
    )
    pmb.add_argument(
        "--force",
        action="store_true",
        help="Override pre-flight checks (e.g. active tmux session)",
    )
    pmb.set_defaults(func=cmd_move_bot)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
