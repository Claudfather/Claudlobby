"""Request one selected, enrolled host timer job through its native unit."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .activation_enrollment import _target, selected_phase_entries
from .activation_state import read_selection
from .config import load_host_jobs
from .config_plan import read_plan
from .config_units import current_declarations, planned_units
from .runtime_admission import RuntimeIdentity, mutation_admission, validate_unit_admission
from .supervision_inventory import Adapter, InventoryError, _catalog, collect_enrollment


class HostJobError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False,
                 unavailable: bool = False, release_id: str | None = None,
                 target: str | None = None):
        self.effect_attempted = effect_attempted
        self.unavailable = unavailable
        self.release_id = release_id
        self.target = target
        super().__init__(reason)


@dataclass(frozen=True)
class HostJobRun:
    name: str
    release_id: str
    target: str
    native_outcome: str = "requested"
    completion: str = "unobserved"


def run_host_job(root: Path, name: str, *, adapter: Adapter | None = None) -> HostJobRun:
    """Invoke an already installed oneshot; never install, unmask or retry it."""
    if name == "pull-root":
        raise HostJobError("pull-root is retired; release changes require host activation")
    with mutation_admission(root, identity=RuntimeIdentity.current()) as release:
        cfg = load_host_jobs().get(name)
        if cfg is None:
            raise HostJobError("host job is not packaged in the selected release")
        if cfg.get("unit") == "service" or cfg.get("type", "oneshot") != "oneshot":
            raise HostJobError("host job run requires a scheduled oneshot job")
        if cfg.get("enroll", True) is False:
            raise HostJobError("host job is disabled by this host's effective configuration")
        selected = read_selection(root)
        if selected is None or selected["release_id"] != release.release_id:
            raise HostJobError("selected host job release changed")
        plan = read_plan(root, selected["plan_id"])
        if plan.release_id != release.release_id or plan.release_seal != release.seal_sha256:
            raise HostJobError("selected host job plan differs from its release")
        adapter = adapter or Adapter()
        if adapter.package.native != release.native_path:
            raise HostJobError("host job adapter differs from selected release")
        entries = selected_phase_entries(root, "producers")
        manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
        stem = f"claudlobby-{name}"
        if manager == "Darwin" and domain == f"user/{os.getuid()}":
            selected_gui = [entry["target"] for entry in entries
                            if Path(entry["source"]).name == stem + ".plist"
                            and entry["target"] == f"gui/{os.getuid()}/{stem}"]
            if len(selected_gui) == 1:
                adapter = adapter.in_selected_gui(selected_gui[0])
                manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
        wanted = ({stem + ".plist"} if manager == "Darwin" else
                  {stem + ".service", stem + ".timer"})
        planned = [(declaration, item) for declaration, item in planned_units(plan, manager)
                   if declaration.source.name in wanted]
        if (len(planned) != len(wanted) or {declaration.source.name for declaration, _ in planned} != wanted
                or any(declaration.scope != "host" or item["phase"] != "producers"
                       or item["enroll"] is not True for declaration, item in planned)):
            raise HostJobError("host job has no complete selected, enrolled timer unit")
        if manager == "Linux":
            timer = next(declaration for declaration, _ in planned if declaration.source.suffix == ".timer")
            if timer.service != stem + ".service":
                raise HostJobError("host timer does not activate its selected service")
        declarations = current_declarations(plan, manager)
        selected_entries = [entry for entry in entries if Path(entry["source"]).name in wanted]
        if len(selected_entries) != len(wanted):
            raise HostJobError("host job has no complete frozen native placement")
        by_source = {str(declaration.source): (declaration, item) for declaration, item in planned}
        by_name = {}
        for entry in selected_entries:
            declaration, item = by_source.get(entry["source"], (None, None))
            installed = Path(entry["installed"])
            if (declaration is None or entry["target"] != _target(manager, domain, declaration.source)
                    or entry["after"] != {"kind": "file", "sha256": item["sha256"], "mode": item["mode"]}
                    or entry["working_directory"] != str(declaration.working_directory)
                    or entry["environment"] != dict(declaration.environment)
                    or installed.name != declaration.source.name or not installed.is_absolute()
                    or installed.is_symlink() or installed.parent not in directories
                    or installed.parent.resolve() != installed.parent):
                raise HostJobError("selected host job placement differs from reviewed unit")
            validate_unit_admission(release, declaration, item, plan.blob(item["sha256"]))
            by_name[declaration.source.name] = entry
        if len(by_name) != len(wanted):
            raise HostJobError("selected host job native placement is ambiguous")
        inventory = collect_enrollment(root, declarations, adapter=adapter,
                                       only_names=frozenset(wanted)).require_complete()
        if len(inventory.units) != len(wanted):
            raise HostJobError("selected host job inventory is incomplete", unavailable=True)
        for unit in inventory.units:
            entry = by_name[unit.declaration.source.name]
            if (unit.target != entry["target"] or len(unit.installed) != 1
                    or unit.installed[0].path != entry["installed"]
                    or dict(unit.properties).get("LoadState") != "loaded"):
                raise HostJobError("host job is not installed at its frozen placement", unavailable=True)
        service = by_name[stem + (".plist" if manager == "Darwin" else ".service")]
        target, installed = service["target"], service["installed"]
        try:
            result = adapter.call("svc_host_job_run_exact", installed, target, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostJobError("host job native request outcome is unknown", effect_attempted=True,
                               unavailable=True, release_id=release.release_id, target=target) from exc
        invoked = result.stdout.startswith("invoking\n")
        if result.returncode or result.stdout != "invoking\nrun-requested\n":
            raise HostJobError("host job native request was refused or is unverified",
                               effect_attempted=invoked or result.returncode == 0, unavailable=True,
                               release_id=release.release_id, target=target)
        return HostJobRun(name, release.release_id, target)
