"""Argparse subparser registration for the claudlobby CLI."""

from __future__ import annotations

from ._helpers import _add_migration_args
from .core import (
    cmd_brief,
    cmd_diff,
    cmd_doctor,
    cmd_freshbox,
    cmd_generate,
    cmd_host_timers,
    cmd_list_library,
    cmd_promote,
    cmd_report_back,
    cmd_workstreams,
    cmd_status,
    cmd_uptime,
    cmd_validate,
    cmd_warm_cache,
)
from .cron_migrate import cmd_cron_migrate
from .data_migrate import cmd_data_migrate
from .env_migrate import cmd_env_migrate
from .lessons_migrate import cmd_lessons_migrate
from .memory_migrate import cmd_memory_migrate
from .move_bot import cmd_move_bot
from .scaffolding import cmd_new_bot, cmd_new_guardrail, cmd_new_skill


def register_subparsers(sub) -> None:
    """Register all CLI subcommands on the given subparsers action."""

    pv = sub.add_parser("validate", help="Validate fleet.yaml against library/")
    pv.add_argument("--strict", action="store_true", help="Fail on warnings")
    pv.set_defaults(func=cmd_validate)

    pdr = sub.add_parser(
        "doctor",
        help="Pre-flight fleet health diagnostic (env, MCP, services, creds)",
    )
    pdr.set_defaults(func=cmd_doctor)

    pfb = sub.add_parser(
        "freshbox",
        help="Fresh-box self-containment audit — grants trace to sources, "
        "Tier-A composed (#644 P4)",
    )
    pfb.add_argument("--bot", help="Audit only one bot")
    pfb.add_argument(
        "--strict", action="store_true", help="Fail on advisory warnings too"
    )
    pfb.add_argument(
        "--reap",
        action="store_true",
        help="Remove stale orphan supervision units (short-form <bot>.plist)",
    )
    pfb.set_defaults(func=cmd_freshbox)

    pg = sub.add_parser(
        "generate", help="Compose runtime/bots/ from fleet.yaml + library/"
    )
    pg.add_argument("--bot", help="Generate only one bot")
    pg.add_argument(
        "--strict", action="store_true", help="Refuse to generate on warnings"
    )
    pg.set_defaults(func=cmd_generate)

    pht = sub.add_parser(
        "host-timers",
        help="Compose host-global timer units from system.yaml host.jobs",
    )
    pht.set_defaults(func=cmd_host_timers)

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

    pb = sub.add_parser(
        "brief",
        help="One read door over fleet state for a bot (mission, dispatches, "
        "workstreams, unacked reports, alerts)",
    )
    pb.add_argument("--bot", required=True, help="Bot to brief (required in v1)")
    pb.add_argument("--json", action="store_true", help="Schema-1 JSON envelope")
    pb.add_argument(
        "--ack",
        action="store_true",
        help="Advance this bot's report cursor past everything just shown "
        "(the only write this command performs)",
    )
    pb.set_defaults(func=cmd_brief)

    pws = sub.add_parser(
        "workstreams",
        help="Read-only view of the fleet workstream registry",
    )
    pws.set_defaults(func=cmd_workstreams, ws_command="list")
    ws_sub = pws.add_subparsers(dest="ws_command")
    ws_sub.add_parser("list", help="List all workstreams (default)")
    pws_show = ws_sub.add_parser("show", help="Show one workstream by id")
    pws_show.add_argument("id", help="Workstream id (e.g. ws-ship-the-widget)")

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

    plm = sub.add_parser(
        "lessons-migrate",
        help="Migrate referential library/lessons/ into the Claudron vault via "
        "`claudron capture` (dry-run by default; behavior-class lessons stay put)",
    )
    plm.add_argument(
        "--apply",
        action="store_true",
        help="Write to the vault via `claudron capture` (default: dry-run plan)",
    )
    plm.add_argument(
        "--vault",
        help="Target vault path for --apply (falls back to CLAUDRON_VAULT_PATH)",
    )
    plm.add_argument(
        "--vault-fleet",
        dest="fleet_scope",
        help="Capture into a fleet tier instead of the default _shared/ hub",
    )
    plm.add_argument(
        "--claudron-bin",
        dest="claudron_bin",
        help="Path to the claudron executable (default: `claudron` on PATH)",
    )
    plm.set_defaults(func=cmd_lessons_migrate)

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
        "--dangerously-skip-permissions",
        action="store_true",
        help="Opt in to --dangerously-skip-permissions (default: conservative acceptEdits)",
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
        help="Remove source bot directory after move (default: left in place, "
        "which orphans the source dir — move-bot warns when this is omitted)",
    )
    pmb.add_argument(
        "--force",
        action="store_true",
        help="Override pre-flight checks (e.g. active tmux session)",
    )
    pmb.set_defaults(func=cmd_move_bot)

    pev = sub.add_parser(
        "events",
        help="Tail/filter JSONL events across all bots",
    )
    pev.add_argument("--bot", help="Filter by bot name")
    pev.add_argument(
        "--type", help="Filter by event type (e.g. service_down, tool_call)"
    )
    pev.add_argument(
        "--source", help="Filter by source (vitals, pulse, keepalive, lib)"
    )
    pev.add_argument(
        "--critical",
        action="store_true",
        help="Show only critical events (service_down, session_missing, etc.)",
    )
    pev.add_argument(
        "--tail",
        type=int,
        default=50,
        help="Show last N events (default: 50)",
    )
    pev.add_argument("--json", action="store_true", help="Output raw JSONL")

    from .events import cmd_events  # local import to avoid circular at module top-level

    pev.set_defaults(func=cmd_events)
