"""Read-only observations of declared composition source trees (#953).

This is an inventory, not a consumed-file trace or an atomic attestation of a
concurrently changing checkout. Only digests and bounded diagnostics persist.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

_EXCLUDED = {'__pycache__', '.DS_Store', 'build', '.pytest_cache', '.git', 'runtime'}


def _excluded(parts) -> bool:
    return any(p in _EXCLUDED or p.endswith('.pyc') or p == '.env' or p.startswith('.env.')
               for p in parts)


def source_inventory(paths, package_dir: Path, system_yaml: Path | None) -> list[tuple[str, Path]]:
    """Actual lookup roots; overlay precedes base, exact shared roots deduplicate.

    Python comes from the imported package, which need not equal Paths.root.
    Templates also support an overlay, so include it alongside library/voices.
    """
    def present(path):
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True  # preserve unreadable as a declared, incomplete root
    roots = [('package', package_dir)]
    for name, overlay, base in (
        ('templates', paths.fleet_dir / 'templates' if paths.fleet_dir else None, paths.root / 'templates'),
        ('library', paths.overlay_library, paths.base_library),
        ('voices', paths.overlay_voices, paths.base_voices),
    ):
        if overlay is not None and present(overlay) and overlay != base:
            roots.append((f'overlay/{name}', overlay))
        roots.append((f'base/{name}', base))
    roots.append(('base/lib', paths.root / 'lib'))
    roots.append(('system.yaml', system_yaml if system_yaml is not None else package_dir / 'system.yaml'))
    return roots


def _normal(path: Path) -> Path:
    # Resolve parents, not the final component: a declared symlink does not
    # implicitly authorize its target as another source root.
    return path.parent.resolve() / path.name


def _safe_open(path: Path, *, directory=False) -> int:
    """Walk descriptors with NOFOLLOW, including parents; a link swap fails closed."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(path.parts) - 2 or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _tree(root: Path, allowed: list[Path], cache: dict) -> dict:
    records, issues = [], []
    complete = True
    def issue(rel, reason):
        nonlocal complete
        complete = False
        records.append((rel, reason))
        if len(issues) < 16:
            issues.append(f'{rel}: {reason}')
    def permitted(path):
        for boundary in allowed:
            try:
                relative = path.relative_to(boundary)
            except ValueError:
                continue
            if not _excluded(relative.parts) and not _excluded((path.name,)):
                return True
        return False
    def walk(path, rel, ancestors):
        if _excluded(Path(rel).parts):
            return
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(path)
                records.append((rel, 'symlink', link))
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    issue(rel, 'dangling-or-cyclic-symlink')
                    return
                if not permitted(target):
                    issue(rel, 'outside-or-excluded-symlink')
                    return
                walk(target, rel, ancestors)
                return
            if not permitted(path):
                issue(rel, 'outside-or-excluded-source')
                return
            if stat.S_ISDIR(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if identity in ancestors:
                    issue(rel, 'cyclic-directory-link')
                    return
                fd = _safe_open(path, directory=True)
                try:
                    with os.scandir(fd) as entries:
                        names = sorted(e.name for e in entries if not _excluded((e.name,)))
                finally:
                    os.close(fd)
                for name in names:
                    walk(path / name, f'{rel}/{name}' if rel != '.' else name, ancestors | {identity})
            elif stat.S_ISREG(info.st_mode):
                key = str(path)
                if key not in cache:
                    fd = _safe_open(path)
                    with os.fdopen(fd, 'rb') as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise OSError('source changed type')
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: stream.read(65536), b''):
                            digest.update(chunk)
                    cache[key] = digest.hexdigest()
                records.append((rel, 'file', cache[key]))
            else:
                issue(rel, 'unsupported-file-type')
        except FileNotFoundError:
            issue(rel, 'missing')
        except (OSError, RuntimeError):
            issue(rel, 'unreadable')
    walk(root, '.', set())
    raw = json.dumps(sorted(records), ensure_ascii=True, separators=(',', ':')).encode()
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'complete': complete,
            'entries': len(records), 'issues': issues}


def _git(args, cwd):
    try:
        result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-C', str(cwd), *args], capture_output=True,
                                text=True, timeout=5,
                                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C'})
        if result.returncode == 0:
            return result.stdout.rstrip('\n'), None
        if args == ['symbolic-ref', '--quiet', '--short', 'HEAD'] and result.returncode == 1 and not result.stderr:
            return None, 'detached'
        reason = 'no_git' if 'not a git repository' in result.stderr else 'unavailable'
        return None, reason
    except (OSError, subprocess.SubprocessError):
        return None, 'unavailable'


