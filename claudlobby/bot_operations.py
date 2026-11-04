"""Committed-current bot enrollment and de-enrollment.

Committed configuration owns Git native placement and bot identity; the
transitional sealed path retains its activation placement. This module observes
one exact target before and after the existing native supervisor adapter acts. It never purges a bot or changes the
active roster, release selection, or retained actor bindings.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import atexit
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple
from uuid import uuid4

from .supervision_inventory import _target
from .supervision_runtime import RuntimeEvidenceError, assert_quiescent, unit_start_budget
from .config_plan import path_state
from .config_units import current_git_declarations
from .loader import parse_frontmatter
from .operation_context import resolve_operation_scope
from .plane.emit_api import emit_batch
from .plane.fleet_events import fleet_event_request
from .runtime_admission import GitCurrentRuntime, RuntimeIdentity, mutation_admission
from .stop_record import StopRecordError, check_reason, clear_stop, write_stop
from .supervision import build_supervision_spec
from .supervision_inventory import Adapter, InventoryError, _catalog, collect_enrollment


_SELF_RESPONSE_WAIT_S = 30
# A KillMode=process bot unit reads stopped once its ExecStop returns, while the
# session it started (tmux, claude, MCP servers) is still exiting. The stop's
# quiet check waits this long for that session to leave the unit's cgroup (#2227).
_STOP_SETTLE_S = 30


class BotLifecycleError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False,
                 unavailable: bool = False, release_id: str | None = None,
                 target: str | None = None, handoff_reason: str | None = None,
                 busy: bool = False, skip_reason: str | None = None,
                 source_revision: str | None = None, config_fingerprint: str | None = None):
        self.effect_attempted = effect_attempted
        self.unavailable = unavailable
        self.release_id = release_id
        self.source_revision = source_revision
        self.config_fingerprint = config_fingerprint
        self.target = target
        self.handoff_reason = handoff_reason
        # Another lifecycle operation holds the host lock; nothing was attempted.
        self.busy = busy
        # A deliberate operator state (for example de-enrolled) refused this verb.
        self.skip_reason = skip_reason
        super().__init__(reason)


class StaleHandoffError(BotLifecycleError):
    """A saved handoff the verifier read and found too old for its window (#2553).

    It carries the two ages its verdict read, in seconds: ``age_s`` from the
    capture's own last_updated, and ``mtime_age_s`` from its file's write,
    which is negative while the file's time is ahead of this host's clock.
    """

    def __init__(self, *, age_s: float, mtime_age_s: float):
        self.age_s, self.mtime_age_s = age_s, mtime_age_s
        super().__init__("self restart requires a fresh owned session handoff")


@dataclass(frozen=True)
class BotLifecycleResult:
    fleet: str
    bot: str
    release_id: str | None
    target: str
    state: str
    changed: bool
    readiness: str
    handoff: str = "not_applicable"
    request_id: str | None = None
    log_path: str | None = None
    reason: str | None = None
    # A stop: whether its local record and its plane row landed; a start: whether its
    # plane row landed (#2243).
    recording: str | None = None
    # A start that could not remove the stop record (#2243): while it stays, a later
    # loss of the bot's unit reads as a deliberate stop. None when it was removed or
    # there was none.
    stop_record_kept: bool | None = None
    source_revision: str | None = None
    config_fingerprint: str | None = None


@dataclass(frozen=True)
class BotHandoffResult:
    fleet: str
    bot: str
    release_id: str | None
    target: str
    handoff: str
    reason: str
    source_revision: str | None = None
    config_fingerprint: str | None = None


class HandoffState(NamedTuple):
    state: str  # saved, pending or unknown
    reason: str
    requested_at: str | None = None
    written_at: str | None = None


@dataclass(frozen=True)
class BotHandoffCheck:
    fleet: str
    bot: str
    release_id: str | None
    handoff: str
    reason: str
    requested_at: str | None
    written_at: str | None
    source_revision: str | None = None
    config_fingerprint: str | None = None


@dataclass(frozen=True)
class BotControlResult:
    fleet: str
    bot: str
    release_id: str | None
    target: str
    control: str
    outcome: str
    source_revision: str | None = None
    config_fingerprint: str | None = None


def _owned_handoff_dir(bot_dir: Path) -> Path:
    """The bot's .claude directory, when it and the bot directory are this user's own directories."""
    handoff_dir = bot_dir / ".claude"
    try:
        bot_info = bot_dir.lstat()
        dir_info = handoff_dir.lstat()
    except OSError as exc:
        raise BotLifecycleError("self restart requires an owned handoff directory") from exc
    if (not stat.S_ISDIR(bot_info.st_mode) or bot_info.st_uid != os.getuid()
            or not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.getuid()):
        raise BotLifecycleError("self restart requires an owned handoff directory")
    return handoff_dir


# The coarsest file clock in common use, FAT's, keeps 2 s.
_HANDOFF_CLOCK_WAIT_S = 5


def _file_clock_ns(handoff_dir: Path) -> int:
    """One reading of the clock that stamps files in handoff_dir: a new file's mtime.

    The file has no name where the filesystem takes O_TMPFILE, and is unlinked
    at once elsewhere.
    """
    with tempfile.TemporaryFile(dir=handoff_dir) as stream:
        return os.fstat(stream.fileno()).st_mtime_ns


def _handoff_clock_ns(bot_dir: Path) -> int:
    """A handoff request's time, in epoch ns, on the clock that stamps the owned handoff file (#2528).

    A file's mtime comes from the filesystem, not from time.time_ns(): the
    kernel stamps files from a clock that advances once a timer tick (it
    trailed time_ns() by up to 7.4 ms with a 4 ms tick), and a filesystem can
    keep whole seconds. So a request timed with time_ns() can postdate the
    handoff written for it. A file created in the handoff's own directory is
    stamped by the same clock, at the same granularity. Its first reading can
    equal a capture saved just before it, in the same tick, so the request
    takes the clock's next reading: every file already written there reads
    older than it, and no file written after it does.
    """
    handoff_dir = _owned_handoff_dir(bot_dir)
    try:
        first = _file_clock_ns(handoff_dir)
        deadline = time.monotonic() + _HANDOFF_CLOCK_WAIT_S
        while (stamp := _file_clock_ns(handoff_dir)) <= first:
            if time.monotonic() > deadline:
                raise OSError("the handoff directory's file clock did not move")
            time.sleep(0.001)
    except OSError as exc:
        raise BotLifecycleError("self restart requires an owned handoff directory") from exc
    return stamp


_REFRESH_BEGIN = b"<!-- claudlobby first-adoption reference refresh begin -->"
_REFRESH_END = b"<!-- claudlobby first-adoption reference refresh end -->"
_TIMESTAMP = re.compile(rb"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _refresh_envelope(bot_dir: Path, timestamp: str, *, field: str = "references_refreshed") -> bytes:
    """Fresh reference timestamp, explicitly separate from the old session capture.

    Before #2094 the field was ``last_updated``; the reader still accepts that
    shape, and the next write is always this one.
    """
    return (f"---\ncwd: {bot_dir}\n{field}: {timestamp}\nschema_version: 2\n---\n".encode()
            + _REFRESH_BEGIN + b"\n"
            + b"This timestamp marks a canonical task-reference refresh, not a new session capture.\n"
            + b"The original handoff bytes below are historical and their capture freshness is unverified.\n"
            + b"Read the canonical task and assignment IDs in the section at the end of this file.\n"
            + _REFRESH_END + b"\n\n")



def _strip_envelope(previous: bytes, bot_dir: Path, path: Path) -> bytes:
    """Split off the refresh envelope where the refresh writes it: a frontmatter
    followed at once by a line that is exactly _REFRESH_BEGIN (#2094). Both
    shapes are read, ``references_refreshed`` and the ``last_updated`` written
    before #2094. Its time is checked for shape only and never reused."""
    if not previous.startswith(b"---\n"):
        return previous
    end = previous.find(b"\n---\n", 3)
    if end < 0 or not previous.startswith(_REFRESH_BEGIN + b"\n", end + 5):
        return previous  # the session's own frontmatter
    head = previous[:end + 5]
    for field in ("references_refreshed", "last_updated"):
        found = re.search(rb"^" + field.encode() + rb": (\S+)$", head, re.M)
        if found and _TIMESTAMP.fullmatch(found.group(1)):
            envelope = _refresh_envelope(bot_dir, found.group(1).decode(), field=field)
            if previous.startswith(envelope):
                return previous[len(envelope):]
    raise ValueError(f"existing canonical reference refresh is malformed: {path}")


def _handoff_capture(bot_dir: Path, *, not_before: int | None = None) -> tuple[float, datetime]:
    """The owned handoff file's mtime and its session capture's last_updated.

    The session capture's own frontmatter decides. Under the activation's
    reference refresh envelope (#2094) that is the capture below it: the
    activation's own reader finds the envelope, so the two agree on it, and
    one that reader refuses is refused here by name (#2119).
    """
    path = _owned_handoff_dir(bot_dir) / "session.md"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise BotLifecycleError("self restart requires an owned handoff file")
            head = stream.read(8192)
        try:
            head = _strip_envelope(head, bot_dir, path)
        except ValueError as exc:
            raise BotLifecycleError(
                "self restart requires a fresh owned session handoff, and the reference refresh"
                " envelope at its top is malformed (the next activation refuses it too):"
                " delete that envelope block") from exc
        close = head.find(b"\n---\n", 4)
        if not head.startswith(b"---\n") or close < 0:
            raise ValueError("missing handoff frontmatter")
        # Only the frontmatter is decoded: the read can end inside a character.
        header = head[:close + 5].decode("utf-8")
        frontmatter = header[4:-5]
        if len(re.findall(r"^last_updated\s*:", frontmatter, re.MULTILINE)) != 1:
            raise ValueError("missing UTC handoff timestamp")
        fields, _ = parse_frontmatter(header)
        value = fields.get("last_updated")
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("invalid handoff timestamp")
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("naive handoff timestamp")
        if not_before is not None and (
                type(not_before) is not int or not_before < 0
                or info.st_mtime_ns < not_before
                or stamp.timestamp() < not_before // 1_000_000_000):
            raise ValueError("handoff predates its request")
    except (OSError, UnicodeError, ValueError) as exc:
        raise BotLifecycleError("self restart requires a fresh owned session handoff") from exc
    return info.st_mtime, stamp


# A handoff is fresh while at most this many seconds old, by its capture's timestamp and its file's mtime.
_HANDOFF_FRESH_S = 300


def _fresh_self_handoff(bot_dir: Path, *, observed_capture: bool = False,
                        not_before: int | None = None, max_age_s: int = _HANDOFF_FRESH_S) -> float:
    """Require a fresh owned handoff; explicit capture already witnessed its write.

    Returns the capture's age in seconds. pre-stop-handoff.sh asks this check
    whether a handoff below the refresh envelope is recent (#2119).
    An optional request time in epoch nanoseconds also binds the verified
    file write and the capture's second-resolution timestamp to that request.
    Take that time with `_handoff_clock_ns`, never time.time_ns() (#2528).
    Only activation passes max_age_s, its --handoff-max-age for bots that
    saved and stayed idle (#963); bot handoff and self restart keep 300 s.
    """
    mtime, stamp = _handoff_capture(bot_dir, not_before=not_before)
    age = datetime.now(timezone.utc).timestamp() - stamp.timestamp()
    mtime_age = datetime.now(timezone.utc).timestamp() - mtime
    # Explicit capture's changed checksum proves a fresh write, but resume
    # still reads last_updated. Require a recent model timestamp on both
    # paths; tolerate future clock skew observed in the live canary.
    if not (-30 <= mtime_age <= max_age_s) or age > max_age_s:
        raise StaleHandoffError(age_s=age, mtime_age_s=mtime_age) from ValueError("handoff is stale")
    return age


_HANDOFF_REQUEST = ".handoff-request"
_HANDOFF_CAPTURE_WAIT_S = 900
_HANDOFF_CAPTURE_POLL_S = 2


def _handoff_boundary(requested, native_result):
    return None if native_result == {"marker": "handoff-skipped:recent", "returncode": 0} else requested


def wait_for_handoff(bot_dir: Path, *, requested: int, native_result: dict | None,
                     target: str, max_age_s: int = _HANDOFF_FRESH_S,
                     wait_s: int = _HANDOFF_CAPTURE_WAIT_S,
                     poll_s: int = _HANDOFF_CAPTURE_POLL_S, _clock=None) -> float:
    """Verify one recorded save, observing a native timeout without another send.

    The caller persists intent before sending and owns stop/start. Only a
    successful native recent-skip permits a capture older than the request.
    A missing/unknown native result can be reconciled by a genuine late save,
    but never authorizes another native request or an inferred successful save.
    """
    boundary = _handoff_boundary(requested, native_result)
    if native_result != {"marker": "handoff-timeout", "returncode": 3}:
        return _fresh_self_handoff(bot_dir, not_before=boundary, max_age_s=max_age_s)
    clock = time if _clock is None else _clock
    started = clock.monotonic()
    deadline, next_progress = started + wait_s, started
    while True:
        try:
            age = _fresh_self_handoff(bot_dir, not_before=boundary, max_age_s=max_age_s)
        except BotLifecycleError as exc:
            now = clock.monotonic()
            if now >= deadline:
                raise BotLifecycleError(
                    f"still waiting for checkpoint from {target}: no fresh verified session handoff "
                    f"arrived within {wait_s} s; the existing handoff request is retained and is never resent",
                    unavailable=True, handoff_reason="timeout") from exc
            if now >= next_progress:
                print(f"waiting for checkpoint from {target} ({now - started:.0f} s elapsed); "
                      "observing the existing request", file=sys.stderr, flush=True)
                next_progress = now + 30
            clock.sleep(min(poll_s, deadline - now))
        else:
            print(f"checkpoint verified for {target}; continuing", file=sys.stderr, flush=True)
            return age


def _write_handoff_request(bot_dir: Path, value: dict) -> bool:
    """Atomically retain request intent/result in the existing owned metadata file."""
    data = bot_dir / "data"
    try:
        data.mkdir(mode=0o700, exist_ok=True)
        info = data.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return False
        fd, tmp = tempfile.mkstemp(dir=data, prefix=_HANDOFF_REQUEST + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, data / _HANDOFF_REQUEST)
            directory_fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError:
        return False
    return True


def _read_handoff_request(bot_dir: Path) -> dict | None:
    """Read the shared save intent without following a redirected metadata file."""
    path = bot_dir / "data" / _HANDOFF_REQUEST
    try:
        data = path.parent.lstat()
        if not stat.S_ISDIR(data.st_mode) or data.st_uid != os.getuid():
            raise OSError("not an owned request directory")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("not an owned request file")
            record = json.load(stream)
        if not isinstance(record, dict):
            raise ValueError("invalid handoff request")
    except FileNotFoundError:
        record = None
    except (OSError, ValueError) as exc:
        raise BotLifecycleError("existing handoff request cannot be verified; inspect before another save",
                                unavailable=True) from exc
    return record


def _new_handoff_request(bot_dir: Path, **fields) -> dict:
    requested = _handoff_clock_ns(bot_dir)
    record = {"request_id": str(uuid4()),
              "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(requested // 1_000_000_000)),
              "requested_ns": requested, "native_result": None, **fields}
    if not _write_handoff_request(bot_dir, record):
        raise BotLifecycleError("cannot record handoff intent; no handoff was sent", unavailable=True)
    return record


def _record_native_handoff(bot_dir: Path, record: dict, outcome) -> list[str]:
    marker = outcome.stdout.strip().splitlines()[-1:] if outcome.stdout else []
    record["native_result"] = {"marker": marker[0] if marker else "unrecognized",
                               "returncode": outcome.returncode}
    if not _write_handoff_request(bot_dir, record):
        raise BotLifecycleError("cannot record handoff result; existing request is retained",
                                unavailable=True)
    return marker


def save_planned_checkpoint(bot_dir: Path, label: str, tmpdir: str, adapter: Adapter, *,
                            operation_id: str) -> dict:
    """Observe exact durable native continuation; never ask the model to save."""
    from .native_session import checkpoint, checkpoint_account, NativeSessionBusy, NativeSessionError
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", operation_id):
        raise BotLifecycleError("checkpoint requires one exact operation ID")
    try:
        account = checkpoint_account(bot_dir, label, adapter)
    except (NativeSessionError, OSError, InventoryError, subprocess.SubprocessError) as exc:
        raise BotLifecycleError(str(exc), unavailable=True) from exc
    started = time.monotonic()
    deadline, next_progress = started + _HANDOFF_CAPTURE_WAIT_S, started
    while True:
        try:
            return checkpoint(bot_dir, label, tmpdir, adapter, operation_id=operation_id, account=account)
        except NativeSessionBusy as exc:
            now = time.monotonic()
            if now >= deadline:
                raise BotLifecycleError("still waiting for native idle; original update is retained and no model request was sent",
                                        unavailable=True, handoff_reason="timeout") from exc
            if now >= next_progress:
                print(f"waiting for native idle from {label} ({now - started:.0f} s elapsed); no model request",
                      file=sys.stderr, flush=True)
                next_progress = now + 30
            time.sleep(min(_HANDOFF_CAPTURE_POLL_S, deadline - now))
        except (NativeSessionError, OSError, InventoryError, subprocess.SubprocessError) as exc:
            raise BotLifecycleError(str(exc), unavailable=True) from exc


def _checkpoint_image(bot_dir: Path) -> dict:
    """Hash only the owned capture, refusing replacement or concurrent writes."""
    try:
        path = _owned_handoff_dir(bot_dir) / "session.md"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                raise OSError("not an owned capture")
            digest = hashlib.sha256(stream.read()).hexdigest()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise OSError("capture changed while reading")
    except OSError as exc:
        raise BotLifecycleError("checkpoint capture cannot be verified", unavailable=True) from exc
    return {"capture_sha256": digest, "capture_mtime_ns": after.st_mtime_ns}


def _checkpoint_request(bot_dir: Path, proof: dict, *, absent_ok=False) -> dict | None:
    fields = {"schema", "operation_id", "request_id", "requested_ns", "bot_dir", "label", "native_result",
              "last_updated", "capture_sha256", "capture_mtime_ns"}
    if (not isinstance(proof, dict) or set(proof) != fields or type(proof["schema"]) is not int
            or proof["schema"] != 1 or proof["bot_dir"] != str(bot_dir)
            or type(proof["requested_ns"]) is not int or proof["requested_ns"] < 0
            or type(proof["capture_mtime_ns"]) is not int or proof["capture_mtime_ns"] < 0
            or not isinstance(proof["capture_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof["capture_sha256"])):
        raise BotLifecycleError("checkpoint request proof is invalid", unavailable=True)
    record = _read_handoff_request(bot_dir)
    if record is None and absent_ok:
        return None
    if (record is None or record.get("checkpoint_proof") != proof
            or any(record.get(key) != proof[key] for key in
                   ("operation_id", "request_id", "requested_ns", "native_result"))
            or record.get("checkpoint_label") != proof["label"]):
        raise BotLifecycleError("checkpoint request differs from its recorded proof", unavailable=True)
    return record


def verify_checkpoint_after_stop(bot_dir: Path, proof: dict) -> None:
    """Require unchanged request/capture after caller-proven quiet, without an age cliff.

    This is a file verifier, not evidence that a native consumer stopped or
    did no later work. The caller must retain its quiet/no-new-work fence.
    """
    if proof.get("kind") == "native-session-v1":
        from .native_session import verify
        verify(bot_dir, proof)
        return
    _checkpoint_request(bot_dir, proof)
    image = _checkpoint_image(bot_dir)
    _, stamp = _handoff_capture(bot_dir, not_before=_handoff_boundary(proof["requested_ns"], proof["native_result"]))
    if (image != {key: proof[key] for key in ("capture_sha256", "capture_mtime_ns")}
            or stamp.isoformat() != proof["last_updated"] or _checkpoint_image(bot_dir) != image):
        raise BotLifecycleError("checkpoint capture differs from its saved proof", unavailable=True)


def retire_planned_checkpoint(bot_dir: Path, proof: dict) -> None:
    """Under the caller's lifecycle lock, retire only this operation's request."""
    if proof.get("kind") == "native-session-v1":
        from .native_session import confirm
        confirm(bot_dir, expected=proof["session"])
        return
    if _checkpoint_request(bot_dir, proof, absent_ok=True) is not None:
        (bot_dir / "data" / _HANDOFF_REQUEST).unlink()
        directory_fd = os.open(bot_dir / "data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _planned_restart_handoff(bot_dir: Path, label: str, tmpdir: str,
                             adapter: Adapter) -> dict:
    proof = save_planned_checkpoint(bot_dir, label, tmpdir, adapter, operation_id="restart:" + label)
    # The native-session owner retains the exact transcript prefix through
    # launch; a matching process-bound SessionStart completes continuation.
    return proof


def handoff_state(bot_dir: Path) -> HandoffState:
    """What came of the door's last recorded handoff request, from files only (#990).

    saved: the handoff file was written after the request, and its capture's
    own last_updated is no older than the request less the 300 s
    `_fresh_self_handoff` allows a model's timestamp, so a rewrite with no new
    capture (activation's reference refresh) is not one. pending: not written
    since the request; the bot may still be writing it, or may never, since a
    turn can end without writing it (#963). unknown: no request is recorded,
    or the file cannot be verified.
    """
    try:
        record = json.loads((bot_dir / "data" / _HANDOFF_REQUEST).read_text(encoding="utf-8"))
        requested_at = record["requested_at"]
        requested = datetime.strptime(requested_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
        requested_ns = record.get("requested_ns")
        if requested_ns is not None and (type(requested_ns) is not int or requested_ns < 0):
            raise ValueError("invalid request boundary")
    except FileNotFoundError:
        return HandoffState("unknown", "no_request_recorded")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return HandoffState("unknown", "request_unreadable")
    pending = HandoffState("pending", "not_written_since_request", requested_at)
    try:
        info = (bot_dir / ".claude" / "session.md").lstat()
    except FileNotFoundError:
        return pending
    except OSError:
        return HandoffState("unknown", "file_unverified", requested_at)
    if stat.S_ISREG(info.st_mode) and (info.st_mtime_ns < requested_ns if requested_ns is not None
                                      else info.st_mtime < requested):
        return pending
    try:
        mtime, stamp = _handoff_capture(bot_dir)
    except BotLifecycleError:
        return HandoffState("unknown", "file_unverified", requested_at)
    if mtime < requested:
        return pending
    written_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
    if stamp.timestamp() < (requested_ns // 1_000_000_000 if requested_ns is not None else requested - 300):
        return HandoffState("unknown", "capture_predates_request", requested_at, written_at)
    return HandoffState("saved", "written_after_request", requested_at, written_at)


def _self_restart_log(bot_dir: Path) -> tuple[int, Path]:
    log_dir = bot_dir / "logs"
    try:
        info = log_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unowned log directory")
        path = log_dir / "startup.log"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise OSError("unowned startup log")
        return fd, path
    except OSError as exc:
        raise BotLifecycleError("self restart outcome log is unavailable") from exc


def _log_self_result(fd: int, request_id: str, *, status: str, **fields: object) -> None:
    row = {"event": "self_restart", "request_id": request_id, "status": status,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    os.write(fd, (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode())


def _schedule_self_restart(*, root: Path, fleet: str, bot: str,
                           ceiling: int | None, bot_dir: Path) -> BotLifecycleResult:
    with mutation_admission(root, identity=RuntimeIdentity.current(),
                            expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as current:
        provenance = (_release_id(current), _provenance(current))
    log_fd, log_path = _self_restart_log(bot_dir)
    request_id = str(uuid4())
    admitted_read, admitted_write = os.pipe()
    release_read, release_write = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        pid = os.fork()
    except OSError as exc:
        for fd in (log_fd, admitted_read, admitted_write, release_read, release_write):
            os.close(fd)
        raise BotLifecycleError("self restart could not launch its completion witness") from exc
    if pid == 0:
        os.close(admitted_read)
        os.close(release_write)
        admitted = False
        try:
            os.setsid()  # macOS has no setsid utility; leave the dying tmux group.
            null_fd = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null_fd, 0)
            os.close(null_fd)
            os.dup2(log_fd, 1)
            os.dup2(log_fd, 2)

            def after_lock() -> None:
                nonlocal admitted
                os.write(admitted_write, b"1")
                admitted = True
                os.close(admitted_write)
                # The caller flushes its requested result before exiting.
                # Keep both admission locks held while awaiting that exit.
                ready, _, _ = select.select([release_read], [], [], _SELF_RESPONSE_WAIT_S)
                if not ready or os.read(release_read, 1) != b"":
                    raise BotLifecycleError("requesting CLI did not finish its response")

            result = set_bot_running(root=root, fleet=fleet, bot=bot, running=True,
                                     restart=True, ceiling=ceiling, _self_child=True,
                                     _on_lock=after_lock, _expected_provenance=provenance)
            _log_self_result(log_fd, request_id, status="complete",
                             release_id=result.release_id, source_revision=result.source_revision,
                             config_fingerprint=result.config_fingerprint, target=result.target,
                             readiness=result.readiness, handoff=result.handoff)
            os._exit(0)
        except BaseException as exc:
            if not admitted:
                try:
                    os.write(admitted_write, b"0")
                except OSError:
                    pass
            try:
                fields = {"error": type(exc).__name__}
                if isinstance(exc, BotLifecycleError):
                    fields["effect_attempted"] = exc.effect_attempted
                    fields["reason"] = str(exc)
                _log_self_result(log_fd, request_id, status="incomplete", **fields)
            except OSError:
                pass
            os._exit(1)
    os.close(admitted_write)
    os.close(release_read)
    os.close(log_fd)
    ready, _, _ = select.select([admitted_read], [], [], 5)
    admitted = os.read(admitted_read, 1) if ready else b""
    os.close(admitted_read)
    if admitted != b"1":
        os.close(release_write)
        if not ready:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise BotLifecycleError("self restart completion witness was not admitted")

    def release_after_response() -> None:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.close(release_write)

    atexit.register(release_after_response)
    return _bot_result(BotLifecycleResult, current, fleet, bot, _release_id(current), "",
                              "requested", False, "not_checked", "native_session",
                              request_id, str(log_path))


@contextmanager
def _operation_lock(root: Path):
    """Serialize selected bot operations after host mutation admission."""
    path = root / "state/bot-lifecycle.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        linked = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
            raise BotLifecycleError("bot lifecycle lock is not private or changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BotLifecycleError("another bot lifecycle operation is running on this host",
                                    busy=True) from exc
        yield
    finally:
        os.close(fd)


def _release_id(current):
    return None


def _provenance(current):
    return {"source_revision": current.revision, "config_fingerprint": current.config_fingerprint}


def _bot_result(result_type, current, *args, **kwargs):
    return result_type(*args, **kwargs, **_provenance(current))


@dataclass(frozen=True)
class _CurrentBotBinding:
    adapter: object
    manager: str
    declaration: object
    entry: dict
    declarations: tuple
    spec: object
    configuration: dict

    def verify_configuration(self):
        path = self.spec.bot_dir / "bot.conf"
        if self.configuration.get("kind") != "file" or path_state(path)["node"] != self.configuration:
            raise BotLifecycleError("generated bot.conf differs from selected frozen configuration")


def _current_bot_binding(root, current, destination, bot, adapter=None):
    """Bind one committed bot to the same exact native lifecycle owner."""
    package = destination.paths.package
    adapter = adapter or Adapter(package)
    if not isinstance(current, GitCurrentRuntime):
        raise BotLifecycleError("bot lifecycle requires a managed Git current runtime")
    from .active_config import read_current_config
    if (package.installation != current.installation or package.source_revision != current.revision
            or adapter.package.installation != current.installation
            or adapter.package.native != current.installation.native):
        raise BotLifecycleError("bot native adapter differs from current Git installation")
    snapshot = read_current_config(root, installation=current.installation, revision=current.revision)
    if snapshot != current.snapshot:
        raise BotLifecycleError("committed bot configuration changed before operation")
    manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    placement = snapshot.get("supervision")
    if placement is not None:
        if placement["manager"] != manager:
            raise BotLifecycleError("committed native manager differs from this host")
    declarations = current_git_declarations(snapshot, manager)
    matches = [item for item in snapshot["units"] if item["platform"] == manager
               and item["scope"] == "bot" and item["fleet"] == destination.fleet.name
               and item["bot"] == bot and item["enroll"]]
    if len(matches) != 1:
        raise BotLifecycleError("selected bot has no unique declared native unit")
    item = matches[0]
    declaration = next(value for value in declarations if str(value.source) == item["source"])
    if placement is not None:
        selected_domain = placement["domain"]
        if manager == "Darwin":
            if selected_domain not in {f"gui/{os.getuid()}", f"user/{os.getuid()}"}:
                raise BotLifecycleError("committed native domain belongs to another user")
            target = f"{selected_domain}/{declaration.source.stem}"
            if selected_domain == f"gui/{os.getuid()}" and domain == f"user/{os.getuid()}":
                if not hasattr(adapter, "in_selected_gui"):
                    raise BotLifecycleError("selected GUI native adapter is unavailable", unavailable=True)
                adapter = adapter.in_selected_gui(target)
                actual_manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
                if actual_manager != manager:
                    raise BotLifecycleError("selected native manager changed")
        if domain != selected_domain:
            raise BotLifecycleError("native adapter domain differs from committed supervision; "
                                    "invoke from the recorded domain", unavailable=True)
    if placement is not None:
        installed = Path(placement["directory"]) / declaration.source.name
    else:
        # Prototype snapshots have no placement authority after de-enrollment.
        # Exact installed evidence can still support existing-session reads.
        inventory = collect_enrollment(root, declarations, adapter=adapter,
                                       only_names=frozenset({declaration.source.name})).require_complete()
        units = [unit for unit in inventory.units if unit.declaration.source == declaration.source]
        if len(units) != 1 or len(units[0].installed) != 1:
            raise BotLifecycleError("current bot native placement is unavailable; explicit enrollment is required")
        installed = Path(units[0].installed[0].path)
    entry = {"source": str(declaration.source), "installed": str(installed),
             "target": _target(manager, domain, declaration.source)}
    if (installed.parent not in directories or installed.parent.resolve() != installed.parent
            or installed == declaration.source or installed.is_symlink()):
        raise BotLifecycleError("selected bot native placement is redirected or ambiguous")
    spec = build_supervision_spec(destination.fleet.bots[bot], destination.fleet, destination.paths)
    states = [output["state"] for output in snapshot["owned_outputs"]
              if output["target"] == str(spec.bot_dir / "bot.conf")]
    native_label = (entry["target"].removesuffix(".service") if manager == "Linux"
                    else entry["target"].split("/")[-1])
    if (spec.bot_dir != declaration.working_directory or spec.label != declaration.source.stem
            or spec.label != native_label):
        raise BotLifecycleError("bot supervision spec differs from frozen native unit")
    if len(states) != 1:
        raise BotLifecycleError("bot has no unique committed bot.conf")
    return _CurrentBotBinding(adapter, manager, declaration, entry, declarations, spec, states[0])


def _observed(root, declarations, adapter, target, installed):
    inventory = collect_enrollment(root, declarations, adapter=adapter,
                                   only_names=frozenset({installed.name})).require_complete()
    matches = [unit for unit in inventory.units if unit.target == target]
    if len(matches) != 1:
        raise BotLifecycleError("bot has no unique native inventory observation")
    unit = matches[0]
    if len(unit.installed) > 1 or unit.installed and unit.installed[0].path != str(installed):
        raise BotLifecycleError("bot has another installed native definition")
    return unit


def _native(adapter, function, *args, timeout=30):
    effect = function in {"svc_bot_enroll_exact", "svc_bot_disenroll_exact"}
    try:
        result = adapter.call(function, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BotLifecycleError(f"{function} native observation is unavailable",
                                effect_attempted=effect, unavailable=True) from exc
    if result.returncode:
        if function == "svc_bot_disenroll_exact":
            effect = result.stdout.startswith("effect-attempted\n")
        detail = (f"{function} failed after beginning a native effect"
                  if effect else f"{function} refused before a native effect")
        raise BotLifecycleError(f"{detail} (rc {result.returncode})",
                                effect_attempted=effect, unavailable=True)
    return result.stdout.strip()


def handoff_bot(*, root: Path, fleet: str | None, bot: str,
                identity: RuntimeIdentity | None = None,
                adapter: Adapter | None = None) -> BotHandoffResult:
    """Ask the selected private session to save context; leave it running."""
    if not isinstance(bot, str) or not bot or Path(bot).name != bot or bot in {".", ".."}:
        raise BotLifecycleError("supply one exact bot ID")
    identity = identity or RuntimeIdentity.current()
    with mutation_admission(root, identity=identity,
                            expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        with _operation_lock(root):
            destination, origin = resolve_operation_scope(root=root, fleet=fleet)
            if origin is not None and (origin.fleet.name != destination.fleet.name
                                       or origin.bot_id == bot
                                       or origin.bot_id != destination.fleet.manager):
                raise BotLifecycleError("only the selected fleet manager may hand off another bot")
            if bot not in destination.fleet.bots:
                raise BotLifecycleError("bot is not declared in the selected active fleet")
            binding = _current_bot_binding(root, release, destination, bot, adapter)
            adapter, manager = binding.adapter, binding.manager
            declaration, entry, declarations, spec = (binding.declaration, binding.entry,
                                                       binding.declarations, binding.spec)
            binding.verify_configuration()
            unit = _observed(root, declarations, adapter, entry["target"], Path(entry["installed"]))
            if not unit.installed:
                raise BotLifecycleError("bot is not enrolled in its selected native unit")
            session = _native(adapter, "svc_bot_session_observe", spec.bot_dir, spec.label,
                              spec.environment["TMUX_TMPDIR"], entry["installed"], entry["target"])
            if session == "absent":
                return _bot_result(BotHandoffResult, release, destination.fleet.name, bot, _release_id(release),
                                        entry["target"], "skipped", "no_session")
            if session != "ready":
                raise BotLifecycleError("private bot session is unverified", unavailable=True)
            prior_request = _read_handoff_request(spec.bot_dir)
            if (prior_request is not None and (prior_request.get("operation_id") is not None
                                              or handoff_state(spec.bot_dir).state != "saved")):
                raise BotLifecycleError("existing handoff request is unresolved; inspect with --check; "
                                        "it is never automatically resent", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"], handoff_reason="pending")
            try:
                request = _new_handoff_request(spec.bot_dir)
            except BotLifecycleError as exc:
                raise BotLifecycleError(str(exc), unavailable=True) from exc
            try:
                outcome = adapter.call("svc_activation_handoff", spec.bot_dir, spec.label,
                                       spec.environment["TMUX_TMPDIR"], "explicit", timeout=45)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BotLifecycleError("bot handoff outcome is unavailable", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"]) from exc
            marker = _record_native_handoff(spec.bot_dir, request, outcome)
            skipped = (["handoff-skipped:recent"], ["handoff-skipped:capability"],
                       ["handoff-skipped:no-session"])
            if marker == ["handoff-timeout"]:
                raise BotLifecycleError("bot handoff timed out after 30 seconds", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"], handoff_reason="timeout")
            if outcome.returncode or marker not in (["handoff-saved"], *skipped):
                raise BotLifecycleError("bot handoff outcome is unavailable", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"])
            if marker == ["handoff-saved"]:
                try:
                    wait_for_handoff(spec.bot_dir, requested=request["requested_ns"],
                                     native_result=request["native_result"], target=entry["target"])
                except BotLifecycleError as exc:
                    raise BotLifecycleError("fresh bot handoff file is unverified", effect_attempted=True,
                                            unavailable=True, release_id=_release_id(release), **_provenance(release),
                                            target=entry["target"]) from exc
                return _bot_result(BotHandoffResult, release, destination.fleet.name, bot, _release_id(release),
                                        entry["target"], "saved", "fresh_file_verified")
            # A definitive no-send result is not an outstanding request.
            (spec.bot_dir / "data" / _HANDOFF_REQUEST).unlink()
            return _bot_result(BotHandoffResult, release, destination.fleet.name, bot, _release_id(release),
                                    entry["target"], "skipped", marker[0].split(":", 1)[1])


def check_handoff(*, root: Path, fleet: str | None, bot: str) -> BotHandoffCheck:
    """Read the door's last handoff request against the handoff file (#990).

    It sends nothing and takes neither mutation admission nor the lifecycle
    lock, so it answers while another bot operation runs.
    """
    if not isinstance(bot, str) or not bot or Path(bot).name != bot or bot in {".", ".."}:
        raise BotLifecycleError("supply one exact bot ID")
    destination, origin = resolve_operation_scope(root=root, fleet=fleet)
    if origin is not None and (origin.fleet.name != destination.fleet.name
                               or origin.bot_id == bot
                               or origin.bot_id != destination.fleet.manager):
        raise BotLifecycleError("only the selected fleet manager may check another bot's handoff")
    if bot not in destination.fleet.bots:
        raise BotLifecycleError("bot is not declared in the selected active fleet")
    package = destination.paths.package
    if package.installation is None:
        raise BotLifecycleError("handoff inspection requires a managed Git installation")
    from .active_config import read_current_config
    snapshot = read_current_config(root, installation=package.installation, revision=package.source_revision)
    return BotHandoffCheck(destination.fleet.name, bot, None,
                           *handoff_state(destination.paths.bot_runtime(bot)),
                           source_revision=snapshot["revision"], config_fingerprint=snapshot["fingerprint"])


def control_bot(*, root: Path, fleet: str | None, bot: str, control: str,
                identity: RuntimeIdentity | None = None,
                adapter: Adapter | None = None) -> BotControlResult:
    """Submit one explicit control to an exact selected private session."""
    if control not in {"interrupt", "compact"}:
        raise BotLifecycleError("unsupported bot control")
    if not isinstance(bot, str) or not bot or Path(bot).name != bot or bot in {".", ".."}:
        raise BotLifecycleError("supply one exact bot ID")
    identity = identity or RuntimeIdentity.current()
    with mutation_admission(root, identity=identity,
                            expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        with _operation_lock(root):
            destination, origin = resolve_operation_scope(root=root, fleet=fleet)
            if origin is not None and (origin.fleet.name != destination.fleet.name
                                       or origin.bot_id == bot
                                       or origin.bot_id != destination.fleet.manager):
                raise BotLifecycleError("only the selected fleet manager may control another bot")
            if bot not in destination.fleet.bots:
                raise BotLifecycleError("bot is not declared in the selected active fleet")
            binding = _current_bot_binding(root, release, destination, bot, adapter)
            adapter, manager = binding.adapter, binding.manager
            declaration, entry, declarations, spec = (binding.declaration, binding.entry,
                                                       binding.declarations, binding.spec)
            binding.verify_configuration()
            unit = _observed(root, declarations, adapter, entry["target"], Path(entry["installed"]))
            if not unit.installed:
                raise BotLifecycleError("bot is not enrolled in its selected native unit")
            session = _native(adapter, "svc_bot_session_observe", spec.bot_dir, spec.label,
                              spec.environment["TMUX_TMPDIR"], entry["installed"], entry["target"])
            if session != "ready":
                raise BotLifecycleError("private bot session is not ready", unavailable=True)
            try:
                outcome = adapter.call("svc_bot_control_exact", spec.bot_dir, spec.label,
                                       spec.environment["TMUX_TMPDIR"], control, timeout=45)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BotLifecycleError("bot control outcome is unknown", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"]) from exc
            if outcome.returncode or outcome.stdout.strip() != "control-submitted":
                raise BotLifecycleError("bot control outcome is unknown", effect_attempted=True,
                                        unavailable=True, release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"])
            return _bot_result(BotControlResult, release, destination.fleet.name, bot, _release_id(release),
                                    entry["target"], control, "submitted")


def _record_lifecycle(root: Path, fleet: str, bot: str, event: str, *, by: str,
                      reason: str | None, request_id: str, changed: bool) -> str:
    """The plane's audit row for a stop or a start (#2243): "committed", or "degraded"
    when the plane did not take it. Never raises: a stop must not depend on the plane,
    and the sweep reads the local stop record, not this row."""
    raw = fleet_event_request(event, fleet=fleet, subject_kind="actor", subject=f"bot:{fleet}/{bot}",
                              source="lifecycle", key=request_id,
                              data={"by": by, "reason": reason, "request_id": request_id,
                                    "changed": changed})
    try:
        recorded = emit_batch(root, [raw], require_commit=True)[0]
    except Exception:
        return "degraded"
    return "committed" if recorded.status in {"committed", "duplicate"} else "degraded"


def _started(root: Path, fleet: str, bot: str, bot_dir: Path, by: str, request_id: str, *,
             changed: bool) -> tuple[str | None, bool | None]:
    """A start that left the bot running: the unit is back, so the stop record goes,
    and a start that changed something or cleared a record leaves a bot_started row.
    Returns the row's recording, and True when the record could not be removed."""
    try:
        cleared, kept = clear_stop(bot_dir), None
    except OSError:
        cleared, kept = False, True
    if not (changed or cleared):
        return None, kept
    return _record_lifecycle(root, fleet, bot, "bot_started", by=by, reason=None,
                             request_id=request_id, changed=changed), kept


def _confirm_stopped(adapter, installed, target, socket_path, *, effect_attempted=False,
                     settle_s=0):
    state = _native(adapter, "svc_inventory_state", installed, target)
    if state not in {"not-found not-found inactive", "unchanged unloaded inactive"}:
        raise BotLifecycleError("bot de-enrollment is not established",
                                effect_attempted=effect_attempted)
    try:
        assert_quiescent(adapter, installed_file=installed, target=target, socket_path=socket_path,
                         settle_s=settle_s)
    except (RuntimeEvidenceError, OSError) as exc:
        raise BotLifecycleError("bot native/session quiescence is unavailable",
                                effect_attempted=effect_attempted, unavailable=True) from exc


def _confirm_running(adapter, installed, target, manager, *, session_ready=False,
                     effect_attempted=False):
    state = _native(adapter, "svc_inventory_state", installed, target)
    if (state != ("unchanged loaded active" if manager == "Darwin" else "enabled loaded active")
            and not (manager == "Darwin" and state == "unchanged loaded inactive"
                     and session_ready)):
        raise BotLifecycleError("bot native enrollment is not active",
                                effect_attempted=effect_attempted)


def set_bot_running(*, root: Path, fleet: str | None, bot: str, running: bool,
                    restart: bool = False, ceiling: int | None = None,
                    identity: RuntimeIdentity | None = None,
                    adapter: Adapter | None = None, _self_child: bool = False,
                    _on_lock=None, reason: str | None = None,
                    _expected_provenance=None) -> BotLifecycleResult:
    """Start, stop or restart one exact selected bot; prove native effects."""
    if restart and not running:
        raise BotLifecycleError("restart requires a running target")
    if reason is not None and running:
        raise BotLifecycleError("a reason is recorded only for a stop")
    try:
        reason = check_reason(reason)
    except StopRecordError as exc:
        raise BotLifecycleError(str(exc)) from exc
    if ceiling is not None and (not restart or isinstance(ceiling, bool)
                                or not isinstance(ceiling, int) or ceiling <= 0):
        raise BotLifecycleError("restart ceiling must be a positive integer")
    if not isinstance(bot, str) or not bot or Path(bot).name != bot or bot in {".", ".."}:
        raise BotLifecycleError("supply one exact bot ID")
    if restart and not _self_child:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        if origin is not None and origin.bot_id == bot:
            if origin.fleet.name != destination.fleet.name or bot not in destination.fleet.bots:
                raise BotLifecycleError("self restart target differs from generated origin")
            return _schedule_self_restart(root=root, fleet=destination.fleet.name,
                                          bot=bot, ceiling=ceiling,
                                          bot_dir=destination.paths.bot_runtime(bot))
    identity = identity or RuntimeIdentity.current()
    with mutation_admission(root, identity=identity,
                            expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        with _operation_lock(root):
            if (_expected_provenance is not None
                    and (_release_id(release), _provenance(release)) != _expected_provenance):
                raise BotLifecycleError("self restart configuration changed before admission")
            if _on_lock is not None:
                _on_lock()
            destination, origin = resolve_operation_scope(root=root, fleet=fleet)
            if origin is not None and (origin.fleet.name != destination.fleet.name
                                       or (bot == origin.bot_id and not (restart and _self_child))
                                       or (bot != origin.bot_id and origin.bot_id != destination.fleet.manager)):
                raise BotLifecycleError("only the selected fleet manager may operate another bot")
            if _self_child and (not restart or origin is None or origin.bot_id != bot):
                raise BotLifecycleError("self restart lost its exact generated origin")
            if bot not in destination.fleet.bots:
                raise BotLifecycleError("bot is not declared in the selected active fleet")
            binding = _current_bot_binding(root, release, destination, bot, adapter)
            adapter, manager = binding.adapter, binding.manager
            declaration, entry, declarations, spec = (binding.declaration, binding.entry,
                                                       binding.declarations, binding.spec)
            installed = Path(entry["installed"])
            socket = Path(spec.environment["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / spec.label
            unit = _observed(root, declarations, adapter, entry["target"], installed)
            by = (f"bot:{origin.fleet.name}/{origin.bot_id}" if origin is not None
                  else "operator")
            request_id = str(uuid4())
            if not running:
                # #2243: the stop door always records that the stop was meant, and
                # first, so no sweep sees the unit file gone without the record.
                # fleet-pulse reads this record, never the plane row below, so a
                # stop made while the plane is down stays quiet. A record that
                # cannot be written does not hold the stop up.
                try:
                    write_stop(spec.bot_dir, by=by, reason=reason, request_id=request_id)
                    recorded = True
                except (StopRecordError, OSError):
                    recorded = False
                if unit.installed:
                    effect_begun = False
                    try:
                        _native(adapter, "svc_bot_disenroll_exact", declaration.source, installed,
                                entry["target"], spec.bot_dir, spec.label,
                                spec.environment["TMUX_TMPDIR"])
                        effect_begun = True
                        _confirm_stopped(adapter, installed, entry["target"], socket,
                                         effect_attempted=True, settle_s=_STOP_SETTLE_S)
                        after = _observed(root, declarations, adapter, entry["target"], installed)
                        if after.installed:
                            raise BotLifecycleError("bot unit remains installed")
                    except (BotLifecycleError, RuntimeEvidenceError, InventoryError, OSError) as exc:
                        attempted = effect_begun or (isinstance(exc, BotLifecycleError)
                                                     and exc.effect_attempted)
                        if not attempted:
                            # refused before any effect: nothing was stopped
                            try:
                                clear_stop(spec.bot_dir)
                            except OSError:
                                pass
                        stage = str(exc) if isinstance(exc, BotLifecycleError) else type(exc).__name__
                        raise BotLifecycleError(f"bot stop refused or proof is incomplete: {stage}",
                                                effect_attempted=attempted,
                                                unavailable=True,
                                                release_id=_release_id(release), **_provenance(release),
                                                target=entry["target"]) from exc
                else:
                    _confirm_stopped(adapter, installed, entry["target"], socket,
                                     settle_s=_STOP_SETTLE_S)
                row = _record_lifecycle(root, destination.fleet.name, bot, "bot_stopped", by=by,
                                        reason=reason, request_id=request_id,
                                        changed=bool(unit.installed))
                return _bot_result(BotLifecycleResult, release, destination.fleet.name, bot, _release_id(release),
                                          entry["target"], "stopped", bool(unit.installed), "not_running",
                                          recording=row if recorded else "degraded")
            binding.verify_configuration()
            if restart:
                # A deliberate stop removed supervision. Restart may bounce only
                # an already enrolled exact unit; bot start owns re-enrollment.
                if not unit.installed:
                    raise BotLifecycleError("bot is de-enrolled; use bot start",
                                            skip_reason="de_enrolled",
                                            release_id=_release_id(release), **_provenance(release),
                                            target=entry["target"])
                properties = dict(unit.properties)
                if (manager == "Linux" and properties.get("UnitFileState") == "disabled"
                        or manager == "Darwin" and properties.get("EnabledState") == "disabled"):
                    raise BotLifecycleError("bot native unit is disabled; use bot start",
                                            skip_reason="disabled",
                                            release_id=_release_id(release), **_provenance(release),
                                            target=entry["target"])
                native_state = properties.get("ActiveState")
                if native_state in {"failed", "activating"} and manager == "Linux":
                    raise BotLifecycleError(f"bot native state is {native_state}; use bot stop, then bot start")
                if native_state not in {"active", "inactive"}:
                    raise BotLifecycleError(f"bot native state is {native_state or 'unknown'}; wait or inspect the exact unit",
                                            unavailable=True)
            handoff = "not_applicable"
            if restart:
                session = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                  spec.label, spec.environment["TMUX_TMPDIR"])
                if session not in {"ready", "absent"}:
                    raise BotLifecycleError("current bot session observation is indeterminate",
                                            unavailable=True)
                if _self_child:
                    if session != "ready":
                        raise BotLifecycleError("self restart lost its private live session",
                                                unavailable=True)
                    _planned_restart_handoff(spec.bot_dir, spec.label,
                                             spec.environment["TMUX_TMPDIR"], adapter)
                    handoff = "native_session"
                elif session == "ready":
                    # A planned live bounce must retain context before the
                    # existing native restart can terminate its private pane.
                    try:
                        _planned_restart_handoff(spec.bot_dir, spec.label,
                                                 spec.environment["TMUX_TMPDIR"], adapter)
                    except BotLifecycleError as exc:
                        raise BotLifecycleError(str(exc), unavailable=True) from exc
                    handoff = "native_session"
                # An absent/dead session cannot produce a fresh checkpoint.
                # Its native recovery restores whatever was durably saved;
                # this branch makes no claim that new context was captured.
            if unit.installed and not restart:
                props = dict(unit.properties)
                if (props.get("ActiveState") == "active"
                        or manager == "Darwin" and props.get("ActiveState") == "inactive"):
                    session = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                      spec.label, spec.environment["TMUX_TMPDIR"])
                    if session == "ready":
                        _confirm_running(adapter, installed, entry["target"], manager,
                                         session_ready=True)
                        recording, kept = _started(root, destination.fleet.name, bot, spec.bot_dir,
                                                   by, request_id, changed=False)
                        return _bot_result(BotLifecycleResult, release, destination.fleet.name, bot, _release_id(release),
                                                  entry["target"], "running", False,
                                                  "current_session_ready",
                                                  recording=recording, stop_record_kept=kept)
                    if session != "absent":
                        raise BotLifecycleError("current bot session readiness is indeterminate; "
                                                "inspect the session or explicitly restart",
                                                unavailable=True)
                elif (props.get("ActiveState") == "inactive"
                      or manager == "Linux" and props.get("ActiveState") == "failed"):
                    try:
                        assert_quiescent(adapter, installed_file=installed,
                                         target=entry["target"], socket_path=socket)
                    except (RuntimeEvidenceError, OSError) as exc:
                        raise BotLifecycleError("stopped bot session cannot be proved quiet",
                                                unavailable=True) from exc
                elif manager == "Linux" and props.get("ActiveState") == "activating":
                    raise BotLifecycleError(f"bot native state is {props['ActiveState']}; use bot stop, then bot start")
                else:
                    raise BotLifecycleError(f"bot native state is {props.get('ActiveState') or 'unknown'}; wait or inspect the exact unit",
                                            unavailable=True)
            elif not unit.installed:
                _confirm_stopped(adapter, installed, entry["target"], socket)
            # systemctl restart and enable --now block through the unit's
            # ExecStartPre stagger, so the enroll call gets that delay on top
            # of its own 30 s (#2087). Read before the fence: no effect yet.
            enroll_budget = unit_start_budget(declaration.source, declaration.source.read_bytes())
            fence_args = (root, spec.bot_dir) if ceiling is None else (root, spec.bot_dir, str(ceiling))
            fence = _native(adapter, "svc_activation_bot_fence", *fence_args).split("\t")
            if len(fence) != 2 or not fence[0].isdigit() or not fence[1]:
                raise BotLifecycleError("bot readiness fence is incomplete")
            try:
                _native(adapter, "svc_bot_enroll_exact", declaration.source, installed, entry["target"],
                        timeout=enroll_budget)
                _observed(root, declarations, adapter, entry["target"], installed)
                readiness = _native(adapter, "svc_activation_bot_ready", root, spec.bot_dir,
                                    fence[0], fence[1], spec.environment["TMUX_TMPDIR"], timeout=int(fence[0]) + 30)
                if readiness not in {"bridge-ready", "session-ready"}:
                    raise BotLifecycleError("bot native readiness outcome is unknown",
                                            effect_attempted=True, unavailable=True)
                session_ready = False
                if manager == "Darwin":
                    session_ready = _native(adapter, "svc_bot_session_observe", spec.bot_dir,
                                            spec.label, spec.environment["TMUX_TMPDIR"]) == "ready"
                    if not session_ready:
                        raise BotLifecycleError("current bot session readiness is indeterminate",
                                                effect_attempted=True, unavailable=True)
                _confirm_running(adapter, installed, entry["target"], manager,
                                 session_ready=session_ready, effect_attempted=True)
            except (BotLifecycleError, RuntimeEvidenceError, InventoryError, OSError) as exc:
                raise BotLifecycleError("bot restart effect or readiness proof is incomplete" if restart
                                        else "bot start effect or readiness proof is incomplete",
                                        effect_attempted=True,
                                        release_id=_release_id(release), **_provenance(release),
                                        target=entry["target"]) from exc
            recording, kept = (None, None) if restart else _started(
                root, destination.fleet.name, bot, spec.bot_dir, by, request_id, changed=True)
            return _bot_result(BotLifecycleResult, release, destination.fleet.name, bot, _release_id(release),
                                      entry["target"], "running", True,
                                      readiness.replace("-", "_"), handoff,
                                      recording=recording, stop_record_kept=kept)
