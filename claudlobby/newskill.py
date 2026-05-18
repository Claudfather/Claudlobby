"""`claudlobby new-skill` — interactive skill scaffolding.

Two modes:

  Interactive:     `claudlobby new-skill`              (prompts for each field)
  Non-interactive: `claudlobby new-skill --name X ...` (all flags up front)

Creates a skill directory under `library/skills/<name>/` containing a SKILL.md
with proper YAML frontmatter (name, description, argument-hint), H1 heading,
and placeholder content.
"""

from __future__ import annotations
import logging
import re

log = logging.getLogger(__name__)



def _ask(prompt: str, default: str | None = None, allow_empty: bool = True) -> str:
    """Prompt with optional default. Empty input returns default."""
    suffix = f" [{default}]" if default else ""
    while True:
        v = input(f"{prompt}{suffix}: ").strip()
        if v:
            return v
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("  (required)")


def render_skill(name: str, description: str, argument_hint: str | None) -> str:
    """Render SKILL.md content."""
    lines: list[str] = []
    lines.append("---")
    lines.append(f"name: {name}")
    lines.append(f'description: "{description}"')
    if argument_hint:
        lines.append(f'argument-hint: "{argument_hint}"')
    lines.append("---")
    lines.append("")
    # H1 heading — title-cased from the slug
    title = name.replace("-", " ").title()
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"{description}")
    lines.append("")
    lines.append("## Usage")
    lines.append("")
    lines.append("```")
    lines.append(f"/{name}")
    lines.append("```")
    lines.append("")
    lines.append("## Behavior")
    lines.append("")
    lines.append("<!-- Describe what this skill does when invoked -->")
    lines.append("")
    return "\n".join(lines)


def interactive_collect() -> tuple[str, str, str | None]:
    """Walk the user through skill creation. Returns (name, description, argument_hint)."""
    print("\n=== claudlobby new-skill — interactive ===\n")

    name = _ask("Skill name (lowercase, e.g. 'deploy-status')", allow_empty=False)
    while not re.match(r"^[a-z][a-z0-9_-]*$", name):
        print("  (must be lowercase, start with a letter, only [a-z0-9_-])")
        name = _ask("Skill name", allow_empty=False)

    description = _ask("Description (one line)", allow_empty=False)
    argument_hint = (
        _ask("Argument hint (e.g. '<task> [--repo <repo>]', optional)") or None
    )

    return name, description, argument_hint
