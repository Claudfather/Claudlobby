"""Read-only proof for each atomic request batch, using ingest's own projection.

Only hashes and column names are retained in the request receipt. A ledger-only
duplicate (including retention-pruned payload) is not proof of the same effect.
No function here emits, creates identities, migrates, retries or sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from .request_receipts import ExpectedFact, FACT_FAMILIES, ReceiptError


def _hash(values: dict) -> str:
    values = dict(values)
    if values.get("detail") is not None and not values.get("detail_truncated"):
        # Ingest serializes JSON detail. Key order is not semantic identity.
        values["detail"] = json.loads(values["detail"])
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def expected_fact(item, *, host_uid: str, fleet_uid: str, parties: dict[str, str],
                  entities: dict[tuple[str, str], str] | None = None) -> ExpectedFact:
    """Project an already validated, capture-transformed item without a write.

    Callers freeze identities first. Missing aliases refuse instead of minting
    during preparation. Clock fields are observations, not retry semantics;
    schema, scope, provenance, links and every family field are included.
    Use emit_api.validate_item and its capture owner before calling this.
    """
    from .plane.ingest import _envelope, _family_values

    env, payload = item
    if not env.event_id:
        raise ReceiptError("freeze event IDs before preparing expected facts")
    try:
        _, values = _family_values(payload, parties.__getitem__,
                                   lambda kind, alias: (entities or {})[(kind, alias)])
    except KeyError as exc:
        raise ReceiptError("expected fact refers to an unfrozen identity") from exc
    envelope = _envelope(None, env.event_id, env, host_uid=host_uid,
                         fleet_uid=fleet_uid, now=None)
    for name in ("ingest_seq", "occurred_at", "observed_at", "ingested_at"):
        envelope.pop(name)
    projection = {**envelope, **values}
    return ExpectedFact(env.event_id, env.event_type, _hash(projection), tuple(sorted(projection)))


@dataclass(frozen=True)
class FactProof:
    status: str  # committed | unrecorded | unknown | conflict
    reason: str
    matched: int = 0


def reconcile_facts(conn: sqlite3.Connection, facts: tuple[ExpectedFact, ...]) -> FactProof:
    """Observe all facts in one snapshot; a partial atomic batch is a conflict.

    An unavailable query is unknown, never absence. Retained ledger rows with
    unavailable payload stay unknown and cannot authorize replay as new work.
    Caller must keep the request lock and any domain lock while acting on proof.
    """
    from .plane.ingest import CONSTRUCT_TABLES
    from .plane.contracts import WIRE_TO_KIND

    if not facts or len({fact.event_id for fact in facts}) != len(facts):
        raise ReceiptError("expected atomic batch must contain distinct facts")
    own_snapshot = False
    absent = matched = 0
    unavailable = False
    try:
        own_snapshot = not conn.in_transaction
        if own_snapshot:
            conn.execute("BEGIN")
        for fact in facts:
            if fact.family not in FACT_FAMILIES:
                raise ReceiptError("unsupported request fact family")
            table = CONSTRUCT_TABLES.get(fact.family, "events")
            ledger = conn.execute("SELECT rowid, family FROM ingest_ledger WHERE event_id=?",
                                  (fact.event_id,)).fetchone()
            cursor = conn.execute(f"SELECT * FROM {table} WHERE event_id=?", (fact.event_id,))
            raw = cursor.fetchone()
            row = dict(zip((column[0] for column in cursor.description), raw)) if raw is not None else None
            if ledger is None:
                if row is not None:
                    return FactProof("conflict", "family row has no ledger entry", matched)
                absent += 1
                continue
            if ledger[1] != fact.family:
                return FactProof("conflict", "event ID was committed as a different family", matched)
            if row is None:
                unavailable = True
                continue
            if (row["ingest_seq"] != ledger[0]
                    or (table == "events" and row["kind"] != WIRE_TO_KIND.get(fact.family, fact.family))):
                return FactProof("conflict", "ledger and family row disagree", matched)
            if not fact.fields or not {"event_id", "host_uid", "fleet_uid", "emitter"} <= set(fact.fields):
                raise ReceiptError("expected fact has no complete scoped projection")
            if not set(fact.fields) <= row.keys():
                return FactProof("unknown", "projection fields are unavailable in this schema", matched)
            if _hash({key: row[key] for key in fact.fields}) != fact.projection_sha256:
                return FactProof("conflict", "stored fact differs from prepared identities, links or content", matched)
            matched += 1
        if absent == len(facts):
            return FactProof("unrecorded", "all expected event IDs are absent")
        if absent:
            return FactProof("conflict", "only part of the expected atomic batch exists", matched)
        if unavailable:
            return FactProof("unknown", "ledger proves prior recording but payload proof is unavailable", matched)
        return FactProof("committed", "all expected facts match", matched)
    except (sqlite3.Error, OSError):
        return FactProof("unknown", "recording proof is unavailable", matched)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ReceiptError):
            raise
        return FactProof("conflict", "stored fact cannot be compared to its prepared projection", matched)
    finally:
        if own_snapshot:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except sqlite3.Error:
                pass  # unavailable proof was already disclosed; never write a repair
