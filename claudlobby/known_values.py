"""Known-good value sets and validation helpers for fleet.yaml fields.

Consolidates all known-value constants used by config.py (hard validation)
and validator.py (soft validation with suggestions). Single source of truth
for what values the compositor recognizes.
"""

from __future__ import annotations

import difflib
import re

# Shell-identifier pattern — the SSOT for anything that becomes a shell variable
# name (bot.env keys, model-strategy tiers, briefing slot -> BRIEFING_SECTIONS_<SLOT>).
# Shared by config.py and composer.py so the rule can't drift between them.
SHELL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# ── Models ───────────────────────────────────────────────────────
# Short aliases that Claude Code accepts, plus full model IDs for
# explicit version pinning.
KNOWN_MODELS: frozenset[str] = frozenset(
    {
        # Short aliases (what fleet.yaml typically uses)
        "opus",
        "sonnet",
        "haiku",
        "fable",
        # Full model IDs (for explicit version pinning) — refresh to the
        # current GA roster when the lineup moves (canonical IDs live in the
        # claude-api skill / Anthropic model docs).
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-fable-5",
    }
)

# ── Effort ───────────────────────────────────────────────────────
# Claude Code --effort accepts exactly these.
KNOWN_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "max"})

# ── Hook events ──────────────────────────────────────────────────
# The full authoritative Claude Code hook-event set. Unknown event = silently
# ignored hook, so this must stay COMPLETE — a missing name false-warns a valid
# hook (the clauDNA/Claudron vault integration rides SessionStart/SessionEnd/
# PreCompact). Refresh from the canonical reference when it grows:
# https://code.claude.com/docs/en/hooks.md
KNOWN_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "Setup",
        "UserPromptSubmit",
        "UserPromptExpansion",
        "PreToolUse",
        "PermissionRequest",
        "PermissionDenied",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "Notification",
        "MessageDisplay",
        "SubagentStart",
        "SubagentStop",
        "TaskCreated",
        "TaskCompleted",
        "Stop",
        "StopFailure",
        "TeammateIdle",
        "InstructionsLoaded",
        "ConfigChange",
        "CwdChanged",
        "FileChanged",
        "WorktreeCreate",
        "WorktreeRemove",
        "PreCompact",
        "PostCompact",
        "Elicitation",
        "ElicitationResult",
        "SessionEnd",
    }
)

# ── Tool types ───────────────────────────────────────────────────
# library/tools/<name>/tool.yaml `type:` values the compositor can render.
# `script`: render the tool's Jinja template into bot_dir/tools/ and chmod +x.
KNOWN_TOOL_TYPES: frozenset[str] = frozenset({"script"})

# ── Permission modes ─────────────────────────────────────────────
# Valid values for BotConfig.permission_mode.
VALID_PERMISSION_MODES: frozenset[str] = frozenset(
    {"default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "auto"}
)

# ── Headless env vars (bot.conf) ─────────────────────────────────
# Two sets, split by effect on Claude Code's --remote-control: HEADLESS_TRIM
# vars are safe to emit, RC_KILLING vars must never be. Empirically verified
# on Claude Code 2.1.198 (2026-07-09, Pi fleet): each HEADLESS_TRIM var alone
# AND all together leave remote-control active, while either RC_KILLING var
# suppresses it — RC is feature-flag-gated and flag evaluation rides the
# telemetry channel, so disabling telemetry silently drops every channel reply
# while inbound still arrives (the July 2026 fleet-wide Telegram outage, #533).
#
# Emitted by the composer when disable_nonessential_traffic is true. Both
# survey spellings are set — the documented name drifts across binary versions.
HEADLESS_TRIM_VARS: tuple[str, ...] = (
    "DISABLE_AUTOUPDATER",
    "DISABLE_ERROR_REPORTING",
    "DISABLE_BUG_COMMAND",
    "DISABLE_FEEDBACK_SURVEY",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
)

# Never composed; the validator errors when a bot that needs remote-control
# carries one in its env (#533 guard).
RC_KILLING_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_TELEMETRY",
)

# ── Projects tier (projects.yaml) ────────────────────────────────
# Validation tiers — alternatives, not levels; each is a complete condition.
# Semantics live in documentation/projects-yaml-schema.md. Tuple: error
# messages join them in this stable order, which is not a ranking.
VALID_TIERS: tuple[str, ...] = ("auto", "review", "preview", "human")

# ── .env tiers (#1214 / #1226) ───────────────────────────────────
# The four tiers start-bot.sh sources, in CASCADE ORDER, least specific first.
# Later wins, so the most specific tier that ASSIGNS a key decides its value —
# including assigning it empty, which blanks an upstream secret rather than
# falling through to it.
#
# This tuple is a NAME registry for validating a declaration. It is not the
# resolver and must never become one: the order and the paths live in
# `env_tier_rows` (lib-common.sh), which the runtime itself sources.
# `test_env_cascade.py::test_the_tier_registry_matches_the_resolver` pins these
# names against what that function actually emits, so the two cannot fork.
ENV_TIERS: tuple[str, ...] = ("host", "root", "fleet", "bot")

