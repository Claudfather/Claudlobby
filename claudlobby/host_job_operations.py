"""Request one selected, enrolled host timer job through its native unit."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .config import _load_system_defaults, host_unit_name
from .config_units import current_git_declarations
from .runtime_admission import GitCurrentRuntime, RuntimeIdentity, mutation_admission
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
    release_id: str | None
    target: str
    native_outcome: str = "requested"
    completion: str = "unobserved"
    revision: str | None = None
    config_fingerprint: str | None = None


def _bind_git_job(current: GitCurrentRuntime, name: str, adapter: Adapter | None = None):
    """Read one committed, enrolled job binding without starting it."""
    root, installation = current.installation.data_root, current.installation
    packaged_jobs = (_load_system_defaults().get("host") or {}).get("jobs") or {}
    if name not in packaged_jobs:
        raise HostJobError("host job is not packaged in the current installation")
    adapter = adapter or Adapter()
    if (adapter.package.installation != installation or adapter.package.native != installation.native
            or adapter.package.source_revision != current.revision):
        raise HostJobError("host job adapter differs from the current installation")
    manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    placement = current.snapshot.get("supervision")
    if placement is None:
        raise HostJobError("host job requires committed native placement")
    if manager != placement["manager"]:
        raise HostJobError("host job adapter differs from committed native manager")
    declarations = current_git_declarations(current.snapshot, manager)
    metadata = {item["source"]: item for item in current.snapshot["units"]}
    # Prefixes are frozen in the committed labels. Use the existing label codec
    # rather than rereading a pending host.unit_prefix override.
    families = []
    for declaration in declarations:
        stem = declaration.source.stem
        if declaration.scope != "host" or not stem.endswith("-" + name):
            continue
        suffixes = [key for key in packaged_jobs if stem.endswith("-" + key)]
        if (suffixes == [name]
                and host_unit_name(name, prefix=stem[:-len(name) - 1]) == stem):
            families.append(stem)
    if len(set(families)) != 1:
        raise HostJobError("host job has no unique committed native family")
    stem = families[0]
    wanted = {stem + ".plist"} if manager == "Darwin" else {stem + ".service", stem + ".timer"}
    selected = [declaration for declaration in declarations if declaration.source.name in wanted]
    if (len(selected) != len(wanted) or any(metadata[str(declaration.source)]["phase"] != "producers"
            or not metadata[str(declaration.source)]["enroll"] for declaration in selected)):
        raise HostJobError("host job is disabled or lacks a complete committed timer family")
    service = next(declaration for declaration in selected if declaration.service is None)
    admission = metadata[str(service.source)]["admission"]
    if admission is None or admission["mode"] != "oneshot":
        raise HostJobError("host job run requires a scheduled oneshot job")
    if (manager == "Darwin" and placement["domain"] == f"gui/{os.getuid()}"
            and domain == f"user/{os.getuid()}"):
        # Select only the committed GUI domain, never guess another placement.
        if not hasattr(adapter, "in_selected_gui"):
            raise HostJobError("committed GUI host job adapter is unavailable")
        adapter = adapter.in_selected_gui(f"{placement['domain']}/{stem}")
        manager, domain, directories, _, _ = _catalog(adapter.read("svc_inventory_catalog"))
    if (manager != placement["manager"] or domain != placement["domain"]
            or Path(placement["directory"]) not in directories):
        raise HostJobError("host job adapter differs from committed native placement")
    inventory = collect_enrollment(root, tuple(selected), adapter=adapter,
                                   only_names=frozenset(wanted)).require_complete()
    if len(inventory.units) != len(wanted):
        raise HostJobError("host job inventory is incomplete", unavailable=True)
    target = installed = None
    for unit in inventory.units:
        if (len(unit.installed) != 1 or dict(unit.properties).get("LoadState") != "loaded"
                or Path(unit.installed[0].path).parent != Path(placement["directory"])):
            raise HostJobError("host job is not installed at its committed placement", unavailable=True)
        if unit.declaration.source == service.source:
            target, installed = unit.target, unit.installed[0].path
    if target is None:
        raise HostJobError("host job service placement is unavailable", unavailable=True)
    return adapter, service, target, installed


def _run_git_job(current: GitCurrentRuntime, name: str, adapter: Adapter | None) -> HostJobRun:
    adapter, _service, target, installed = _bind_git_job(current, name, adapter)
    return _request_job(adapter, name, target, installed, revision=current.revision,
                        config_fingerprint=current.config_fingerprint)


def _request_job(adapter, name, target, installed, *, release_id=None, revision=None, config_fingerprint=None):
    try:
        result = adapter.call("svc_host_job_run_exact", installed, target, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostJobError("host job native request outcome is unknown", effect_attempted=True,
                           unavailable=True, release_id=release_id, target=target) from exc
    invoked = result.stdout.startswith("invoking\n")
    if result.returncode or result.stdout != "invoking\nrun-requested\n":
        raise HostJobError("host job native request was refused or is unverified",
                           effect_attempted=invoked or result.returncode == 0, unavailable=True,
                           release_id=release_id, target=target)
    return HostJobRun(name, release_id, target, revision=revision, config_fingerprint=config_fingerprint)


def run_host_job(root: Path, name: str, *, adapter: Adapter | None = None) -> HostJobRun:
    """Invoke an already installed oneshot; never install, unmask or retry it."""
    if name == "pull-root":
        raise HostJobError("pull-root is retired; use host update apply for Git changes")
    with mutation_admission(root, identity=RuntimeIdentity.current()) as current:
        return _run_git_job(current, name, adapter)
