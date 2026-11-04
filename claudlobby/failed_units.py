"""Owned units in the failed state, read for fleet-pulse once a sweep (#2532).

A job unit raises an alert when it fails only through a failure template it
names in OnFailure= (#2522). A host job, a launchd job, a unit whose OnFailure=
was removed and the template's own instances name none, so fleet-pulse asks
this read which owned units are failed now and reports each episode once.

Owned is committed Git configuration, never a unit's spelling: a unit is owned
when the snapshot declares it, and a template's instance when the snapshot
declares the template (``template_of_instance``). It reads the
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
import os

from .supervision_inventory import (Adapter, InventoryError, launchd_list_rows, systemd_show_blocks,
                                    template_of_instance, UnitDeclaration, _catalog)

_STATE = {"failed": "failed", "activating": "running", "deactivating": "running", "reloading": "running"}
_SYSTEMD_FIELDS = ("ActiveState", "SubState", "Result", "ExecMainStatus")


def _row(name, declaration, surface, state, detail, template=None) -> dict:
    return {"unit": name, "scope": declaration.scope, "fleet": declaration.fleet, "bot": declaration.bot,
            "surface": surface, "state": state, "detail": detail, "template": template}


def read_failed_units(root: Path, *, adapter: Adapter | None = None) -> dict:
    """The current runtime's owned units that are failed or running now."""

    package = getattr(adapter, "package", None)
    if adapter is None:
        from .context import resolve_paths
        package = resolve_paths(root=root).package
        adapter = Adapter(package)
    installation = getattr(package, "installation", None)
    if installation is None:
        raise InventoryError("failed-unit observation requires a managed Git installation")
    from .active_config import read_current_config
    if package.native != installation.native or not package.source_revision:
        raise InventoryError("failed-unit adapter differs from current Git installation")
    snapshot = read_current_config(root, installation=installation, revision=package.source_revision)
    placement = snapshot.get("supervision")
    if placement is None:
        raise InventoryError("failed-unit observation requires committed native placement")
    manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    if (manager == "Darwin" and placement["domain"] == f"gui/{os.getuid()}"
            and domain == f"user/{os.getuid()}"):
        names = [Path(item["source"]).stem for item in snapshot["units"] if item["platform"] == manager]
        if not names or not hasattr(adapter, "in_selected_gui"):
            raise InventoryError("committed GUI native adapter is unavailable")
        adapter = adapter.in_selected_gui(f"{placement['domain']}/{names[0]}")
        manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    if (manager != placement["manager"] or domain != placement["domain"]
            or Path(placement["directory"]) not in directories):
        raise InventoryError("failed-unit adapter differs from committed native placement")
    head, _, body = adapter.read("svc_inventory_failed").partition("\n")
    manager = head.removeprefix("manager\t")
    if manager not in ("Linux", "Darwin"):
        raise InventoryError("the supervision adapter named no manager")
    if manager != placement["manager"]:
        raise InventoryError("failed-unit native manager changed during observation")
    # read_current_config already validates every metadata/installation
    # binding. As with planned_units, this diagnostic does not require all
    # generated files to be pristine: drift must not silence other alerts.
    declarations = (UnitDeclaration(Path(item["source"]), item["scope"], Path(item["working_directory"]),
                     "", tuple(sorted(item["environment"].items())), item["fleet"], item["bot"], item["service"])
                    for item in snapshot["units"] if item["platform"] == manager)
    declared = {declaration.source.name: declaration for declaration in declarations}
    if len(declared) != sum(item["platform"] == manager for item in snapshot["units"]):
        raise InventoryError("committed failed-unit names are ambiguous")
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
    if read_current_config(root, installation=installation, revision=package.source_revision) != snapshot:
        raise InventoryError("committed configuration changed during failed-unit observation")
    return {"release_id": None, "source_revision": snapshot["revision"],
            "config_fingerprint": snapshot["fingerprint"],
            "platform": manager, "declared": len(declared), "units": units}
