"""Explicit operator management of Claude Code's OS-wide channel approvals."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from ..command_result import CommandFailure, CommandOutput


_TELEGRAM = (
    {"marketplace": "claude-plugins-official", "plugin": "telegram"},
    {"marketplace": "claudfather-plugins", "plugin": "telegram"},
)


def _managed_path() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform.startswith("linux"):
        return Path("/etc/claude-code/managed-settings.json")
    raise CommandFailure("unavailable", "managed channel approvals are supported on macOS and Linux only")


def _admin_hint(path: Path) -> str:
    return f"ask a host administrator to provision and grant write access to {path.parent}; no sudo is invoked"


def _read(path: Path) -> tuple[dict, os.stat_result | None]:
    if path.parent.is_symlink() or path.parent.resolve() != path.parent or path.is_symlink():
        raise CommandFailure("conflict", "managed settings path is redirected")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        raise CommandFailure("unavailable", "managed settings cannot be read", hint=_admin_hint(path)) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CommandFailure("conflict", "managed settings is not a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            value = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise CommandFailure("conflict", "managed settings is invalid JSON; left untouched") from exc
    except OSError as exc:
        raise CommandFailure("unavailable", "managed settings cannot be read", hint=_admin_hint(path)) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        raise CommandFailure("conflict", "managed settings must be a JSON object; left untouched")
    entries = value.get("allowedChannelPlugins", [])
    if not isinstance(entries, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("marketplace"), str)
        or not item["marketplace"] or not isinstance(item.get("plugin"), str)
        or not item["plugin"] for item in entries
    ):
        raise CommandFailure("conflict", "managed channel approvals have an invalid shape; left untouched")
    return value, info


def _missing(settings: dict) -> list[dict[str, str]]:
    current = {(item["marketplace"], item["plugin"])
               for item in settings.get("allowedChannelPlugins", [])}
    return [dict(item) for item in _TELEGRAM
            if (item["marketplace"], item["plugin"]) not in current]


def _write(path: Path, settings: dict, previous: os.stat_result | None) -> None:
    if not path.parent.is_dir():
        raise CommandFailure("unavailable", "managed settings directory is absent", hint=_admin_hint(path))
    try:
        fd, name = tempfile.mkstemp(prefix=".managed-settings-", dir=path.parent)
    except OSError as exc:
        raise CommandFailure("unavailable", "managed settings directory is not writable",
                             hint=_admin_hint(path)) from exc
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if previous is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
                temporary_info = os.fstat(stream.fileno())
                if (temporary_info.st_uid, temporary_info.st_gid) != (previous.st_uid, previous.st_gid):
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            else:
                # mkstemp defaults to 0600. The managed file must be readable
                # by the unprivileged Claude Code sessions it governs.
                os.fchmod(stream.fileno(), 0o644)
            json.dump(settings, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat() if path.exists() or path.is_symlink() else None
        if (previous is None and current is not None or previous is not None and
                (current is None or (current.st_dev, current.st_ino, current.st_mtime_ns)
                 != (previous.st_dev, previous.st_ino, previous.st_mtime_ns))):
            raise CommandFailure("conflict", "managed settings changed during approval; left untouched")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CommandFailure("unavailable", "managed settings write failed; inspect its current state",
                             hint=_admin_hint(path)) from exc
    finally:
        temporary.unlink(missing_ok=True)


def dispatch(args) -> CommandOutput:
    from .host import _operator_shell

    _operator_shell()
    if args.fleet or args.seed:
        raise CommandFailure("invalid_argument", "host channels does not select a fleet")
    if args.public_command == "host.channels.approve":
        from .operator_context import require_operator_context
        require_operator_context(args.root)
    path = _managed_path()
    settings, info = _read(path)
    missing = _missing(settings)
    action = args.public_command
    if action == "host.channels.approve" and missing:
        updated = dict(settings)
        updated["allowedChannelPlugins"] = [*settings.get("allowedChannelPlugins", []), *missing]
        _write(path, updated, info)
        settings = updated
    elif action not in {"host.channels.check", "host.channels.approve"}:
        raise CommandFailure("invalid_argument", "unsupported host channels command")
    approved = not _missing(settings)
    data = {"path": str(path), "present": info is not None or action == "host.channels.approve" and approved,
            "approved": approved, "missing": [] if action == "host.channels.approve" else missing,
            "channels_enabled": settings.get("channelsEnabled"),
            "changed": action == "host.channels.approve" and bool(missing)}
    note = "approved" if approved else "missing approvals"
    if settings.get("channelsEnabled") is not True:
        note += "; managed channelsEnabled is not true, so organization channels may remain blocked"
    return CommandOutput(data, lines=(f"Managed Telegram channels: {note} at {path}.",))
