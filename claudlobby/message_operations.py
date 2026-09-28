"""One ordinary send to a bot, with a receipt but no transport replay.

The public caller must hold runtime mutation_admission for the whole operation.
This owner neither resolves a route nor verifies receiver delivery or idle Enter.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import sys
from typing import Callable, Mapping
from uuid import UUID

from .message_context import MessageRoute
from .message_payload import (MessageBody, encode_communication, encode_transmission,
                              native_message_envelope, native_unlinked_report_envelope)
from .message_transport import TransportOutcome, send as native_send
from .plane.db import connect_ro, db_file
from .plane.emit_api import _load_capture_config, emit_batch, validate_item
from .plane.ids import ID_PATTERNS, mint_event_id, mint_msg_id
from .plane.schema_state import require_current_schema
from .recording_alerts import (ChannelOutcome, RecordingAlertOutcome,
                               clear_recording_degraded, notify_recording_degraded)
from .request_facts import expected_fact, reconcile_facts
from .request_receipts import (ReceiptConflict, RequestIntent, RequestReceipt, RequestStore, StagePlan,
                               TransportObservation, locked_request, semantic_digest)
from .report_payload import ReportPayload, encode_report_facts
from .resources import PackageResources


class MessageConflict(ValueError):
    """Frozen scope, request or Plane proof conflicts; no native effect is authorized."""


class MessageIdentityUnavailable(RuntimeError):
    """A local human has no readable identity proof before native transport."""


@dataclass(frozen=True)
class MessageSendResult:
    request_id: str
    message_id: str
    delivery: str                 # submitted, failed, unknown; never receiver-verified
    recording: str                # committed, unrecorded, unknown
    request_persisted: bool
    replayed: bool
    ok: bool
    code: str
    exit_code: int
    retryable: bool = False
    alert: object | None = None


@dataclass(frozen=True)
class NativeAttemptReservation:
    attempt_no: int
    event_id: str
    previous: object | None
    new: bool
    retained: bool


@dataclass(frozen=True)
class RenderedNativeEnvelope:
    message_id: str
    body: str


@dataclass(frozen=True)
class NativeAttemptResult:
    delivery: str
    transmission_recording: str
    observation: TransportObservation | None
    request_persisted: bool
    replayed: bool
    attempt_no: int
    event_id: str


def _identity_proof(conn, route: MessageRoute) -> None:
    expected = (("fleet", route.selected.fleet.name, route.selected_fleet_uid, None),
                ("fleet", route.peer_context.fleet.name, route.peer_fleet_uid, None),
                ("actor", route.caller.alias, route.caller.uid, route.caller_fleet_uid),
                ("actor", route.peer.alias, route.peer.uid, route.peer_fleet_uid),
                ("actor", route.manager.alias, route.manager.uid, route.selected_fleet_uid))
    if route.origin is not None:
        expected += (("fleet", route.origin.fleet.name, route.caller_fleet_uid, None),)
    for kind, alias, uid, parent in expected:
        row = conn.execute("SELECT uid, parent_uid FROM identity_registry WHERE kind=? AND alias=?",
                           (kind, alias)).fetchone()
        if row is None or row[0] != uid or (parent is not None and row[1] != parent):
            raise MessageConflict("frozen message identity is absent or foreign")


@contextmanager
def _reader(root: Path, route: MessageRoute):
    try:
        conn = connect_ro(db_file(root), timeout=2)
    except (FileNotFoundError, OSError, sqlite3.Error):
        yield None
        return
    with closing(conn):
        try:
            require_current_schema(conn)  # A wrong schema is admission failure, not O1.
            _identity_proof(conn, route)
        except sqlite3.Error:
            yield None
            return
        yield conn


def _proof(root: Path, route: MessageRoute, fact) -> str:
    with _reader(root, route) as conn:
        if conn is None:
            return "unknown"
        status = reconcile_facts(conn, fact if isinstance(fact, tuple) else (fact,)).status
    if status == "conflict":
        raise MessageConflict("stored message fact differs from frozen request")
    return status


def _record(root: Path, route: MessageRoute, raw: dict | tuple[dict, ...], fact, *, store, stage: int | None,
            attempt_no: int | None, persistence: list[bool]) -> str:
    """Only exact absence allows another Plane write; duplicate is not proof."""
    status = _proof(root, route, fact)
    if store is not None and persistence[0] and status in {"unrecorded", "committed"}:
        try:
            if stage is not None:
                old = store.load().stages[stage]
                if old.status in {"prepared", "unrecorded"}:
                    store.stage(stage)
            else:
                item = next(item for item in store.load().message_attempts
                            if item.attempt_no == attempt_no)
                if item.recording_status in {"prepared", "unrecorded"}:
                    store.stage_message_transmission(attempt_no)
        except OSError:
            persistence[0] = False
    if status == "unrecorded":
        try:
            emit_batch(root, list(raw) if isinstance(raw, tuple) else [raw], require_commit=True)
        except (OSError, sqlite3.Error):
            pass  # Reconciliation below decides whether a commit actually happened.
        status = _proof(root, route, fact)
    if store is not None and persistence[0] and status in {"committed", "unrecorded", "unknown"}:
        try:
            if stage is not None and store.load().stages[stage].status == "unknown":
                store.outcome(stage, status)
            elif stage is None and next(item for item in store.load().message_attempts
                                        if item.attempt_no == attempt_no).recording_status == "unknown":
                store.message_transmission_outcome(attempt_no, status)
        except OSError:
            persistence[0] = False
    return status


def _observation(result: TransportOutcome) -> TransportObservation:
    return TransportObservation(result.status, result.wire_sha256, result.wire_bytes,
                                result.native_returncode)


def _unobserved_attempt_has_no_fact(root: Path, route: MessageRoute, event_id: str) -> bool:
    """A retained reservation without a result may have a submitted Plane fact.

    The receipt has no expected projection yet, so exact reconciliation is not
    possible. Any row at that event ID, or unavailable proof, forbids retry.
    Definite absence leaves the usual explicit-uncertain-retry choice.
    """
    with _reader(root, route) as conn:
        if conn is None:
            return False
        try:
            ledger = conn.execute("SELECT 1 FROM ingest_ledger WHERE event_id=?", (event_id,)).fetchone()
            row = conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone()
        except sqlite3.Error:
            return False
        return ledger is None and row is None


def reserve_native_attempt(store: RequestStore | None, route: MessageRoute, receipt, *,
                           retry_uncertain: bool, persistence: list[bool],
                           strict: bool = False) -> NativeAttemptReservation:
    """One reservation under the caller's request lock; never acquires another lock.

    A strict task/report caller must already have prepared its frozen receipt.
    A missing O1 receipt is possible only for ordinary or unlinked communication.
    """
    if strict and (store is None or receipt is None or not persistence[0]):
        raise ReceiptConflict("strict native delivery requires a durable request receipt")
    prior = receipt.message_attempts[-1] if receipt and receipt.message_attempts else None
    if (prior is not None and retry_uncertain and prior.observation is None
            and not _unobserved_attempt_has_no_fact(route.selected.paths.root, route,
                                                    prior.transmission_event_id)):
        raise ReceiptConflict("reserved native attempt may already have a recorded submission; "
                              "inspect request before retry")
    if prior is not None and not retry_uncertain:
        return NativeAttemptReservation(prior.attempt_no, prior.transmission_event_id,
                                        prior, False, True)
    event_id = mint_event_id()
    updated = receipt
    if store is not None and receipt is not None:
        try:
            updated = store.begin_native_attempt(event_id, retry_uncertain=retry_uncertain)
        except OSError:
            if strict:
                # A post-rename failure may have left a reservation. No send is
                # allowed until a later invocation inspects it under the lock.
                raise
            persistence[0] = False
            updated = store.load()
    retained = (updated is not None and bool(updated.message_attempts)
                and updated.message_attempts[-1].transmission_event_id == event_id)
    if strict and not retained:
        raise ReceiptConflict("strict native reservation could not be proved")
    attempt_no = (updated.attempt if retained else
                  receipt.attempt + 1 if receipt is not None else 1)
    return NativeAttemptReservation(attempt_no, event_id, None, True, retained)


def transmit_native_attempt(route: MessageRoute, package: PackageResources,
                            intent: RequestIntent, *, request_id: str,
                            reservation: NativeAttemptReservation, envelope: RenderedNativeEnvelope,
                            modes, parties: dict[str, str], persistence: list[bool],
                            store: RequestStore | None, at: datetime,
                            transport: Callable = native_send,
                            strict: bool = False) -> NativeAttemptResult:
    """The sole native/observation/transmission leg for frozen message effects.

    A replay may revalidate or record retained facts; it never calls transport.
    The caller remains responsible for its separate communication/task batch.
    """
    if (not isinstance(envelope, RenderedNativeEnvelope) or
            envelope.message_id != intent.message_id or not isinstance(envelope.body, str) or
            intent.route != route.receipt_binding()):
        raise MessageConflict("native envelope or route differs from frozen request")
    prior = reservation.previous
    if reservation.new:
        try:
            native = transport(package, route.peer_destination,
                               message_id=intent.message_id, body=envelope.body)
            if not isinstance(native, TransportOutcome):
                native = TransportOutcome("unknown")
        except Exception:
            native = TransportOutcome("unknown")
        observation = _observation(native)
        observation_retained = False
        if store is not None and reservation.retained:
            try:
                store.observe_message_transport(reservation.attempt_no, observation)
                observation_retained = True
            except OSError:
                persistence[0] = False
    else:
        observation = prior.observation
        observation_retained = observation is not None
    tx_status = "unknown"
    if observation is not None and (not strict or observation_retained):
        try:
            tx_raw = encode_transmission(intent, observation, request_id=request_id,
                                         attempt_no=reservation.attempt_no,
                                         event_id=reservation.event_id,
                                         occurred_at=at.isoformat())
            tx_item, _ = validate_item(tx_raw, modes)
            tx_fact = expected_fact(tx_item, host_uid=route.host_uid,
                                    fleet_uid=route.selected_fleet_uid, parties=parties)
            if prior is not None and prior.transmission_fact is not None and prior.transmission_fact != tx_fact:
                raise ReceiptConflict("retained transmission projection changed")
            if store is not None and observation_retained:
                try:
                    store.prepare_message_transmission(reservation.attempt_no, tx_fact)
                except OSError:
                    persistence[0] = False
            tx_status = _record(route.selected.paths.root, route, tx_raw, tx_fact, store=store,
                                stage=None, attempt_no=reservation.attempt_no,
                                persistence=persistence)
        except Exception:
            if not reservation.new:
                raise  # A replay has no new effect, so an invalid projection is a refusal.
            tx_status = "unknown"  # Preserve the observed native result after a post-send failure.
    return NativeAttemptResult(observation.status if observation is not None else "unknown",
                               tx_status, observation, persistence[0], not reservation.new,
                               reservation.attempt_no, reservation.event_id)


def send_committed_native_attempt(route: MessageRoute, package: PackageResources,
                                  store: RequestStore, receipt: RequestReceipt,
                                  envelope: RenderedNativeEnvelope, *, request_id: str,
                                  retry_uncertain: bool = False,
                                  transport: Callable = native_send) -> NativeAttemptResult:
    """Notify after a proved task/report batch, using the caller's held request store.

    The caller commits the recording batch under its task lock, then releases
    that lock before entering here. A failed notification never replays that
    batch. No request lock is acquired here, and no O1 best-effort path applies.
    """
    if (not isinstance(store, RequestStore) or not isinstance(receipt, RequestReceipt)
            or receipt.request_id != request_id or receipt.intent.operation not in {
                "task.nudge", "task.recheck", "assignment.deliver", "assignment.progress", "assignment.block",
                "assignment.return", "assignment.complete", "assignment.fail"}):
        raise MessageConflict("strict native attempt requires a frozen task or linked report request")
    if (not isinstance(envelope, RenderedNativeEnvelope) or
            not isinstance(envelope.body, str)):
        raise MessageConflict("typed rendered native envelope required")
    store.assert_locked()
    if store.load() != receipt:
        raise ReceiptConflict("request changed before native notification")
    intent = receipt.intent
    if (intent.route != route.receipt_binding() or intent.message_id != envelope.message_id
            or package != route.selected.paths.package
            or intent.host_uid != route.host_uid or intent.fleet_uid != route.selected_fleet_uid
            or intent.caller_uid != route.caller.uid or intent.recipient_uid != route.peer.uid):
        raise ReceiptConflict("native notification differs from frozen request or route")
    if receipt.stages[0].status != "committed":
        raise ReceiptConflict("strict native attempt requires committed recording")
    with _reader(route.selected.paths.root, route) as conn:
        if conn is None:
            raise MessageConflict("strict recording proof is unavailable")
        proof = reconcile_facts(conn, intent.stages[0].facts)
        if proof.status != "committed":
            raise MessageConflict("strict recording fact is absent, unavailable or conflicting")
    modes = _load_capture_config(route.selected.paths.root)
    persistence = [True]
    reservation = reserve_native_attempt(store, route, receipt,
                                         retry_uncertain=retry_uncertain,
                                         persistence=persistence, strict=True)
    at = datetime.now(timezone.utc)
    parties = {route.caller.alias: route.caller.uid, route.peer.alias: route.peer.uid}
    return transmit_native_attempt(route, package, intent, request_id=request_id,
                                   reservation=reservation, envelope=envelope, modes=modes,
                                   parties=parties, persistence=persistence, store=store, at=at,
                                   transport=transport, strict=True)


def send_message(route: MessageRoute, package: PackageResources, body: MessageBody, *,
                 request_id: str, kind: str = "chat", retry_uncertain: bool = False,
                 parent_message_id: str | None = None,
                 trusted_tiers: Mapping[str, str],
                 transport: Callable = native_send,
                 notify: Callable = notify_recording_degraded,
                 clear: Callable = clear_recording_degraded,
                 _report: ReportPayload | None = None) -> MessageSendResult:
    """Send once under caller-held runtime admission; inspect or explicitly retry later.

    ReceiptBusy/ReceiptError/MessageConflict and capture-policy failures are
    refusals. Recording outages alone permit the ordinary native attempt (O1).
    IDs and route are frozen before any effect. No request body enters a receipt.
    """
    if not isinstance(route, MessageRoute) or not isinstance(body, MessageBody):
        raise MessageConflict("frozen route and validated body required")
    if (route.origin is not None and route.selected.paths.root != route.origin.paths.root
            or route.selected.paths.root != route.peer_context.paths.root
            or package != route.selected.paths.package):
        raise MessageConflict("message route and package differ")
    if (route.peer_destination.root != route.selected.paths.root
            or route.manager_destination.root != route.selected.paths.root
            or route.peer_destination.fleet != route.peer_context.fleet.name
            or route.manager_destination.fleet != route.selected.fleet.name
            or route.origin is not None and not route.caller.alias.startswith(
                f"bot:{route.origin.fleet.name}/")
            or route.origin is None and (not route.caller.alias.startswith("human:")
                                         or route.caller_fleet_uid is not None)):
        raise MessageConflict("native destination or caller differs from frozen route")
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError("noncanonical")
    except (ValueError, AttributeError) as exc:
        raise MessageConflict("canonical request UUID required") from exc
    if _report is not None:
        if (route.origin is None or not isinstance(_report, ReportPayload) or body.text != _report.to_body()
                or parent_message_id is not None or kind != "chat"
                or route.origin.fleet.name != route.selected.fleet.name
                or route.caller_fleet_uid != route.selected_fleet_uid
                or route.peer_fleet_uid != route.selected_fleet_uid
                or route.peer != route.manager
                or route.peer_destination != route.manager_destination
                or route.manager.alias != (
                    f"bot:{route.selected.fleet.name}/{route.selected.fleet.manager}")
                or route.manager_destination.session != route.selected.fleet.manager):
            raise MessageConflict("unlinked report requires its frozen fleet manager and typed body")
        operation = "fleet.reports.submit"
    elif parent_message_id is None:
        if kind not in {"question", "notice", "chat"}:
            raise MessageConflict("ordinary send kind must be question, notice or chat")
        operation = "message.send"
    else:
        if kind != "answer" or not isinstance(parent_message_id, str) or not re.fullmatch(
                ID_PATTERNS["msg"], parent_message_id):
            raise MessageConflict("reply requires an exact parent message ID and answer kind")
        operation = "message.reply"
    root = route.selected.paths.root
    try:
        if (root / "state/host-uid").read_text().strip() != route.host_uid:
            raise MessageConflict("active host identity differs from frozen route")
    except OSError as exc:
        raise MessageConflict("active host identity is unavailable") from exc
    if route.origin is None:
        # Unlike generated callers, a human has no retained activation UID.
        # Prove the existing registry binding before creating request state.
        with _reader(root, route) as conn:
            if conn is None:
                raise MessageIdentityUnavailable("local human identity proof is unavailable")

    modes = _load_capture_config(root)  # Invalid capture policy is a refusal.
    if _report is not None:
        semantic_input = {"report": body.text.encode("utf-8")}
    elif parent_message_id is not None:
        semantic_input = {"body": body.text.encode("utf-8"), "kind": kind,
                          "parent_message_id": parent_message_id}
    else:
        semantic_input = {"body": body.text.encode("utf-8"), "kind": kind}
    semantic = semantic_digest(semantic_input)
    parties = {route.caller.alias: route.caller.uid, route.peer.alias: route.peer.uid}
    persistence = [True]
    at = datetime.now(timezone.utc)

    def run(store):
        existing = store.load() if store is not None else None
        if existing is not None:
            old = existing.intent
            if (old.operation != operation or old.operation_version != 1
                    or old.host_uid != route.host_uid or old.fleet_uid != route.selected_fleet_uid
                    or old.caller_uid != route.caller.uid or old.recipient_uid != route.peer.uid
                    or old.semantic_sha256 != semantic or old.route != route.receipt_binding()):
                raise ReceiptConflict("request UUID already has different message semantics or route")
            if not existing.message_attempts and not retry_uncertain:
                # An O1 invocation might have sent after a failed reservation.
                # A prepared-only receipt therefore cannot authorize a resend.
                raise ReceiptConflict("prepared message requires an explicit uncertain retry")
            message_id = old.message_id
            event_ids = tuple(fact.event_id for fact in old.stages[0].facts)
            if len(event_ids) != (2 if _report is not None else 1):
                raise ReceiptConflict("frozen message fact count differs from operation")
        else:
            message_id = mint_msg_id()
            event_ids = (mint_event_id(), mint_event_id()) if _report is not None else (mint_event_id(),)
        intent = RequestIntent(operation, 1, route.host_uid, route.selected_fleet_uid,
                               route.caller.uid, route.peer.uid, semantic,
                               (StagePlan("recording"), StagePlan("delivery")),
                               message_id=message_id, route=route.receipt_binding())
        if _report is not None:
            raw = encode_report_facts(_report, fleet=route.selected.fleet.name,
                                      sender=route.caller.alias, recipient=route.manager.alias,
                                      msg_id=message_id, event_ids=event_ids,
                                      occurred_at=at.isoformat(), link=None)
        else:
            raw = encode_communication(intent, body, request_id=request_id,
                                       event_id=event_ids[0], occurred_at=at.isoformat(),
                                       kind=None if parent_message_id else kind,
                                       parent_message_id=parent_message_id)
        raws = raw if isinstance(raw, tuple) else (raw,)
        facts = tuple(expected_fact(validate_item(item, modes)[0], host_uid=route.host_uid,
                                    fleet_uid=route.selected_fleet_uid, parties=parties)
                      for item in raws)
        intent = RequestIntent(operation, 1, route.host_uid, route.selected_fleet_uid,
                               route.caller.uid, route.peer.uid, semantic,
                               (StagePlan("recording", facts), StagePlan("delivery")),
                               message_id=message_id, route=route.receipt_binding())
        if existing is not None:
            if existing.intent.stages[0].facts != facts:
                raise ReceiptConflict("capture policy or communication projection changed")
            receipt = existing
        elif store is not None:
            try:
                receipt = store.prepare(intent)
            except OSError:
                persistence[0] = False
                # A failure after rename may still have left the prepare
                # visible. Inspect under the held lock; never assume absence.
                receipt = store.load()
                if receipt is not None and receipt.intent != intent:
                    raise ReceiptConflict("request UUID changed during failed prepare")
        else:
            receipt = None

        reservation = reserve_native_attempt(store, route, receipt,
                                             retry_uncertain=retry_uncertain,
                                             persistence=persistence)

        comm_status = _record(root, route, raw, facts, store=store, stage=0,
                              attempt_no=None, persistence=persistence)
        envelope = RenderedNativeEnvelope(
            message_id, (native_unlinked_report_envelope(
                intent, body, request_id=request_id,
                recording_degraded=(comm_status != "committed" or not persistence[0]))
                if _report is not None else native_message_envelope(
                    intent, body, request_id=request_id,
                    kind=None if parent_message_id else kind,
                    parent_message_id=parent_message_id,
                    recording_degraded=(comm_status != "committed" or not persistence[0]))))
        native_result = transmit_native_attempt(route, package, intent, request_id=request_id,
                                                reservation=reservation, envelope=envelope,
                                                modes=modes, parties=parties, persistence=persistence,
                                                store=store, at=at, transport=transport)
        delivery, tx_status = native_result.delivery, native_result.transmission_recording
        recording = ("committed" if comm_status == tx_status == "committed" else
                     "unrecorded" if "unrecorded" in (comm_status, tx_status) and
                     "unknown" not in (comm_status, tx_status) else "unknown")
        degraded = recording != "committed" or not persistence[0]
        alert = None
        if degraded:
            component = ("request_receipt" if not persistence[0] else
                         ("report_intent" if _report is not None else "message_intent")
                         if comm_status != "committed" else "transmission_record")
            try:
                alert = notify(route.selected, package, route.manager_destination,
                               request_id=request_id, component=component, at=at,
                               trusted_tiers=trusted_tiers)
            except Exception:
                alert = RecordingAlertOutcome(ChannelOutcome("failed", None, "adapter_error"),
                                              ChannelOutcome("failed", None, "adapter_error"))
                print(f"recording-alert: fleet={route.selected.fleet.name} request={request_id} "
                      "status=failed", file=sys.stderr)
        elif reservation.new:
            try:
                clear(route.selected, package, route.manager_destination,
                      request_id=request_id,
                      component="unlinked_report" if _report is not None else "ordinary_message", at=at)
            except Exception:
                pass
        code = ("recording_degraded" if degraded else
                "submitted" if delivery == "submitted" else "transport_" + delivery)
        return MessageSendResult(request_id, message_id, delivery, recording,
                                 persistence[0], native_result.replayed,
                                 not degraded and delivery == "submitted",
                                 code, 11 if degraded else (0 if delivery == "submitted" else 5),
                                 alert=alert)

    # Distinguish unavailable request persistence from an existing receipt.
    # A failed read after lock acquisition never becomes a fresh send.
    try:
        lock = locked_request(root, route.selected_fleet_uid, request_id)
        store = lock.__enter__()
    except OSError:
        path = root / "state/requests" / route.selected_fleet_uid / (request_id + ".json")
        try:
            path.lstat()
        except FileNotFoundError:
            persistence[0] = False
            with _reader(root, route) as conn:
                if route.origin is None and conn is None:
                    raise MessageIdentityUnavailable("local human identity proof is unavailable")
                return run(None)
        except OSError as exc:
            raise ReceiptConflict("existing request history cannot be inspected") from exc
        raise ReceiptConflict("existing request history cannot be locked")
    try:
        with _reader(root, route) as conn:
            if route.origin is None and conn is None:
                raise MessageIdentityUnavailable("local human identity proof is unavailable")
            return run(store)
    finally:
        lock.__exit__(None, None, None)


def send_unlinked_report(route: MessageRoute, package: PackageResources, report: ReportPayload, *,
                         request_id: str, retry_uncertain: bool = False,
                         trusted_tiers: Mapping[str, str],
                         transport: Callable = native_send,
                         notify: Callable = notify_recording_degraded,
                         clear: Callable = clear_recording_degraded) -> MessageSendResult:
    """Report to this fleet's manager without consulting or changing task state."""
    if not isinstance(report, ReportPayload):
        raise MessageConflict("typed report payload required")
    return send_message(route, package, MessageBody(report.to_body()), request_id=request_id,
                        retry_uncertain=retry_uncertain, trusted_tiers=trusted_tiers,
                        transport=transport, notify=notify, clear=clear, _report=report)
