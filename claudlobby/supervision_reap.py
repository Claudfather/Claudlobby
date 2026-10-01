"""Selected-fleet entry to freshbox's existing orphan-unit predicate and reaper."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .freshbox import _orphan_unit_files, reap_orphan_units
from .operation_context import resolve_operation_scope
from .runtime_admission import mutation_admission


class SupervisionReapError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupervisionReapResult:
    fleet: str
    release_id: str
    dry_run: bool
    paths: tuple[Path, ...]


def reap_selected_orphans(*, root: Path, fleet: str | None, bot: str | None,
                          dry_run: bool) -> SupervisionReapResult:
    """Preview or remove only stale units beneath declared, owned bot directories."""
    with mutation_admission(root, expected_release=os.environ.get("CLAUDLOBBY_RELEASE_ID")) as release:
        destination, origin = resolve_operation_scope(root=root, fleet=fleet)
        # Fleet timer carriers resolve to origin=None after scope validation;
        # they are still generated callers, not an operator shell.
        if origin is not None or "FLEET_ROOT" in os.environ or "BOT_DIR" in os.environ:
            raise SupervisionReapError("orphan supervision reap requires an operator process")
        if bot is not None and bot not in destination.fleet.bots:
            raise SupervisionReapError(f"bot is not declared in the selected fleet: {bot}")
        paths = destination.paths
        if paths.runtime_bots.resolve() != paths.runtime_bots:
            raise SupervisionReapError("selected bot runtime is redirected")
        bots = ([destination.fleet.bots[bot]] if bot is not None
                else list(destination.fleet.bots.values()))
        for selected_bot in bots:
            directory = paths.bot_runtime(selected_bot.bot_id)
            if directory.parent != paths.runtime_bots or directory.resolve() != directory:
                raise SupervisionReapError("selected bot directory is redirected")
            try:
                info = directory.lstat()
            except FileNotFoundError:
                continue  # A declared bot need not have been generated yet.
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise SupervisionReapError("selected bot directory is not owned")
        if dry_run:
            found = [unit for selected_bot in bots
                     for unit in _orphan_unit_files(selected_bot, destination.fleet, paths)]
        else:
            found = reap_orphan_units(destination.fleet, paths, bots)
        return SupervisionReapResult(destination.fleet.name, release.release_id, dry_run,
                                     tuple(found))
