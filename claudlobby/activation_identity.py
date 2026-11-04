"""Verified current identity bindings for operational fleets.

Git configuration and legacy activation journals retain verified registry results.
Selected reads never open Plane. Explicit registry seeding uses the canonical
committed scan, then the read-only binding owner; this module never invents IDs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from .active_config import context_from_plan, read_current_config, _selected_activation, _plan_for_selection
from .activation_state import ActivationError, ActivationRefusal
from .config_plan import ConfigPlan, RenderedConfiguration
from .operation_context import OperationContextError, _host_uid
from .plane.ids import ID_PATTERNS
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
    contexts = {name: context_from_plan(plan, name, package=package) for name in plan.fleets}
    _validate_roster(plan.data_root, contexts, payload)


def validate_current_identity_bindings(root: Path, payload: dict, *, contexts: dict) -> None:
    """Bind recorded IDs to committed fleet inputs and the existing host, without Plane."""
    if (not isinstance(payload, dict) or set(payload) != {"schema", "host_uid", "fleets"}
            or type(payload["schema"]) is not int or payload["schema"] != 1):
        raise ActivationError("current configuration identity bindings are invalid")
    _validate_roster(root, contexts, payload)


def _validate_roster(root: Path, contexts: dict, payload: dict) -> None:
    """The single host/manager/bot identity model for both committed carriers."""
    if (not _uid(payload["host_uid"], "host") or not isinstance(payload["fleets"], dict)
            or set(payload["fleets"]) != set(contexts)):
        raise ActivationError("active identity bindings differ from the frozen fleets")
    try:
        actual_host = _host_uid(root)
    except OperationContextError as exc:
        raise ActivationError("active host identity is unavailable") from exc
    if payload["host_uid"] != actual_host:
        raise ActivationError("active identity bindings belong to another host")

    fleet_uids, actor_uids = set(), set()
    for name, context in contexts.items():
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


def identity_bindings_from_registry(plan: ConfigPlan | RenderedConfiguration, registry: list[dict], *,
                                    package: PackageResources) -> dict:
    """Retain existing registry results against exact frozen fleet inputs; never mint."""
    if (not isinstance(registry, list) or len(registry) != len(plan.fleets)
            or any(not isinstance(row, dict) or not isinstance(row.get("fleet"), str)
                   or not isinstance(row.get("bindings"), dict)
                   or not {"host_uid", "fleet_uid", "manager_uid", "bots"} <= set(row["bindings"])
                   or not _uid(row["bindings"]["host_uid"], "host")
                   for row in registry)
            or {row["fleet"] for row in registry} != set(plan.fleets)):
        raise ActivationError("candidate registry bindings do not cover the frozen fleets")
    hosts = {row["bindings"]["host_uid"] for row in registry}
    if len(hosts) != 1:
        raise ActivationError("candidate registry bindings disagree on the host")
    contexts = {name: context_from_plan(plan, name, package=package) for name in plan.fleets}
    payload = {
        "schema": 1, "host_uid": hosts.pop(),
        "fleets": {row["fleet"]: {
            "fleet_uid": row["bindings"]["fleet_uid"],
            "manager": contexts[row["fleet"]].fleet.manager,
            "manager_uid": row["bindings"]["manager_uid"],
            "bots": row["bindings"]["bots"],
        } for row in registry},
    }
    if package.installation is not None:
        if package.installation.data_root != plan.data_root:
            raise ActivationError("current identity bindings belong to another data root")
        validate_current_identity_bindings(plan.data_root, payload, contexts=contexts)
    else:
        if not isinstance(plan, ConfigPlan):
            raise ActivationError("rendered identity bindings require a Git installation")
        payload.update(root=str(plan.data_root), plan_id=plan.plan_id,
                       release_id=plan.release_id, release_seal=plan.release_seal)
        validate_identity_bindings(plan, payload, package=package)
    return payload


def registry_scan_refusal(contexts) -> str | None:
    """Collect every opt-out/unavailable reason before recording or stopping anything."""
    from .plane.registry_emit import activation_scan_refusal
    reasons = (activation_scan_refusal(context.paths, context.fleet) for context in contexts)
    return "; ".join(dict.fromkeys(reason for reason in reasons if reason)) or None


def preflight_registry_scan(contexts) -> None:
    """Refuse a required registry scan before its first effect."""
    reason = registry_scan_refusal(contexts)
    if reason:
        raise ActivationRefusal(reason)


def _registry_scan(context):
    from .plane.registry_emit import run_generate_scan
    return run_generate_scan(context.paths, context.fleet, require_commit=True)


def _registry_bindings(context) -> dict:
    """Project the canonical existing identity reader's result for one frozen fleet."""
    from .operation_context import bind_task_context
    bound = bind_task_context(context, origin=replace(context, bot_id=context.fleet.manager))
    return {"host_uid": bound.host_uid, "fleet_uid": bound.fleet_uid, "manager_uid": bound.caller.uid,
            "bots": {bot: actor.uid for bot, actor in bound.bots.items()}}


