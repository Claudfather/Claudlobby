"""Internal owner-to-message adapter, with no HTTP or runtime entry point.

The caller must verify the principal/session carrier before constructing a
VerifiedReader. This module does not trust browser claims, supply a verifier,
or establish CSRF/origin protection. Only locally granted ordinary messages
to bots in one exact fleet are supported; no reply, task mutation or retry API.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from uuid import UUID

from ..active_config import resolve_active_context
from ..command_result import CommandOutput
from ..commands.message_write import deliver_bound_message
from ..message_context import resolve_message_route
from ..message_payload import MessageBody
from ..message_queries import (MessageNotFoundError, MessageUnavailableError,
                               ReceiptObservation, receipt)
from ..operation_context import bind_task_context
from ..request_queries import RequestView, read_request
from ..resources import PackageResources
from ..runtime_admission import RuntimeIdentity, mutation_admission
from .ids import read_host_uid
from .owner_access import AccessDenied, OwnerAccess, OwnerMessageGrant, VerifiedReader


# These selectors can turn an otherwise explicit operation into a generated
# bot/timer call. Refuse even empty carriers; never clear or reinterpret them.
_GENERATED_ENV = ("BOT_ID", "BOT_NAME", "BOT_DIR", "FLEET_ROOT", "FLEET_NAME",
                  "CLAUDLOBBY_FLEET", "CLAUDLOBBY_ROOT", "CLAUDLOBBY_RELEASE_ID")


@dataclass(frozen=True)
class OwnerMessageObservation:
    """Internal evidence, not a browser response or permission to resend.

    The retained request describes recording/transport; receiver evidence is
    separate. Neither a submitted transport nor a missing receipt is delivery.
    """

    request: RequestView
    receiver: ReceiptObservation


class OwnerMessages:
    """Pin one installation; reauthorize every operation against host state.

    Requires an already registered human actor and an explicit local message
    grant. Never registers a browser-provided alias or falls back to the OS
    account. Revocation stops subsequent admission, not an in-flight effect.
    Instances and their results must not outlive a principal/workspace change
    in a future browser adapter. No constructor or method enables a service.
    """

    def __init__(self, root: Path, *, package: PackageResources):
        self.root = Path(root)
        if not self.root.is_absolute() or self.root.resolve(strict=True) != self.root:
            raise ValueError("owner messages require an explicit unredirected root")
        self.host_uid = read_host_uid(self.root / "state")
        self.package = package
        self.access = OwnerAccess(self.root)

    def _authorize(self, reader: VerifiedReader, fleet_uid: str) -> OwnerMessageGrant:
        if type(reader) is not VerifiedReader:
            raise AccessDenied("verified_reader_required")
        if any(key in os.environ for key in _GENERATED_ENV):
            raise AccessDenied("generated_context_refused")
        if read_host_uid(self.root / "state") != self.host_uid:
            raise AccessDenied("wrong_deployment")
        return self.access.authorize_message(reader.token, reader.principal,
                                             host_uid=self.host_uid, fleet_uid=fleet_uid)

    def _bind(self, reader: VerifiedReader, fleet: str, fleet_uid: str):
        grant = self._authorize(reader, fleet_uid)
        # Explicit fleet names only: None must not select an ambient/default fleet.
        if not isinstance(fleet, str) or not fleet:
            raise AccessDenied("fleet_required")
        selected = resolve_active_context(root=self.root, fleet=fleet, package=self.package)
        ctx = bind_task_context(selected, operator_alias=grant.actor_alias)
        if (ctx.host_uid != self.host_uid or ctx.fleet_uid != grant.fleet_uid
                or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
                or ctx.caller_fleet_uid is not None):
            raise AccessDenied("message_binding_changed")
        return grant, ctx

    @staticmethod
    def _target(ctx, recipient_uid: str) -> str:
        matches = [name for name, actor in ctx.bots.items() if actor.uid == recipient_uid]
        if len(matches) != 1:
            raise AccessDenied("recipient_not_in_fleet")
        return matches[0]

    def send(self, reader: VerifiedReader, *, fleet: str, fleet_uid: str,
             recipient_uid: str, request_id: str, text: str) -> CommandOutput:
        """Authorize one ordinary send; reuse canonical idempotency and proof.

        Exceptions may follow a committed/native effect. Keep the original
        request UUID and inspect it; never retry automatically with a new UUID.
        The shared workflow only returns success after receiver integrity proof.
        """
        self._authorize(reader, fleet_uid)
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError("canonical request UUID required")
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError("message requires at most 2000 characters")
        body = MessageBody.from_input(text)
        identity = RuntimeIdentity(RuntimeIdentity.current().cli, self.package.native,
                                   self.package.artifact_id)
        with mutation_admission(self.root, identity=identity) as release:
            grant, ctx = self._bind(reader, fleet, fleet_uid)
            target = self._target(ctx, recipient_uid)
            route = resolve_message_route(target, root=self.root, fleet=ctx.context.fleet.name,
                                          package=self.package, caller_context=ctx)
            if (route.release_id != release.release_id or route.host_uid != self.host_uid
                    or route.selected_fleet_uid != fleet_uid or route.peer_fleet_uid != fleet_uid
                    or route.caller != ctx.caller or route.peer.uid != recipient_uid):
                raise AccessDenied("message_route_changed")
            # Route lookup can take time. Admit again at the dispatch boundary;
            # the action thereafter may complete even if access is revoked.
            if self._authorize(reader, fleet_uid) != grant:
                raise AccessDenied("message_binding_changed")
            return deliver_bound_message(route, body=body, request_id=request_id,
                                         caller_context=ctx)

    def inspect(self, reader: VerifiedReader, *, fleet: str, fleet_uid: str,
                recipient_uid: str, request_id: str) -> OwnerMessageObservation:
        """Read the original request and receiver proof, with no native effect."""
        grant, ctx = self._bind(reader, fleet, fleet_uid)
        recipient_alias = ctx.bots[self._target(ctx, recipient_uid)].alias
        retained = read_request(self.root, fleet_uid, request_id)
        if (retained.operation != "message.send" or retained.host_uid != self.host_uid
                or retained.fleet_uid != fleet_uid or retained.caller_uid != ctx.caller.uid
                or retained.recipient_uid != recipient_uid or retained.message_id is None
                or retained.route is None or retained.route.caller_alias != ctx.caller.alias
                or retained.route.recipient_alias != recipient_alias
                or retained.route.peer_fleet_uid != fleet_uid):
            raise AccessDenied("request_scope_mismatch")
        try:
            observed = receipt(ctx, retained.message_id, destination=recipient_alias, wait=0)
        except (MessageNotFoundError, MessageUnavailableError):
            # Preparation or a native effect can survive missing communication
            # recording. Preserve that evidence; absence never permits resend.
            observed = ReceiptObservation(
                message_id=retained.message_id, root=str(self.root), sender=None,
                destination=None, receipt_observation="unavailable",
                integrity_verdict="unknown", exit_code=MessageUnavailableError.exit_code,
                code=MessageUnavailableError.code,
                reason="Receiver proof unavailable; retained request does not prove delivery")
        if (observed.sender is not None and observed.sender.uid != ctx.caller.uid
                or observed.destination is not None and observed.destination.uid != recipient_uid):
            raise AccessDenied("request_scope_mismatch")
        if self._authorize(reader, fleet_uid) != grant:
            raise AccessDenied("message_binding_changed")
        return OwnerMessageObservation(retained, observed)
