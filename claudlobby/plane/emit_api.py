"""emit(): the programmatic spine every writer uses (design v2 §5; round-2 v2.1).

Failure taxonomy is the contract:
  ContractViolation  -> caller bug: propagate, write NOTHING (not even spool)
  DowngradeError     -> db newer than code: propagate LOUDLY, never spooled
  OperationalError accepted by is_retryable() -> spool + report spooled;
  all other database errors -> propagate loudly
  spool also failed  -> SpoolWriteError (CLI exit 3)

occurred_at is finalized BEFORE the first db attempt (round-2 F6) so a
spooled replay preserves event time and spool lag stays measurable as
ingested_at - occurred_at. Capture policy is resolved from plane config
keyed by fleet — NEVER from the caller's request (F23): in metadata mode
the body is dropped at the door with its proof triple retained.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from .contracts import (
    CONTENT_FIELDS,
    ContractViolation,
    cap_body,
    validate_request,
)
from .db import connect, db_path
from .ids import ensure_host_uid, mint_event_id
from .ingest import ingest_many
from .migrations import DowngradeError, migrate
from .spool import SpoolWriteError, is_retryable, spool_write


@dataclass(frozen=True)
class EmitOutcome:
    event_id: str
    status: Literal["committed", "duplicate", "spooled"]
    detail: Optional[str] = None


def _capture_mode(root: Path, fleet: str | None) -> str:
    """Fleet-keyed capture mode from plane config; default 'metadata' (F7/F23).
    The caller's request never decides this."""
    cfg = Path(root) / "state" / "plane" / "capture.json"
    try:
        modes = json.loads(cfg.read_text())
    except (OSError, json.JSONDecodeError):
        return "metadata"
    mode = modes.get(fleet or "", modes.get("*", "metadata"))
    return mode if mode in ("full", "metadata") else "metadata"


def _apply_capture(root: Path, raw: dict) -> dict:
    """Round-3 F8: the policy transforms EVERY content-bearing family
    (contracts.CONTENT_FIELDS is the registry's code form), not
    communications alone. Communications keep the proof triple on drop."""
    fields = CONTENT_FIELDS.get(raw.get("event_type"))
    if not fields:
        return raw
    mode = _capture_mode(root, raw.get("fleet"))
    payload = dict(raw.get("payload") or {})
    if raw.get("event_type") == "communication":
        if mode == "full":
            payload["privacy"] = "full"
        else:
            body = payload.get("body")
            payload["privacy"] = "metadata"
            if body is not None:
                proof = cap_body(body)
                payload["body"] = None      # dropped AT THE DOOR (F23)
                payload["body_bytes"] = proof.body_bytes
                payload["body_sha256"] = proof.body_sha256
                payload["truncated"] = proof.truncated
    elif mode != "full":
        for field in fields:
            payload.pop(field, None)        # dropped, no proof triple owed
    return {**raw, "payload": payload}


def _finalize(raw: dict) -> dict:
    out = dict(raw)
    if not out.get("event_id"):
        out["event_id"] = mint_event_id()
    if not out.get("occurred_at"):
        out["occurred_at"] = datetime.now(timezone.utc).isoformat()
    return out


def emit_batch(root: Path, raw_requests: list[dict]) -> list[EmitOutcome]:
    """One atomic unit of work: validate ALL, then ONE transaction (F4).
    The dispatch door commits work_item + assignment + communication here."""
    finalized = [_finalize(_apply_capture(root, r)) for r in raw_requests]
    items = [validate_request(r) for r in finalized]   # ContractViolation propagates
    try:
        conn = connect(db_path(root))
        try:
            migrate(conn)                               # DowngradeError propagates
            host = ensure_host_uid(Path(root) / "state")
            results = ingest_many(conn, items, host_uid=host)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                # Post-review fix: a WAL-flush failure on close, AFTER a
                # successful commit, must not fall into the spool path —
                # that reported committed events as "spooled" and queued a
                # redundant replay. A close failure after a FAILED ingest
                # changes nothing (that exception already routed).
                pass
    except (DowngradeError, ContractViolation):
        raise
    except sqlite3.OperationalError as exc:
        # Spool ONLY whitelisted-retryable codes (round-4 F6): IntegrityError
        # never lands here (a bug, propagates), and a missing table / SQL typo
        # — OperationalError but equally bugs — propagate loudly too.
        if not is_retryable(exc):
            raise
        path = spool_write(root, finalized, str(exc))   # raises SpoolWriteError
        return [
            EmitOutcome(r["event_id"], "spooled", detail=str(path))
            for r in finalized
        ]
    return [
        EmitOutcome(res.event_id, "duplicate" if res.duplicate else "committed")
        for res in results
    ]


def emit(root: Path, raw_request: dict) -> EmitOutcome:
    return emit_batch(root, [raw_request])[0]
