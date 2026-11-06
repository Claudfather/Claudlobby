"""Explicit Git installation paths and read-only update preflight.

This record supplies locations and source provenance, never runtime authority.
The update owner alone changes Git, dependencies, configuration or services.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


class InstallationError(ValueError):
    """Installation paths or the managed source cannot be verified."""


class FencedError(InstallationError):
    """An update's fence turned a native start away: nothing failed, and a later start can pass."""


def unit_systemd_command(argv) -> str:
    """Literal argv in systemd's quoted command syntax, including $/% escaping."""
    return " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("%", "%%").replace("$", "$$") + '"' for part in argv)


def _path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path != path.resolve():
        raise InstallationError("installation paths must be absolute and unredirected")
    return path


def checkout_revision(checkout: Path, *, clean: bool = False) -> str:
    """Read only this explicit Git worktree; dirty scans are update preflight."""
    checkout = _path(checkout)
    # Ambient Git selectors must not redirect a check to another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args):
        try:
            result = subprocess.run(["git", "--no-optional-locks", "-C", str(checkout), *args],
                                    env=env, capture_output=True, text=True, timeout=15, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallationError("explicit installation Git checkout is unavailable") from exc
        return result.stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")) != checkout:
        raise InstallationError("source must be the exact Git checkout root")
    revision = git("rev-parse", "--verify", "HEAD^{commit}")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise InstallationError("source Git revision is invalid")
    if clean:
        dirty = git("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
        if dirty:
            raise InstallationError(f"managed source is dirty; leave these paths unchanged:\n{dirty}")
        if git("rev-parse", "--verify", "HEAD^{commit}") != revision:
            raise InstallationError("source revision changed during preflight")
    return revision


@dataclass(frozen=True)
class Installation:
    directory: Path
    data_root: Path

    def __post_init__(self):
        object.__setattr__(self, "directory", _path(self.directory))
        object.__setattr__(self, "data_root", _path(self.data_root))
        if (self.data_root.is_relative_to(self.directory)
                or self.directory.is_relative_to(self.data_root)):
            raise InstallationError("installation and persistent data must be separate")

    @property
    def source(self) -> Path:
        return self.directory / "src"

    @property
    def environment(self) -> Path:
        return self.directory / "env"

    @property
    def bin(self) -> Path:
        return self.directory / "bin"

    @property
    def cli(self) -> Path:
        return self.bin / "claudlobby"

    @property
    def native(self) -> Path:
        return self.source / "claudlobby/_runtime_scripts"

    def preflight(self, *, expected_revision: str | None = None) -> str:
        """Verify the managed checkout before effects; never repair local edits."""
        revision = checkout_revision(self.source, clean=True)
        if expected_revision is not None and expected_revision != revision:
            raise InstallationError("managed source revision differs from the expected revision")
        return revision


def read_installation(directory: Path) -> Installation:
    directory = _path(directory)
    record = directory / "installation.json"
    try:
        if record.is_symlink():
            raise InstallationError("installation record is redirected")
        payload = json.loads(record.read_bytes())
        if set(payload) != {"schema", "data_root"} or type(payload["schema"]) is not int or payload["schema"] != 1:
            raise InstallationError("unsupported installation record")
        return Installation(directory, Path(payload["data_root"]))
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, InstallationError):
            raise
        raise InstallationError("installation record is unavailable or invalid") from exc


def write_installation(installation: Installation) -> None:
    """Atomically record explicit paths; do not create a checkout or environment."""
    # A differing existing record is not silently repointed by setup/retry.
    record = installation.directory / "installation.json"
    if record.exists() or record.is_symlink():
        if read_installation(installation.directory) != installation:
            raise InstallationError("installation is already bound to another data root")
        return
    installation.directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".installation-", dir=installation.directory)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"schema": 1, "data_root": str(installation.data_root)}, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, record)  # atomic create; never replace a concurrent binding
        except FileExistsError:
            if read_installation(installation.directory) != installation:
                raise InstallationError("installation is already bound to another data root")
        directory_fd = os.open(installation.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def install_launcher(installation: Installation, *, interpreter: Path) -> None:
    """Install this stdlib entry outside src/env; create no runtime or services."""
    _install_entry(installation, interpreter, Path(__file__), installation.cli)
    state = _path(installation.data_root / "state")
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "runtime-use.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    _sync_directory(state)


def install_updater(installation: Installation, *, interpreter: Path) -> None:
    """Install the independent updater outside the mutable source/environment."""
    _install_entry(installation, interpreter, Path(__file__).with_name("git_update.py"),
                   installation.bin / "claudlobby-update")


def _install_entry(installation, interpreter, source, destination):
    if read_installation(installation.directory) != installation:
        raise InstallationError("launcher installation differs from its recorded paths")
    if not Path(interpreter).is_absolute():
        raise InstallationError("launcher requires an explicit absolute host Python")
    interpreter = Path(interpreter).resolve()
    if (not interpreter.is_file() or not os.access(interpreter, os.X_OK)
            or any(char.isspace() for char in str(interpreter))
            or interpreter.is_relative_to(installation.source)
            or interpreter.is_relative_to(installation.environment)):
        raise InstallationError("launcher needs an executable host Python outside source/environment")
    _path(installation.bin).mkdir(parents=True, exist_ok=True)
    content = f"#!{interpreter}\n".encode() + source.read_bytes()
    fd, name = tempfile.mkstemp(prefix=".launcher-", dir=installation.bin)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o755)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(installation.bin, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _runtime_fd(installation, inherited=None):
    path = _path(installation.data_root / "state/runtime-use.lock")
    fd = inherited
    try:
        if fd is None:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        linked, opened = path.lstat(), os.fstat(fd)
        if (not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)):
            raise InstallationError("runtime exclusion file changed")
        return fd
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        raise InstallationError("runtime is in use or its exclusion lock is unavailable") from None


