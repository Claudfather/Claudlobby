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

from .known_values import KNOWN_EFFORTS, VALID_PERMISSION_MODES, closest_match

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
class SystemDefaultsConfig:
    """Controls which system defaults are injected.

    ``system_defaults: false`` in fleet.yaml sets ``enabled=False``,
    disabling all injection.  Per-category bools allow surgical opt-out.
    """

    enabled: bool = True
    hooks: bool = True
    timers: bool = True
    observability: bool = True


@dataclass
class ObservabilityConfig:
    """Fleet observability settings — pulse interval and event retention.

    Composed into bot.conf as env vars so bot-vitals.sh and fleet-pulse.sh
    can read them at runtime.  Fields use None sentinel so _merge_observability
    can distinguish "not set" from "explicitly set to the default value".
    """

    pulse_interval: int | None = None  # seconds between heartbeat pulses
    reap_days: int | None = None  # days to retain event files before reaping
    # seconds of no tool-call activity (while not idle) before a bot is flagged
    # activity_stuck — catches an animated-but-hung session that pane_stuck misses
    activity_stuck_threshold: int | None = None
    # seconds after a manager dispatch before an unanswered task is flagged
    # overdue_dispatch (manager-side watchdog)
    dispatch_deadline: int | None = None


@dataclass
class SweepConfig:
    """Fleet rolling code-audit sweep — opt-in via the fleet.yaml `sweep:` block.

    A fleet-level nightly job like fleet-pulse/creds-check: the no-LLM selector
    lib/code-audit-sweep.sh picks the stalest repo by GitHub `auto-audit` issue
    timestamps and dispatches the audit into the owner bot's session.  Presence
    of the block is opt-in; absence ⇒ FleetConfig.sweep is None ⇒ nothing
    emitted (no env, no timer).
    """

    enabled: bool = False
    owner_bot: str | None = None
    repos: list[str] = field(default_factory=list)
    label: str = "auto-audit"
    schedule: str = "*-*-* 03:00:00"  # systemd OnCalendar; nightly 03:00
    audit_types: list[str] = field(default_factory=lambda: ["tech-debt"])


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
class AutonomousRunnerPicker:
    """Picker config for autonomous-runner. Selects work items per cadence tick."""

    type: str = "github_issues"
    label: str | None = None
    state: str = "open"
    score_by: str = "recency"


@dataclass
class AutonomousRunnerBypass:
    """Pre-flight risk-based bypass. See design spec §6.1.1."""

    risk_classifier: str = "structural_vs_mechanical"
    block_on: list[str] = field(default_factory=lambda: ["structural"])
    on_bypass: str = "comment_and_label"


@dataclass
class AutonomousRunnerConfig:
    """Configuration for the library/skills/autonomous-runner wrapper skill."""

    skill: str
    cadence: str
    target_repo: str
    args: str = ""
    picker: AutonomousRunnerPicker | None = None
    bypass: AutonomousRunnerBypass | None = None
    pre_hooks: list[str] = field(default_factory=list)
    post_hooks: list[str] = field(default_factory=list)
    on_outcome: dict[str, str] = field(default_factory=dict)


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
    permission_mode: str | None = (
        None  # --permission-mode (overrides dangerously_skip_permissions)
    )
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
    permissions: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    post_actions: list[str] = field(default_factory=list)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    mounts: dict[str, str] = field(default_factory=dict)  # name → absolute host path
    env: dict[str, str] = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    startup_prompt: str | None = None
    bench: bool = False  # marks the bot as the fleet's benchmarking target
    # Ecosystem-aware fields — optional integration with clauDNA, Claudron, Claudosseum
    claudna_version: str | None = None
    claudron_vault_path: str | None = None
    claudosseum_tenant_id: str | None = None
    autonomous_runner: AutonomousRunnerConfig | None = None


@dataclass
class TeamConfig:
    name: str
    manager: str
    workers: list[str] = field(default_factory=list)


@dataclass
class PluginsConfig:
    marketplaces: dict[str, dict] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    include_defaults: bool = True


