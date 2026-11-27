"""Tiny .env file parser used across the compositor.

Hand-rolled because the .env files claudlobby reads are simple shell-style
files, not the full POSIX env-var grammar. Helpers:

  - read(path)               → parse to dict, strips `export ` prefix and quotes
  - format_file(header, vars) → render `export VAR=<quoted>` lines for writing
  - _write_config(...)       → the one private writer: 0600 temp file, fsync,
                               atomic replace (shared with fleet setup); the
                               file keeps an existing one's owner read and
                               write bits, so 0400 stays 0400
  - refuse_unsafe_destination(path) → refuse a symlink or non-regular file
                               before any private write

Lives in its own module so __main__ (migration env writer) and validator
(env-presence checker) can both import without circularity.

Output contract: format_file produces shell-safe output suitable for
`source file` or `set -a; . file; set +a`. Values are single-quoted via
shlex.quote so $, backtick, backslash, and newlines are literal.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

from .command_result import CommandFailure

_log = logging.getLogger(__name__)


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


def _sync_parent(path: Path) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _kept_mode(destination: Path) -> int:
    """The mode the private writer gives ``destination``: an existing regular file's owner
    read and write bits (0640 and 0644 become 0600, 0400 stays 0400), else 0600."""
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        return 0o600
    return stat.S_IMODE(info.st_mode) & 0o600 if stat.S_ISREG(info.st_mode) else 0o600


def _write_config(destination: Path, content: bytes, *, replace: bool, prefix: str = ".fleet-yaml-") -> None:
    """Write ``content`` to ``destination`` from a temp file in its folder, 0600 from the first byte.

    Before the rename the temp file takes ``_kept_mode(destination)``. A folder fsync that
    fails once the new file is in place is a warning, not an error: the file is live.
    """
    fd, name = tempfile.mkstemp(prefix=prefix, dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _kept_mode(destination))
        if replace:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise CommandFailure("conflict", "conflict: fleet configuration changed while setup was preparing") from exc
        try:
            _sync_parent(destination)
        except OSError as exc:
            _log.warning("%s: written, but its folder could not be synced: %s", destination, exc)
    finally:
        temporary.unlink(missing_ok=True)


def refuse_unsafe_destination(path: Path) -> None:
    """Refuse a private write over a symlink or a non-regular file; nothing is touched."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        kind = "a symlink" if path.is_symlink() else "not a regular file"
        raise CommandFailure(
            "conflict",
            f"conflict: {path} is {kind}; left unchanged",
            hint="replace it with a regular file (copy the values in), then run the command again",
        )
