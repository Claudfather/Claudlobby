"""A bot's request for its own compaction (#1053, PR 2 of its build).

`bot compact <its own ID>` records a request instead of refusing. The caller's
own turn is running while it runs the CLI, so the door types nothing. The
request is one JSON line in `<bot_dir>/data/.compact-request`, keys sorted, and
a `compact_requested` row on the plane; `--cancel` takes the pending request
away and writes `compact_cancelled`.

- One request at a time: a call while one is pending returns that one.
- One request per request ID: a call whose request ID the plane already holds
  records nothing (`replayed`), so a replayed call cannot ask twice.
- The file is the fact a reader acts on and the row is the audit trail. The
  file is written first, and a plane that refuses the row does not refuse the
  request (`recording: degraded`), as with the stop door's record (#2243).

Nothing acts on a request yet. The build's executor (PR 4) is to read this
file, type `/compact` at the bot's next idle, empty prompt, and take the
request away as `take_request` does.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
import time

RECORD_NAME = ".compact-request"


class CompactRequestError(ValueError):
    pass


@dataclass(frozen=True)
class CompactRequestResult:
    fleet: str
    bot: str
    release_id: str | None
    control: str
    # requested, pending, replayed, cancelled or none_pending
    outcome: str
    request: dict | None
    # committed or degraded; None when the call wrote no row
    recording: str | None
    source_revision: str | None = None
    config_fingerprint: str | None = None


def record_path(bot_dir: Path) -> Path:
    return Path(bot_dir) / "data" / RECORD_NAME


def _data_dir(bot_dir: Path) -> Path:
    data = Path(bot_dir) / "data"
    try:
        info = data.lstat()
    except FileNotFoundError:
        data.mkdir(mode=0o700)
        info = data.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise CompactRequestError("the bot's data directory is not a directory this user owns")
    return data


def _read(path: Path) -> dict | None:
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


def read_request(bot_dir: Path) -> dict | None:
    """The pending request: None when there is none, {} when one is there but
    cannot be read (it is still pending)."""
    return _read(record_path(bot_dir))


def write_request(bot_dir: Path, record: dict) -> bool:
    """Create the request unless one is pending: True when this call created it,
    False when one was already there, which stays as it was. A reader sees the
    whole line or no file: the line goes to a temporary file, which is then
    linked to the name, and a link refuses a name that exists."""
    data = _data_dir(bot_dir)
    fd, tmp = tempfile.mkstemp(dir=data, prefix=RECORD_NAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, data / RECORD_NAME)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def take_request(bot_dir: Path) -> dict | None:
    """Take the pending request away and return what it held ({} when it could
    not be read); None when none was pending. The rename is the one step that
    takes it, so of two takers only one gets it."""
    path = record_path(bot_dir)
    taken = path.with_name(f"{RECORD_NAME}.{os.getpid()}.{time.monotonic_ns()}.taken")
    try:
        os.rename(path, taken)
    except FileNotFoundError:
        return None
    try:
        return _read(taken) or {}
    finally:
        try:
            os.unlink(taken)
        except OSError:
            pass


def _row(root: Path, event: str, *, fleet: str, bot: str, request_id: str, data: dict) -> str:
    """The plane's row for a request or a cancel: committed, duplicate (the plane
    already holds this request ID's row) or degraded. Never raises."""
    from .plane.emit_api import emit_batch
    from .plane.fleet_events import fleet_event_request

    raw = fleet_event_request(event, fleet=fleet, subject_kind="actor", subject=f"bot:{fleet}/{bot}",
                              source="lifecycle", key=f"{event}:{request_id}", data=data)
    try:
        recorded = emit_batch(root, [raw], require_commit=True)[0]
    except Exception:  # noqa: BLE001 - a request must not depend on the plane
        return "degraded"
    return recorded.status if recorded.status in {"committed", "duplicate"} else "degraded"


def record_own_compaction(root: Path, *, fleet: str, bot: str, bot_dir: Path, request_id: str,
                          cancel: bool, context, now: float | None = None
                          ) -> tuple[str, dict | None, str | None]:
    """Record (or with `cancel`, withdraw) the bot's request for its own compaction.

    Returns (outcome, request, recording). `context` is called once, for a new
    request only, and returns the bot's live context as `current_context` does;
    a reader fault costs that field, never the request.
    """
    by = f"bot:{fleet}/{bot}"
    if cancel:
        taken = take_request(bot_dir)
        if taken is None:
            return "none_pending", None, None
        recording = _row(root, "compact_cancelled", fleet=fleet, bot=bot, request_id=request_id,
                         data={"by": by, "request_id": request_id,
                               "cancelled_request_id": taken.get("request_id"),
                               "requested_at": taken.get("requested_at")})
        return "cancelled", taken, "committed" if recording == "duplicate" else recording
    pending = read_request(bot_dir)
    if pending is not None:
        return "pending", pending, None
    try:
        live = context() or {}
    except Exception:  # noqa: BLE001 - named in the field, not raised
        live = {"reason": "unread"}
    epoch = int(time.time() if now is None else now)
    record = {"by": by, "request_id": request_id,
              "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
              "requested_epoch": epoch,
              "context": {"tokens": live.get("tokens"), "at": live.get("at"),
                          "reason": live.get("reason")}}
    if not write_request(bot_dir, record):
        return "pending", read_request(bot_dir), None
    recording = _row(root, "compact_requested", fleet=fleet, bot=bot, request_id=request_id,
                     data=record)
    if recording == "duplicate":
        # This request ID asked before: take back the file just written. The caller's
        # own turn is running, so no reader can have acted on it in between.
        take_request(bot_dir)
        return "replayed", None, None
    return "requested", record, recording