def _runtime_lock(installation, *, exclusive=False, inherited=None):
    fd = _runtime_fd(installation, inherited)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        os.set_inheritable(fd, False)
        return fd
    except OSError:
        os.close(fd)
        raise InstallationError("runtime is in use or its exclusion lock is unavailable") from None


@contextmanager
def native_start_fence(installation):
    """Coordinator EX: after quiesce, before final quiet, through config commit.

    Persist quiesce first to deny new native entries while existing short CLI
    calls finish saves. Acquire EX after stopping consumers, then re-observe
    quiet before cutover. Exit before restoring services. Never hold this for
    a service lifetime or replace the independent update-owner process lock.
    """
    fd = _runtime_lock(installation, exclusive=True)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(frozen=True)
class GitUnitStart:
    argv: tuple[str, ...]
    root: Path
    unit: str
    phase: str
    mode: str
    command: tuple[str, ...]


def parse_git_unit_argv(argv) -> GitUnitStart:
    if (not isinstance(argv, (list, tuple)) or len(argv) < 12
            or not all(isinstance(value, str) and "\x00" not in value for value in argv)
            or not all(value and value.isprintable() for value in argv[:12])
            or argv[1] != "--root" or tuple(argv[3:5]) != ("_native", "--unit")
            or argv[6] != "--phase" or argv[8] != "--mode" or argv[10] != "--"
            or not all(Path(argv[index]).is_absolute() for index in (0, 2, 11))
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", argv[5])
            or argv[7] not in {"bots", "ingest", "producers"} or argv[9] not in {"exec", "oneshot"}
            or argv[7] in {"bots", "ingest"} and argv[9] != "exec"):
        raise InstallationError("native entry requires the exact committed argument vector")
    return GitUnitStart(tuple(argv), Path(argv[2]), argv[5], argv[7], argv[9], tuple(argv[11:]))


def _committed_snapshot(installation):
    path = _path(installation.data_root / "state/config.json")
    try:
        snapshot = json.loads(path.read_bytes())
        fingerprint = snapshot["fingerprint"]
        payload = {key: value for key, value in snapshot.items() if key != "fingerprint"}
        if (type(snapshot["schema"]) is not int or snapshot["schema"] != 1
                or snapshot["installation"] != str(installation.directory)
                or snapshot["data_root"] != str(installation.data_root)
                or snapshot["source"] != str(installation.source)
                or fingerprint != hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()):
            raise ValueError()
        return snapshot
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise InstallationError("committed configuration cannot be verified") from exc


