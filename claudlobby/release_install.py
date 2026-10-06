"""Offline assembly at a release's final path; never select or activate it.

Inputs are trusted operator-supplied wheels and a hash-locked requirements file.
Only a complete lock of pinned wheel requirements is supported: URLs, nested
files and pip options add sources outside that explicit offline boundary.
A copied interpreter still uses its host stdlib and other system libraries;
this is not OS hermeticity.
Failures retain the owned incomplete directory for inspection, without a seal.
"""

from __future__ import annotations

import configparser
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import shlex
import signal
import stat
import subprocess
import tempfile
import zipfile

from .releases import (Compatibility, InterpreterIdentity, ReleaseError,
                       ReleaseInputs, ReleaseManifest, ReleasePaths,
                       read_release, release_path, seal_release)


_INTERPRETER_QUERY = """
import json, sys, sysconfig
library = sysconfig.get_config_var('LDLIBRARY')
library_dir = sysconfig.get_config_var('LIBDIR')
print(json.dumps(dict(implementation=sys.implementation.name, version=sys.version,
    platform=':'.join((sys.platform, sysconfig.get_platform(),
                      sysconfig.get_config_var('SOABI') or '')),
    shared_library=(str(library_dir) + '/' + str(library))
        if sys.platform == 'darwin' and library_dir and library and library.endswith('.dylib')
        else None)))
"""
_SITE_QUERY = "import sysconfig; print(sysconfig.get_path('purelib'))"


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: int = 300) -> str:
    """Bound the whole local subprocess group, including ensurepip's child."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise ReleaseError(f"release assembly subprocess timed out: {argv[0]}") from exc
    if process.returncode:
        raise ReleaseError(f"release assembly subprocess failed ({process.returncode}): "
                           f"{stderr.strip() or stdout.strip()}")
    return stdout


def _check_lock(lock: bytes) -> None:
    """Keep pip's accepted sources local; pip owns version/marker/hash parsing."""
    try:
        text = lock.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError("dependency lock must be UTF-8 requirements text") from exc
    if not lock or "\x00" in text:
        raise ReleaseError("dependency lock is empty or invalid")
    text = re.sub(r"\\\r?\n", " ", text)
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        hashes = re.findall(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", line)
        requirement = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?"
            r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;\s*[^\r\n]+)?", requirement)
        if (not hashes or not match or any(value in requirement for value in
                                          ("--", "/", "\\", "@"))):
            raise ReleaseError("dependency lock requires pinned names with SHA-256 hashes; "
                               "URLs, includes and pip options are unsupported")
        if re.sub(r"[-_.]+", "-", match[1]).lower() == "claudlobby":
            raise ReleaseError("dependency lock must not install the candidate claudlobby wheel")


def _wheel_contents(wheel: Path, content: bytes) -> tuple[dict, dict[str, tuple[bytes, int]]]:
    """Validate the candidate carrier and retain exact package bytes for comparison."""
    if wheel.suffix != ".whl":
        raise ReleaseError("candidate must be a local wheel")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = {}
            for entry in archive.infolist():
                name = entry.filename
                path = PurePosixPath(name)
                mode = entry.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or str(path) != name.rstrip("/")
                        or "\\" in name or name in files or stat.S_ISLNK(mode)
                        or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}):
                    raise ReleaseError(f"unsupported candidate wheel path: {name}")
                if entry.is_dir():
                    continue
                files[name] = (archive.read(entry), mode & 0o111)
        metadata_files = [name for name in files if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ReleaseError("candidate wheel needs one distribution metadata record")
        dist = metadata_files[0].split("/", 1)[0]
        metadata = BytesParser().parsebytes(files[metadata_files[0]][0])
        if metadata["Name"] != "claudlobby" or any(
            name.split("/", 1)[0] not in {"claudlobby", dist} for name in files
        ):
            raise ReleaseError("candidate wheel must contain only claudlobby and its metadata")
        entrypoints = configparser.ConfigParser()
        entrypoints.read_string(files[f"{dist}/entry_points.txt"][0].decode())
        if entrypoints.get("console_scripts", "claudlobby") != "claudlobby.__main__:main":
            raise ReleaseError("candidate wheel has no canonical claudlobby entrypoint")
        artifact = json.loads(files["claudlobby/_artifact.json"][0])
        if (type(artifact.get("schema")) is not int or artifact["schema"] != 1
                or not {"source_revision", "artifact_id", "content_sha256"} <= artifact.keys()):
            raise ReleaseError("candidate wheel has unsupported artifact metadata")
        Compatibility.from_dict(artifact.get("compatibility"))
        package = {name: value for name, value in files.items() if name.startswith("claudlobby/")}
        for prefix in ("_runtime_scripts/", "_resources/library/", "_resources/library/voices/",
                       "_resources/templates/", "_resources/seeds/"):
            if not any(name.startswith("claudlobby/" + prefix) for name in package):
                raise ReleaseError(f"candidate wheel is missing packaged {prefix}")
        if "claudlobby/system.yaml" not in package:
            raise ReleaseError("candidate wheel is missing system.yaml")
        return artifact, package
    except (OSError, ValueError, KeyError, AttributeError, zipfile.BadZipFile,
            configparser.Error) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"invalid candidate wheel: {exc}") from exc


