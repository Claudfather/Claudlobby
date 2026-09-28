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
class NativeDestination:
    """Exact, nonsecret native target retained without resolving mutable config."""

    root: str
    fleet: str
    socket: str
    session: str
    tmux_tmpdir: str


@dataclass(frozen=True)
class MessageRouteBinding:
    """Selection and parties that authorized one immutable message request."""

    activation_id: str
    plan_id: str
    release_id: str
    caller_fleet_uid: str
    peer_fleet_uid: str
    caller_alias: str
    recipient_alias: str
    manager_uid: str
    manager_alias: str
    peer_destination: NativeDestination
    manager_destination: NativeDestination


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
    route: MessageRouteBinding | None = None


@dataclass(frozen=True)
class StageOutcome:
    status: str = "prepared"
    attempt: int = 0


@dataclass(frozen=True)
class TransportObservation:
    """Nonsecret native result; absence after a reserved attempt means unknown."""

    status: str  # submitted, failed, unknown
    wire_sha256: str | None = None
    wire_bytes: int | None = None
    native_returncode: int | None = None


@dataclass(frozen=True)
class MessageAttempt:
    attempt_no: int
    transmission_event_id: str
    observation: TransportObservation | None = None
    transmission_fact: ExpectedFact | None = None
    recording_status: str = "prepared"


@dataclass(frozen=True)
class RequestReceipt:
    request_id: str
    intent: RequestIntent
    stages: tuple[StageOutcome, ...]
    attempt: int = 0
    message_attempts: tuple[MessageAttempt, ...] = ()
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
_NATIVE_STAGES = {
    "message.send": "delivery", "message.reply": "delivery",
    "fleet.reports.submit": "delivery",
    "assignment.deliver": "delivery",
    **{f"assignment.{verb}": "notification"
       for verb in ("progress", "block", "return", "complete", "fail")},
}
_STRICT_NATIVE = frozenset({"assignment.deliver", "assignment.progress", "assignment.block",
                            "assignment.return", "assignment.complete", "assignment.fail"})
_O1_NATIVE = frozenset({"message.send", "message.reply", "fleet.reports.submit"})


def _id(value, kind):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERNS[kind], value):
        raise ReceiptError("invalid receipt identity")


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReceiptError("invalid receipt digest")


def _fact(fact, seen):
    _id(fact.event_id, "event")
    _sha(fact.projection_sha256)
    if fact.family not in FACT_FAMILIES or fact.event_id in seen:
        raise ReceiptError("invalid or repeated expected fact")
    if (len(set(fact.fields)) != len(fact.fields)
            or any(not re.fullmatch(r"[a-z][a-z0-9_]*", field) for field in fact.fields)):
        raise ReceiptError("invalid expected fact fields")
    seen.add(fact.event_id)


def _native_destination(destination):
    for value in (destination.root, destination.tmux_tmpdir):
        if (not isinstance(value, str) or not os.path.isabs(value)
                or os.path.normpath(value) != value):
            raise ReceiptError("message route requires canonical absolute native paths")
    for value, pattern in ((destination.fleet, r"[A-Za-z0-9_-]+"),
                           (destination.socket, r"[A-Za-z0-9][A-Za-z0-9_.-]*"),
                           (destination.session, r"[A-Za-z0-9_-]+")):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ReceiptError("invalid message native destination")


