"""Flag contract for source-only bot authoring."""


def register_bot_create(children, dispatch):
    pn = children.add_parser(
        "create",
        help="Interactive bot creation (or flag-driven for scripts/skills)",
    )
    pn.add_argument("--name", help="Bot name (lowercase, e.g. 'eng-1')")
    pn.add_argument("--expertise", help="Comma-separated expertise areas (required)")
    pn.add_argument(
        "--voice", help="Path to voice file (e.g. voices/erlich-bachman.md)"
    )
    pn.add_argument(
        "--voice-text", help="Inline voice description (creates library/voices/<name>.md)"
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
    pn.add_argument("--json", action="store_true", help="One schema-1 result object")
    pn.set_defaults(func=dispatch, public_command="bot.create")
