#!/usr/bin/env python3
"""Read a bot unit's WorkingDirectory without executing its contents.

Exit 0 for the expected directory, 1 for a different owner, and 3 when
ownership cannot be established. Only the compositor's unit forms are read;
unsupported syntax must preserve an installed unit, never authorize removal.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path


class _UniqueKeys(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError("duplicate plist key")
        super().__setitem__(key, value)


def _systemd_directory(text: str) -> str:
    section = ""
    services = 0
    directories = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            # A previous directive can continue across a would-be ownership
            # line. The composer emits no continuations, so refuse the unit.
            raise ValueError("unsupported continuation")
        if line.startswith("["):
            if not line.endswith("]"):
                raise ValueError("malformed section")
            section = line
            if section == "[Service]":
                services += 1
            continue
        if section == "[Service]" and line.startswith("WorkingDirectory"):
            if not line.startswith("WorkingDirectory="):
                raise ValueError("unsupported WorkingDirectory directive")
            directories.append(line.split("=", 1)[1].strip())
    if services != 1 or len(directories) != 1:
        raise ValueError("missing or ambiguous WorkingDirectory")
    value = directories[0]
    # The renderer emits a raw absolute path. Do not guess at systemd quoting,
    # escapes, specifiers or continuation syntax; those need an operator.
    if any(char in value for char in "\\\"'%"):
        raise ValueError("unsupported WorkingDirectory syntax")
    return value


def _canonical_directory(value: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("invalid working directory")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("working directory is not absolute")
    return path.resolve()


def main(unit_file: str, expected_bot_dir: str) -> int:
    try:
        unit = Path(unit_file)
        if unit.suffix == ".plist":
            content = plistlib.loads(unit.read_bytes(), dict_type=_UniqueKeys)
            if not isinstance(content, dict):
                return 3
            directory = content.get("WorkingDirectory")
        elif unit.suffix == ".service":
            directory = _systemd_directory(unit.read_text(encoding="utf-8"))
        else:
            return 3
        return 0 if _canonical_directory(directory) == _canonical_directory(expected_bot_dir) else 1
    except Exception:
        # Any failed read/parse is unknown. In particular, plist parsers can
        # raise different exception classes for malformed XML and binary data.
        return 3


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: bot-unit-owner.py <unit-file> <expected-bot-dir>", file=sys.stderr)
        sys.exit(3)
    sys.exit(main(sys.argv[1], sys.argv[2]))
