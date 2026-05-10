"""fleet.yaml → typed config objects.

Loads and normalises the manifest. Applies fleet.defaults to each bot,
flattens lists, and resolves team membership.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    handle: str | None = None
    token_env: str | None = None
    require_mention: bool = True
    chat_id: str | None = None


@dataclass
class ScopeConfig:
    """Org / repos / data sources the bot operates on. Optional."""

    org: str | None = None
    repos: list[str] = field(default_factory=list)
    snowflake_targets: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # any extra fields land here


@dataclass
class ModelStrategyConfig:
    """When and how to escalate / compact / restart. Optional."""

    base: str | None = None  # default model
    escalate_to: str | None = None  # e.g. "opus" if base is "sonnet"
    escalate_when: str | None = None  # human-readable rule
    compact_when: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolsConfig:
    """Tool allow/deny lists → .claude/settings.local.json permissions.

    Controls which Claude Code tools a bot may use. Deny rules generate
    permission deny patterns like "Write(**)", enforcing bot roles at the
    platform level rather than relying on CLAUDE.md prose alone.
    """

    deny: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)


@dataclass
class SandboxConfig:
    """Sandbox network/filesystem settings → .claude/settings.local.json.

    Controls the Claude Code sandbox layer (separate from --dangerously-skip-permissions
    which controls tool-call permission prompts).
    """

    enabled: bool | None = None  # None = inherit from global settings
    network_allowed_domains: list[str] = field(default_factory=list)
    filesystem_allow_write: list[str] = field(default_factory=list)
    auto_allow_bash: bool | None = None  # None = inherit from fleet default


@dataclass
class McpEntry:
    """Parsed MCP entry from fleet.yaml.

    Supports three forms:
      - `"github"` → name="github", instances=["default"]
      - `"notion:"` with `instances: [default, work]` → name="notion", instances=["default", "work"]
      - `"gws:"` with `instances: [personal, work]` → name="gws", instances=["personal", "work"]
    """

    name: str
    instances: list[str] = field(default_factory=lambda: ["default"])

    @property
    def is_multi(self) -> bool:
        return len(self.instances) > 1 or self.instances != ["default"]

    def server_names(self) -> list[str]:
        """Output server names for .mcp.json."""
        out = []
        for inst in self.instances:
            if inst == "default":
                out.append(self.name)
            else:
                out.append(f"{self.name}-{inst}")
        return out

    def instance_prefix(self, instance: str) -> str:
        """Env var prefix for an instance. E.g., gws/personal → GWS_PERSONAL_."""
        if instance == "default":
            return f"{self.name.upper().replace('-', '_')}_"
        return f"{self.name.upper().replace('-', '_')}_{instance.upper().replace('-', '_')}_"


@dataclass
class BotConfig:
    bot_id: str  # dict key — immutable system slug
    name: str  # display name (defaults to bot_id)
    expertise: list[str]  # list of library/expertise/<name>.md
    voice: str | None = None
    mission: str | None = None  # one-paragraph charter
    reports_to: str | None = None  # bot_id this bot reports to
    manages: list[str] | None = None  # bot_ids this bot manages
    scope: ScopeConfig | None = None
    model_strategy: ModelStrategyConfig | None = None
    account: str = "default"
    model: str | None = None
    effort: str | None = None
    # Claude Code CLI flags — composed into CLAUDE_FLAGS in bot.conf.
    remote_control: bool = True  # --remote-control
    dangerously_skip_permissions: bool = True  # --dangerously-skip-permissions
    channels: list[str] = field(
        default_factory=lambda: ["plugin:telegram@claude-plugins-official"]
    )  # --channels <name>
    prompt_suggestions: bool = False  # CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION
    extra_flags: list[str] = field(default_factory=list)  # any other claude CLI args
    skills: list[str] = field(default_factory=list)
    mcp: list[McpEntry] = field(default_factory=list)
    integrations: list[str] = field(
        default_factory=list
    )  # explicit; auto-paired with mcp by default
    guardrails: list[str] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    principles: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    post_actions: list[str] = field(default_factory=list)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mounts: dict[str, str] = field(default_factory=dict)  # name → absolute host path
    env: dict[str, str] = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    startup_prompt: str | None = None


@dataclass
class TeamConfig:
    name: str
    manager: str
    workers: list[str] = field(default_factory=list)


@dataclass
class FleetConfig:
    name: str
    service_prefix: str
    telegram_group_chat_id: str | None = None
    human_telegram_id: str | None = None
    accounts: dict[str, str] = field(default_factory=lambda: {"default": "~/.claude"})
    defaults: dict[str, Any] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)
    bots: dict[str, BotConfig] = field(default_factory=dict)

    def manager_bots(self) -> set[str]:
        """Bot names that manage at least one team."""
        return {team.manager for team in self.teams.values()}

    def teams_for_manager(self, bot_name: str) -> list[TeamConfig]:
        return [team for team in self.teams.values() if team.manager == bot_name]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _merge_lists(*lists) -> list[str]:
    """Concat lists, dedupe preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _as_list(v) -> list[str]:
    """Accept str or list[str]; return list[str]."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _parse_mcp_list(raw_list: list | None) -> list[McpEntry]:
    """Parse mcp list from fleet.yaml into McpEntry objects.

    Accepts:
      - "github"                          → McpEntry(name="github", instances=["default"])
      - {"notion": {"instances": [...]}}  → McpEntry(name="notion", instances=[...])
    """
    if not raw_list:
        return []
    entries: list[McpEntry] = []
    seen: set[str] = set()
    for item in raw_list:
        if isinstance(item, str):
            if item not in seen:
                seen.add(item)
                entries.append(McpEntry(name=item))
        elif isinstance(item, dict):
            for name, config in item.items():
                if name not in seen:
                    seen.add(name)
                    instances = (
                        config.get("instances", ["default"]) if config else ["default"]
                    )
                    # Accept both list of strings and list of dicts (future: per-instance config)
                    parsed_instances = []
                    for inst in instances:
                        if isinstance(inst, str):
                            parsed_instances.append(inst)
                        elif isinstance(inst, dict):
                            # Future: per-instance config like {personal: {port: 8001}}
                            parsed_instances.extend(inst.keys())
                    entries.append(McpEntry(name=name, instances=parsed_instances))
    return entries


def _merge_mcp_lists(*lists) -> list[McpEntry]:
    """Merge MCP entry lists, deduplicating by name. First-seen wins."""
    seen: set[str] = set()
    out: list[McpEntry] = []
    for lst in lists:
        for entry in lst or []:
            if entry.name not in seen:
                seen.add(entry.name)
                out.append(entry)
    return out


def _coerce_scope(raw: dict | None) -> ScopeConfig | None:
    if not raw:
        return None
    return ScopeConfig(
        org=raw.get("org"),
        repos=list(raw.get("repos") or []),
        snowflake_targets=list(raw.get("snowflake_targets") or []),
        raw={
            k: v
            for k, v in raw.items()
            if k not in {"org", "repos", "snowflake_targets"}
        },
    )


def _coerce_model_strategy(raw: dict | None) -> ModelStrategyConfig | None:
    if not raw:
        return None
    return ModelStrategyConfig(
        base=raw.get("base"),
        escalate_to=raw.get("escalate_to"),
        escalate_when=raw.get("escalate_when"),
        compact_when=raw.get("compact_when"),
        raw={
            k: v
            for k, v in raw.items()
            if k not in {"base", "escalate_to", "escalate_when", "compact_when"}
        },
    )


def _coerce_sandbox(raw: dict | None) -> SandboxConfig:
    if not raw:
        return SandboxConfig()
    enabled_raw = raw.get("enabled")
    auto_allow_raw = raw.get("auto_allow_bash")
    return SandboxConfig(
        enabled=bool(enabled_raw) if enabled_raw is not None else None,
        network_allowed_domains=list(raw.get("network_allowed_domains") or []),
        filesystem_allow_write=list(raw.get("filesystem_allow_write") or []),
        auto_allow_bash=bool(auto_allow_raw) if auto_allow_raw is not None else None,
    )


def _merge_sandbox(default: SandboxConfig, override: SandboxConfig) -> SandboxConfig:
    """Merge sandbox configs — lists are unioned, bools use override value when not None."""
    return SandboxConfig(
        enabled=override.enabled if override.enabled is not None else default.enabled,
        network_allowed_domains=_merge_lists(
            default.network_allowed_domains, override.network_allowed_domains
        ),
        filesystem_allow_write=_merge_lists(
            default.filesystem_allow_write, override.filesystem_allow_write
        ),
        auto_allow_bash=override.auto_allow_bash
        if override.auto_allow_bash is not None
        else default.auto_allow_bash,
    )


def _coerce_tools(raw: dict | None) -> ToolsConfig:
    if not raw:
        return ToolsConfig()
    return ToolsConfig(
        deny=list(raw.get("deny") or []),
        allow=list(raw.get("allow") or []),
    )


def _merge_tools(default: ToolsConfig, override: ToolsConfig) -> ToolsConfig:
    """Merge tools configs — lists are unioned, deduplicated."""
    return ToolsConfig(
        deny=_merge_lists(default.deny, override.deny),
        allow=_merge_lists(default.allow, override.allow),
    )


def _coerce_hooks(raw: dict | None) -> dict[str, list[dict[str, Any]]]:
    """Parse hooks from fleet.yaml into {event_name: [hook_entry, ...]}."""
    if not raw or not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for event, entries in raw.items():
        if not isinstance(entries, list):
            continue
        out[event] = [e for e in entries if isinstance(e, dict)]
    return out


def _merge_hooks(
    default: dict[str, list[dict[str, Any]]],
    override: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Merge hooks — default entries first, bot entries appended per event."""
    merged: dict[str, list[dict[str, Any]]] = {}
    all_events = set(default) | set(override)
    for event in sorted(all_events):
        merged[event] = list(default.get(event, [])) + list(override.get(event, []))
    return merged


