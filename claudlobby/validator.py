"""fleet.yaml validation.

Permissive by default: warnings let `generate` proceed; errors block it.
Pass `--strict` to make warnings into errors (CI-friendly).
"""
from __future__ import annotations
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import dotenv
from .config import BotConfig, FleetConfig
from .paths import Paths


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


def _bot_required_env_vars(
    bot: BotConfig, paths: Paths
) -> list[tuple[str, str, str, str | None]]:
    """Return [(canonical_var, tier, source, instance)] this bot needs.

    Walks two contract sources, mirroring composer.collect_env_contracts:

    - **MCP fragments** (`library/mcp/<name>.json`, `_env_contract` key) —
      same instance-scope rename as composer._resolve_instance_env, so
      the validator sees the canonical names that land in the rendered
      `.mcp.json` (`${TOKEN}` → `${NOTION_WORK_TOKEN}` per instance).
    - **Integration docs** (`library/integrations/<name>.md`, frontmatter
      `env_contract` key) — same auto-pair fallback as composer (if
      `bot.integrations` is empty, derive from the bot's MCP names that
      have a matching `<name>.md` integration doc).

    `instance` is None for non-instance-scoped vars."""
    from .loader import parse_frontmatter

    out: list[tuple[str, str, str, str | None]] = []
    seen_mcp: set[str] = set()
    for entry in bot.mcp:
        if entry.name in seen_mcp:
            continue
        seen_mcp.add(entry.name)
        frag_path = paths.find_library_file("mcp", entry.name, ".json")
        if frag_path is None:
            continue
        try:
            frag = json.loads(frag_path.read_text())
        except json.JSONDecodeError:
            print(f"WARN: failed to parse {frag_path}, skipping", file=sys.stderr)
            continue
        contract = frag.get("_env_contract", {})
        for var_name, meta in contract.items():
            if not isinstance(meta, dict):
                continue
            tier = meta.get("tier", "fleet")
            scope = meta.get("scope", "shared")
            if scope == "instance":
                for instance in entry.instances:
                    prefix = entry.instance_prefix(instance)
                    out.append((prefix + var_name, tier, f"mcp/{entry.name}", instance))
            else:
                out.append((var_name, tier, f"mcp/{entry.name}", None))

    # Integration doc contracts (auto-pair fallback matches composer)
    integration_names = bot.integrations or [
        e.name for e in bot.mcp
        if paths.find_library_file("integrations", e.name, ".md") is not None
    ]
    seen_int: set[str] = set()
    for int_name in integration_names:
        if int_name in seen_int:
            continue
        seen_int.add(int_name)
        int_path = paths.find_library_file("integrations", int_name, ".md")
        if int_path is None:
            continue
        try:
            fm, _ = parse_frontmatter(int_path.read_text())
        except (OSError, ValueError, KeyError):
            print(f"WARN: failed to parse frontmatter in {int_path}, skipping", file=sys.stderr)
            continue
        contract = fm.get("env_contract", {}) if isinstance(fm, dict) else {}
        if not isinstance(contract, dict):
            continue
        for var_name, meta in contract.items():
            if not isinstance(meta, dict):
                continue
            tier = meta.get("tier", "fleet")
            out.append((var_name, tier, f"integration/{int_name}", None))

    return out


def validate(fleet: FleetConfig, paths: Paths) -> ValidationReport:
    report = ValidationReport()

    # Hard error: no bots
    if not fleet.bots:
        report.errors.append("fleet.bots is empty — nothing to compose")

    # Read fleet-tier .env once. Bot-tier .env is read per-bot inside the loop
    # because each bot has its own. The 3-tier composition mirrors what
    # lib/start-bot.sh does at runtime: os.environ → fleet → bot, later wins.
    fleet_env = dotenv.read(paths.env_file)

    # Per-bot checks (overlay-aware lookups — overlay first, base fallback)
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
                report.errors.append(
                    f"bot '{bot_name}': expertise '{area}' not found in overlay or base library"
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
                found = False
                for d in paths.library_search_dirs("skills"):
                    target = d / dir_name if dir_name else d
                    if target.is_dir():
                        # Empty folder is still a warning — flag it.
                        has_skill = any(
                            (sub / "SKILL.md").is_file()
                            for sub in target.rglob("*")
                            if sub.is_dir()
                        )
                        if has_skill:
                            found = True
                            break
                if not found:
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
                report.warnings.append(
                    f"bot '{bot_name}': mcp fragment '{mcp.name}.json' not found — server will not be configured"
                )

        # MCP env-contract check (warn) — uses the canonical instance-renamed
        # var names (the same names composer puts into the rendered
        # `.mcp.json`), and looks across the full 3-tier env (host →
        # fleet/.env → bot/.env). Replaces a fragile placeholder-scan that
        # didn't know about instance scoping or bot-tier .env files.
        for var, tier, source, instance in _bot_required_env_vars(bot, paths):
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
                found = False
                for d in paths.library_search_dirs("integrations"):
                    target = d / dir_name if dir_name else d
                    if target.is_dir() and any(
                        p.is_file() and not p.stem.lower().startswith("readme")
                        for p in target.rglob("*.md")
                    ):
                        found = True
                        break
                if not found:
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
                    found = False
                    for d in paths.library_search_dirs(kind):
                        target = d / dir_name if dir_name else d
                        if target.is_dir() and any(
                            p.is_file() and not p.stem.lower().startswith("readme")
                            for p in target.rglob("*.md")
                        ):
                            found = True
                            break
                    if not found:
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

        # Account (warn)
        if bot.account not in fleet.accounts:
            report.warnings.append(
                f"bot '{bot_name}': account '{bot.account}' not in fleet.accounts — falling back to 'default'"
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

    # clauDNA dependency check (warn)
    clauDNA_breadcrumb = Path.home() / ".claude" / ".clauDNA-repo"
    if not clauDNA_breadcrumb.is_file():
        report.warnings.append(
            "clauDNA not installed — bots will lack global skills "
            "(/simplify, /review-pr, /tech-debt, etc.). "
            "Clone and run install.sh from your clauDNA repo."
        )
    else:
        repo_path = Path(clauDNA_breadcrumb.read_text().strip())
        if not repo_path.is_dir():
            report.warnings.append(
                f"clauDNA breadcrumb points to {repo_path} but directory not found — "
                "re-run /clauDNA-setup or update ~/.claude/.clauDNA-repo"
            )

    return report
