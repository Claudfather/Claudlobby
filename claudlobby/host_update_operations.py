"""Selected host update requests through the existing packaged native doors."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .config import host_unit_name, load_host_jobs
from .activation_enrollment import selected_phase_entries
from .context import resolve_paths, native_environment
from .env_tiers import resolve as resolve_env_tiers
from .runtime_admission import (GitCurrentRuntime, mutation_admission, bound_runtime_release,
                                runtime_release_id, runtime_provenance)
from .host_job_operations import _bind_git_job, HostJobError
from .supervision_inventory import InventoryError


class HostUpdateError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False):
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class HostUpdateResult:
    action: str
    release_id: str | None
    dry_run: bool
    outcome: str = "tick_completed"
    source_revision: str | None = None
    config_fingerprint: str | None = None


def run_host_update(root: Path, action: str, *, dry_run: bool = False,
                    scheduled: bool = False) -> HostUpdateResult:
    """Run one host updater; a completed tick does not claim every target moved."""
    if action not in ("runtime", "siblings") or (action == "runtime" and dry_run):
        raise HostUpdateError("unsupported host update request")
    name = "claude-update" if action == "runtime" else "update-siblings"
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        paths = resolve_paths(root=root)
        bound_runtime_release(paths.package, generated=False)
        selected_service = None
        if scheduled and isinstance(release, GitCurrentRuntime):
            try:
                _adapter, service, _target, _installed = _bind_git_job(release, name)
            except (HostJobError, InventoryError, ValueError, OSError) as exc:
                raise HostUpdateError("scheduled host update has no verified committed job") from exc
            selected_service = {"environment": dict(service.environment)}
        elif scheduled:
            config = load_host_jobs().get(name)
            if config is None or config.get("enroll", True) is not True:
                raise HostUpdateError("scheduled host update is not enrolled")
            entries = selected_phase_entries(root, "producers")
            sources = {Path(entry["source"]).name for entry in entries}
            stem = host_unit_name(name)
            if not (stem + ".plist" in sources or
                    {stem + ".service", stem + ".timer"} <= sources):
                raise HostUpdateError("scheduled host update has no selected timer")
            selected_service = next((entry for entry in entries if
                                     Path(entry["source"]).name in
                                     (stem + ".plist", stem + ".service")), None)
            if selected_service is None:
                raise HostUpdateError("scheduled host update has no selected service")
        env = {**os.environ, **native_environment(paths, current=release)}
        script = paths.package.runtime_native / ("update-claude-code.sh" if action == "runtime" else "update-siblings.sh")
        for key in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "FLEET_NAME",
                    "CLAUDLOBBY_FLEET", "FLEET_ROOT", "CLAUDLOBBY_UPDATE_SCHEDULED"):
            env.pop(key, None)
        if action == "runtime":
            # The unit stamps this flag from the host/root cascade. An explicit
            # operator request must read the SAME switch, including an empty
            # assignment that disarms it, not inherit a caller's ambient value.
            flag = "CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED"
            if scheduled:
                value = selected_service["environment"].get(flag)
                if os.environ.get(flag) != value:
                    raise HostUpdateError("scheduled runtime update switch differs from selected unit")
            else:
                enabled = resolve_env_tiers(paths).get(flag)
                value = enabled.value if enabled is not None else None
            env.pop("CLAUDLOBBY_STAGED_CLAUDE_UPDATE_ENABLED", None)
            if value is not None:
                env[flag] = value
        command = [str(script), "--admitted-runtime"]
        if dry_run:
            command.append("--dry-run")
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    check=False, timeout=3600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HostUpdateError("host update outcome is unknown; inspect its log before retrying",
                                  effect_attempted=True) from exc
        if result.returncode:
            raise HostUpdateError("host update did not complete; inspect its log before retrying",
                                  effect_attempted=True)
        return HostUpdateResult(action, runtime_release_id(release), dry_run, **runtime_provenance(release))
