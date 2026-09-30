"""One committed check-in decision, with exact replay proof and no ACT leg."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import sqlite3
from uuid import UUID


@dataclass(frozen=True)
class CheckinResult:
    request_id: str
    checkin_id: str
    actor: str
    fleet_uid: str
    recording: str
    request_persisted: bool
    replayed: bool


class CheckinError(RuntimeError):
    def __init__(self, code: str, message: str, *, result: CheckinResult | None = None,
                 hint: str | None = None, retryable: bool = False, data: dict | None = None):
        self.code, self.result = code, result
        self.hint, self.retryable, self.data = hint, retryable, data
        super().__init__(message)


def _inspect(request_id: str) -> str:
    return (f"inspect claudlobby --json request show {request_id}; "
            "then retry the same --request-id, never a new one")


def _proof(root, fact):
    from .plane.db import connect_ro, db_file
    from .plane.schema_state import require_current_schema
    from .request_facts import reconcile_facts

    with closing(connect_ro(db_file(root))) as conn:
        conn.execute("BEGIN")
        require_current_schema(conn)
        return reconcile_facts(conn, (fact,))


def _result(ctx, request_id, checkin_id, *, persisted, replayed):
    return CheckinResult(request_id, checkin_id, ctx.caller.alias, ctx.fleet_uid,
                         "committed", persisted, replayed)


def validate_content(decision: dict, selection: dict | None, *, checkin_id: str) -> dict:
    """Return exact retained data, refusing invalid or truncatable evidence."""
    from .checkin_contract import normalize
    from .checkin_selection import OK, verify_record
    from .plane.registries import FIELD_POLICY

    if isinstance(decision, dict) and decision.get("checkin_id") not in (None, checkin_id):
        raise CheckinError("invalid_argument", "decision file names a different check-in ID")
    data = dict(normalize(decision, checkin_id=checkin_id))
    if selection is not None:
        verdict, findings = verify_record(selection)
        if verdict != OK:
            raise CheckinError("invalid_argument",
                               f"selection evidence verdict {verdict} ({len(findings)} findings): "
                               + "; ".join(findings),
                               data={"verdict": verdict, "findings": findings})
        data["selection"] = selection
        data["selection_verification"] = {"verdict": verdict, "findings": findings}
    cap = FIELD_POLICY[("system", "data")]["cap"]
    try:
        size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise CheckinError("invalid_argument", "check-in evidence is not JSON-safe") from exc
    if size > cap:
        raise CheckinError("invalid_argument", "combined check-in and selection evidence exceeds 16384 bytes")
    return data


def record_checkin(ctx, selected: dict, decision: dict, selection: dict | None, *,
                   request_id: str) -> CheckinResult:
    """Commit the exact decision first; caller holds selected-release admission."""
    from .activation_state import read_selection
    from .plane import PLANE_SCHEMA_VERSION
    from .plane.emit_api import emit_batch, validate_item
    from .plane.ids import derive_uid
    from .request_facts import expected_fact
    from .request_receipts import (ReceiptBusy, ReceiptError, RequestIntent, StagePlan,
                                   locked_request, semantic_digest)

    root = ctx.root
    fleet = ctx.context.fleet.name
    manager = ctx.context.fleet.manager
    manager_actor = ctx.bots.get(manager)
    generated_manager = (manager_actor is not None and ctx.caller == manager_actor
                         and ctx.caller_fleet_uid == ctx.fleet_uid)
    local_human = (ctx.caller_fleet_uid is None
                   and re.fullmatch(r"human:[^\s:/]+", ctx.caller.alias) is not None)
    if (not isinstance(selected, dict) or read_selection(root) != selected
            or not (generated_manager or local_human)):
        raise CheckinError("conflict", "check-in caller differs from selected manager or local human")
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise CheckinError("invalid_argument", "check-in requires a canonical request UUID") from exc
    checkin_id = derive_uid("ck", f"checkin:v1:{ctx.host_uid}:{ctx.fleet_uid}:{request_id}")
    data = validate_content(decision, selection, checkin_id=checkin_id)
    # Release selection is checked under admission, not frozen into semantics:
    # a committed decision stays replayable across a release switch.
    semantic = semantic_digest({"decision": data})
    event_id = derive_uid("ev", f"checkin-record:v1:{ctx.host_uid}:{ctx.fleet_uid}:{request_id}")
    inspect = _inspect(request_id)
    try:
        with locked_request(root, ctx.fleet_uid, request_id) as store:
            previous = store.load()
            if previous is not None:
                intent = previous.intent
                if (intent.operation != "checkin.record" or intent.operation_version != 1
                        or intent.host_uid != ctx.host_uid or intent.fleet_uid != ctx.fleet_uid
                        or intent.caller_uid != ctx.caller.uid or intent.recipient_uid is not None
                        or intent.semantic_sha256 != semantic or len(intent.stages) != 1
                        or intent.stages[0].kind != "recording"
                        or len(intent.stages[0].facts) != 1
                        or intent.stages[0].facts[0].event_id != event_id):
                    raise CheckinError("conflict", "request UUID has different check-in semantics",
                                       hint="this request ID already names another decision; "
                                            "use a new --request-id for new content")
                proof = _proof(root, intent.stages[0].facts[0])
                if proof.status == "committed":
                    if previous.stages[0].status == "unknown":
                        try:
                            store.outcome(0, "committed")
                        except OSError as exc:
                            raise CheckinError("unavailable", "decision committed but request persistence failed",
                                               hint=inspect,
                                               result=_result(ctx, request_id, checkin_id,
                                                              persisted=False, replayed=True)) from exc
                    elif previous.stages[0].status != "committed":
                        raise CheckinError("conflict", "decision fact conflicts with retained request state",
                                           hint=inspect)
                    return _result(ctx, request_id, checkin_id, persisted=True, replayed=True)
                if proof.status != "unrecorded" or previous.stages[0].status not in ("prepared", "unknown", "unrecorded"):
                    raise CheckinError("unavailable" if proof.status == "unknown" else "conflict",
                                       "prior check-in attempt cannot be proved absent", hint=inspect)
                if previous.stages[0].status == "unknown":
                    store.outcome(0, "unrecorded")
            # The event ID and data are request-derived and the frozen fact
            # excludes clock fields, so a definitely absent attempt rebuilds to
            # the same projection. Anything else refuses below.
            raw = {"event_id": event_id, "event_type": "system", "emitter": "checkin-record",
                   "schema_version": PLANE_SCHEMA_VERSION, "fleet": fleet,
                   "source_ref": f"checkin:{checkin_id}",
                   "occurred_at": datetime.now(timezone.utc).isoformat(),
                   "payload": {"event": "checkin_decision", "subject_kind": "actor",
                               "subject": ctx.caller.alias, "data": data}}
            fact = expected_fact(validate_item(raw, {})[0], host_uid=ctx.host_uid,
                                 fleet_uid=ctx.fleet_uid,
                                 parties={ctx.caller.alias: ctx.caller.uid})
            if previous is None:
                store.prepare(RequestIntent("checkin.record", 1, ctx.host_uid, ctx.fleet_uid,
                                            ctx.caller.uid, None, semantic,
                                            (StagePlan("recording", (fact,)),)))
            elif previous.intent.stages[0].facts[0] != fact:
                raise CheckinError("conflict", "unrecorded check-in attempt does not rebuild exactly",
                                   hint=inspect)
            if read_selection(root) != selected:
                raise CheckinError("conflict", "active check-in selection changed", retryable=True,
                                   hint="retry the same --request-id")
            store.begin_attempt()
            store.stage(0)
            try:
                outcome = emit_batch(root, [raw], require_commit=True)
            except (OSError, sqlite3.Error) as exc:
                try:
                    proof = _proof(root, fact)
                except (OSError, sqlite3.Error):
                    proof = None
                if proof is not None and proof.status == "committed":
                    raise CheckinError("unavailable", "decision committed but recording result is unavailable",
                                       hint=inspect,
                                       result=_result(ctx, request_id, checkin_id,
                                                      persisted=False, replayed=False)) from exc
                raise CheckinError("unavailable", "check-in recording unconfirmed", hint=inspect) from exc
            if len(outcome) != 1 or outcome[0].event_id != event_id:
                raise CheckinError("unavailable", "check-in recording result unconfirmed", hint=inspect)
            proof = _proof(root, fact)
            if proof.status != "committed":
                raise CheckinError("conflict" if proof.status == "conflict" else "unavailable",
                                   "check-in exact recording proof is unavailable", hint=inspect)
            try:
                store.outcome(0, "committed")
            except OSError as exc:
                raise CheckinError("unavailable", "decision committed but request persistence failed",
                                   hint=inspect,
                                   result=_result(ctx, request_id, checkin_id,
                                                  persisted=False, replayed=False)) from exc
            return _result(ctx, request_id, checkin_id, persisted=True, replayed=False)
    except ReceiptBusy as exc:
        raise CheckinError("conflict", "check-in request is already being processed", retryable=True,
                           hint="retry the same --request-id after the other invocation exits") from exc
    except ReceiptError as exc:
        raise CheckinError("conflict", "check-in request receipt is invalid or conflicts",
                           hint=inspect) from exc
    except (OSError, sqlite3.Error) as exc:
        raise CheckinError("unavailable", "check-in request or Plane proof is unavailable",
                           hint=inspect) from exc
