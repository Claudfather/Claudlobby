"""Topology-only preview of the link operations generate would perform."""
from __future__ import annotations

import os
from collections import deque
from pathlib import Path
import stat

from .link_plan import mount_link_plan, skill_link_plan


def _node(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


class _Unavailable(ValueError):
    """A topology the preview cannot predict without crossing its boundary."""


def _guard_directory(path, trusted_root, *, required=False):
    """Inspect owned segments without following links below the chosen root.

    The operator-selected root may itself be a symlink (including /tmp on
    macOS). Canonicalize that root, then lstat each owned segment in order.
    This is a metadata snapshot, not protection against concurrent replacement.
    """
    root = Path(os.path.abspath(trusted_root))
    relative = path.absolute().relative_to(root)
    if '..' in relative.parts:
        raise _Unavailable('path leaves the namespace')
    current = root.resolve()
    for part in relative.parts:
        current = current / part
        node = _node(current)
        if node is None:
            if required:
                raise _Unavailable('selected path parent is absent')
            return
        if stat.S_ISLNK(node.st_mode):
            raise _Unavailable('namespace is a symlink (at or below the trusted root)')
        if not stat.S_ISDIR(node.st_mode):
            raise _Unavailable('namespace ancestor is not a directory')


def _references_namespace(source, namespace):
    """Follow metadata, detecting even an intermediate namespace reference.

    Comparing only final resolve() results misses source -> runtime-link ->
    external-directory. Both cleanup and creation can change source eligibility
    or target resolution. No contents are opened; missing paths are checked too.
    """
    pending = deque(source.absolute().parts[1:])
    current = Path(source.absolute().anchor)
    hops = 0
    while pending:
        part = pending.popleft()
        if part == '..':
            current = current.parent
            continue
        candidate = current / part
        if candidate.is_relative_to(namespace):
            return True
        node = _node(candidate)
        if node is not None and stat.S_ISLNK(node.st_mode):
            hops += 1
            if hops > 40:
                raise _Unavailable('source/target symlink chain is unresolved')
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current = Path(target.anchor)
                pending.extendleft(reversed(target.parts[1:]))
            else:
                pending.extendleft(reversed(target.parts))
        else:
            current = candidate
    return False


def _skill_source_candidates(paths, skills):
    """Inspect potential lookup paths, not a second skill selection policy.

    A missing candidate may appear after an earlier link is created. Folder
    expansion likewise needs missing/dangling entries checked before claiming
    a complete selection. This preflight only refuses namespace dependencies;
    the writer's plan remains authoritative for the actual selected set.
    """
    # library_search_dirs intentionally filters absent overlays for selection.
    # Preflight must also inspect that missing root: an earlier generated skill
    # can make an overlay alias eligible before the next selection occurs.
    roots = [paths.base_library]
    if paths.overlay_library is not None:
        roots.insert(0, paths.overlay_library)
    for skill in skills:
        if '..' in skill:
            raise ValueError('path traversal in skill selection')
        for root in roots:
            candidate = root / 'skills' / skill.rstrip('/')
            yield candidate
            if skill.endswith('/'):
                yield from candidate.rglob('*')


def _preview(kind, directory, operations, *, trusted_root, selected=(), sources=(), targets=()):
    try:
        _guard_directory(directory, trusted_root)
        for entry in selected:
            relative = entry.relative_to(directory)
            if '..' in relative.parts or not relative.parts:
                raise _Unavailable('selected path leaves the namespace')
            if entry.parent != directory:
                _guard_directory(entry.parent, trusted_root, required=True)
        namespace = directory.resolve()
        for source in sources:
            if _references_namespace(source, namespace):
                raise _Unavailable('skill source selection may depend on generate cleanup/creation')
        for target in targets:
            target = Path(target).expanduser()
            if not target.is_absolute():
                raise _Unavailable('relative mount declarations use distinct cwd/link-parent frames')
            if _references_namespace(target, namespace):
                raise _Unavailable('mount target resolution may depend on generate cleanup/creation')
        plan = list(operations)
        actual = {p.name: p for p in directory.iterdir()} if directory.exists() else {}
        def name(path):
            return path.relative_to(directory).as_posix()
        expected = {name(op.path): op for op in plan if op.kind in ('create', 'unchanged', 'blocked')}
        changes, notes = [], []
        matching = 0
        for label, operation in expected.items():
            entry = operation.path
            node = _node(entry)
            if node is None:
                changes.append(f'create {kind} link {label} -> {operation.target}')
            elif stat.S_ISLNK(node.st_mode):
                target = Path(os.readlink(entry))
                target = Path(os.path.abspath(target if target.is_absolute() else entry.parent / target))
                wanted = Path(os.path.abspath(operation.target))
                if operation.kind == 'unchanged' or target == wanted:
                    matching += 1
                    if kind == 'mount' and not entry.exists():
                        notes.append(f'matching mount link {label} is dangling (target absent)')
                else:
                    changes.append(f'retarget {kind} link {label} -> {operation.target}')
            elif kind == 'skill' and stat.S_ISDIR(node.st_mode):
                changes.append(f'replace skill directory {label} with link -> {operation.target}')
            else:
                changes.append(f'{kind} obstacle {label}: non-link '
                               f'{"directory" if stat.S_ISDIR(node.st_mode) else "entry"} preserved; '
                               + ('generate is blocked' if kind == 'skill' else 'mount skipped'))
        for operation in plan:
            if name(operation.path) not in expected and operation.kind in ('unlink', 'rmtree'):
                shape = 'link' if operation.kind == 'unlink' else 'directory'
                changes.append(f'remove {kind} {shape} {name(operation.path)}')
            elif operation.kind == 'skip':
                changes.append(operation.message.strip())
            elif operation.kind == 'notice':
                notes.append(operation.message.strip())
        removed = {name(op.path) for op in plan if op.kind in ('unlink', 'rmtree')}
        for label in sorted(actual.keys() - expected.keys() - removed):
            notes.append(f'preserved extra {kind} entry {label}')
        plural = 'skills' if kind == 'skill' else 'mounts'
        notes.insert(0, f'{plural}: expected {len(expected)}, matching {matching}')
        if kind == 'skill':
            notes.append(f'{len(expected)} skill links recreated on generate (if unblocked); next use, no restart gate')
            if any(op.kind == 'notice' for op in plan):
                notes.append('skipped local selection: no local link expected for missing sources; '
                             'plugin availability not assessed')
        else:
            notes.append('mount link changes are visible at next use, no restart gate')
        return [f'{kind} topology: {change}' for change in changes], notes
    except _Unavailable as exc:
        return [f'{kind} topology unavailable: {exc}'], []
    except (OSError, ValueError, RuntimeError) as exc:
        return [f'{kind} topology unavailable ({type(exc).__name__})'], []


def link_preview(bot, paths, *, skills):
    """Return diagnostics and coverage notes without writers or content reads."""
    bot_dir = paths.bot_runtime(bot.bot_id)
    trusted_root = paths.fleet_dir or paths.root
    changes, notes = _preview('skill', bot_dir / '.claude/skills',
                              skill_link_plan(paths, bot.bot_id, skills),
                              trusted_root=trusted_root,
                              sources=_skill_source_candidates(paths, skills))
    mount_changes, mount_notes = _preview('mount', bot_dir / 'mounts',
                                         mount_link_plan(bot.mounts, bot_dir),
                                         trusted_root=trusted_root,
                                         selected=(bot_dir / 'mounts' / name for name in bot.mounts),
                                         targets=bot.mounts.values())
    return changes + mount_changes, notes + mount_notes + [
        'target content not compared: unchanged links do not establish unchanged source bytes; '
        'current target content is read on demand']