def _remove_owned_bytecode(venv: Path) -> None:
    """Remove ensurepip/pip caches only inside this newly created environment."""
    for parent, dirs, files in os.walk(venv, followlinks=False):
        for name in list(dirs) + files:
            path = Path(parent) / name
            if name != "__pycache__" and path.suffix not in {".pyc", ".pyo"}:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(venv):
                raise ReleaseError("refusing redirected bytecode cleanup")
            if path.is_dir():
                shutil.rmtree(path)
                dirs.remove(name)
            else:
                path.unlink()


def _verify_package(site: Path, expected: dict[str, tuple[bytes, int]]) -> None:
    for relative, (content, mode) in expected.items():
        path = site / relative
        if (not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(site)
                or path.read_bytes() != content or path.stat().st_mode & 0o111 != mode):
            raise ReleaseError(f"installed candidate differs from wheel: {relative}")
    actual = {path.relative_to(site).as_posix() for path in (site / "claudlobby").rglob("*")
              if not path.is_dir()}
    if actual != set(expected):
        raise ReleaseError("installed candidate contains unexpected package files")


def _write_cli(cli: Path, interpreter: Path) -> None:
    # One installed CLI, with -B effective even without a parent environment.
    # A shell exec also supports spaces and paths beyond kernel shebang limits.
    if (not cli.is_file() or cli.is_symlink()
            or not cli.resolve().is_relative_to(interpreter.parent.parent)):
        raise ReleaseError("refusing missing or redirected installed CLI")
    bootstrap = ("import sys; sys.argv[0] = sys.argv.pop(1); "
                 "from claudlobby.__main__ import main; raise SystemExit(main())")
    cli.write_text(f"#!/bin/sh\nexec {shlex.quote(str(interpreter))} -I -B -c "
                   f"{shlex.quote(bootstrap)} \"$0\" \"$@\"\n")
    cli.chmod(0o755)


