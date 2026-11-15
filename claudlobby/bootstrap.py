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
import pwd
import configparser
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


_BROWSER_REMOTE = "https://github.com/Claudfather/Claudlobby.git"
_PREPARATION_FIELDS = {"schema", "purpose", "installation", "data_root", "remote", "ref",
                       "interpreter", "interpreter_sha256", "revision", "lock_sha256",
                       "environment_owned", "complete"}
_GIT_SAFETY = ("--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "credential.helper=", "-c", "core.askPass=/bin/false", "-c", "http.sslVerify=true",
               "-c", "maintenance.auto=false", "-c", "gc.auto=0")


def _checked_lock(descriptor, path):
    """An inherited integer is never authority to skip the canonical flock."""
    import fcntl
    if type(descriptor) is not int or descriptor < 0:
        raise BundleError("preparation lock descriptor is invalid")
    observed, canonical = os.fstat(descriptor), path.lstat()
    if (not stat.S_ISREG(observed.st_mode) or canonical.st_dev != observed.st_dev
            or canonical.st_ino != observed.st_ino or not stat.S_ISREG(canonical.st_mode)
            or observed.st_uid != os.geteuid() or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o600 or observed.st_size != 0
            or fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR):
        raise BundleError("canonical preparation lock is redirected or unsafe")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _checked_git_config(source):
    """Fresh bootstrap repos need only init-generated core configuration."""
    config = source / ".git/config"
    if not config.exists() and not config.is_symlink():
        return
    info = config.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or info.st_mode & 0o022 or info.st_size > 65536):
        raise BundleError("browser Git configuration is unsafe")
    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(config.read_text())
        values = dict(parser.items("core"))
        allowed = {"repositoryformatversion": {"0"}, "filemode": {"true"}, "bare": {"false"},
                   "logallrefupdates": {"true"}, "ignorecase": {"true", "false"},
                   "precomposeunicode": {"true", "false"}}
        if (parser.sections() != ["core"] or not values or parser.defaults()
                or any(key not in allowed or value not in allowed[key] for key, value in values.items())):
            raise ValueError()
    except (ValueError, configparser.Error) as exc:
        raise BundleError("browser Git configuration is not the fixed profile") from exc
    if any((source / ".git" / name).exists() for name in ("commondir", "objects/info/alternates")):
        raise BundleError("browser Git storage is redirected")


def _browser_receipt(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise BundleError("browser preparation receipt is unsafe")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(65537)
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        state = json.loads(raw, object_pairs_hook=unique)
        if (len(raw) > 65536 or not isinstance(state, dict) or set(state) != _PREPARATION_FIELDS
                or type(state["schema"]) is not int or state["schema"] != 1
                or type(state["complete"]) is not bool or type(state["environment_owned"]) is not bool):
            raise ValueError()
        return state
    except (ValueError, TypeError) as exc:
        raise BundleError("browser preparation receipt is invalid") from exc
    finally:
        os.close(fd)


def _browser_environment(installation):
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    family = home / ".local/share/claudlobby-browser"
    if installation.directory != family / "install" or installation.data_root != family / "data":
        raise BundleError("browser preparation requires the fixed account paths")
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PLANE_EMIT_DISABLED": "1",
            "TMPDIR": str(home / ".local/share/claudlobby-browser/staging")}


def _browser_startup_files_safe(environment):
    # Narrow admission for retained incomplete environments, not a general
    # defense against arbitrary same-UID/root modification. Preserve the known
    # ensurepip setuptools shim used by CPython 3.10/3.11; unknown hooks stop.
    lib = environment / "lib"
    def private_directory(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o700)
    if not private_directory(lib):
        return False
    expected = "python" + str(sys.version_info.major) + "." + str(sys.version_info.minor)
    if {path.name for path in lib.iterdir()} != {expected}:
        return False
    version = lib / expected
    site = version / "site-packages"
    if not private_directory(version) or not private_directory(site):
        return False
    for path in site.iterdir():
        if (path.name in {"sitecustomize", "usercustomize"}
                or path.name.startswith(("sitecustomize.", "usercustomize.")) or path.suffix == ".pth"):
            if (path.name != "distutils-precedence.pth" or path.is_symlink()
                    or not path.is_file() or path.stat().st_size > 4096
                    or _digest(path) != "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224"):
                return False
    return True


def _browser_data_preflight(data_root):
    """Only install_launcher own inactive skeleton can precede fleet setup."""
    info = data_root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise BundleError("browser data root is unsafe")
    if set(path.name for path in data_root.iterdir()) - {"state"}:
        raise BundleError("browser data root contains existing state")
    state = data_root / "state"
    if state.exists() or state.is_symlink():
        info = state.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or set(path.name for path in state.iterdir()) - {"runtime-use.lock"}):
            raise BundleError("browser data root contains existing state")
        lock = state / "runtime-use.lock"
        if lock.exists() or lock.is_symlink():
            info = lock.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size):
                raise BundleError("browser runtime lock is unsafe")