def _coerce_bot(name: str, raw: dict[str, Any], defaults: dict[str, Any]) -> BotConfig:
    raw = raw or {}
    tg_defaults = defaults.get("telegram", {}) or {}
    tg_raw = {**tg_defaults, **(raw.get("telegram", {}) or {})}

    # `expertise` accepts a list or a single string. Required field.
    # Backwards-compat: accept `persona:` for one or two more refactor cycles.
    expertise_raw = raw.get("expertise") or raw.get("persona")
    if raw.get("persona") and not raw.get("expertise"):
        log.warning(
            "bot '%s': 'persona' is deprecated, use 'expertise' (a list) instead", name
        )
    if not expertise_raw:
        raise ValueError(f"bot '{name}': missing required field 'expertise'")

    def _bool(key: str, fallback: bool) -> bool:
        if key in raw:
            return bool(raw[key])
        if key in defaults:
            return bool(defaults[key])
        return fallback

    return BotConfig(
        bot_id=name,
        name=raw.get("name", name),
        expertise=_merge_lists(
            _as_list(defaults.get("expertise")), _as_list(expertise_raw)
        ),
        voice=raw.get("voice"),
        mission=raw.get("mission") or defaults.get("mission"),
        reports_to=raw.get("reports_to"),
        manages=_as_list(raw.get("manages")) or None,
        scope=_coerce_scope(raw.get("scope") or defaults.get("scope")),
        model_strategy=_coerce_model_strategy(
            raw.get("model_strategy") or defaults.get("model_strategy")
        ),
        account=raw.get("account", defaults.get("account", "default")),
        model=raw.get("model", defaults.get("model")),
        effort=raw.get("effort", defaults.get("effort")),
        remote_control=_bool("remote_control", True),
        dangerously_skip_permissions=_bool("dangerously_skip_permissions", True),
        prompt_suggestions=_bool("prompt_suggestions", False),
        channels=_as_list(raw.get("channels") or defaults.get("channels"))
        or ["plugin:telegram@claude-plugins-official"],
        extra_flags=_merge_lists(defaults.get("extra_flags"), raw.get("extra_flags")),
        skills=_merge_lists(defaults.get("skills"), raw.get("skills")),
        mcp=_merge_mcp_lists(
            _parse_mcp_list(defaults.get("mcp")), _parse_mcp_list(raw.get("mcp"))
        ),
        integrations=_merge_lists(
            defaults.get("integrations"), raw.get("integrations")
        ),
        guardrails=_merge_lists(defaults.get("guardrails"), raw.get("guardrails")),
        protocols=_merge_lists(defaults.get("protocols"), raw.get("protocols")),
        principles=_merge_lists(defaults.get("principles"), raw.get("principles")),
        resources=_merge_lists(defaults.get("resources"), raw.get("resources")),
        lessons=_merge_lists(defaults.get("lessons"), raw.get("lessons")),
        post_actions=_merge_lists(
            defaults.get("post_actions"), raw.get("post_actions")
        ),
        sandbox=_merge_sandbox(
            _coerce_sandbox(defaults.get("sandbox")),
            _coerce_sandbox(raw.get("sandbox")),
        ),
        tools=_merge_tools(
            _coerce_tools(defaults.get("tools")),
            _coerce_tools(raw.get("tools")),
        ),
        hooks=_merge_hooks(
            _coerce_hooks(defaults.get("hooks")),
            _coerce_hooks(raw.get("hooks")),
        ),
        mounts={**(defaults.get("mounts") or {}), **(raw.get("mounts") or {})},
        env=raw.get("env", {}) or {},
        telegram=TelegramConfig(
            handle=tg_raw.get("handle"),
            token_env=tg_raw.get("token_env"),
            require_mention=bool(tg_raw.get("require_mention", True)),
            chat_id=tg_raw.get("chat_id"),
        ),
        startup_prompt=raw.get("startup_prompt"),
    )


