"""Frozen routes for generated bots and already-bound local human callers.

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
from .request_receipts import MessageRouteBinding, NativeDestination, RecordedReplyBinding
from .resources import PackageResources
from .supervision import build_supervision_spec
from .task_operations import TaskActor, TaskOperationContext


class MessageContextError(ValueError):
    """The generated origin or exact active destination cannot be bound."""


@dataclass(frozen=True)
class MessageRoute:
    origin: Context | None
    selected: Context
    peer_context: Context
    activation_id: str
    plan_id: str
    release_id: str
    host_uid: str
    caller_fleet_uid: str | None
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
                          package: PackageResources | None = None,
                          caller_context: TaskOperationContext | None = None) -> MessageRoute:
    """Resolve one exact bot and the selected fleet manager without Plane.

    A human caller must already be bound by the operation identity owner under
    runtime admission. Explicit destination selectors never replace a generated
    origin. Returned IDs and native targets are observations to freeze before
    any recording or transport.
    """
    selected, origin = resolve_operation_scope(root=root, fleet=fleet, package=package)
    if origin is None:
        if (not isinstance(caller_context, TaskOperationContext)
                or not caller_context.caller.alias.startswith("human:")
                or caller_context.caller_fleet_uid is not None
                or caller_context.context.fleet.name != selected.fleet.name
                or caller_context.root != selected.paths.root
                or caller_context.context.paths.package != selected.paths.package):
            raise MessageContextError("local human messaging requires a bound selected caller")
    elif (origin.bot_id is None or caller_context is not None):
        raise MessageContextError("generated messaging requires its own caller origin")
    if origin is not None and (origin.paths.root != selected.paths.root
                               or origin.paths.package != selected.paths.package):
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

    contexts = {context.fleet.name: context for context in (origin, selected, peer) if context is not None}
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

    source_ids = bindings[origin.fleet.name] if origin is not None else bindings[selected.fleet.name]
    selected_ids = bindings[selected.fleet.name]
    peer_ids = bindings[peer.fleet.name]
    if origin is None:
        if (caller_context.host_uid != source_ids["host_uid"]
                or caller_context.fleet_uid != selected_ids["fleet_uid"]):
            raise MessageContextError("bound local human differs from selected host or fleet")
        caller = caller_context.caller
        caller_fleet_uid = None
    else:
        caller = TaskActor(source_ids["bots"][origin.bot_id],
                           f"bot:{origin.fleet.name}/{origin.bot_id}")
        caller_fleet_uid = source_ids["fleet_uid"]
    recipient = TaskActor(peer_ids["bots"][bot_id], f"bot:{fleet_name}/{bot_id}")
    manager_id = selected.fleet.manager
    manager = TaskActor(selected_ids["manager_uid"],
                        f"bot:{selected.fleet.name}/{manager_id}")
    return MessageRoute(origin, selected, peer, selection["activation_id"],
                        selection["plan_id"], selection["release_id"],
                        source_ids["host_uid"], caller_fleet_uid,
                        selected_ids["fleet_uid"], peer_ids["fleet_uid"], caller,
                        recipient, manager, _transport(peer, bot_id),
                        _transport(selected, manager_id))


@dataclass(frozen=True)
class HumanReplyRoute:
    """A generated bot answering its recorded human sender (#2068). There is no
    native destination, so nothing on this route is sent, only recorded."""

    origin: Context
    selected: Context
    activation_id: str
    plan_id: str
    release_id: str
    host_uid: str
    caller_fleet_uid: str
    selected_fleet_uid: str
    caller: TaskActor
    peer: TaskActor

    def receipt_binding(self, parent_message_id: str) -> RecordedReplyBinding:
        return RecordedReplyBinding(
            activation_id=self.activation_id, plan_id=self.plan_id,
            release_id=self.release_id, caller_fleet_uid=self.caller_fleet_uid,
            caller_alias=self.caller.alias, recipient_alias=self.peer.alias,
            parent_message_id=parent_message_id)


def resolve_human_reply_route(sender: TaskActor, *, root: Path | None = None,
                              fleet: str | None = None,
                              package: PackageResources | None = None) -> HumanReplyRoute:
    """Freeze a generated bot's answer to the recorded human `sender`, without Plane.

    Only a generated bot answers, inside its own fleet. The human is the
    parent's recorded sender, never a target the caller names, so `message
    send` to a human stays refused.
    """
    selected, origin = resolve_operation_scope(root=root, fleet=fleet, package=package)
    if origin is None or origin.bot_id is None:
        raise MessageContextError("only a generated bot answers a human sender")
    if (origin.paths.root != selected.paths.root or origin.paths.package != selected.paths.package
            or origin.fleet.name != selected.fleet.name):
        raise MessageContextError("a reply to a human stays in the caller's own fleet")
    if (not isinstance(sender, TaskActor) or not isinstance(sender.alias, str)
            or not re.fullmatch(r"human:[^\s:/]+", sender.alias)):
        raise MessageContextError("reply recipient is not a recorded human sender")
    selection = read_selection(selected.paths.root)
    if selection is None:
        raise ActivationError("host has no active configuration")
    ids = read_selected_identity_bindings(selected.paths.root, selected.fleet.name,
                                          package=selected.paths.package)
    if (ids["manager"] != selected.fleet.manager or set(ids["bots"]) != set(selected.fleet.bots)
            or origin.bot_id not in ids["bots"]):
        raise MessageContextError(f"active identity bindings differ from frozen fleet {selected.fleet.name}")
    if read_selection(selected.paths.root) != selection:
        raise MessageContextError("active host selection changed during message resolution")
    caller = TaskActor(ids["bots"][origin.bot_id], f"bot:{origin.fleet.name}/{origin.bot_id}")
    return HumanReplyRoute(origin, selected, selection["activation_id"], selection["plan_id"],
                           selection["release_id"], ids["host_uid"], ids["fleet_uid"],
                           ids["fleet_uid"], caller, sender)
