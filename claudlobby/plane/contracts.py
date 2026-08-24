"""Pydantic v2 wire contracts (design v2 §4, §7-8, F11, F17).

The vocabulary is a CLOSED enum enforced here: an unknown message_class or
task event is a caller bug and fails loud (ContractViolation) — never coerced,
never spooled. `delivered` is deliberately absent from ATTEMPT_STATES (F9).
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from . import SUPPORTED_SCHEMA_VERSIONS
from .ids import ID_PATTERNS

MESSAGE_CLASSES = (
    "task_request", "report", "question", "answer", "alert", "notice",
    "briefing", "nudge", "acknowledgement", "chat", "config_change",
    "raw_control",
)
COMMAND_TYPES = ("task", "cancel", "compact", "restart", "query")
ATTEMPT_STATES = (
    "send_attempted", "carrier_accepted", "pane_submitted", "failed",
    "unknown", "recipient_acknowledged", "duplicate_suppressed",
)
TASK_EVENTS = (
    # 19 — receiver_acknowledged DELETED (F9 v2.1; recount ruled 2026-08-25: the
    # pre-deletion tuple was 20, mis-stated as 19 — the count error predated the
    # deletion): the transmission ack row is
    # the single acknowledgement fact; activation derives through the join.
    "dispatch_intended", "transmission_failed", "dispatch_submitted",
    "accepted", "rejected", "progress",
    "blocked_waiting", "returned_blocked", "resumed", "completed", "failed",
    "cancelled", "deadline_changed", "superseded", "reassigned",
    "retry_created", "orphaned_by_session_loss", "recovered_after_restart",
    "expired",
)
DECLARATION_EVENTS = ("revision_seen", "scan_completed")
SYSTEM_SUBJECT_KINDS = ("host", "vault", "fleet", "actor", "bot_instance", "session")
WORKSTREAM_EVENTS = (
    "progressed", "renewed", "blocked", "unblocked", "closed", "archived",
    "plan_linked", "plan_unlinked",
)
CARRIERS = ("tmux", "telegram-tgpost", "telegram-bridge")

# THE kind manifest (F16 v2.1) — the SSOT the DDL CHECK and the INSERT-matrix
# test both derive from. require = NOT NULL for the kind; forbid = must be
# NULL; vocab None = registry-governed (F19: system tokens never CHECK).
_STREAM_COLS = ("event", "carrier", "attempt_no", "carrier_ref", "msg_id",
                "work_item_id", "assignment_id", "workstream_id",
                "subject_kind", "subject_uid", "subject_alias", "actor_uid",
                "session_uid", "severity", "deadline", "successor_id",
                "renewed_until")
KIND_MANIFEST: dict[str, dict] = {
    # require = NOT NULL for the kind; allowed = optional; FORBIDDEN IS
    # DERIVED (round-3 F3): every _STREAM_COLS member not required and not
    # allowed must be NULL — hand-listing forbids is how actor_uid and
    # session_uid escaped round 2.
    "transmission": {
        "vocab": ATTEMPT_STATES,
        "require": ("event", "msg_id", "carrier", "attempt_no"),
        "allowed": ("carrier_ref",),
    },
    "task": {
        "vocab": TASK_EVENTS,
        "require": ("event", "work_item_id"),
        "allowed": ("assignment_id", "actor_uid", "session_uid", "deadline",
                     "successor_id"),
    },
    "workstream": {
        "vocab": WORKSTREAM_EVENTS,
        "require": ("event", "workstream_id"),
        "allowed": ("actor_uid", "renewed_until"),
    },
    "system": {
        "vocab": None,   # registry-governed (F19)
        "require": ("event",),
        "allowed": ("severity",),
        # Round-6 (reviewer's exhaustive-subset probe): the DDL's real
        # semantics are a required PAIR with a conditionally-optional alias —
        # kind+uid must appear together; alias is legal only WITH the pair.
        # (kind+uid, alias NULL) is ACCEPTED; any subset missing part of the
        # anchor is rejected. The round-5 "all-three-as-a-unit" model was
        # wrong about the DDL, which was right.
        "allowed_groups": (
            {"anchor": ("subject_kind", "subject_uid"),
             "dependent": ("subject_alias",)},
        ),
    },
    "declaration": {
        "vocab": DECLARATION_EVENTS,
        "require": ("event", "subject_kind", "subject_uid"),
        "allowed": ("subject_alias",),
    },
}


def kind_forbidden(kind: str) -> tuple[str, ...]:
    manifest = KIND_MANIFEST[kind]
    keep = set(manifest["require"]) | set(manifest["allowed"])
    for group in manifest.get("allowed_groups", ()):
        keep |= set(group["anchor"]) | set(group["dependent"])
    return tuple(c for c in _STREAM_COLS if c not in keep)


FLEET_REQUIRED = {"communication", "work_item", "assignment", "transmission", "task"}

# Field policy lives in plane/registries.py (the design's stated home) and is
# imported here so validators ENFORCE from it — one SSOT, no duplicated caps
# (round-5 F8: descriptive-only policy meant editing a cap changed nothing).
from .registries import CONTENT_FIELDS, FIELD_POLICY  # noqa: E402  (re-export)

# BODY_CAP_BYTES retired (round-6): caps are read from FIELD_POLICY at call time.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class ContractViolation(ValueError):
    """Payload violates the wire contract — caller bug, fail loud."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(str(errors))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BodyFields(_Strict):
    body: str
    body_bytes: int
    body_sha256: str
    truncated: bool


