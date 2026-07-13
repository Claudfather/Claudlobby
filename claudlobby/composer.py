"""Template-driven composition.

Reads `templates/claude.md.j2`, loads library files via the frontmatter-aware
loader, renders the bot's CLAUDE.md. Library files are pure content — the
template owns all top-level structure.
"""

from __future__ import annotations
import copy
import json
import logging
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from . import dotenv
from .config import BotConfig, FleetConfig, load_host_jobs
from .known_values import HEADLESS_TRIM_VARS
from .loader import (
    LibraryItem,
    _demote_headings,
    load_library_items_overlay,
    load_voice,
    parse_expertise_file,
)
from .mcp_resolve import iter_operator_contract_vars, resolve_placeholders
from .paths import Paths


# ----------------------------------------------------------------------
# Templating
# ----------------------------------------------------------------------


def _expand(text: str, ctx: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders. Used for non-Jinja substitution within
    library bodies (e.g., `{{BOT_NAME}}` inside a protocol). Missing keys
    are left as-is."""
    out = text
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def _bot_template_context(
    bot: BotConfig, fleet: FleetConfig, paths: Paths
) -> dict[str, str]:
    bot_dir = paths.bot_runtime(bot.bot_id)
    return {
        "BOT_ID": bot.bot_id,
        "BOT_ID_UPPER": bot.bot_id.upper(),
        "BOT_NAME": bot.name,
        "BOT_NAME_UPPER": bot.name.upper(),
        "FLEET_NAME": fleet.name,
        "SERVICE_PREFIX": fleet.service_prefix,
        "CLAUDLOBBY_ROOT": str(paths.root),
        "BOT_DIR": str(bot_dir),
        "TELEGRAM_GROUP_CHAT_ID": (
            bot.telegram.chat_id or fleet.telegram_group_chat_id or ""
        ),
        "SHARED_DOCS_PATH": str(paths.shared_docs) if paths.shared_docs else "",
    }


def _expand_item(item: LibraryItem, ctx: dict[str, str]) -> LibraryItem:
    return LibraryItem(
        title=_expand(item.title, ctx),
        description=_expand(item.description, ctx) if item.description else None,
        body=_expand(item.body, ctx),
        source_path=item.source_path,
    )


# ----------------------------------------------------------------------
# Expertise composition (special — provides H1 label + base body)
# ----------------------------------------------------------------------


def _compose_expertise(
    bot: BotConfig, paths: Paths, ctx: dict[str, str]
) -> tuple[str | None, str]:
    """Return (title_label, expertise_body).

    First expertise file's H1 (if present) provides the title_label. Subsequent
    expertise files' H1s are stripped; their bodies are concatenated.

    Overlay-aware: looks in `local/<fleet>/library/expertise/` first, then
    in the public `library/expertise/`.
    """
    if not bot.expertise:
        raise ValueError(f"bot '{bot.bot_id}': expertise list is empty")

    first_label: str | None = None
    body_chunks: list[str] = []
    for i, area in enumerate(bot.expertise):
        path = paths.find_library_file("expertise", area, ".md")
        if path is None:
            continue
        item = parse_expertise_file(path)
        if item is None:
            continue
        if i == 0:
            first_label = item.title_label
        body_chunks.append(item.body)
    expertise_body = "\n\n".join(b for b in body_chunks if b).rstrip()
    return first_label, _expand(expertise_body, ctx)


# ----------------------------------------------------------------------
# Jinja2 environment
# ----------------------------------------------------------------------


def _build_jinja_env(paths: Paths) -> jinja2.Environment:
    """Jinja env with overlay-aware template loader.

    A fleet can override the master template by placing
    `local/<fleet>/templates/claude.md.j2`.
    """
    search = []
    if paths.fleet_dir and (paths.fleet_dir / "templates").is_dir():
        search.append(str(paths.fleet_dir / "templates"))
    search.append(str(paths.root / "templates"))
    env = SandboxedEnvironment(
        loader=jinja2.FileSystemLoader(search),
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["quote_backtick"] = lambda s: f"`{s}`"
    return env


# ----------------------------------------------------------------------
# .mcp.json merge
# ----------------------------------------------------------------------


def compose_mcp_json(bot: BotConfig, paths: Paths) -> dict:
    import shutil

    merged: dict = {"mcpServers": {}}
    for entry in bot.mcp:
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise ValueError(f"invalid JSON in MCP fragment {frag_path}: {e}") from e
        contract = frag.pop("_env_contract", {})
        global_binary = frag.pop("_global_binary", None)

        # Find the server config (the non-underscore key)
        server_key = None
        server_config = None
        for k, v in frag.items():
            if not k.startswith("_") and isinstance(v, dict):
                server_key = k
                server_config = v
                break
        if server_key is None:
            continue

        # Generate one server entry per instance
        for instance in entry.instances:
            if instance == "default":
                output_name = entry.name
            else:
                output_name = f"{entry.name}-{instance}"

            instance_config = copy.deepcopy(server_config)

            # Use global binary if available (saves ~0.8s npx overhead per server)
            resolved_binary = shutil.which(global_binary) if global_binary else None
            if resolved_binary and instance_config.get("command") == "npx":
                instance_config["command"] = "node"
                # Replace npx args ([-y, pkg, ...rest]) with [binary, ...rest]
                npx_args = instance_config.get("args", [])
                rest_args = []
                skip_next = False
                for a in npx_args:
                    if a == "-y":
                        skip_next = True
                        continue
                    if skip_next:
                        skip_next = False
                        continue
                    rest_args.append(a)
                instance_config["args"] = [resolved_binary] + rest_args

            # Resolve ${VAR} placeholders (instance-scoped vars get prefixed)
            for field in ["env", "url", "args", "headers"]:
                if field in instance_config:
                    instance_config[field] = resolve_placeholders(
                        instance_config[field], contract, entry, instance
                    )

            merged["mcpServers"][output_name] = instance_config

    return merged


def _resolve_mcp_permissions(bot: BotConfig, paths: Paths) -> list[str]:
    """Resolve MCP permission patterns from fragment _permissions_contract fields.

    For each MCP entry the bot uses, reads the fragment, extracts
    _permissions_contract.tools, and generates permission patterns per instance.

    Wildcard compression: when the bot is allowed all tools in the contract
    (i.e., no partial restriction), emit a single ``mcp__<server>__*`` wildcard
    instead of one entry per tool.  This keeps settings.local.json compact and
    prevents staleness when MCP servers add new tools.

    Individual patterns are still emitted when only a subset of the contract
    tools should be allowed (not yet wired up, but the logic is ready).
    """
    patterns: list[str] = []
    for entry in bot.mcp:
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except json.JSONDecodeError as exc:
            _log.warning(
                "skipping MCP permissions for %s: malformed JSON: %s", frag_path, exc
            )
            continue
        contract = frag.get("_permissions_contract", {})
        tools = contract.get("tools", [])
        if not tools:
            continue
        for instance in entry.instances:
            if instance == "default":
                output_name = entry.name
            else:
                output_name = f"{entry.name}-{instance}"
            # Emit a server-level wildcard when ALL contract tools are allowed.
            # This is always the case currently (no partial allow mechanism),
            # so the wildcard is emitted for every server with a non-empty tools list.
            patterns.append(f"mcp__{output_name}__*")
    return patterns


# ----------------------------------------------------------------------
# bot.conf
# ----------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
_SHELL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Telegram handle: must start alnum/underscore, then alnum/underscore/dash.
# One canonical rule used by both compose_bot_conf and compose_bot.
_TELEGRAM_HANDLE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*\Z")

# Pinned fleet-wide tmux tmpdir. Per-bot sockets are reached via
# `tmux -L <socket>`, which resolves to "$TMUX_TMPDIR/tmux-$(id -u)/<socket>".
# Pinning one explicit value (tmux's own built-in default is /tmp) guarantees
# every script and unit resolves the same `-L <name>` to the same server —
# drift here would silently spawn a second server for the same socket name.
_TMUX_TMPDIR = "/tmp"


def _shq(v: object) -> str:
    """Shell-quote a value for safe embedding in sourced bash scripts.

    Uses shlex.quote (single-quoting) so $, `, \\, and " are literal.
    """
    return shlex.quote(str(v))


def compose_bot_conf(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    # Defense-in-depth: validate identifiers embedded in double-quoted lines
    # that require shell variable expansion ($HOME, $CLAUDLOBBY_ROOT).
    if not _SAFE_NAME_RE.match(bot.bot_id):
        raise ValueError(f"bot_id {bot.bot_id!r} contains shell-unsafe characters")
    if not _SAFE_NAME_RE.match(bot.name):
        raise ValueError(f"bot name {bot.name!r} contains shell-unsafe characters")
    tg_handle = (bot.telegram.handle if bot.telegram else None) or bot.bot_id
    if not _TELEGRAM_HANDLE_RE.match(tg_handle):
        raise ValueError(
            f"telegram handle {tg_handle!r} contains shell-unsafe characters"
        )

    ctx = _bot_template_context(bot, fleet, paths)
    bot_dir = paths.bot_runtime(bot.bot_id)
    account_dir = fleet.accounts.get(
        bot.account, fleet.accounts.get("default", "~/.claude")
    )

    # When bot_dir is inside claudlobby root, use $CLAUDLOBBY_ROOT-relative path.
    # When outside (vault mode), use absolute path.
    try:
        bot_dir_rel = bot_dir.relative_to(paths.root)
        bot_dir_line = f'BOT_DIR="$CLAUDLOBBY_ROOT/{bot_dir_rel}"'
    except ValueError:
        bot_dir_line = f"BOT_DIR={_shq(str(bot_dir))}"
    # BOT_SERVICE is the bot's host-wide-unique, fleet-prefixed identity. It
    # names the systemd/launchd unit AND the per-bot tmux server socket — one
    # value, two of the three identity axes (the third is the dir-slug session).
    bot_service = f"{fleet.service_prefix}.{bot.bot_id}"
    lines = [
        "# Generated by claudlobby — do not hand-edit. Edit fleet.yaml + library/, then re-run `claudlobby generate`.",
        f"# Bot: {bot.bot_id}",
        "",
        f"export CLAUDLOBBY_ROOT={_shq(str(paths.root))}",
        "",
        f"export BOT_ID={_shq(bot.bot_id)}",
        f"BOT_NAME={_shq(bot.name)}",
        f"BOT_SERVICE={_shq(bot_service)}",
        # Per-bot tmux server socket (the `-L` name) — equals BOT_SERVICE so one
        # server's death drops only this bot. Resolved for peer bots via
        # tmux_socket_for_bot() (lib/lib-common.sh).
        f"TMUX_SOCKET={_shq(bot_service)}",
        f"BOT_LABEL={_shq(bot.bot_id.upper())}",
        bot_dir_line,
        f'TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{bot.telegram.handle or bot.bot_id}"',
        "",
        "# Claude Code config dir (multi-account support)",
    ]
    if bot.account != "default":
        lines.append(f"CLAUDE_CONFIG_DIR={_shq(account_dir)}")
    else:
        lines.append(f"# CLAUDE_CONFIG_DIR={_shq(account_dir)}  # default account")
    lines.append("")

    # Assemble the full claude CLI flag set. lib/start-bot.sh reads
    # CLAUDE_FLAGS verbatim and appends only --name <session>.
    flags: list[str] = []
    for ch in bot.channels:
        flags.append(f"--channels {ch}")
    if bot.remote_control:
        flags.append("--remote-control")
    # Permission model, in precedence order:
    #   1. explicit permission_mode always wins;
    #   2. else an explicit dangerously_skip_permissions opt-in bypasses prompts;
    #   3. else the conservative default — acceptEdits auto-accepts edits while
    #      still honoring the composed allow/deny lists (headless-safe, and unlike
    #      dangerously-skip it does not bypass the cross-bot read isolation).
    if bot.permission_mode:
        flags.append(f"--permission-mode {bot.permission_mode}")
    elif bot.dangerously_skip_permissions:
        flags.append("--dangerously-skip-permissions")
    else:
        flags.append("--permission-mode acceptEdits")
    if bot.model:
        flags.append(f"--model {bot.model}")
    if bot.effort:
        flags.append(f"--effort {bot.effort}")
    flags.extend(bot.extra_flags)
    lines.append(f"CLAUDE_FLAGS={_shq(' '.join(flags))}")
    lines.append("")

    # Prompt suggestions — off by default for headless bots.
    lines.append(
        f"export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION={_shq(str(bot.prompt_suggestions).lower())}"
    )
    # Headless traffic trim — kills the satisfaction survey, /bug, Sentry
    # error reporting, and the auto-updater (claudlobby self-manages updates).
    # Granular by design: see HEADLESS_TRIM_VARS / RC_KILLING_ENV_VARS for why
    # the umbrella vars stay off (they break remote-control, #533). Presence
    # flags — an override to false omits them, never emits "=0".
    if bot.disable_nonessential_traffic:
        for var in HEADLESS_TRIM_VARS:
            lines.append(f"export {var}=1")
    lines.append("")
    lines.append("# Exports for skills + scripts")
    lines.append(f"export FLEET_NAME={_shq(fleet.name)}")
    lines.append(f"export SERVICE_PREFIX={_shq(fleet.service_prefix)}")
    # Pin the tmux tmpdir so `tmux -L <socket>` resolves to the same server in
    # every bot process (start parent, session, watchdog, cross-socket peers) —
    # drift would silently spawn a duplicate server for the same socket name.
    lines.append(f"export TMUX_TMPDIR={_shq(_TMUX_TMPDIR)}")
    lines.append('export FLEET_STATE_PATH="$CLAUDLOBBY_ROOT/state/fleet-state.json"')
    if fleet.mission_file and fleet.mission:
        # Gated on the PAIR, mirroring the CLAUDE.md section: in the
        # pairing-forbidden state (file without paragraph) no composed prose
        # references the var, so emitting it would dangle. Resolved at
        # compose time (the config field stays fleet-relative): consumers
        # just read the path — no bot has to re-derive the fleet layout,
        # which breaks in vault mode. Same anchored-path pattern as
        # FLEET_STATE_PATH above.
        charter_path = paths.fleet_config_dir / fleet.mission_file
        lines.append(f"export FLEET_MISSION_FILE={_shq(str(charter_path))}")
    chat_id = ctx["TELEGRAM_GROUP_CHAT_ID"]
    if chat_id:
        lines.append(f"export TELEGRAM_GROUP_CHAT_ID={_shq(str(chat_id))}")
    if bot.telegram.token_env:
        lines.append(f"export TELEGRAM_TOKEN_ENV_NAME={_shq(bot.telegram.token_env)}")
    if bot.telegram.require_mention is not None:
        lines.append(
            f"export TELEGRAM_REQUIRE_MENTION={_shq(str(bot.telegram.require_mention).lower())}"
        )
    if bot.telegram.handle:
        lines.append(f"export TELEGRAM_BOT_HANDLE={_shq(bot.telegram.handle)}")

    # Model strategy — config-driven model escalation / compaction / subagent models.
    if bot.model_strategy:
        ms = bot.model_strategy
        lines.append("")
        lines.append("# Model strategy")
        if ms.base:
            lines.append(f"export MODEL_STRATEGY_BASE={_shq(ms.base)}")
        if ms.escalate_to:
            lines.append(f"export MODEL_STRATEGY_ESCALATE_TO={_shq(ms.escalate_to)}")
        if ms.escalate_when:
            lines.append(
                f"export MODEL_STRATEGY_ESCALATE_WHEN={_shq(ms.escalate_when)}"
            )
        if ms.compact_when:
            lines.append(f"export MODEL_STRATEGY_COMPACT_WHEN={_shq(ms.compact_when)}")
        # Subagent model preferences (from raw extras)
        for key in ("explore", "plan", "general"):
            val = ms.raw.get(key)
            if val:
                lines.append(f"export MODEL_STRATEGY_{key.upper()}={_shq(val)}")

    # Observability — pulse interval and event retention.
    # Values may be None when system defaults are disabled via system_defaults: false.
    obs = bot.observability
    if any(
        v is not None
        for v in [
            obs.pulse_interval,
            obs.reap_days,
            obs.activity_stuck_threshold,
            obs.dispatch_deadline,
            obs.bridge_heal,
            obs.bridge_heal_max_attempts,
        ]
    ):
        lines.append("")
        lines.append("# Observability")
        if obs.pulse_interval is not None:
            lines.append(
                f"export OBSERVABILITY_PULSE_INTERVAL={_shq(obs.pulse_interval)}"
            )
        if obs.reap_days is not None:
            lines.append(f"export OBSERVABILITY_REAP_DAYS={_shq(obs.reap_days)}")
        if obs.activity_stuck_threshold is not None:
            lines.append(
                f"export OBSERVABILITY_ACTIVITY_STUCK_THRESHOLD={_shq(obs.activity_stuck_threshold)}"
            )
        if obs.dispatch_deadline is not None:
            lines.append(
                f"export OBSERVABILITY_DISPATCH_DEADLINE={_shq(obs.dispatch_deadline)}"
            )
        if obs.bridge_heal is not None:
            # keepalive.sh gates on the string "1"; emit a shell boolean (1/0),
            # NOT _shq(bool) which renders "True"/"False" and would leave the
            # gate closed.
            lines.append(
                f"export OBSERVABILITY_BRIDGE_HEAL={'1' if obs.bridge_heal else '0'}"
            )
        if obs.bridge_heal_max_attempts is not None:
            lines.append(
                f"export BRIDGE_HEAL_MAX_ATTEMPTS={_shq(obs.bridge_heal_max_attempts)}"
            )

    # Project validation tiers (projects.yaml) — the repo -> closure-bar map,
    # emitted into EVERY bot's conf: any sprint/runner bot must resolve a
    # working repo's tier locally (there is no "sprint owner" concept).
    if fleet.projects:
        lines.append("")
        lines.append("# Projects (projects.yaml) — repo -> validation tier")
        for key, project in sorted(fleet.projects.items()):
            tier_var = f"PROJECT_TIER_{project.env_slug}"
            if not _SHELL_IDENT_RE.match(tier_var):
                raise ValueError(
                    f"project key '{key}' does not yield a valid env name "
                    f"(run claudlobby validate)"
                )
            for repo in project.repos:
                # Emit-time corruption backstop (mirrors the slug raise
                # above): the value is a space-delimited list by contract,
                # so an entry containing whitespace cannot be represented.
                if any(c.isspace() for c in repo):
                    raise ValueError(
                        f"project '{key}': repos entry '{repo}' contains "
                        f"whitespace (run claudlobby validate)"
                    )
            lines.append(f"export {tier_var}={_shq(project.validation.tier)}")
            lines.append(
                f"export PROJECT_REPOS_{project.env_slug}="
                f"{_shq(' '.join(project.repos))}"
            )

    # Workstream registry bounds (fleet.workstreams). Read from the env by the
    # single-writer helper (lib/workstream-update.sh) at open/renew time.
    # Emitted into EVERY bot.conf rather than manager-gated so the fleet-wide
    # cap/lease applies regardless of team topology — a teamless fleet would
    # silently lose its config under a manager-only gate. Defaults (12/14) apply
    # when the fleet omits the block.
    lines.append("")
    lines.append("# Workstream registry (fleet.workstreams)")
    lines.append(f"export WORKSTREAM_MAX_ACTIVE={_shq(fleet.workstreams.max_active)}")
    lines.append(f"export WORKSTREAM_LEASE_DAYS={_shq(fleet.workstreams.lease_days)}")

    # Rolling code-audit sweep — emitted only into the owner bot's conf, so the
    # fleet-level selector (lib/code-audit-sweep.sh) resolves exactly one owner.
    # repos default to the owner's scope.repos when sweep.repos is unset.
    if fleet.sweep_enabled() and fleet.sweep.owner_bot == bot.bot_id:
        sweep_repos = fleet.sweep.repos or (bot.scope.repos if bot.scope else [])
        lines.append("")
        lines.append("# Code-audit sweep (owner)")
        lines.append(f"export SWEEP_OWNER_BOT={_shq(bot.bot_id)}")
        lines.append(f"export SWEEP_REPOS={_shq(' '.join(sweep_repos))}")
        lines.append(f"export SWEEP_LABEL={_shq(fleet.sweep.label)}")
        lines.append(
            f"export SWEEP_AUDIT_TYPES={_shq(' '.join(fleet.sweep.audit_types))}"
        )

    # Ecosystem — clauDNA version pin, Claudron vault, Claudosseum tenant
    if bot.claudna_version or bot.claudron_vault_path or bot.claudosseum_tenant_id:
        lines.append("")
        lines.append("# Ecosystem")
        if bot.claudna_version:
            lines.append(f"export CLAUDNA_VERSION={_shq(bot.claudna_version)}")
        if bot.claudron_vault_path:
            lines.append(f"export CLAUDRON_VAULT_PATH={_shq(bot.claudron_vault_path)}")
        if bot.claudosseum_tenant_id:
            lines.append(
                f"export CLAUDOSSEUM_TENANT_ID={_shq(bot.claudosseum_tenant_id)}"
            )

    # Plugin sync — if fleet declares plugins, enable auto-install on session start
    if fleet.plugins.required:
        lines.append('export CLAUDE_CODE_SYNC_PLUGIN_INSTALL="1"')
        lines.append(
            f"export FLEET_PLUGINS_REQUIRED={_shq(' '.join(fleet.plugins.required))}"
        )
    if fleet.plugins.marketplaces:
        # Encode as "Name=github:Owner/Repo Name2=github:Owner2/Repo2"
        pairs = []
        for name, meta in fleet.plugins.marketplaces.items():
            src = meta.get("source", {})
            src_type = src.get("source", "github")
            src_repo = src.get("repo", "")
            pairs.append(f"{name}={src_type}:{src_repo}")
        lines.append(f"export FLEET_PLUGINS_MARKETPLACES={_shq(' '.join(pairs))}")

    for k, v in bot.env.items():
        if not _SHELL_IDENT_RE.match(k):
            raise ValueError(f"bot.env key {k!r} is not a valid shell identifier")
        lines.append(f"export {k}={_shq(v)}")

    lines.append("")

    for team in fleet.teams.values():
        if bot.bot_id in team.workers:
            lines.append(f"export MANAGER_TMUX={_shq(team.manager)}")
            # The manager's private tmux socket — mirrors MANAGER_TMUX, mapped to
            # the manager's BOT_SERVICE so report-back / pulse / sprint sends
            # reach the manager's own server (see bot_tmux_send).
            lines.append(
                f"export MANAGER_TMUX_SOCKET={_shq(f'{fleet.service_prefix}.{team.manager}')}"
            )
            break
    if bot.bot_id in fleet.manager_bots():
        lines.append(f"export MANAGER_TMUX={_shq(bot.bot_id)}  # this bot is a manager")
        lines.append(f"export MANAGER_TMUX_SOCKET={_shq(bot_service)}")

    lines.append("")
    if bot.startup_prompt:
        rendered = _render_startup_prompt(bot.startup_prompt, bot, fleet)
        lines.append(f"STARTUP_PROMPT={json.dumps(rendered)}")
    else:
        lines.append(
            'STARTUP_PROMPT="Welcome back. Read your CLAUDE.md. Idle and await Telegram messages."'
        )

    return "\n".join(lines) + "\n"


def _render_startup_prompt(prompt: str, bot: BotConfig, fleet: FleetConfig) -> str:
    """Render jinja placeholders in startup_prompt against fleet/bot context.

    Exposed variables (each is an empty string when not configured):
      - {{ bot_name }}                — bot.name
      - {{ fleet_name }}              — fleet.name
      - {{ telegram_group_chat_id }}  — fleet.telegram_group_chat_id
      - {{ telegram_handle }}         — bot.telegram.handle

    Lets fleet.yaml authors stop hand-duplicating identifiers — e.g.
    chat_id literals that already live in fleet.telegram_group_chat_id.
    """
    env = SandboxedEnvironment()
    return env.from_string(prompt).render(
        bot_id=bot.bot_id,
        bot_name=bot.name,
        fleet_name=fleet.name,
        telegram_group_chat_id=fleet.telegram_group_chat_id or "",
        telegram_handle=(bot.telegram.handle if bot.telegram else "") or "",
    )


# ----------------------------------------------------------------------
# Service units
# ----------------------------------------------------------------------


def compose_systemd_unit(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, *, boot_delay_s: int = 0
) -> str:
    bot_dir = paths.bot_runtime(bot.bot_id)
    bot_service = f"{fleet.service_prefix}.{bot.bot_id}"
    stagger = f"\nExecStartPre=/bin/sleep {boot_delay_s}" if boot_delay_s > 0 else ""
    return f"""# Generated by claudlobby — do not hand-edit.
[Unit]
Description=claudlobby bot: {bot.name} ({fleet.name})
After=network-online.target

[Service]
Type=simple
# start-bot.sh exits 0 after spawning tmux. Without these two, the default
# control-group cleanup kills the tmux server we just started. KillMode=process
# limits the kill to the main process; RemainAfterExit=yes keeps the unit
# "active" while tmux runs underneath.
RemainAfterExit=yes
KillMode=process
WorkingDirectory={bot_dir}{stagger}
ExecStart={paths.lib}/start-bot.sh {bot_dir}
# kill-server (not kill-session): this bot owns its tmux server. kill-server
# tears the whole server down deterministically; kill-session leaves the emptied
# server orphaned unless tmux's exit-empty default reaps it.
ExecStop=/bin/sh -c 'tmux -L {bot_service} kill-server 2>/dev/null || true'
ExecStopPost=/bin/rm -f {bot_dir}/.tmux-env
# Restart= here only fires on non-zero exit of start-bot.sh — i.e., a config
# failure before tmux ever spawned. Tmux dying after we've gone "active" is
# detected by lib/keepalive.sh, NOT by systemd, because exit 0 + RemainAfterExit
# leaves the unit looking healthy regardless of what tmux is doing.
Restart=on-failure
RestartSec=5
Environment=CLAUDLOBBY_ROOT={paths.root}
Environment=TMUX_TMPDIR={_TMUX_TMPDIR}

[Install]
WantedBy=default.target
"""


# NOTE: launchd has no ExecStartPre equivalent for boot stagger.
# On macOS, fleet boot contention is less of an issue (Mac Mini has
# more cores/RAM than Pi). If needed, stagger can be added via a
# BOOT_DELAY env var honored by start-bot.sh itself.
def compose_launchd_plist(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    bot_dir = paths.bot_runtime(bot.bot_id)
    label = f"{fleet.service_prefix}.{bot.bot_id}"
    log_dir = paths.lib / "logs"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by claudlobby — do not hand-edit. -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{paths.lib}/start-bot.sh</string>
        <string>{bot_dir}</string>
    </array>
    <key>WorkingDirectory</key><string>{bot_dir}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>StandardOutPath</key><string>{log_dir}/{bot.bot_id}.out.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/{bot.bot_id}.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDLOBBY_ROOT</key><string>{paths.root}</string>
        <key>TMUX_TMPDIR</key><string>{_TMUX_TMPDIR}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key><string>{Path.home()}</string>
    </dict>
</dict>
</plist>
"""


# ----------------------------------------------------------------------
# Skill symlinks
# ----------------------------------------------------------------------


def link_skills(bot: BotConfig, paths: Paths, log) -> None:
    """Symlink each skill into the bot's runtime .claude/skills/.

    Overlay-aware: looks in `local/<fleet>/library/skills/` first, then in
    public `library/skills/`.

    Each skill entry can be:
      - `name`       — single skill at `skills/name/`
      - `dir/name`   — nested skill at `skills/dir/name/`
      - `dir/`       — folder expansion: every skill subdir under `skills/dir/`
                       (recursive; overlay wins per leaf name).

    Symlinks are created under the skill's leaf name in `.claude/skills/`
    (Claude Code looks up skills by directory name, not nested path).
    Collisions (two source dirs with the same leaf) are first-wins; the
    second is logged and skipped.
    """
    bot_skills_dir = paths.bot_runtime(bot.bot_id) / ".claude" / "skills"
    if bot_skills_dir.exists():
        for entry in bot_skills_dir.iterdir():
            if entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
    bot_skills_dir.mkdir(parents=True, exist_ok=True)

    linked: dict[str, Path] = {}  # leaf name → source dir, for collision detection

    def _add(leaf: str, src: Path) -> None:
        if leaf in linked:
            log(f"  skill '{leaf}' already linked from {linked[leaf]} — skipping {src}")
            return
        linked[leaf] = src
        (bot_skills_dir / leaf).symlink_to(src.resolve())

    for skill in bot.skills:
        if skill.endswith("/"):
            dir_name = skill.rstrip("/")
            collected = paths.expand_skill_folder(dir_name)
            if not collected:
                log(f"  skill folder '{skill}' empty or missing — skipped")
                continue
            for leaf, src in collected.items():
                _add(leaf, src)
        else:
            src = paths.find_skill_dir(skill)
            if src is None:
                log(f"  skill '{skill}' missing — skipped")
                continue
            _add(src.name, src)


def link_mounts(bot: BotConfig, bot_dir: Path, log) -> None:
    """Create symlinks for declared mounts.

    Each mount maps a local name to an absolute host path.
    Symlinks are placed under bot_dir/mounts/<name>.
    Stale symlinks (removed from config) are cleaned up.
    """
    mounts_dir = bot_dir / "mounts"
    mounts_dir.mkdir(exist_ok=True)

    # Clean stale symlinks
    for entry in mounts_dir.iterdir():
        if entry.is_symlink() and entry.name not in bot.mounts:
            entry.unlink()

    for name, target in bot.mounts.items():
        target_path = Path(target).expanduser()
        try:
            resolved = target_path.resolve()
            if not resolved.is_relative_to(Path.home()) and not resolved.is_relative_to(
                bot_dir
            ):
                log(
                    f"  mount '{name}': target {target_path} escapes home and bot dir — skipping"
                )
                continue
        except (ValueError, OSError):
            log(f"  mount '{name}': could not resolve target {target_path} — skipping")
            continue
        link = mounts_dir / name
        if link.is_symlink():
            if link.resolve() == target_path.resolve():
                continue  # already correct
            link.unlink()
        elif link.exists():
            log(f"  mount '{name}': non-symlink already exists at {link} — skipping")
            continue
        if not target_path.exists():
            log(
                f"  mount '{name}' target does not exist: {target_path} — creating dangling symlink"
            )
        link.symlink_to(target_path)


# ----------------------------------------------------------------------
# Org structure
# ----------------------------------------------------------------------


def _compose_org_structure(bot: BotConfig, fleet: FleetConfig) -> str | None:
    """Render the org-structure block for CLAUDE.md when reports_to/manages is set."""
    if not bot.reports_to and not bot.manages:
        return None

    def _ref(bot_id: str) -> str:
        b = fleet.bots.get(bot_id)
        if b and b.name != b.bot_id:
            return f"{b.name} (`{bot_id}`)"
        return bot_id

    lines: list[str] = []
    if bot.reports_to:
        lines.append(f"- **Reports to:** {_ref(bot.reports_to)}")
    if bot.manages:
        lines.append("- **Direct reports:**")
        for mid in bot.manages:
            lines.append(f"  - {_ref(mid)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Telegram access.json
# ----------------------------------------------------------------------


def compose_access_json(bot: BotConfig, fleet: FleetConfig) -> dict | None:
    """Generate Telegram channel access.json from fleet.yaml.

    Returns None if the bot has no telegram config. The generated config
    controls DM policy, group requireMention, and human allowlisting.

    Written to ~/.claude/channels/telegram-<handle>/access.json so the
    Telegram plugin picks up correct settings on first boot — preventing
    the default-config bug where requireMention defaults to false.
    """
    if not bot.telegram or not bot.telegram.handle:
        return None

    chat_id = bot.telegram.chat_id or fleet.telegram_group_chat_id
    if not chat_id:
        return None

    access: dict = {
        "dmPolicy": "allowlist",
        "allowFrom": [],
        "groups": {
            chat_id: {
                "requireMention": bot.telegram.require_mention,
                "allowFrom": [],
            }
        },
        "pending": {},
    }

    if fleet.human_telegram_id:
        access["allowFrom"] = [fleet.human_telegram_id]

    return access


# ----------------------------------------------------------------------
# Integration resolution
# ----------------------------------------------------------------------


def resolve_effective_integrations(bot: BotConfig, paths: Paths) -> list[str]:
    """Return the list of integration names to use for a bot.

    If bot.integrations is explicitly set, use it verbatim. Otherwise,
    derive from bot.mcp: any MCP entry whose name has a matching
    integrations/<name>.md file (in the overlay or base library) is
    auto-paired. This avoids requiring authors to repeat integration names
    that are already implied by the MCP config.
    """
    if bot.integrations:
        return bot.integrations
    return [
        e.name
        for e in bot.mcp
        if paths.find_library_file("integrations", e.name, ".md") is not None
    ]


# ----------------------------------------------------------------------
# CLAUDE.md composition (template-driven)
# ----------------------------------------------------------------------


def compose_claude_md(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    ctx = _bot_template_context(bot, fleet, paths)

    title_label, expertise_body = _compose_expertise(bot, paths, ctx)

    voice_item: LibraryItem | None = None
    if bot.voice:
        voice_path = paths.find_voice_file(bot.voice)
        if voice_path is not None:
            voice_item = load_voice(voice_path)
            if voice_item is not None:
                voice_item = _expand_item(voice_item, ctx)

    def _items(names: list[str], kind: str) -> list[LibraryItem]:
        return [
            _expand_item(it, ctx)
            for it in load_library_items_overlay(names, paths, kind)
        ]

    integration_names = resolve_effective_integrations(bot, paths)

    # Auto-include shared-documentation protocol when shared docs are available
    protocol_names = list(bot.protocols)
    if paths.shared_docs and "shared-documentation" not in protocol_names:
        protocol_names.append("shared-documentation")

    teams = fleet.teams_for_manager(bot.bot_id)
    org_structure = _compose_org_structure(bot, fleet)

    is_manager = bot.bot_id in fleet.manager_bots()

    # Projects table composes for managers only (F6-style context budget:
    # workers resolve tiers from the bot.conf env map, not prose).
    projects = (
        sorted(fleet.projects.values(), key=lambda p: p.key) if is_manager else []
    )
    for p in projects:
        # Emit-time corruption backstop (validator owns the UX error): a
        # newline in a title would inject fake sections into the composed
        # instructions; a pipe breaks the table.
        if "\n" in p.title or "|" in p.title:
            raise ValueError(
                f"project '{p.key}': title contains newline or '|' — refusing "
                f"to render it into CLAUDE.md (run claudlobby validate)"
            )

    # Fleet-mission extra content under the paragraph, decided in ONE place:
    # managers compose the full charter body (headings demoted, like every
    # composed library body); workers — and a manager whose charter file is
    # missing (validator already warned; benign absence) — get a pointer to
    # $FLEET_MISSION_FILE. None when no mission_file is configured.
    fleet_mission_extra = None
    if fleet.mission and "\n" in fleet.mission.strip():
        # Emit-time corruption backstop (validator owns the UX error): a
        # multi-line mission would inject sections into EVERY bot's
        # composed instructions.
        raise ValueError(
            "fleet.mission contains newlines — refusing to render it into "
            "CLAUDE.md (run claudlobby validate)"
        )
    if fleet.mission_file:
        charter = paths.fleet_config_dir / fleet.mission_file
        if is_manager and charter.is_file():
            # Double demote: the body nests under the H2 "## Fleet Mission"
            # section, so the charter's own H1 must land at H3 (a single
            # demote would leave it an H2 sibling, escaping the section).
            fleet_mission_extra = _demote_headings(
                _demote_headings(charter.read_text())
            )
        else:
            fleet_mission_extra = (
                "Full charter: read $FLEET_MISSION_FILE when picking or "
                "prioritizing work."
            )

    env = _build_jinja_env(paths)
    template = env.get_template("claude.md.j2")
    rendered = template.render(
        bot=bot,
        fleet=fleet,
        title_label=title_label,
        expertise_body=expertise_body,
        voice=voice_item,
        teams=teams,
        projects=projects,
        fleet_mission_extra=fleet_mission_extra,
        org_structure=org_structure,
        shared_docs_path=str(paths.shared_docs) if paths.shared_docs else None,
        resources=_items(bot.resources, "resources"),
        integrations=_items(integration_names, "integrations"),
        principles=_items(bot.principles, "principles"),
        permissions=_items(bot.permissions, "permissions"),
        protocols=_items(protocol_names, "protocols"),
        guardrails=_items(bot.guardrails, "guardrails"),
        lessons=_items(bot.lessons, "lessons"),
        post_actions=_items(bot.post_actions, "post_actions"),
    )
    # Collapse 3+ blank lines → 2 to keep output tidy.
    while "\n\n\n\n" in rendered:
        rendered = rendered.replace("\n\n\n\n", "\n\n\n")
    return rendered


# ----------------------------------------------------------------------
# Main entry: compose one bot
# ----------------------------------------------------------------------


def _compose_hooks(hooks: dict[str, list[dict[str, Any]]]) -> dict[str, list]:
    """Transform fleet.yaml hook entries into Claude Code settings.local.json format.

    Fleet.yaml uses a flat format per entry:
        {command: "script.sh", matcher: "Bash", timeout: 10}

    Claude Code expects nested matcher groups:
        [{matcher: "Bash", hooks: [{type: "command", command: "script.sh", timeout: 10}]}]

    Entries with the same matcher within one event are grouped together.
    """
    if not hooks:
        return {}
    out: dict[str, list] = {}
    for event, entries in hooks.items():
        if not entries:
            continue
        # Group entries by matcher value
        by_matcher: dict[str, list[dict]] = {}
        for entry in entries:
            matcher = entry.get("matcher", "")
            if matcher not in by_matcher:
                by_matcher[matcher] = []
            # Build the hook object — everything except "matcher" is a hook field
            hook = {k: v for k, v in entry.items() if k != "matcher"}
            if "type" not in hook:
                hook["type"] = "command"
            by_matcher[matcher].append(hook)
        # Build matcher groups
        groups = []
        for matcher, hook_list in by_matcher.items():
            group: dict[str, Any] = {"hooks": hook_list}
            if matcher:
                group["matcher"] = matcher
            groups.append(group)
        out[event] = groups
    return out


_TELEGRAM_PLUGIN_TOOLS = [
    "mcp__plugin_telegram_telegram__reply",
    "mcp__plugin_telegram_telegram__edit_message",
    "mcp__plugin_telegram_telegram__react",
    "mcp__plugin_telegram_telegram__download_attachment",
]


def _resolve_channel_permissions(bot: BotConfig) -> list[str]:
    """Auto-derive tool permissions from configured channels.

    When a bot has a Telegram handle set, include the 4 plugin tools.
    """
    tools: list[str] = []
    if bot.telegram.handle:
        tools.extend(_TELEGRAM_PLUGIN_TOOLS)
    return tools


def _resolve_skill_permissions(bot: BotConfig) -> list[str]:
    """Auto-derive Skill() permission patterns from bot's skill list.

    Each skill needs both Skill(<name>) and Skill(<name>:*) for full operation.
    """
    patterns: list[str] = []
    for skill in bot.skills:
        patterns.append(f"Skill({skill})")
        patterns.append(f"Skill({skill}:*)")
    return patterns


def _resolve_expertise_permissions(
    bot: BotConfig,
    paths: Paths,
) -> tuple[list[str], list[str]]:
    """Merge permission profiles from all expertise files for a bot.

    Returns (allow_patterns, deny_patterns). For each expertise:
    - allow_all expands to a broad tool set
    - allow lists are unioned
    - deny lists are unioned
    - bash_allow entries become Bash(<cmd> *) patterns in the allow list

    Deny wins over allow at the same layer — if a tool appears in both,
    it stays in deny and is removed from allow.
    """
    ALL_TOOLS = [
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Agent",
        "Grep",
        "Glob",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
    ]

    merged_allow: list[str] = []
    merged_deny: list[str] = []
    merged_bash: list[str] = []
    has_allow_all = False

    for area in bot.expertise:
        path = paths.find_library_file("expertise", area, ".md")
        if path is None:
            continue
        item = parse_expertise_file(path)
        if item is None or item.permissions is None:
            continue
        p = item.permissions
        if p.allow_all:
            has_allow_all = True
        for t in p.allow:
            if t not in merged_allow:
                merged_allow.append(t)
        for t in p.deny:
            if t not in merged_deny:
                merged_deny.append(t)
        for cmd in p.bash_allow:
            if cmd not in merged_bash:
                merged_bash.append(cmd)

    if has_allow_all:
        for t in ALL_TOOLS:
            if t not in merged_allow:
                merged_allow.append(t)

    # Deny wins at this layer
    merged_allow = [t for t in merged_allow if t not in merged_deny]

    allow_patterns = list(merged_allow)
    for cmd in merged_bash:
        allow_patterns.append(f"Bash({cmd} *)")

    deny_patterns = list(merged_deny)
    return allow_patterns, deny_patterns


# Minimal base tools every bot needs for read-only operation.
# Expertise profiles add Write, Edit, Bash, etc. in Phase 2.
BASE_TOOLS = ["Read", "Grep", "Glob"]


def compose_settings_local(
    bot: BotConfig,
    fleet: FleetConfig,
    paths: Paths,
    mcp_server_names: list[str] | None = None,
) -> dict:
    """Generate .claude/settings.local.json with memory dir, sibling isolation, tools, sandbox, and hooks.

    ``mcp_server_names`` is the bot's composed project MCP-server set (the keys of
    :func:`compose_mcp_json`), threaded in from :func:`compose_bot` so it is computed
    once. When non-empty it is emitted as ``enabledMcpjsonServers`` — a per-server trust
    allowlist that pre-approves exactly the fleet-configured servers, so a headless
    ``claude`` boot never stalls on the interactive MCP-approval prompt (``--permission-mode
    auto`` does not answer it). Because this file is fully overwritten every generate, this
    key is what makes MCP trust durable: it is re-derived each run rather than preserved as
    runtime state. The set comes from fleet config, not the on-disk ``.mcp.json``, so a
    server absent from fleet config stays untrusted and a regenerate drops any entry added
    to the on-disk file (fails closed). The blanket ``enableAllProjectMcpServers`` is
    deliberately NOT emitted: it would trust any server present on disk, including one
    carried by a checked-out repo's ``.mcp.json``.
    """
    bot_dir = paths.bot_runtime(bot.bot_id)
    memory_dir = str(bot_dir / "memory")

    settings: dict = {
        "autoMemoryDirectory": memory_dir,
    }

    # Build permissions block — layered composition
    deny_patterns: list[str] = []

    # Layer 0: Sibling isolation — deny reading other bots' files
    siblings = [bid for bid in fleet.bots if bid != bot.bot_id]
    for sibling in siblings:
        sibling_dir = str(paths.bot_runtime(sibling))
        deny_patterns.append(f"Read({sibling_dir}/**)")

    # Layer 2: Expertise permissions (from library/expertise/ frontmatter)
    expertise_allow, expertise_deny = _resolve_expertise_permissions(bot, paths)
    deny_patterns.extend(expertise_deny)

    # Layer 7: Bot-level deny (from fleet.yaml tools.deny) — wins over everything
    for tool in bot.tools.deny:
        deny_patterns.append(tool)

    # Build allow list: layers 2-7
    allow_patterns: list[str] = []

    # Layer 2: Expertise allow (plain tool names + bash command patterns)
    allow_patterns.extend(expertise_allow)

    # Layer 3: MCP tool contracts (auto-derived from fragment _permissions_contract)
    allow_patterns.extend(_resolve_mcp_permissions(bot, paths))

    # Layer 4: Channel/plugin tools (auto-derived from config)
    allow_patterns.extend(_resolve_channel_permissions(bot))

    # Layer 5: Skill patterns (auto-derived from bot.skills)
    allow_patterns.extend(_resolve_skill_permissions(bot))

    # Layer 6/7: Explicit tools.allow from fleet defaults + bot config
    for tool in bot.tools.allow:
        if tool not in allow_patterns:
            allow_patterns.append(tool)

    # Bot-level deny wins over all allow layers
    bot_deny_plain = set(bot.tools.deny)
    allow_patterns = [p for p in allow_patterns if p not in bot_deny_plain]

    # Ensure base tools are present whenever allow list is non-empty
    # (settings.local.json allow REPLACES global — without these, bots lose basic access)
    if allow_patterns:
        for t in BASE_TOOLS:
            if t not in allow_patterns and t not in bot_deny_plain:
                allow_patterns.insert(0, t)

    if deny_patterns:
        permissions: dict = {"deny": deny_patterns}
        if allow_patterns:
            permissions["allow"] = allow_patterns
        settings["permissions"] = permissions
    elif allow_patterns:
        settings["permissions"] = {
            "allow": allow_patterns,
        }

    # Sandbox: enabled toggle + network + filesystem allowlists + bash auto-allow
    sandbox_cfg: dict = {}
    sandbox = bot.sandbox
    if sandbox.enabled is not None:
        sandbox_cfg["enabled"] = sandbox.enabled
    if sandbox.network_allowed_domains:
        sandbox_cfg["network"] = {"allowedDomains": sandbox.network_allowed_domains}
    if sandbox.filesystem_allow_write:
        sandbox_cfg["filesystem"] = {"allowWrite": list(sandbox.filesystem_allow_write)}
    if sandbox.auto_allow_bash is not None:
        sandbox_cfg["autoAllowBashIfSandboxed"] = sandbox.auto_allow_bash

    # Inject shared docs write access
    if paths.shared_docs:
        fs = sandbox_cfg.setdefault("filesystem", {})
        aw = fs.setdefault("allowWrite", [])
        shared_str = str(paths.shared_docs)
        if shared_str not in aw:
            aw.append(shared_str)

    if sandbox_cfg:
        settings["sandbox"] = sandbox_cfg

    # Hooks: PreToolUse, PostToolUse, etc.
    hooks = _compose_hooks(bot.hooks)
    if hooks:
        settings["hooks"] = hooks

    # Plugins — enabledPlugins + extraKnownMarketplaces from fleet config
    if fleet.plugins.required:
        settings["enabledPlugins"] = {plugin: True for plugin in fleet.plugins.required}
    if fleet.plugins.marketplaces:
        settings["extraKnownMarketplaces"] = dict(fleet.plugins.marketplaces)

    # MCP trust allowlist — see docstring for the derive-and-fail-closed rationale.
    if mcp_server_names:
        settings["enabledMcpjsonServers"] = sorted(mcp_server_names)

    # Headless UX defaults (fleet.yaml-overridable per bot): silence the spinner
    # tip lines, disable the notification prompts, and prefer reduced motion — safe
    # now that keepalive liveness is marker-based rather than spinner-based.
    settings["spinnerTipsEnabled"] = bot.spinner_tips_enabled
    settings["preferredNotifChannel"] = bot.preferred_notif_channel
    settings["prefersReducedMotion"] = bot.prefers_reduced_motion

    return settings


def _reconcile_access_json(
    access_path: Path,
    fresh: dict,
    bot: BotConfig,
    fleet: FleetConfig,
    log=None,
) -> None:
    """Update fleet-derived fields in an existing access.json, preserving runtime state.

    Fleet-controlled fields (updated every generate):
      - dmPolicy, groups.<chat_id>.requireMention, allowFrom (human ID)

    Runtime state (preserved):
      - pending, extra groups added at runtime, extra allowFrom entries
    """
    try:
        existing = json.loads(access_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        msg = f"  WARNING: {access_path} is unreadable ({exc}), leaving unchanged"
        _log.warning("%s is unreadable: %s, leaving unchanged", access_path, exc)
        if log is not None:
            log(msg)
        return

    if not isinstance(existing, dict):
        msg = f"  WARNING: {access_path} is not a JSON object, leaving unchanged"
        _log.warning("%s is not a JSON object, leaving unchanged", access_path)
        if log is not None:
            log(msg)
        return

    chat_id = bot.telegram.chat_id or fleet.telegram_group_chat_id
    existing["dmPolicy"] = fresh["dmPolicy"]

    if chat_id:
        existing.setdefault("groups", {})
        if chat_id in existing["groups"]:
            existing["groups"][chat_id]["requireMention"] = bot.telegram.require_mention
        else:
            existing["groups"][chat_id] = {
                "requireMention": bot.telegram.require_mention,
                "allowFrom": [],
            }

    if fleet.human_telegram_id:
        allow = existing.setdefault("allowFrom", [])
        if fleet.human_telegram_id not in allow:
            allow.append(fleet.human_telegram_id)

    access_path.write_text(json.dumps(existing, indent=2) + "\n")


def compose_bot(
    bot: BotConfig, fleet: FleetConfig, paths: Paths, log=None, *, boot_delay_s: int = 0
) -> Path:
    bot_dir = paths.bot_runtime(bot.bot_id)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / ".claude").mkdir(exist_ok=True)
    (bot_dir / "memory").mkdir(exist_ok=True)
    (bot_dir / "projects").mkdir(exist_ok=True)
    (bot_dir / "data").mkdir(exist_ok=True)
    (bot_dir / "data" / "events").mkdir(exist_ok=True)
    (bot_dir / "logs").mkdir(exist_ok=True)

    (bot_dir / "CLAUDE.md").write_text(compose_claude_md(bot, fleet, paths))

    mcp = compose_mcp_json(bot, paths)
    (bot_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")

    (bot_dir / "bot.conf").write_text(compose_bot_conf(bot, fleet, paths))

    settings_local = compose_settings_local(
        bot, fleet, paths, list(mcp["mcpServers"].keys())
    )
    (bot_dir / ".claude" / "settings.local.json").write_text(
        json.dumps(settings_local, indent=2) + "\n"
    )

    _emit = log if log is not None else _log.info
    link_skills(bot, paths, _emit)
    link_mounts(bot, bot_dir, _emit)

    # Telegram access.json — write to channel state dir so the plugin
    # picks up correct requireMention/dmPolicy on first boot.
    access = compose_access_json(bot, fleet)
    if access is not None:
        handle = bot.telegram.handle
        # compose_bot_conf already enforced this rule and raised; defensive re-check.
        if not _TELEGRAM_HANDLE_RE.match(handle):
            _log.warning(
                "bot %s has invalid telegram handle %r, skipping access.json",
                bot.bot_id,
                handle,
            )
            if log is not None:
                log(
                    f"  WARNING: bot {bot.bot_id} has invalid telegram handle {handle!r}, skipping access.json"
                )
        else:
            channel_dir = Path.home() / ".claude" / "channels" / f"telegram-{handle}"
            channel_dir.mkdir(parents=True, exist_ok=True)
            access_path = channel_dir / "access.json"
            if access_path.exists():
                _reconcile_access_json(access_path, access, bot, fleet, log)
            else:
                access_path.write_text(json.dumps(access, indent=2) + "\n")

    (bot_dir / f"{fleet.service_prefix}.{bot.bot_id}.service").write_text(
        compose_systemd_unit(bot, fleet, paths, boot_delay_s=boot_delay_s)
    )
    (bot_dir / f"{fleet.service_prefix}.{bot.bot_id}.plist").write_text(
        compose_launchd_plist(bot, fleet, paths)
    )

    return bot_dir


@dataclass
class EnvVar:
    """A single env var requirement from a library contract."""

    name: str
    description: str
    tier: str  # "fleet" or "bot"
    source: str  # e.g. "mcp/github", "integration/neon", "telegram"


def collect_env_contracts(fleet: FleetConfig, paths: Paths) -> list[EnvVar]:
    """Walk fleet config, collect all env var contracts. Instance-aware."""
    from .loader import parse_frontmatter

    vars: dict[str, EnvVar] = {}

    seen_mcp: set[str] = set()
    for bot in fleet.bots.values():
        for entry in bot.mcp:
            if entry.name in seen_mcp:
                continue
            seen_mcp.add(entry.name)
            frag_path = paths.find_library_file("mcp", entry.name, ".json")
            if frag_path is None:
                continue
            frag = json.loads(frag_path.read_text())
            contract = frag.get("_env_contract", {})
            # Operator-facing vars only: the shared kernel skips
            # provided_by:composer (a composer-emitted var like CLAUDRON_VAULT_PATH
            # would otherwise scaffold a misleading empty .env stub and false-alarm
            # doctor) and applies instance naming — one home so validate + this
            # collection can never drift again (#568, finishes #233). Dedup by
            # canonical name and the .env description label are this consumer's job.
            for cv in iter_operator_contract_vars(contract, entry):
                if cv.canonical_name in vars:
                    continue
                inst_label = (
                    f" ({cv.instance})" if cv.instance not in (None, "default") else ""
                )
                vars[cv.canonical_name] = EnvVar(
                    name=cv.canonical_name,
                    description=f"{cv.description}{inst_label}",
                    tier=cv.tier,
                    source=f"mcp/{entry.name}",
                )

        # Integration doc contracts
        integration_names = resolve_effective_integrations(bot, paths)
        for int_name in integration_names:
            int_path = paths.find_library_file("integrations", int_name, ".md")
            if int_path is None:
                continue
            fm, _ = parse_frontmatter(int_path.read_text())
            contract = fm.get("env_contract", {})
            if not contract:
                continue
            for var_name, meta in contract.items():
                tier = meta.get("tier", "fleet")
                if var_name not in vars:
                    vars[var_name] = EnvVar(
                        name=var_name,
                        description=meta.get("description", ""),
                        tier=tier,
                        source=f"integration/{int_name}",
                    )

        # Telegram token_env
        if bot.telegram and bot.telegram.token_env:
            var_name = bot.telegram.token_env
            if var_name not in vars:
                vars[var_name] = EnvVar(
                    name=var_name,
                    description="Telegram bot token",
                    tier="bot",
                    source="telegram",
                )

    return list(vars.values())


def _scaffold_env_merge(
    env_path: Path,
    header: str,
    required: list[EnvVar],
    log=None,
) -> None:
    """Idempotent .env scaffold: preserve existing values, append new stubs.

    Reads the file (if it exists), keeps every existing line verbatim, then
    appends stub entries for any contract vars not already present.
    Re-running is safe: no values are lost and new vars always surface.
    """
    existing_keys: set[str] = set()
    existing_content = ""
    if env_path.is_file():
        existing_content = env_path.read_text()
        existing_keys = set(dotenv.read(env_path).keys())

    new_vars = [
        ev
        for ev in sorted(required, key=lambda e: e.name)
        if ev.name not in existing_keys
    ]
    if not new_vars and existing_content:
        env_path.chmod(0o600)
        _log.debug("%s: up to date", env_path.name)
        if log is not None:
            log(f"  {env_path.name}: up to date")
        return

    lines: list[str] = []
    if existing_content:
        lines.append(existing_content.rstrip("\n"))
    else:
        lines.append(header)
        lines.append("# Generated by claudlobby generate — fill in real values.")
        lines.append("")

    if new_vars:
        lines.append("")
        for ev in new_vars:
            source_note = f" (from {ev.source})" if ev.source else ""
            lines.append(f"# {ev.description}{source_note}")
            lines.append(f"export {ev.name}=")

    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    if new_vars:
        new_names = ", ".join(ev.name for ev in new_vars)
        msg = f"  {env_path.name}: added {len(new_vars)} new vars ({new_names})"
        _log.info("%s: added %d new vars (%s)", env_path.name, len(new_vars), new_names)
        if log is not None:
            log(msg)


def scaffold_env_files(fleet: FleetConfig, paths: Paths, log=None) -> None:
    """Idempotent .env scaffolding at fleet and bot tiers.

    Preserves existing key=value pairs, appends stubs for any new
    contract vars discovered from MCP fragments and integrations.
    """
    env_vars = collect_env_contracts(fleet, paths)
    fleet_vars = [ev for ev in env_vars if ev.tier != "bot"]
    bot_vars = [ev for ev in env_vars if ev.tier == "bot"]

    if paths.fleet_dir:
        _scaffold_env_merge(
            paths.fleet_dir / ".env",
            f"# Fleet environment for: {fleet.name}",
            fleet_vars,
            log=log,
        )

    if bot_vars:
        for bot_name in fleet.bots:
            _scaffold_env_merge(
                paths.bot_runtime(bot_name) / ".env",
                f"# Bot environment for: {bot_name}",
                bot_vars,
                log=log,
            )


_BOOT_STAGGER_SECONDS = 3  # delay between each bot's startup on fleet boot


# ---------------------------------------------------------------------------
# Fleet-level timer generation
# ---------------------------------------------------------------------------


def _resolve_timer_schedule(timer_cfg: dict, merged_defaults: dict) -> dict:
    """Resolve timer scheduling from config.

    Returns a dict describing the schedule type:
      {"type": "interval", "seconds": 300}
      {"type": "calendar", "expression": "*-*-* 06:00:00"}
    """
    if "schedule" in timer_cfg:
        return {"type": "calendar", "expression": timer_cfg["schedule"]}
    if "interval_from" in timer_cfg:
        ref = timer_cfg["interval_from"]
        section, _, field = ref.partition(".")
        if section == "observability":
            obs = merged_defaults.get("observability", {})
            val = obs.get(field)
            if val is not None:
                return {"type": "interval", "seconds": int(val)}
    return {"type": "interval", "seconds": timer_cfg.get("interval", 300)}


def _write_timer_units(
    timers_dir: Path,
    service_name: str,
    name: str,
    sched: dict,
    script: str,
    svc_type: str,
    fleet_name: str | None,
    paths: Paths,
    *,
    persistent: bool = False,
    randomized_delay: int = 0,
    telegram_group_chat_id: str | None = None,
) -> None:
    """Write the .service/.timer/.plist units for a single timer.

    One emitter shared by the system_defaults timer loop, the opt-in
    fleet.sweep branch, and the host-global jobs (consolidate, don't fork).

    ``service_name`` is the full unit basename, computed by the caller:
    ``<service_prefix>.<name>`` for fleet timers, ``claudlobby-<name>`` for
    host singletons. ``fleet_name=None`` emits a host-scoped unit: no
    CLAUDLOBBY_FLEET env and no fleet argument on ExecStart. ``persistent`` /
    ``randomized_delay`` map to the systemd ``Persistent=`` /
    ``RandomizedDelaySec=`` timer knobs; launchd has no equivalent, so the
    plist ignores them.
    """
    scope = fleet_name if fleet_name is not None else "host"
    script_expanded = script.replace("$CLAUDLOBBY_ROOT", str(paths.root))
    exec_start = f"{script_expanded} {fleet_name}" if fleet_name else script_expanded

    # --- systemd service unit ---
    service_lines = [
        "# Generated by claudlobby — do not hand-edit.",
        "[Unit]",
        f"Description=claudlobby {name} ({scope})",
        "",
        "[Service]",
        f"Type={svc_type}",
        # Pin cwd to the install root so jobs never depend on the
        # supervisor's spawn cwd.
        f"WorkingDirectory={paths.root}",
        f"Environment=CLAUDLOBBY_ROOT={paths.root}",
    ]
    if fleet_name:
        service_lines.append(f"Environment=CLAUDLOBBY_FLEET={fleet_name}")
    # Fleet timers run in a minimal scheduler env (systemd/launchd start with
    # almost nothing). Carry the fleet Telegram group so a scheduled job can
    # deliver an alert from that env — creds-check's tg-post exits without it,
    # silently dropping the dead-credential alert while the unit still exits 0
    # (false-healthy). See lib/creds-check.sh record_and_alert.
    if fleet_name and telegram_group_chat_id:
        service_lines.append(
            f"Environment=TELEGRAM_GROUP_CHAT_ID={telegram_group_chat_id}"
        )
    service_lines.append(f"ExecStart={exec_start}")
    (timers_dir / f"{service_name}.service").write_text("\n".join(service_lines) + "\n")

    # --- systemd timer unit ---
    timer_lines = [
        "# Generated by claudlobby — do not hand-edit.",
        "[Unit]",
    ]
    if sched["type"] == "interval":
        secs = sched["seconds"]
        timer_lines.append(
            f"Description=claudlobby {name} timer ({scope}) -- tick every {secs}s"
        )
        timer_lines.extend(
            [
                "",
                "[Timer]",
                f"OnBootSec={secs}",
                f"OnUnitActiveSec={secs}",
                "AccuracySec=10",
            ]
        )
    else:
        expr = sched["expression"]
        timer_lines.append(f"Description=claudlobby {name} timer ({scope}) -- {expr}")
        timer_lines.extend(
            [
                "",
                "[Timer]",
                f"OnCalendar={expr}",
                "AccuracySec=60",
            ]
        )
    if persistent:
        timer_lines.append("Persistent=true")
    if randomized_delay:
        timer_lines.append(f"RandomizedDelaySec={randomized_delay}")
    timer_lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=timers.target",
        ]
    )
    (timers_dir / f"{service_name}.timer").write_text("\n".join(timer_lines) + "\n")

    # --- launchd plist ---
    plist_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key>",
        f"  <string>{service_name}</string>",
        "  <key>ProgramArguments</key>",
        "  <array>",
        f"    <string>{script_expanded}</string>",
    ]
    if fleet_name:
        plist_lines.append(f"    <string>{fleet_name}</string>")
    plist_lines.extend(
        [
            "  </array>",
            "  <key>EnvironmentVariables</key>",
            "  <dict>",
            "    <key>CLAUDLOBBY_ROOT</key>",
            f"    <string>{paths.root}</string>",
        ]
    )
    if fleet_name:
        plist_lines.extend(
            [
                "    <key>CLAUDLOBBY_FLEET</key>",
                f"    <string>{fleet_name}</string>",
            ]
        )
    if fleet_name and telegram_group_chat_id:
        plist_lines.extend(
            [
                "    <key>TELEGRAM_GROUP_CHAT_ID</key>",
                f"    <string>{telegram_group_chat_id}</string>",
            ]
        )
    plist_lines.append("  </dict>")
    plist_lines.extend(
        [
            "  <key>WorkingDirectory</key>",
            f"  <string>{paths.root}</string>",
        ]
    )
    if sched["type"] == "interval":
        plist_lines.extend(
            [
                "  <key>StartInterval</key>",
                f"  <integer>{sched['seconds']}</integer>",
            ]
        )
    else:
        # Parse HH:MM from OnCalendar expression (e.g. "*-*-* 06:00:00").
        cal_match = re.search(r"(\d{1,2}):(\d{2})", sched["expression"])
        hour = int(cal_match.group(1)) if cal_match else 6
        minute = int(cal_match.group(2)) if cal_match else 0
        cal_interval = ["  <key>StartCalendarInterval</key>", "  <dict>"]
        # A leading systemd weekday (e.g. "Sun *-*-* 05:00:00") maps to a launchd
        # Weekday so a weekly schedule stays weekly on macOS — without it launchd
        # fires daily at the same HH:MM. Only a single weekday is mapped; systemd
        # ranges/lists (e.g. Mon..Fri) have no single-dict launchd equivalent and
        # fall back to firing daily.
        weekdays = {
            "Sun": 0,
            "Mon": 1,
            "Tue": 2,
            "Wed": 3,
            "Thu": 4,
            "Fri": 5,
            "Sat": 6,
        }
        wd_match = re.match(r"\s*(Sun|Mon|Tue|Wed|Thu|Fri|Sat)\b", sched["expression"])
        if wd_match:
            cal_interval += [
                "    <key>Weekday</key>",
                f"    <integer>{weekdays[wd_match.group(1)]}</integer>",
            ]
        cal_interval += [
            "    <key>Hour</key>",
            f"    <integer>{hour}</integer>",
            "    <key>Minute</key>",
            f"    <integer>{minute}</integer>",
            "  </dict>",
        ]
        plist_lines.extend(cal_interval)
    plist_lines.extend(
        [
            "</dict>",
            "</plist>",
        ]
    )
    (timers_dir / f"{service_name}.plist").write_text("\n".join(plist_lines) + "\n")


def compose_fleet_timers(
    fleet: FleetConfig,
    paths: Paths,
    merged_defaults: dict,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Generate fleet-level systemd/launchd timer units into runtime/fleet/timers/.

    Emits the merged ``defaults.jobs`` (when system_defaults timers are enabled)
    and, independently, the opt-in ``code-audit-sweep`` timer (when fleet.sweep
    is enabled).  Returns the timers dir.

    Jobs flagged ``enroll: false`` are composed-but-dormant: their units are
    emitted like any other, but their basenames land in the ``DORMANT``
    manifest beside the units, and the setup backbone (setup-fleet,
    reconcile's job-drift audit) skips them. A fleet opts in per job via
    ``defaults.jobs.<name>.enroll: true`` in fleet.yaml.

    ``output_dir`` overrides the destination directory (the ``timers/`` subdir is
    written beneath it); it defaults to ``paths.runtime_fleet``. ``diff`` passes a
    temp dir here so it can hand this function the real ``Paths`` — keeping the
    diff and generate code paths on an identical ``Paths`` surface — while writing
    the expected units somewhere other than ``runtime/``.
    """
    timers = merged_defaults.get("jobs", {})
    sd = fleet.system_defaults
    emit_defaults = bool(sd.enabled and sd.timers and timers)
    sweep_on = fleet.sweep_enabled()

    base_dir = output_dir if output_dir is not None else paths.runtime_fleet
    timers_dir = base_dir / "timers"
    if not emit_defaults and not sweep_on:
        return timers_dir

    timers_dir.mkdir(parents=True, exist_ok=True)
    prefix = fleet.service_prefix

    if emit_defaults:
        for name, cfg in timers.items():
            sched = _resolve_timer_schedule(cfg, merged_defaults)
            script = cfg.get("script", "")
            svc_type = cfg.get("type", "oneshot")
            _write_timer_units(
                timers_dir,
                f"{prefix}.{name}",
                name,
                sched,
                script,
                svc_type,
                fleet.name,
                paths,
                persistent=bool(cfg.get("persistent", False)),
                randomized_delay=int(cfg.get("randomized_delay") or 0),
                telegram_group_chat_id=fleet.telegram_group_chat_id,
            )
        dormant = sorted(
            f"{prefix}.{n}" for n, c in timers.items() if not c.get("enroll", True)
        )
        manifest_lines = [
            "# Composed-but-dormant units — the setup backbone does not enroll",
            "# these. Opt in via fleet.yaml defaults.jobs.<name>.enroll: true.",
            *dormant,
        ]
        (timers_dir / "DORMANT").write_text("\n".join(manifest_lines) + "\n")

    if sweep_on:
        # Opt-in sweep timer: synthesized from fleet.sweep, not system_defaults.
        _write_timer_units(
            timers_dir,
            f"{prefix}.code-audit-sweep",
            "code-audit-sweep",
            {"type": "calendar", "expression": fleet.sweep.schedule},
            "$CLAUDLOBBY_ROOT/lib/code-audit-sweep.sh",
            "oneshot",
            fleet.name,
            paths,
            telegram_group_chat_id=fleet.telegram_group_chat_id,
        )

    return timers_dir


def compose_host_timers(paths: Paths, *, output_dir: Path | None = None) -> Path:
    """Emit host-global singleton units from system.yaml ``host.jobs``.

    Host jobs are platform equipment with a fixed ``claudlobby-<name>``
    identity (no fleet prefix): one instance per host, package-owned, NOT
    layered through the fleet defaults merge. ``generate`` (and the
    ``host-timers`` subcommand) compose them; ``setup-system`` enrolls them.

    Returns the host timers dir (``runtime/_host/timers/`` under the repo
    root), only created when host jobs are declared.
    """
    host_jobs = load_host_jobs()
    base_dir = (
        output_dir if output_dir is not None else (paths.root / "runtime" / "_host")
    )
    timers_dir = base_dir / "timers"
    if not host_jobs:
        return timers_dir

    timers_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg in host_jobs.items():
        sched = _resolve_timer_schedule(cfg, {})
        _write_timer_units(
            timers_dir,
            f"claudlobby-{name}",
            name,
            sched,
            cfg.get("script", ""),
            cfg.get("type", "oneshot"),
            None,
            paths,
            persistent=bool(cfg.get("persistent", False)),
            randomized_delay=int(cfg.get("randomized_delay") or 0),
        )
    return timers_dir


def compose_fleet(fleet: FleetConfig, paths: Paths, log=None) -> dict[str, Path]:
    paths.runtime_bots.mkdir(parents=True, exist_ok=True)

    # Scaffold shared documentation directories
    if paths.shared_docs:
        for subdir in [
            "planning/active",
            "planning/completed",
            "decisions",
            "knowledge",
            "runbooks",
        ]:
            (paths.shared_docs / subdir).mkdir(parents=True, exist_ok=True)

    out: dict[str, Path] = {}
    for i, (bot_name, bot) in enumerate(fleet.bots.items()):
        _log.info("composing %s...", bot_name)
        if log is not None:
            log(f"composing {bot_name}...")
        out[bot_name] = compose_bot(
            bot, fleet, paths, log=log, boot_delay_s=i * _BOOT_STAGGER_SECONDS
        )

    scaffold_env_files(fleet, paths, log=log)

    return out