# The repo-root tier is real — start-bot.sh sources it — but it is shared by
# every fleet on the host and is being wound down in favour of the fleet tier.
# Declarable so the register can report a value found there; warned on so a new
# contract does not scaffold into it.
DEPRECATED_ENV_TIERS: frozenset[str] = frozenset({"root"})

# ── Credential sources (#1214 Phase 1) ───────────────────────────
# The CLOSED, framework-owned registry of values an ``_env_contract`` entry's
# ``source`` may take. A contract declares WHO needs a var and at WHICH tier;
# ``source`` is the third fact — where the value comes from — so the system can
# supply it at boot instead of a human pasting it into a ``.env``.
#
# Entries are WHOLE identifiers, not a kind plus a free parameter, and that is a
# security property rather than a style choice (fork F5). The resolver dispatches
# on the entire string via a fixed ``case`` arm per entry, so a contract value
# selects a framework-authored command and can never contribute text to one.
# Admitting ``cli:<anything>`` would put contract text in command position and
# turn every fragment — including a fleet-overlay fragment — into an injection
# surface at boot. Widening the registry therefore means adding an arm here and
# in the resolver, deliberately, and never accepting a caller-supplied command.
#
# ``source`` is OPTIONAL. Its absence means "a human supplies this value", which
# is how 47 of the 48 declared vars behave today. ``literal`` is the EXPLICIT spelling
# of that same opt-out, for a fleet or bot that wants to state on the line that
# this scope declines automatic resolution (fork F6's per-tier opt-out).
KNOWN_CREDENTIAL_SOURCES: frozenset[str] = frozenset(
    {
        # No resolution — a human supplies the value. Same effect as omitting
        # `source`; spelled out when a tier wants to decline resolution visibly.
        "literal",
        # `gh auth token` — the host default GitHub identity.
        "cli:gh-token",
        # RESERVED, and deliberately unresolvable: no resolver arm reads this.
        # Fleet-scope GitHub App minting SHIPPED as the use-time helper
        # (lib/git-credential-github-app + lib/mint-github-token.sh, App-auth
        # P1 #1271) rather than as this boot-time resolver — a resolver would
        # put ~1h tokens at rest in the launch env, the exact hazard #1214 F5
        # names. The arm stays reserved for #252's per-bot sidecar, where a
        # per-bot vending process changes the shape. Registered so the schema
        # is proven against the harder class and adding that arm later is one
        # change rather than a migration of every contract entry.
        "mint:github-app",
    }
)

# The v1 project schema — config.py splits unknown keys into .raw with
# this set; validator.py uses the same set for did-you-mean suggestions.
PROJECT_KEYS: frozenset[str] = frozenset(
    {"title", "repos", "mission_file", "validation"}
)

# ── clauDNA skill renames ────────────────────────────────────────
# Canonical dead -> live map for clauDNA's verb-mode consolidation, which
# collapsed standalone skills into engine verbs (/claudna:security-audit ->
# /claudna:audit security) with no aliases. Single source of truth: the
# --auto-eligible subset below and both dead-name CI guards
# (tests/test_no_dead_claudna_refs.py, tests/test_no_dead_session_command.py)
# derive from this map, so one clauDNA consolidation updates one list.
#
# Every key is a name that actually shipped and was renamed away, verified
# against the installed plugin's "Replaces" lists. Hyphen-typos of live verbs
# that never existed as skills (e.g. /claudna:session-checkpoint — `checkpoint`
# is a net-new verb, not a rename) are pattern policy the guards cover with
# family globs, never entries here. NB the live token is not the dead name
# minus prefix (security-audit -> `security`, docs-review -> `docs`,
# frontend-performance-audit -> `frontend-perf`).
CLAUDNA_SKILL_RENAMES: dict[str, str] = {
    # audit engine lenses
    "/claudna:security-audit": "/claudna:audit security",
    "/claudna:tech-debt": "/claudna:audit tech-debt",
    "/claudna:docs-review": "/claudna:audit docs",
    "/claudna:design-review": "/claudna:audit design",
    "/claudna:data-model-audit": "/claudna:audit data-model",
    "/claudna:access-path-audit": "/claudna:audit access-path",
    "/claudna:frontend-performance-audit": "/claudna:audit frontend-perf",
    "/claudna:repo-health": "/claudna:audit repo-health",
    # session engine verbs
    "/claudna:session-handoff": "/claudna:session handoff",
    "/claudna:session-resume": "/claudna:session resume",
    "/claudna:name-session": "/claudna:session name",
    # whole-skill successors
    "/claudna:review-pr": "/claudna:review-work",
    "/claudna:review-changes": "/claudna:review-work",
    "/claudna:skill-health": "/claudna:using-claudna",
    "/claudna:product-brainstorm": "/claudna:product-vision",
}

