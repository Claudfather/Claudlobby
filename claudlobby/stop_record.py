"""The stop door's local record of a deliberate stop (#2243).

`bot stop` removes the bot's unit file, and that missing file is the framework's
fact that a bot is stopped: activation refuses to start such a bot, keepalive
skips it, reconcile reads it as not enrolled, and fleet-pulse reads it with
`svc_is_registered`. The unit file says THAT a bot is stopped; this record says
the stop was MEANT. The stop door writes it on every stop, before the unit file
goes and whatever the plane does, and `bot start` removes it once the unit is
back. fleet-pulse reads only this record to keep a stopped bot silent, so a stop
made while the plane is down stays quiet; the plane's `bot_stopped` row is the
audit trail. A bot with no unit file and no record is a fault (`unit_missing`).

One JSON line in `<bot_dir>/data/.stopped`, keys sorted. fleet-pulse.sh reads
`stopped_epoch` and `by` from it with sed, so `by` is held to a plain alias and
the line never breaks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import stat
import tempfile
import time

RECORD_NAME = ".stopped"
_BY = re.compile(r"[A-Za-z0-9:/._@-]{1,128}")
_REASON_MAX_BYTES = 2000


class StopRecordError(ValueError):
    pass


def record_path(bot_dir: Path) -> Path:
    return Path(bot_dir) / "data" / RECORD_NAME


def _data_dir(bot_dir: Path, *, create: bool) -> Path:
    data = Path(bot_dir) / "data"
    try:
        info = data.lstat()
    except FileNotFoundError:
        if not create:
            raise
        data.mkdir(mode=0o700)
        info = data.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise StopRecordError("the bot's data directory is not a directory this user owns")
    return data


def check_reason(reason: str | None) -> str | None:
    """A stop reason is one printable line of at most 2000 UTF-8 bytes, or none."""
    if reason is None:
        return None
    if (not isinstance(reason, str) or not reason.strip() or not reason.isprintable()
            or len(reason.encode("utf-8")) > _REASON_MAX_BYTES):
        raise StopRecordError("a stop reason is one printable line of at most 2000 UTF-8 bytes")
    return reason


def write_stop(bot_dir: Path, *, by: str, reason: str | None, request_id: str,
               now: float | None = None) -> dict:
    """Write (or replace) the record, atomically, and return what it holds."""
    if not isinstance(by, str) or not _BY.fullmatch(by):
        raise StopRecordError("the recorded caller must be a plain alias")
    reason = check_reason(reason)
    epoch = int(time.time() if now is None else now)
    record = {"by": by, "reason": reason, "request_id": str(request_id),
              "stopped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
              "stopped_epoch": epoch}
    data = _data_dir(bot_dir, create=True)
    fd, tmp = tempfile.mkstemp(dir=data, prefix=RECORD_NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, data / RECORD_NAME)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return record


def read_stop(bot_dir: Path) -> dict | None:
    """None when there is no record; {} when one is there but cannot be read.

    Presence is the fact the sweep keeps a bot silent on, so an unreadable
    record still says a stop was recorded."""
    path = record_path(bot_dir)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    if not stat.S_ISREG(info.st_mode):
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def clear_stop(bot_dir: Path) -> bool:
    """Remove the record; True when there was one."""
    try:
        record_path(bot_dir).unlink()
    except FileNotFoundError:
        return False
    return True


def unit_installed(label: str, *, home: Path | None = None, system: str | None = None) -> bool:
    """Whether the bot's unit file is installed: supervisor.sh's `svc_is_registered`,
    for Python readers (parity-pinned by tests/test_stop_record.py)."""
    if not label:
        return False
    home = Path.home() if home is None else Path(home)
    system = platform.system() if system is None else system
    if system == "Linux":
        return (home / ".config/systemd/user" / f"{label}.service").is_file()
    if system == "Darwin":
        return (home / "Library/LaunchAgents" / f"{label}.plist").is_file()
    return False
