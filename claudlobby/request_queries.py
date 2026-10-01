"""Read one retained request and independently inspect its recorded Plane facts.

The result is an observation of one receipt-file read and one database snapshot.
It is not a current request lock, a send queue, or permission to retry an effect.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from .plane.db import connect_ro, db_file
from .plane.ids import ID_PATTERNS
from .plane.schema_state import require_current_schema
from .request_facts import FactProof, reconcile_facts
from .request_receipts import (ExpectedFact, MessageRouteBinding, ReceiptError,
                               TransportObservation, _regular, decode_receipt)


MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_MISSING_HINT = ("No retained request record exists in this fleet; this cannot prove "
                 "that no earlier best-effort message was sent.")


class RequestQueryError(ValueError):
    code = "conflict"


class RequestInvalidArgumentError(RequestQueryError):
    code = "invalid_argument"


class RequestNotFoundError(RequestQueryError):
    code = "not_found"

    def __init__(self):
        self.hint = _MISSING_HINT
        super().__init__("no retained request record for this fleet")


class RequestUnavailableError(RequestQueryError):
    code = "unavailable"


@dataclass(frozen=True)
class RecordedStage:
    kind: str
    recorded_status: str
    recorded_attempt: int
    expected_facts: tuple[ExpectedFact, ...]
    proof: FactProof | None  # None: this stage has no Plane fact to inspect.


@dataclass(frozen=True)
class RecordedTransmission:
    attempt_no: int
    transmission_event_id: str
    transport: TransportObservation | None  # None: reserved, effect may have happened.
    recorded_status: str
    expected_fact: ExpectedFact | None
    proof: FactProof | None


@dataclass(frozen=True)
class RequestView:
    request_id: str
    format_version: int
    operation: str
    operation_version: int
    host_uid: str
    fleet_uid: str
    caller_uid: str
    recipient_uid: str | None
    task_id: str | None
    assignment_id: str | None
    message_id: str | None
    semantic_sha256: str
    route: MessageRouteBinding | None
    observed_attempt: int  # Receipt snapshot, not a claim that no later attempt exists.
    stages: tuple[RecordedStage, ...]
    transmissions: tuple[RecordedTransmission, ...]


def _scope(root: Path, fleet_uid: str, request_id: str) -> Path:
    if not isinstance(fleet_uid, str) or not re.fullmatch(ID_PATTERNS["fleet"], fleet_uid):
        raise RequestInvalidArgumentError("selected fleet requires a canonical fleet identity")
    try:
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError("noncanonical request ID")
    except (TypeError, ValueError, AttributeError) as exc:
        raise RequestInvalidArgumentError("request ID must be a canonical UUID") from exc
    path = Path(root)
    if not path.is_absolute():
        raise RequestInvalidArgumentError("selected request root must be absolute")
    try:
        if path.resolve(strict=True) != path:
            raise RequestQueryError("selected request root is redirected")
    except (OSError, RuntimeError) as exc:
        raise RequestUnavailableError("selected request root is unavailable") from exc
    return path


def _read_receipt(root: Path, fleet_uid: str, request_id: str):
    """Follow no request-path symlinks, provision nothing, and bound the read."""
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            for part in ("state", "requests", fleet_uid):
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(f"{request_id}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                _regular(stream.fileno())
                if os.fstat(stream.fileno()).st_size > MAX_RECEIPT_BYTES:
                    raise RequestQueryError("retained request record exceeds the read limit")
                data = stream.read(MAX_RECEIPT_BYTES + 1)
            if len(data) > MAX_RECEIPT_BYTES:
                raise RequestQueryError("retained request record exceeds the read limit")
        receipt = decode_receipt(json.loads(data), request_id=request_id, fleet_uid=fleet_uid)
        route = receipt.intent.route
        if route is not None and route.peer_destination.root != str(root):
            raise RequestQueryError("retained message route belongs to another host root")
        return receipt
    except FileNotFoundError as exc:
        raise RequestNotFoundError() from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RequestQueryError("retained request path is redirected or not a directory") from exc
        raise RequestUnavailableError("retained request record is unavailable") from exc
    except (ReceiptError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RequestQueryError("retained request record is invalid or unsafe") from exc


def _proof(conn: sqlite3.Connection | None, facts: tuple[ExpectedFact, ...]) -> FactProof | None:
    if not facts:
        return None
    if conn is None:
        return FactProof("unknown", "Plane recording proof is unavailable")
    try:
        return reconcile_facts(conn, facts)
    except ReceiptError:
        return FactProof("unknown", "retained fact projection cannot prove recording")


def read_request(root: Path, fleet_uid: str, request_id: str) -> RequestView:
    """Inspect one selected-fleet receipt and exact facts without writing state."""
    root = _scope(root, fleet_uid, request_id)
    receipt = _read_receipt(root, fleet_uid, request_id)
    conn = None
    try:
        try:
            conn = connect_ro(db_file(root), timeout=1.0)
            conn.execute("BEGIN")
            require_current_schema(conn)
        except (OSError, sqlite3.Error, RuntimeError):
            if conn is not None:
                conn.close()
            conn = None
        stages = tuple(RecordedStage(plan.kind, outcome.status, outcome.attempt,
                                     plan.facts, _proof(conn, plan.facts))
                       for plan, outcome in zip(receipt.intent.stages, receipt.stages))
        transmissions = tuple(RecordedTransmission(
            item.attempt_no, item.transmission_event_id, item.observation,
            item.recording_status, item.transmission_fact,
            _proof(conn, (item.transmission_fact,)) if item.transmission_fact is not None else None,
        ) for item in receipt.message_attempts)
    finally:
        if conn is not None:
            conn.close()
    intent = receipt.intent
    return RequestView(request_id, receipt.format_version, intent.operation, intent.operation_version,
                       intent.host_uid, intent.fleet_uid, intent.caller_uid, intent.recipient_uid,
                       intent.task_id, intent.assignment_id, intent.message_id,
                       intent.semantic_sha256, intent.route, receipt.attempt, stages, transmissions)
