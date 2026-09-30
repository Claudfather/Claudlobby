"""Run the selected fleet's existing native credential probe once."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .activation_state import read_selection
from .config_plan import path_state, read_plan
from .context import native_environment
from .operation_context import resolve_operation_scope
from .runtime_admission import mutation_admission


class CredentialCheckError(RuntimeError):
    def __init__(self, reason: str, *, effect_attempted: bool = False):
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class CredentialCheckResult:
    fleet: str
    release_id: str
    state_path: Path
    checks: str = "tick_completed"
    health: str = "unobserved"


def check_credentials(*, root: Path, fleet: str | None) -> CredentialCheckResult:
    """Run the probe owner; a completed tick is not a claim that tokens work."""
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        if origin is not None and origin.bot_id != destination.fleet.manager:
            raise CredentialCheckError("only the selected fleet manager may check its credentials")
        native = destination.paths.lib / "creds-check.sh"
        if native != release.native_path / "creds-check.sh":
            raise CredentialCheckError("credential probe differs from the selected release")

        # The native alert resolver also reads fleet.yaml. Refuse an authored
        # edit after activation rather than let it choose another live roster.
        selected = read_selection(root)
        if selected is None or selected["release_id"] != release.release_id:
            raise CredentialCheckError("credential check has no stable selected fleet")
        plan = read_plan(root, selected["plan_id"])
        source = plan.effects["fleet_sources"][destination.fleet.name]["fleet"]
        manifest, _ = plan.frozen_input(source, required=True)
        if manifest != destination.paths.fleet_yaml or path_state(manifest, source=True) != plan.inputs[str(manifest)]["state"]:
            raise CredentialCheckError("fleet source changed since activation")
        if destination.paths.runtime_bots.resolve() != destination.paths.runtime_bots:
            raise CredentialCheckError("fleet bot runtime is redirected")

        state_path = root / "state/creds-check" / f"{destination.fleet.name}.json"
        env = {**os.environ, **native_environment(destination.paths),
               "CLAUDLOBBY_FLEET": destination.fleet.name,
               "CLAUDLOBBY_CREDS_LOG": str(root / "state/logs/creds-check.log"),
               "CLAUDLOBBY_CREDS_STATE": str(state_path)}
        if destination.fleet.telegram_group_chat_id:
            env["TELEGRAM_GROUP_CHAT_ID"] = str(destination.fleet.telegram_group_chat_id)
        else:
            env.pop("TELEGRAM_GROUP_CHAT_ID", None)
        command = [str(native), "--selected-release", release.release_id,
                   "--fleet", destination.fleet.name,
                   "--fleet-root", str(destination.paths.fleet_config_dir),
                   "--bots-dir", str(destination.paths.runtime_bots)]
        for bot in sorted(destination.fleet.bots):
            command.extend(("--bot", bot))
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    check=False, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialCheckError("credential probe outcome is unknown; inspect its state and log",
                                       effect_attempted=True) from exc
        if result.returncode or result.stdout != "tick-complete\n":
            # Probe failures, alert attempts and state writes may already have
            # happened. Never auto-retry a possibly delivered alert.
            raise CredentialCheckError("credential probe outcome is unverified; inspect its state and log",
                                       effect_attempted=True)
        return CredentialCheckResult(destination.fleet.name, release.release_id, state_path)
