"""Permission decision/notification orchestration below CLI and HTTP.

Caller admission, canonical permission state and no-replay notification policy
remain with their existing owners.
"""

def _notification(request_id, permission, decision):
    from uuid import UUID, uuid5
    from .message_payload import MessageBody

    child = str(uuid5(UUID(request_id), "claudlobby.permission.notification.v1"))
    body = MessageBody.from_input(
        "[Claudlobby permission decision]\n"
        f"Decision: {decision}\nPermission: {permission.permission_id}\n"
        f"Task: {permission.task_id}\nAssignment: {permission.assignment_id or '-'}\n"
        f"Decision request: {request_id}\n"
        "Read the permission record for its exact scope and reason. "
        "Check permission again immediately before the action; this notice is not an execution ticket.")
    return child, body


def inspect_permission_notification(ctx, request_id, permission, decision):
    """Inspect only the original decision's targeted notice; never send or repair."""
    from .message_queries import receipt
    from .request_queries import read_request
    from .request_receipts import semantic_digest

    try:
        child, body = _notification(request_id, permission, decision)
        saved = read_request(ctx.root, ctx.fleet_uid, child)
        if (saved.operation != "message.send" or saved.host_uid != ctx.host_uid
                or saved.fleet_uid != ctx.fleet_uid or saved.caller_uid != ctx.caller.uid
                or saved.recipient_uid != permission.requester.uid or saved.message_id is None
                or saved.semantic_sha256 != semantic_digest({"body": body.text.encode("utf-8"), "kind": "notice"})
                or saved.route is None or saved.route.caller_alias != ctx.caller.alias
                or saved.route.caller_fleet_uid is not None
                or saved.route.recipient_alias != permission.requester.alias
                or saved.route.peer_fleet_uid != ctx.fleet_uid):
            return "unknown"
        observed = receipt(ctx, saved.message_id, destination=permission.requester.alias, wait=0)
        if (observed.sender is None or observed.sender.uid != ctx.caller.uid
                or observed.sender.alias != ctx.caller.alias
                or observed.destination is None or observed.destination.uid != permission.requester.uid
                or observed.destination.alias != permission.requester.alias):
            return "unknown"
        if observed.receipt_observation == "received" and observed.integrity_verdict == "delivered":
            return "received"
        if observed.integrity_verdict in {"altered", "truncated"}:
            return "failed"
    except Exception:
        pass  # Missing evidence cannot certify delivery or license a resend.
    return "unknown"


def decide_and_notify_permission(ctx, package, request_id, permission_id, *, decision,
                                 scope_sha256, reason, release_id=None, admit_read=None,
                                 admit_notification=None):
    """Canonical decision first, then one durable notice to its original requester.

    The caller holds runtime admission and authenticates the human. A replay or
    receipt read only inspects notification evidence; a crash gap never sends.
    """
    from .message_context import resolve_message_route
    from .task_permissions import decide_permission
    from .application_messages import deliver_bound_message

    result = decide_permission(ctx, request_id, permission_id, decision=decision,
        scope_sha256=scope_sha256, reason=reason, release_id=release_id, admit_read=admit_read)
    if not result.replayed:
        try:
            permission = result.permission
            target = next(name for name, actor in ctx.bots.items() if actor == permission.requester)
            route = resolve_message_route(target, root=ctx.root, fleet=ctx.context.fleet.name,
                                          package=package, caller_context=ctx)
            if (route.host_uid != ctx.host_uid or route.selected_fleet_uid != ctx.fleet_uid
                    or route.peer_fleet_uid != ctx.fleet_uid or route.caller != ctx.caller
                    or route.peer != permission.requester or route.caller_fleet_uid is not None
                    or release_id is not None and route.release_id != release_id):
                raise ValueError("permission notification route changed")
            if admit_notification is not None:
                admit_notification()
            child, body = _notification(request_id, permission, decision)
            deliver_bound_message(route, body=body, request_id=child, kind="notice",
                                  caller_context=ctx, require_durable_request=True)
        except Exception:
            pass  # The recorded decision survives an unavailable bot or carrier.
    return result, inspect_permission_notification(ctx, request_id, result.permission, decision)
