"""Read-only link operations, consumed in order by writers and their preview.

Generators deliberately interleave discovery and application in the writers:
cleanup/selection/obstacle semantics remain those of the original link doors.
No operation here opens linked target contents or mutates the filesystem.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator


@dataclass(frozen=True)
class LinkOperation:
    kind: str
    path: Path
    target: Path | None = None
    message: str = ""
    source: Path | None = None


def skill_link_plan(paths, bot_id: str, skills: list[str]) -> Iterator[LinkOperation]:
    directory = paths.bot_runtime(bot_id) / '.claude/skills'
    if directory.exists():
        for entry in directory.iterdir():
            if entry.is_symlink():
                yield LinkOperation('unlink', entry)
            elif entry.is_dir():
                yield LinkOperation('rmtree', entry)
    yield LinkOperation('mkdir', directory)
    linked: dict[str, Path] = {}

    def add(leaf, src):
        if leaf in linked:
            return LinkOperation('notice', directory / leaf, message=(
                f"  skill '{leaf}' already linked from {linked[leaf]} — skipping {src}"))
        linked[leaf] = src
        return LinkOperation('create', directory / leaf, src.resolve(), source=src)

    for skill in skills:
        if skill.endswith('/'):
            collected = paths.expand_skill_folder(skill.rstrip('/'))
            if not collected:
                yield LinkOperation('notice', directory, message=(
                    f"  skill folder '{skill}' empty or missing — skipped"))
            for leaf, src in collected.items():
                yield add(leaf, src)
        else:
            src = paths.find_library_dir('skills', skill)
            if src is None:
                yield LinkOperation('notice', directory, message=f"  skill '{skill}' missing — skipped")
            else:
                yield add(src.name, src)


def mount_link_plan(mounts: dict[str, str], bot_dir: Path) -> Iterator[LinkOperation]:
    directory = bot_dir / 'mounts'
    yield LinkOperation('mkdir', directory)
    if directory.exists():
        for entry in directory.iterdir():
            if entry.is_symlink() and entry.name not in mounts:
                yield LinkOperation('unlink', entry)
    for name, target in mounts.items():
        target_path = Path(target).expanduser()
        link = directory / name
        try:
            resolved = target_path.resolve()
            if not resolved.is_relative_to(Path.home()) and not resolved.is_relative_to(bot_dir):
                yield LinkOperation('skip', link, message=(
                    f"  mount '{name}': target {target_path} escapes home and bot dir — skipping"))
                continue
        except (ValueError, OSError):
            yield LinkOperation('skip', link, message=(
                f"  mount '{name}': could not resolve target {target_path} — skipping"))
            continue
        if link.is_symlink():
            if link.resolve() == target_path.resolve():
                yield LinkOperation('unchanged', link, target_path)
                continue
            yield LinkOperation('unlink', link)
        elif link.exists():
            yield LinkOperation('blocked', link, target_path, (
                f"  mount '{name}': non-symlink already exists at {link} — skipping"))
            continue
        if not target_path.exists():
            yield LinkOperation('notice', link, message=(
                f"  mount '{name}' target does not exist: {target_path} — creating dangling symlink"))
        yield LinkOperation('create', link, target_path)
