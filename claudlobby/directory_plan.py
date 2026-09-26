"""Ordered create-only directory intentions shared by generation and preview.

The plans are lazy: a writer performs each mkdir before evaluating the next
intention, preserving existing partial-failure behavior. Preview reads metadata
only; directory contents and existing modes belong to their current owners.
"""
from __future__ import annotations

import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .config import FleetConfig
from .paths import Paths


@dataclass(frozen=True)
class DirectoryCreation:
    path: Path
    parents: bool = False


def bot_directory_plan(bot_dir: Path) -> Iterator[DirectoryCreation]:
    """The initial bot skeleton, after the bot's source audit has passed."""
    yield DirectoryCreation(bot_dir, parents=True)
    for relative in (".claude", "memory", "projects", "data", "data/events", "logs"):
        yield DirectoryCreation(bot_dir / relative)


def fleet_directory_plan(paths: Paths) -> Iterator[DirectoryCreation]:
    """Full-fleet scaffolding, before environment resolution or bot composition."""
    yield DirectoryCreation(paths.runtime_bots, parents=True)
    if paths.shared_docs:
        for relative in ("planning/active", "planning/completed", "decisions", "knowledge", "runbooks"):
            yield DirectoryCreation(paths.shared_docs / relative, parents=True)


def _directory_state(path: Path) -> str:
    """Inspect each ancestor before its descendant, never following a link.

    An ordinary lexical alias is intentionally not resolved: the writer follows
    directory symlinks, while this bounded preview discloses that topology as
    unsupported. Metadata observations do not prevent concurrent replacement.
    """
    absolute = path.absolute()
    if ".." in absolute.parts:
        return "preview unavailable — parent-traversal path is unsupported"
    for node in (*reversed(absolute.parents), absolute):
        try:
            mode = node.lstat().st_mode
        except FileNotFoundError:
            return "missing — creation requested"
        except OSError:
            return "preview unavailable — unreadable metadata"
        if stat.S_ISLNK(mode):
            return "preview unavailable — symlink topology is unsupported"
        if not stat.S_ISDIR(mode):
            kind = "target" if node == absolute else "ancestor"
            return f"blocked — {kind} is not a directory"
    return "exists — directory contents not inspected"


def diff_directory_creations(fleet: FleetConfig, paths: Paths,
                            *, bot_name: str | None = None) -> str:
    """Disclose the requested mode's skeleton, without entering mutable trees."""
    if bot_name is not None and bot_name not in fleet.bots:
        return ""
    lines = ["\n=== Directory creation intentions (metadata only) ===",
             "Ordered intentions; earlier validation or writes may prevent later steps.",
             "Existing modes and contents are preserved; no contents are inspected."]
    base = paths.fleet_dir or paths.root

    def append(plan: Iterator[DirectoryCreation]) -> None:
        for creation in plan:
            label = creation.path.relative_to(base).as_posix()
            lines.append(f"{label}: {_directory_state(creation.path)}")

    if bot_name is None:
        append(fleet_directory_plan(paths))
        bots = fleet.bots.values()
    else:
        bots = (fleet.bots[bot_name],)
    for bot in bots:
        append(bot_directory_plan(paths.bot_runtime(bot.bot_id)))
    lines.append("Scope: initial bot skeleton and full-fleet shared documentation scaffolding; "
                 "no complete-generation or concurrent-change guarantee.")
    return "\n".join(lines) + "\n"
