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
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from . import PLANE_SCHEMA_VERSION
from .canonical import canonical_hash
from .contracts import (
    WIRE_TO_KIND,
    Assignment,
    Communication,
    ContractViolation,
    SystemEvent,
    TaskEvent,
    Transmission,
    WorkItem,
    Workstream,
    WorkstreamEvent,
    Declaration,
    ENTITY_IDENTITY_KIND,
    MetricSample,
    RegistrySnapshot,
)
from .identity import resolve, resolve_fleet, resolve_party
from .registries import METRIC_NAMES
from .ids import mint_event_id
from .registries import FIELD_POLICY, SYSTEM_EVENT_SEVERITY


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
    "workstream": "workstreams",
    "registry_snapshot": "registry_snapshots",
    "metric_sample": "metric_samples",
}
# Public alias: the trust surface derives its emitter-coverage roster from
# this registry (a hand-list drifted at birth — #1393 gauntlet), and a
# second consumer must not import a private name.
CONSTRUCT_TABLES = _CONSTRUCT_TABLE

# The wire->kind mapping lives in contracts.WIRE_TO_KIND (spec ruling #8),
# imported above — F4 was a consumer missing it; a private copy here was the
# next F4 waiting (the insert branch hardcoded the same fact separately).


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


def _batch_resolver(conn, now):
    """Per-batch identity memo (gauntlet round, measured): a 3-event dispatch
    batch made 8 resolve() calls for 3 unique aliases — each 3 SQL statements
    plus a last_seen UPDATE writing an identical value (now is fixed per
    batch), ~57% of warm emit_batch time. One resolve per (kind, alias) per
    batch; last_seen still advances once per batch, which is what it means."""
    memo: dict = {}

    def party(alias):
        key = ("party", alias)
        if key not in memo:
            memo[key] = resolve_party(conn, alias, now)
        return memo[key]

    def fleet(alias):
        key = ("fleet", alias)
        if key not in memo:
            memo[key] = resolve_fleet(conn, alias, now)
        return memo[key]

    def entity(kind, alias):
        # kind-explicit resolution for the registry lane (Phase 2b): entity
        # snapshots and metric subjects name kinds the party() inference
        # cannot (host, vault, bot_instance, project, library_item)
        key = (kind, alias)
        if key not in memo:
            memo[key] = resolve(conn, kind, alias, now=now)
        return memo[key]

    return party, fleet, entity


