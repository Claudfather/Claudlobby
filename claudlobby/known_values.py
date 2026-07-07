"""Known-good value sets and validation helpers for fleet.yaml fields.

Consolidates all known-value constants used by config.py (hard validation)
and validator.py (soft validation with suggestions). Single source of truth
for what values the compositor recognizes.
"""

from __future__ import annotations

import difflib

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
        # Full model IDs (for explicit version pinning)
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-fable-5",
    }
)

# ── Effort ───────────────────────────────────────────────────────
# Claude Code --effort accepts exactly these.
KNOWN_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "max"})

# ── Hook events ──────────────────────────────────────────────────
# Claude Code hook event names. Unknown event = silently ignored hook.
KNOWN_HOOK_EVENTS: frozenset[str] = frozenset(
    {"PreToolUse", "PostToolUse", "Notification", "Stop", "SubagentStop"}
)

# ── Permission modes ─────────────────────────────────────────────
# Valid values for BotConfig.permission_mode.
VALID_PERMISSION_MODES: frozenset[str] = frozenset(
    {"default", "acceptEdits", "bypassPermissions", "plan", "dontAsk", "auto"}
)

# ── Projects tier (projects.yaml) ────────────────────────────────
# Validation tiers — a project's closure bar, weakest to strictest.
# Semantics live in documentation/projects-yaml-schema.md. Tuple: error
# messages join them in this stable order.
VALID_TIERS: tuple[str, ...] = ("auto", "review", "preview", "human")

# The v1 project schema — config.py splits unknown keys into .raw with
# this set; validator.py uses the same set for did-you-mean suggestions.
PROJECT_KEYS: frozenset[str] = frozenset(
    {"title", "repos", "mission_file", "validation"}
)

# ── Autonomous runner ────────────────────────────────────────────
# clauDNA skills known to support --auto.
AUTO_ELIGIBLE_SKILLS: frozenset[str] = frozenset(
    {
        "/claudna:tech-debt",
        "/claudna:security-audit",
        "/claudna:product-enhance",
        "/claudna:frontend-performance-audit",
        "/claudna:docs-review",
        "/claudna:access-path-audit",
        "/claudna:product-vision",
        "/claudna:session-handoff",
        "/claudna:visual-crawl",
        "/claudna:implement-plan",
    }
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
