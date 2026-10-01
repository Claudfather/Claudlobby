"""Run the selected fleet's private pulse sweep through runtime admission."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

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
    stderr_tail: str


def _sweep(command: list[str], env: dict[str, str]) -> tuple[str, str, int]:
    """Bound the private sweep and preserve its journal warnings."""
    def forward(warnings):
        stream = getattr(sys.stderr, "buffer", None)
        if stream is not None:
            shutil.copyfileobj(warnings, stream)
        else:
            for chunk in iter(lambda: warnings.read(8192), b""):
                sys.stderr.write(chunk.decode("utf-8", "replace"))

    with tempfile.TemporaryFile() as warnings:
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                 stderr=warnings, start_new_session=True)
        try:
            stdout, _ = child.communicate(timeout=120)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            warnings.seek(0)
            forward(warnings)
            raise FleetPulseError("private fleet pulse timed out after 120 seconds; inspect its events and state/pulse summary",
                                  code="timeout", effect_attempted=True) from exc
        warnings.seek(0, os.SEEK_END)
        size = warnings.tell()
        warnings.seek(max(size - 4096, 0))
        tail = warnings.read().decode("utf-8", "replace")
        warnings.seek(0)
        forward(warnings)
    return stdout.decode("utf-8", "replace"), tail, child.returncode


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
        try:
            summary, stderr_tail, returncode = _sweep([str(native), destination.fleet.name], env)
        except OSError as exc:
            raise FleetPulseError("private fleet pulse could not start", effect_attempted=False) from exc
        if returncode:
            raise FleetPulseError("private fleet pulse failed; inspect its events and state/pulse summary",
                                  effect_attempted=True)
        return FleetPulseResult(destination.fleet.name, release.release_id,
                                destination.paths.root / "state/pulse" /
                                f"{destination.fleet.name}.pulse-summary.txt",
                                summary, stderr_tail)
