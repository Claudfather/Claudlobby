"""Run the selected fleet's private pulse sweep through runtime admission."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .context import native_environment
from .operation_context import resolve_operation_scope
from .runtime_admission import mutation_admission


class FleetPulseError(RuntimeError):
    def __init__(self, reason: str, *, code: str = "unavailable", effect_attempted: bool = False):
        self.code = code
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class FleetPulseResult:
    fleet: str
    release_id: str
    summary_path: Path
    summary: str


def pulse_fleet(*, root: Path, fleet: str | None) -> FleetPulseResult:
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        if origin is not None and (origin.fleet.name != destination.fleet.name
                                   or origin.bot_id != destination.fleet.manager):
            raise FleetPulseError("only the selected fleet manager may pulse its fleet", code="conflict")
        native = destination.paths.lib / "fleet-pulse.sh"
        if native != release.native_path / "fleet-pulse.sh":
            raise FleetPulseError("pulse owner differs from the selected release",
                                  code="release_mismatch")
        env = {**os.environ, **native_environment(destination.paths),
               "CLAUDLOBBY_FLEET": destination.fleet.name,
               "CLAUDLOBBY_NATIVE_PYTHON": str(release.cli_path.parent / "python"),
               "CLAUDLOBBY_PRIVATE_PULSE_RELEASE": release.release_id}
        completed = subprocess.run([str(native), destination.fleet.name], env=env,
                                   capture_output=True, text=True, check=False)
        if completed.returncode:
            raise FleetPulseError("private fleet pulse failed; inspect its events and state/pulse summary",
                                  effect_attempted=True)
        return FleetPulseResult(destination.fleet.name, release.release_id,
                                destination.paths.root / "state/pulse" /
                                f"{destination.fleet.name}.pulse-summary.txt",
                                completed.stdout)