# Built-in defaults — claudna is always installed unless explicitly disabled.
DEFAULT_MARKETPLACES: dict[str, dict] = {
    "Claudfather": {"source": {"source": "github", "repo": "Claudfather/clauDNA"}},
}

DEFAULT_PLUGINS: list[str] = [
    "claudna@Claudfather",
]


@dataclass
class FleetConfig:
    name: str
    service_prefix: str
    telegram_group_chat_id: str | None = None
    human_telegram_id: str | None = None
    accounts: dict[str, str] = field(default_factory=lambda: {"default": "~/.claude"})
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    system_defaults: SystemDefaultsConfig = field(default_factory=SystemDefaultsConfig)
    defaults: dict[str, Any] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)
    bots: dict[str, BotConfig] = field(default_factory=dict)
    sweep: SweepConfig | None = None

    def sweep_enabled(self) -> bool:
        """True when the opt-in code-audit sweep is configured and enabled."""
        return bool(self.sweep and self.sweep.enabled)

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


def _coerce_plugins(raw: dict | None) -> PluginsConfig:
    include_defaults = True
    additional: list[str] = []
    extra_marketplaces: dict[str, dict] = {}

    if raw:
        include_defaults = bool(raw.get("include_defaults", True))
        additional = _as_list(raw.get("additional"))
        if "required" in raw and "additional" not in raw:
            log.warning("plugins.required is deprecated — rename to plugins.additional")
            additional = _as_list(raw.get("required"))
        extra_marketplaces = raw.get("marketplaces") or {}

    if include_defaults:
        required = list(DEFAULT_PLUGINS)
        marketplaces = dict(DEFAULT_MARKETPLACES)
    else:
        required = []
        marketplaces = {}

    for plugin in additional:
        if plugin not in required:
            required.append(plugin)

    marketplaces.update(extra_marketplaces)

    return PluginsConfig(
        marketplaces=marketplaces,
        required=required,
        include_defaults=include_defaults,
    )


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


def _coerce_observability(raw: dict | None) -> ObservabilityConfig:
    if not raw:
        return ObservabilityConfig()
    pi = raw.get("pulse_interval")
    rd = raw.get("reap_days")
    ast = raw.get("activity_stuck_threshold")
    dd = raw.get("dispatch_deadline")
    return ObservabilityConfig(
        pulse_interval=int(pi) if pi is not None else None,
        reap_days=int(rd) if rd is not None else None,
        activity_stuck_threshold=int(ast) if ast is not None else None,
        dispatch_deadline=int(dd) if dd is not None else None,
    )


def _coerce_sweep(raw: dict | None) -> SweepConfig | None:
    """Coerce the fleet.yaml `sweep:` block. None when absent (opt-out)."""
    if not raw:
        return None
    repos = [str(r) for r in (raw.get("repos") or [])]
    audit_types = [str(t) for t in (raw.get("audit_types") or [])] or ["tech-debt"]
    return SweepConfig(
        enabled=bool(raw.get("enabled", True)),  # presence of the block = opt-in
        owner_bot=raw.get("owner_bot"),
        repos=repos,
        label=str(raw.get("label", "auto-audit")),
        schedule=str(raw.get("schedule", "*-*-* 03:00:00")),
        audit_types=audit_types,
    )


