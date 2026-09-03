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
    model_validator,
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
    # carrier_queued (§6b #7, PR-B): the tmux door detected the payload was
    # accepted by the TUI but parked behind a busy turn (pane_holds_unsubmitted
    # at send) — accepted is not consumed, so this is NOT activation evidence.
    "send_attempted", "carrier_accepted", "carrier_queued", "pane_submitted",
    "failed", "unknown", "recipient_acknowledged", "duplicate_suppressed",
)
TASK_EVENTS = (
    # 20 — receiver_acknowledged DELETED (F9 v2.1; recount ruled 2026-08-25: the
    # pre-deletion tuple was 20, mis-stated as 19 — the count error predated the
    # deletion): the transmission ack row is
    # the single acknowledgement fact; activation derives through the join.
    # supplied_id_not_open ADDED (§6b #6, PR-B): the worker reported with a
    # task id that was not in the open set at report time — a fact about the
    # JOIN, not the work (4 real ledger rows; report-back already names it).
    "dispatch_intended", "transmission_failed", "dispatch_submitted",
    "accepted", "rejected", "progress",
    "blocked_waiting", "returned_blocked", "resumed", "completed", "failed",
    "cancelled", "deadline_changed", "superseded", "reassigned",
    "retry_created", "orphaned_by_session_loss", "recovered_after_restart",
    "expired", "supplied_id_not_open",
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


FLEET_REQUIRED = {"communication", "work_item", "assignment", "transmission",
                  "task", "workstream", "workstream_event"}

# Field policy lives in plane/registries.py (the design's stated home) and is
# imported here so validators ENFORCE from it — one SSOT, no duplicated caps
# (round-5 F8: descriptive-only policy meant editing a cap changed nothing).
from .registries import CONTENT_FIELDS, FIELD_POLICY  # noqa: E402  (re-export)

# BODY_CAP_BYTES retired (round-6): caps are read from FIELD_POLICY at call time.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _reject_over_cap(family: str, field: str, v):
    """CONTENT-classified byte cap, one enforcement shape (gauntlet round —
    three families carried three specialized copies of this check). Caps come
    from the FIELD_POLICY SSOT; UTF-8 BYTES, not characters (round-3 F8);
    authored content over cap REJECTS (§8/§11), never truncates."""
    cap = FIELD_POLICY[(family, field)]["cap"]
    if v is not None and len(v.encode("utf-8")) > cap:
        raise ValueError(f"{family}.{field} exceeds {cap} bytes")
    return v


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


# Carrier/state matrix (#1372 review F12): pane_submitted and carrier_queued
# are PANE facts (tmux only); carrier_accepted is a carrier-API fact (telegram
# only). The rest are carrier-neutral. Enforced here AND in the DDL CHECK, so
# neither the wire nor a direct-SQL writer can record physically impossible
# evidence — which is what keeps the token-only activation queries sound.
_CARRIER_ONLY_STATES = {
    "pane_submitted": ("tmux",),
    "carrier_queued": ("tmux",),
    "carrier_accepted": ("telegram-tgpost", "telegram-bridge"),
}


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

    def model_post_init(self, __context) -> None:
        allowed = _CARRIER_ONLY_STATES.get(self.state)
        if allowed is not None and self.carrier not in allowed:
            raise ValueError(
                f"state {self.state!r} is impossible for carrier"
                f" {self.carrier!r} (allowed: {', '.join(allowed)})"
            )


class WorkItem(_Strict):
    work_item_id: str = Field(pattern=ID_PATTERNS["work_item"])
    title: str = Field(min_length=1)
    created_by: str                             # alias
    workstream_id: Optional[str] = None         # the WHY axis
    repo: Optional[str] = Field(None, pattern=r"[^/\s]+/[^/\s]+")  # WHERE: owner/name
    project_key: Optional[str] = Field(None, pattern=r"^[a-z][a-z0-9-]*$")  # projects.yaml slug
    # Authored, not relayed: oversized bodies REJECT — and the cap is BYTES
    # (round-3 F8: max_length counts characters; multibyte text could pass
    # the char cap while exceeding the byte budget).
    body: Optional[str] = None

    @field_validator("body")
    @classmethod
    def _body_byte_cap(cls, v):
        return _reject_over_cap("work_item", "body", v)


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
        return _reject_over_cap("task", "summary", v)
    pr_url: Optional[str] = None
    deadline: Optional[AwareDatetime] = None
    successor_id: Optional[str] = None  # reassigned/retry_created -> assignment_id; superseded -> superseding id


class SystemEvent(_Strict):
    """kind=system — machinery detections and lifecycle (F19: the token
    vocabulary is REGISTRY-governed, never a closed Literal: system-event
    emitters are the whole estate, and an unknown token must ingest rather
    than vanish. The token SHAPE pattern is WIRE-TIER hardening only — the
    DDL deliberately accepts any non-null token, because the manifest rules
    it registry-governed and a direct-SQL writer bypasses pydantic by
    design; the two layers intentionally differ here and nowhere else).

    severity is REGISTRY-OWNED (§9b): ingest stamps it from
    registries.SYSTEM_EVENT_SEVERITY — it is deliberately NOT a wire field,
    so a caller supplying one is a ContractViolation (extra=forbid).

    data is DIAGNOSTIC (FIELD_POLICY): machinery-relayed, so over-cap
    TRUNCATES with the detail_truncated flag at ingest (§9b: "over-cap =>
    data_truncated flag ONLY") — never rejects; a diagnostic too big to
    store whole must not cost the event that carried it."""

    event: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # Anchor pair (DDL rule, mirrored): kind+uid together or neither; alias
    # only WITH the pair.
    subject_kind: Optional[Literal[SYSTEM_SUBJECT_KINDS]] = None
    subject_uid: Optional[str] = Field(None, min_length=1)
    subject_alias: Optional[str] = None
    # `subject` (cutover Phase B): an ALIAS to resolve at ingest — the
    # MetricSample form — for emitters that cannot know a uid (a bash door's
    # `emit_fleet_event`). Needs subject_kind, excludes the uid anchor pair;
    # ingest mints/looks up the uid and stamps subject_alias from it.
    subject: Optional[str] = Field(None, min_length=1)
    data: Optional[dict] = None

    def model_post_init(self, __context) -> None:
        if self.subject is not None:
            if self.subject_kind is None:
                raise ValueError("subject (an alias) needs subject_kind")
            if self.subject_uid is not None or self.subject_alias is not None:
                raise ValueError("subject (an alias) excludes the uid anchor pair — one or the other")
            return
        if (self.subject_uid is None) != (self.subject_kind is None):
            raise ValueError(
                "subject_kind and subject_uid are an anchor pair — both or neither"
            )
        if self.subject_alias is not None and self.subject_uid is None:
            raise ValueError("subject_alias is legal only WITH the anchor pair")


class Workstream(_Strict):
    """The workstream CONSTRUCT (§8/§19.3: pulled into 0001 for the bench;
    its wire contract lands with the door, PR-B T6). The construct row IS the
    opening — there is no `opened` event token (one-fact-one-row)."""

    workstream_id: str = Field(min_length=1)    # ws-slug (single-writer mints)
    title: str = Field(min_length=1)
    goal: Optional[str] = None
    owner: Optional[str] = None                 # alias
    opened_by: str = Field(min_length=1)        # alias
    project_key: Optional[str] = Field(None, pattern=r"^[a-z][a-z0-9-]*$")


class WorkstreamEvent(_Strict):
    """kind=workstream — wire name `workstream_event` (spec ruling #8: the one
    suffixed wire name, resolving the construct/kind collision)."""

    workstream_id: str = Field(min_length=1)
    event: Literal[WORKSTREAM_EVENTS]
    actor: Optional[str] = None                 # alias
    renewed_until: Optional[AwareDatetime] = None
    # detail tail (§9b): note/next_step are CONTENT (FIELD_POLICY-capped,
    # authored -> over-cap REJECTS); disposition carries close --status
    # done|abandoned (F21); plan_ref is the linked-never-stored doc pointer.
    note: Optional[str] = None
    next_step: Optional[str] = None
    disposition: Optional[Literal["done", "abandoned"]] = None
    plan_ref: Optional[str] = None

    @field_validator("note", "next_step")
    @classmethod
    def _content_byte_caps(cls, v, info):
        return _reject_over_cap("workstream_event", info.field_name, v)


# ---------------------------------------------------------------------------
# Phase 2b — the registry lane (spec §9b: field lists FINAL 2026-08-20).
# Entity payloads are keyframes of slow-changing RESOLVED state; volatile
# telemetry goes to metric_samples (F12/F20). uid fields are Optional on the
# WIRE: uids are system-minted (F10) — ingest resolves entity_alias through
# identity_registry and the stored entity_uid COLUMN is authoritative; a
# payload-carried uid is advisory. Fields marked sensitive in §9b keep their
# classification at render (§11) — the wire carries them verbatim.
# ---------------------------------------------------------------------------


class _HostSystem(_Strict):
    claudlobby_version: str
    claude_version: str
    node_version: Optional[str] = None
    python_version: str
    host_jobs: list[dict] = []
    plugins: list[dict] = []
    emitters: list[dict] = []
    defaults_tier_hash: str


class HostPayload(_Strict):
    host_uid: Optional[str] = None
    aliases: dict
    os: Literal["linux", "darwin"]
    arch: str
    kernel: str
    ram_total_mb: int
    disk_total_gb: int
    system: _HostSystem
    declared_fleets: list[str]
    schema_version: str


class _VaultCompat(_Strict):
    floor: str
    cli_version: Optional[str] = None
    # Optional, deviating from §9b's bare bool DELIBERATELY: no compat
    # probe runs at generate, and a fabricated verdict frozen by the hash
    # gate is the lie this lane exists to kill. None = no probe ran.
    ok: Optional[bool] = None


class VaultPayload(_Strict):
    vault_uid: Optional[str] = None
    alias: str
    role: Literal["primary", "mounted"]
    mount_path: str
    remote: str                                   # sensitive (§11)
    compat: _VaultCompat
    carries_fleets: bool
    gitignore_safe: bool
    schema_version: str


class _FleetGroup(_Strict):
    name: str
    manager: str
    members: list[str]
    mission: Optional[str] = None


class _FleetDefaults(_Strict):
    model: str
    effort: Optional[str] = None
    account: str
    list_tier_hashes: dict[str, str]


class FleetPayload(_Strict):
    fleet_uid: Optional[str] = None
    alias: str
    service_prefix: str
    mission: Optional[str] = None
    mission_file: Optional[dict] = None
    manager: object                               # str | [str] — F5 scalar
    groups: list[_FleetGroup] = []
    org_edges: list[dict] = []
    roster: list[str]
    defaults_summary: _FleetDefaults
    env_keys: list[str] = []                      # names ONLY, never values
    jobs: list[dict] = []
    plugins_additional: list[str] = []
    vault_binding: dict
    telegram: Optional[dict] = None               # group_alias only (§11)
    declared_hash: str
    vault_rev: Optional[str] = None
    schema_version: str


class ProjectPayload(_Strict):
    project_uid: Optional[str] = None
    key: str
    fleet_uid: Optional[str] = None
    title: str
    repos: list[str]
    tier: Literal["auto", "review", "preview", "human"]
    validation_hash: str
    mission_file: Optional[dict] = None
    declared_hash: str
    vault_rev: Optional[str] = None
    schema_version: str


class LibraryItemPayload(_Strict):
    library_item_uid: Optional[str] = None
    category: str
    name: str
    source_tier: Literal["shared", "fleet-overlay"]
    fleet_uid: Optional[str] = None
    content_hash: str
    title: Optional[str] = None
    description: Optional[str] = None
    declared_hash: str
    vault_rev: Optional[str] = None
    schema_version: str


class _BotPosture(_Strict):
    permissions_mode: str                         # sensitive as a block (§11)
    tool_allow: list[str] = []
    tool_deny: list[str] = []
    sandbox: dict = {}
    permissions_grants: dict = {}
    hooks: list[dict] = []
    env_keys: list[str] = []
    rc_enabled: bool = False
    telegram: dict = {}
    git_credentials_profile: Optional[str] = None


class BotPayload(_Strict):
    actor_uid: Optional[str] = None
    bot_instance_uid: Optional[str] = None
    alias: str                                    # "bot:<fleet>/<name>"
    display_name: Optional[str] = None
    fleet_uid: Optional[str] = None
    account: str
    service: str
    model: str
    effort: Optional[str] = None
    org: dict = {}
    equipment: dict = {}
    posture: _BotPosture
    schedule: dict = {}
    vault_binding: Optional[dict] = None
    composed_hashes: dict
    declared_hash: str
    vault_rev: Optional[str] = None
    schema_version: str


ENTITY_PAYLOADS: dict[str, type[BaseModel]] = {
    "host": HostPayload,
    "vault": VaultPayload,
    "fleet": FleetPayload,
    "project": ProjectPayload,
    "library_item": LibraryItemPayload,
    "bot": BotPayload,
}

# entity_type -> identity_registry kind. Bot keyframes key on the INSTANCE
# (§9b: entity_uid is the per-host supervised install; the logical actor is
# reachable through the payload and confirmed alongside at ingest).
ENTITY_IDENTITY_KIND: dict[str, str] = {
    "host": "host", "vault": "vault", "fleet": "fleet",
    "bot": "bot_instance", "project": "project",
    "library_item": "library_item",
}


class RegistrySnapshot(_Strict):
    entity_type: Literal["host", "vault", "fleet", "bot", "project",
                         "library_item"]
    entity_alias: str = Field(min_length=1)
    tombstone: bool = False
    # dict on the wire, validated against ENTITY_PAYLOADS[entity_type] by
    # validate_request; None iff tombstone (mirrors the DDL CHECK).
    payload: Optional[dict] = None
    cause: Literal["generate", "probe", "equip", "migration"]
    scan_id: str = Field(min_length=1)
    vault_rev: Optional[str] = None

    @model_validator(mode="after")
    def _payload_iff_not_tombstone(self):
        if self.tombstone and self.payload is not None:
            raise ValueError("a tombstone carries no payload")
        if not self.tombstone and self.payload is None:
            raise ValueError("a non-tombstone snapshot requires a payload")
        return self


class MetricSample(_Strict):
    subject_kind: Literal["host", "vault", "fleet", "actor", "bot_instance",
                          "session"]
    subject: str = Field(min_length=1)            # alias; uid resolved at ingest
    metric: str = Field(min_length=1)   # open registry (registries.
                                        # METRIC_NAMES): ingest WARNS on
                                        # unknown, never rejects
    value: object = Field(...)                    # number | bool | str | object

    @field_validator("value")
    @classmethod
    def _value_not_none(cls, v):
        if v is None:
            raise ValueError("a sample without a value is not a sample")
        return v
    status: Optional[Literal["ok", "warn", "alert"]] = None


class Declaration(_Strict):
    """events kind=declaration — the provenance chain that never disappears
    into the hash gate: revision_seen records every newly observed vault
    revision even when resolved state is byte-identical; scan_completed is
    the fact that makes tombstones valid (same scan_id, complete=true)."""

    event: Literal["revision_seen", "scan_completed"]
    subject_kind: Literal["vault", "host"]
    subject: str = Field(min_length=1)            # alias; uid resolved at ingest
    vault_rev: Optional[str] = None               # revision_seen detail
    scan_id: Optional[str] = None                 # scan_completed detail (REQUIRED there)
    scope: Optional[str] = None
    counts: Optional[dict] = None
    complete: Optional[bool] = None
    source_rev: Optional[str] = None   # optional BY DESIGN: vaultless fleets scan too

    @model_validator(mode="after")
    def _per_token_detail(self):
        if self.event == "scan_completed":
            if not self.scan_id:
                raise ValueError("scan_completed requires scan_id (round-3"
                                 " F11: a completion must join its"
                                 " tombstones)")
            if self.complete is None or self.counts is None \
                    or self.scope is None:
                raise ValueError("scan_completed requires scope, counts and"
                                 " complete (§9d detail)")
        if self.event == "revision_seen" and not self.vault_rev:
            raise ValueError("revision_seen requires vault_rev")
        return self


FAMILIES: dict[str, type[BaseModel]] = {
    "communication": Communication,
    "transmission": Transmission,
    "work_item": WorkItem,
    "assignment": Assignment,
    "task": TaskEvent,
    "system": SystemEvent,
    "workstream": Workstream,
    "workstream_event": WorkstreamEvent,
    "registry_snapshot": RegistrySnapshot,
    "metric_sample": MetricSample,
    "declaration": Declaration,
}

# Wire family -> physical events.kind where the two DIFFER — the spec-ruling-#8
# fact (`workstream_event` is the one suffixed wire name) declared ONCE, here
# beside FAMILIES where the wire contract lives. Both ingest consumers (the
# insert mapping and duplicate-replay verification) import this; F4 was
# exactly a second site missing the mapping, and a private copy in ingest.py
# left the insert branch hardcoding the same fact separately (gauntlet round).
WIRE_TO_KIND: dict[str, str] = {"workstream_event": "workstream"}


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
    if isinstance(payload, RegistrySnapshot) and payload.payload is not None:
        # the inner entity payload is typed per entity_type (§9b FINAL):
        # a snapshot whose payload fails its entity contract is a contract
        # verdict at the door, never a stored malformed keyframe
        entity_model = ENTITY_PAYLOADS[payload.entity_type]
        try:
            entity_model.model_validate(payload.payload)
        except ValidationError as exc:
            raise ContractViolation(
                [{"loc": ("payload", payload.entity_type, *e["loc"]),
                  "msg": e["msg"]} for e in exc.errors()]
            ) from exc
    return env, payload


def export_schemas() -> dict:
    out = {"envelope": EmitRequest.model_json_schema()}
    for name, model in FAMILIES.items():
        out[name] = model.model_json_schema()
    return out
