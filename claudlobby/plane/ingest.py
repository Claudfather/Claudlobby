"""The one transactional write path (design v2 §5; round-2 F1 rewrite).

ingest_many() is the SOLE transaction owner: BEGIN IMMEDIATE, ledger+family
inserts for every item, COMMIT — helpers never manage transactions (the
nested-`with` early commit is the probe-confirmed lost-event class). Every
INSERT is built from a column dict, so the placeholder count is right by
construction — hand arithmetic is banned. Duplicate event_id replay is
SUCCESS only after verifying ledger AND family rows both exist; a ledger row
without its family row is corruption and RAISES, never absorbs.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import PLANE_SCHEMA_VERSION
from .contracts import (
    Assignment,
    Communication,
    ContractViolation,
    SystemEvent,
    TaskEvent,
    Transmission,
    WorkItem,
)
from .identity import resolve_fleet, resolve_party
from .ids import mint_event_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    ingest_seq: int | None
    duplicate: bool


_CONSTRUCT_TABLE = {
    "communication": "communications",
    "work_item": "work_items",
    "assignment": "assignments",
}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    cols = tuple(values)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' * len(cols))})",
        tuple(values.values()),
    )


def _envelope(seq, event_id, env, *, host_uid, fleet_uid, now) -> dict:
    return {
        "ingest_seq": seq,
        "event_id": event_id,
        # Preserve the wire's version verbatim (§10) — a drained N-1 envelope
        # must not be restamped as current; None only for direct ingest_many
        # callers that never crossed emit's finalize.
        "schema_version": env.schema_version or PLANE_SCHEMA_VERSION,
        "occurred_at": env.occurred_at.isoformat() if env.occurred_at else now,
        "observed_at": env.observed_at.isoformat() if env.observed_at else None,
        "ingested_at": now,
        "host_uid": host_uid,
        "fleet_uid": fleet_uid,
        "emitter": env.emitter,
        "source_ref": env.source_ref,
        "correlation_id": env.correlation_id,
        "causation_id": env.causation_id,
        "trace_id": env.trace_id,
        "span_id": env.span_id,
        "origin": env.origin,
        "import_batch": env.import_batch,
        "confidence": env.confidence,
    }


def _family_values(conn, payload, now) -> tuple[str, dict]:
    """(table, family-column dict) — no SQL here; _insert builds it."""
    if isinstance(payload, Communication):
        return "communications", {
            "msg_id": payload.msg_id,
            "sender_uid": resolve_party(conn, payload.sender, now),
            "sender_alias": payload.sender,
            "sender_session_uid": payload.sender_session_uid,
            "recipient_uid": (
                resolve_party(conn, payload.recipient, now)
                if payload.recipient else None
            ),
            "recipient_alias": payload.recipient,
            "recipient_raw": payload.recipient_raw,
            "message_class": payload.message_class,
            "command_type": payload.command_type,
            "work_item_id": payload.work_item_id,
            "assignment_id": payload.assignment_id,
            "workstream_id": payload.workstream_id,
            "deliberation_id": payload.deliberation_id,
            "reply_to_msg_id": payload.reply_to_msg_id,
            "supersedes_msg_id": payload.supersedes_msg_id,
            "body": payload.body,
            "body_bytes": payload.body_bytes,
            "body_sha256": payload.body_sha256,
            "truncated": int(payload.truncated),
            "privacy": payload.privacy,
            "idempotency_key": payload.idempotency_key,
        }
    if isinstance(payload, WorkItem):
        return "work_items", {
            "work_item_id": payload.work_item_id,
            "title": payload.title,
            "created_by_uid": resolve_party(conn, payload.created_by, now),
            "workstream_id": payload.workstream_id,
            "repo": payload.repo,
            "project_key": payload.project_key,
            "body": payload.body,
        }
    if isinstance(payload, Assignment):
        return "assignments", {
            "assignment_id": payload.assignment_id,
            "work_item_id": payload.work_item_id,
            "assignee_uid": resolve_party(conn, payload.assignee, now),
            "assigned_by_uid": resolve_party(conn, payload.assigned_by, now),
            "expected_by": (
                payload.expected_by.isoformat() if payload.expected_by else None
            ),
            "dispatch_msg_id": payload.dispatch_msg_id,
        }
    if isinstance(payload, Transmission):
        detail = {
            k: v for k, v in {
                "destination": payload.destination,
                "error": payload.error,
                "part_no": payload.part_no,
                "part_count": payload.part_count,
            }.items() if v is not None
        }
        return "events", {
            "kind": "transmission",
            "event": payload.state,
            "carrier": payload.carrier,
            "attempt_no": payload.attempt_no,
            "carrier_ref": payload.carrier_ref,
            "msg_id": payload.msg_id,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    if isinstance(payload, TaskEvent):
        detail = {
            k: v for k, v in {
                "progress": payload.progress,
                "summary": payload.summary,
                "pr_url": payload.pr_url,
            }.items() if v is not None
        }
        return "events", {
            "kind": "task",
            "event": payload.event,
            "work_item_id": payload.work_item_id,
            "assignment_id": payload.assignment_id,
            "actor_uid": (
                resolve_party(conn, payload.actor, now) if payload.actor else None
            ),
            "session_uid": payload.session_uid,
            "deadline": payload.deadline.isoformat() if payload.deadline else None,
            "successor_id": payload.successor_id,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    if isinstance(payload, SystemEvent):
        return "events", {
            "kind": "system",
            "event": payload.event,
            "severity": payload.severity,
            "subject_kind": payload.subject_kind,
            "subject_uid": payload.subject_uid,
            "subject_alias": payload.subject_alias,
            "detail": (
                json.dumps(payload.data, ensure_ascii=False)
                if payload.data else None
            ),
            "detail_truncated": 0,
        }
    raise TypeError(f"no insert mapping for {type(payload).__name__}")


def ingest_many(conn, items, *, host_uid) -> list[IngestResult]:
    """items: [(EmitRequest, payload)] — ONE transaction, all-or-nothing."""
    now = now_iso()
    prepared = [
        (env.event_id or mint_event_id(), env, payload)
        for env, payload in items
    ]
    try:
        conn.execute("BEGIN IMMEDIATE")
        results = []
        for event_id, env, payload in prepared:
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, ?, ?)",
                (event_id, env.event_type, now),
            )
            seq = cur.lastrowid
            fleet_uid = resolve_fleet(conn, env.fleet, now) if env.fleet else None
            base = _envelope(
                seq, event_id, env,
                host_uid=host_uid, fleet_uid=fleet_uid, now=now,
            )
            table, fam = _family_values(conn, payload, now)
            _insert(conn, table, {**base, **fam})
            results.append(IngestResult(event_id, seq, False))
        conn.execute("COMMIT")
        return results
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "ingest_ledger.event_id" in str(exc):
            return _verify_duplicates(conn, prepared)
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _verify_duplicates(conn, prepared) -> list[IngestResult]:
    """Duplicate replay = success ONLY if every event landed FULLY before AND
    AS THE SAME THING: ledger row present, family row present (round-2 F1),
    the incoming event_type EQUAL to the stored family, and the stored row's
    ingest_seq matching the ledger's. Without the family comparison, replaying
    a task under a communication's event_id reported "duplicate" while zero
    task rows existed — an idempotency conflict wearing a success."""
    results = []
    for event_id, env, payload in prepared:
        ledger = conn.execute(
            "SELECT rowid AS seq, family FROM ingest_ledger WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if ledger is None:
            raise RuntimeError(
                f"duplicate-classification refused: {event_id} missing from"
                " ledger while the batch collided — mixed state"
            )
        family = ledger["family"]
        if env.event_type != family:
            raise ContractViolation(
                [{"loc": ("event_id",),
                  "msg": f"idempotency conflict: {event_id} already stored as"
                         f" {family!r}, replayed as {env.event_type!r}"}]
            )
        table = _CONSTRUCT_TABLE.get(family, "events")
        if table == "events":
            fam = conn.execute(
                "SELECT ingest_seq FROM events WHERE event_id = ? AND kind = ?",
                (event_id, family),
            ).fetchone()
        else:
            fam = conn.execute(
                f"SELECT ingest_seq FROM {table} WHERE event_id = ?", (event_id,)
            ).fetchone()
        if fam is None:
            raise RuntimeError(
                f"ledger/family divergence for {event_id} — refusing"
                " duplicate classification (integrity, not idempotency)"
            )
        if fam["ingest_seq"] != ledger["seq"]:
            raise RuntimeError(
                f"ledger/family ingest_seq divergence for {event_id}"
                f" (ledger {ledger['seq']}, family {fam['ingest_seq']})"
                " — refusing duplicate classification"
            )
        results.append(IngestResult(event_id, None, True))
    return results


def ingest(conn, env, payload, *, host_uid) -> IngestResult:
    return ingest_many(conn, [(env, payload)], host_uid=host_uid)[0]