def scan_registry(plan: ConfigPlan | RenderedConfiguration, *, package: PackageResources,
                  contexts=None) -> list[dict]:
    """Seed the frozen roster through committed scans and bind its canonical IDs.

    Schema/recorder initialization and mutation admission belong to the caller.
    A partial or unknown scan keeps its original exception and receipt evidence;
    this owner never retries it or publishes a current configuration.
    """
    if not plan.fleets or len(set(plan.fleets)) != len(plan.fleets):
        raise ActivationError("candidate registry requires an unambiguous frozen fleet roster")
    if package.installation is not None and package.installation.data_root != plan.data_root:
        raise ActivationError("current identity bindings belong to another data root")
    frozen = tuple(context_from_plan(plan, name, package=package) for name in sorted(plan.fleets))
    if contexts is not None and tuple(contexts) != frozen:
        raise ActivationError("candidate registry contexts differ from the frozen fleets")
    preflight_registry_scan(frozen)
    rows = []
    for context in frozen:
        result = _registry_scan(context)
        if (not isinstance(result, dict) or result.get("complete") is not True
                or result.get("recording") != "committed"):
            raise ActivationError("candidate registry scan is disabled, incomplete or uncommitted; no bots started")
        try:
            bindings = _registry_bindings(context)
        except OperationContextError as exc:
            raise ActivationError("candidate registry identity binding failed; no bots started") from exc
        rows.append({"fleet": context.fleet.name, "scan": result, "bindings": bindings})
    identity_bindings_from_registry(plan, rows, package=package)
    return rows


def read_registry_identity_bindings(plan: ConfigPlan | RenderedConfiguration, *, package: PackageResources) -> dict:
    """Read already registered identities for frozen fleets; no scan, repair or write."""
    rows = []
    for name in plan.fleets:
        context = context_from_plan(plan, name, package=package)
        try:
            bindings = _registry_bindings(context)
        except OperationContextError as exc:
            raise ActivationError("existing Plane registry identity bindings are unavailable") from exc
        rows.append({"fleet": name, "bindings": bindings})
    return identity_bindings_from_registry(plan, rows, package=package)


def read_selected_identity_bindings(root: Path, fleet: str, *,
                                    package: PackageResources | None = None) -> dict:
    """Read one active fleet binding without Plane; a mutating caller holds admission."""
    package = package if package is not None else get_resources()
    if package.installation is not None:
        snapshot = read_current_config(root, installation=package.installation, revision=package.source_revision)
        payload = snapshot["identities"]
        if payload is None:
            raise ActivationError("current configuration identities are not initialized; no action authority")
        return _fleet_bindings(payload, fleet)
    selected, record = _selected_activation(root)
    if record.status != "active":
        raise ActivationError("selected identity activation is incomplete")
    plan = _plan_for_selection(root, selected, package)
    return _selected_bindings(plan, record, fleet, package=package)


def _selected_bindings(plan: ConfigPlan, record, fleet: str, *,
                       package: PackageResources) -> dict:
    """Validate recorded binding observation; callers separately require active."""
    if fleet not in plan.fleets:
        raise ActivationError("requested fleet is not in the selected activation")
    payload = record.body.get("identity_bindings")
    validate_identity_bindings(plan, payload, package=package)
    return _fleet_bindings(payload, fleet)


def _fleet_bindings(payload, fleet):
    if fleet not in payload["fleets"]:
        raise ActivationError("requested fleet is not in the committed identity bindings")
    item = payload["fleets"][fleet]
    return {"host_uid": payload["host_uid"], "fleet_uid": item["fleet_uid"],
            "manager": item["manager"], "manager_uid": item["manager_uid"],
            "bots": dict(item["bots"])}
