"""fleet.yaml validation.

Permissive by default: warnings let `generate` proceed; errors block it.
Pass `--strict` to make warnings into errors (CI-friendly).
"""

from __future__ import annotations
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

from . import dotenv
from .config import FleetConfig
from .known_values import (
    AUTO_ELIGIBLE_SKILLS,
    BYPASS_ACTIONS,
    EXPERTISE_CORE_TOOLS,
    KNOWN_HOOK_EVENTS,
    KNOWN_MODELS,
    OUTCOME_ACTIONS,
    OUTCOME_KEYS,
    closest_match,
)
from .mcp_resolve import required_vars as _mcp_required_vars
from .paths import Paths

_CADENCE_RE = re.compile(r"^\d+[mhd]$")


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_issues(self) -> bool:
        return bool(self.errors or self.warnings)

    def merged_for_strict(self) -> list[str]:
        return self.errors + self.warnings


def _available_names(paths: Paths, kind: str, ext: str = ".md") -> set[str]:
    """Scan library search dirs for available file stems of a given kind."""
    names: set[str] = set()
    for d in paths.library_search_dirs(kind):
        if d.is_dir():
            names |= {p.stem for p in d.glob(f"*{ext}")}
    return names


def _validate_bots(
    fleet: FleetConfig,
    paths: Paths,
    fleet_env: dict[str, str],
    report: ValidationReport,
) -> None:
    """Per-bot validation: expertise, voice, skills, MCP, env vars, integrations, etc."""
    # Pre-compute available names for suggestion hints (avoids per-bot re-scan)
    avail_expertise = _available_names(paths, "expertise")
    avail_mcp = _available_names(paths, "mcp", ext=".json")

    for bot_name, bot in fleet.bots.items():
        bot_env = dotenv.read(paths.bot_runtime(bot_name) / ".env")
        effective_env: dict[str, str] = {**os.environ, **fleet_env, **bot_env}
        # Expertise — at least one must exist (HARD)
        if not bot.expertise:
            report.errors.append(
                f"bot '{bot_name}': expertise list is empty — need at least one entry from library/expertise/"
            )
        for area in bot.expertise:
            if paths.find_library_file("expertise", area, ".md") is None:
                suggestion = closest_match(area, avail_expertise)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.errors.append(
                    f"bot '{bot_name}': expertise '{area}' not found in overlay or base library{hint}"
                )

        # Voice (warn)
        if bot.voice:
            if paths.find_voice_file(bot.voice) is None:
                report.warnings.append(
                    f"bot '{bot_name}': voice file '{bot.voice}' not found — bare expertise will be used"
                )

        # Skills (warn). Accepts:
        #   name        — skills/name/
        #   dir/name    — skills/dir/name/
        #   dir/        — folder expansion (skills/dir/**)
        for skill in bot.skills:
            if skill.endswith("/"):
                dir_name = skill.rstrip("/")
                if not paths.expand_skill_folder(dir_name):
                    report.warnings.append(
                        f"bot '{bot_name}': skill folder '{skill}' empty or missing in any library/skills/ — no skills will be linked"
                    )
            elif paths.find_skill_dir(skill) is None:
                report.warnings.append(
                    f"bot '{bot_name}': skill '{skill}' not in any library/skills/ — symlink will be skipped"
                )

        # MCP fragment existence (warn). bot.mcp is list[McpEntry]; the file
        # on disk is named after .name regardless of how many instances the
        # entry composes into .mcp.json.
        for mcp in bot.mcp:
            if paths.find_library_file("mcp", mcp.name, ".json") is None:
                suggestion = closest_match(mcp.name, avail_mcp)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.warnings.append(
                    f"bot '{bot_name}': mcp fragment '{mcp.name}.json' not found — server will not be configured{hint}"
                )

        # MCP env-contract check (warn) — uses the canonical instance-renamed
        # var names (the same names composer puts into the rendered
        # `.mcp.json`), and looks across the full 3-tier env (host →
        # fleet/.env → bot/.env). Replaces a fragile placeholder-scan that
        # didn't know about instance scoping or bot-tier .env files.
        for var, tier, source, instance in _mcp_required_vars(bot, paths):
            if var in effective_env:
                continue
            inst_note = f" (instance: {instance})" if instance else ""
            report.warnings.append(
                f"bot '{bot_name}': {source}{inst_note} requires {var} but it's not set — "
                f"add to {tier}-tier .env (MCP server will fail at runtime)"
            )

        # Integrations (warn). Accepts `name`, `dir/name`, or `dir/`.
        for integ in bot.integrations:
            if integ.endswith("/"):
                dir_name = integ.rstrip("/")
                if not paths.expand_library_folder("integrations", dir_name):
                    report.warnings.append(
                        f"bot '{bot_name}': integration folder '{integ}' empty or missing in any library/integrations/ — skipped"
                    )
            elif paths.find_library_file("integrations", integ, ".md") is None:
                report.warnings.append(
                    f"bot '{bot_name}': integration '{integ}' not in any library/integrations/ — skipped"
                )

        # Guardrails / protocols / resources / lessons / post_actions (warn).
        # Each entry can be `name`, `dir/name`, or `dir/` (folder expansion).
        for ref, kind in [
            (bot.guardrails, "guardrails"),
            (bot.protocols, "protocols"),
            (bot.resources, "resources"),
            (bot.lessons, "lessons"),
            (bot.post_actions, "post_actions"),
        ]:
            for item in ref:
                if item.endswith("/"):
                    dir_name = item.rstrip("/")
                    if not paths.expand_library_folder(kind, dir_name):
                        report.warnings.append(
                            f"bot '{bot_name}': {kind[:-1]} folder '{item}' empty or missing in any library/{kind}/ — no items will be loaded"
                        )
                elif paths.find_library_file(kind, item, ".md") is None:
                    report.warnings.append(
                        f"bot '{bot_name}': {kind[:-1]} '{item}' not in any library/{kind}/ — section will be skipped"
                    )

        # Telegram token env (warn). Check effective_env so bot-tier .env
        # values count (the common case — per-bot Telegram tokens live in
        # runtime/bots/<bot>/.env so multi-bot fleets don't cross-wire).
        if bot.telegram.token_env and bot.telegram.token_env not in effective_env:
            report.warnings.append(
                f"bot '{bot_name}': telegram.token_env '{bot.telegram.token_env}' not set in any tier of .env — bot won't connect to Telegram"
            )

        # Observability config (warn). Fields may be None (= use hardcoded default);
        # only validate when explicitly set.
        obs = bot.observability
        if obs.pulse_interval is not None:
            if obs.pulse_interval <= 0:
                report.warnings.append(
                    f"bot '{bot_name}': observability.pulse_interval must be > 0 (got {obs.pulse_interval})"
                )
            elif obs.pulse_interval > 3600:
                report.warnings.append(
                    f"bot '{bot_name}': observability.pulse_interval > 3600s (1h) is unusually long — got {obs.pulse_interval}"
                )
        if obs.reap_days is not None:
            if obs.reap_days <= 0:
                report.warnings.append(
                    f"bot '{bot_name}': observability.reap_days must be > 0 (got {obs.reap_days})"
                )
            elif obs.reap_days > 365:
                report.warnings.append(
                    f"bot '{bot_name}': observability.reap_days > 365 is unusually long — got {obs.reap_days}"
                )

        # Model validation (warn + pass-through)
        if bot.model and bot.model not in KNOWN_MODELS:
            suggestion = closest_match(bot.model, KNOWN_MODELS)
            hint = f" — did you mean '{suggestion}'?" if suggestion else ""
            report.warnings.append(
                f"bot '{bot_name}': model '{bot.model}' not in known models{hint}. "
                f"Known: {', '.join(sorted(KNOWN_MODELS))}. "
                f"Passing through as-is (may be a new model)."
            )

        # model_strategy.base and escalate_to (warn + suggest)
        if bot.model_strategy:
            for field_name, val in [
                ("base", bot.model_strategy.base),
                ("escalate_to", bot.model_strategy.escalate_to),
            ]:
                if val and val not in KNOWN_MODELS:
                    suggestion = closest_match(val, KNOWN_MODELS)
                    hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                    report.warnings.append(
                        f"bot '{bot_name}': model_strategy.{field_name} '{val}' not in known models{hint}"
                    )

        # Hook event keys (warn)
        for event in bot.hooks:
            if event not in KNOWN_HOOK_EVENTS:
                suggestion = closest_match(event, KNOWN_HOOK_EVENTS)
                hint = f" — did you mean '{suggestion}'?" if suggestion else ""
                report.warnings.append(
                    f"bot '{bot_name}': hook event '{event}' not recognized{hint}. "
                    f"Known events: {', '.join(sorted(KNOWN_HOOK_EVENTS))}. "
                    f"This hook will be silently ignored by Claude Code."
                )

        # Hook command existence (warn)
        for event, entries in bot.hooks.items():
            for entry in entries:
                cmd = entry.get("command")
                if not cmd or entry.get("type") == "prompt":
                    continue
                cmd_path = Path(cmd)
                if cmd_path.is_absolute() and not cmd_path.exists():
                    report.warnings.append(
                        f"bot '{bot_name}': hook {event} command '{cmd}' not found on disk"
                    )

        # Ecosystem: Claudron MCP ↔ vault path cross-check (warn)
        has_claudron_mcp = any(entry.name == "claudron" for entry in bot.mcp)
        if bot.claudron_vault_path and not has_claudron_mcp:
            report.warnings.append(
                f"bot '{bot_name}': claudron_vault_path is set but no 'claudron' MCP server is configured — "
                "the vault path won't be used without the Claudron MCP server"
            )
        if has_claudron_mcp and not bot.claudron_vault_path:
            report.warnings.append(
                f"bot '{bot_name}': claudron MCP configured but claudron_vault_path not set — "
                "queries will have no vault scope"
            )

        # Account (warn)
        if bot.account not in fleet.accounts:
            report.warnings.append(
                f"bot '{bot_name}': account '{bot.account}' not in fleet.accounts — falling back to 'default'"
            )

        # Tool deny vs expertise conflict (warn). Flag when a denied tool is
        # core to the bot's expertise — the bot won't be able to do its job.
        if bot.tools.deny:
            denied = set(bot.tools.deny)
            for area in bot.expertise:
                core = EXPERTISE_CORE_TOOLS.get(area, set())
                conflict = denied & core
                if conflict:
                    report.warnings.append(
                        f"bot '{bot_name}': tools.deny includes {sorted(conflict)} "
                        f"but expertise '{area}' typically requires them"
                    )
            # Also warn if same tool appears in both allow and deny
            if bot.tools.allow:
                overlap = denied & set(bot.tools.allow)
                if overlap:
                    report.warnings.append(
                        f"bot '{bot_name}': tools {sorted(overlap)} appear in both allow and deny lists"
                    )

        # Autonomous-runner block (Phase 4). Soft validation: mostly warnings
        # since the wrapper at runtime is more authoritative than this static
        # checker. One hard error: github_issues picker without a label.
        ar = bot.autonomous_runner
        if ar is not None:
            if ar.skill not in AUTO_ELIGIBLE_SKILLS:
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.skill '{ar.skill}' is not on the "
                    f"--auto-eligible list — the wrapper will still invoke it, but unknown "
                    f"clauDNA skills may not emit a structured result"
                )

            if not _CADENCE_RE.match(ar.cadence):
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.cadence '{ar.cadence}' doesn't match "
                    f"<N><m|h|d> — the bot may not fire on the expected interval"
                )

            if "/" not in ar.target_repo or ar.target_repo.count("/") != 1:
                report.warnings.append(
                    f"bot '{bot_name}': autonomous_runner.target_repo '{ar.target_repo}' "
                    f"should be 'org/repo' format"
                )

            if ar.picker is not None:
                if ar.picker.type == "github_issues" and not ar.picker.label:
                    report.errors.append(
                        f"bot '{bot_name}': autonomous_runner.picker.label is required when "
                        f"type='github_issues'"
                    )

            if ar.bypass is not None:
                if ar.bypass.on_bypass not in BYPASS_ACTIONS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.bypass.on_bypass "
                        f"'{ar.bypass.on_bypass}' not in known set "
                        f"({sorted(BYPASS_ACTIONS)})"
                    )

            for k, v in ar.on_outcome.items():
                if k not in OUTCOME_KEYS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.on_outcome key '{k}' is not a "
                        f"known outcome (expected one of {sorted(OUTCOME_KEYS)})"
                    )
                if v not in OUTCOME_ACTIONS:
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner.on_outcome action '{v}' is not "
                        f"a known action (expected one of {sorted(OUTCOME_ACTIONS)})"
                    )

            for hook in ar.pre_hooks + ar.post_hooks:
                if not hook.startswith("/claudna:"):
                    report.warnings.append(
                        f"bot '{bot_name}': autonomous_runner hook '{hook}' is not a "
                        f"/claudna: skill — hooks should be clauDNA skill names"
                    )


