"""fleet.yaml → typed config objects.

Loads and normalises the manifest. Applies fleet.defaults to each bot,
flattens lists, and resolves team membership.
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import defaults as _defaults

from .known_values import (
    KNOWN_EFFORTS,
    PROJECT_KEYS,
    SHELL_IDENT_RE,
    VALID_PERMISSION_MODES,
    closest_match,
)
from .path_audit import ExternalDecl, parse_external_decls

log = logging.getLogger(__name__)


# The env var the telegram channel plugin reads its token from. A token_env that
# names this same var is "self-referential" (the documented default in
# fleet.yaml.example): the compositor must not scaffold an empty stub for it — a
# defined-but-empty value is authoritative to the poller and poisons it (#749/#750).
TELEGRAM_PLUGIN_TOKEN_VAR = "TELEGRAM_BOT_TOKEN"


@dataclass
class TelegramConfig:
    handle: str | None = None
    token_env: str | None = None
    require_mention: bool = True
    chat_id: str | None = None

    @property
    def token_env_is_self_referential(self) -> bool:
        """True when token_env names the plugin's own read var (TELEGRAM_BOT_TOKEN).

        Such a token's home is the plugin's channel-dir .env fallback, not a
        compositor-scaffolded bot .env stub: the compositor skips scaffolding it and
        the validator skips its "not set" warning (that tier is not one validate
        inspects), so neither ever emits the poisoning empty export (#750).
        """
        return self.token_env == TELEGRAM_PLUGIN_TOKEN_VAR


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
class ToolPermissionsConfig:
    """Tool allow/deny lists → .claude/settings.local.json permissions.

    fleet.yaml key: `tool_permissions:`. Controls which Claude Code tools a
    bot may use. Deny rules generate permission deny patterns like
    "Write(**)", enforcing bot roles at the platform level rather than
    relying on CLAUDE.md prose alone.
    """

    deny: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)


@dataclass
class SystemDefaultsConfig:
    """Controls which system defaults are injected.

    ``system_defaults: false`` in fleet.yaml sets ``enabled=False``,
    disabling all injection.  Per-category bools allow surgical opt-out.

    KNOWN BOUND: the keys below are a fixed set, not "one per entity type".
    Ten of the twelve library entity types still have no opt-out, and an
    unrecognised key is silently dropped — so a fleet cannot yet tell a working
    opt-out from a typo (#1168 Phase 3 finding 2). Adding a key here is what
    gives a type an opt-out; the check is in the code that consumes the default.
    """

    enabled: bool = True
    hooks: bool = True
    timers: bool = True
    observability: bool = True
    guardrails: bool = True
    # Consumed in composer.py, not in the load_fleet merge below, because the
    # protocols default is availability-gated on a Paths fact this layer cannot
    # see. See defaults.AVAILABILITY_GATES.
    protocols: bool = True


@dataclass
class ObservabilityConfig:
    """Fleet observability settings — pulse interval and the watchdog thresholds.

    Composed into bot.conf as env vars so bot-vitals.sh and fleet-pulse.sh
    can read them at runtime.  Fields use None sentinel so _merge_observability
    can distinguish "not set" from "explicitly set to the default value".
    ``retired`` names the keys a manifest still sets that nothing reads any
    more (see _RETIRED_OBSERVABILITY_KEYS) — carried so `validate` can say so,
    never consumed by a door.
    """

    pulse_interval: int | None = None  # seconds between heartbeat pulses
    # seconds of no tool-call activity (while not idle) before a bot is flagged
    # activity_stuck — catches an animated-but-hung session that pane_stuck misses
    activity_stuck_threshold: int | None = None
    # seconds after a manager dispatch before an unanswered task is flagged
    # overdue_dispatch (manager-side watchdog)
    dispatch_deadline: int | None = None
    # enable the keepalive bridge-heal ladder for this bot. Routed
    # through structured observability config — not fleet-tier .env — because
    # keepalive loads bot.conf only; this is the one path that reaches its gate.
    bridge_heal: bool | None = None
    # heal bounce cap before the ladder escalates to a human (keepalive default 3)
    bridge_heal_max_attempts: int | None = None
    # enable the #1024 mirror watchdog: flag a worker that reported terminal and
    # was never re-dispatched. Structured config rather than fleet-tier .env for
    # the same reason as bridge_heal, one door over — the composed fleet-pulse
    # unit carries four fixed Environment= lines and sources no .env, so bot.conf
    # is the only path that reaches the check.
    unassigned_check: bool | None = None
    # seconds since the terminal report before the worker is flagged unassigned
    unassigned_threshold: int | None = None
    # seconds past which a strand stops being reported: a long-idle bot is a
    # known state, not an event, and re-paging it forever is how the alert
    # becomes wallpaper. The trade is real in both directions — past this age
    # the check also clears its debounce, so the signal stops being
    # distinguishable from "resolved" and a strand outliving the window goes
    # quiet. <= 0 disables the cap and keeps reporting forever.
    unassigned_max_age: int | None = None
    # keys a manifest still sets that have no reader (disclosed by the validator)
    retired: tuple[str, ...] = ()


# Field -> environment variable, for the fleet-pulse escalation knobs (#1120).
# ONE definition: the composer emits from it and freshbox's .env rung denies
# from it, so the emitter and the guard against misplacing them cannot drift
# into disagreeing about which keys are involved.
FLEET_PULSE_ENV_KEYS: dict[str, str] = {
    "escalation_threshold": "FLEET_PULSE_ESCALATION_THRESHOLD",
    "escalation_window": "FLEET_PULSE_ESCALATION_WINDOW",
    "escalation_chat_id": "FLEET_PULSE_ESCALATION_CHAT_ID",
    "renotify_after_s": "FLEET_PULSE_RENOTIFY_AFTER_S",
    "rearm_window_s": "FLEET_PULSE_REARM_WINDOW_S",
}


@dataclass
class FleetPulseConfig:
    """Fleet-pulse escalation knobs — the alert-volume controls (#1120).

    **Fleet-scoped, not per-bot**, and that is what decides the transport.
    ``lib/fleet-pulse.sh`` resolves these once at top level, outside its per-bot
    loop, so ``bot.conf`` — the tier ``bridge_heal`` and ``unassigned_check``
    use, per the note on :class:`ObservabilityConfig` — cannot carry them
    without electing an arbitrary bot's conf. That is precisely the
    directory-order fragility ``FLEET_PULSE_ESCALATION_CHAT_ID`` exists to
    avoid, so reusing that tier would cure the silence and inherit the bug.

    They ride the composed timer unit's ``Environment=`` instead — the same door
    that already carries ``TELEGRAM_GROUP_CHAT_ID`` into a fleet timer, for the
    same reason (a scheduler starts with almost no environment). Absent block ⇒
    ``None`` ⇒ nothing emitted and the script keeps its own defaults.
    """

    escalation_threshold: int | None = None
    escalation_window: int | None = None
    escalation_chat_id: str | None = None
    renotify_after_s: int | None = None
    rearm_window_s: int | None = None

    def env(self) -> dict[str, str]:
        """The subset actually set, as ``VAR: value``. Unset stays unset so the
        script's own default remains the single definition of each default."""
        out: dict[str, str] = {}
        for field_name, var in FLEET_PULSE_ENV_KEYS.items():
            value = getattr(self, field_name)
            if value is not None and str(value) != "":
                out[var] = str(value)
        return out


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
class BriefingConfig:
    """Bot-level ``briefing:`` feature stanza (#627).

    Presence equips the bot: the composer expands each slot into a per-(bot,slot)
    OnCalendar timer (``<prefix>.briefing-<bot>-<slot>``) and emits ``BRIEFING_*``
    into the bot's bot.conf. Slot names are free identifiers (a bot may run a
    custom ``analytics`` slot) but MUST be shell identifiers (known_values
    SHELL_IDENT_RE) — they become the ``BRIEFING_SECTIONS_<SLOT>`` env-var suffix
    — and each value is a systemd OnCalendar expression, never 5-field cron.
    """

    slots: dict[str, str] = field(default_factory=dict)  # slot -> OnCalendar
    sections: dict[str, list[str]] = field(default_factory=dict)  # slot -> sections
    sources: list[str] = field(default_factory=list)


@dataclass
class ProjectValidationConfig:
    """Per-project closure bar — what "done" requires (projects.yaml).

    Tier semantics: documentation/projects-yaml-schema.md (VALID_TIERS in
    known_values.py).
    """

    tier: str = "review"
    preview: dict[str, Any] = field(default_factory=dict)  # e.g. {source, require_ack}
    notes: str | None = None
    # Unrecognized validation-block keys — same .raw treatment as the top
    # level, so a tier typo ('teir') warns instead of silently defaulting.
    raw: dict[str, Any] = field(default_factory=dict)


_PROJECT_VALIDATION_KEYS = {"tier", "preview", "notes"}


@dataclass
class ProjectConfig:
    """One entry in projects.yaml — the third config tier; see
    documentation/projects-yaml-schema.md. `repos` is the join key mapping
    work to this project's validation tier."""

    key: str  # dict key — slug, same charset as bot ids
    title: str
    repos: list[str] = field(default_factory=list)
    mission_file: str | None = None  # overlay-relative pointer to a PROJECT_MISSION.md
    validation: ProjectValidationConfig = field(default_factory=ProjectValidationConfig)
    # Unrecognized top-level keys, preserved verbatim so the validator can
    # warn on them (metrics: lives here — reserved for the metrics plan).
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def env_slug(self) -> str:
        """The PROJECT_TIER_<SLUG>/PROJECT_REPOS_<SLUG> env-name fragment.

        The one Python home for the kebab->ENV transform; later phases add
        the bash twin in lib-common (first consumer: fleet-pulse gate_pending).
        """
        return self.key.upper().replace("-", "_")


def _shaped(label: str, value: Any, want: type, example: str) -> Any:
    """Generic YAML shape guard: None -> the type's empty default; wrong
    type -> ValueError naming the field and the expected shape. One
    mechanism for the whole family (a bool where a mapping goes, a list
    where a string goes, …) instead of per-repro patches."""
    if value is None:
        return want()
    if want is str:
        # exact-type: YAML scalars like 123/true are NOT silently stringified
        if not isinstance(value, str):
            raise ValueError(
                f"{label} must be a string (e.g. {example}), got {type(value).__name__}"
            )
        return value
    if not isinstance(value, want):
        shape = "a mapping" if want is dict else "a list"
        raise ValueError(
            f"{label} must be {shape} (e.g. {example}), got {type(value).__name__}"
        )
    return value


def _coerce_project(key: Any, d: Any) -> ProjectConfig:
    key = _shaped("project key", key, str, "acme-shop")
    d = _shaped(f"project '{key}'", d, dict, "a mapping of fields")
    v = _shaped(
        f"project '{key}': validation",
        d.get("validation"),
        dict,
        "validation: {tier: review}",
    )
    repos_raw = _shaped(
        f"project '{key}': repos", d.get("repos"), list, "repos: [org/repo]"
    )
    repos = [
        _shaped(f"project '{key}': repos entry", r, str, "org/repo") for r in repos_raw
    ]
    validation = ProjectValidationConfig(
        tier=_shaped(
            f"project '{key}': validation.tier", v.get("tier"), str, "tier: review"
        )
        or "review",
        preview=_shaped(
            f"project '{key}': validation.preview",
            v.get("preview"),
            dict,
            "preview: {source: vercel}",
        ),
        notes=_shaped(
            f"project '{key}': validation.notes",
            v.get("notes"),
            str,
            "notes: why this bar",
        )
        or None,
        raw={k: val for k, val in v.items() if k not in _PROJECT_VALIDATION_KEYS},
    )
    return ProjectConfig(
        key=key,
        title=_shaped(
            f"project '{key}': title", d.get("title"), str, "title: Acme Shop"
        )
        or key,
        repos=repos,
        mission_file=_shaped(
            f"project '{key}': mission_file",
            d.get("mission_file"),
            str,
            "missions/acme.md",
        )
        or None,
        validation=validation,
        raw={k: val for k, val in d.items() if k not in PROJECT_KEYS},
    )


def load_projects(projects_yaml: Path) -> dict[str, ProjectConfig]:
    """Parse projects.yaml (sits beside fleet.yaml). Absent file => {}."""
    if not projects_yaml.is_file():
        return {}
    with projects_yaml.open() as f:
        doc = yaml.safe_load(f)
    if doc is None:
        return {}  # an all-comments file is still an optional file
    if not isinstance(doc, dict) or "projects" not in doc:
        raise ValueError(f"{projects_yaml}: top-level key 'projects' missing")
    projects = doc.get("projects") or {}
    if not isinstance(projects, dict):
        raise ValueError(
            f"{projects_yaml}: 'projects' must be a mapping of "
            f"<project-key>: <fields>, got {type(projects).__name__}"
        )
    return {key: _coerce_project(key, pdef) for key, pdef in projects.items()}


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

    def instance_prefix(self, instance: str) -> str:
        """Env var prefix for an instance. E.g., gws/personal → GWS_PERSONAL_."""
        if instance == "default":
            return f"{self.name.upper().replace('-', '_')}_"
        return f"{self.name.upper().replace('-', '_')}_{instance.upper().replace('-', '_')}_"

    def output_name(self, instance: str) -> str:
        """Server name for an instance — the composed .mcp.json server key and the
        ``mcp__<name>__*`` grant prefix both derive from this one convention, so they
        stay in lockstep. E.g., gws/personal → ``gws-personal``; default → ``gws``."""
        return self.name if instance == "default" else f"{self.name}-{instance}"


@dataclass
class ToolEntry:
    """Parsed tools entry from fleet.yaml (library tools category ref).

    Supports two forms:
      - `"audit-tracker"` → name="audit-tracker", params={}
      - `{"portfolio-snapshot": {"params": {...}}}` → per-bot param overrides
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)


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
class GithubAppConfig:
    """Per-bot GitHub App git-auth routing (App-auth P3 #1273).

    The credential VALUES ride the ``GITHUB_APP_*`` env contract the
    ``github-app`` MCP fragment declares (fleet tier); this field only decides
    whether and how the composed per-bot gitconfig routes git auth through the
    App helper. ``slug`` + ``bot_user_id`` together arm the composed commit
    identity (F-identity variant a: ``<slug>[bot]`` +
    ``<id>+<slug>[bot]@users.noreply.github.com``); omitting either keeps the
    operator identity flowing through the include (variant b) — one field
    pair, no code fork. ``orgs`` scopes the App helper (and the ssh→https
    rewrite, and whether the gh fallback is kept) to those orgs; empty means
    host-generic github.com.
    """

    slug: str | None = None
    bot_user_id: int | None = None
    orgs: list[str] = field(default_factory=list)

    @property
    def host_generic(self) -> bool:
        """No org scoping: the App serves all of github.com. The composer keys
        the gh-fallback SUPPRESSION on this, and freshbox mirrors the same
        emission decision — one predicate so the audit and the artifact cannot
        drift apart."""
        return not self.orgs

    @property
    def composes_identity(self) -> bool:
        """Both identity fields set -> the composed [user] block arms
        (F-identity variant a). The freshbox D7 split and the validator's
        include-reliance warn both key on the same condition."""
        return bool(self.slug and self.bot_user_id)


# name -> operator-facing description, the ONE mapping every Python surface
# spells the App env trio from (composer registry, validator warns, freshbox
# key audit) — the same emitter-cannot-disagree-with-guard rule
# FLEET_PULSE_ENV_KEYS states. The fragment (library/mcp/github-app.json)
# stays the contract-declaration surface; this is the code-side mirror.
GITHUB_APP_ENV_VARS: dict[str, str] = {
    "GITHUB_APP_ID": "GitHub App ID (numeric; git-auth routing)",
    "GITHUB_APP_INSTALLATION_ID": "GitHub App installation ID (git-auth routing)",
    "GITHUB_APP_PRIVATE_KEY_PATH": "Path to the App private-key .pem (0600; git-auth routing)",
}


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
    # org -> env var NAME; see _parse_git_credentials
    git_credentials: dict[str, str] = field(default_factory=dict)
    # GitHub App git-auth routing (App-auth P3 #1273, fork F8: a dedicated
    # field, never a git_credentials value form — that parser hard-rejects
    # non-identifier values by design). None = App mode off for this bot.
    github_app: GithubAppConfig | None = None
    # env var NAME -> credential source identifier. The per-scope override the
    # tier ruling requires (#1214 F6c): a contract declares where a value comes
    # from BY DEFAULT, and a fleet or a single bot may say otherwise for itself
    # — "(a) should work if configured at bot, fleet, or host level".
    # Fleet-then-bot merged, bot winning, exactly as git_credentials is.
    # SCHEMA ONLY in this phase: nothing resolves these yet (F1(a) ships `cli`
    # and the start-bot.sh resolver is a later phase). Declaring one today
    # records intent and shows up in the register; it does not fetch anything.
    credential_sources: dict[str, str] = field(default_factory=dict)
    # Equipment names (MCP + integration) this bot declares ITSELF, as opposed
    # to inheriting from `fleet.defaults`. The merge that builds `mcp` and
    # `integrations` is flat and first-seen-wins, so it destroys this — and it
    # is exactly what decides which `.env` a var's stub belongs in (#1214 F3a:
    # "tier follows the equipment"). A GitHub App given to ONE bot is that
    # bot's equipment; its var stub belongs in that bot's .env, not the fleet's.
    bot_attached_equipment: frozenset[str] = field(default_factory=frozenset)
    model_strategy: ModelStrategyConfig | None = None
    account: str = "default"
    model: str | None = None
    effort: str | None = None
    # Claude Code CLI flags — composed into CLAUDE_FLAGS in bot.conf.
    remote_control: bool = True  # --remote-control
    # Conservative default: with neither field set the composer emits
    # `--permission-mode acceptEdits` (see compose_bot_conf). dangerously_skip is
    # an explicit opt-in.
    dangerously_skip_permissions: bool = False  # --dangerously-skip-permissions
    permission_mode: str | None = None  # --permission-mode (wins over the skip flag)
    # First-run consent skip-flags → settings.local.json. Distinct from the
    # --dangerously-skip-permissions CLI flag above: these suppress the interactive
    # first-run permission prompts a headless bot would otherwise hang on. Default
    # True = skip the prompts.
    # settings.local.json skipAutoPermissionPrompt
    skip_auto_permission_prompt: bool = True
    # settings.local.json skipDangerousModePermissionPrompt
    skip_dangerous_mode_permission_prompt: bool = True
    channels: list[str] = field(
        default_factory=lambda: ["plugin:telegram@claude-plugins-official"]
    )  # --channels <name>
    prompt_suggestions: bool = False  # CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION
    # Headless config defaults (fleet.yaml-overridable per bot) — trim Claude Code
    # affordances a supervised, non-interactive bot never uses.
    # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC (bot.conf env)
    disable_nonessential_traffic: bool = True
    spinner_tips_enabled: bool = False  # settings.local.json spinnerTipsEnabled
    # settings.local.json preferredNotifChannel
    preferred_notif_channel: str = "notifications_disabled"
    prefers_reduced_motion: bool = True  # settings.local.json prefersReducedMotion
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
    tools: list[ToolEntry] = field(default_factory=list)
    tool_permissions: ToolPermissionsConfig = field(
        default_factory=ToolPermissionsConfig
    )
    hooks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    mounts: dict[str, str] = field(default_factory=dict)  # name → absolute host path
    env: dict[str, str] = field(default_factory=dict)
    # ENV_VAR → fleet-relative path of a secret file (service-account keys,
    # credentials living inside the fleet). The composer anchors the value on
    # FLEET_ROOT so the path is derived, never hand-typed absolute; the secret
    # VALUES stay in .env. Fleet-relative by contract — an absolute path is
    # rejected at compose.
    secret_files: dict[str, str] = field(default_factory=dict)
    # Absolute paths OUTSIDE the fleet overlay that a compose source may
    # legitimately reference — a genuine external dependency (a mount source, a
    # system tool tree). Each is a {path, purpose} declaration; the L1 source
    # guard denies any absolute that is neither anchored on a composer path
    # (FLEET_ROOT / BOT_DIR / CLAUDLOBBY_ROOT) nor blessed here.
    external_paths: list[ExternalDecl] = field(default_factory=list)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    startup_prompt: str | None = None
    bench: bool = False  # marks the bot as the fleet's benchmarking target
    # Ecosystem-aware fields — optional integration with clauDNA, Claudron, Claudosseum
    claudna_version: str | None = None
    claudron_vault_path: str | None = None
    # Wire the Claudron session loop (L2): the composer installs the engine's
    # SessionStart/PreCompact/SessionEnd hooks + narrow verb grants. Tri-state:
    # None = unset → defaults True when claudron_vault_path is set, False
    # otherwise (resolved in composer._session_loop_enabled); an explicit bool
    # overrides that default.
    claudron_session_loop: bool | None = None
    claudosseum_tenant_id: str | None = None
    autonomous_runner: AutonomousRunnerConfig | None = None
    briefing: BriefingConfig | None = None  # equippable briefing feature (#627)
    # #904 M1 (epic #1102 R3, fork R3-F1): SessionStart boot brief. Default off;
    # arming is additionally gated at compose time on the installed CLI exposing
    # `brief --boot` (composed settings outlive installs on this estate).
    brief_on_start: bool = False


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


# Built-in defaults — installed + enabled on every bot unless
# plugins.include_defaults is false. claudna carries the fleet skill set;
# superpowers carries the process skills every bot relies on (brainstorming,
# TDD, systematic-debugging), so a fresh box must restore it too — not only
# claudna. The telegram channel plugin is NOT listed here: it is derived
# per-bot from `channels` (composer._channel_plugins) so each fleet restores
# whichever telegram marketplace its `--channels` flag actually pins.
DEFAULT_MARKETPLACES: dict[str, dict] = {
    "Claudfather": {"source": {"source": "github", "repo": "Claudfather/clauDNA"}},
    "claude-plugins-official": {
        "source": {"source": "github", "repo": "anthropics/claude-plugins-official"}
    },
}

DEFAULT_PLUGINS: list[str] = [
    "claudna@Claudfather",
    "superpowers@claude-plugins-official",
]

# Guardrails composed onto every bot unless system_defaults.guardrails is false.
#
# DECLARED IN claudlobby/defaults.py, not here (#1168). That module's REGISTRY
# is the single source for every entity type's compositor-side default, and this
# is a re-export so existing importers keep working. It is imported rather than
# restated deliberately: a constant declared in two places drifts, and the test
# then passes against whichever copy is not the one that composes — the failure
# behind #1046 and #892/#1143.
#
# The membership test this entry had to clear, and the tier it belongs to, live
# beside it in the registry.
DEFAULT_GUARDRAILS = _defaults.DEFAULT_GUARDRAILS


@dataclass
class WorkstreamsConfig:
    """fleet.workstreams knobs — the anti-rot bounds for the P5 registry.

    max_active: the manager-attention-span cap on concurrently active
    workstreams (`workstream-update.sh open` refuses past it). lease_days:
    how long a workstream keeps its slot without progress before the
    (follow-up PR) fleet-pulse stall check will flag it. Both compose into
    every bot.conf as WORKSTREAM_MAX_ACTIVE / WORKSTREAM_LEASE_DAYS so the
    single-writer helper (and the future pulse checks) read one fleet-wide
    value. `raw` keeps the original block for validation.
    """

    max_active: int = 12
    lease_days: int = 14
    raw: dict = field(default_factory=dict)


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
    fleet_pulse: FleetPulseConfig | None = None
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    # Fleet-level mission: `mission` is the one-paragraph anchor EVERY bot
    # receives; `mission_file` points at a fuller charter (fleet-relative)
    # composed for managers only; mission_file REQUIRES mission (validator-
    # enforced). Rationale + locked decisions (F6/B2):
    # documentation/plans/2026-07-06-goal-aware-fleet-portfolio.md
    mission: str | None = None
    mission_file: str | None = None
    # Workstream registry bounds (P5). Always present (defaults apply when the
    # fleet omits the block) so the composer can emit WORKSTREAM_* unconditionally.
    workstreams: WorkstreamsConfig = field(default_factory=WorkstreamsConfig)

    def sweep_enabled(self) -> bool:
        """True when the opt-in code-audit sweep is configured and enabled."""
        return bool(self.sweep and self.sweep.enabled)

    def briefing_enabled(self) -> bool:
        """True when any bot equips the briefing feature (bots.<bot>.briefing)."""
        return any(b.briefing and b.briefing.slots for b in self.bots.values())

    def manager_bots(self) -> set[str]:
        """Bot names that manage anyone — a team in this fleet, or bots anywhere.

        Two declarations, because a manager's reports are not always in the same
        fleet. ``teams:`` names a within-fleet manager and answers "does a team
        here name this bot". ``bots.<id>.manages:`` names the reports directly,
        and is the only one of the two that can express a CROSS-FLEET report — a
        top-level coordinator whose reports are themselves managers of other
        fleets is named by no ``teams:`` block anywhere, and so was invisible
        here despite the fleet declaring exactly who it manages.

        That is not a stretch of the schema: ``_validate_teams`` already warns
        rather than errors on a ``manages`` target outside ``fleet.bots``,
        precisely because "bot_ids may reference other fleets".

        Widely consumed, including by guards where a missing bot silently loses
        a protection rather than merely being mislabelled — so widen it here,
        once, rather than special-casing whichever consumer notices first.
        """
        from_teams = {team.manager for team in self.teams.values()}
        # An empty or absent `manages:` list is not a claim to manage anyone.
        from_manages = {name for name, bot in self.bots.items() if bot.manages}
        return from_teams | from_manages

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


def _parse_tools_list(raw_list, *, where: str) -> list[ToolEntry]:
    """Parse the `tools:` list (library tool refs) into ToolEntry objects.

    Accepts:
      - "audit-tracker"                       → ToolEntry(name, params={})
      - {"portfolio-snapshot": {"params": {...}}} → per-bot param overrides

    A mapping (the pre-rename allow/deny permissions shape) is a hard error
    with a migration message — no shim.
    """
    if not raw_list:
        return []
    if isinstance(raw_list, dict):
        raise ValueError(
            f"{where}: 'tools:' is now the tool-attachment list — move the "
            "allow/deny block to 'tool_permissions:'"
        )
    entries: list[ToolEntry] = []
    seen: set[str] = set()
    for item in raw_list:
        if isinstance(item, str):
            name, params = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, cfg = next(iter(item.items()))
            cfg = cfg or {}
            if not isinstance(cfg, dict):
                raise ValueError(f"{where}: tool '{name}' config must be a mapping")
            params = dict(cfg.get("params") or {})
        else:
            raise ValueError(f"{where}: unrecognized tools entry {item!r}")
        if name in seen:
            continue
        seen.add(name)
        entries.append(ToolEntry(name=name, params=params))
    return entries


def _merge_tool_lists(
    default: list[ToolEntry], override: list[ToolEntry]
) -> list[ToolEntry]:
    """Merge tools lists by name (defaults-first order); when both declare a
    tool, per-bot params override defaults per key."""
    merged: dict[str, ToolEntry] = {}
    for entry in [*default, *override]:
        if entry.name in merged:
            merged[entry.name] = ToolEntry(
                name=entry.name,
                params={**merged[entry.name].params, **entry.params},
            )
        else:
            merged[entry.name] = ToolEntry(name=entry.name, params=dict(entry.params))
    return list(merged.values())


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


# GitHub's documented token prefixes. A token is a valid shell identifier, so
# this is the only way to tell a pasted secret from an env var name.
_GITHUB_TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")

# GitHub org/user login charset (GitHub's own rule: alphanumerics and single
# hyphens, no leading/trailing/double hyphen). An org name is interpolated
# UNESCAPED into composed .gitconfig subsection headers and insteadOf values,
# so an unvalidated value (a quote, a newline, a `#`) is a config-injection
# sink — the ceiling is a composed core.sshCommand, i.e. RCE at the next git
# call. The `/`-only rejection both git-routing parsers had cannot catch it.
_GITHUB_ORG_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9])*$")


def _check_org_name(org: str, *, where: str, field: str) -> None:
    """Reject an org value that is not a real GitHub login (config-injection
    guard shared by git_credentials and github_app.orgs)."""
    if not _GITHUB_ORG_RE.match(org):
        raise ValueError(
            f"{where}: {field} value {org!r} is not a valid GitHub org name "
            "(letters, digits, single hyphens) — it is interpolated into the "
            "composed .gitconfig and must not carry config-control characters"
        )


def _parse_credential_sources(raw: object, *, where: str) -> dict[str, str]:
    """Validate one scope's ``credential_sources`` block (VAR -> source id).

    One scope at a time with a ``where`` label so a mistake in fleet defaults is
    not reported against a bot; callers dict-merge fleet then bot. The
    ``_parse_git_credentials`` precedent, deliberately — a second shape for the
    same job is how two blocks that must merge identically stop doing so.

    Keys are env var NAMES and must satisfy the same ``SHELL_IDENT_RE`` contract
    as every other composed env-var name. Values are source identifiers; that
    they are members of the CLOSED registry is checked by the validator, on the
    same footing as a contract's own ``source``, so both surfaces produce one
    error shape and neither can be widened without the other.
    """
    mapping = _shaped(
        f"{where}: credential_sources", raw, dict, "{GITHUB_PAT: cli:gh-token}"
    )
    out: dict[str, str] = {}
    for key, source in mapping.items():
        var = str(key)
        if not SHELL_IDENT_RE.fullmatch(var):
            raise ValueError(
                f"{where}: credential_sources key '{var}' must be an env var NAME "
                f"(letters, digits, underscore; not starting with a digit)"
            )
        if not isinstance(source, str) or not source:
            raise ValueError(
                f"{where}: credential_sources['{var}'] must be a source identifier "
                f"string from the closed registry (e.g. 'cli:gh-token', 'literal'), "
                f"got {type(source).__name__}"
            )
        out[var] = source
    return out


def _parse_git_credentials(raw: object, *, where: str) -> dict[str, str]:
    """Validate one tier's ``git_credentials`` block (org -> env var NAME).

    One tier at a time, with a ``where`` label, so a mistake in fleet defaults is
    not reported against a bot (the ``_parse_tools_list`` precedent). Callers
    dict-merge the tiers.

    Values are env var NAMES, never tokens: names are contracts and belong in
    git, values live in the gitignored .env. The name is interpolated into a
    shell function in the composed .gitconfig, so it must satisfy the same
    SHELL_IDENT_RE contract as every other composed env-var name (bot.env keys,
    secret_files keys, project tier vars).
    """
    mapping = _shaped(
        f"{where}: git_credentials", raw, dict, "{MyOrg: MYORG_GITHUB_PAT}"
    )
    out: dict[str, str] = {}
    for key, env_name in mapping.items():
        org = str(key)
        if "/" in org:
            raise ValueError(
                f"{where}: git_credentials key '{org}' must be an org name, not "
                f"'org/repo' — credential routing is org-scoped and a repo-scoped "
                f"key would silently never match"
            )
        _check_org_name(org, where=where, field="git_credentials key")
        if not isinstance(env_name, str) or not SHELL_IDENT_RE.match(env_name):
            raise ValueError(
                f"{where}: git_credentials['{org}'] must be an env var NAME "
                f"(e.g. MYORG_GITHUB_PAT), not a token value — got {env_name!r}"
            )
        # SHELL_IDENT_RE alone cannot catch this: a real token like
        # "ghp_xxx" / "github_pat_xxx" IS a valid shell identifier, so it would
        # pass as a "name" and then be interpolated verbatim into the composed,
        # world-readable .gitconfig. Refuse the known GitHub token prefixes —
        # the one place a leak could be introduced by a plausible typo.
        if env_name.lower().startswith(_GITHUB_TOKEN_PREFIXES):
            raise ValueError(
                f"{where}: git_credentials['{org}'] looks like a TOKEN VALUE, not "
                f"an env var name. Put the value in .env and name the var here"
            )
        out[org] = env_name
    return out


_GITHUB_APP_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_github_app(raw: object, *, where: str) -> dict:
    """Validate one tier's ``github_app`` block into a plain field dict.

    Returns a DICT (not the dataclass) so the fleet→bot merge is per-field:
    a bot restating one key must not silently reset the others to defaults.
    Strict-bool ``enabled`` per the ``_parse_brief`` arming-knob precedent —
    this knob changes how every git push authenticates, and a typo must not
    arm it. Org entries reuse the no-``/`` rule from ``_parse_git_credentials``
    (credential routing is org-scoped; a repo-scoped key silently never
    matches).
    """
    if raw is None:
        return {}
    raw = _shaped(f"{where}: github_app", raw, dict, "{slug: my-app, bot_user_id: 123}")
    out: dict = {}
    if "enabled" in raw:
        out["enabled"] = _strict_bool(f"{where}: github_app.enabled", raw["enabled"])
    if "slug" in raw and raw["slug"] is not None:
        slug = raw["slug"]
        if not isinstance(slug, str) or not _GITHUB_APP_SLUG_RE.match(slug):
            raise ValueError(
                f"{where}: github_app.slug must be the App URL slug "
                f"(lowercase letters, digits, hyphens), got {slug!r}"
            )
        out["slug"] = slug
    if "bot_user_id" in raw and raw["bot_user_id"] is not None:
        if not is_pos_int(raw["bot_user_id"]):
            raise ValueError(
                f"{where}: github_app.bot_user_id must be a positive integer "
                f"(the App BOT USER id, not the App id), got {raw['bot_user_id']!r}"
            )
        out["bot_user_id"] = raw["bot_user_id"]
    if "orgs" in raw and raw["orgs"] is not None:
        orgs = raw["orgs"]
        if not isinstance(orgs, list) or not all(isinstance(o, str) for o in orgs):
            raise ValueError(
                f"{where}: github_app.orgs must be a list of org names, got {orgs!r}"
            )
        for org in orgs:
            if "/" in org or not org:
                raise ValueError(
                    f"{where}: github_app.orgs entry {org!r} must be an org name, "
                    "not 'org/repo' — credential routing is org-scoped and a "
                    "repo-scoped key would silently never match"
                )
            _check_org_name(org, where=where, field="github_app.orgs")
        # De-dup while preserving order: a repeated org would compose duplicate
        # credential/insteadOf sections.
        out["orgs"] = list(dict.fromkeys(orgs))
    unknown = set(raw) - {"enabled", "slug", "bot_user_id", "orgs"}
    if unknown:
        raise ValueError(
            f"{where}: github_app has unknown key(s) {sorted(unknown)!r} — "
            "known: enabled, slug, bot_user_id, orgs"
        )
    return out


def _merge_github_app(defaults_raw: object, bot_raw: object, *, bot_name: str):
    """Fleet-defaults → bot per-field merge; ``enabled: false`` after the
    merge normalizes to None (a per-bot opt-out of a fleet-wide default)."""
    merged = {
        **_parse_github_app(defaults_raw, where="fleet defaults"),
        **_parse_github_app(bot_raw, where=f"bot '{bot_name}'"),
    }
    if not merged:
        return None
    if merged.get("enabled") is False:
        return None
    merged.pop("enabled", None)
    return GithubAppConfig(**merged)


def is_pos_int(v: object) -> bool:
    """True for a positive int. bool is excluded — it is an int subclass, so
    `True`/`False` would otherwise slip through as 1/0. Shared by the tolerant
    loader (below) and the validator so 'valid' is defined in exactly one place."""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _coerce_workstreams(raw: dict | None) -> WorkstreamsConfig:
    """Parse fleet.workstreams. Tolerant: a bad value falls back to the default
    so `generate` never crashes — `claudlobby validate` surfaces the error."""
    if not raw:
        return WorkstreamsConfig()

    def _pos_int(key: str, default: int) -> int:
        val = raw.get(key, default)
        return val if is_pos_int(val) else default

    return WorkstreamsConfig(
        max_active=_pos_int("max_active", WorkstreamsConfig.max_active),
        lease_days=_pos_int("lease_days", WorkstreamsConfig.lease_days),
        raw=dict(raw),
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


def _coerce_tool_permissions(raw: dict | None) -> ToolPermissionsConfig:
    if not raw:
        return ToolPermissionsConfig()
    return ToolPermissionsConfig(
        deny=list(raw.get("deny") or []),
        allow=list(raw.get("allow") or []),
    )


def _merge_tool_permissions(
    default: ToolPermissionsConfig, override: ToolPermissionsConfig
) -> ToolPermissionsConfig:
    """Merge tool-permission configs — lists are unioned, deduplicated."""
    return ToolPermissionsConfig(
        deny=_merge_lists(default.deny, override.deny),
        allow=_merge_lists(default.allow, override.allow),
    )


# Keys that once meant something and now have no reader. `observability.reap_days`
# aged the per-bot event files (fleet-pulse's reap_events, keepalive's own
# reaper); the F18 closure (#1467) removed the files and the reapers with them,
# and the plane's `plane prune` retention took over. A manifest that still sets
# one is not refused (the loader must not crash on an old fleet.yaml) — it is
# RECORDED here and `claudlobby validate` warns, naming the key.
_RETIRED_OBSERVABILITY_KEYS: dict[str, str] = {
    "reap_days": "no reader since the F18 closure (#1467) — the event files it aged are gone;"
                 " the plane's `plane prune` retention replaced them",
}


def _coerce_observability(raw: dict | None) -> ObservabilityConfig:
    if not raw:
        return ObservabilityConfig()
    pi = raw.get("pulse_interval")
    ast = raw.get("activity_stuck_threshold")
    dd = raw.get("dispatch_deadline")
    bh = raw.get("bridge_heal")
    bhma = raw.get("bridge_heal_max_attempts")
    uc = raw.get("unassigned_check")
    ut = raw.get("unassigned_threshold")
    uma = raw.get("unassigned_max_age")
    return ObservabilityConfig(
        pulse_interval=int(pi) if pi is not None else None,
        activity_stuck_threshold=int(ast) if ast is not None else None,
        dispatch_deadline=int(dd) if dd is not None else None,
        bridge_heal=bool(bh) if bh is not None else None,
        bridge_heal_max_attempts=int(bhma) if bhma is not None else None,
        unassigned_check=bool(uc) if uc is not None else None,
        unassigned_threshold=int(ut) if ut is not None else None,
        unassigned_max_age=int(uma) if uma is not None else None,
        retired=tuple(k for k in _RETIRED_OBSERVABILITY_KEYS if k in raw),
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


def _coerce_fleet_pulse(raw: dict | None) -> FleetPulseConfig | None:
    """Coerce the fleet-level ``fleet_pulse:`` block. None when absent."""
    if not isinstance(raw, dict):
        return None

    def _int(key: str) -> int | None:
        v = raw.get(key)
        return None if v is None or v == "" else int(v)

    chat = raw.get("escalation_chat_id")
    return FleetPulseConfig(
        escalation_threshold=_int("escalation_threshold"),
        escalation_window=_int("escalation_window"),
        escalation_chat_id=None if chat is None else str(chat),
        renotify_after_s=_int("renotify_after_s"),
        rearm_window_s=_int("rearm_window_s"),
    )


def _coerce_briefing(raw: dict | None) -> BriefingConfig | None:
    """Coerce a bot's ``briefing:`` block. None when absent (opt-out).

    Hard-rejects at parse time (surfaced friendly by ``_load_fleet_or_exit``) so
    a bad stanza fails ``validate`` AND ``generate`` before it can corrupt
    bot.conf or a timer: empty/non-map slots, a non-shell-identifier slot name
    (would break ``BRIEFING_SECTIONS_<SLOT>`` sourcing), or a 5-field cron value
    (the timer chain speaks OnCalendar, not cron).
    """
    if not raw:
        return None
    raw = _shaped("briefing", raw, dict, "briefing: {slots: {morning: ...}}")
    slots_raw = _shaped(
        "briefing.slots", raw.get("slots"), dict, "slots: {morning: '*-*-* 08:30:00'}"
    )
    if not slots_raw:
        raise ValueError(
            "briefing.slots must declare at least one slot "
            "(e.g. slots: {morning: '*-*-* 08:30:00'})"
        )
    slots: dict[str, str] = {}
    for name, expr in slots_raw.items():
        name = str(name)
        if not SHELL_IDENT_RE.match(name):
            raise ValueError(
                f"briefing slot name {name!r} is not a shell identifier "
                "([A-Za-z_][A-Za-z0-9_]*) — it becomes the BRIEFING_SECTIONS_<SLOT> "
                "env-var suffix"
            )
        expr = _shaped(f"briefing.slots.{name}", expr, str, "'*-*-* 08:30:00'")
        if len(expr.split()) == 5:
            raise ValueError(
                f"briefing slot {name!r} value {expr!r} looks like 5-field cron; "
                "use a systemd OnCalendar expression (e.g. '*-*-* 08:30:00')"
            )
        slots[name] = expr
    sections_raw = _shaped(
        "briefing.sections",
        raw.get("sections"),
        dict,
        "sections: {morning: [overnight]}",
    )
    sections = {
        str(k): [
            str(s) for s in _shaped(f"briefing.sections.{k}", v, list, "[overnight]")
        ]
        for k, v in sections_raw.items()
    }
    sources = [
        str(s) for s in _shaped("briefing.sources", raw.get("sources"), list, "[gmail]")
    ]
    return BriefingConfig(slots=slots, sections=sections, sources=sources)


def _merge_observability(
    default: ObservabilityConfig, override: ObservabilityConfig
) -> ObservabilityConfig:
    """Merge observability — override wins when not None, else default."""
    return ObservabilityConfig(
        pulse_interval=override.pulse_interval
        if override.pulse_interval is not None
        else default.pulse_interval,
        activity_stuck_threshold=override.activity_stuck_threshold
        if override.activity_stuck_threshold is not None
        else default.activity_stuck_threshold,
        dispatch_deadline=override.dispatch_deadline
        if override.dispatch_deadline is not None
        else default.dispatch_deadline,
        bridge_heal=override.bridge_heal
        if override.bridge_heal is not None
        else default.bridge_heal,
        bridge_heal_max_attempts=override.bridge_heal_max_attempts
        if override.bridge_heal_max_attempts is not None
        else default.bridge_heal_max_attempts,
        unassigned_check=override.unassigned_check
        if override.unassigned_check is not None
        else default.unassigned_check,
        unassigned_threshold=override.unassigned_threshold
        if override.unassigned_threshold is not None
        else default.unassigned_threshold,
        unassigned_max_age=override.unassigned_max_age
        if override.unassigned_max_age is not None
        else default.unassigned_max_age,
        retired=tuple(dict.fromkeys(default.retired + override.retired)),
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


def _strict_bool(label: str, value: object) -> bool:
    """Arming-knob boolean: only a real YAML bool passes (#904 M1 precedent).

    The loose bool() coercion the cosmetic knobs use would arm on any typo —
    a YAML string is truthy — and a silently armed knob is the
    no-silent-switches failure exactly."""
    if not isinstance(value, bool):
        raise ValueError(
            f"{label} must be a YAML boolean, got {value!r} — "
            "an arming knob must not switch on via a typo"
        )
    return value


def _parse_brief(raw: Any) -> bool:
    """Parse the bot-level ``brief:`` stanza (#904 M1) to its arming bool.

    STRICT on purpose: this knob arms runtime behavior (a composed SessionStart
    hook), and the loose ``bool()`` coercion the cosmetic knobs use would arm it
    on any typo — ``on_start: tue`` is a YAML *string*, truthy under ``bool()``,
    and a silently armed knob is the no-silent-switches failure exactly. Only a
    real YAML boolean arms; anything else is a parse error naming the value.
    """
    raw = _shaped("brief", raw, dict, "{on_start: true}")
    return _strict_bool("'brief.on_start'", raw.get("on_start", False))


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

    def _str(key: str, fallback: str) -> str:
        if key in raw:
            return str(raw[key])
        if key in defaults:
            return str(defaults[key])
        return fallback

    def _tristate(key: str) -> bool | None:
        """Presence-based tri-state: an explicit *non-null* bot/fleet value wins,
        else None — the unset sentinel the composer resolves to its vault-presence
        default (unlike `_bool`, which collapses unset to a fixed fallback). A
        YAML null (`key:` with no value) is treated as unset, not False, so a bare
        key never silently disables a vault-wired loop (the sibling string fields
        like `claudron_vault_path` fall through on null the same way)."""
        if raw.get(key) is not None:
            return bool(raw[key])
        if defaults.get(key) is not None:
            return bool(defaults[key])
        return None

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
        git_credentials={
            **_parse_git_credentials(
                defaults.get("git_credentials"), where="fleet defaults"
            ),
            **_parse_git_credentials(raw.get("git_credentials"), where=f"bot '{name}'"),
        },
        github_app=_merge_github_app(
            defaults.get("github_app"), raw.get("github_app"), bot_name=name
        ),
        credential_sources={
            **_parse_credential_sources(
                defaults.get("credential_sources"), where="fleet defaults"
            ),
            **_parse_credential_sources(
                raw.get("credential_sources"), where=f"bot '{name}'"
            ),
        },
        model_strategy=_coerce_model_strategy(
            raw.get("model_strategy") or defaults.get("model_strategy")
        ),
        account=raw.get("account", defaults.get("account", "default")),
        model=raw.get("model", defaults.get("model")),
        effort=_parse_enum(
            "effort", raw.get("effort", defaults.get("effort")), KNOWN_EFFORTS
        ),
        remote_control=_bool("remote_control", True),
        dangerously_skip_permissions=_bool("dangerously_skip_permissions", False),
        permission_mode=_parse_enum(
            "permission_mode",
            raw.get("permission_mode") or defaults.get("permission_mode"),
            VALID_PERMISSION_MODES,
        ),
        skip_auto_permission_prompt=_bool("skip_auto_permission_prompt", True),
        skip_dangerous_mode_permission_prompt=_bool(
            "skip_dangerous_mode_permission_prompt", True
        ),
        prompt_suggestions=_bool("prompt_suggestions", False),
        disable_nonessential_traffic=_bool("disable_nonessential_traffic", True),
        spinner_tips_enabled=_bool("spinner_tips_enabled", False),
        preferred_notif_channel=_str(
            "preferred_notif_channel", "notifications_disabled"
        ),
        prefers_reduced_motion=_bool("prefers_reduced_motion", True),
        # Presence-based via `.get` defaults (not `x or default`): an explicit
        # `channels: []` must win over the fleet default — the documented way
        # to make a bot channel-less, e.g. to pair with RC-killing env (see the
        # validator's #533 guard). A deliberate one-off: sibling fields use
        # `x or default`, but channels is the only one whose explicit-empty is
        # a load-bearing contract worth the exception.
        channels=_as_list(
            raw.get(
                "channels",
                defaults.get("channels", "plugin:telegram@claude-plugins-official"),
            )
        ),
        extra_flags=_merge_lists(defaults.get("extra_flags"), raw.get("extra_flags")),
        skills=_merge_lists(defaults.get("skills"), raw.get("skills")),
        mcp=_merge_mcp_lists(
            _parse_mcp_list(defaults.get("mcp")), _parse_mcp_list(raw.get("mcp"))
        ),
        integrations=_merge_lists(
            defaults.get("integrations"), raw.get("integrations")
        ),
        # Read from `raw` ONLY — `defaults` is the fleet-attached half by
        # definition, and including it would make every bot claim every piece
        # of fleet equipment as its own.
        bot_attached_equipment=frozenset(
            [e.name for e in _parse_mcp_list(raw.get("mcp"))]
            + [str(i).split("/")[-1] for i in _as_list(raw.get("integrations"))]
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
        tools=_merge_tool_lists(
            _parse_tools_list(defaults.get("tools"), where="defaults"),
            _parse_tools_list(raw.get("tools"), where=f"bot '{name}'"),
        ),
        tool_permissions=_merge_tool_permissions(
            _coerce_tool_permissions(defaults.get("tool_permissions")),
            _coerce_tool_permissions(raw.get("tool_permissions")),
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
        secret_files={
            **(defaults.get("secret_files") or {}),
            **(raw.get("secret_files") or {}),
        },
        # Union defaults ∪ bot, then validate the merged set once. A list of
        # {path, purpose} mappings — not a dict — so it concatenates rather than
        # dict-spreads; parse_external_decls dedupes and hardens.
        external_paths=parse_external_decls(
            [
                *(defaults.get("external_paths") or []),
                *(raw.get("external_paths") or []),
            ]
        ),
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
        claudron_session_loop=_tristate("claudron_session_loop"),
        claudosseum_tenant_id=raw.get("claudosseum_tenant_id")
        or defaults.get("claudosseum_tenant_id"),
        autonomous_runner=_coerce_autonomous_runner(raw.get("autonomous_runner"), name),
        briefing=_coerce_briefing(raw.get("briefing")),
        brief_on_start=_parse_brief(raw.get("brief", defaults.get("brief"))),
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
            guardrails=bool(raw.get("guardrails", True)),
            protocols=bool(raw.get("protocols", True)),
        )
    return SystemDefaultsConfig()


def _resolve_system_yaml(pkg_dir: Path) -> Path | None:
    """Locate the package-owned system.yaml.

    Returns the path, or ``None`` when absent. Fails loud when a stale
    ``system_defaults.yaml`` lingers (the file was renamed to ``system.yaml``):
    silently ignoring it would drop every default hook / observability / job.
    """
    path = pkg_dir / "system.yaml"
    if path.is_file():
        return path
    stale = pkg_dir / "system_defaults.yaml"
    if stale.is_file():
        raise RuntimeError(
            f"{stale} is stale -- system defaults were renamed to system.yaml. "
            "Remove the old system_defaults.yaml."
        )
    return None


def _load_system_defaults(_cache: dict = {}) -> dict:  # noqa: B006
    """Load system.yaml from the package directory (cached)."""
    if "data" not in _cache:
        path = _resolve_system_yaml(Path(__file__).parent)
        if path is None:
            _cache["data"] = {}
        else:
            with path.open() as f:
                _cache["data"] = yaml.safe_load(f) or {}
    return _cache["data"]


def load_host_jobs() -> dict:
    """Return ``host.jobs`` from system.yaml.

    Host jobs are host-global singletons (one instance per host, fixed
    ``claudlobby-<name>`` unit identity) and deliberately bypass the fleet
    defaults merge -- a fleet does not override platform equipment.
    """
    return (_load_system_defaults().get("host") or {}).get("jobs") or {}


def _shallow_merge(base: dict | None, override: dict | None) -> dict:
    """Shallow dict merge by key -- override wins on collision."""
    return {**(base or {}), **(override or {})}


def _merge_system_into_defaults(system: dict, defaults: dict) -> dict:
    """Merge system defaults under fleet defaults. Fleet defaults win."""
    merged = {}
    all_keys = set(system) | set(defaults)

    for key in all_keys:
        sys_val = system.get(key)
        usr_val = defaults.get(key)

        if key == "hooks":
            merged[key] = _merge_hooks_dedup(sys_val or {}, usr_val or {})
        elif key == "jobs":
            # Merge by job-name; sibling jobs are preserved (not hook-style
            # dedup). Within a job, fleet fields overlay the system entry
            # (field-level spread) so a one-line toggle like
            # `weekly-worker-restart: { enroll: true }` opts in without
            # re-declaring the job's script/schedule.
            merged_jobs = dict(sys_val or {})
            for jname, jval in (usr_val or {}).items():
                base = merged_jobs.get(jname)
                if isinstance(base, dict) and isinstance(jval, dict):
                    merged_jobs[jname] = _shallow_merge(base, jval)
                else:
                    merged_jobs[jname] = jval
            merged[key] = merged_jobs
        elif key == "observability":
            merged[key] = _shallow_merge(sys_val, usr_val)
        elif usr_val is not None:
            merged[key] = usr_val
        else:
            merged[key] = sys_val

    return merged


def load_fleet(fleet_yaml: Path) -> tuple[FleetConfig, dict]:
    """Parse fleet.yaml into a FleetConfig; returns (fleet, merged_defaults)."""
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

    system_section = raw_system.get("defaults", {}) or {}
    if not system_defaults_cfg.enabled:
        effective_system: dict = {}
    else:
        effective_system = {}
        if system_defaults_cfg.hooks:
            effective_system["hooks"] = system_section.get("hooks", {})
        if system_defaults_cfg.observability:
            effective_system["observability"] = system_section.get("observability", {})
        if system_defaults_cfg.timers:
            # jobs flow through the defaults merge so fleet.yaml can override an
            # individual job by name; compose_fleet_timers reads merged["jobs"].
            effective_system["jobs"] = system_section.get("jobs", {})
    merged_defaults = _merge_system_into_defaults(effective_system, defaults)

    if system_defaults_cfg.enabled and system_defaults_cfg.guardrails:
        # Applied AFTER the system.yaml merge, and unioned rather than assigned.
        # fleet.yaml.seed ships a `defaults.guardrails` list, so every newly
        # seeded fleet supplies one — and the merge above replaces a fleet-set
        # key rather than combining it, which would drop these for exactly the
        # new fleets the default exists to protect. Opting out is
        # `system_defaults.guardrails: false`, visible in fleet.yaml; declaring
        # a guardrail list is not an opt-out and must not act as one.
        merged_defaults["guardrails"] = _merge_lists(
            DEFAULT_GUARDRAILS, merged_defaults.get("guardrails")
        )

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
        fleet_pulse=_coerce_fleet_pulse(fleet.get("fleet_pulse")),
        projects=load_projects(fleet_yaml.parent / "projects.yaml"),
        mission=_shaped(
            "fleet.mission", fleet.get("mission"), str, "mission: one paragraph"
        )
        or None,
        mission_file=_shaped(
            "fleet.mission_file", fleet.get("mission_file"), str, "missions/fleet.md"
        )
        or None,
        workstreams=_coerce_workstreams(fleet.get("workstreams")),
    )
    return fleet_cfg, merged_defaults
