"""fleet.yaml validation.

Permissive by default: warnings let `generate` proceed; errors block it.
Pass `--strict` to make warnings into errors (CI-friendly).
"""
from __future__ import annotations
from dataclasses import dataclass, field
import os
import re
from pathlib import Path

from .config import FleetConfig
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


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _scan_env_placeholders(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return set(_ENV_PLACEHOLDER.findall(path.read_text()))


def validate(fleet: FleetConfig, paths: Paths) -> ValidationReport:
    report = ValidationReport()

    # Hard error: no bots
    if not fleet.bots:
        report.errors.append("fleet.bots is empty — nothing to compose")

    # Per-bot checks (overlay-aware lookups — overlay first, base fallback)
    for bot_name, bot in fleet.bots.items():
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

        # MCP (warn) — also check for unset env placeholders. MCP fragments
        # are JSON; overlay wins over base (same lookup model as other kinds).
        # bot.mcp is list[McpEntry]; the file on disk is named after .name
        # regardless of how many instances the entry composes into .mcp.json.
        for mcp in bot.mcp:
            mcp_path = paths.find_library_file("mcp", mcp.name, ".json")
            if mcp_path is None:
                report.warnings.append(
                    f"bot '{bot_name}': mcp fragment '{mcp.name}.json' not found — server will not be configured"
                )
                continue
            for var in _scan_env_placeholders(mcp_path):
                if var not in os.environ:
                    report.warnings.append(
                        f"bot '{bot_name}': mcp '{mcp.name}' references ${{{var}}} but {var} is not set — MCP server will fail at runtime"
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

        # Telegram token env (warn)
        if bot.telegram.token_env and bot.telegram.token_env not in os.environ:
            report.warnings.append(
                f"bot '{bot_name}': telegram.token_env '{bot.telegram.token_env}' not set in environment — bot won't connect to Telegram until you add it to .env"
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