def cap_body(text: str) -> BodyFields:
    """ANSI-strip, then cap at FIELD_POLICY's communication-body byte cap
    (UTF-8 safe), hashing the FULL stripped content so a truncated row still
    proves what it truncated."""
    stripped = _ANSI_RE.sub("", text)
    raw = stripped.encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    # Read the cap from the registry AT CALL TIME (round-6): an import-time
    # constant snapshot made FIELD_POLICY descriptive for communications.
    cap = FIELD_POLICY[("communication", "body")]["cap"]
    if len(raw) <= cap:
        return BodyFields(
            body=stripped, body_bytes=len(raw), body_sha256=digest, truncated=False
        )
    cut = raw[:cap].decode("utf-8", errors="ignore")
    return BodyFields(
        body=cut, body_bytes=len(raw), body_sha256=digest, truncated=True
    )


class Communication(_Strict):
    msg_id: str = Field(pattern=ID_PATTERNS["msg"])
    sender: str = Field(min_length=1)          # alias; resolved to uid at ingest
    sender_session_uid: Optional[str] = Field(None, pattern=ID_PATTERNS["session"])
    recipient: Optional[str] = None            # alias; None = broadcast-shaped
    recipient_raw: Optional[str] = None        # carrier-native address (chat id)
    message_class: Literal[MESSAGE_CLASSES]
    command_type: Optional[Literal[COMMAND_TYPES]] = None
    work_item_id: Optional[str] = Field(None, pattern=ID_PATTERNS["work_item"])
    assignment_id: Optional[str] = Field(None, pattern=ID_PATTERNS["assignment"])
    workstream_id: Optional[str] = None
    deliberation_id: Optional[str] = None      # Phase-5 seam, reserved
    reply_to_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    supersedes_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])
    body: Optional[str] = None
    privacy: Literal["metadata", "preview", "full"] = "metadata"
    idempotency_key: Optional[str] = None
    # correlation/causation/trace/span live ONLY on the EmitRequest envelope
    # (round-3 F4): payload duplicates were accepted and silently ignored.
    # Derived at validation from `body`; caller-supplied ONLY by the door's
    # capture policy when the body is withheld (metadata mode keeps the proof
    # triple while dropping content — F23):
    body_bytes: int = 0
    body_sha256: Optional[str] = None
    truncated: bool = False

    def model_post_init(self, __context) -> None:
        if self.body is not None:
            fields = cap_body(self.body)
            object.__setattr__(self, "body", fields.body)
            object.__setattr__(self, "body_bytes", fields.body_bytes)
            object.__setattr__(self, "body_sha256", fields.body_sha256)
            object.__setattr__(self, "truncated", fields.truncated)


class Transmission(_Strict):
    msg_id: str = Field(pattern=ID_PATTERNS["msg"])
    attempt_no: int = Field(ge=1)
    carrier: Literal[CARRIERS]
    destination: str
    state: Literal[ATTEMPT_STATES]
    carrier_ref: Optional[str] = None
    error: Optional[str] = None
    part_no: Optional[int] = Field(None, ge=1)     # bridge chunking (round-2 F10)
    part_count: Optional[int] = Field(None, ge=1)


