"""Shared interactive prompt helpers for CLI wizard commands (new-bot, new-skill, new-guardrail)."""

from __future__ import annotations


def ask(prompt: str, default: str | None = None, allow_empty: bool = True) -> str:
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


def ask_yn(prompt: str, default: bool) -> bool:
    """Yes/no prompt with default."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        v = input(f"{prompt}{suffix}: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def ask_pick(prompt: str, options: list[str], multi: bool = True) -> list[str]:
    """Show numbered options; user picks comma-separated indices or 'none'."""
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i:>2}. {opt}")
    while True:
        v = (
            input("  pick (comma-separated numbers, 'none', or 'all'): ")
            .strip()
            .lower()
        )
        if v in ("", "none"):
            return []
        if v == "all":
            return list(options)
        try:
            picks = []
            for tok in v.split(","):
                idx = int(tok.strip()) - 1
                if 0 <= idx < len(options):
                    picks.append(options[idx])
            if not multi and len(picks) > 1:
                print("  (single selection)")
                continue
            return picks
        except ValueError:
            print("  (numbers only, e.g. '1,3,5')")
