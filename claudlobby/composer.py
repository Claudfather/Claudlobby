"""Template-driven composition.

Reads `templates/claude.md.j2`, loads library files via the frontmatter-aware
loader, renders the bot's CLAUDE.md. Library files are pure content — the
template owns all top-level structure.
"""
from __future__ import annotations
import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import jinja2

from .config import BotConfig, FleetConfig, TeamConfig
from .loader import (
    ExpertiseItem,
    LibraryItem,
    load_library_item,
    load_library_items,
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


def _bot_template_context(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> dict[str, str]:
    return {
        "BOT_NAME": bot.name,
        "BOT_NAME_UPPER": bot.name.upper(),
        "FLEET_NAME": fleet.name,
        "SERVICE_PREFIX": fleet.service_prefix,
        "CLAUDLOBBY_ROOT": str(paths.root),
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

def _compose_expertise(bot: BotConfig, paths: Paths, ctx: dict[str, str]) -> tuple[str | None, str]:
    """Return (title_label, expertise_body).

    First expertise file's H1 (if present) provides the title_label. Subsequent
    expertise files' H1s are stripped; their bodies are concatenated.

    Overlay-aware: looks in `local/<fleet>/library/expertise/` first, then
    in the public `library/expertise/`.
    """
    if not bot.expertise:
        raise ValueError(f"bot '{bot.name}': expertise list is empty")

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

def _resolve_instance_env(env: dict[str, str], contract: dict, entry, instance: str) -> dict[str, str]:
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


# ----------------------------------------------------------------------
# bot.conf
# ----------------------------------------------------------------------

def compose_bot_conf(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    ctx = _bot_template_context(bot, fleet, paths)
    bot_dir = paths.bot_runtime(bot.name)
    account_dir = fleet.accounts.get(bot.account, fleet.accounts.get("default", "~/.claude"))

    lines = [
        "# Generated by claudlobby — do not hand-edit. Edit fleet.yaml + library/, then re-run `claudlobby generate`.",
        f"# Bot: {bot.name}",
        "",
        f'BOT_NAME="{bot.name}"',
        f'BOT_SERVICE="{fleet.service_prefix}.{bot.name}"',
        f'BOT_LABEL="{bot.name.upper()}"',
        f'BOT_DIR="{bot_dir}"',
        f'TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-{bot.name}"',
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
    lines.append(f'export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION="{str(bot.prompt_suggestions).lower()}"')
    lines.append("")
    lines.append("# Exports for skills + scripts")
    lines.append(f'export CLAUDLOBBY_ROOT="{paths.root}"')
    lines.append(f'export FLEET_NAME="{fleet.name}"')
    lines.append(f'export SERVICE_PREFIX="{fleet.service_prefix}"')
    lines.append(f'export FLEET_STATE_PATH="{paths.lib / "fleet-state.json"}"')
    chat_id = ctx["TELEGRAM_GROUP_CHAT_ID"]
    if chat_id:
        lines.append(f'export TELEGRAM_GROUP_CHAT_ID="{chat_id}"')
    if bot.telegram.token_env:
        lines.append(f'export TELEGRAM_TOKEN_ENV_NAME="{bot.telegram.token_env}"')
    if bot.telegram.require_mention is not None:
        lines.append(f'export TELEGRAM_REQUIRE_MENTION="{str(bot.telegram.require_mention).lower()}"')
    if bot.telegram.handle:
        lines.append(f'export TELEGRAM_BOT_HANDLE="{bot.telegram.handle}"')

    for k, v in bot.env.items():
        lines.append(f'export {k}="{v}"')

    lines.append("")

    for team in fleet.teams.values():
        if bot.name in team.workers:
            lines.append(f'export MANAGER_TMUX="{team.manager}"')
            break
    if bot.name in fleet.manager_bots():
        lines.append(f'export MANAGER_TMUX="{bot.name}"  # this bot is a manager')

    lines.append("")
    if bot.startup_prompt:
        rendered = _render_startup_prompt(bot.startup_prompt, bot, fleet)
        lines.append(f'STARTUP_PROMPT={json.dumps(rendered)}')
    else:
        lines.append('STARTUP_PROMPT="Welcome back. Read your CLAUDE.md. Idle and await Telegram messages."')

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
        bot_name=bot.name,
        fleet_name=fleet.name,
        telegram_group_chat_id=fleet.telegram_group_chat_id or "",
        telegram_handle=(bot.telegram.handle if bot.telegram else "") or "",
    )


# ----------------------------------------------------------------------
# Service units
# ----------------------------------------------------------------------

def compose_systemd_unit(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    bot_dir = paths.bot_runtime(bot.name)
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
WorkingDirectory={bot_dir}
ExecStart={paths.lib}/start-bot.sh {bot_dir}
ExecStop=/bin/sh -c 'tmux kill-session -t {bot.name} 2>/dev/null || true'
# Restart= here only fires on non-zero exit of start-bot.sh — i.e., a config
# failure before tmux ever spawned. Tmux dying after we've gone "active" is
# detected by lib/keepalive.sh, NOT by systemd, because exit 0 + RemainAfterExit
# leaves the unit looking healthy regardless of what tmux is doing.
Restart=on-failure
RestartSec=10
Environment=CLAUDLOBBY_ROOT={paths.root}

[Install]
WantedBy=default.target
"""


def compose_launchd_plist(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> str:
    bot_dir = paths.bot_runtime(bot.name)
    label = f"{fleet.service_prefix}.{bot.name}"
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
    <key>StandardOutPath</key><string>{log_dir}/{bot.name}.out.log</string>
    <key>StandardErrorPath</key><string>{log_dir}/{bot.name}.err.log</string>
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
    bot_skills_dir = paths.bot_runtime(bot.name) / ".claude" / "skills"
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
        return [_expand_item(it, ctx) for it in load_library_items_overlay(names, paths, kind)]

    # Auto-pair integrations with mcp by default; explicit overrides.
    # An integration matches if EITHER overlay or base has `<mcp>.md` under integrations/.
    if bot.integrations:
        integration_names = bot.integrations
    else:
        integration_names = [
            e.name for e in bot.mcp
            if paths.find_library_file("integrations", e.name, ".md") is not None
        ]

    teams = fleet.teams_for_manager(bot.name)

    env = _build_jinja_env(paths)
    template = env.get_template("claude.md.j2")
    rendered = template.render(
        bot=bot,
        fleet=fleet,
        title_label=title_label,
        expertise_body=expertise_body,
        voice=voice_item,
        teams=teams,
        resources=_items(bot.resources, "resources"),
        integrations=_items(integration_names, "integrations"),
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

def compose_settings_local(bot: BotConfig, fleet: FleetConfig, paths: Paths) -> dict:
    """Generate .claude/settings.local.json with memory dir + sibling isolation."""
    bot_dir = paths.bot_runtime(bot.name)
    memory_dir = str(bot_dir / "memory")

    settings: dict = {
        "autoMemoryDirectory": memory_dir,
    }

    # Sibling isolation: deny reading other bots' files
    # Bot can still communicate via tmux send-keys (process-level, not file-level)
    siblings = [name for name in fleet.bots if name != bot.name]
    if siblings:
        deny_patterns = []
        for sibling in siblings:
            sibling_dir = str(paths.bot_runtime(sibling))
            deny_patterns.append(f"Read({sibling_dir}/**)")
        settings["permissions"] = {"deny": deny_patterns}

    return settings


def compose_bot(bot: BotConfig, fleet: FleetConfig, paths: Paths, log=print) -> Path:
    bot_dir = paths.bot_runtime(bot.name)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / ".claude").mkdir(exist_ok=True)
    (bot_dir / "memory").mkdir(exist_ok=True)

    (bot_dir / "CLAUDE.md").write_text(compose_claude_md(bot, fleet, paths))

    mcp = compose_mcp_json(bot, paths)
    (bot_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")

    (bot_dir / "bot.conf").write_text(compose_bot_conf(bot, fleet, paths))

    settings_local = compose_settings_local(bot, fleet, paths)
    (bot_dir / ".claude" / "settings.local.json").write_text(
        json.dumps(settings_local, indent=2) + "\n"
    )

    link_skills(bot, paths, log)

    (bot_dir / f"{bot.name}.service").write_text(compose_systemd_unit(bot, fleet, paths))
    (bot_dir / f"{bot.name}.plist").write_text(compose_launchd_plist(bot, fleet, paths))

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
                            inst_label = f" ({instance})" if instance != "default" else ""
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
            e.name for e in bot.mcp
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


def scaffold_env_files(fleet: FleetConfig, paths: Paths, log=print) -> None:
    """Create empty-placeholder .env files at fleet and bot tiers.
    Only creates files that don't already exist."""
    env_vars = collect_env_contracts(fleet, paths)
    fleet_vars = [ev for ev in env_vars if ev.tier != "bot"]
    bot_vars = [ev for ev in env_vars if ev.tier == "bot"]

    if paths.fleet_dir:
        fleet_env_path = paths.fleet_dir / ".env"
        if not fleet_env_path.is_file():
            lines = [
                f"# Fleet environment for: {fleet.name}",
                f"# Generated by claudlobby generate — fill in real values.",
                "",
            ]
            for ev in sorted(fleet_vars, key=lambda e: e.name):
                lines.append(f"# {ev.description} (from {ev.source})")
                lines.append(f"export {ev.name}=")
            fleet_env_path.write_text("\n".join(lines) + "\n")
            log(f"  scaffolded fleet .env: {fleet_env_path} ({len(fleet_vars)} vars)")
        else:
            log(f"  fleet .env exists, skipping: {fleet_env_path}")

    if bot_vars:
        for bot_name in fleet.bots:
            bot_env_path = paths.bot_runtime(bot_name) / ".env"
            if not bot_env_path.is_file():
                lines = [
                    f"# Bot environment for: {bot_name}",
                    f"# Generated by claudlobby generate — fill in real values.",
                    "",
                ]
                for ev in sorted(bot_vars, key=lambda e: e.name):
                    lines.append(f"# {ev.description}")
                    lines.append(f"export {ev.name}=")
                bot_env_path.write_text("\n".join(lines) + "\n")
                log(f"  scaffolded bot .env: {bot_env_path} ({len(bot_vars)} vars)")
            else:
                log(f"  bot .env exists, skipping: {bot_env_path}")


def compose_fleet(fleet: FleetConfig, paths: Paths, log=print) -> dict[str, Path]:
    paths.runtime_bots.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for bot_name, bot in fleet.bots.items():
        log(f"composing {bot_name}...")
        out[bot_name] = compose_bot(bot, fleet, paths, log=log)

    scaffold_env_files(fleet, paths, log=log)

    return out