class WorkItem(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    title: str = Field(min_length=1)
    created_by: str                             # alias
    workstream_id: Optional[str] = None         # the WHY axis
    repo: Optional[str] = Field(None, pattern=r"[^/\s]+/[^/\s]+")  # WHERE: owner/name
    project_key: Optional[str] = Field(None, pattern=r"[a-z][a-z0-9-]*")  # projects.yaml slug
    # Authored, not relayed: oversized bodies REJECT — and the cap is BYTES
    # (round-3 F8: max_length counts characters; multibyte text could pass
    # the char cap while exceeding the byte budget).
    body: Optional[str] = None

    @field_validator("body")
    @classmethod
    def _body_byte_cap(cls, v):
        cap = FIELD_POLICY[("work_item", "body")]["cap"]
        if v is not None and len(v.encode("utf-8")) > cap:
            raise ValueError(f"work_item.body exceeds {cap} bytes")
        return v


class Assignment(_Strict):
    assignment_id: str = Field(pattern=ID_PATTERNS["assignment"])
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    assignee: str                               # alias
    assigned_by: str                            # alias
    expected_by: Optional[AwareDatetime] = None
    dispatch_msg_id: Optional[str] = Field(None, pattern=ID_PATTERNS["msg"])


class TaskEvent(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    assignment_id: Optional[str] = Field(None, pattern=ID_PATTERNS["assignment"])
    event: Literal[TASK_EVENTS]
    actor: Optional[str] = None                 # alias: who reported it
    session_uid: Optional[str] = Field(None, pattern=ID_PATTERNS["session"])
    progress: Optional[int] = Field(None, ge=0, le=100)
    summary: Optional[str] = None

    @field_validator("summary")
    @classmethod
    def _summary_byte_cap(cls, v):
        # CONTENT-classified (FIELD_POLICY); authored — over-cap REJECTS.
        cap = FIELD_POLICY[("task", "summary")]["cap"]
        if v is not None and len(v.encode("utf-8")) > cap:
            raise ValueError(f"task summary exceeds {cap} bytes")
        return v
    pr_url: Optional[str] = None
    deadline: Optional[AwareDatetime] = None
    successor_id: Optional[str] = None  # reassigned/retry_created -> assignment_id; superseded -> superseding id


class SystemEvent(_Strict):
    """kind=system — machinery detections and lifecycle (F19: the token
    vocabulary is REGISTRY-governed, never a closed Literal: system-event
    emitters are the whole estate, and an unknown token must ingest rather
    than vanish. Only the token's SHAPE is enforced here; the known-set
    registry and warn-on-unknown accounting are Phase 2b)."""

    event: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    severity: Optional[Literal["critical", "notice"]] = None
    # Anchor pair (DDL rule, mirrored): kind+uid together or neither; alias
    # only WITH the pair.
    subject_kind: Optional[Literal[SYSTEM_SUBJECT_KINDS]] = None
    subject_uid: Optional[str] = Field(None, min_length=1)
    subject_alias: Optional[str] = None
    # DIAGNOSTIC payload (FIELD_POLICY ("system","data")): bounded, authored
    # by machinery — over-cap REJECTS like every authored body.
    data: Optional[dict] = None

    @field_validator("data")
    @classmethod
    def _data_byte_cap(cls, v):
        cap = FIELD_POLICY[("system", "data")]["cap"]
        if v is not None:
            import json as _json

            size = len(_json.dumps(v, ensure_ascii=False).encode("utf-8"))
            if size > cap:
                raise ValueError(f"system data exceeds {cap} bytes ({size})")
        return v

    def model_post_init(self, __context) -> None:
        if (self.subject_uid is None) != (self.subject_kind is None):
            raise ValueError(
                "subject_kind and subject_uid are an anchor pair — both or neither"
            )
        if self.subject_alias is not None and self.subject_uid is None:
            raise ValueError("subject_alias is legal only WITH the anchor pair")


FAMILIES: dict[str, type[BaseModel]] = {
    "communication": Communication,
    "transmission": Transmission,
    "work_item": WorkItem,
    "assignment": Assignment,
    "task": TaskEvent,
    "system": SystemEvent,
}


class EmitRequest(_Strict):
    event_type: str
    # None → emit stamps the current version at finalize; an explicit value
    # rides the wire and the spool VERBATIM (§10: versioned envelope), so a
    # drained N-1 entry is ingestable and a future one quarantines instead of
    # being reinterpreted as current.
    schema_version: Optional[str] = None
    occurred_at: Optional[AwareDatetime] = None   # None → emit stamps BEFORE first attempt (F6)
    observed_at: Optional[AwareDatetime] = None   # §4: reporter-of-another-system's-fact only
    emitter: str = Field(min_length=1)
    source_ref: Optional[str] = None
    fleet: Optional[str] = None                   # alias; REQUIRED for FLEET_REQUIRED types
    event_id: Optional[str] = Field(None, pattern=ID_PATTERNS["event"])
    correlation_id: Optional[str] = None          # round-2 F4: the envelope rides the wire
    causation_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    origin: Literal["live", "legacy"] = "live"    # F18 representability
    import_batch: Optional[str] = None
    confidence: Optional[str] = None
    payload: dict


def validate_request(raw: dict) -> tuple[EmitRequest, BaseModel]:
    try:
        env = EmitRequest.model_validate(raw)
    except ValidationError as exc:
        raise ContractViolation(exc.errors()) from exc
    if env.schema_version is not None and env.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractViolation(
            [{"loc": ("schema_version",),
              "msg": f"unsupported schema_version {env.schema_version!r}"
                     f" (supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"}]
        )
    model = FAMILIES.get(env.event_type)
    if model is None:
        raise ContractViolation(
            [{"loc": ("event_type",), "msg": f"unknown event type {env.event_type!r}"}]
        )
    if env.event_type in FLEET_REQUIRED and not env.fleet:
        raise ContractViolation(
            [{"loc": ("fleet",), "msg": f"{env.event_type} is fleet-scoped"}]
        )
    try:
        payload = model.model_validate(env.payload)
    except ValidationError as exc:
        raise ContractViolation(exc.errors()) from exc
    return env, payload


def export_schemas() -> dict:
    out = {"envelope": EmitRequest.model_json_schema()}
    for name, model in FAMILIES.items():
        out[name] = model.model_json_schema()
    return out