def _message_route(intent):
    route = intent.route
    if not isinstance(route, MessageRouteBinding) or intent.recipient_uid is None or intent.message_id is None:
        raise ReceiptError("message request requires a frozen route, recipient and message ID")
    for value in (route.activation_id, route.plan_id, route.release_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            raise ReceiptError("invalid message selection stamp")
    for value in (route.caller_fleet_uid, route.peer_fleet_uid):
        _id(value, "fleet")
    _id(route.manager_uid, "actor")
    for value in (route.caller_alias, route.recipient_alias, route.manager_alias):
        if not isinstance(value, str) or not re.fullmatch(r"bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", value):
            raise ReceiptError("invalid frozen message actor alias")
    for destination in (route.peer_destination, route.manager_destination):
        if not isinstance(destination, NativeDestination):
            raise ReceiptError("message route requires native destinations")
        _native_destination(destination)
    if (route.peer_destination.root != route.manager_destination.root
            or route.peer_destination.fleet != route.recipient_alias.split(":", 1)[1].split("/", 1)[0]
            or route.peer_destination.session != route.recipient_alias.rsplit("/", 1)[1]
            or route.manager_destination.fleet != route.manager_alias.split(":", 1)[1].split("/", 1)[0]
            or route.manager_destination.session != route.manager_alias.rsplit("/", 1)[1]):
        raise ReceiptError("message route aliases differ from native destinations")


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
    is_message = intent.operation in _O1_NATIVE
    native_stage = _NATIVE_STAGES.get(intent.operation)
    if is_message or intent.route is not None:
        if native_stage is None:
            raise ReceiptError("operation cannot retain a native message route")
        _message_route(intent)
    if native_stage is not None:
        if is_message and intent.route is None:
            raise ReceiptError("ordinary message requires a frozen route")
        if intent.route is not None and sum(plan.kind == native_stage for plan in intent.stages) != 1:
            raise ReceiptError("native request requires one delivery or notification stage")
    if is_message:
        if sum(plan.kind == "delivery" for plan in intent.stages) != 1:
            raise ReceiptError("message request requires one delivery stage")
        if any(fact.family == "transmission" for plan in intent.stages for fact in plan.facts):
            raise ReceiptError("message transmission facts belong to reserved attempts")
    elif receipt.message_attempts and (intent.route is None or native_stage is None):
        raise ReceiptError("native attempts require a frozen message route")
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
            _fact(fact, seen)
    if native_stage is not None and (is_message or receipt.message_attempts):
        delivery = next(outcome for plan, outcome in zip(intent.stages, receipt.stages)
                        if plan.kind == native_stage)
        if is_message and len(receipt.message_attempts) != receipt.attempt:
            raise ReceiptError("each message attempt must be durably reserved")
        previous_attempt = 0
        for item in receipt.message_attempts:
            if (type(item.attempt_no) is not int or
                    not previous_attempt < item.attempt_no <= receipt.attempt):
                raise ReceiptError("native attempts must increase within operation attempts")
            previous_attempt = item.attempt_no
            _id(item.transmission_event_id, "event")
            if item.transmission_event_id in seen:
                raise ReceiptError("repeated message transmission event ID")
            seen.add(item.transmission_event_id)
            if item.observation is not None:
                observation = item.observation
                if observation.status not in {"submitted", "failed", "unknown"}:
                    raise ReceiptError("invalid message transport observation")
                if (observation.wire_sha256 is None) != (observation.wire_bytes is None):
                    raise ReceiptError("incomplete message wire proof")
                if observation.wire_sha256 is not None and (
                        not isinstance(observation.wire_sha256, str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", observation.wire_sha256)
                        or type(observation.wire_bytes) is not int or observation.wire_bytes < 0):
                    raise ReceiptError("invalid message wire proof")
                if observation.native_returncode is not None and type(observation.native_returncode) is not int:
                    raise ReceiptError("invalid native return code")
                if (observation.status == "submitted" and observation.native_returncode != 0
                        or observation.status == "failed" and (
                            observation.native_returncode is not None or observation.wire_sha256 is not None)):
                    raise ReceiptError("message transport observation is inconsistent")
            if item.recording_status not in _STATUSES["recording"]:
                raise ReceiptError("invalid message transmission recording status")
            if item.transmission_fact is not None:
                fact = item.transmission_fact
                if fact.event_id != item.transmission_event_id or fact.family != "transmission":
                    raise ReceiptError("message transmission fact differs from reserved event")
                if not {"event_id", "host_uid", "fleet_uid", "emitter"} <= set(fact.fields):
                    raise ReceiptError("message transmission fact lacks a scoped projection")
                seen.remove(item.transmission_event_id)
                _fact(fact, seen)
                if item.observation is None:
                    raise ReceiptError("transmission fact requires retained transport observation")
            elif item.recording_status != "prepared":
                raise ReceiptError("transmission recording status has no expected fact")
        if receipt.message_attempts:
            latest = receipt.message_attempts[-1]
            if latest.attempt_no != receipt.attempt or delivery.attempt != latest.attempt_no:
                raise ReceiptError("delivery must refer to latest reserved native attempt")
            expected_status = latest.observation.status if latest.observation is not None else "unknown"
            if delivery.status not in ({"submitted", "received"} if expected_status == "submitted"
                                       else {expected_status}):
                raise ReceiptError("delivery disagrees with retained native observation")


def _decode(raw):
    try:
        intent = raw["intent"]
        route = intent["route"]
        if route is not None:
            route = MessageRouteBinding(**{
                **route,
                "peer_destination": NativeDestination(**route["peer_destination"]),
                "manager_destination": NativeDestination(**route["manager_destination"]),
            })
        intent = RequestIntent(**{**intent, "route": route, "stages": tuple(
            StagePlan(**{**s, "facts": tuple(ExpectedFact(**{**f, "fields": tuple(f["fields"])})
                                             for f in s["facts"])})
            for s in intent["stages"])})
        attempts = tuple(MessageAttempt(**{
            **item,
            "observation": (TransportObservation(**item["observation"])
                            if item["observation"] is not None else None),
            "transmission_fact": (ExpectedFact(**{
                **item["transmission_fact"],
                "fields": tuple(item["transmission_fact"]["fields"]),
            }) if item["transmission_fact"] is not None else None),
        }) for item in raw["message_attempts"])
        receipt = RequestReceipt(**{**raw, "intent": intent,
                                   "stages": tuple(StageOutcome(**s) for s in raw["stages"]),
                                   "message_attempts": attempts})
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
        receipt = decode_receipt(raw, request_id=self.path.stem, fleet_uid=self.fleet_uid)
        self._check_route_root(receipt)
        return receipt

    def _check_route_root(self, receipt):
        route = receipt.intent.route
        if route is not None and route.peer_destination.root != str(self.path.parents[3]):
            raise ReceiptConflict("message route belongs to another request root")

    def _save(self, receipt):
        self.assert_locked()
        _validate(receipt)
        self._check_route_root(receipt)
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
        if receipt.intent.operation in _O1_NATIVE:
            raise ReceiptError("reserve a message attempt and delivery outcome atomically")
        if receipt.message_attempts:
            raise ReceiptConflict("a native notification cannot return to the recording stage")
        return self._save(replace(receipt, attempt=receipt.attempt + 1))

    def begin_native_attempt(self, transmission_event_id: str, *, retry_uncertain: bool = False) -> RequestReceipt:
        """Reserve an event ID and unknown delivery in one write BEFORE native send.

        Communication recording may still be pending after this reservation.
        A crash before the send then leaves delivery conservatively unknown;
        inspection never infers no send and this store never resends.
        A prior unknown/failed attempt needs an explicit caller decision. A
        submitted or received attempt never authorizes another send.
        """
        receipt = self._native_required()
        _id(transmission_event_id, "event")
        index = self._native_stage_index(receipt)
        if receipt.intent.operation in _STRICT_NATIVE and receipt.stages[0].status != "committed":
            raise ReceiptConflict("strict notification requires committed recording")
        old = receipt.stages[index]
        if old.status != "prepared" and not (old.status in {"unknown", "failed"} and retry_uncertain):
            raise ReceiptConflict("message send requires an explicit uncertain retry or has already submitted")
        if any(transmission_event_id == fact.event_id for plan in receipt.intent.stages for fact in plan.facts):
            raise ReceiptConflict("message event ID is already reserved")
        if any(transmission_event_id == item.transmission_event_id for item in receipt.message_attempts):
            raise ReceiptConflict("message event ID is already reserved")
        number = receipt.attempt + 1
        stages = receipt.stages[:index] + (StageOutcome("unknown", number),) + receipt.stages[index + 1:]
        return self._save(replace(receipt, attempt=number, stages=stages,
                                  message_attempts=receipt.message_attempts + (
                                      MessageAttempt(number, transmission_event_id),)))

    def observe_message_transport(self, attempt_no: int, observation: TransportObservation) -> RequestReceipt:
        """First post-send durable write; never revise a retained native result."""
        receipt = self._native_required()
        index, item = self._message_attempt(receipt, attempt_no)
        if not isinstance(observation, TransportObservation):
            raise ReceiptError("message transport observation must be typed and nonsecret")
        if item.observation is not None:
            if item.observation != observation:
                raise ReceiptConflict("message transport observation is immutable")
            return receipt
        delivery_index = self._native_stage_index(receipt)
        if index != len(receipt.message_attempts) - 1 or receipt.stages[delivery_index].attempt != attempt_no:
            raise ReceiptConflict("a later attempt superseded this delivery stage")
        attempts = self._replace_message_attempt(receipt, index, replace(item, observation=observation))
        stages = (receipt.stages[:delivery_index] + (StageOutcome(observation.status, attempt_no),)
                  + receipt.stages[delivery_index + 1:])
        return self._save(replace(receipt, message_attempts=attempts, stages=stages))

    def prepare_message_transmission(self, attempt_no: int, fact: ExpectedFact) -> RequestReceipt:
        """Freeze the exact observed transmission projection before Plane ingest."""
        receipt = self._native_required()
        index, item = self._message_attempt(receipt, attempt_no)
        if item.observation is None or not isinstance(fact, ExpectedFact):
            raise ReceiptError("retained native observation and expected fact are required")
        if fact.event_id != item.transmission_event_id or fact.family != "transmission":
            raise ReceiptConflict("transmission fact differs from its reserved event")
        if item.transmission_fact is not None:
            if item.transmission_fact != fact:
                raise ReceiptConflict("prepared transmission fact is immutable")
            return receipt
        attempts = self._replace_message_attempt(receipt, index, replace(item, transmission_fact=fact))
        return self._save(replace(receipt, message_attempts=attempts))

    def stage_message_transmission(self, attempt_no: int) -> RequestReceipt:
        """Mark exact-fact recording unknown BEFORE ingest; never sends payload."""
        receipt = self._native_required()
        index, item = self._message_attempt(receipt, attempt_no)
        if item.transmission_fact is None or item.recording_status not in {"prepared", "unrecorded"}:
            raise ReceiptConflict("transmission requires a prepared fact and reconciliation")
        attempts = self._replace_message_attempt(receipt, index, replace(item, recording_status="unknown"))
        return self._save(replace(receipt, message_attempts=attempts))

    def message_transmission_outcome(self, attempt_no: int, status: str) -> RequestReceipt:
        """Retain caller-proven exact-fact reconciliation, not transport claims."""
        receipt = self._native_required()
        index, item = self._message_attempt(receipt, attempt_no)
        if status not in {"unknown", "committed", "unrecorded"} or item.recording_status != "unknown":
            raise ReceiptConflict("transmission recording needs a staged, reconciled outcome")
        attempts = self._replace_message_attempt(receipt, index, replace(item, recording_status=status))
        return self._save(replace(receipt, message_attempts=attempts))

    def _native_required(self):
        receipt = self._required()
        if receipt.intent.operation not in _NATIVE_STAGES or receipt.intent.route is None:
            raise ReceiptError("native attempt requires a frozen message route")
        return receipt

    @staticmethod
    def _native_stage_index(receipt):
        kind = _NATIVE_STAGES[receipt.intent.operation]
        return next(i for i, plan in enumerate(receipt.intent.stages) if plan.kind == kind)

    @staticmethod
    def _message_attempt(receipt, attempt_no):
        if type(attempt_no) is int:
            for index, item in enumerate(receipt.message_attempts):
                if item.attempt_no == attempt_no:
                    return index, item
        raise ReceiptError("unknown native attempt")

    @staticmethod
    def _replace_message_attempt(receipt, index, value):
        return receipt.message_attempts[:index] + (value,) + receipt.message_attempts[index + 1:]

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
        same_message_recording_attempt = (receipt.intent.operation in _O1_NATIVE
                                          and plan.kind == "recording" and old.status == "unrecorded"
                                          and receipt.attempt == old.attempt > 0)
        if not allowed or (receipt.attempt <= old.attempt and not same_message_recording_attempt):
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
