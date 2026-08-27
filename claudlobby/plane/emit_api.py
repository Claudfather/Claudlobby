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

from . import PLANE_SCHEMA_VERSION
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


class CaptureConfigError(ContractViolation):
    """state/plane/capture.json exists but cannot be trusted — unreadable,
    invalid JSON, or an unknown mode value. An ABSENT file is the documented
    default ('metadata'); a BROKEN file must fail visibly, because silently
    falling back to metadata strips content an operator opted into keeping
    (F23 + the no-silent-switch rule). Routes like ContractViolation: loud,
    never spooled, CLI exit 2."""


def _load_capture_config(root: Path) -> dict:
    cfg = Path(root) / "state" / "plane" / "capture.json"
    try:
        text = cfg.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise CaptureConfigError(
            [{"loc": ("capture.json",), "msg": f"unreadable: {exc}"}]
        ) from exc
    try:
        modes = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CaptureConfigError(
            [{"loc": ("capture.json",), "msg": f"invalid JSON: {exc}"}]
        ) from exc
    # The WHOLE file must be valid, not just the looked-up key: a typo'd mode
    # on any fleet is a policy error someone believes is in force.
    if not isinstance(modes, dict) or not all(
        isinstance(k, str) and v in ("full", "metadata") for k, v in modes.items()
    ):
        raise CaptureConfigError(
            [{"loc": ("capture.json",),
              "msg": "must map fleet (or '*') to 'full' | 'metadata'"}]
        )
    return modes


def _capture_mode(root: Path, fleet: str | None) -> str:
    """Fleet-keyed capture mode from plane config; default 'metadata' (F7/F23).
    The caller's request never decides this."""
    modes = _load_capture_config(root)
    return modes.get(fleet or "", modes.get("*", "metadata"))


def _apply_capture(root: Path, raw: dict) -> dict:
    """Round-3 F8: the policy transforms EVERY content-bearing family
    (contracts.CONTENT_FIELDS is the registry's code form), not
    communications alone. Communications keep the proof triple on drop.

    IDENTITY CONTRACT (T8): returns the INPUT OBJECT ITSELF when the policy
    changed nothing — the caller uses `is` to skip the second validation pass
    for untransformed requests, which is the safe half of the #1345-review
    disclosure (warm emit 62->106ms from validating twice)."""
    fields = CONTENT_FIELDS.get(raw.get("event_type"))
    if not fields:
        return raw
    mode = _capture_mode(root, raw.get("fleet"))
    if raw.get("event_type") == "communication":
        payload = dict(raw.get("payload") or {})
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
        return {**raw, "payload": payload}
    if mode == "full":
        return raw                          # nothing to transform — identity
    payload = dict(raw.get("payload") or {})
    dropped = False
    for field in fields:
        if field in payload:
            payload.pop(field)              # dropped, no proof triple owed
            dropped = True
    if not dropped:
        return raw                          # metadata mode, no content present
    return {**raw, "payload": payload}


def _finalize(raw: dict) -> dict:
    out = dict(raw)
    if not out.get("event_id"):
        out["event_id"] = mint_event_id()
    if not out.get("occurred_at"):
        out["occurred_at"] = datetime.now(timezone.utc).isoformat()
    if not out.get("schema_version"):
        out["schema_version"] = PLANE_SCHEMA_VERSION
    return out


def emit_batch(root: Path, raw_requests: list[dict]) -> list[EmitOutcome]:
    """One atomic unit of work: validate ALL, then ONE transaction (F4).
    The dispatch door commits work_item + assignment + communication here.

    Order is a contract: RAW requests with REJECT semantics validate BEFORE
    capture transforms them, or an over-cap authored body (work_item.body,
    task summary — REJECT per §8/§11) is stripped by metadata mode first and
    sails through as accepted. Then the TRANSFORMED form — what gets stored
    and spooled (§11) — is what the transaction receives.

    T8-as-amended-by-#1372-F1: the double pass is paid only where both passes
    DO something, but RAW validation is unconditional and FIRST for every
    family — the T8 comms skip let capture launder malformed wire into valid
    shape. The second (transformed-form) pass runs only when capture actually
    changed the request (_apply_capture's identity contract); communications
    always change under capture, so they pay both passes."""
    finalized = [_finalize(dict(r)) if isinstance(r, dict) else r
                 for r in raw_requests]
    captured: list = []
    items = []
    for r in finalized:
        # RAW validation runs FIRST for EVERY family — #1372 review F1: the
        # T8 skip for communications let capture LAUNDER invalid wire (a
        # list-of-pairs payload, privacy="bogus") into a committed row,
        # because dict(payload) reshapes pairs and the privacy stamp
        # overwrites the invalid token. The T8 win survives where it was
        # measured (identity-return skips the second pass); communications
        # pay the double pass as the price of the capture rewrite.
        first = validate_request(r)                    # ContractViolation propagates
        c = _apply_capture(root, r)                    # CaptureConfigError propagates
        captured.append(c)
        items.append(first if c is r else validate_request(c))
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
        # The spool stores the policy-applied envelope, never a fuller body (§11).
        path = spool_write(root, captured, str(exc))    # raises SpoolWriteError
        return [
            EmitOutcome(r["event_id"], "spooled", detail=str(path))
            for r in captured
        ]
    return [
        EmitOutcome(res.event_id, "duplicate" if res.duplicate else "committed")
        for res in results
    ]


def emit(root: Path, raw_request: dict) -> EmitOutcome:
    return emit_batch(root, [raw_request])[0]