def _committed_unit(installation, target, environment, *, directory=None):
    snapshot = _committed_snapshot(installation)
    try:
        platform = "Darwin" if sys.platform == "darwin" else "Linux" if sys.platform == "linux" else "unsupported"
        rows = [row for row in snapshot["units"] if row["platform"] == platform
                and isinstance(row.get("admission"), dict) and row["admission"].get("unit") == target.unit]
        if len(rows) != 1:
            raise ValueError()
        row = rows[0]
        admission = row["admission"]
        if (admission != {"kind": "git-native-v1", "unit": target.unit, "mode": target.mode,
                          "argv": list(target.argv), "command": list(target.command)}
                or row["phase"] != target.phase or row["enroll"] is not True
                or Path(row["working_directory"]) != (Path.cwd() if directory is None else directory)
                or type(row["environment"]) is not dict
                or any(environment.get(key) != value for key, value in row["environment"].items())):
            raise ValueError()
        if row["scope"] == "bot":
            if (target.phase != "bots" or row["fleet"] not in snapshot["fleets"]
                    or not isinstance(row["bot"], str) or Path(row["working_directory"]).name != row["bot"]):
                raise ValueError()
        elif (row["scope"] not in {"fleet", "host"} or target.phase == "bots" or row["bot"] is not None
                or row["scope"] == "fleet" and row["fleet"] not in snapshot["fleets"]
                or row["scope"] == "host" and row["fleet"] is not None):
            raise ValueError()
        required = {"CLAUDLOBBY_ROOT": str(installation.data_root), "CLAUDLOBBY_INSTALLATION": str(installation.directory),
                    "CLAUDLOBBY_CLI": str(installation.cli), "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
                    "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
        if any(row["environment"].get(key) != value for key, value in required.items()):
            raise ValueError()
        source = _path(Path(row["source"]))
        if (not source.is_file() or source.is_symlink() or stat.S_IMODE(source.stat().st_mode) != row["mode"]
                or hashlib.sha256(source.read_bytes()).hexdigest() != row["sha256"]
                or snapshot["revision"] != checkout_revision(installation.source)):
            raise ValueError()
        return row
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise InstallationError("native command or context differs from committed configuration") from exc


def _native_shell(installation, arguments, environment):
    """Private inherited-fd boundary for startup/watchdog, not an action grant.

    Acquire locks the shell-owned open description. Explicit release unlocks
    its inherited copies too; neither operation imports the changing runtime.
    """
    try:
        action, descriptor, *values = arguments
        fd = int(descriptor)
        if fd < 3:
            raise ValueError()
        if action == "python" and not values:
            fd = _runtime_lock(installation, inherited=fd)
            try:
                _check_maintenance(installation, native=True)
                python = installation.environment / "bin/python"
                if python.parent.resolve() != python.parent or not python.is_file() or not os.access(python, os.X_OK):
                    raise InstallationError("recorded runtime Python is unavailable")
                print(python)
                return 0
            finally:
                os.close(fd)  # leave the shared description locked in the shell
        if not values or int(values[0]) != os.getppid():
            raise InstallationError("native shell caller changed")
        if action == "release" and len(values) == 1:
            fd = _runtime_fd(installation, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return 0
        if action != "acquire" or len(values) != 6:
            raise ValueError()
        _, operation, directory, label, fleet, native = values
        fd = _runtime_lock(installation, inherited=fd)
        try:
            _check_maintenance(installation, native=True)
            if (operation not in {"start-bot", "keepalive"} or native != str(installation.native)
                    or environment.get("CLAUDLOBBY_NATIVE_DIR") != native):
                raise ValueError()
            directory = _path(Path(directory))
            snapshot = _committed_snapshot(installation)
            platform = "Darwin" if sys.platform == "darwin" else "Linux" if sys.platform == "linux" else "unsupported"
            rows = [row for row in snapshot["units"] if row["platform"] == platform
                    and row["scope"] == "bot" and row["working_directory"] == str(directory)
                    and isinstance(row.get("admission"), dict) and row["admission"].get("unit") == label]
            if len(rows) != 1 or rows[0]["fleet"] != fleet:
                raise ValueError()
            row = rows[0]
            target = parse_git_unit_argv(row["admission"]["argv"])
            if (target.argv[0] != str(installation.cli) or target.root != installation.data_root
                    or target.command != (str(installation.native / "start-bot.sh"), str(directory))):
                raise ValueError()
            _committed_unit(installation, target, row["environment"], directory=directory)
            expected = row["environment"]
            keys = ("CLAUDLOBBY_ROOT", "CLAUDLOBBY_INSTALLATION", "CLAUDLOBBY_CLI",
                    "CLAUDLOBBY_NATIVE_DIR", "CLAUDLOBBY_LIBRARY_DIR", "FLEET_ROOT")
            if any(environment.get(key) != expected.get(key) for key in keys):
                raise ValueError()
            configuration = _path(directory / "bot.conf")
            outputs = [output["state"] for output in snapshot["owned_outputs"]
                       if output["target"] == str(configuration)]
            if (not configuration.is_file() or len(outputs) != 1 or outputs[0] != {
                    "kind": "file", "mode": stat.S_IMODE(configuration.stat().st_mode),
                    "sha256": hashlib.sha256(configuration.read_bytes()).hexdigest()}):
                raise ValueError()
            return 0
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            raise
        finally:
            os.close(fd)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InstallationError):
            raise
        raise InstallationError("native shell context differs from committed configuration") from exc


_NATIVE_CLI = """import pathlib,sys
source=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(source))
import claudlobby
if pathlib.Path(claudlobby.__file__).resolve()!=source/'claudlobby/__init__.py':
    raise SystemExit('native CLI package differs from committed source')
from claudlobby.__main__ import main
raise SystemExit(main(sys.argv[2:]))
"""


def _run_git_unit(installation, argv, environment):
    target = parse_git_unit_argv(argv)
    if target.argv[0] != str(installation.cli) or target.root != installation.data_root:
        raise InstallationError("native entry differs from its installation/data root")
    try:
        _check_maintenance(installation, native=True)  # before the lock: a fenced job is no reader for the updater
        _exec_git_unit(installation, target, environment)
    except FencedError as exc:
        # A scheduled job that meets the fence ends clean, so its timer raises no failure; a resident
        # unit must stay a failure or systemd would read it active with nothing running.
        if target.mode != "oneshot":
            raise
        print(f"claudlobby: skipped {target.unit}: {exc}")
        return 0


def _exec_git_unit(installation, target, environment):
    fd = _runtime_lock(installation)
    try:
        _check_maintenance(installation, native=True)
        _committed_unit(installation, target, environment)
        clean = {key: value for key, value in environment.items()
                 if key not in {"BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME"} and not key.startswith("BASH_FUNC_")}
        if target.command[0] == str(installation.cli):
            python = installation.environment / "bin/python"
            arguments = _bound_arguments(installation, list(target.command[1:]))
            os.execve(python, [str(python), "-I", "-B", "-c", _NATIVE_CLI,
                               str(installation.source), *arguments], clean)
        os.execve(target.command[0], target.command, clean)
    finally:
        os.close(fd)  # CLOEXEC closes it at the native process transition


def _global_option(argument: str) -> str:
    """Normalize only unambiguous argparse global long-option prefixes."""
    option = argument.partition("=")[0]
    if option.startswith("--"):
        matches = [name for name in ("--root", "--fleet", "--seed", "--json",
                                     "--version", "--verbose", "--help") if name.startswith(option)]
        if len(matches) == 1:
            return matches[0]
    return option


def _bound_arguments(installation: Installation, argv: list[str]) -> list[str]:
    """Pin root without allowing another root option to override it later."""
    arguments = []
    iterator = iter(argv)
    for argument in iterator:
        if argument == "--":
            arguments.extend([argument, *iterator])
            break
        _, separator, inline = argument.partition("=")
        option = _global_option(argument)
        # The current argparse CLI accepts unambiguous long-option prefixes.
        # Normalize those too, so a later abbreviation cannot override our root.
        if option == "--root" and not separator:
            value = next(iterator, None)
            if value is None:
                raise InstallationError("--root requires the recorded data root")
        elif option == "--root":
            value = inline
        else:
            arguments.append(argument)
            continue
        if not Path(value).is_absolute() or Path(value) != installation.data_root:
            raise InstallationError("CLI data root differs from the recorded installation")
    inherited = os.environ.get("CLAUDLOBBY_ROOT")
    if inherited is not None and inherited != str(installation.data_root):
        raise InstallationError("environment data root differs from the recorded installation")
    return ["--root", str(installation.data_root), *arguments]


def _update_route(argv: list[str]) -> bool:
    iterator = iter(argv)
    for argument in iterator:
        option = _global_option(argument)
        if option in {"--root", "--fleet"}:
            if "=" not in argument:
                next(iterator, None)
        elif option in {"--json", "--seed", "-v", "--verbose"} and "=" not in argument:
            continue
        else:
            return (argument == "host" and next(iterator, None) == "update"
                    and next(iterator, None) not in {"runtime", "siblings"})
    return False


def _check_maintenance(installation: Installation, *, native=False) -> None:
    record = installation.data_root / "state/update.json"
    try:
        if record.is_symlink() or record.parent.resolve() != record.parent:
            raise InstallationError("maintenance record is redirected")
        payload = json.loads(record.read_bytes())
        if (type(payload) is not dict or type(payload.get("schema")) is not int or payload["schema"] != 1
                or type(payload.get("maintenance")) is not bool
                or not isinstance(payload.get("phase"), str) or not payload["phase"]):
            raise InstallationError("maintenance record has an unsupported schema")
        if payload["maintenance"]:
            raise InstallationError("maintenance is active; use host update status or recovery")
        if native and payload["phase"] not in {"prepare", "commit", "restore", "done"}:
            raise FencedError("native startup is fenced while the update quiesces consumers")
    except FileNotFoundError:
        return  # no update exists; core admission still owns operational authority
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, InstallationError):
            raise
        raise InstallationError("maintenance record cannot be verified") from exc


