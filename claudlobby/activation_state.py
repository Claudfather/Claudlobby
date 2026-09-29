"""Durable state and single-host lock for manual release activation.

This private store does not prove quiescence or perform supervision/migration.
Those owners must reconcile each started step after interruption before marking
it complete. A selected release is not operational until the final active mark;
ordinary mutation admission must check both selection and activation status.
No database snapshot is restored here, including during rollback.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .config_plan import ConfigPlan, read_plan
from .releases import read_release


STEPS = (
    "producers_paused", "sessions_handed_off", "sessions_quiesced",
    # Final queue/DB inventory follows ingest shutdown: draining, the shutdown
    # receipt and SQLite's last-connection checkpoint can still change WAL.
    "ingest_quiesced", "queues_classified", "backup_saved", "migration_applied",
    "selection_switched", "configuration_applied", "ingest_started",
    "bots_started", "verified", "producers_resumed",
)
ROLLBACK_STEPS = (
    "producers_paused", "sessions_quiesced", "ingest_quiesced",
    "compatibility_verified", "candidate_units_removed", "configuration_restored",
    "selection_restored", "ingest_started", "bots_started", "verified",
    "producers_resumed",
)


class ActivationError(RuntimeError):
    """Another activation or an unproven recovery state; keep the host paused."""


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: Path, value) -> None:
    fd, name = tempfile.mkstemp(prefix=".activation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _state(root: Path) -> Path:
    path = Path(root).expanduser().resolve() / "state"
    if path.resolve() != path:
        raise ActivationError("activation state directory is redirected")
    return path


def _record_path(root: Path, activation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", activation_id):
        raise ActivationError("supply an explicit simple activation id")
    path = _state(root) / "activations" / activation_id / "activation.json"
    if path.resolve() != path:
        raise ActivationError("activation record is redirected")
    return path


@dataclass(frozen=True)
class ActivationRecord:
    activation_id: str
    root: Path
    body: dict

    @property
    def status(self) -> str:
        return self.body["status"]


def read_activation(root: Path, activation_id: str) -> ActivationRecord:
    path = _record_path(root, activation_id)
    try:
        raw = json.loads(path.read_bytes())
        body = raw["record"]
        if (raw["sha256"] != _digest(body) or body["schema"] != 1
                or body["activation_id"] != activation_id
                or body["root"] != str(_state(root).parent)):
            raise ActivationError("activation record identity changed")
        return ActivationRecord(activation_id, _state(root).parent, body)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ActivationError(f"cannot read activation {activation_id}: {exc}") from exc


def read_selection(root: Path) -> dict | None:
    path = _state(root) / "selected-release.json"
    if path.is_symlink():
        raise ActivationError("release selection is redirected")
    try:
        result = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ActivationError(f"cannot read release selection: {exc}") from exc
    if (not isinstance(result, dict)
            or set(result) != {"schema", "activation_id", "release_id", "plan_id"}
            or result["schema"] != 1):
        raise ActivationError("invalid release selection")
    return result


_LOCK_TOKEN = object()


class ActivationStore:
    """Only valid inside locked_activation; never cache it across that scope."""

    def __init__(self, root: Path, fd: int, token):
        if token is not _LOCK_TOKEN:
            raise ActivationError("use locked_activation to acquire the host lock")
        self.root = root
        self.held = True
        self._fd = fd
        self._pid = os.getpid()
        info = os.fstat(fd)
        self._inode = (info.st_dev, info.st_ino)

    def assert_locked(self) -> None:
        """Refuse an expired, inherited or replaced lock handle."""
        if not self.held or self._pid != os.getpid():
            raise ActivationError("host activation lock is not held")
        try:
            descriptor = os.fstat(self._fd)
            path = (_state(self.root) / "activation.lock").lstat()
        except OSError as exc:
            raise ActivationError("host activation lock is not held") from exc
        if (self._inode != (descriptor.st_dev, descriptor.st_ino)
                or self._inode != (path.st_dev, path.st_ino)):
            raise ActivationError("host activation lock was replaced")

    def _save(self, record: ActivationRecord) -> ActivationRecord:
        self.assert_locked()
        if record.root != self.root:
            raise ActivationError("activation belongs to a different host")
        path = _record_path(self.root, record.activation_id)
        _write(path, {"record": record.body, "sha256": _digest(record.body)})
        return read_activation(self.root, record.activation_id)

    def prepare(self, activation_id: str, plan: ConfigPlan, *,
                recovery_release_id: str, enrollment_digest: str,
                source_release_id: str | None = None,
                legacy_source: bool = False) -> ActivationRecord:
        self.assert_locked()
        path = _record_path(self.root, activation_id)
        if plan.data_root != self.root or read_plan(self.root, plan.plan_id) != plan:
            raise ActivationError("activation plan belongs to different or changed host state")
        if not re.fullmatch(r"[0-9a-f]{64}", enrollment_digest):
            raise ActivationError("verified enrollment manifest digest is required")
        existing = read_activation(self.root, activation_id) if path.exists() else None
        previous = existing.body["previous_selection"] if existing else read_selection(self.root)
        if legacy_source:
            if previous is not None or source_release_id is not None:
                raise ActivationError("unsealed first adoption cannot claim a selected source release")
        else:
            source_release_id = source_release_id or (
                existing.body["intent"]["source_release_id"] if existing else
                previous["release_id"] if previous else recovery_release_id)
            if previous is not None and source_release_id != previous["release_id"]:
                raise ActivationError("activation source differs from the selected release")
        intent = {"plan_id": plan.plan_id, "release_id": plan.release_id,
                  "source_release_id": source_release_id,
                  "recovery_release_id": recovery_release_id,
                  "enrollment_digest": enrollment_digest}
        if legacy_source:
            intent["source_kind"] = "legacy-unsealed"
        if existing is not None:
            if existing.body["intent"] != intent:
                raise ActivationError("activation id already belongs to a different intent")
            return existing
        for prior in (_state(self.root) / "activations").glob("*/activation.json"):
            record = read_activation(self.root, prior.parent.name)
            if record.status not in {"active", "rolled_back"}:
                raise ActivationError(f"unfinished activation must be recovered: {record.activation_id}")
        plan.check_fresh()
        read_release(self.root, plan.release_id)
        if not legacy_source:
            read_release(self.root, source_release_id)
        read_release(self.root, recovery_release_id)
        body = {"schema": 1, "activation_id": activation_id, "root": str(self.root),
                "intent": intent, "previous_selection": previous, "status": "prepared",
                "completed": [], "pending": None, "evidence": {}}
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        _sync(path.parent.parent)
        return self._save(ActivationRecord(activation_id, self.root, body))

    def cancel_prepared(self, activation_id: str) -> ActivationRecord:
        """End an intent that never prepared a journal or began a step.

        This is a recorded cancellation, not rollback of any effect. Once a
        journal or step exists, the ordinary recovery owners must take over.
        """
        self.assert_locked()
        record = read_activation(self.root, activation_id)
        body = record.body
        path = _record_path(self.root, activation_id)
        if (record.status != "prepared" or body["pending"] is not None
                or body["completed"] or body["evidence"]
                or read_selection(self.root) != body["previous_selection"]
                or set(path.parent.iterdir()) != {path}):
            raise ActivationError("prepared activation has effects or changed selection; explicit recovery required")
        from .activation_units import PHASES, journal_id as unit_journal_id
        from .activation_enrollment import journal_id as enrollment_journal_id
        siblings = [unit_journal_id(activation_id, phase) for phase in PHASES]
        siblings += [enrollment_journal_id(activation_id, phase)
                     for phase in (*PHASES, "directories")]
        if any((path.parent.parent / name).exists() or (path.parent.parent / name).is_symlink()
               for name in siblings):
            raise ActivationError("prepared activation has a unit or enrollment journal; explicit recovery required")
        body["status"] = "rolled_back"
        body["cancellation"] = {"kind": "prepared-before-effects",
                                "selection_sha256": _digest(body["previous_selection"]),
                                "journals": []}
        return self._save(record)

    def begin(self, activation_id: str, step: str) -> ActivationRecord:
        record = read_activation(self.root, activation_id)
        body = record.body
        steps = ROLLBACK_STEPS if body["status"] == "rolling_back" else STEPS
        if (body["status"] in {"active", "rolled_back"}
                or len(body["completed"]) >= len(steps)
                or steps[len(body["completed"])] != step
                or body["pending"] not in (None, step)):
            raise ActivationError("activation step is out of order")
        body["status"] = "rolling_back" if steps is ROLLBACK_STEPS else "activating"
        body["pending"] = step
        return self._save(record)

    def record_identity_bindings(self, activation_id: str, bindings: dict, *, package) -> ActivationRecord:
        """Persist verified IDs once, before the first candidate bot can start."""
        self.assert_locked()
        record = read_activation(self.root, activation_id)
        if record.status != "activating" or record.body["pending"] != "bots_started":
            raise ActivationError("identity bindings require the pending bot-start step")
        from .activation_identity import validate_identity_bindings
        plan = read_plan(self.root, record.body["intent"]["plan_id"])
        validate_identity_bindings(plan, bindings, package=package)
        if "identity_bindings" in record.body:
            if record.body["identity_bindings"] != bindings:
                raise ActivationError("identity bindings were already recorded")
            return record
        record.body["identity_bindings"] = bindings
        return self._save(record)

    def complete(self, activation_id: str, step: str, *, evidence_digest: str) -> ActivationRecord:
        if step in {"selection_switched", "selection_restored"}:
            raise ActivationError("selection completion belongs to the atomic selector")
        return self._complete(activation_id, step, evidence_digest=evidence_digest)

    def _complete(self, activation_id: str, step: str, *, evidence_digest: str) -> ActivationRecord:
        record = read_activation(self.root, activation_id)
        body = record.body
        if body["pending"] != step or not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
            raise ActivationError("only a started step with verified evidence can complete")
        body["completed"].append(step)
        body["evidence"][step] = evidence_digest
        body["pending"] = None
        steps = ROLLBACK_STEPS if body["status"] == "rolling_back" else STEPS
        if tuple(body["completed"]) == steps:
            body["status"] = "rolled_back" if steps is ROLLBACK_STEPS else "active"
        return self._save(record)

    def select(self, activation_id: str) -> ActivationRecord:
        """Reconcile an interrupted atomic switch; leave mutations disabled."""
        self.assert_locked()
        record = read_activation(self.root, activation_id)
        if record.body["pending"] != "selection_switched":
            raise ActivationError("release selection is not the admitted activation step")
        intent = record.body["intent"]
        read_release(self.root, intent["release_id"])
        selected = {"schema": 1, "activation_id": activation_id,
                    "release_id": intent["release_id"], "plan_id": intent["plan_id"]}
        actual = read_selection(self.root)
        if actual not in (record.body["previous_selection"], selected):
            raise ActivationError("release selection changed outside this activation")
        if actual != selected:
            _write(_state(self.root) / "selected-release.json", selected)
        return self._complete(activation_id, "selection_switched", evidence_digest=_digest(selected))

    def begin_rollback(self, activation_id: str) -> ActivationRecord:
        self.assert_locked()
        record = read_activation(self.root, activation_id)
        if record.body["intent"].get("source_kind") == "legacy-unsealed":
            raise ActivationError("unsealed first adoption has no recorded rollback release; repair forward")
        if record.status == "rolling_back":
            return record
        if record.status == "rolled_back":
            raise ActivationError("activation already rolled back")
        record.body["forward"] = {key: record.body[key] for key in
                                  ("status", "completed", "pending", "evidence")}
        record.body.update(status="rolling_back", completed=[], pending=None, evidence={})
        return self._save(record)

    def restore_selection(self, activation_id: str) -> ActivationRecord:
        """Restore the saved complete selection, after compatibility/config checks.

        A distinct recovery build needs its own matching configuration, never a
        pointer substitution over old generated paths. That is a forward repair
        through the coordinator, not this previous-selection rollback primitive.
        """
        self.assert_locked()
        record = read_activation(self.root, activation_id)
        if (record.status != "rolling_back"
                or record.body["pending"] != "selection_restored"):
            raise ActivationError("selection restoration is not the admitted rollback step")
        previous = record.body["previous_selection"]
        intent = record.body["intent"]
        if previous is not None:
            if previous["release_id"] != intent["recovery_release_id"]:
                raise ActivationError("recovery build requires its matching configuration; use forward repair")
            read_release(self.root, previous["release_id"])
        selected = {"schema": 1, "activation_id": activation_id,
                    "release_id": intent["release_id"], "plan_id": intent["plan_id"]}
        if read_selection(self.root) not in (previous, selected):
            raise ActivationError("release selection changed outside this activation")
        path = _state(self.root) / "selected-release.json"
        if previous is None:
            path.unlink(missing_ok=True)
            _sync(path.parent)
        else:
            _write(path, previous)
        return self._complete(activation_id, "selection_restored", evidence_digest=_digest(previous))


@contextmanager
def locked_activation(root: Path):
    """Nonblocking process lock; retain the lock file to avoid inode races."""
    state = _state(root)
    state.mkdir(parents=True, exist_ok=True)
    lock = state / "activation.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    store = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("another host activation holds the lock") from exc
        store = ActivationStore(state.parent, fd, _LOCK_TOKEN)
        yield store
    finally:
        if store is not None:
            store.held = False
        os.close(fd)
