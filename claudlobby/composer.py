"""Template-driven composition.

Reads `templates/claude.md.j2`, loads library files via the frontmatter-aware
loader, renders the bot's CLAUDE.md. Library files are pure content — the
template owns all top-level structure.
"""

from __future__ import annotations
import copy
import json
import logging
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

import jinja2

from . import dotenv
from .config import BotConfig, FleetConfig
from .loader import (
    LibraryItem,
    load_library_items_overlay,
    load_voice,
    parse_expertise_file,
)
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
    env = jinja2.Environment(
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


def _resolve_instance_env(
    env: dict[str, str], contract: dict, entry, instance: str
) -> dict[str, str]:
    """Resolve env var placeholders for an MCP instance.

    Instance-scoped vars get prefixed: ${TOKEN} → ${NOTION_WORK_TOKEN}
    Shared vars stay as-is: ${GOOGLE_OAUTH_CLIENT_ID} → ${GOOGLE_OAUTH_CLIENT_ID}
    """
    import re

    prefix = entry.instance_prefix(instance)
    resolved = {}
    for env_key, env_val in env.items():
        if not isinstance(env_val, str):
            resolved[env_key] = env_val
            continue

        def replace_var(m):
            var = m.group(1)
            meta = contract.get(var, {})
            scope = meta.get("scope", "shared")
            if scope == "instance":
                return "${" + prefix + var + "}"
            return m.group(0)  # keep as-is

        resolved[env_key] = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", replace_var, env_val)
    return resolved


def compose_mcp_json(bot: BotConfig, paths: Paths) -> dict:
    import shutil

    merged: dict = {"mcpServers": {}}
    for entry in bot.mcp:
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except json.JSONDecodeError as e:
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

            if "env" in instance_config:
                instance_config["env"] = _resolve_instance_env(
                    instance_config["env"], contract, entry, instance
                )
            # Also resolve placeholders in other string fields (url, args, headers)
            import re

            def resolve_field(val):
                if isinstance(val, str):

                    def replace_var(m):
                        var = m.group(1)
                        meta = contract.get(var, {})
                        scope = meta.get("scope", "shared")
                        if scope == "instance":
                            return "${" + entry.instance_prefix(instance) + var + "}"
                        return m.group(0)

                    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", replace_var, val)
                elif isinstance(val, list):
                    return [resolve_field(v) for v in val]
                elif isinstance(val, dict):
                    return {k: resolve_field(v) for k, v in val.items()}
                return val

            for field in ["url", "args", "headers"]:
                if field in instance_config:
                    instance_config[field] = resolve_field(instance_config[field])

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
        except json.JSONDecodeError:
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


def compose_bot_conf(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    ctx = _bot_template_context(bot, fleet, paths)
    bot_dir = paths.bot_runtime(bot.bot_id)
    account_dir = fleet.accounts.get(
        bot.account, fleet.accounts.get("default", "~/.claude")
    )

    lines = [
        "# Generated by claudlobby — do not hand-edit. Edit fleet.yaml + library/, then re-run `claudlobby generate`.",
        f"# Bot: {bot.bot_id}",
        "",
        f'BOT_ID="{bot.bot_id}"',
        f'BOT_NAME="{bot.name}"',
        f'BOT_SERVICE="{fleet.service_prefix}.{bot.bot_id}"',
        f'BOT_LABEL="{bot.bot_id.upper()}"',
        f'BOT_DIR="{bot_dir}"',
        f'TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{bot.telegram.handle or bot.bot_id}"',
        "",
        "# Claude Code config dir (multi-account support)",
    ]
    if bot.account != "default":
        lines.append(f'CLAUDE_CONFIG_DIR="{account_dir}"')
    else:
        lines.append(f'# CLAUDE_CONFIG_DIR="{account_dir}"  # default account')
    lines.append("")

    # Assemble the full claude CLI flag set. lib/start-bot.sh reads
    # CLAUDE_FLAGS verbatim and appends only --name <session>.
    flags: list[str] = []
    for ch in bot.channels:
        flags.append(f"--channels {ch}")
    if bot.remote_control:
        flags.append("--remote-control")
    if bot.dangerously_skip_permissions:
        flags.append("--dangerously-skip-permissions")
    if bot.model:
        flags.append(f"--model {bot.model}")
    if bot.effort:
        flags.append(f"--effort {bot.effort}")
    flags.extend(bot.extra_flags)
    lines.append(f'CLAUDE_FLAGS="{" ".join(flags)}"')
    lines.append("")

    # Prompt suggestions — off by default for headless bots.
    lines.append(
        f'export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION="{str(bot.prompt_suggestions).lower()}"'
    )
    lines.append("")
    lines.append("# Exports for skills + scripts")
    lines.append(f'export CLAUDLOBBY_ROOT="{paths.root}"')
    lines.append(f'export FLEET_NAME="{fleet.name}"')
    lines.append(f'export SERVICE_PREFIX="{fleet.service_prefix}"')
    lines.append(f'export FLEET_STATE_PATH="{paths.root / "state" / "fleet-state.json"}"')
    chat_id = ctx["TELEGRAM_GROUP_CHAT_ID"]
    if chat_id:
        lines.append(f'export TELEGRAM_GROUP_CHAT_ID="{chat_id}"')
    if bot.telegram.token_env:
        lines.append(f'export TELEGRAM_TOKEN_ENV_NAME="{bot.telegram.token_env}"')
    if bot.telegram.require_mention is not None:
        lines.append(
            f'export TELEGRAM_REQUIRE_MENTION="{str(bot.telegram.require_mention).lower()}"'
        )
    if bot.telegram.handle:
        lines.append(f'export TELEGRAM_BOT_HANDLE="{bot.telegram.handle}"')

    # Model strategy — config-driven model escalation / compaction / subagent models.
    if bot.model_strategy:
        ms = bot.model_strategy
        lines.append("")
        lines.append("# Model strategy")
        if ms.base:
            lines.append(f'export MODEL_STRATEGY_BASE="{ms.base}"')
        if ms.escalate_to:
            lines.append(f'export MODEL_STRATEGY_ESCALATE_TO="{ms.escalate_to}"')
        if ms.escalate_when:
            lines.append(
                f"export MODEL_STRATEGY_ESCALATE_WHEN={shlex.quote(ms.escalate_when)}"
            )
        if ms.compact_when:
            lines.append(
                f"export MODEL_STRATEGY_COMPACT_WHEN={shlex.quote(ms.compact_when)}"
            )
        # Subagent model preferences (from raw extras)
        for key in ("explore", "plan", "general"):
            val = ms.raw.get(key)
            if val:
                lines.append(f'export MODEL_STRATEGY_{key.upper()}="{val}"')

    for k, v in bot.env.items():
        lines.append(f'export {k}="{v}"')

    lines.append("")

    for team in fleet.teams.values():
        if bot.bot_id in team.workers:
            lines.append(f'export MANAGER_TMUX="{team.manager}"')
            break
    if bot.bot_id in fleet.manager_bots():
        lines.append(f'export MANAGER_TMUX="{bot.bot_id}"  # this bot is a manager')

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
    return jinja2.Template(prompt).render(
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
ExecStop=/bin/sh -c 'tmux kill-session -t {bot.bot_id} 2>/dev/null || true'
ExecStopPost=/bin/rm -f {bot_dir}/.tmux-env
# Restart= here only fires on non-zero exit of start-bot.sh — i.e., a config
# failure before tmux ever spawned. Tmux dying after we've gone "active" is
# detected by lib/keepalive.sh, NOT by systemd, because exit 0 + RemainAfterExit
# leaves the unit looking healthy regardless of what tmux is doing.
Restart=on-failure
RestartSec=5
Environment=CLAUDLOBBY_ROOT={paths.root}

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
            collected: dict[str, Path] = {}  # leaf → src; overlay (first) wins
            for search_dir in paths.library_search_dirs("skills"):
                target = search_dir / dir_name if dir_name else search_dir
                if not target.is_dir():
                    continue
                for sub in sorted(target.rglob("*")):
                    if not sub.is_dir():
                        continue
                    if not (sub / "SKILL.md").is_file():
                        continue
                    leaf = sub.name
                    if leaf not in collected:
                        collected[leaf] = sub
            if not collected:
                log(f"  skill folder '{skill}' empty or missing — skipped")
                continue
            for leaf, src in sorted(collected.items()):
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

    # Auto-pair integrations with mcp by default; explicit overrides.
    # An integration matches if EITHER overlay or base has `<mcp>.md` under integrations/.
    if bot.integrations:
        integration_names = bot.integrations
    else:
        integration_names = [
            e.name
            for e in bot.mcp
            if paths.find_library_file("integrations", e.name, ".md") is not None
        ]

    teams = fleet.teams_for_manager(bot.bot_id)
    org_structure = _compose_org_structure(bot, fleet)

    env = _build_jinja_env(paths)
    template = env.get_template("claude.md.j2")
    rendered = template.render(
        bot=bot,
        fleet=fleet,
        title_label=title_label,
        expertise_body=expertise_body,
        voice=voice_item,
        teams=teams,
        org_structure=org_structure,
        resources=_items(bot.resources, "resources"),
        integrations=_items(integration_names, "integrations"),
        principles=_items(bot.principles, "principles"),
        protocols=_items(bot.protocols, "protocols"),
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

    deny_patterns = [f"{t}(**)" for t in merged_deny]
    return allow_patterns, deny_patterns


# Minimal base tools every bot needs for read-only operation.
# Expertise profiles add Write, Edit, Bash, etc. in Phase 2.
BASE_TOOLS = ["Read", "Grep", "Glob"]


def compose_settings_local(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> dict:
    """Generate .claude/settings.local.json with memory dir, sibling isolation, tools, sandbox, and hooks."""
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
        deny_patterns.append(f"{tool}(**)")

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
    sandbox = bot.sandbox
    if (
        sandbox.enabled is not None
        or sandbox.network_allowed_domains
        or sandbox.filesystem_allow_write
        or sandbox.auto_allow_bash is not None
    ):
        sandbox_cfg: dict = {}
        if sandbox.enabled is not None:
            sandbox_cfg["enabled"] = sandbox.enabled
        if sandbox.network_allowed_domains:
            sandbox_cfg["network"] = {"allowedDomains": sandbox.network_allowed_domains}
        if sandbox.filesystem_allow_write:
            sandbox_cfg["filesystem"] = {"allowWrite": sandbox.filesystem_allow_write}
        if sandbox.auto_allow_bash is not None:
            sandbox_cfg["autoAllowBashIfSandboxed"] = sandbox.auto_allow_bash
        settings["sandbox"] = sandbox_cfg

    # Hooks: PreToolUse, PostToolUse, etc.
    hooks = _compose_hooks(bot.hooks)
    if hooks:
        settings["hooks"] = hooks

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
    (bot_dir / "logs").mkdir(exist_ok=True)

    (bot_dir / "CLAUDE.md").write_text(compose_claude_md(bot, fleet, paths))

    mcp = compose_mcp_json(bot, paths)
    (bot_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")

    (bot_dir / "bot.conf").write_text(compose_bot_conf(bot, fleet, paths))

    settings_local = compose_settings_local(bot, fleet, paths)
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
        import re

        handle = bot.telegram.handle
        if not re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$", handle):
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

    (bot_dir / f"{bot.bot_id}.service").write_text(
        compose_systemd_unit(bot, fleet, paths, boot_delay_s=boot_delay_s)
    )
    (bot_dir / f"{bot.bot_id}.plist").write_text(
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
            for var_name, meta in contract.items():
                tier = meta.get("tier", "fleet")
                scope = meta.get("scope", "shared")
                if scope == "instance":
                    for instance in entry.instances:
                        prefix = entry.instance_prefix(instance)
                        canonical = prefix + var_name
                        if canonical not in vars:
                            inst_label = (
                                f" ({instance})" if instance != "default" else ""
                            )
                            vars[canonical] = EnvVar(
                                name=canonical,
                                description=f"{meta.get('description', '')}{inst_label}",
                                tier=tier,
                                source=f"mcp/{entry.name}",
                            )
                else:
                    if var_name not in vars:
                        vars[var_name] = EnvVar(
                            name=var_name,
                            description=meta.get("description", ""),
                            tier=tier,
                            source=f"mcp/{entry.name}",
                        )

        # Integration doc contracts
        integration_names = bot.integrations or [
            e.name
            for e in bot.mcp
            if paths.find_library_file("integrations", e.name, ".md") is not None
        ]
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


def compose_fleet(fleet: FleetConfig, paths: Paths, log=None) -> dict[str, Path]:
    paths.runtime_bots.mkdir(parents=True, exist_ok=True)
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
