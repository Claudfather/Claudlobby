"""Owned units in the failed state, read for fleet-pulse once a sweep (#2532).

A job unit raises an alert when it fails only through a failure template it
names in OnFailure= (#2522). A host job, a launchd job, a unit whose OnFailure=
was removed and the template's own instances name none, so fleet-pulse asks
this read which owned units are failed now and reports each episode once.

Owned is the selected plan's unit manifest (``planned_units``), never a unit's
spelling: a unit is owned when the plan declares it, and a template's instance
when the plan declares the template (``template_of_instance``). It reads the
manifest, not ``current_declarations``, which refuses the whole estate when one
generated file has drifted from its plan: a drifted unit must not silence the
alert for every other one. Each row names its scope (host, fleet or bot); which
of them a reader reports is its own choice (fleet-pulse leaves bots to its
per-bot checks).

The native state is the supervisor adapter's one read (``svc_inventory_failed``):

- ``failed``: systemd holds the unit in ActiveState=failed, or launchd has the
  job loaded, not running, with a non-zero last exit status.
- ``running``: systemd has the unit between states (activating, deactivating,
  reloading), or launchd runs the job again after a non-zero exit. A reader
  keeps an open episode open through it and opens none.

Any other owned unit is in neither list. Nothing here changes a unit.
"""

from __future__ import annotations

from pathlib import Path

from .supervision_inventory import (Adapter, InventoryError, launchd_list_rows, systemd_show_blocks,
                                    template_of_instance)

_STATE = {"failed": "failed", "activating": "running", "deactivating": "running", "reloading": "running"}
_SYSTEMD_FIELDS = ("ActiveState", "SubState", "Result", "ExecMainStatus")


def _row(name, declaration, surface, state, detail, template=None) -> dict:
    return {"unit": name, "scope": declaration.scope, "fleet": declaration.fleet, "bot": declaration.bot,
            "surface": surface, "state": state, "detail": detail, "template": template}


def read_failed_units(root: Path, *, adapter: Adapter | None = None) -> dict:
    """The selected release's owned units that are failed or running now."""
    from .activation_state import read_selection
    from .config_plan import read_plan
    from .config_units import planned_units

    selected = read_selection(root)
    if selected is None:
        raise InventoryError("no release is selected on this host, so it owns no unit yet")
    plan = read_plan(root, selected["plan_id"])
    if plan.release_id != selected["release_id"]:
        raise InventoryError("the selected plan differs from the selected release")
    if adapter is None:
        from .context import resolve_paths
        adapter = Adapter(resolve_paths(root=root).package)
    head, _, body = adapter.read("svc_inventory_failed").partition("\n")
    manager = head.removeprefix("manager\t")
    if manager not in ("Linux", "Darwin"):
        raise InventoryError("the supervision adapter named no manager")
    declared = {declaration.source.name: declaration for declaration, _ in planned_units(plan, manager)}
    units = []
    if manager == "Linux":
        for block in systemd_show_blocks(body):
            if not block.get("Id") or not block.get("ActiveState"):
                raise InventoryError("a systemctl show block has no Id or ActiveState")
            name, state = block["Id"], _STATE.get(block["ActiveState"])
            template = template_of_instance(name)
            declaration = declared.get(name) or declared.get(template or "")
            if declaration is not None and state is not None:
                units.append(_row(name, declaration, "systemd", state,
                                  " ".join(f"{key}={block[key]}" for key in _SYSTEMD_FIELDS if key in block),
                                  declaration.source.name if declaration.source.name != name else None))
    else:
        for pid, status, label in launchd_list_rows(body):
            declaration = declared.get(label + ".plist")
            if declaration is not None and status != "0":
                units.append(_row(label, declaration, "launchd", "failed" if pid == "-" else "running",
                                  f"last exit status {status}" + ("" if pid == "-" else f", running as pid {pid}")))
    units.sort(key=lambda unit: (unit["scope"], unit["fleet"] or "", unit["unit"]))
    return {"release_id": plan.release_id, "platform": manager, "declared": len(declared), "units": units}