# Live clauDNA skills that library/ content is permitted to reference by name
# (#1253). Maintained beside the rename map above, because a clauDNA
# consolidation is the one event that changes both: a name leaves this set at
# the same moment it gains a CLAUDNA_SKILL_RENAMES entry.
#
# Deliberately a DECLARATION, not a scan of the installed plugin. Reading the
# plugin tree to learn what it ships is consuming a sibling by assertion —
# correct today, silently wrong the next time clauDNA moves, and unavailable in
# CI where no plugin is installed. Every entry below is backed by a real
# reference in library/; adding a new reference means adding a line here once.
#
# NOT validated against a local clauDNA checkout, and that is deliberate. The
# obvious-looking improvement -- reuse test_rename_map_gate's resolver and
# assert each name exists as a skills/<name>/ dir -- was measured and rejected:
# both checkouts on this host lack skills/audit/ and skills/capture/, which are
# unambiguously live, because one is 130 commits behind and the other's origin
# is stale. Eight of the fourteen below would have failed against a stale
# oracle. That gate resolves only narrow paths and SKIPS otherwise for exactly
# this reason; its CI leg clones fresh, which is the only trustworthy oracle.
CLAUDNA_LIVE_SKILLS: frozenset[str] = frozenset(
    {
        "/claudna:audit",
        "/claudna:capture",
        "/claudna:claudron",
        "/claudna:development-retro",
        "/claudna:implement-plan",
        "/claudna:index",
        "/claudna:modal",
        "/claudna:neon",
        "/claudna:product-enhance",
        "/claudna:product-vision",
        "/claudna:publish",
        "/claudna:railway",
        "/claudna:recall",
        "/claudna:vercel",
    }
)

# ── Autonomous runner ────────────────────────────────────────────
# The --auto-eligible subset of the renamed skills; the live values (the whole
# space-form) are what validator.py exact-matches. Selected from the canonical
# map by key so the two can never disagree — a dropped or misspelled key here
# fails loudly at import.
_AUTO_ELIGIBLE_RENAMES: dict[str, str] = {
    k: CLAUDNA_SKILL_RENAMES[k]
    for k in (
        "/claudna:security-audit",
        "/claudna:tech-debt",
        "/claudna:docs-review",
        "/claudna:access-path-audit",
        "/claudna:frontend-performance-audit",
        "/claudna:session-handoff",
    )
}

# --auto-capable clauDNA skills that were NOT consolidated (still invoked by
# their own name). Kept separate from the rename map so their live names never
# read as a dead-form value.
_AUTO_ELIGIBLE_STANDALONE: frozenset[str] = frozenset(
    {
        "/claudna:product-enhance",
        "/claudna:product-vision",
        "/claudna:visual-crawl",
        "/claudna:implement-plan",
    }
)

# clauDNA skills known to support --auto (validator.py checks
# autonomous_runner.skill against this). Derived from the rename map's live
# values + the un-consolidated standalones, so the consolidated space-forms
# validate and the dead hyphen-names cannot.
AUTO_ELIGIBLE_SKILLS: frozenset[str] = (
    frozenset(_AUTO_ELIGIBLE_RENAMES.values()) | _AUTO_ELIGIBLE_STANDALONE
)

OUTCOME_KEYS: frozenset[str] = frozenset(
    {"completed", "bypassed", "needs_input", "blocked", "partial"}
)
OUTCOME_ACTIONS: frozenset[str] = frozenset({"report", "report_and_pause", "silent"})
BYPASS_ACTIONS: frozenset[str] = frozenset(
    {"comment_and_label", "comment_only", "exit_silent"}
)

# ── Expertise → core tools ──────────────────────────────────────
# Which tools each expertise area typically requires.
EXPERTISE_CORE_TOOLS: dict[str, set[str]] = {
    "software-engineering": {"Write", "Edit"},
    "frontend-design": {"Write", "Edit"},
    "data-engineering": {"Write", "Edit"},
    "pipeline-engineering": {"Write", "Edit"},
    "orchestration": {"Agent", "Bash"},
}


# ── Helpers ──────────────────────────────────────────────────────


def closest_match(
    value: str,
    known: set[str] | frozenset[str],
    n: int = 1,
    cutoff: float = 0.6,
) -> str | None:
    """Return the closest known value, or None if nothing is close enough.

    Case-insensitive comparison: lowercases both the input and the known
    set for matching, then returns the original-cased known value.
    """
    lower_to_original = {k.lower(): k for k in known}
    matches = difflib.get_close_matches(
        value.lower(), sorted(lower_to_original.keys()), n=n, cutoff=cutoff
    )
    return lower_to_original[matches[0]] if matches else None


def hint(value: str, known: set[str] | frozenset[str] | tuple[str, ...]) -> str:
    """The " — did you mean 'x'?" suffix, or "" when nothing is close.

    One home for the suggestion voice (the closest_match + f-string pair
    is copy-pasted ~10x across validator.py/config.py; new call sites use
    this, old ones migrate opportunistically).
    """
    suggestion = closest_match(value, set(known))
    return f" — did you mean '{suggestion}'?" if suggestion else ""