def _family_values(payload, party) -> tuple[str, dict]:
    """(table, family-column dict) — no SQL here; _insert builds it.
    `party` is the batch-scoped alias->uid resolver from _batch_resolver."""
    if isinstance(payload, Communication):
        return "communications", {
            "msg_id": payload.msg_id,
            "sender_uid": party(payload.sender),
            "sender_alias": payload.sender,
            "sender_session_uid": payload.sender_session_uid,
            "recipient_uid": (
                party(payload.recipient)
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
            "created_by_uid": party(payload.created_by),
            "workstream_id": payload.workstream_id,
            "repo": payload.repo,
            "project_key": payload.project_key,
            "body": payload.body,
        }
    if isinstance(payload, Assignment):
        return "assignments", {
            "assignment_id": payload.assignment_id,
            "work_item_id": payload.work_item_id,
            "assignee_uid": party(payload.assignee),
            "assigned_by_uid": party(payload.assigned_by),
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
                party(payload.actor) if payload.actor else None
            ),
            "session_uid": payload.session_uid,
            "deadline": payload.deadline.isoformat() if payload.deadline else None,
            "successor_id": payload.successor_id,
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    if isinstance(payload, Workstream):
        return "workstreams", {
            "workstream_id": payload.workstream_id,
            "title": payload.title,
            "goal": payload.goal,
            "owner_uid": (
                party(payload.owner) if payload.owner else None
            ),
            "opened_by_uid": party(payload.opened_by),
            "project_key": payload.project_key,
        }
    if isinstance(payload, WorkstreamEvent):
        detail = {
            k: v for k, v in {
                "note": payload.note,
                "next_step": payload.next_step,
                "disposition": payload.disposition,
                "plan_ref": payload.plan_ref,
            }.items() if v is not None
        }
        return "events", {
            "kind": WIRE_TO_KIND["workstream_event"],
            "event": payload.event,
            "workstream_id": payload.workstream_id,
            "actor_uid": (
                party(payload.actor) if payload.actor else None
            ),
            "renewed_until": (
                payload.renewed_until.isoformat()
                if payload.renewed_until else None
            ),
            "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
            "detail_truncated": 0,
        }
    if isinstance(payload, SystemEvent):
        # severity: registry-stamped, never caller-supplied (§9b — the wire
        # model has no severity field at all); unknown token => NULL.
        detail = (
            json.dumps(payload.data, ensure_ascii=False)
            if payload.data else None
        )
        truncated = 0
        if detail is not None:
            # DIAGNOSTIC over-cap TRUNCATES with the flag (§9b) — never
            # rejects. A flagged detail is a raw UTF-8 prefix, not JSON;
            # detail_truncated=1 IS the reader's parse guard.
            cap = FIELD_POLICY[("system", "data")]["cap"]
            raw = detail.encode("utf-8")
            if len(raw) > cap:
                detail = raw[:cap].decode("utf-8", errors="ignore")
                truncated = 1
        return "events", {
            "kind": "system",
            "event": payload.event,
            "severity": SYSTEM_EVENT_SEVERITY.get(payload.event),
            "subject_kind": payload.subject_kind,
            "subject_uid": payload.subject_uid,
            "subject_alias": payload.subject_alias,
            "detail": detail,
            "detail_truncated": truncated,
        }
    raise TypeError(f"no insert mapping for {type(payload).__name__}")


def _confirm(conn: sqlite3.Connection, uid: str) -> None:
    """Phase 1's identity loop closes here (spec §18): the registry OBSERVED
    the declared entity, so its lazily-minted identity stops being
    provisional. A sanctioned UPDATE (§9b mutation surface)."""
    conn.execute(
        "UPDATE identity_registry SET provisional = 0 WHERE uid = ?", (uid,))


def _registry_row(conn, payload, entity, host_uid):
    """(entity_uid, values-or-None): None = hash-suppressed. Resolution +
    confirmation happen even when suppressed — the scan observed the entity,
    and provenance rides the never-gated declaration events."""
    kind = ENTITY_IDENTITY_KIND[payload.entity_type]
    uid = entity(kind, payload.entity_alias)
    if not payload.tombstone:
        # Confirmation is for OBSERVED entities only — a tombstone is the
        # opposite claim, and confirming it silenced the exact provisional
        # signal doctor watches; a tombstone for a never-seen alias even
        # minted-and-confirmed a ghost (gauntlet r1, probed).
        _confirm(conn, uid)
        if payload.entity_type == "bot":
            # the logical actor is confirmed ALONGSIDE its instance (§18)
            _confirm(conn, entity("actor", payload.entity_alias))
    values = {
        "entity_type": payload.entity_type,
        "entity_uid": uid,
        "entity_alias": payload.entity_alias,
        "tombstone": 1 if payload.tombstone else 0,
        "payload": None,
        "payload_hash": None,
        "cause": payload.cause,
        "scan_id": payload.scan_id,
        "vault_rev": payload.vault_rev,
    }
    prev = conn.execute(
        "SELECT payload_hash, tombstone FROM registry_snapshots"
        " WHERE host_uid = ? AND entity_type = ? AND entity_uid = ?"
        " ORDER BY ingest_seq DESC LIMIT 1",
        (host_uid, payload.entity_type, uid)).fetchone()
    # EVERY suppression decision below asks the READER'S question through
    # one definition (chunk-B gauntlet SEV-1 + r3 — see the derivation at
    # queries.REG_CURRENT_POINT_SQL). Ledger-latest (`prev`) keyed both
    # decisions before, and both broke: an invalid tombstone suppressed
    # every later valid deletion, and a stale-clock backfill row made the
    # gate suppress every honest rescan while the reader served the stale
    # payload indefinitely.
    from .registry_read import current_hash
    if payload.tombstone:
        if prev and prev["tombstone"]:
            # (r3 disclosure lives here: duplicate same-scan tombstones
            # BEFORE their completion both commit — at each evaluation the
            # entity genuinely is still current, the emitter cannot
            # produce the shape, and post-completion duplicates suppress.
            # Row spam only, accepted.)
            try:
                still_current = current_hash(
                    conn, host_uid, payload.entity_type, uid) is not None
            except sqlite3.Error as exc:
                # unreadable ≠ deleted: fail toward WRITING (append-only,
                # still needs a completion to be honored) rather than
                # raising — a raise here loses the whole batch unspooled
                # for probe/equip/migration causes (r3, disclosed)
                print(f"plane ingest: registry current-check unreadable"
                      f" ({exc}) — writing the tombstone", file=sys.stderr)
                still_current = True
            if not still_current:
                return uid, None   # already gone — nothing to re-claim
        return uid, values
    phash = canonical_hash(_gate_view(payload.payload))
    try:
        cur = current_hash(conn, host_uid, payload.entity_type, uid)
    except sqlite3.Error as exc:
        # the SNAPSHOT path needs the same armor as the tombstone path
        # above (r4, probed: the F11 join parses every completion detail
        # whenever the partition holds a tombstone, so ONE corrupt row
        # made a whole generate batch raise unspooled — clean keyframes
        # lost with it). Unreadable gate ⇒ treat as changed and WRITE:
        # append-only, and the next healthy read self-heals.
        print(f"plane ingest: registry gate unreadable ({exc}) — writing"
              f" the keyframe", file=sys.stderr)
        cur = None
    if cur == phash:
        return uid, None    # the write gate: unchanged per the READER
    values["payload"] = json.dumps(payload.payload, ensure_ascii=False)
    values["payload_hash"] = phash
    return uid, values


def _gate_view(payload: dict) -> dict:
    """The hash gate's view of a payload: everything EXCEPT vault_rev. The
    vault takes commits daily (memories, captures) and generate runs daily,
    so a rev inside the hashed bytes turned every vault commit into a full
    keyframe set per fleet (gauntlet r1, measured) — blowing the spec's
    tens-of-rows/week envelope. Provenance is exactly what revision_seen
    declarations exist for, and vault_rev remains a stored COLUMN; only the
    gate ignores it."""
    return {k: v for k, v in payload.items() if k != "vault_rev"}


_warned_metrics: set[str] = set()


def _metric_row(payload, entity):
    if (payload.metric not in METRIC_NAMES
            and payload.metric not in _warned_metrics):
        # open registry, warn-on-unknown (§9d): accepted, never rejected —
        # additions arrive by PR; the warning is the drift signal. ONCE per
        # metric per process: a misnamed probe at sample volume would
        # otherwise write 30-45k lines/day into the daemon's stderr on the
        # SD-fragile Pi (gauntlet r2).
        _warned_metrics.add(payload.metric)
        print(f"plane-ingest: unknown metric {payload.metric!r}"
              " (not in registries.METRIC_NAMES)", file=sys.stderr)
    return {
        "subject_kind": payload.subject_kind,
        "subject_uid": entity(payload.subject_kind, payload.subject),
        "metric": payload.metric,
        "value": json.dumps(payload.value, ensure_ascii=False),
        "status": payload.status,
    }


def _declaration_row(payload, entity):
    if payload.event == "revision_seen":
        detail = {"vault_rev": payload.vault_rev}
    else:
        detail = {k: v for k, v in {
            "scan_id": payload.scan_id, "scope": payload.scope,
            "counts": payload.counts, "complete": payload.complete,
            "source_rev": payload.source_rev}.items() if v is not None}
    return {
        "kind": "declaration",
        "event": payload.event,
        "subject_kind": payload.subject_kind,
        "subject_uid": entity(payload.subject_kind, payload.subject),
        "subject_alias": payload.subject,
        "detail": json.dumps(detail, ensure_ascii=False),
        "detail_truncated": 0,
    }


def ingest_many(conn, items, *, host_uid) -> list[IngestResult]:
    """items: [(EmitRequest, payload)] — ONE transaction, all-or-nothing."""
    now = now_iso()
    prepared = [
        (env.event_id or mint_event_id(), env, payload)
        for env, payload in items
    ]
    # Intra-batch collision is a CONTRACT verdict, not transport (gauntlet
    # round, probed): the same event_id twice in one batch used to collide on
    # the second INSERT, roll back the first, then fail duplicate verification
    # with "missing from ledger — mixed state" — daemon said `internal`,
    # client exited 5, and the shim replayed a deterministic error through the
    # cold CLI.
    seen_ids: dict[str, int] = {}
    for idx, (event_id, _env, _payload) in enumerate(prepared):
        if event_id in seen_ids:
            raise ContractViolation(
                [{"loc": ("event_id",),
                  "msg": f"intra-batch duplicate event_id {event_id}"
                         f" (items {seen_ids[event_id]} and {idx})"}]
            )
        seen_ids[event_id] = idx
    party, fleet, entity = _batch_resolver(conn, now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        results = []
        for event_id, env, payload in prepared:
            fam_override = None
            if isinstance(payload, RegistrySnapshot):
                _uid, fam_override = _registry_row(
                    conn, payload, entity, host_uid)
                if fam_override is None:
                    # hash-suppressed: unchanged resolved state writes
                    # NOTHING (no ledger row — replay verification must
                    # never find a ledger entry with no family row).
                    # Reported as duplicate: same state observed again.
                    results.append(IngestResult(event_id, None, True))
                    continue
                table_override = "registry_snapshots"
            elif isinstance(payload, MetricSample):
                fam_override = _metric_row(payload, entity)
                table_override = "metric_samples"
            elif isinstance(payload, Declaration):
                fam_override = _declaration_row(payload, entity)
                table_override = "events"
            cur = conn.execute(
                "INSERT INTO ingest_ledger (event_id, family, ingested_at)"
                " VALUES (?, ?, ?)",
                (event_id, env.event_type, now),
            )
            seq = cur.lastrowid
            fleet_uid = fleet(env.fleet) if env.fleet else None
            base = _envelope(
                seq, event_id, env,
                host_uid=host_uid, fleet_uid=fleet_uid, now=now,
            )
            if fam_override is not None:
                table, fam = table_override, fam_override
            else:
                table, fam = _family_values(payload, party)
            _insert(conn, table, {**base, **fam})
            results.append(IngestResult(event_id, seq, False))
        conn.execute("COMMIT")
        return results
    except sqlite3.IntegrityError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "ingest_ledger.event_id" in str(exc):
            return _verify_duplicates(conn, prepared, host_uid)
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _suppressed_on_replay(conn, payload, host_uid) -> bool:
    """Deterministic re-evaluation of the hash gate for a ledger-less
    RegistrySnapshot met during duplicate verification: a retried batch
    (ack lost) legitimately contains events whose first attempt was
    hash-SUPPRESSED — no ledger row exists BY DESIGN, not by corruption.
    Read-only (SELECT, never resolve/mint): the first attempt's commit
    persisted the identities; absence here is genuine mixed state."""
    if not isinstance(payload, RegistrySnapshot) or payload.tombstone:
        return False
    kind = ENTITY_IDENTITY_KIND[payload.entity_type]
    row = conn.execute(
        "SELECT uid FROM identity_registry WHERE kind = ? AND alias = ?",
        (kind, payload.entity_alias)).fetchone()
    if row is None:
        return False
    prev = conn.execute(
        "SELECT payload_hash, tombstone FROM registry_snapshots"
        " WHERE host_uid = ? AND entity_type = ? AND entity_uid = ?"
        " ORDER BY ingest_seq DESC LIMIT 1",
        (host_uid, payload.entity_type, row["uid"])).fetchone()
    return bool(prev and not prev["tombstone"]
                and prev["payload_hash"]
                == canonical_hash(_gate_view(payload.payload)))


def _verify_duplicates(conn, prepared, host_uid) -> list[IngestResult]:
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
            if _suppressed_on_replay(conn, payload, host_uid):
                results.append(IngestResult(event_id, None, True))
                continue
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
                (event_id, WIRE_TO_KIND.get(family, family)),
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