def load_fleet(fleet_yaml: Path) -> FleetConfig:
    if not fleet_yaml.is_file():
        raise FileNotFoundError(f"fleet.yaml not found at {fleet_yaml}")

    with fleet_yaml.open() as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict) or "fleet" not in doc:
        raise ValueError(f"{fleet_yaml}: top-level key 'fleet' missing")

    fleet = doc["fleet"]

    teams_raw = fleet.get("teams", {}) or {}
    teams = {
        team_name: TeamConfig(
            name=team_name,
            manager=team_def["manager"],
            workers=list(team_def.get("workers", []) or []),
        )
        for team_name, team_def in teams_raw.items()
    }

    defaults = fleet.get("defaults", {}) or {}

    bots = {
        bot_name: _coerce_bot(bot_name, bot_def, defaults)
        for bot_name, bot_def in (fleet.get("bots", {}) or {}).items()
    }

    return FleetConfig(
        name=fleet.get("name", "unnamed-fleet"),
        service_prefix=fleet.get("service_prefix", "claudlobby"),
        telegram_group_chat_id=fleet.get("telegram_group_chat_id"),
        human_telegram_id=fleet.get("human_telegram_id"),
        accounts=fleet.get("accounts", {"default": "~/.claude"})
        or {"default": "~/.claude"},
        defaults=defaults,
        teams=teams,
        bots=bots,
    )
