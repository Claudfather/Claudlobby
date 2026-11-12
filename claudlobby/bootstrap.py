"""Prepare a managed Git installation from a trusted source export; stdlib only.

Run with ``python -m claudlobby.bootstrap`` and explicit installation, data root,
trusted remote/ref and host interpreter. Preparation freezes Git/dependencies and
installs stable entries; it never activates a fleet or launches a local guide.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


class BundleError(ValueError):
    """A fixed preparation refusal; name retained for existing direct callers."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_git_installation(directory: Path, data_root: Path, *, remote: str, ref: str,
                             interpreter: Path) -> dict:
    """Prepare explicit owned code/dependencies only; never initialize a fleet.

    The small preparation receipt freezes a first-install Git resolution. It is
    not an update, activation, owner grant or proof of runtime health.
    """
    import fcntl
    import runpy
    from .installation import Installation, write_installation
    from .git_update import (_write, validate_dependency_lock, run_dependency_command,
                             install_locked_dependencies, verify_locked_dependencies)

    installation = Installation(Path(directory), Path(data_root))
    interpreter = Path(interpreter)
    if (not interpreter.is_absolute() or interpreter != interpreter.resolve()
            or not interpreter.is_file() or not os.access(interpreter, os.X_OK)
            or interpreter.is_relative_to(installation.directory)
            or interpreter.is_relative_to(installation.data_root)):
        raise BundleError("Git preparation requires an independent explicit host Python")
    if (not isinstance(remote, str) or not remote or remote.startswith('-') or not remote.isprintable()
            or remote != remote.strip() or not isinstance(ref, str) or not ref or ref.startswith('-')
            or not ref.isprintable() or any(char.isspace() for char in ref)):
        raise BundleError("Git preparation requires an explicit trusted remote and ref")
    installation.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = installation.directory / 'preparation.json'
    if not receipt.exists() and set(path.name for path in installation.directory.iterdir()) - {
            'installation.json', 'preparation.lock'}:
        raise BundleError("Git preparation refuses an unowned installation directory")
    descriptor = os.open(installation.directory / 'preparation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = {'schema': 1, 'purpose': 'installation-preparation-only',
                   'installation': str(installation.directory), 'data_root': str(installation.data_root),
                   'remote': remote, 'ref': ref, 'interpreter': str(interpreter),
                   'interpreter_sha256': _digest(interpreter)}
        if receipt.is_symlink():
            raise BundleError("Git preparation receipt is redirected")
        if receipt.exists():
            state = json.loads(receipt.read_bytes())
            if not isinstance(state, dict) or any(state.get(key) != value for key, value in binding.items()):
                raise BundleError("existing preparation belongs to different explicit inputs")
        else:
            state = {**binding, 'revision': None, 'lock_sha256': None, 'environment_owned': False,
                     'complete': False}
            _write(receipt, json.dumps(state, sort_keys=True).encode())
        write_installation(installation)
        def save():
            _write(receipt, json.dumps(state, sort_keys=True).encode())
        def git(*arguments, check=True):
            environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
            try:
                result = subprocess.run(['git', '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                    '-C', str(installation.source), *arguments], env=environment, capture_output=True,
                    timeout=300, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                raise BundleError("managed Git preparation did not complete; retry the same explicit inputs") from exc
            if check and result.returncode:
                raise BundleError("managed Git preparation refused; existing files were retained")
            return result
        source = installation.source
        if source.is_symlink() or source.resolve() != source:
            raise BundleError("managed source path is redirected")
        if not source.exists():
            source.mkdir(mode=0o700)
        if not (source / '.git').exists():
            if next(source.iterdir(), None) is not None:
                raise BundleError("managed source contains unowned files")
            git('init', '--quiet', '.')
        if (source / '.git').is_symlink() or not (source / '.git').is_dir():
            raise BundleError("managed source must have its own plain Git directory")
        if state['revision'] is None:
            # FETCH_HEAD survives a crash after fetching but before recording SHA.
            fetched = git('rev-parse', '--verify', 'FETCH_HEAD^{commit}', check=False)
            if fetched.returncode:
                git('fetch', '--no-tags', '--no-recurse-submodules', '--', remote, ref)
                fetched = git('rev-parse', '--verify', 'FETCH_HEAD^{commit}')
            revision = fetched.stdout.decode().strip()
            if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision) is None:
                raise BundleError("trusted ref did not resolve to a full Git commit")
            state['revision'] = revision
            save()
        revision = state['revision']
        head = git('rev-parse', '--verify', 'HEAD^{commit}', check=False)
        if head.returncode:
            git('checkout', '--detach', revision)
        installation.preflight(expected_revision=revision)
        lock = source / 'requirements-runtime.lock'
        content = git('show', revision + ':requirements-runtime.lock').stdout
        validate_dependency_lock(content)
        if lock.is_symlink() or lock.read_bytes() != content:
            raise BundleError("tracked runtime dependency lock differs from the frozen commit")
        digest = hashlib.sha256(content).hexdigest()
        if state['lock_sha256'] not in (None, digest):
            raise BundleError("frozen installation dependency lock changed")
        state['lock_sha256'] = digest
        save()
        environment = installation.environment
        if environment.is_symlink() or environment.resolve() != environment:
            raise BundleError("managed environment is redirected")
        if environment.exists() and not state['environment_owned']:
            raise BundleError("Git preparation refuses an unowned Python environment")
        if not state['complete']:
            state['environment_owned'] = True
            save()
            cache = installation.directory / 'cache'
            wheels = cache / 'bootstrap-wheels'
            for path in (cache, wheels):
                if path.is_symlink() or path.resolve() != path:
                    raise BundleError("dependency preparation cache is redirected")
                path.mkdir(mode=0o700, exist_ok=True)
            for path in wheels.iterdir():
                if path.is_symlink() or not path.is_file() or path.suffix != '.whl':
                    raise BundleError("dependency cache contains an unowned or redirected file")
            frozen_lock = cache / 'bootstrap-requirements.lock'
            if frozen_lock.is_symlink() or frozen_lock.exists() and frozen_lock.read_bytes() != content:
                raise BundleError("frozen dependency preparation input changed")
            if not frozen_lock.exists():
                _write(frozen_lock, content)
            # Re-running venv without --clear preserves unrelated files. This path
            # is receipt-owned, final and never moved (its shebangs remain correct).
            run_dependency_command([interpreter, '-I', '-m', 'venv', environment])
            python = environment / 'bin/python'
            run_dependency_command([python, '-I', '-m', 'pip', '--isolated', 'download',
                '--disable-pip-version-check', '--only-binary=:all:', '--require-hashes', '--no-deps',
                '--dest', wheels, '-r', frozen_lock])
            install_locked_dependencies(python, frozen_lock, wheels)
        python = environment / 'bin/python'
        if not python.is_file() or not os.access(python, os.X_OK):
            raise BundleError("prepared environment is unavailable; use the update recovery owner")
        observed = run_dependency_command([python, '-I', '-c',
            'import json,sys; print(json.dumps({"prefix":sys.prefix,'
            '"base_prefix":sys.base_prefix,"executable":sys.executable,'
            '"version":list(sys.version_info[:2])}))'])
        try:
            identity = json.loads(observed)
            valid = (identity['prefix'] == str(environment)
                     and identity['base_prefix'] != identity['prefix']
                     and identity['executable'] == str(python)
                     and tuple(identity['version']) >= (3, 10))
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            raise BundleError("prepared interpreter is not the exact managed Python environment")
        wheels = installation.directory / 'cache/bootstrap-wheels'
        if wheels.is_symlink() or wheels.resolve() != wheels or wheels.exists() and not wheels.is_dir():
            raise BundleError("dependency preparation cache is redirected")
        verify_locked_dependencies(python, lock, wheels if wheels.is_dir() else None)
        installation.preflight(expected_revision=revision)
        for name in ('installation.py', 'git_update.py'):
            target = source / 'claudlobby' / name
            if target.is_symlink() or target.read_bytes() != git('show', revision + ':claudlobby/' + name).stdout:
                raise BundleError("stable entry source differs from the frozen target commit")
        entries = {'claudlobby': source / 'claudlobby/installation.py',
                   'claudlobby-update': source / 'claudlobby/git_update.py'}
        if installation.bin.is_symlink() or installation.bin.resolve() != installation.bin:
            raise BundleError("stable entry directory is redirected")
        if installation.bin.exists() and set(path.name for path in installation.bin.iterdir()) - set(entries):
            raise BundleError("stable entry directory contains unowned files")
        for name, target in entries.items():
            destination = installation.bin / name
            expected = f'#!{interpreter}\n'.encode() + target.read_bytes()
            if destination.is_symlink():
                raise BundleError("stable entry destination contains foreign bytes or mode")
            if destination.exists():
                info = destination.lstat()
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o755
                        or destination.read_bytes() != expected):
                    raise BundleError("stable entry destination contains foreign bytes or mode")
            elif state['complete']:
                raise BundleError("prepared stable entry is missing; use the update recovery owner")
        if state['complete']:
            return {'installation': str(installation.directory), 'data_root': str(data_root),
                    'source_revision': revision, 'cli': str(installation.cli), 'prepared': True, 'activated': False}
        namespace = runpy.run_path(str(source / 'claudlobby/installation.py'), run_name='_prepared_installation')
        target_installation = namespace['Installation'](installation.directory, installation.data_root)
        namespace['install_launcher'](target_installation, interpreter=interpreter)
        namespace['install_updater'](target_installation, interpreter=interpreter)
        state['complete'] = True
        save()
        return {'installation': str(installation.directory), 'data_root': str(data_root),
                'source_revision': revision, 'cli': str(installation.cli), 'prepared': True, 'activated': False}
    finally:
        os.close(descriptor)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--remote", required=True, help="Explicit trusted Git remote")
    parser.add_argument("--ref", required=True, help="Resolve once for this preparation")
    parser.add_argument("--interpreter", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = prepare_git_installation(args.installation.expanduser(), args.root.expanduser(),
                                          remote=args.remote, ref=args.ref,
                                          interpreter=args.interpreter.expanduser())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Never echo remote credentials, Git/Python output or retained metadata.
        if isinstance(exc, BlockingIOError):
            code, message, status = "conflict", "Git preparation is already in progress", 4
        elif isinstance(exc, OSError):
            code, message, status = "unavailable", "Git preparation filesystem or executable is unavailable; partial work retained", 6
        else:
            code, message, status = "conflict", "Git preparation refused; inspect retained installation and explicit inputs", 4
        if args.json:
            print(json.dumps({"ok": False, "error": {"code": code, "message": message}}, sort_keys=True))
        else:
            print(message, file=sys.stderr)
        return status
    if args.json:
        print(json.dumps({"ok": True, **result}, sort_keys=True))
    else:
        print(f"Git installation prepared at {result['installation']} (revision {result['source_revision']}).")
        print(f"CLI: {result['cli']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
