"""Run the selected fleet's private pulse sweep through runtime admission."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from .config import FLEET_PULSE_TIMEOUT_RANGE
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


# A tick's cap. Scaled by load per CPU up to MAX_CAP_S, which keeps a sweep and
# its grace under the default 300 s pulse cadence: every oneshot holds the
# activation lock shared while it runs, and `host activate` takes it exclusive
# without waiting, so a cap past the cadence could refuse every activation.
DEFAULT_CAP_S = 120
MAX_CAP_S = 240
TERM_GRACE_S = 10


def sweep_cap(configured: int | None, load1: float, cpus: int) -> tuple[int, str]:
    """The sweep's time cap and how it was chosen: fleet.yaml's fleet_pulse.timeout_s,
    clamped to its validated range, else DEFAULT_CAP_S scaled by load per CPU up to
    MAX_CAP_S."""
    if configured is not None:
        low, high = FLEET_PULSE_TIMEOUT_RANGE
        return min(max(configured, low), high), "fleet_pulse.timeout_s"
    cap = int(min(MAX_CAP_S, max(DEFAULT_CAP_S, DEFAULT_CAP_S * load1 / max(cpus, 1))))
    return cap, f"scaled from load1 {load1:.1f} on {cpus} cpus"


def _stop(child: subprocess.Popen) -> None:
    """SIGTERM the sweep's own process group so its traps can clean up, then
    SIGKILL whatever is left after a grace period."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=TERM_GRACE_S)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.communicate()


def _record_timeout(path: Path, cap: int, why: str, tail: str) -> None:
    """Leave a summary that says the sweep stopped, and keep the last complete one
    beside it as <fleet>.pulse-summary.last-complete.txt."""
    from .composer import _write_atomic  # keeps the summary's 0600 mode; loaded only here

    kept = path.with_suffix(".last-complete.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.read_text(encoding="utf-8", errors="replace").startswith("TIMED OUT"):
        os.replace(path, kept)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_atomic(path, (
        f"TIMED OUT: the fleet pulse sweep was stopped after {cap} s ({why}) at {stamp}.\n"
        "Events it recorded before the stop stand. The rest of this tick did not run: "
        "the bots after the stop, the fleet escalation and escalated-task paging.\n"
        + (f"The last complete summary: {kept}\n" if kept.is_file() else "")
        + "Last lines the sweep wrote to stderr:\n" + tail))


def _sweep(command: list[str], env: dict[str, str], *, timeout_s: int | None = None,
           summary_path: Path) -> tuple[str, str, int]:
    """Bound the private sweep and preserve its journal warnings."""
    def drain(warnings) -> str:
        """Forward the warnings to stderr and return their last 4 KiB."""
        warnings.seek(0, os.SEEK_END)
        warnings.seek(max(warnings.tell() - 4096, 0))
        tail = warnings.read().decode("utf-8", "replace")
        warnings.seek(0)
        stream = getattr(sys.stderr, "buffer", None)
        if stream is not None:
            shutil.copyfileobj(warnings, stream)
        else:
            for chunk in iter(lambda: warnings.read(8192), b""):
                sys.stderr.write(chunk.decode("utf-8", "replace"))
        return tail

    cap, why = sweep_cap(timeout_s, os.getloadavg()[0], os.cpu_count() or 1)
    with tempfile.TemporaryFile() as warnings:
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                 stderr=warnings, start_new_session=True)
        try:
            stdout, _ = child.communicate(timeout=cap)
        except subprocess.TimeoutExpired as exc:
            _stop(child)
            tail = drain(warnings)
            try:
                _record_timeout(summary_path, cap, why, tail)
                where = f"its summary records the stop: {summary_path}"
            except OSError as err:
                where = f"its summary was not written ({err}); inspect its events"
            raise FleetPulseError(f"private fleet pulse timed out after {cap} seconds ({why}); {where}",
                                  code="timeout", effect_attempted=True) from exc
        tail = drain(warnings)
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
        summary_path = (destination.paths.root / "state/pulse" /
                        f"{destination.fleet.name}.pulse-summary.txt")
        pulse = destination.fleet.fleet_pulse
        try:
            summary, stderr_tail, returncode = _sweep([str(native), destination.fleet.name], env,
                                                      timeout_s=pulse.timeout_s if pulse else None,
                                                      summary_path=summary_path)
        except OSError as exc:
            raise FleetPulseError("private fleet pulse could not start", effect_attempted=False) from exc
        if returncode:
            raise FleetPulseError("private fleet pulse failed; inspect its events and state/pulse summary",
                                  effect_attempted=True)
        return FleetPulseResult(destination.fleet.name, release.release_id, summary_path,
                                summary, stderr_tail)