def assemble_release(data_root: Path, wheel: Path, dependency_lock: Path,
                     wheelhouse: Path, interpreter: Path) -> ReleaseManifest:
    """Assemble offline, or verify an already sealed identical assembly.

    Never repair, delete or overwrite an existing release directory. Every venv
    is born at its final canonical path so generated entrypoints remain valid.
    """
    wheel = Path(wheel).expanduser().resolve(strict=True)
    wheelhouse = Path(wheelhouse).expanduser().resolve(strict=True)
    interpreter = Path(interpreter).expanduser().resolve(strict=True)
    if not wheelhouse.is_dir() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ReleaseError("wheelhouse directory and executable interpreter are required")
    if any(path.is_symlink() or not path.is_file() or path.suffix != ".whl"
           for path in wheelhouse.iterdir()):
        raise ReleaseError("offline wheelhouse must contain only regular local .whl files")
    wheel_bytes = wheel.read_bytes()
    lock = Path(dependency_lock).expanduser().read_bytes()
    _check_lock(lock)
    artifact, package = _wheel_contents(wheel, wheel_bytes)
    compatibility = Compatibility.from_dict(artifact["compatibility"])
    interpreter_hash = hashlib.sha256(interpreter.read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="claudlobby-assemble-") as temporary:
        work = Path(temporary).resolve()
        for name in ("home", "tmp", "cache"):
            (work / name).mkdir()
        env = {"HOME": str(work / "home"), "TMPDIR": str(work / "tmp"),
               "XDG_CACHE_HOME": str(work / "cache"),
               "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
               "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PLANE_EMIT_DISABLED": "1",
               "PYTHONDONTWRITEBYTECODE": "1"}
        identity = json.loads(_run([str(interpreter), "-I", "-S", "-B", "-c",
                                    _INTERPRETER_QUERY], env, work, timeout=15))
        shared_library = identity.pop("shared_library", None)
        inputs = ReleaseInputs.from_lock(
            source_revision=artifact["source_revision"], artifact_id=artifact["artifact_id"],
            artifact_sha256=artifact["content_sha256"],
            wheel_sha256=hashlib.sha256(wheel_bytes).hexdigest(), dependency_lock=lock,
            interpreter=InterpreterIdentity(**identity, sha256=interpreter_hash))
        directory = release_path(data_root, inputs.release_id)
        if directory.exists():
            if not (directory / "release.json").is_file():
                raise ReleaseError(f"incomplete release retained; refusing to overwrite: {directory}")
            existing = read_release(data_root, inputs.release_id)
            if existing.inputs != inputs or existing.compatibility != compatibility:
                raise ReleaseError("sealed release differs from requested assembly or compatibility")
            return existing
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()  # exclusive ownership; a competing assembly refuses
        except FileExistsError as exc:
            raise ReleaseError(f"release directory already exists; refusing to overwrite: {directory}") from exc
        try:
            artifacts = directory / "artifacts"
            artifacts.mkdir()
            installed_wheel = artifacts / wheel.name
            installed_wheel.write_bytes(wheel_bytes)
            installed_lock = directory / "dependency.lock"
            installed_lock.write_bytes(lock)
            venv = directory / "venv"
            # Some macOS CPython builds resolve @rpath/libpython from
            # @executable_path/../lib.
            # venv --copies copies the executable but not that library. Create
            # without pip, copy the library into the owned release, then run
            # ensurepip through the copied interpreter.
            if shared_library:
                source_library = Path(shared_library)
                if not source_library.is_file():
                    raise ReleaseError(f"interpreter shared library is missing: {source_library}")
                _run([str(interpreter), "-I", "-B", "-m", "venv", "--copies",
                      "--without-pip", str(venv)], env, work)
                shutil.copy2(source_library, venv / "lib" / source_library.name)
                python = venv / "bin/python"
                _run([str(python), "-I", "-B", "-m", "ensurepip", "--upgrade"], env, work)
            else:
                _run([str(interpreter), "-I", "-B", "-m", "venv", "--copies", str(venv)], env, work)
            python = venv / "bin/python"
            pip = [str(python), "-I", "-B", "-m", "pip", "--isolated",
                   "--disable-pip-version-check", "--no-input", "--no-cache-dir", "install",
                   "--no-compile", "--no-index", "--only-binary=:all:"]
            # Resolve no dependency metadata: even --no-index permits a
            # Requires-Dist direct URL. The supplied lock must list the closure.
            _run(pip + ["--require-hashes", "--no-deps", "--find-links", str(wheelhouse),
                        "-r", str(installed_lock)], env, work)
            _run(pip + ["--no-deps", str(installed_wheel)], env, work)
            _run([str(python), "-I", "-B", "-m", "pip", "--isolated",
                  "--disable-pip-version-check", "check"], env, work)
            site = Path(_run([str(python), "-I", "-B", "-c", _SITE_QUERY], env, work,
                             timeout=15).strip()).resolve(strict=True)
            if not site.is_relative_to(venv):
                raise ReleaseError("installed package directory escapes release environment")
            _remove_owned_bytecode(venv)
            _verify_package(site, package)
            _write_cli(venv / "bin/claudlobby", python)
            paths = ReleasePaths(
                interpreter=python.relative_to(directory).as_posix(),
                cli=(venv / "bin/claudlobby").relative_to(directory).as_posix(),
                native=(site / "claudlobby/_runtime_scripts").relative_to(directory).as_posix(),
                artifact=(site / "claudlobby/_artifact.json").relative_to(directory).as_posix(),
                wheel=installed_wheel.relative_to(directory).as_posix(),
                dependency_lock="dependency.lock")
            return seal_release(data_root, inputs, paths)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ReleaseError(f"assembly failed; incomplete release retained at {directory}: {exc}") from exc
