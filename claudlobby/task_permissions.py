"""Task-scoped permission records owned by the host's canonical Plane.

Requests and decisions are immutable system facts, with the existing request
receipts and task lock providing commit/replay semantics. Public callers supply
an admitted TaskOperationContext; they must authenticate the human separately.
This module neither executes the approved effect nor treats a chat as authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import re

from .plane.ids import ID_PATTERNS, derive_uid
from .request_receipts import semantic_digest
from .task_operations import (
    TaskActor, TaskConflictError, TaskOperationContext, TaskOperationUnavailableError, _commit, _existing,
    _identities, _locked_task, _own_fleet, _prepare, _raw, _reader,
    _read_snapshot, _replayed,
)
from .task_queries import TaskNotFoundError, TaskQueryError, show_task


EMITTER = "claudlobby.task-permissions.v1"
ACTIONS = ("merge", "deploy", "spend", "delete")
_PERMISSION_ID = r"perm_[0-9a-f]{32}"
_DECISIONS = {"approve": ("pending", "approved"), "decline": ("pending", "declined"),
              "revoke": ("approved", "revoked")}


def _text(value, name, cap):
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode("utf-8")) > cap
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise TaskQueryError(f"permission {name} requires nonempty text within {cap} bytes")
    return value


@dataclass(frozen=True)
class PermissionScope:
    action: str
    target: str
    constraints: str

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise TaskQueryError("permission action must be merge, deploy, spend or delete")
        _text(self.target, "target", 1024)
        _text(self.constraints, "constraints", 4096)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"action", "target", "constraints"}:
            raise TaskQueryError("permission scope requires exactly action, target and constraints")
        return cls(**value)

    @property
    def digest(self):
        return semantic_digest(asdict(self))


@dataclass(frozen=True)
class Permission:
    permission_id: str
    request_event_id: str
    task_id: str
    assignment_id: str | None
    requester: TaskActor
    scope: PermissionScope
    reason: str
    state: str
    decided_by: TaskActor | None = None
    decision_reason: str | None = None

    def as_dict(self):
        result = asdict(self)
        result["scope_sha256"] = self.scope.digest
        return result


@dataclass(frozen=True)
class PermissionResult:
    request_id: str
    permission: Permission
    replayed: bool
    recording: str = "committed"
    execution: str = "not_requested"


def _id(value, pattern, name):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise TaskQueryError(f"permission {name} must be a canonical ID")
    return value


def _canonical_fact(root, conn, row, data):
    """Generic ingest can assert an emitter/subject; only an owned receipt proves it.

    The receipt is not a second permission state. Its immutable projection ties
    this exact fact to the admitted command. A committed fact whose response was
    lost remains provable while its receipt stage is still unknown.
    """
    from .request_facts import reconcile_facts
    from .request_queries import (_read_receipt, _scope, RequestNotFoundError,
                                  RequestQueryError, RequestUnavailableError)

    try:
        request_id = data.get("operation_request_id")
        _scope(root, row["fleet_uid"], request_id)
        receipt = _read_receipt(root, row["fleet_uid"], request_id)
    except RequestNotFoundError:
        return False
    except RequestUnavailableError as exc:
        raise TaskOperationUnavailableError("permission command proof is unavailable") from exc
    except RequestQueryError:
        return False
    intent = receipt.intent
    operations = {"permission_requested": "task.permission.request",
                  **{"permission_" + state: "task.permission." + verb
                     for verb, (_before, state) in _DECISIONS.items()}}
    if (intent.operation != operations.get(row["event"]) or intent.host_uid != row["host_uid"]
            or intent.caller_uid != row["subject_uid"]
            or tuple(stage.kind for stage in intent.stages) != ("recording",)
            or len(intent.stages[0].facts) != 1
            or intent.stages[0].facts[0].event_id != row["event_id"]):
        return False
    if (receipt.stages[0].status not in {"unknown", "committed"}
            or reconcile_facts(conn, intent.stages[0].facts).status != "committed"):
        raise TaskConflictError("permission command proof is incomplete or inconsistent")
    return True


def read_permission(conn, permission_id, *, root, host_uid, fleet_uid):
    """Read a complete immutable history, refusing missing or conflicting facts."""
    own_snapshot = not conn.in_transaction
    if own_snapshot:
        conn.execute("BEGIN")
    try:
        return _permission_snapshot(conn, permission_id, root=root, host_uid=host_uid, fleet_uid=fleet_uid)
    finally:
        if own_snapshot:
            conn.rollback()


def _permission_snapshot(conn, permission_id, *, root, host_uid, fleet_uid):
    _id(permission_id, _PERMISSION_ID, "reference")
    rows = conn.execute(
        "SELECT e.* FROM events e WHERE e.kind='system' AND e.emitter=? "
        "AND e.host_uid=? AND e.fleet_uid=? AND e.source_ref=? ORDER BY e.ingest_seq",
        (EMITTER, host_uid, fleet_uid, "permission:" + permission_id),
    )
    permission = None
    for row in rows:
        try:
            if row["origin"] != "live" or row["detail_truncated"] or row["subject_kind"] != "actor":
                raise ValueError
            data = json.loads(row["detail"])
            if not isinstance(data, dict):
                raise ValueError
            if not _canonical_fact(root, conn, row, data):
                # In particular, do not ignore a missing revocation receipt and
                # resurrect an older approval. An ambiguous chain denies all use.
                raise ValueError
            if data["permission_id"] != permission_id:
                raise ValueError
            actor = TaskActor(row["subject_uid"], row["subject_alias"])
            _id(actor.uid, ID_PATTERNS["actor"], "actor")
            if row["event"] == "permission_requested" and permission is None:
                if set(data) != {"operation_request_id", "permission_id", "task_id", "assignment_id", "scope", "reason"}:
                    raise ValueError
                _id(data["task_id"], ID_PATTERNS["work_item"], "task")
                if data["assignment_id"] is not None:
                    _id(data["assignment_id"], ID_PATTERNS["assignment"], "assignment")
                if not re.fullmatch(r"bot:[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", actor.alias):
                    raise ValueError
                permission = Permission(permission_id, row["event_id"], data["task_id"],
                    data["assignment_id"], actor, PermissionScope.from_dict(data["scope"]),
                    _text(data["reason"], "reason", 2048), "pending")
            elif permission is not None:
                if (set(data) != {"operation_request_id", "permission_id", "request_event_id", "scope_sha256", "reason"}
                        or data["request_event_id"] != permission.request_event_id
                        or data["scope_sha256"] != permission.scope.digest
                        or not re.fullmatch(r"human:[^\s:/]+", actor.alias)):
                    raise ValueError
                transition = next((pair for pair in _DECISIONS.values()
                                   if row["event"] == "permission_" + pair[1]), None)
                if transition is None or permission.state != transition[0]:
                    raise ValueError
                permission = replace(permission, state=transition[1], decided_by=actor,
                    decision_reason=_text(data["reason"], "reason", 2048))
            else:
                raise ValueError
        except TaskOperationUnavailableError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise TaskConflictError("permission history is incomplete or inconsistent") from exc
    if permission is None:
        raise TaskNotFoundError("permission request has no canonical command proof in this host and fleet")
    return permission


def _read(ctx, conn, permission_id):
    return read_permission(conn, permission_id, root=ctx.root, host_uid=ctx.host_uid, fleet_uid=ctx.fleet_uid)


def _current(ctx, conn, permission):
    task = show_task(conn, permission.task_id, fleet_uid=ctx.fleet_uid).require_resolved()
    assignment = task.current_assignment.assignment_id if task.current_assignment else None
    if task.host_uid != ctx.host_uid or not task.open or assignment != permission.assignment_id:
        raise TaskConflictError("permission request belongs to closed work or a different assignment")
    requester = permission.requester
    if requester not in ctx.bots.values():
        raise TaskConflictError("permission requester is no longer a declared bot")
    if requester != ctx.bots[ctx.context.fleet.manager] and (
            task.current_assignment is None or task.current_assignment.assignee_uid != requester.uid):
        raise TaskConflictError("permission requester is not this task's manager or current assignee")
    _identities(ctx, conn, (ctx.caller, requester))


def request_permission(ctx: TaskOperationContext, request_id: str, task_id: str, *,
                       scope: PermissionScope, reason: str, admit_read=None) -> PermissionResult:
    """A declared task worker or manager asks; this never grants authority."""
    from .request_receipts import locked_request

    _own_fleet(ctx)
    if ctx.caller_fleet_uid != ctx.fleet_uid or ctx.caller not in ctx.bots.values():
        raise TaskConflictError("permission requests require a declared bot caller")
    if not isinstance(scope, PermissionScope):
        raise TaskQueryError("a typed permission scope is required")
    _id(task_id, ID_PATTERNS["work_item"], "task")
    _text(reason, "reason", 2048)
    permission_id = derive_uid("perm", f"permission:v1:{ctx.host_uid}:{ctx.fleet_uid}:{request_id}")
    semantic = semantic_digest(dict(task_id=task_id, scope=asdict(scope), reason=reason))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store, _reader(ctx) as conn:
        previous = _existing(store, ctx, "task.permission.request", semantic)
        with _locked_task(store, task_id) as check:
            with _read_snapshot(conn, admit_read):
                if _replayed(store, previous, conn):
                    return PermissionResult(request_id, _read(ctx, conn, permission_id), True)
                task = show_task(conn, task_id, fleet_uid=ctx.fleet_uid).require_resolved()
                aid = task.current_assignment.assignment_id if task.current_assignment else None
                if previous and previous.intent.assignment_id != aid:
                    raise TaskConflictError("permission request's original assignment changed")
                pending = Permission(permission_id, "", task_id, aid, ctx.caller, scope, reason, "pending")
                _current(ctx, conn, pending)
                raw = _event(ctx, request_id, permission_id, "requested", dict(
                    permission_id=permission_id, task_id=task_id, assignment_id=aid,
                    scope=asdict(scope), reason=reason), previous)
                receipt = _prepare(store, ctx, "task.permission.request", semantic, (raw,), task_id, aid)
            _commit(store, ctx, conn, receipt, (raw,), check, admit_read=admit_read)
            with _read_snapshot(conn, admit_read):
                return PermissionResult(request_id, _read(ctx, conn, permission_id), False)


def _event(ctx, request_id, permission_id, event, data, previous):
    raw = _raw(ctx, request_id, "system", dict(event="permission_" + event,
        subject_kind="actor", subject=ctx.caller.alias,
        data={**data, "operation_request_id": request_id}), previous)
    raw.update(emitter=EMITTER, source_ref="permission:" + permission_id)
    return raw


def decide_permission(ctx: TaskOperationContext, request_id: str, permission_id: str, *,
                      decision: str, scope_sha256: str, reason: str,
                      admit_read=None) -> PermissionResult:
    """Record an authenticated human decision for the displayed exact scope.

    The terminal/browser boundary authenticates the human; a bot or provenance
    alias cannot substitute for that context. Revocation remains available for
    stale work, but a new approval requires the original live task binding.
    """
    from .request_receipts import locked_request

    if ctx.caller_fleet_uid is not None or not re.fullmatch(r"human:[^\s:/]+", ctx.caller.alias):
        raise TaskConflictError("permission decisions require an authenticated human caller")
    if decision not in _DECISIONS:
        raise TaskQueryError("permission decision must be approve, decline or revoke")
    _id(permission_id, _PERMISSION_ID, "reference")
    if not isinstance(scope_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", scope_sha256):
        raise TaskQueryError("permission decision requires the displayed scope digest")
    _text(reason, "reason", 2048)
    operation = "task.permission." + decision
    semantic = semantic_digest(dict(permission_id=permission_id, scope_sha256=scope_sha256, reason=reason))
    with locked_request(ctx.root, ctx.fleet_uid, request_id) as store, _reader(ctx) as conn:
        previous = _existing(store, ctx, operation, semantic)
        with _read_snapshot(conn, admit_read):
            first = _read(ctx, conn, permission_id)
        with _locked_task(store, first.task_id) as check:
            with _read_snapshot(conn, admit_read):
                if _replayed(store, previous, conn):
                    return PermissionResult(request_id, _read(ctx, conn, permission_id), True)
                permission = _read(ctx, conn, permission_id)
                if permission.scope.digest != scope_sha256:
                    raise TaskConflictError("permission scope differs from the selected request")
                expected_state, next_state = _DECISIONS[decision]
                if permission.state != expected_state:
                    raise TaskConflictError("permission already decided or not eligible for this decision")
                _identities(ctx, conn, (ctx.caller,))
                if decision == "approve":
                    _current(ctx, conn, permission)
                raw = _event(ctx, request_id, permission_id, next_state,
                    dict(permission_id=permission_id, request_event_id=permission.request_event_id,
                         scope_sha256=scope_sha256, reason=reason), previous)
                receipt = _prepare(store, ctx, operation, semantic, (raw,), permission.task_id,
                                   permission.assignment_id)
            _commit(store, ctx, conn, receipt, (raw,), check, admit_read=admit_read)
            with _read_snapshot(conn, admit_read):
                return PermissionResult(request_id, _read(ctx, conn, permission_id), False)


def require_permission(ctx, conn, permission_id, *, scope: PermissionScope):
    """Check the exact bot, scope and current task; never execute an effect.

    Call within the operation's admission boundary, immediately before its
    effect. This read is not a reusable execution token or an execution receipt.
    """
    _own_fleet(ctx)
    permission = _read(ctx, conn, permission_id)
    if not isinstance(scope, PermissionScope) or scope != permission.scope or ctx.caller != permission.requester:
        raise TaskConflictError("permission does not cover this caller and exact scope")
    if permission.state != "approved":
        raise TaskConflictError("permission has not been approved or was revoked")
    _current(ctx, conn, permission)
    return permission