def _validate_teams(fleet: FleetConfig, report: ValidationReport) -> None:
    """Org structure integrity and team membership checks."""
    # Org structure integrity (warn — bot_ids may reference other fleets)
    for bot_name, bot in fleet.bots.items():
        if bot.reports_to and bot.reports_to not in fleet.bots:
            report.warnings.append(
                f"bot '{bot_name}': reports_to '{bot.reports_to}' not found in fleet.bots"
            )
        if bot.manages:
            for managed_id in bot.manages:
                if managed_id not in fleet.bots:
                    report.warnings.append(
                        f"bot '{bot_name}': manages '{managed_id}' not found in fleet.bots"
                    )

    # Team integrity (warn)
    for team in fleet.teams.values():
        if team.manager not in fleet.bots:
            report.warnings.append(
                f"team '{team.name}': manager '{team.manager}' is not in fleet.bots"
            )
        for worker in team.workers:
            if worker not in fleet.bots:
                report.warnings.append(
                    f"team '{team.name}': worker '{worker}' is not in fleet.bots"
                )


def _validate_fleet(fleet: FleetConfig, report: ValidationReport) -> None:
    """Fleet-level dependency checks."""
    # Warn about disabling defaults — unusual, worth flagging
    if not fleet.plugins.include_defaults:
        report.warnings.append(
            "plugins.include_defaults is false — default plugins (claudna) will not be installed"
        )

    # Plugin name format: must be name@marketplace
    for plugin in fleet.plugins.required:
        if not re.match(r"^[\w-]+@[\w-]+$", plugin):
            report.warnings.append(
                f"plugin '{plugin}' does not match expected name@marketplace format"
            )

    # Marketplace source format: github repos must be org/repo
    for mp_name, mp_config in fleet.plugins.marketplaces.items():
        src = mp_config.get("source", {})
        if isinstance(src, dict):
            src_type = src.get("source", "")
            src_repo = src.get("repo", "")
            if src_type == "github" and not re.match(r"^[\w.-]+/[\w.-]+$", src_repo):
                report.warnings.append(
                    f"marketplace '{mp_name}': repo '{src_repo}' does not match "
                    "expected <org>/<repo> format"
                )

    if not fleet.plugins.required:
        return

    # Check installed_plugins.json for declared plugins
    config_dir = Path.home() / ".claude"
    installed_path = config_dir / "plugins" / "installed_plugins.json"

    if not installed_path.is_file():
        report.warnings.append(
            f"plugins declared in fleet.yaml but {installed_path} not found — "
            "run 'claude plugin install <plugin>' for each declared plugin, or "
            "set CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1 to auto-install on first bot start"
        )
        return

    try:
        installed = json.loads(installed_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        report.warnings.append(
            f"could not read {installed_path}: {exc} — plugin state unknown"
        )
        return

    installed_plugins = installed.get("plugins", {})
    for plugin in fleet.plugins.required:
        if plugin not in installed_plugins:
            report.warnings.append(
                f"plugin '{plugin}' declared in fleet.yaml but not installed — "
                f"run 'claude plugin install {plugin}'"
            )


def _validate_cross_fleet_collisions(
    fleet: FleetConfig, paths: Paths, report: ValidationReport
) -> None:
    """Warn when bot names collide with bots in other fleets on the same host.

    tmux session names are derived from the bot directory basename, so two
    fleets with a bot named 'alex' would fight over the same tmux session.
    """
    local_dir = paths.root / "local"
    if not local_dir.is_dir():
        return

    current_fleet = paths.fleet_dir.name if paths.fleet_dir else None
    bot_names = set(fleet.bots)

    for fleet_dir in sorted(local_dir.iterdir()):
        if not fleet_dir.is_dir() or fleet_dir.name == current_fleet:
            continue
        other_bots_dir = fleet_dir / "runtime" / "bots"
        if not other_bots_dir.is_dir():
            continue
        for bot_dir in sorted(other_bots_dir.iterdir()):
            if not bot_dir.is_dir() or not (bot_dir / "bot.conf").is_file():
                continue
            if bot_dir.name in bot_names:
                report.warnings.append(
                    f"bot '{bot_dir.name}' also exists in fleet '{fleet_dir.name}' "
                    f"— tmux session names will collide on this host"
                )


def validate(fleet: FleetConfig, paths: Paths) -> ValidationReport:
    report = ValidationReport()

    if not fleet.bots:
        report.errors.append("fleet.bots is empty — nothing to compose")

    fleet_env = dotenv.read(paths.env_file)
    _validate_bots(fleet, paths, fleet_env, report)
    _validate_teams(fleet, report)
    _validate_fleet(fleet, report)
    _validate_cross_fleet_collisions(fleet, paths, report)

    # bench marker — multi-bot fleets should designate a bench bot
    if len(fleet.bots) > 1 and not any(b.bench for b in fleet.bots.values()):
        report.warnings.append(
            "fleet has multiple bots but none has bench: true — "
            "cold-start benchmarking will not know which bot to measure"
        )

    return report
