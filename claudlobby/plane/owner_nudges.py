"""Locally granted owner task nudges, with no HTTP or browser entry point.

VerifiedReader comes from trusted ingress. This adapter neither verifies a
browser principal nor provides CSRF/session carriers. A nudge asks the selected
fleet manager about canonical work; it never wakes an arbitrary bot or approves
another action. Revocation stops subsequent admission, not an in-flight effect.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

from ..activation_identity import read_selected_identity_bindings
from ..active_config import resolve_active_context
from ..command_result import CommandOutput
from ..commands.task_write import nudge_bound_task
from ..message_context import resolve_message_route
from ..message_queries import (MessageNotFoundError, MessageUnavailableError,
                               ReceiptObservation, receipt)
from ..operation_context import bind_task_context
from ..request_queries import RequestView, read_request
from ..resources import PackageResources
from ..runtime_admission import RuntimeIdentity, mutation_admission
from .ids import ID_PATTERNS, read_host_uid
from .owner_access import AccessDenied, OwnerAccess, OwnerNudgeGrant, VerifiedReader
from .owner_messages import _GENERATED_ENV
from .owner_source import admit_source, inspect_source


@dataclass(frozen=True)
class OwnerNudgeObservation:
    """Retained recording/transport and independent receiver proof, no resend."""

    request: RequestView
    receiver: ReceiptObservation


class OwnerNudges:
    def __init__(self, root: Path, *, package: PackageResources):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.resolve(strict=True) != self.root:
            raise ValueError("owner nudges require an explicit unredirected root")
        self.host_uid = read_host_uid(self.root / "state")
        self.package = package
        self.access = OwnerAccess(self.root)

    def _authorize(self, reader, fleet_uid, expected_grant):
        if type(reader) is not VerifiedReader:
            raise AccessDenied("verified_reader_required")
        if any(key in os.environ for key in _GENERATED_ENV):
            raise AccessDenied("generated_context_refused")
        if read_host_uid(self.root / "state") != self.host_uid:
            raise AccessDenied("wrong_deployment")
        if type(expected_grant) is not OwnerNudgeGrant:
            raise AccessDenied("expected_nudge_grant_required")
        grant = self.access.authorize_nudge(reader.token, reader.principal,
            host_uid=self.host_uid, fleet_uid=fleet_uid)
        if grant != expected_grant:
            raise AccessDenied("nudge_binding_changed")
        if inspect_source(self.root) != self.host_uid:
            raise AccessDenied("wrong_deployment")
        return grant

    def _bind(self, reader, fleet, fleet_uid, expected_grant):
        grant = self._authorize(reader, fleet_uid, expected_grant)
        if not isinstance(fleet, str) or not fleet:
            raise AccessDenied("fleet_required")
        selected = resolve_active_context(root=self.root, fleet=fleet, package=self.package)
        bindings = read_selected_identity_bindings(self.root, fleet, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.host_uid != self.host_uid or ctx.fleet_uid != grant.fleet_uid
                or bindings["host_uid"] != self.host_uid or bindings["fleet_uid"] != grant.fleet_uid
                or {name: bot.uid for name, bot in ctx.bots.items()} != bindings["bots"]
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None):
            raise AccessDenied("nudge_binding_changed")
        return grant, ctx

    def _admit_read(self, conn):
        if read_host_uid(self.root / "state") != self.host_uid:
            raise AccessDenied("wrong_deployment")
        admit_source(conn, self.host_uid)

    @staticmethod
    def _target(ctx, task_id, manager_uid):
        if not isinstance(task_id, str) or not re.fullmatch(ID_PATTERNS["work_item"], task_id):
            raise ValueError("canonical task ID required")
        manager = ctx.bots[ctx.context.fleet.manager]
        if manager.uid != manager_uid:
            raise AccessDenied("nudge_manager_changed")
        return manager

    def nudge(self, reader: VerifiedReader, *, fleet: str, fleet_uid: str,
              task_id: str, manager_uid: str, expected_assignment_id: str | None,
              expected_release_id: str, request_id: str, reason: str,
              expected_grant: OwnerNudgeGrant) -> CommandOutput:
        """Commit one nudge then notify; keep its UUID after any uncertain result.

        The caller must supply its selected assignment (None means queued),
        active release and manager. No ambient actor, provenance override,
        recipient override or uncertain-retry mode is available.
        """
        self._authorize(reader, fleet_uid, expected_grant)
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError("nudge reason requires at most 2000 characters")
        if not isinstance(expected_release_id, str) or not re.fullmatch(r"r-[0-9a-f]{64}", expected_release_id):
            raise ValueError("selected release ID required")
        identity = RuntimeIdentity(RuntimeIdentity.current().cli, self.package.native,
                                   self.package.artifact_id)
        with mutation_admission(self.root, identity=identity, expected_release=expected_release_id) as release:
            grant, ctx = self._bind(reader, fleet, fleet_uid, expected_grant)
            manager = self._target(ctx, task_id, manager_uid)
            route = resolve_message_route(ctx.context.fleet.manager, root=self.root, fleet=fleet,
                                          package=self.package, caller_context=ctx)
            if (route.release_id != release.release_id or route.host_uid != self.host_uid
                    or route.selected_fleet_uid != fleet_uid or route.peer_fleet_uid != fleet_uid
                    or route.caller != ctx.caller or route.peer != manager or route.manager != manager):
                raise AccessDenied("nudge_route_changed")
            self._authorize(reader, fleet_uid, grant)
            try:
                result = nudge_bound_task(ctx, route, self.package, request_id=request_id,
                    task_id=task_id, reason=reason, expected_assignment_id=expected_assignment_id,
                    admit_read=self._admit_read)
            except Exception:
                # Raw canonical failures can also disclose task or receipt
                # state, including after recording or native delivery.
                self._authorize(reader, fleet_uid, grant)
                raise
            self._authorize(reader, fleet_uid, grant)
            return result

    def inspect(self, reader: VerifiedReader, *, fleet: str, fleet_uid: str,
                task_id: str, manager_uid: str, request_id: str,
                expected_grant: OwnerNudgeGrant) -> OwnerNudgeObservation:
        """Inspect original evidence, including an old assignment; never nudge.

        Source and exact grant admission bracket the existing read owners. A
        later assignment/state does not reinterpret the retained request.
        """
        grant, ctx = self._bind(reader, fleet, fleet_uid, expected_grant)
        manager = self._target(ctx, task_id, manager_uid)
        retained = read_request(self.root, fleet_uid, request_id)
        if (retained.operation != "task.nudge" or retained.host_uid != self.host_uid
                or retained.fleet_uid != fleet_uid or retained.caller_uid != ctx.caller.uid
                or retained.task_id != task_id or retained.recipient_uid != manager.uid
                or retained.message_id is None or retained.route is None
                or retained.route.caller_alias != ctx.caller.alias
                or retained.route.caller_fleet_uid is not None
                or retained.route.recipient_alias != manager.alias
                or retained.route.manager_uid != manager.uid or retained.route.manager_alias != manager.alias
                or retained.route.peer_fleet_uid != fleet_uid):
            raise AccessDenied("request_scope_mismatch")
        try:
            observed = receipt(ctx, retained.message_id, destination=manager.alias, wait=0)
        except (MessageNotFoundError, MessageUnavailableError):
            observed = ReceiptObservation(message_id=retained.message_id, root=str(self.root),
                sender=None, destination=None, receipt_observation="unavailable",
                integrity_verdict="unknown", exit_code=MessageUnavailableError.exit_code,
                code=MessageUnavailableError.code,
                reason="Receiver proof unavailable; retained nudge does not prove delivery")
        if (observed.sender is not None and (observed.sender.uid != ctx.caller.uid
                or observed.sender.alias != ctx.caller.alias)
                or observed.destination is not None and (observed.destination.uid != manager.uid
                or observed.destination.alias != manager.alias)):
            raise AccessDenied("request_scope_mismatch")
        self._authorize(reader, fleet_uid, grant)
        return OwnerNudgeObservation(retained, observed)
