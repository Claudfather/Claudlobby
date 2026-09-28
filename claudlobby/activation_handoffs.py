"""Persist quiescence-time canonical work references in old bot handoffs.

This is a first-adoption write to the exact old bot directories. It does not
create task state, infer ownership from a legacy display ID, or certify that a
session actually produced a handoff. The original session bytes are retained.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .activation_state import ActivationError
from .operation_context import OperationContextError, _host_uid, _identity
from .plane.db import connect_ro, db_file
from .task_audit import TaskAuditError, audit_tasks
from .task_state import TaskStateError, read_tasks


_BEGIN = b"<!-- claudlobby first-adoption canonical references begin -->"
_END = b"<!-- claudlobby first-adoption canonical references end -->"
_REFRESH_BEGIN = b"<!-- claudlobby first-adoption reference refresh begin -->"
_REFRESH_END = b"<!-- claudlobby first-adoption reference refresh end -->"


def _refresh_envelope(bot_dir: Path, timestamp: str) -> bytes:
    """Fresh reference timestamp, explicitly separate from the old session capture."""
    return (f"---\ncwd: {bot_dir}\nlast_updated: {timestamp}\nschema_version: 2\n---\n".encode()
            + _REFRESH_BEGIN + b"\n"
            + b"This timestamp marks a canonical task-reference refresh, not a new session capture.\n"
            + b"The original handoff bytes below are historical and their capture freshness is unverified.\n"
            + b"Read the canonical task and assignment IDs in the section at the end of this file.\n"
            + _REFRESH_END + b"\n\n")


def _owned_directory(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
        info = path.lstat()
        if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()):
            raise ValueError("redirected or foreign directory")
    except (OSError, ValueError) as exc:
        raise ActivationError(f"handoff directory is unavailable or foreign: {path}") from exc


def _handoff_file(bot_dir: Path, root: Path) -> tuple[Path, bytes, str, str | None]:
    _owned_directory(bot_dir, root)
    directory = bot_dir / ".claude"
    if directory.exists() or directory.is_symlink():
        _owned_directory(directory, root)
    else:
        # A missing prior session is disclosed in the new handoff, never
        # presented as successful capture. No directory is written while
        # checking targets; creation belongs to the write phase below.
        return directory / "session.md", b"", "unavailable (no previous session handoff)", None
    path = directory / "session.md"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return path, b"", "unavailable (no previous session handoff)", None
    except OSError as exc:
        raise ActivationError(f"existing handoff cannot be read: {path}") from exc
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ActivationError(f"existing handoff is foreign or not a regular file: {path}")
            previous = stream.read()
    except OSError as exc:
        raise ActivationError(f"existing handoff cannot be read: {path}") from exc
    prior_status = None
    if _BEGIN in previous or _END in previous:
        if previous.count(_BEGIN) != 1 or previous.count(_END) != 1 or not previous.rstrip().endswith(_END):
            raise ActivationError(f"existing canonical handoff section is malformed: {path}")
        try:
            section = previous[previous.index(_BEGIN):]
            prior_status = json.loads(section.split(b"```json\n", 1)[1].split(b"\n```", 1)[0])[
                "previous_handoff"]
        except (IndexError, KeyError, ValueError, UnicodeError) as exc:
            raise ActivationError(f"existing canonical handoff section is unreadable: {path}") from exc
        if prior_status not in ("existing file present (fresh capture unverified)",
                                "unavailable (no previous session handoff)",
                                "unavailable (empty previous session handoff)"):
            raise ActivationError(f"existing canonical handoff status is invalid: {path}")
        prefix = previous[:previous.index(_BEGIN)]
        if prefix.endswith(b"\n\n"):
            previous = prefix[:-2]
        elif prefix:
            raise ActivationError(f"existing canonical handoff separator is malformed: {path}")
        else:
            previous = b""
    refresh_time = None
    if _REFRESH_BEGIN in previous or _REFRESH_END in previous:
        if (previous.count(_REFRESH_BEGIN) != 1 or previous.count(_REFRESH_END) != 1
                or not previous.startswith(b"---\n")):
            raise ActivationError(f"existing canonical reference refresh is malformed: {path}")
        try:
            refresh_time = previous.split(b"\nlast_updated: ", 1)[1].split(b"\n", 1)[0].decode("ascii")
            envelope = _refresh_envelope(bot_dir, refresh_time)
            if not previous.startswith(envelope):
                raise ValueError("unexpected reference refresh envelope")
        except (IndexError, UnicodeError, ValueError) as exc:
            raise ActivationError(f"existing canonical reference refresh is malformed: {path}") from exc
        previous = previous[len(envelope):]
    return path, previous, (prior_status or ("existing file present (fresh capture unverified)" if previous else
                                            "unavailable (empty previous session handoff)")), refresh_time


def _replace(path: Path, content: bytes) -> None:
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".session-a4f-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persist_canonical_handoffs(root: Path, *, roster: dict[str, tuple[str, tuple[str, ...]]],
                               bot_dirs: dict[tuple[str, str], Path],
                               expected_audit: dict) -> dict:
    """Write exact current IDs after old writers stop and before any new bot starts.

    ``roster`` comes from the frozen candidate fleet contexts; ``bot_dirs``
    comes from reviewed, installed old bot declarations. An actor may receive
    an assignment owned by a different fleet; the task retains its own fleet.
    The caller holds the activation lock and owns the stopped-writer proof.
    """
    root = Path(root).resolve()
    if not roster or set(bot_dirs) != {(fleet, bot) for fleet, (_, bots) in roster.items()
                                   for bot in bots}:
        raise ActivationError("old bot handoffs do not cover the reviewed fleet roster")
    if len({str(path) for path in bot_dirs.values()}) != len(bot_dirs):
        raise ActivationError("old bot handoff paths are ambiguous")
    try:
        with closing(connect_ro(db_file(root))) as conn:
            conn.execute("BEGIN")
            audit = audit_tasks(conn)
            if asdict(audit) != expected_audit or audit.blockers:
                raise ActivationError("quiesced task audit changed or has unresolved active links")
            host_uid = _host_uid(root)
            fleet_uids = {}
            actor_by_uid = {}
            for fleet, (_, bots) in sorted(roster.items()):
                fleet_uid = _identity(conn, "fleet", fleet, parent=host_uid)
                fleet_uids[fleet] = fleet_uid
                for bot in bots:
                    alias = f"bot:{fleet}/{bot}"
                    uid = _identity(conn, "actor", alias, parent=fleet_uid)
                    if uid in actor_by_uid:
                        raise ActivationError("old bot actor UID is ambiguous")
                    actor_by_uid[uid] = (fleet, bot)
            if len(set(fleet_uids.values())) != len(fleet_uids):
                raise ActivationError("old fleet UIDs are ambiguous")
            owning = defaultdict(list)
            assigned = defaultdict(list)
            actual = set()
            for fleet, fleet_uid in sorted(fleet_uids.items()):
                snapshot = read_tasks(conn, fleet_uid=fleet_uid)
                if any(issue.blocking for issue in snapshot.issues):
                    raise ActivationError("canonical task state has unresolved active history")
                for task in snapshot.tasks:
                    if not task.open:
                        continue
                    assignment = task.current_assignment
                    aid = assignment.assignment_id if assignment else None
                    actual.add((fleet_uid, task.task_id, aid))
                    assignee = actor_by_uid.get(assignment.assignee_uid) if assignment else None
                    if assignment and assignee is None:
                        raise ActivationError("current assignment has no reviewed old bot actor")
                    row = {"task_id": task.task_id, "assignment_id": aid,
                           "owning_fleet": fleet, "state": task.state,
                           "current_assignee": f"bot:{assignee[0]}/{assignee[1]}" if assignee else None}
                    owning[(fleet, roster[fleet][0])].append(row)
                    if assignee is not None:
                        assigned[assignee].append(row)
            expected = {(row.fleet_uid, row.task_id, row.assignment_id)
                        for row in audit.references if row.active}
            if actual != expected:
                raise ActivationError("canonical current work differs from the quiesced A0 mapping")
    except (OSError, sqlite3.Error, TaskAuditError, TaskStateError, OperationContextError) as exc:
        raise ActivationError("quiesced canonical handoff mapping is unavailable") from exc

    # Validate and render every target before replacing the first file. A
    # failure during replacement still leaves activation pending, not started.
    writes = []
    now = datetime.now(timezone.utc)
    for key, directory in sorted(bot_dirs.items()):
        path, previous, status, prior_refresh = _handoff_file(Path(directory), root)
        refresh = now
        if prior_refresh is not None:
            try:
                previous_time = datetime.strptime(prior_refresh, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ActivationError(f"existing canonical reference refresh has invalid time: {path}") from exc
            if timedelta(0) <= now - previous_time < timedelta(hours=12):
                refresh = previous_time
        timestamp = refresh.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {"schema": 1, "previous_handoff": status,
                   "manager_owned_work": sorted(owning[key], key=lambda row: row["task_id"]),
                   "assigned_to_this_bot": sorted(assigned[key], key=lambda row: (row["owning_fleet"], row["task_id"]))}
        section = (_BEGIN + b"\n```json\n" + json.dumps(payload, sort_keys=True, indent=2).encode()
                   + b"\n```\n" + _END + b"\n")
        writes.append((path, _refresh_envelope(Path(directory), timestamp)
                       + previous + b"\n\n" + section))
    try:
        for path, content in writes:
            _replace(path, content)
    except OSError as exc:
        raise ActivationError("canonical handoff persistence failed; activation remains pending") from exc
    return {"bots": len(writes), "open_tasks": len(actual),
            "active_assignments": sum(aid is not None for _, _, aid in actual),
            "handoffs": [str(path) for path, _ in writes]}
