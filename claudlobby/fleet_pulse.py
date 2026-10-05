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
import time

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


DEFAULT_CAP_S = 120
MAX_CAP_S = 900
TERM_GRACE_S = 10


def sweep_cap(configured: int | None) -> tuple[int, str]:
    """The sweep's time cap and how it was chosen (#2059): fleet.yaml's
    fleet_pulse.timeout_s when set, else 120 s scaled by load per CPU, 120-900 s,
    read as the sweep starts. A fixed 120 s read host load as a failed unit."""
    if configured:
        return int(configured), "fleet_pulse.timeout_s"
    load1, cpus = os.getloadavg()[0], os.cpu_count() or 1
    cap = int(min(MAX_CAP_S, max(DEFAULT_CAP_S, DEFAULT_CAP_S * load1 / cpus)))
    return cap, f"scaled from load1 {load1:.1f} on {cpus} cpus"


def _stop(child: subprocess.Popen) -> None:
    """SIGTERM the sweep's own process group so its traps can clean up, then
    SIGKILL whatever is left after a grace period."""
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, None)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.communicate(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _record_timeout(path: Path, cap: int, why: str, tail: str) -> None:
    """Replace the previous tick's summary with one that says the sweep stopped."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    text = (f"TIMED OUT: the fleet pulse sweep was stopped after {cap} s ({why}) at {stamp}.\n"
            "Events it recorded before the stop stand; the checks after the stop did not run this tick.\n"
            "Last lines the sweep wrote to stderr:\n" + tail)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".timeout.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _sweep(command: list[str], env: dict[str, str], *, cap: int = DEFAULT_CAP_S,
           why: str = "default", summary_path: Path | None = None) -> tuple[str, str, int]:
    """Bound the private sweep and preserve its journal warnings."""
    def forward(warnings):
        stream = getattr(sys.stderr, "buffer", None)
        if stream is not None:
            shutil.copyfileobj(warnings, stream)
        else:
            for chunk in iter(lambda: warnings.read(8192), b""):
                sys.stderr.write(chunk.decode("utf-8", "replace"))

    def tail_of(warnings) -> str:
        warnings.seek(0, os.SEEK_END)
        warnings.seek(max(warnings.tell() - 4096, 0))
        tail = warnings.read().decode("utf-8", "replace")
        warnings.seek(0)
        return tail

    with tempfile.TemporaryFile() as warnings:
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                 stderr=warnings, start_new_session=True)
        try:
            stdout, _ = child.communicate(timeout=cap)
        except subprocess.TimeoutExpired as exc:
            _stop(child)
            tail = tail_of(warnings)
            forward(warnings)
            where = "inspect its events and state/pulse summary"
            if summary_path is not None:
                _record_timeout(summary_path, cap, why, tail)
                where = f"its summary records the stop: {summary_path}"
            raise FleetPulseError(f"private fleet pulse timed out after {cap} seconds ({why}); {where}",
                                  code="timeout", effect_attempted=True) from exc
        tail = tail_of(warnings)
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
        summary_path = (destination.paths.root / "state/pulse" /
                        f"{destination.fleet.name}.pulse-summary.txt")
        cap, why = sweep_cap(getattr(getattr(destination.fleet, "fleet_pulse", None), "timeout_s", None))
        try:
            summary, stderr_tail, returncode = _sweep([str(native), destination.fleet.name], env,
                                                      cap=cap, why=why, summary_path=summary_path)
        except OSError as exc:
            raise FleetPulseError("private fleet pulse could not start", effect_attempted=False) from exc
        if returncode:
            raise FleetPulseError("private fleet pulse failed; inspect its events and state/pulse summary",
                                  effect_attempted=True)
        return FleetPulseResult(destination.fleet.name, release.release_id, summary_path,
                                summary, stderr_tail)
