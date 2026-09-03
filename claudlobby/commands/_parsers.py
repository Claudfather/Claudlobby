"""Argparse subparser registration for the claudlobby CLI."""

from __future__ import annotations

from ._helpers import _add_migration_args
from .core import (
    cmd_brief,
    cmd_creds_reconcile,
    cmd_env_register,
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
from .plane import (
    cmd_emit,
    cmd_emit_batch,
    cmd_plane_doctor,
    cmd_plane_expire,
    cmd_plane_import,
    cmd_plane_parity,
    cmd_plane_shadow,
    cmd_plane_cutover,
    cmd_plane_prune,
    cmd_plane_registry,
    cmd_plane_schema,
    cmd_plane_open,
    cmd_plane_serve,
    cmd_plane_view,
    cmd_plane_spool,
    cmd_plane_status,
)
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

    pcr = sub.add_parser(
        "creds-reconcile",
        help="Reconcile declared credentials vs stored values vs equipped bots "
        "(#1104 shapes 1+2; shape 3 reports UNKNOWN by design)",
    )
    pcr.set_defaults(func=cmd_creds_reconcile)

    per = sub.add_parser(
        "env-register",
        help="Derived credential register — every declared var, the tier it "
        "resolves from, and what it shadowed (#1226)",
    )
    per.add_argument(
        "--bot", help="Include this bot's .env tier (the most specific one)"
    )
    per.add_argument("--json", action="store_true", help="Machine-readable output")
    per.set_defaults(func=cmd_env_register)

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
    pb.add_argument(
        "--boot",
        action="store_true",
        help="Render the SessionStart boot payload (#1102 R3/M1): dispatch "
        "lines + empty-state provenance + door line, token-capped — the "
        "composed hook's mode, never the full brief",
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

    # --- observable plane (Phase 1 kernel) ---
    pe = sub.add_parser("emit", help="Validated event ingest into the plane db")
    pe.add_argument("event_type", help="communication | transmission | work_item | assignment | task")
    pe.add_argument("--json", required=True, help="Request JSON path, or '-' for stdin")
    pe.set_defaults(func=cmd_emit)

    peb = sub.add_parser("emit-batch", help="Atomic multi-event unit of work (F4)")
    peb.add_argument("--json", required=True, help='{"events": [...]} path, or "-"')
    peb.set_defaults(func=cmd_emit_batch)

    pp = sub.add_parser("plane", help="Observable-plane operations")
    psub = pp.add_subparsers(dest="plane_action", required=True)
    ps = psub.add_parser("status", help="Kernel health: db, counts, spool")
    ps.set_defaults(func=cmd_plane_status)
    pd = psub.add_parser("doctor", help="Kernel health rungs (exit 1 on attention)")
    pd.set_defaults(func=cmd_plane_doctor)
    pv = psub.add_parser("serve", help="Run the ingest daemon (foreground)")
    pv.add_argument("--socket", help="Socket path override (default: state/plane/ingest.sock)")
    pv.add_argument("--drain-interval", default="600",
                    help="Seconds between spool drains (default 600)")
    pv.set_defaults(func=cmd_plane_serve)
    pvw = psub.add_parser("view", help="Run the operator-plane view daemon (read-only UI)")
    pvw.add_argument("--host", default="127.0.0.1",
                     help="Bind address (default 127.0.0.1 — Tailscale Serve fronts it; a raw address is the dev fallback)")
    pvw.add_argument("--port", type=int, default=8899, help="Bind port (default 8899)")
    pvw.set_defaults(func=cmd_plane_view)
    po = psub.add_parser("open", help="Print/launch the operator plane URL")
    po.add_argument("--port", type=int, default=8899, help="View daemon port (default 8899)")
    po.add_argument("--no-browser", action="store_true", help="Print the URL only")
    po.set_defaults(func=cmd_plane_open)
    psc = psub.add_parser("schema", help="Export JSON Schemas (envelope + families)")
    psc.set_defaults(func=cmd_plane_schema)
    ppr = psub.add_parser(
        "prune",
        help="Age out raw metric_samples past the retention window (30d;"
        " family-scoped, never the ledger)")
    ppr.add_argument("--days", type=int, default=None,
                     help="Retention window in days (default 30)")
    ppr.add_argument("--dry-run", action="store_true",
                     help="Report the count without deleting")
    ppr.set_defaults(func=cmd_plane_prune)
    pex = psub.add_parser(
        "expire",
        help="Attention expiry sweep: emit `expired` for assignments overdue"
        " past the horizon (7d; a Lane-B fact through ingest, idempotent)")
    pex.add_argument("--after-days", type=int, default=None,
                     help="Days an overdue assignment must be QUIET (no task"
                     " event) before it expires (default 7; 0 = anything overdue"
                     " and silent right now — sharp)")
    pex.add_argument("--dry-run", action="store_true",
                     help="Report the count without emitting")
    pex.set_defaults(func=cmd_plane_expire)
    ppa = psub.add_parser(
        "parity",
        help="Are the legacy ledgers fully in the plane? (dispatch-log +"
        " this fleet's report-back; rc 0 clean, 1 gaps, 3 unreachable)")
    ppa.add_argument("--since", default=None,
                     help="ISO instant; older ledger rows are outside the window")
    ppa.add_argument("--show", type=int, default=10,
                     help="Missing/duplicate rows to list per ledger (default 10)")
    ppa.add_argument("--json", action="store_true",
                     help="Schema-1 envelope on stdout (text moves to stderr)")
    ppa.set_defaults(func=cmd_plane_parity)
    pim = psub.add_parser(
        "import",
        help="Land the legacy rows the plane is missing for THIS fleet"
        " (origin=legacy, content-hashed ids, idempotent). Dry-run by default")
    pim.add_argument("--since", default=None,
                     help="ISO instant; older ledger rows are outside the window")
    pim.add_argument("--apply", action="store_true",
                     help="Write the batch (default: plan and report only)")
    pim.set_defaults(func=cmd_plane_import)
    pcut = psub.add_parser(
        "cutover",
        help="Cutover chunk 5: declare a reader's flip to the plane — refuses"
        " unless the J4 gate is met on every bot, records cutover_declared,"
        " prints the PLANE_READ_* flag line (rc 0 declared / 1 refused / 3 unreachable)")
    pcut.add_argument("--retire-writes", action="store_true",
                      help="Chunk 6b: declare the legacy JSONL writes retired (dispatch-log +"
                      " report-back) — refuses unless every reader is declared; records"
                      " legacy_write_retired; prints the PLANE_LEGACY_WRITE_*=0 lines")
    pcut.add_argument("--reader", choices=("open", "overdue", "open_task", "unassigned", "events"), default=None,
                      help="Which reader flips: the open list, the watchdog's overdue set, or the"
                      " resolver (--open-task; its bar is 200 agreeing heads + a head change)")
    pcut.add_argument("--force", default="",
                      help="Declare despite a short gate; the reason is recorded in the event")
    pcut.set_defaults(func=cmd_plane_cutover)
    psh = psub.add_parser(
        "shadow",
        help="Cutover J4 shadow: the legacy open set vs the plane's, per bot,"
        " classified; --record writes the comparison as a system event;"
        " --gate reads the streaks (rc 0 met / 1 not / 3 unreachable)")
    psh.add_argument("--bot", default=None, help="One bot (default: the fleet roster)")
    psh.add_argument("--record", action="store_true",
                     help="Record each comparison (shadow_parity_clean/diverged)")
    psh.add_argument("--gate", action="store_true",
                     help="Report the per-(bot, reader) gate from recorded comparisons")
    psh.add_argument("--check", action="store_true",
                     help="The fleet-pulse bridge: rc 1 when any (bot, reader)'s LATEST"
                     " recorded comparison diverged (unexplained, or the heads differ)")
    psh.add_argument("--reader", choices=("open", "overdue", "unassigned", "open_task", "all"), default="all",
                     help="Which reader to shadow: the deadline-blind open list, the"
                     " watchdog's overdue set, or both (default); open_task is --gate/--check"
                     " only (the resolver's head is graded inside the open records)")
    psh.add_argument("--replay-hours", type=int, default=0,
                     help="Also compare at each of the last N hourly instants"
                     " (front-loads the gate from history)")
    psh.add_argument("--skew-grace", type=int, default=600,
                     help="Seconds a row may be newer than the comparison and"
                     " still class as skew (default 600)")
    psh.add_argument("--intentional", default="",
                     help="Comma-separated task ids declared as intentional divergence")
    psh.add_argument("--show", type=int, default=5,
                     help="Unexplained divergences to list per bot (default 5)")
    psh.add_argument("--verbose", action="store_true",
                     help="Print every replayed instant, not just now")
    psh.set_defaults(func=cmd_plane_shadow)
    prg = psub.add_parser(
        "registry",
        help="Registry lane reads: current state, history, changes, verify")
    prg.add_argument("--type", choices=(
        "host", "vault", "fleet", "bot", "project", "library_item"),
        help="Filter current listing by entity type")
    # DELIBERATELY NOT --fleet: that dest is the global overlay selector
    # (_resolve_paths consumes it), and sharing it made a legal db-scope
    # query refuse unless a whole overlay existed by that name (gauntlet,
    # probed). --verify uses the GLOBAL --fleet, which it genuinely needs.
    prg.add_argument("--scope", dest="scope_fleet", metavar="FLEET",
                     help="Filter the listing by fleet scope (a db fact —"
                     " needs no overlay)")
    mode = prg.add_mutually_exclusive_group()
    mode.add_argument("--show", metavar="ALIAS",
                      help="One entity's current payload (alias or uid)")
    mode.add_argument("--history", metavar="ALIAS",
                      help="One entity's SCD windows (alias or uid)")
    mode.add_argument("--changes", type=int, nargs="?", const=20,
                      metavar="N", help="Recent field-level changes"
                      " (default 20)")
    mode.add_argument("--verify", action="store_true",
                      help="Hash-verify the projection against the"
                      " re-derived estate (root-mode fleet.yaml, or the"
                      " global --fleet <name> for an overlay)")
    prg.set_defaults(func=cmd_plane_registry)
    psp = psub.add_parser("spool", help="Inspect/drain the emit spool")
    psp.add_argument("spool_action", choices=["list", "inspect", "retry", "quarantine"])
    psp.add_argument("name", nargs="?", help="Spool file name (inspect/quarantine)")
    psp.set_defaults(func=cmd_plane_spool)
