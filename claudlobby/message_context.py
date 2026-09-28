"""Frozen, recording-independent routes for generated bot communication.

The public operation holds activation admission. This resolver reads selected
configuration and activation identities, never Plane, bot.conf or mutable
authoring. It authorizes no send and makes no receiver-readiness claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .active_config import resolve_active_context
from .activation_identity import read_selected_identity_bindings
from .activation_state import ActivationError, read_selection
from .context import Context
from .message_transport import TransportDestination
from .operation_context import resolve_operation_scope
from .request_receipts import MessageRouteBinding, NativeDestination
from .resources import PackageResources
from .supervision import build_supervision_spec
from .task_operations import TaskActor


class MessageContextError(ValueError):
    """The generated origin or exact active destination cannot be bound."""


@dataclass(frozen=True)
class MessageRoute:
    origin: Context
    selected: Context
    peer_context: Context
    activation_id: str
    plan_id: str
    release_id: str
    host_uid: str
    caller_fleet_uid: str
    selected_fleet_uid: str
    peer_fleet_uid: str
    caller: TaskActor
    peer: TaskActor
    manager: TaskActor
    peer_destination: TransportDestination
    manager_destination: TransportDestination

    def receipt_binding(self) -> MessageRouteBinding:
        """Retain the route's historical targets before recording or transport."""
        return MessageRouteBinding(
            activation_id=self.activation_id, plan_id=self.plan_id,
            release_id=self.release_id, caller_fleet_uid=self.caller_fleet_uid,
            peer_fleet_uid=self.peer_fleet_uid, caller_alias=self.caller.alias,
            recipient_alias=self.peer.alias, manager_uid=self.manager.uid,
            manager_alias=self.manager.alias,
            peer_destination=_native_destination(self.peer_destination),
            manager_destination=_native_destination(self.manager_destination),
        )


def _native_destination(destination: TransportDestination) -> NativeDestination:
    """Serialize a validated native target without checking its later existence."""
    return NativeDestination(str(destination.root), destination.fleet,
                             destination.socket, destination.session,
                             str(destination.tmux_tmpdir))


def _parts(target: str, selected_fleet: str) -> tuple[str, str]:
    if not isinstance(target, str):
        raise MessageContextError("message target must name a declared bot")
    parts = target.split("/")
    if len(parts) == 1:
        fleet, bot = selected_fleet, parts[0]
    elif len(parts) == 2:
        fleet, bot = parts
    else:
        raise MessageContextError("message target must be BOT or FLEET/BOT")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in (fleet, bot)):
        raise MessageContextError("message target must be a literal fleet and bot")
    return fleet, bot


def _transport(context: Context, bot_id: str) -> TransportDestination:
    spec = build_supervision_spec(context.fleet.bots[bot_id], context.fleet, context.paths)
    if spec.bot_dir.name != bot_id or "TMUX_TMPDIR" not in spec.environment:
        raise MessageContextError("frozen supervision target is incomplete")
    return TransportDestination(context.paths.root, context.fleet.name, spec.label,
                                spec.bot_dir.name, Path(spec.environment["TMUX_TMPDIR"]))


def resolve_message_route(target: str, *, root: Path | None = None, fleet: str | None = None,
                          package: PackageResources | None = None) -> MessageRoute:
    """Resolve one exact bot and the selected fleet manager without Plane.

    Only a validated generated origin is supported in this slice. Explicit
    destination selectors never replace that origin; human identity registration
    belongs to the later public operation. Returned IDs and native targets are
    observations to freeze in a request before any recording or transport.
    """
    selected, origin = resolve_operation_scope(root=root, fleet=fleet, package=package)
    if origin is None or origin.bot_id is None:
        raise MessageContextError("ordinary messaging requires a generated bot origin")
    if origin.paths.root != selected.paths.root or origin.paths.package != selected.paths.package:
        raise MessageContextError("generated caller and selected fleet belong to different hosts")
    fleet_name, bot_id = _parts(target, selected.fleet.name)
    peer = (selected if fleet_name == selected.fleet.name else
            resolve_active_context(root=selected.paths.root, fleet=fleet_name,
                                   package=selected.paths.package))
    if peer.paths.root != selected.paths.root or peer.paths.package != selected.paths.package:
        raise MessageContextError("message target belongs to another host or package")
    if bot_id not in peer.fleet.bots:
        raise MessageContextError(f"bot {bot_id!r} is not in active fleet {fleet_name!r}")
    selection = read_selection(selected.paths.root)
    if selection is None:
        raise ActivationError("host has no active configuration")

    contexts = {context.fleet.name: context for context in (origin, selected, peer)}
    bindings = {}
    for name, context in contexts.items():
        item = read_selected_identity_bindings(context.paths.root, name,
                                               package=context.paths.package)
        if item["manager"] != context.fleet.manager or set(item["bots"]) != set(context.fleet.bots):
            raise MessageContextError(f"active identity bindings differ from frozen fleet {name}")
        bindings[name] = item
    if (len({item["host_uid"] for item in bindings.values()}) != 1
            or read_selection(selected.paths.root) != selection):
        raise MessageContextError("active host selection changed during message resolution")

    source_ids = bindings[origin.fleet.name]
    selected_ids = bindings[selected.fleet.name]
    peer_ids = bindings[peer.fleet.name]
    caller = TaskActor(source_ids["bots"][origin.bot_id],
                       f"bot:{origin.fleet.name}/{origin.bot_id}")
    recipient = TaskActor(peer_ids["bots"][bot_id], f"bot:{fleet_name}/{bot_id}")
    manager_id = selected.fleet.manager
    manager = TaskActor(selected_ids["manager_uid"],
                        f"bot:{selected.fleet.name}/{manager_id}")
    return MessageRoute(origin, selected, peer, selection["activation_id"],
                        selection["plan_id"], selection["release_id"],
                        source_ids["host_uid"], source_ids["fleet_uid"],
                        selected_ids["fleet_uid"], peer_ids["fleet_uid"], caller,
                        recipient, manager, _transport(peer, bot_id),
                        _transport(selected, manager_id))