def _launch(argv: list[str]) -> int:
    # This file is copied outside both changing trees. No application imports
    # occur until the recorded root and current maintenance fence are checked.
    # Generated bot PATH entries symlink to this stable launcher. Bind its
    # canonical file, never the alias directory or an inherited installation.
    launcher = Path(__file__).resolve(strict=True)
    installation = read_installation(launcher.parent.parent)
    if launcher != installation.cli:
        raise InstallationError("invoke the stable installation launcher")
    runtime = bool(argv and argv[0] == "--_source-runtime")
    inherited = None
    if runtime:
        try:
            inherited = int(argv[1])
        except (IndexError, ValueError):
            raise InstallationError("runtime entry requires its inherited reader lock") from None
    arguments = _bound_arguments(installation, argv[2:] if runtime else argv)
    environment = {**os.environ, "CLAUDLOBBY_INSTALLATION": str(installation.directory),
                   "CLAUDLOBBY_ROOT": str(installation.data_root), "PYTHONDONTWRITEBYTECODE": "1"}
    if _update_route(arguments):
        updater = installation.bin / "claudlobby-update"
        if not updater.is_file() or not os.access(updater, os.X_OK):
            raise InstallationError("independent Git updater is unavailable; no runtime fallback attempted")
        os.execve(updater, [str(updater), *arguments], environment)
    if len(arguments) > 2 and arguments[2] == "_native-shell":
        if os.environ.get("CLAUDLOBBY_INSTALLATION") != str(installation.directory):
            raise InstallationError("native shell installation differs from its launcher")
        return _native_shell(installation, arguments[3:], environment)
    _check_maintenance(installation)
    if len(arguments) > 2 and arguments[2] == "_native":
        return _run_git_unit(installation, [str(installation.cli), *arguments], environment)
    fd = _runtime_lock(installation, inherited=inherited)
    try:
        return _run_cli(installation, arguments, environment, runtime, fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_cli(installation, arguments, environment, runtime, fd):
    _check_maintenance(installation)  # same lock as coordinator, before env/source use
    python = installation.environment / "bin/python"
    if not runtime:
        if not python.is_file() or not os.access(python, os.X_OK):
            raise InstallationError("recorded runtime Python is unavailable")
        os.set_inheritable(fd, True)
        os.execve(python, [str(python), "-I", "-B", str(installation.cli), "--_source-runtime", str(fd), *arguments], environment)
    if Path(sys.executable).absolute() != python:
        raise InstallationError("runtime interpreter differs from the recorded environment")
    os.environ.update(environment)
    source = _path(installation.source)
    package = source / "claudlobby/__init__.py"
    if not package.is_file() or package.resolve() != package:
        raise InstallationError("recorded source package is unavailable or redirected")
    sys.path.insert(0, str(source))
    import claudlobby
    if Path(claudlobby.__file__).resolve() != source / "claudlobby/__init__.py":
        raise InstallationError("runtime package does not belong to the recorded checkout")
    from claudlobby.__main__ import main
    return main(arguments)


if __name__ == "__main__":
    try:
        sys.exit(_launch(sys.argv[1:]))
    except FencedError as exc:
        print(f"claudlobby: {exc}", file=sys.stderr)
        sys.exit(75)
    except (InstallationError, OSError) as exc:
        print(f"claudlobby: {exc}", file=sys.stderr)
        sys.exit(6)
