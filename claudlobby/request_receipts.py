"""Durable operational observations, never task state or a transport replay queue.

Hold this request lock before any task lock; nested messaging reuses the store.
Prepare stable identities/IDs before effects, then persist an unknown stage before
acting. Callers own exact-fact reconciliation, domain checks and transport proof.
A missing receipt says nothing about prior best-effort communication. Persistence
errors propagate: O1 may disclose request_persisted=false, never fabricate success.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from .plane.ids import ID_PATTERNS

FORMAT_VERSION = 1


class ReceiptError(ValueError):
    """Invalid, unsupported or unsafe request history; no effects are authorized."""


class ReceiptConflict(ReceiptError):
    """The UUID or a completed stage cannot be reused for this operation."""


class ReceiptBusy(ReceiptError):
    """Another caller holds this request's lock."""


def semantic_digest(semantic: dict, *, presentation: dict | None = None) -> str:
    """Hash caller-normalized semantic arguments; presentation/retry flags are separate.

    Include file bytes (supported directly) or their content digest, never merely
    their pathname. Nothing supplied here is retained in the receipt as plaintext.
    """
    def normalize(value):
        if isinstance(value, bytes):
            return ["bytes", hashlib.sha256(value).hexdigest()]
        if isinstance(value, dict) and all(type(k) is str for k in value):
            return ["object", [[k, normalize(value[k])] for k in sorted(value)]]
        if isinstance(value, (list, tuple)):
            return ["array", [normalize(v) for v in value]]
        if value is None or type(value) in (str, int, bool, float):
            return [type(value).__name__, value]
        raise ReceiptError("semantic arguments must be JSON values or bytes")
    return hashlib.sha256(json.dumps(normalize(semantic), ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class ExpectedFact:
    event_id: str
    family: str
    projection_sha256: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class StagePlan:
    kind: str  # recording, delivery, notification; index identifies each actual stage
    facts: tuple[ExpectedFact, ...] = ()


@dataclass(frozen=True)
class RequestIntent:
    operation: str
    operation_version: int
    host_uid: str
    fleet_uid: str
    caller_uid: str
    recipient_uid: str | None
    semantic_sha256: str
    stages: tuple[StagePlan, ...]
    task_id: str | None = None
    assignment_id: str | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class StageOutcome:
    status: str = "prepared"
    attempt: int = 0


@dataclass(frozen=True)
class RequestReceipt:
    request_id: str
    intent: RequestIntent
    stages: tuple[StageOutcome, ...]
    attempt: int = 0
    format_version: int = FORMAT_VERSION


_OPERATIONS = frozenset(
    "task.admit task.assign task.withdraw task.reassign task.escalate task.nudge task.recheck "
    "assignment.deliver assignment.accept assignment.progress assignment.block assignment.return "
    "assignment.complete assignment.fail message.send message.reply fleet.reports.submit fleet.reports.ack".split())
_STATUSES = {"recording": {"prepared", "unknown", "committed", "unrecorded"},
             "delivery": {"prepared", "unknown", "received", "submitted", "failed"},
             "notification": {"prepared", "unknown", "received", "submitted", "failed"}}
# This operation codec supports task/message facts and report acknowledgements.
# These are wire event_type / ingest_ledger.family values, never SQL table names;
# physical storage mapping remains owned by ingest.CONSTRUCT_TABLES.
FACT_FAMILIES = frozenset({"communication", "transmission", "work_item", "assignment", "task", "system"})


def _id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise ReceiptError("invalid receipt identity")


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReceiptError("invalid receipt digest")


def _validate(receipt):
    if type(receipt.format_version) is not int or receipt.format_version != FORMAT_VERSION:
        raise ReceiptError("unsupported request receipt format")
    if str(UUID(receipt.request_id)) != receipt.request_id:
        raise ReceiptError("request ID must be a canonical UUID")
    intent = receipt.intent
    if intent.operation not in _OPERATIONS or type(intent.operation_version) is not int or intent.operation_version != 1:
        raise ReceiptError("unsupported request operation/version")
    for field, kind in (("host_uid", "host"), ("fleet_uid", "fleet"), ("caller_uid", "actor")):
        _id(getattr(intent, field), kind)
    for field, kind in (("recipient_uid", "actor"), ("task_id", "work_item"),
                        ("assignment_id", "assignment"), ("message_id", "msg")):
        if getattr(intent, field) is not None:
            _id(getattr(intent, field), kind)
    _sha(intent.semantic_sha256)
    if type(receipt.attempt) is not int or receipt.attempt < 0 or len(intent.stages) != len(receipt.stages):
        raise ReceiptError("invalid receipt attempt/stages")
    seen = set()
    for plan, outcome in zip(intent.stages, receipt.stages):
        if plan.kind not in _STATUSES or outcome.status not in _STATUSES[plan.kind]:
            raise ReceiptError("invalid receipt stage outcome")
        if plan.kind == "recording" and not plan.facts:
            raise ReceiptError("recording stage requires frozen expected facts")
        if type(outcome.attempt) is not int or not 0 <= outcome.attempt <= receipt.attempt:
            raise ReceiptError("invalid stage attempt")
        if (outcome.status == "prepared") != (outcome.attempt == 0):
            raise ReceiptError("stage outcome lacks an attempt")
        for fact in plan.facts:
            _id(fact.event_id, "event")
            _sha(fact.projection_sha256)
            if fact.family not in FACT_FAMILIES or fact.event_id in seen:
                raise ReceiptError("invalid or repeated expected fact")
            if (len(set(fact.fields)) != len(fact.fields)
                    or any(not re.fullmatch(r"[a-z][a-z0-9_]*", field) for field in fact.fields)):
                raise ReceiptError("invalid expected fact fields")
            seen.add(fact.event_id)


def _decode(raw):
    try:
        intent = raw["intent"]
        intent = RequestIntent(**{**intent, "stages": tuple(
            StagePlan(**{**s, "facts": tuple(ExpectedFact(**{**f, "fields": tuple(f["fields"])})
                                             for f in s["facts"])})
            for s in intent["stages"])})
        receipt = RequestReceipt(**{**raw, "intent": intent,
                                   "stages": tuple(StageOutcome(**s) for s in raw["stages"])})
        _validate(receipt)
        if json.loads(json.dumps(asdict(receipt))) != raw:
            raise ReceiptError("request receipt fields are incomplete")
        return receipt
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReceiptError("invalid or unsupported request receipt") from exc


def decode_receipt(raw, *, request_id: str, fleet_uid: str) -> RequestReceipt:
    """Decode retained metadata and bind it to its scoped filename; no IO."""
    receipt = _decode(raw)
    if receipt.request_id != request_id or receipt.intent.fleet_uid != fleet_uid:
        raise ReceiptConflict("receipt identity differs from its scoped path")
    return receipt


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ReceiptError("receipt node must be an owned private regular file")
    return info.st_dev, info.st_ino


_LOCK_TOKEN = object()


class RequestStore:
    """Only usable inside locked_request; share it instead of reacquiring its lock."""

    def __init__(self, path, fleet_uid, fd, token):
        if token is not _LOCK_TOKEN:
            raise ReceiptError("use locked_request to acquire the request lock")
        self.path, self.fleet_uid, self._fd = path, fleet_uid, fd
        self._pid, self._inode = os.getpid(), _regular(fd)
        self._held = True

    def assert_locked(self):
        if not self._held or self._pid != os.getpid():
            raise ReceiptError("request lock is not held")
        info = self.path.with_suffix(".lock").lstat()
        if self._inode != _regular(self._fd) or self._inode != (info.st_dev, info.st_ino):
            raise ReceiptError("request lock was replaced")

    def load(self) -> RequestReceipt | None:
        """None means no retained receipt, NOT that a previous send never happened."""
        self.assert_locked()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            _regular(stream.fileno())
            try:
                raw = json.load(stream)
            except (ValueError, UnicodeError) as exc:
                raise ReceiptError("invalid request receipt") from exc
        return decode_receipt(raw, request_id=self.path.stem, fleet_uid=self.fleet_uid)

    def _save(self, receipt):
        self.assert_locked()
        _validate(receipt)
        temporary = self.path.with_name(f".{self.path.stem}.{uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write((json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":")) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            _sync(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return receipt

    def prepare(self, intent: RequestIntent) -> RequestReceipt:
        candidate = RequestReceipt(self.path.stem, intent, tuple(StageOutcome() for _ in intent.stages))
        _validate(candidate)
        if intent.fleet_uid != self.fleet_uid:
            raise ReceiptConflict("request belongs to a different fleet")
        previous = self.load()
        if previous is not None:
            if previous.intent != intent:
                raise ReceiptConflict("request UUID already has different semantics or identities")
            return previous
        return self._save(candidate)

    def begin_attempt(self) -> RequestReceipt:
        """An explicit execution attempt only; inspection/replay lookup never increments."""
        receipt = self._required()
        return self._save(replace(receipt, attempt=receipt.attempt + 1))

    def _required(self):
        receipt = self.load()
        if receipt is None:
            raise ReceiptError("prepare a durable request before effects")
        return receipt

    def stage(self, index: int, *, retry_uncertain: bool = False) -> RequestReceipt:
        """Persist unknown BEFORE an effect. This method never performs/resends it."""
        receipt = self._required()
        plan, old = self._stage(receipt, index)
        allowed = old.status == "prepared" or (plan.kind == "recording" and old.status == "unrecorded")
        allowed |= plan.kind != "recording" and old.status in {"unknown", "failed"} and retry_uncertain
        if not allowed or receipt.attempt <= old.attempt:
            raise ReceiptConflict("stage requires reconciliation or an explicit uncertain retry in a new attempt")
        return self._update(receipt, index, StageOutcome("unknown", receipt.attempt))

    def outcome(self, index: int, status: str) -> RequestReceipt:
        """Persist caller-proven observations; duplicate ingest alone is not proof.

        Recording is unrecorded only when definitely absent. Unavailable proof,
        including a retained ledger row with pruned payload, remains unknown.
        """
        receipt = self._required()
        plan, old = self._stage(receipt, index)
        if status not in _STATUSES[plan.kind] - {"prepared"}:
            raise ReceiptError("invalid observed outcome")
        if old.status != status and old.status != "unknown" and not (old.status == "submitted" and status == "received"):
            raise ReceiptConflict("observed outcome would overwrite a completed stage")
        return self._update(receipt, index, replace(old, status=status))

    @staticmethod
    def _stage(receipt, index):
        if type(index) is not int or not 0 <= index < len(receipt.stages):
            raise ReceiptError("unknown request stage")
        return receipt.intent.stages[index], receipt.stages[index]

    def _update(self, receipt, index, value):
        return self._save(replace(receipt, stages=receipt.stages[:index] + (value,) + receipt.stages[index + 1:]))


@contextmanager
def locked_request(root: Path, fleet_uid: str, request_id: str):
    """Nonblocking, process-owned per-request flock; acquire before any task lock."""
    _id(fleet_uid, "fleet")
    if str(UUID(request_id)) != request_id:
        raise ReceiptError("request ID must be a canonical UUID")
    directory = Path(root).resolve(strict=True)
    for part in ("state", "requests", fleet_uid):
        child = directory / part
        if child.is_symlink():
            raise ReceiptError("request directory is redirected")
        child.mkdir(mode=0o700, exist_ok=True)
        _sync(directory)
        directory = child
    path = directory / f"{request_id}.json"
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    store = None
    try:
        _regular(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReceiptBusy("request is already being processed") from exc
        store = RequestStore(path, fleet_uid, fd, _LOCK_TOKEN)
        yield store
    finally:
        if store is not None:
            store._held = False
        os.close(fd)
