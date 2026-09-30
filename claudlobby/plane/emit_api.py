"""emit(): the programmatic spine every writer uses (design v2 §5; round-2 v2.1).

Failure taxonomy is the contract:
  ContractViolation  -> caller bug: propagate, write NOTHING (not even spool)
  DowngradeError     -> db newer than code: propagate LOUDLY, never spooled
  OperationalError accepted by is_retryable() -> spool + report spooled,
    or propagate when require_commit=True (never queued for replay)
  all other database errors -> propagate loudly
  spool also failed  -> SpoolWriteError (CLI exit 3)

occurred_at is finalized BEFORE the first db attempt (round-2 F6) so a
spooled replay preserves event time and spool lag stays measurable as
ingested_at - occurred_at. Capture policy is resolved from plane config
keyed by fleet — NEVER from the caller's request (F23): in metadata mode
the body is dropped at the door with its proof triple retained.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from . import PLANE_SCHEMA_VERSION
from . import capture_policy
from .capture_policy import DEFAULT_CAPTURE, CaptureConfigInvalid
from .contracts import (
    CONTENT_FIELDS,
    ContractViolation,
    validate_request,
)
from .db import connect, db_file
from .ids import ensure_host_uid, mint_event_id
from .ingest import ingest_many
from .migrations import DowngradeError
from .schema_state import preflight_schema, require_current_schema
from .spool import SpoolWriteError, is_retryable, is_transient_lock, spool_write


@dataclass(frozen=True)
class EmitOutcome:
    event_id: str
    status: Literal["committed", "duplicate", "spooled"]
    detail: Optional[str] = None


class CaptureConfigError(CaptureConfigInvalid, ContractViolation):
    """state/plane/capture.json exists but cannot be trusted — unreadable,
    invalid JSON, or an unknown mode value. An ABSENT file is the documented
    default (:data:`DEFAULT_CAPTURE`); a BROKEN file must fail visibly rather
    than resolving to ANY mode. The direction that bites moved with the default
    (2026-09-20): under `metadata` a silent fallback stripped content an
    operator opted INTO keeping; under `full` it would STORE content an
    operator opted OUT of keeping (F23 + the no-silent-switch rule). Routes
    like ContractViolation: loud, never spooled, CLI exit 2. It is an
    environment fault, not a batch fault: staged replay keeps the batch
    (S5a-04) rather than quarantining it."""


def _load_capture_config(root: Path) -> dict:
    # One reader and one rule (capture_policy), shared with the stdlib
    # socket client that applies it before staging.
    try:
        return capture_policy.load_capture_config(root)
    except CaptureConfigInvalid as exc:
        raise CaptureConfigError(exc.errors) from exc


#: The shipped capture policy when nothing is configured. `full` since
#: 2026-09-20: the channel IS the product, and under `metadata` every message
#: renders "captured as metadata only (N bytes)" forever — the ledger is
#: append-only, so a default that strips bodies does not merely hide the words,
#: it destroys them at the door for every operator who never learned the knob
#: exists. Measured on the second host: 4,506 events and 233 communications
#: recorded, not one of them legible, on an install nobody had misconfigured.
#:
#: What the default trades: bodies are stored in the host's own SQLite ledger,
#: read back only by the localhost-bound view. Nothing is transmitted, and the
#: content is the operator's own agents talking to each other. An operator who
#: wants shapes without words opts out per fleet or host-wide in
#: state/plane/capture.json ({"*": "metadata"}). That choice is not folklore:
#: `claudlobby plane doctor`'s "capture config" rung states the default and the
#: opt-out, the view's fleet cards render "<mode> capture" on every fleet, and
#: the trust surface carries the resolved mode per fleet. A broken file still
#: fails LOUD rather than resolving to either mode (CaptureConfigError) — that
#: refusal matters more under a `full` default, not less, because a silent
#: fallback would now STORE content an operator opted out of keeping.
#: DEFAULT_CAPTURE itself is owned by capture_policy and re-exported here.

#: Fleet-keyed capture mode (F7/F23, re-ruled 2026-09-20); capture_policy owns it.
_capture_mode = capture_policy.capture_mode


# Public aliases: the trust surface (view.py) is a second consumer of the
# capture policy — reader and mode-resolution rule alike must have ONE
# definition, or the panel silently disagrees with the recorder about what
# policy is in force (the words-vs-metadata knob, where disagreement bites).
load_capture_config = _load_capture_config
capture_mode = _capture_mode


#: Round-3 F8: the policy transforms EVERY content-bearing family, with the
#: T8 identity contract (the caller uses `is` to skip the second validation
#: pass). One owner, capture_policy, which the socket client also applies.
_apply_capture = capture_policy.apply_capture


def _finalize(raw: dict) -> dict:
    out = dict(raw)
    if not out.get("event_id"):
        out["event_id"] = mint_event_id()
    if not out.get("occurred_at"):
        out["occurred_at"] = datetime.now(timezone.utc).isoformat()
    if not out.get("schema_version"):
        out["schema_version"] = PLANE_SCHEMA_VERSION
    return out


def validate_item(finalized: dict, modes: dict):
    """ONE item's validation, exactly as the batch door does it — RAW first,
    then the capture-transformed form when capture changed it. Returns
    ``(validated, captured)``; ``captured`` is what ingest and the spool
    receive. RAW validation runs FIRST for EVERY family (#1372 review F1):
    the T8 skip for communications let capture LAUNDER invalid wire (a
    list-of-pairs payload, privacy="bogus") into a committed row, and an
    over-cap authored field with REJECT semantics (task summary, work_item
    body) is STRIPPED by metadata mode, so validating only the captured
    form would accept it. The second pass is paid only when capture actually
    changed the request (``_apply_capture``'s identity contract). Public so
    a batch PRODUCER (the legacy importer) can refuse one unit instead of
    discovering the refusal when the whole batch aborts here."""
    first = validate_request(finalized)
    if CONTENT_FIELDS.get(finalized.get("event_type")):
        c = _apply_capture(finalized, modes)
    else:
        c = finalized
    return (first if c is finalized else validate_request(c)), c


# A retryable lock is retried this many times, with a growing pause, before a
# batch is spooled (fresh-plane first-writer races; see emit_batch).
LOCK_RETRY_ATTEMPTS = 6
LOCK_RETRY_BACKOFF_S = 0.15


def emit_batch(root: Path, raw_requests: list[dict], *,
               conn_factory: "Callable[[], sqlite3.Connection] | None" = None,
               require_commit: bool = False,
               precondition: "Callable[[sqlite3.Connection], None] | None" = None,
               ) -> list[EmitOutcome]:
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
    always change under capture, so they pay both passes.

    ``require_commit`` is for conditional mutations whose preconditions must
    not be replayed later. It preserves in-process lock retries but never
    writes a spool or staging entry. Storage failures propagate unchanged;
    an exception does not prove that a commit did not occur. Callers must
    reconcile their durable event IDs before deciding whether to retry.
    ``precondition`` runs read-only under ingest_many's BEGIN IMMEDIATE lock,
    before any row in the batch is written. It requires ``require_commit``:
    a spool cannot retain the in-process condition for a later replay."""
    if precondition is not None and not require_commit:
        raise ValueError("precondition requires require_commit=True")
    captured: list = []
    items = []
    # Capture config loads AT MOST ONCE per batch (gauntlet round): a report
    # batch used to stat+read+re-validate capture.json per content-bearing
    # event. Lazy, not eager, so a content-free batch (pure transmissions)
    # keeps succeeding under a broken capture.json exactly as before.
    modes: dict | None = None
    for raw in raw_requests:
        r = _finalize(dict(raw)) if isinstance(raw, dict) else raw
        if modes is None and CONTENT_FIELDS.get(r.get("event_type")):
            modes = _load_capture_config(root)         # CaptureConfigError propagates
        item, c = validate_item(r, modes or {})        # ContractViolation propagates
        captured.append(c)
        items.append(item)
    # A caller-supplied connection is USED AND NOT CLOSED: its owner holds the
    # lifecycle and the checkpoint cadence (#1693 arm D). Without one this is
    # byte-for-byte today's behaviour -- check schema, connect, ingest, close -- which
    # is what the cold CLI needs, being a fresh process per batch whose close is
    # necessarily the last-connection close.
    #
    # The durability difference rides on the CONNECTION, not on this branch: a
    # long-lived connection is opened `synchronous=FULL`, so its commits fsync
    # the WAL and an acknowledgment is durable without any checkpoint. A
    # per-batch connection stays NORMAL and is made durable by the truncate
    # checkpoint its close triggers. Both paths acknowledge only after a
    # commit that is on disk; they differ in which syscall put it there.
    # A FACTORY, never a ready-made connection. Passing the connection itself
    # makes the caller open the db in the ARGUMENT LIST -- before `emit_batch`
    # is entered, and therefore before the capture-config load and validation
    # that precede the connect here. That reordering is not cosmetic: on a root
    # whose `state/` is a regular file, `db_path()`'s mkdir raises
    # NotADirectoryError, and evaluating it early turned a typed
    # `contract_violation` (the capture policy is unreadable, which is what
    # fails FIRST) into an unrouted traceback. The factory is called exactly
    # where `connect` used to be, so the order of failures is unchanged.
    borrowed = conn_factory is not None
    attempt = 0
    while True:
        try:
            # BOTH paths open synchronous=FULL, so the two transports make ONE
            # promise (#1693). Under NORMAL the cold rung's durability rides on
            # the close-checkpoint's fsync -- and that checkpoint's failure is
            # caught and still reported `committed` just below, so the rung kept
            # an acknowledged-but-not-durable path the daemon rung no longer
            # has. Which rung an emit takes is arbitrary from the caller's side
            # (the shim falls to the cold one whenever the socket wedges, which
            # on this host is most of the time), so a divergence here is a
            # promise that varies by accident -- worse than either semantic
            # chosen deliberately.
            #
            # Measured cost of FULL on the cold path: NONE. Interleaved arms
            # (reps outer, arms inner, so both share the same minutes of host
            # load) over 60 batches each: NORMAL 66.34ms median / 139.83 p95,
            # FULL 60.17 / 120.58 -- FULL nominally FASTER, ranges overlapping,
            # so the honest reading is no measurable difference. A first,
            # SEQUENTIAL pass had reported +44ms; that was load drift, not a
            # cost, and the arms had to be interleaved to see it (the
            # send-size-probe.sh lesson). The cold path already fsyncs at its
            # close-checkpoint, so the commit-time fsync buys durability
            # without adding a syscall the rung was not already paying.
            if not borrowed:
                preflight_schema(root)
            own = conn_factory() if borrowed else connect(db_file(root),
                                                          synchronous="FULL")
            try:
                require_current_schema(own)
                host = ensure_host_uid(Path(root) / "state")
                if precondition is None:
                    results = ingest_many(own, items, host_uid=host)
                else:
                    results = ingest_many(own, items, host_uid=host,
                                          precondition=precondition)
            finally:
                if not borrowed:
                    try:
                        own.close()
                    except sqlite3.Error:
                        # Post-review fix: a WAL-flush failure on close, AFTER a
                        # successful commit, must not fall into the spool path —
                        # that reported committed events as "spooled" and queued a
                        # redundant replay. A close failure after a FAILED ingest
                        # changes nothing (that exception already routed).
                        #
                        # #1693: the swallow is now HARMLESS rather than
                        # merely unreached. Both paths commit under FULL, so the
                        # rows are already fsync'd before this close runs -- a
                        # checkpoint failure here is a WAL-size event, not a
                        # silent durability hole. The swallow stays because its
                        # original reason stands: a close failure after a
                        # successful commit must not route into the spool and
                        # queue a redundant replay.
                        pass
            break
        except (DowngradeError, ContractViolation):
            raise
        except sqlite3.OperationalError as exc:
            # Spool ONLY whitelisted-retryable codes (round-4 F6): IntegrityError
            # never lands here (a bug, propagates), and a missing table / SQL typo
            # — OperationalError but equally bugs — propagate loudly too.
            if not is_retryable(exc):
                raise
            # A transient LOCK is RETRIED in-process before it is spooled: two
            # first emitters on a brand-new plane race the WAL switch and the
            # first migration, a case SQLite refuses to wait on (a would-be
            # deadlock returns BUSY at once, busy_timeout or not), and the
            # loser's whole batch went to the spool — measured 3 of 10 pairs
            # (Phase B: a door's detached fleet event beside its own emission
            # is exactly that pair). The transaction rolled back, so a retry
            # re-ingests nothing twice. Only contention is retried: a readonly
            # / I/O / full / cannot-open fault spools at once, as before — the
            # spool is the record for those, and a pause would only delay it.
            attempt += 1
            if is_transient_lock(exc) and attempt < LOCK_RETRY_ATTEMPTS:
                time.sleep(LOCK_RETRY_BACKOFF_S * attempt)
                continue
            if require_commit:
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


def emit(root: Path, raw_request: dict, *, require_commit: bool = False) -> EmitOutcome:
    return emit_batch(root, [raw_request], require_commit=require_commit)[0]