def prepare_git_installation(directory: Path, data_root: Path, *, remote: str, ref: str,
                             interpreter: Path, _preparation_lock_fd=None, _browser_runner=None) -> dict:
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
    isolated = _browser_runner is not None
    if isolated and (not callable(_browser_runner) or _preparation_lock_fd is None
                     or remote != _BROWSER_REMOTE or re.fullmatch(r"[0-9a-f]{40}", ref) is None):
        raise BundleError("browser preparation requires its fixed pinned profile and canonical lock")
    clean_environment = _browser_environment(installation) if isolated else None
    if isolated:
        _browser_data_preflight(installation.data_root)
    installation.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = installation.directory / 'preparation.json'
    if not receipt.exists() and set(path.name for path in installation.directory.iterdir()) - {
            'installation.json', 'preparation.lock'}:
        raise BundleError("Git preparation refuses an unowned installation directory")
    owns_descriptor = _preparation_lock_fd is None
    descriptor = (os.open(installation.directory / 'preparation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                  if owns_descriptor else _preparation_lock_fd)
    try:
        _checked_lock(descriptor, installation.directory / "preparation.lock")
        binding = {'schema': 1, 'purpose': 'installation-preparation-only',
                   'installation': str(installation.directory), 'data_root': str(installation.data_root),
                   'remote': remote, 'ref': ref, 'interpreter': str(interpreter),
                   'interpreter_sha256': _digest(interpreter)}
        if receipt.is_symlink():
            raise BundleError("Git preparation receipt is redirected")
        if receipt.exists():
            state = _browser_receipt(receipt) if isolated else json.loads(receipt.read_bytes())
            if not isinstance(state, dict) or any(state.get(key) != value for key, value in binding.items()):
                raise BundleError("existing preparation belongs to different explicit inputs")
        else:
            state = {**binding, 'revision': None, 'lock_sha256': None, 'environment_owned': False,
                     'complete': False}
            _write(receipt, json.dumps(state, sort_keys=True).encode())
        pinned_revision = ref.lower() if re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', ref) else None
        def checked_revision(value):
            if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) is None:
                raise BundleError("preparation revision is not a full Git commit")
            if pinned_revision is not None and value != pinned_revision:
                raise BundleError("preparation revision differs from the explicit Git commit")
            return value
        if state['revision'] is None:
            if (state['complete'] is not False or state['environment_owned'] is not False
                    or state['lock_sha256'] is not None):
                raise BundleError("unresolved preparation receipt contains retained progress")
        else:
            checked_revision(state['revision'])
        write_installation(installation)
        def save():
            _write(receipt, json.dumps(state, sort_keys=True).encode())
        def isolated_command(arguments, *, timeout=300):
            return _browser_runner([str(value) for value in arguments],
                                   env=clean_environment, timeout=timeout)
        def policy_git(command):
            _checked_git_config(installation.source)
            return isolated_command(["/usr/bin/git", *_GIT_SAFETY, *command[2:]], timeout=15)
        def preflight():
            if isolated:
                return installation.preflight(expected_revision=revision, _run_git=policy_git)
            return installation.preflight(expected_revision=revision)
        def dependency(arguments):
            if not isolated:
                return run_dependency_command(arguments)
            arguments = [str(value) for value in arguments]
            if "pip" in arguments:
                arguments.insert(arguments.index("pip") + 1, "--no-cache-dir")
            result = isolated_command(arguments)
            if result.returncode:
                raise BundleError("browser dependency preparation refused")
            return result.stdout
        def git(*arguments, check=True):
            if isolated:
                _checked_git_config(installation.source)
                result = isolated_command(["/usr/bin/git", *_GIT_SAFETY,
                                           "-C", str(installation.source), *arguments])
                if check and result.returncode:
                    raise BundleError("managed Git preparation refused; existing files were retained")
                return result
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
            # An unrecorded FETCH_HEAD has no symbolic-ref provenance. Resolve
            # that explicit remote/ref again before any dependency effects. A
            # full commit pin can safely reuse only its exact fetched identity.
            fetched = (git('rev-parse', '--verify', 'FETCH_HEAD^{commit}', check=False)
                       if pinned_revision is not None else None)
            if fetched is None or fetched.returncode:
                git('fetch', '--no-tags', '--no-recurse-submodules', '--', remote, ref)
                fetched = git('rev-parse', '--verify', 'FETCH_HEAD^{commit}')
            revision = checked_revision(fetched.stdout.decode().strip())
            state['revision'] = revision
            save()
        revision = state['revision']
        head = git('rev-parse', '--verify', 'HEAD^{commit}', check=False)
        if head.returncode:
            git('checkout', '--detach', revision)
        preflight()
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
        if isolated and ((environment / "pip.conf").exists() or (environment / "pip.conf").is_symlink()):
            raise BundleError("browser environment contains local pip configuration")
        if isolated and environment.exists() and not _browser_startup_files_safe(environment):
            raise BundleError("browser environment contains unknown Python startup files")
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
            dependency([interpreter, '-I', '-m', 'venv', environment])
            python = environment / 'bin/python'
            dependency([python, '-I', '-m', 'pip', '--isolated', 'download',
                '--disable-pip-version-check', '--only-binary=:all:', '--require-hashes', '--no-deps',
                '--dest', wheels, '-r', frozen_lock])
            if isolated:
                install_locked_dependencies(python, frozen_lock, wheels, runner=dependency)
            else:
                install_locked_dependencies(python, frozen_lock, wheels)
        python = environment / 'bin/python'
        if not python.is_file() or not os.access(python, os.X_OK):
            raise BundleError("prepared environment is unavailable; use the update recovery owner")
        observed = dependency([python, '-I', '-c',
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
        if isolated:
            verify_locked_dependencies(python, lock, wheels if wheels.is_dir() else None, runner=dependency)
        else:
            verify_locked_dependencies(python, lock, wheels if wheels.is_dir() else None)
        preflight()
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
        if isolated:
            _browser_data_preflight(installation.data_root)
        namespace = runpy.run_path(str(source / 'claudlobby/installation.py'), run_name='_prepared_installation')
        target_installation = namespace['Installation'](installation.directory, installation.data_root)
        namespace['install_launcher'](target_installation, interpreter=interpreter)
        namespace['install_updater'](target_installation, interpreter=interpreter)
        state['complete'] = True
        save()
        return {'installation': str(installation.directory), 'data_root': str(data_root),
                'source_revision': revision, 'cli': str(installation.cli), 'prepared': True, 'activated': False}
    finally:
        if owns_descriptor:
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
