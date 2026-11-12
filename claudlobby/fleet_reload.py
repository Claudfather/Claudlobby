"""Refresh selected-fleet plugins through the packaged native reload owner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from .context import native_environment
from .operation_context import resolve_operation_scope
from .runtime_admission import mutation_admission, runtime_release_id, runtime_provenance, bound_runtime_release


class FleetReloadError(RuntimeError):
    def __init__(self, reason: str, *, code: str = "unavailable", effect_attempted: bool = False):
        self.code = code
        self.effect_attempted = effect_attempted
        super().__init__(reason)


@dataclass(frozen=True)
class FleetReloadResult:
    fleet: str
    release_id: str | None
    plugins_refreshed: tuple[str, ...]
    bots_marked: tuple[str, ...]
    source_revision: str | None = None
    config_fingerprint: str | None = None


def reload_fleet(*, root: Path, fleet: str | None) -> FleetReloadResult:
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        if origin is not None and (origin.fleet.name != destination.fleet.name
                                   or origin.bot_id != destination.fleet.manager):
            raise FleetReloadError("only the selected fleet manager may reload its fleet",
                                   code="conflict")
        bound_runtime_release(destination.paths.package, generated=origin is not None)
        plugins = tuple(destination.fleet.plugins.required)
        bots = tuple(sorted(destination.fleet.bots))
        native = destination.paths.lib / "reload-fleet.sh"
        if native != destination.paths.package.runtime_native / "reload-fleet.sh":
            raise FleetReloadError("reload owner differs from the selected release",
                                   code="release_mismatch")
        env = {**os.environ, **native_environment(destination.paths, current=release),
               "CLAUDLOBBY_FLEET": destination.fleet.name}
        command = [str(native), "--admitted-runtime",
                   "--fleet", destination.fleet.name, "--bots-dir",
                   str(destination.paths.runtime_bots)]
        for plugin in plugins:
            command.extend(("--plugin", plugin))
        for bot in bots:
            command.extend(("--bot", bot))
        completed = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
        if completed.returncode:
            raise FleetReloadError("native fleet reload failed; inspect state/reload-fleet.log",
                                   effect_attempted=True)
        refreshed, marked = [], []
        for line in completed.stdout.splitlines():
            kind, separator, value = line.partition("\t")
            if not separator or kind not in {"refreshed", "marked"}:
                raise FleetReloadError("native fleet reload returned an unfamiliar result",
                                       effect_attempted=True)
            (refreshed if kind == "refreshed" else marked).append(value)
        if (tuple(refreshed) != plugins or len(set(marked)) != len(marked)
                or any(bot not in bots for bot in marked)):
            raise FleetReloadError("native fleet reload outcome differs from selected scope",
                                   effect_attempted=True)
        return FleetReloadResult(destination.fleet.name, runtime_release_id(release),
                                 tuple(refreshed), tuple(marked), **runtime_provenance(release))
