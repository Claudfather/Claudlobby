"""Explicit Git installation paths and read-only update preflight.

This record supplies locations and source provenance, never runtime authority.
The update owner alone changes Git, dependencies, configuration or services.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class InstallationError(ValueError):
    """Installation paths or the managed source cannot be verified."""


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


def install_updater(installation: Installation, *, interpreter: Path) -> None:
    """Install independent current-update inspection; it exposes no apply shortcut."""
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


def _check_maintenance(installation: Installation) -> None:
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
    except FileNotFoundError:
        return  # no update exists; core admission still owns operational authority
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, InstallationError):
            raise
        raise InstallationError("maintenance record cannot be verified") from exc


def _launch(argv: list[str]) -> int:
    # This file is copied outside both changing trees. No application imports
    # occur until the recorded root and current maintenance fence are checked.
    installation = read_installation(Path(__file__).absolute().parent.parent)
    if Path(__file__).absolute() != installation.cli:
        raise InstallationError("invoke the stable installation launcher")
    runtime = bool(argv and argv[0] == "--_source-runtime")
    arguments = _bound_arguments(installation, argv[1:] if runtime else argv)
    environment = {**os.environ, "CLAUDLOBBY_INSTALLATION": str(installation.directory),
                   "CLAUDLOBBY_ROOT": str(installation.data_root), "PYTHONDONTWRITEBYTECODE": "1"}
    if _update_route(arguments):
        updater = installation.bin / "claudlobby-update"
        if not updater.is_file() or not os.access(updater, os.X_OK):
            raise InstallationError("independent Git updater is unavailable; no runtime fallback attempted")
        os.execve(updater, [str(updater), *arguments], environment)
    _check_maintenance(installation)
    python = installation.environment / "bin/python"
    if not runtime:
        if not python.is_file() or not os.access(python, os.X_OK):
            raise InstallationError("recorded runtime Python is unavailable")
        os.execve(python, [str(python), "-I", "-B", str(installation.cli), "--_source-runtime", *arguments], environment)
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
    except (InstallationError, OSError) as exc:
        print(f"claudlobby: {exc}", file=sys.stderr)
        sys.exit(6)
