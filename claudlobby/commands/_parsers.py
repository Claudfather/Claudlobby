"""Argparse registration: command implementations load only on dispatch."""

from __future__ import annotations

from importlib import import_module

import argparse


def _command(module: str, name: str):
    """Defer implementation imports until argparse has accepted the call."""
    def dispatch(args):
        handler = getattr(import_module(f".{module}", __package__), name)
        return handler(args)

    return dispatch


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


def register_subparsers(sub) -> None:
    """Register all CLI subcommands on the given subparsers action."""

    from ._release_parsers import register_release_subparsers
    register_release_subparsers(sub)
    from ._orientation_parsers import register_orientation_subparsers
    register_orientation_subparsers(sub)

    pv = sub.add_parser("validate", help="Validate fleet.yaml against library/")
    pv.add_argument("--strict", action="store_true", help="Fail on warnings")
    pv.add_argument(
        "--warn-baseline",
        metavar="FILE",
        help="Fail (rc 1) only on a warning category that is new or has grown"
        " since FILE was written; rc 2 when FILE cannot be read. For a fleet"
        " that has accepted some warnings and so cannot use --strict",
    )
    pv.add_argument(
        "--write",
        action="store_true",
        help="With --warn-baseline: record this run's warning categories to FILE",
    )
    pv.set_defaults(func=_command("core", "cmd_validate"))

    pcr = sub.add_parser(
        "creds-reconcile",
        help="Reconcile declared credentials vs stored values vs equipped bots "
        "(#1104 shapes 1+2; shape 3 reports UNKNOWN by design)",
    )
    pcr.set_defaults(func=_command("core", "cmd_creds_reconcile"))

    per = sub.add_parser(
        "env-register",
        help="Derived credential register — every declared var, the tier it "
        "resolves from, and what it shadowed (#1226)",
    )
    per.add_argument(
        "--bot", help="Include this bot's .env tier (the most specific one)"
    )
    per.add_argument("--json", action="store_true", help="Machine-readable output")
    per.set_defaults(func=_command("core", "cmd_env_register"))

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
    pfb.set_defaults(func=_command("core", "cmd_freshbox"))

    pg = sub.add_parser(
        "generate", help="Compose runtime/bots/ from fleet.yaml + library/"
    )
    pg.add_argument("--bot", help="Generate only one bot")
    pg.add_argument(
        "--strict", action="store_true", help="Refuse to generate on warnings"
    )
    pg.set_defaults(func=_command("core", "cmd_generate"))

    pht = sub.add_parser(
        "host-timers",
        help="Compose host-global timer units from system.yaml host.jobs",
    )
    pht.set_defaults(func=_command("core", "cmd_host_timers"))

    phj = sub.add_parser(
        "host-job",
        help="Print one host job as this host runs it (packaged + host override), as JSON",
    )
    phj.add_argument("name", help="the host job, e.g. pull-root")
    phj.set_defaults(func=_command("core", "cmd_host_job"))

    pl = sub.add_parser(
        "list-library",
        help="List available personas, skills, mcp, guardrails, protocols, voices",
    )
    pl.set_defaults(func=_command("core", "cmd_list_library"))

    pd = sub.add_parser(
        "diff",
        help="Show drift between runtime/bots/<bot>/ and what generate would produce",
    )
    pd.add_argument("--bot", help="Diff only one bot (default: all)")
    pd.set_defaults(func=_command("core", "cmd_diff"))

    pp = sub.add_parser("promote", help="Promote runtime drift back to library/")
    pp.add_argument("bot", help="Bot name")
    pp.set_defaults(func=_command("core", "cmd_promote"))

    ps = sub.add_parser("status", help="Fleet health dashboard")
    ps.add_argument("--bot", help="Show detailed status for one bot")
    ps.add_argument(
        "--json", action="store_true", dest="json", help="JSON output for scripting"
    )
    ps.set_defaults(func=_command("core", "cmd_status"))

    pb = sub.add_parser(
        "brief",
        help="Read a bot's active fleet mission, canonical work, workstreams, reports and alerts",
    )
    pb.add_argument("--bot", help="Read this active-fleet bot's view (defaults to caller or fleet manager)")
    pb.add_argument("--json", action="store_true", help="One schema-1 result containing a schema-2 brief")
    pb.add_argument("--usage-since", metavar="DURATION",
                    help="Include bounded viewer transcript usage for up to 7d (for example 24h)")
    pb.add_argument(
        "--boot",
        action="store_true",
        help="Render the SessionStart boot payload (#1102 R3/M1): work "
        "lines + empty-state provenance + door line, token-capped — the "
        "composed hook's mode, never the full brief",
    )
    from ..command_result import execute

    def _brief_dispatch(args):
        return execute("brief", lambda: _command("brief_read", "dispatch")(args),
                       json_output=args.json)

    pb.set_defaults(func=_brief_dispatch, public_command="brief")

    from ._workstream_parsers import register_workstream_subparsers
    register_workstream_subparsers(sub)

    from ._checkin_parsers import register_checkin_subparsers
    register_checkin_subparsers(sub)

    pt = sub.add_parser("task", help="Read and operate on fleet-owned work")
    t_sub = pt.add_subparsers(dest="task_action", required=True)

    from ._task_read_parsers import register_task_read_subparsers
    assignment_children = register_task_read_subparsers(sub, t_sub)
    from ._task_write_parsers import register_task_write_subparsers
    register_task_write_subparsers(t_sub, assignment_children)

    tick = sub.add_parser("_task-recheck-tick", help=argparse.SUPPRESS)
    tick.add_argument("tick_fleet", metavar="FLEET")
    tick.set_defaults(func=_command("task_recheck", "tick"))

    from ._message_read_parsers import register_message_read_subparsers
    message_children = register_message_read_subparsers(sub)
    from ._message_write_parsers import register_message_write_subparsers
    register_message_write_subparsers(message_children)
    from ._request_read_parsers import register_request_read_subparsers
    register_request_read_subparsers(sub)

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
    pu.set_defaults(func=_command("core", "cmd_uptime"))

    pe = sub.add_parser(
        "env-migrate",
        help="Extract secrets from an existing bot setup into tiered .env files (dry-run by default)",
    )
    _add_migration_args(pe)
    pe.set_defaults(func=_command("env_migrate", "cmd_env_migrate"))

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
    pdm.set_defaults(func=_command("data_migrate", "cmd_data_migrate"))

    pcm = sub.add_parser(
        "cron-migrate",
        help="Rewrite cron entries from a legacy bot-fleet path layout to claudlobby's (dry-run by default)",
    )
    _add_migration_args(pcm)
    pcm.set_defaults(func=_command("cron_migrate", "cmd_cron_migrate"))

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
    pm.set_defaults(func=_command("memory_migrate", "cmd_memory_migrate"))

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
    plm.set_defaults(func=_command("lessons_migrate", "cmd_lessons_migrate"))

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
    pn.set_defaults(func=_command("scaffolding", "cmd_new_bot"))

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
    pns.set_defaults(func=_command("scaffolding", "cmd_new_skill"))

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
    png.set_defaults(func=_command("scaffolding", "cmd_new_guardrail"))

    pw = sub.add_parser(
        "warm-cache",
        help="Pre-download npx and uvx packages for all MCP servers in fleet",
    )
    pw.add_argument(
        "--dry-run",
        action="store_true",
        help="Show packages that would be warmed without downloading",
    )
    pw.set_defaults(func=_command("core", "cmd_warm_cache"))

    pev = sub.add_parser(
        "events",
        help="Tail/filter the fleet's events on the plane",
    )
    pev.add_argument("--bot", help="Filter by bot name")
    pev.add_argument(
        "--type", help="Filter by event type (e.g. service_down, tool_call)"
    )
    pev.add_argument(
        "--source", help="Filter by the emitting script (vitals, pulse, keepalive, lib)"
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
    pev.add_argument(
        "--since",
        help="Only events since a window or instant: 24h, 7d, 30m, or an ISO instant",
    )
    pev.add_argument("--json", action="store_true", help="Output raw JSONL")

    pev.set_defaults(func=_command("events", "cmd_events"))

    # --- observable plane (Phase 1 kernel) ---
    pe = sub.add_parser("emit", help="Validated event ingest into the plane db")
    pe.add_argument("event_type", help="communication | transmission | work_item | assignment | task")
    pe.add_argument("--json", required=True, help="Request JSON path, or '-' for stdin")
    pe.set_defaults(func=_command("plane", "cmd_emit"))

    peb = sub.add_parser("emit-batch", help="Atomic multi-event unit of work (F4)")
    peb.add_argument("--json", required=True, help='{"events": [...]} path, or "-"')
    peb.set_defaults(func=_command("plane", "cmd_emit_batch"))

    pp = sub.add_parser("plane", help="Observable-plane operations")
    psub = pp.add_subparsers(dest="plane_action", required=True)
    ps = psub.add_parser("status", help="Kernel health: db, counts, spool")
    ps.set_defaults(func=_command("plane", "cmd_plane_status"))
    pd = psub.add_parser("doctor", help="Kernel health rungs (exit 1 on attention)")
    pd.set_defaults(func=_command("plane", "cmd_plane_doctor"))
    pv = psub.add_parser("serve", help="Run the ingest daemon (foreground)")
    pv.add_argument("--socket", help="Socket path override (default: state/plane/ingest.sock)")
    pv.add_argument("--drain-interval", default="600",
                    help="Seconds between spool drains (default 600)")
    pv.set_defaults(func=_command("plane", "cmd_plane_serve"))
    pvw = psub.add_parser("view", help="Run the operator-plane view daemon (read-only UI)")
    pvw.add_argument("--host", default="127.0.0.1",
                     help="Bind address (default 127.0.0.1 — Tailscale Serve fronts it; a raw address is the dev fallback)")
    pvw.add_argument("--port", type=int, default=8899, help="Bind port (default 8899)")
    pvw.set_defaults(func=_command("plane", "cmd_plane_view"))
    po = psub.add_parser("open", help="Print/launch the operator plane URL")
    po.add_argument("--port", type=int, default=8899, help="View daemon port (default 8899)")
    po.add_argument("--no-browser", action="store_true", help="Print the URL only")
    po.set_defaults(func=_command("plane", "cmd_plane_open"))
    psc = psub.add_parser("schema", help="Export JSON Schemas (envelope + families)")
    psc.set_defaults(func=_command("plane", "cmd_plane_schema"))
    ppr = psub.add_parser(
        "prune",
        help="Age out raw metric_samples past the retention window (30d;"
        " family-scoped, never the ledger)")
    ppr.add_argument("--days", type=int, default=None,
                     help="Retention window in days (default 30)")
    ppr.add_argument("--dry-run", action="store_true",
                     help="Report the count without deleting")
    ppr.set_defaults(func=_command("plane", "cmd_plane_prune"))
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
    pex.set_defaults(func=_command("plane", "cmd_plane_expire"))
    piw = psub.add_parser(
        "import-workstreams",
        help="#1635: one-shot import of a pre-cutover workstreams.json into"
        " the plane, with original instants (never the import instant)",
    )
    piw.add_argument(
        "--file", default=None,
        help="Path to the residual registry file (default: <fleet runtime>/workstreams.json)",
    )
    piw.add_argument(
        "--dry-run", action="store_true",
        help="Print the full envelope plan and emit nothing",
    )
    piw.add_argument(
        "--archive", action="store_true",
        help="Rename the source file to <name>.imported-<batch> on success",
    )
    piw.set_defaults(func=_command("plane", "cmd_plane_import_workstreams"))
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
    prg.set_defaults(func=_command("plane", "cmd_plane_registry"))
    psp = psub.add_parser("spool", help="Inspect/drain the emit spool")
    psp.add_argument("spool_action", choices=["list", "inspect", "retry", "quarantine"])
    psp.add_argument("name", nargs="?", help="Spool file name (inspect/quarantine)")
    psp.set_defaults(func=_command("plane", "cmd_plane_spool"))
