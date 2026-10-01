"""Verified activation identity bindings for selected operational fleets.

The activation journal retains the result of the pre-start Plane registry check.
Reading it never opens Plane or creates an identity.
"""

from __future__ import annotations

from pathlib import Path
import re

from .active_config import context_from_plan
from .activation_state import ActivationError, read_activation, read_selection
from .config_plan import ConfigPlan, read_plan
from .operation_context import OperationContextError, _host_uid
from .plane.ids import ID_PATTERNS
from .releases import read_release
from .resources import PackageResources, get_resources


def _uid(value, kind: str) -> bool:
    return isinstance(value, str) and re.fullmatch(ID_PATTERNS[kind], value) is not None


def validate_identity_bindings(plan: ConfigPlan, payload: dict, *, package: PackageResources) -> None:
    """Check the recorded IDs against the exact plan and current host identity."""
    if (not isinstance(payload, dict) or set(payload) != {
            "schema", "root", "plan_id", "release_id", "release_seal", "host_uid", "fleets"}
            or payload["schema"] != 1 or payload["root"] != str(plan.data_root)
            or payload["plan_id"] != plan.plan_id or payload["release_id"] != plan.release_id
            or payload["release_seal"] != plan.release_seal or not _uid(payload["host_uid"], "host")
            or not isinstance(payload["fleets"], dict)
            or len(set(plan.fleets)) != len(plan.fleets)
            or set(payload["fleets"]) != set(plan.fleets)):
        raise ActivationError("active identity bindings differ from the selected plan")
    try:
        actual_host = _host_uid(plan.data_root)
    except OperationContextError as exc:
        raise ActivationError("active host identity is unavailable") from exc
    if payload["host_uid"] != actual_host:
        raise ActivationError("active identity bindings belong to another host")

    fleet_uids, actor_uids = set(), set()
    for name in plan.fleets:
        context = context_from_plan(plan, name, package=package)
        item = payload["fleets"][name]
        if (not isinstance(item, dict) or set(item) != {"fleet_uid", "manager", "manager_uid", "bots"}
                or not _uid(item["fleet_uid"], "fleet")
                or not _uid(item["manager_uid"], "actor")
                or item["manager"] != context.fleet.manager
                or not isinstance(item["bots"], dict)
                or set(item["bots"]) != set(context.fleet.bots)
                or item["bots"].get(item["manager"]) != item["manager_uid"]
                or any(not _uid(uid, "actor") for uid in item["bots"].values())):
            raise ActivationError(f"active identity bindings differ from frozen fleet {name}")
        if item["fleet_uid"] in fleet_uids or actor_uids.intersection(item["bots"].values()):
            raise ActivationError("active identity bindings are ambiguous across fleets")
        fleet_uids.add(item["fleet_uid"])
        actor_uids.update(item["bots"].values())
    if len(actor_uids) != sum(len(item["bots"]) for item in payload["fleets"].values()):
        raise ActivationError("active bot actor identities are ambiguous")


def identity_bindings_from_registry(plan: ConfigPlan, registry: list[dict], *,
                                    package: PackageResources) -> dict:
    """Retain only IDs already verified by the activation registry gate."""
    if len(registry) != len(plan.fleets) or {row["fleet"] for row in registry} != set(plan.fleets):
        raise ActivationError("candidate registry bindings do not cover the frozen fleets")
    hosts = {row["bindings"]["host_uid"] for row in registry}
    if len(hosts) != 1:
        raise ActivationError("candidate registry bindings disagree on the host")
    payload = {
        "schema": 1, "root": str(plan.data_root), "plan_id": plan.plan_id,
        "release_id": plan.release_id, "release_seal": plan.release_seal,
        "host_uid": hosts.pop(),
        "fleets": {row["fleet"]: {
            "fleet_uid": row["bindings"]["fleet_uid"],
            "manager": context_from_plan(plan, row["fleet"], package=package).fleet.manager,
            "manager_uid": row["bindings"]["manager_uid"],
            "bots": row["bindings"]["bots"],
        } for row in registry},
    }
    validate_identity_bindings(plan, payload, package=package)
    return payload


def read_selected_identity_bindings(root: Path, fleet: str, *,
                                    package: PackageResources | None = None) -> dict:
    """Read one active fleet binding without Plane; a mutating caller holds admission."""
    package = package if package is not None else get_resources()
    selected = read_selection(root)
    if selected is None:
        raise ActivationError("host has no active configuration")
    record = read_activation(root, selected["activation_id"])
    if (record.status != "active" or record.body["intent"]["plan_id"] != selected["plan_id"]
            or record.body["intent"]["release_id"] != selected["release_id"]):
        raise ActivationError("selected identity activation is incomplete")
    release = read_release(root, selected["release_id"], verify_files=False)
    if release.native_path != package.native or release.inputs.artifact_id != package.artifact_id:
        raise ActivationError("active identity bindings and executing package differ")
    plan = read_plan(root, selected["plan_id"])
    if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
        raise ActivationError("active identity bindings and selected release differ")
    if fleet not in plan.fleets:
        raise ActivationError("requested fleet is not in the selected activation")
    payload = record.body.get("identity_bindings")
    validate_identity_bindings(plan, payload, package=package)
    item = payload["fleets"][fleet]
    return {"host_uid": payload["host_uid"], "fleet_uid": item["fleet_uid"],
            "manager": item["manager"], "manager_uid": item["manager_uid"],
            "bots": dict(item["bots"])}
