"""Tiny .env file parser used across the compositor.

Hand-rolled because the .env files claudlobby reads are simple shell-style
files, not the full POSIX env-var grammar. Two helpers:

  - read(path)               → parse to dict, strips `export ` prefix and quotes
  - format_file(header, vars) → render `export VAR=<quoted>` lines for writing

Lives in its own module so __main__ (env-migrate writer) and validator
(env-presence checker) can both import without circularity.

Output contract: format_file produces shell-safe output suitable for
`source file` or `set -a; . file; set +a`. Values are single-quoted via
shlex.quote so $, backtick, backslash, and newlines are literal.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path


def read(path: Path) -> dict[str, str]:
    """Parse a .env file into {var: value}.

    Handles both `VAR=value` and `export VAR=value` forms. Parses
    shell-quoted values via shlex.split (handles shlex.quote output like
    ``'it'"'"'s'``). Returns {} if the file doesn't exist. Lines that
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
            k = k[len("export ") :].strip()
        v = v.strip()
        # Parse shell-quoted values (handles shlex.quote output like 'it'"'"'s')
        if v and v[0] in ('"', "'"):
            try:
                v = shlex.split(v)[0]
            except ValueError:
                # Malformed quoting — strip matching outer quotes as fallback
                if len(v) >= 2 and v[0] == v[-1]:
                    v = v[1:-1]
        if k:
            out[k] = v
    return out


def merge_into(path: Path, new_vars: dict[str, str]) -> dict[str, str]:
    """Read existing .env at path, merge new_vars over it (new wins).
    Returns the merged dict. Caller writes it back."""
    return {**read(path), **new_vars}


_VALID_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def format_file(header: str, vars_dict: dict[str, str]) -> str:
    """Render a .env file with `export VAR=<value>` lines, alpha-sorted.

    Values are shell-quoted via shlex.quote so that $, backtick, backslash,
    and newlines in the value are preserved as literals when the file is
    sourced (`. file` or `set -a; . file; set +a`).
    """
    lines = [header, ""]
    for k in sorted(vars_dict):
        if not _VALID_KEY.fullmatch(k):
            raise ValueError(f"env key {k!r} is not a valid shell identifier")
        lines.append(f"export {k}={shlex.quote(str(vars_dict[k]))}")
    lines.append("")
    return "\n".join(lines)
