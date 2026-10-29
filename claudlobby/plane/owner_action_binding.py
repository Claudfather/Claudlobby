"""Exact identity checks shared by the four owner action-context builders.

Callers retain session/grant admission, explicit room selection and closing
rechecks. This checks already resolved evidence; it neither grants nor caches.
"""
from ..task_operations import TaskOperationContext
from .owner_access import (AccessDenied, OwnerMessageGrant, OwnerNudgeGrant,
                           OwnerFeedbackGrant, OwnerPermissionGrant)


def check_owner_binding(ctx: TaskOperationContext, *, host: str, bindings: dict,
                        grant: OwnerMessageGrant | OwnerNudgeGrant | OwnerFeedbackGrant | OwnerPermissionGrant,
                        refusal: str) -> None:
    if (ctx.host_uid != host or ctx.fleet_uid != grant.fleet_uid
            or bindings["host_uid"] != host or bindings["fleet_uid"] != grant.fleet_uid
            or ctx.caller.uid != grant.actor_uid or ctx.caller.alias != grant.actor_alias
            or ctx.caller_fleet_uid is not None
            or {name: actor.uid for name, actor in ctx.bots.items()} != bindings["bots"]):
        raise AccessDenied(refusal)