def _repositories(roots):
    groups, unavailable = {}, []
    for root in roots:
        path = Path(root['path'])
        cwd = path if path.is_dir() and not path.is_symlink() else path.parent
        top, error = _git(['rev-parse', '--show-toplevel'], cwd)
        if error:
            unavailable.append({'path': str(cwd), 'roots': root['labels'], 'state': error})
        else:
            groups.setdefault(top, []).append(root)
    result = []
    for top, members in groups.items():
        cwd = Path(top)
        commit, commit_error = _git(['rev-parse', '--verify', 'HEAD'], cwd)
        branch, branch_error = _git(['symbolic-ref', '--quiet', '--short', 'HEAD'], cwd)
        default, _ = _git(['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], cwd)
        dirty, dirty_error = _git(['status', '--porcelain=v1', '-z', '--untracked-files=all', '--',
                                   *[r['path'] for r in members]], cwd)
        # NUL output avoids whitespace/quoted-name ambiguity. Rename source and
        # destination are both scoped through the same exclusions.
        changed = []
        if dirty is not None:
            for entry in dirty.split('\0'):
                name = entry[3:] if len(entry) > 2 and entry[2] == ' ' else entry
                if name and not _excluded(Path(name).parts):
                    changed.append(name)
        git_dir, git_error = _git(['rev-parse', '--absolute-git-dir'], cwd)
        interrupted = None
        if git_dir:
            try:
                with os.scandir(git_dir) as entries:
                    names = {e.name for e in entries}
                interrupted = bool(names & {'rebase-merge', 'rebase-apply', 'MERGE_HEAD'})
            except OSError:
                git_error = 'unavailable'
        default = default.split('/', 1)[1] if default and '/' in default else None
        result.append({'path': top, 'roots': [label for r in members for label in r['labels']],
                       'state': 'unavailable' if commit_error or dirty_error or git_error or branch_error not in (None, 'detached') else 'observed',
                       'commit': commit, 'branch': branch, 'detached': branch_error == 'detached' if commit and branch_error in (None, 'detached') else None,
                       'on_default_branch': branch == default if default else branch in ('main', 'master') if branch else None,
                       'dirty': bool(changed) if dirty is not None else None,
                       'interrupted': interrupted})
    return result + unavailable


def source_observation(paths, package_dir: Path, system_yaml: Path | None, *,
                       file_hashes_out: dict | None = None) -> dict:
    roots = {}
    for label, raw in source_inventory(paths, package_dir, system_yaml):
        path = _normal(raw)
        roots.setdefault(str(path), {'path': str(path), 'labels': []})['labels'].append(label)
    allowed = [Path(path) for path in roots if not Path(path).is_symlink()]
    cache = {}
    for path, record in roots.items():
        record.update(_tree(Path(path), allowed, cache))
    records = list(roots.values())
    if file_hashes_out is not None:
        file_hashes_out.update(cache)
    return {'package_root': str(_normal(package_dir)), 'roots': records,
            'repositories': _repositories(records),
            'complete': all(r['complete'] for r in records)}


def observed_file_hash(path: Path, cache: dict) -> str | None:
    """Only a file already safely read can contribute a hash to another field."""
    try:
        return cache.get(str(path.resolve(strict=True)))
    except (OSError, RuntimeError):
        return None


def system_file_hash(paths, package_dir: Path, path: Path) -> str | None:
    """Read-side manifest comparison uses the same source/symlink boundary."""
    roots = [_normal(p) for _, p in source_inventory(paths, package_dir, path)]
    allowed = [p for p in roots if not p.is_symlink()]
    cache = {}
    _tree(_normal(path), allowed, cache)
    return observed_file_hash(path, cache)


def source_warnings(sources) -> list[str]:
    if not isinstance(sources, dict):
        return ['core source provenance unavailable; generate with a source-aware compositor to record it']
    out = []
    for root in sources['roots']:
        if not root['complete']:
            out.append('core source coverage incomplete: ' + ', '.join(root['labels']))
    for repo in sources['repositories']:
        labels = ', '.join(repo['roots'])
        if repo['state'] == 'unavailable':
            out.append(f'core source git state unavailable: {labels}')
        if repo.get('dirty'):
            out.append(f'core source has uncommitted changes: {labels}')
        if repo.get('interrupted'):
            out.append(f'core source git operation interrupted: {labels}')
        if repo.get('detached') or repo.get('on_default_branch') is False:
            out.append(f'core source is detached or off the local default branch: {labels}')
    return out


def source_comparison(recorded, current) -> tuple[list[str], list[str]]:
    warnings = source_warnings(recorded)
    if not isinstance(recorded, dict):
        return [], warnings
    warnings.extend(source_warnings(current))
    before = {r['path']: r for r in recorded['roots']}
    after = {r['path']: r for r in current['roots']}
    changed = []
    for path in before.keys() | after.keys():
        a, b = before.get(path), after.get(path)
        if a != b:
            changed.extend((b or a)['labels'])
    return sorted(set(changed)), list(dict.fromkeys(warnings))
