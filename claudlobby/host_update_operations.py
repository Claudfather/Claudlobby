"""Selected host update requests through the existing packaged native doors."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .config import load_host_jobs
from .activation_enrollment import selected_phase_entries
from .context import resolve_paths
from .env_tiers import resolve as resolve_env_tiers
from .runtime_admission import mutation_admission


class HostUpdateError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False):
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class HostUpdateResult:
    action: str
    release_id: str
    dry_run: bool
    outcome: str = "tick_completed"


def run_host_update(root: Path, action: str, *, dry_run: bool = False,
                    scheduled: bool = False) -> HostUpdateResult:
    """Run one host updater; a completed tick does not claim every target moved."""
    if action not in ("runtime", "siblings") or (action == "runtime" and dry_run):
        raise HostUpdateError("unsupported host update request")
    name = "claude-update" if action == "runtime" else "update-siblings"
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        paths = resolve_paths(root=root)
        if paths.lib != release.native_path:
            raise HostUpdateError("host updater differs from selected release")
        selected_service = None
        if scheduled:
            config = load_host_jobs().get(name)
            if config is None or config.get("enroll", True) is not True:
                raise HostUpdateError("scheduled host update is not enrolled")
            entries = selected_phase_entries(root, "producers")
            sources = {Path(entry["source"]).name for entry in entries}
            stem = f"claudlobby-{name}"
            if not (stem + ".plist" in sources or
                    {stem + ".service", stem + ".timer"} <= sources):
                raise HostUpdateError("scheduled host update has no selected timer")
            selected_service = next((entry for entry in entries if
                                     Path(entry["source"]).name in
                                     (stem + ".plist", stem + ".service")), None)
            if selected_service is None:
                raise HostUpdateError("scheduled host update has no selected service")
        script = release.native_path / ("update-claude-code.sh" if action == "runtime" else "update-siblings.sh")
        env = {**os.environ, "CLAUDLOBBY_ROOT": str(root),
               "CLAUDLOBBY_NATIVE_DIR": str(release.native_path),
               "CLAUDLOBBY_RELEASE_ID": release.release_id,
               "CLAUDLOBBY_CLI": str(release.cli_path)}
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
        command = [str(script), "--selected-release", release.release_id]
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
        return HostUpdateResult(action, release.release_id, dry_run)
