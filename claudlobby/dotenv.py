"""Tiny .env file parser used across the compositor.

Hand-rolled because the .env files claudlobby reads are simple shell-style
files, not the full POSIX env-var grammar. Two helpers:

  - read(path)               → parse to dict, strips `export ` prefix and quotes
  - format_file(header, vars) → render `export VAR="value"` lines for writing

Lives in its own module so __main__ (env-migrate writer) and validator
(env-presence checker) can both import without circularity.
"""
from __future__ import annotations
from pathlib import Path


def read(path: Path) -> dict[str, str]:
    """Parse a .env file into {var: value}.

    Handles both `VAR=value` and `export VAR=value` forms. Strips matched
    surrounding quotes. Returns {} if the file doesn't exist. Lines that
    start with `#` or have no `=` are skipped silently.
    """
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def merge_into(path: Path, new_vars: dict[str, str]) -> dict[str, str]:
    """Read existing .env at path, merge new_vars over it (new wins).
    Returns the merged dict. Caller writes it back."""
    return {**read(path), **new_vars}


def format_file(header: str, vars_dict: dict[str, str]) -> str:
    """Render a .env file with `export VAR="value"` lines, alpha-sorted.

    Inner double-quotes are backslash-escaped. Pair with read() for a
    round-trip-safe parse/render cycle.
    """
    lines = [header, ""]
    for k in sorted(vars_dict):
        v = vars_dict[k].replace('"', '\\"')
        lines.append(f'export {k}="{v}"')
    lines.append("")
    return "\n".join(lines)
