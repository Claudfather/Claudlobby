"""`claudlobby new-guardrail` — interactive guardrail scaffolding.

Two modes:

  Interactive:     `claudlobby new-guardrail`              (prompts for each field)
  Non-interactive: `claudlobby new-guardrail --name X ...` (all flags up front)

Creates a guardrail file at `library/guardrails/<name>.md` with proper YAML
frontmatter (title, description), H1 heading, and placeholder content.
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


def render_guardrail(name: str, title: str, description: str) -> str:
    """Render guardrail markdown content."""
    lines: list[str] = []
    lines.append("---")
    lines.append(f"title: {title}")
    lines.append(f"description: {description}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"{description}")
    lines.append("")
    lines.append("<!-- Add specific rules and examples below -->")
    lines.append("")
    return "\n".join(lines)


def interactive_collect() -> tuple[str, str, str]:
    """Walk the user through guardrail creation. Returns (name, title, description)."""
    print("\n=== claudlobby new-guardrail — interactive ===\n")

    name = _ask("Guardrail slug (lowercase, e.g. 'no-push-main')", allow_empty=False)
    while not re.match(r"^[a-z][a-z0-9_-]*$", name):
        print("  (must be lowercase, start with a letter, only [a-z0-9_-])")
        name = _ask("Guardrail slug", allow_empty=False)

    title = _ask("Title (human-readable, e.g. 'Never push to main')", allow_empty=False)
    description = _ask("Description (one line explaining the rule)", allow_empty=False)

    return name, title, description