def _merge_observability(
    default: ObservabilityConfig, override: ObservabilityConfig
) -> ObservabilityConfig:
    """Merge observability — override wins when not None, else default."""
    return ObservabilityConfig(
        pulse_interval=override.pulse_interval
        if override.pulse_interval is not None
        else default.pulse_interval,
        reap_days=override.reap_days
        if override.reap_days is not None
        else default.reap_days,
        activity_stuck_threshold=override.activity_stuck_threshold
        if override.activity_stuck_threshold is not None
        else default.activity_stuck_threshold,
        dispatch_deadline=override.dispatch_deadline
        if override.dispatch_deadline is not None
        else default.dispatch_deadline,
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


def _hook_key(entry: dict) -> tuple[str, str]:
    """Identity key for hook deduplication."""
    return (entry.get("command", ""), entry.get("matcher", ""))


def _merge_hooks_dedup(
    base: dict[str, list[dict[str, Any]]],
    override: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Merge hooks, deduplicating by (command, matcher). Override wins on collision.

    Preserves base-first ordering: base entries appear first,
    override entries appended after. When a collision occurs (same
    command+matcher in both layers), the override version replaces the
    base version in-place.
    """
    merged: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(set(base) | set(override)):
        override_map: dict[tuple[str, str], dict] = {}
        for e in override.get(event, []):
            override_map[_hook_key(e)] = e

        seen: set[tuple[str, str]] = set()
        entries: list[dict] = []
        for e in base.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(override_map.get(key, e))
        for e in override.get(event, []):
            key = _hook_key(e)
            if key not in seen:
                seen.add(key)
                entries.append(e)
        merged[event] = entries
    return merged


def _coerce_autonomous_runner(
    raw: dict | None, bot_name: str
) -> AutonomousRunnerConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"bot '{bot_name}': autonomous_runner must be a mapping, got {type(raw).__name__}"
        )
    for required in ("skill", "cadence", "target_repo"):
        if not raw.get(required):
            raise ValueError(
                f"bot '{bot_name}': autonomous_runner missing required field '{required}'"
            )

    picker = None
    if raw.get("picker"):
        p = raw["picker"]
        picker = AutonomousRunnerPicker(
            type=p.get("type", "github_issues"),
            label=p.get("label"),
            state=p.get("state", "open"),
            score_by=p.get("score_by", "recency"),
        )

    bypass = None
    if raw.get("bypass"):
        b = raw["bypass"]
        bypass = AutonomousRunnerBypass(
            risk_classifier=b.get("risk_classifier", "structural_vs_mechanical"),
            block_on=list(b.get("block_on") or ["structural"]),
            on_bypass=b.get("on_bypass", "comment_and_label"),
        )

    return AutonomousRunnerConfig(
        skill=raw["skill"],
        cadence=raw["cadence"],
        target_repo=raw["target_repo"],
        args=raw.get("args", "") or "",
        picker=picker,
        bypass=bypass,
        pre_hooks=list(raw.get("pre_hooks") or []),
        post_hooks=list(raw.get("post_hooks") or []),
        on_outcome=dict(raw.get("on_outcome") or {}),
    )


def _parse_enum(label: str, value: str | None, known: frozenset[str]) -> str | None:
    """Validate a string field against a known set. Returns value or raises."""
    if value is None:
        return None
    if value not in known:
        suggestion = closest_match(value, known)
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        raise ValueError(
            f"Invalid {label} '{value}'.{hint} "
            f"Must be one of: {', '.join(sorted(known))}"
        )
    return value


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
        effort=_parse_enum(
            "effort", raw.get("effort", defaults.get("effort")), KNOWN_EFFORTS
        ),
        remote_control=_bool("remote_control", True),
        dangerously_skip_permissions=_bool("dangerously_skip_permissions", True),
        permission_mode=_parse_enum(
            "permission_mode",
            raw.get("permission_mode") or defaults.get("permission_mode"),
            VALID_PERMISSION_MODES,
        ),
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
        permissions=_merge_lists(defaults.get("permissions"), raw.get("permissions")),
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
        hooks=_merge_hooks_dedup(
            _coerce_hooks(defaults.get("hooks")),
            _coerce_hooks(raw.get("hooks")),
        ),
        observability=_merge_observability(
            _coerce_observability(defaults.get("observability")),
            _coerce_observability(raw.get("observability")),
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
        bench=_bool("bench", False),
        claudna_version=raw.get("claudna_version") or defaults.get("claudna_version"),
        claudron_vault_path=raw.get("claudron_vault_path")
        or defaults.get("claudron_vault_path"),
        claudosseum_tenant_id=raw.get("claudosseum_tenant_id")
        or defaults.get("claudosseum_tenant_id"),
        autonomous_runner=_coerce_autonomous_runner(raw.get("autonomous_runner"), name),
    )


def _coerce_system_defaults(raw: Any) -> SystemDefaultsConfig:
    """Parse the ``fleet.system_defaults`` field.

    Accepts ``false`` (kill switch), ``true`` (all on), or a mapping
    with per-category booleans.
    """
    if raw is None or raw is True:
        return SystemDefaultsConfig()
    if raw is False:
        return SystemDefaultsConfig(enabled=False)
    if isinstance(raw, dict):
        return SystemDefaultsConfig(
            enabled=bool(raw.get("enabled", True)),
            hooks=bool(raw.get("hooks", True)),
            timers=bool(raw.get("timers", True)),
            observability=bool(raw.get("observability", True)),
        )
    return SystemDefaultsConfig()


def _load_system_defaults(_cache: dict = {}) -> dict:  # noqa: B006
    """Load system_defaults.yaml from the package directory (cached)."""
    if "data" not in _cache:
        pkg_dir = Path(__file__).parent
        path = pkg_dir / "system_defaults.yaml"
        if not path.is_file():
            _cache["data"] = {}
        else:
            with path.open() as f:
                _cache["data"] = yaml.safe_load(f) or {}
    return _cache["data"]


def _merge_system_into_defaults(system: dict, defaults: dict) -> dict:
    """Merge system defaults under fleet defaults. Fleet defaults win."""
    merged = {}
    all_keys = set(system) | set(defaults)

    for key in all_keys:
        sys_val = system.get(key)
        usr_val = defaults.get(key)

        if key == "hooks":
            merged[key] = _merge_hooks_dedup(sys_val or {}, usr_val or {})
        elif key == "observability":
            merged[key] = {**(sys_val or {}), **(usr_val or {})}
        elif usr_val is not None:
            merged[key] = usr_val
        else:
            merged[key] = sys_val

    return merged


def load_fleet(fleet_yaml: Path) -> tuple[FleetConfig, dict]:
    if not fleet_yaml.is_file():
        raise FileNotFoundError(f"fleet.yaml not found at {fleet_yaml}")

    with fleet_yaml.open() as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict) or "fleet" not in doc:
        raise ValueError(f"{fleet_yaml}: top-level key 'fleet' missing")

    fleet = doc["fleet"]

    teams_raw = fleet.get("teams", {}) or {}
    teams = {}
    for team_name, team_def in teams_raw.items():
        if not isinstance(team_def, dict) or "manager" not in team_def:
            raise ValueError(f"team '{team_name}': missing required 'manager' field")
        teams[team_name] = TeamConfig(
            name=team_name,
            manager=team_def["manager"],
            workers=list(team_def.get("workers", []) or []),
        )

    defaults = fleet.get("defaults", {}) or {}

    # System defaults tier
    system_defaults_cfg = _coerce_system_defaults(fleet.get("system_defaults"))
    raw_system = _load_system_defaults()

    if not system_defaults_cfg.enabled:
        effective_system: dict = {}
    else:
        effective_system = {}
        if system_defaults_cfg.hooks:
            effective_system["hooks"] = raw_system.get("hooks", {})
        if system_defaults_cfg.observability:
            effective_system["observability"] = raw_system.get("observability", {})
        # fleet_timers are not merged into defaults — consumed by compose_fleet_timers()

    merged_defaults = _merge_system_into_defaults(effective_system, defaults)

    bots = {
        bot_name: _coerce_bot(bot_name, bot_def, merged_defaults)
        for bot_name, bot_def in (fleet.get("bots", {}) or {}).items()
    }

    fleet_cfg = FleetConfig(
        name=fleet.get("name", "unnamed-fleet"),
        service_prefix=fleet.get("service_prefix", "claudlobby"),
        telegram_group_chat_id=fleet.get("telegram_group_chat_id"),
        human_telegram_id=fleet.get("human_telegram_id"),
        accounts=fleet.get("accounts", {"default": "~/.claude"})
        or {"default": "~/.claude"},
        plugins=_coerce_plugins(fleet.get("plugins")),
        system_defaults=system_defaults_cfg,
        defaults=merged_defaults,
        teams=teams,
        bots=bots,
        sweep=_coerce_sweep(fleet.get("sweep")),
    )
    return fleet_cfg, merged_defaults
