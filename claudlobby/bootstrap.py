"""First command for a trusted, unpacked release bundle; stdlib only.

Checksums establish local integrity, not publisher authenticity. This launcher
installs a temporary matching CLI, then delegates to canonical host setup. It
never selects a release or activates a fleet. Bundles target CPython 3.11 on
Linux aarch64 or macOS arm64; the host's interpreter/stdlib remain prerequisites.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile

_MAX_FILE = 256 * 1024 * 1024
_MAX_TOTAL = 1024 * 1024 * 1024
_MAX_FILES = 512
_FIXED = {"bootstrap.py", "dependency.lock", "rehearsal.json", "built-on.txt"}


class BundleError(ValueError):
    """A fixed, safe operator-facing refusal."""


def _regular(path: Path) -> int:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE:
        raise BundleError("bundle contains a redirected, non-regular or oversized file")
    return info.st_size


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lock(content: bytes) -> None:
    # Bootstrap pip must obey the same local-only boundary as the assembler,
    # before the matching installed release_install module is available.
    text = content.decode("utf-8")
    if not text or "\0" in text:
        raise BundleError("bundle dependency lock is empty or invalid")
    for raw in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        hashes = re.findall(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", line)
        requirement = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}(?=\s|$)", "", line)
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?"
                             r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;\s*[^\r\n]+)?", requirement)
        if (not match or not hashes or any(value in requirement for value in ("--", "/", "\\", "@"))
                or re.sub(r"[-_.]+", "-", match[1]).lower() == "claudlobby"):
            raise BundleError("bundle lock requires pinned local dependencies with SHA-256 hashes")


def _platform(built_on: str) -> None:
    lines = dict(line.split(" ", 1) for line in built_on.splitlines())
    target = ("linux-aarch64" if sys.platform == "linux" and platform.machine() == "aarch64"
              else "macos-arm64" if sys.platform == "darwin" and platform.machine() == "arm64" else None)
    recorded = lines.get("platform", "")
    if (sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11)
            or not lines.get("python", "").startswith("3.11.")
            or target is None or (target == "linux-aarch64" and recorded != "linux-aarch64")
            or (target == "macos-arm64" and not re.fullmatch(r"macosx-[0-9.]+-arm64", recorded))):
        raise BundleError("bundle requires matching Linux aarch64 or macOS arm64 CPython 3.11; wheel/libc compatibility remains host-specific")


def _bundle(script: Path) -> tuple[Path, Path, dict[str, str]]:
    if not script.is_absolute() or script != script.resolve(strict=True):
        raise BundleError("run the real bootstrap.py from an absolute, unredirected bundle path")
    directory = script.parent
    entries = list(directory.iterdir())
    if len(entries) > 8:
        raise BundleError("unexpected bundle inventory")
    wheelhouse = directory / "wheelhouse"
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise BundleError("bundle needs an unredirected wheelhouse directory")
    wheels = list(wheelhouse.iterdir())
    if not wheels or len(wheels) > _MAX_FILES - 5:
        raise BundleError("bundle wheelhouse is empty or oversized")
    root_wheels = [path for path in entries if path.suffix == ".whl"]
    if len(root_wheels) != 1 or {p.name for p in entries} != _FIXED | {"SHA256SUMS", "wheelhouse", root_wheels[0].name}:
        raise BundleError("bundle needs one candidate wheel and the exact supported inventory")
    paths = [p for p in entries if p != wheelhouse] + wheels
    if any(p.parent == wheelhouse and p.suffix != ".whl" for p in paths):
        raise BundleError("wheelhouse may contain only regular local wheels")
    if sum(_regular(p) for p in paths) > _MAX_TOTAL:
        raise BundleError("bundle is oversized")
    sums = directory / "SHA256SUMS"
    if sums.stat().st_size > 128 * 1024:
        raise BundleError("checksum inventory is oversized")
    checksums = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (?:\./)?([A-Za-z0-9_./+-]+)", line)
        if not match:
            raise BundleError("invalid checksum inventory")
        name = match[2]
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or str(relative) != name
                or name in checksums or name == "SHA256SUMS"):
            raise BundleError("invalid checksum path or duplicate")
        checksums[name] = match[1]
    actual = {p.relative_to(directory).as_posix() for p in paths if p != sums}
    if set(checksums) != actual or any(_digest(directory / name) != value for name, value in checksums.items()):
        raise BundleError("bundle checksum inventory or bytes differ")
    wheel = root_wheels[0]
    if (directory / "rehearsal.json").stat().st_size > 128 * 1024:
        raise BundleError("bundle rehearsal metadata is oversized")
    rehearsal = json.loads((directory / "rehearsal.json").read_bytes())
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        required = {"claudlobby/bootstrap.py", "claudlobby/_artifact.json"}
        if any(names.count(name) != 1 for name in required) or any(archive.getinfo(name).file_size > 128 * 1024 for name in required):
            raise BundleError("candidate lacks bounded unique bootstrap identity")
        if archive.read("claudlobby/bootstrap.py") != script.read_bytes():
            raise BundleError("bootstrap.py differs from the candidate module")
        artifact = json.loads(archive.read("claudlobby/_artifact.json"))
    revision = artifact.get("source_revision") if isinstance(artifact, dict) else None
    if (not isinstance(rehearsal, dict) or rehearsal.get("passed") is not True
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
            or rehearsal.get("source_revision") != revision or rehearsal.get("wheel_sha256") != checksums[wheel.name]):
        raise BundleError("candidate and successful rehearsal identity differ")
    _lock((directory / "dependency.lock").read_bytes())
    _platform((directory / "built-on.txt").read_text(encoding="utf-8"))
    return directory, wheel, checksums


def _run(command: list[str], env: dict[str, str], *, timeout: int = 300) -> None:
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode:
        raise BundleError("offline bootstrap installation failed; check host Python/venv and bundle wheel compatibility")


def bootstrap(script: Path, root: Path, *, json_output: bool = False) -> int:
    if not root.is_absolute() or root.is_symlink():
        raise BundleError("--root must be an explicit absolute, real host data path")
    directory, wheel, checksums = _bundle(script)
    interpreter = Path(sys.executable).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="claudlobby-bootstrap-") as temporary:
        work = Path(temporary).resolve()
        inputs = work / "bundle"
        inputs.mkdir()
        # Install and assemble from an owned snapshot; a changed input refuses.
        for name, expected in checksums.items():
            source = directory / name
            _regular(source)
            target = inputs / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if _digest(target) != expected:
                raise BundleError("bundle changed while snapshotting")
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
               "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
               "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME"):
            p = work / key.lower()
            p.mkdir()
            env[key] = str(p)
        venv = work / "venv"
        _run([str(interpreter), "-I", "-B", "-m", "venv", str(venv)], env)
        python = venv / "bin/python"
        pip = [str(python), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
               "--no-input", "--no-cache-dir", "install", "--no-compile", "--no-index", "--only-binary=:all:"]
        _run(pip + ["--require-hashes", "--no-deps", "--find-links", str(inputs / "wheelhouse"),
                    "-r", str(inputs / "dependency.lock")], env)
        _run(pip + ["--no-deps", str(inputs / wheel.name)], env)
        _run([str(python), "-I", "-B", "-m", "pip", "--isolated", "check"], env)
        # Preserve native user-domain inputs, never ambient Python/pip injection
        # or credential variables. Canonical setup owns its subprocess budgets.
        host_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "DBUS_SESSION_BUS_ADDRESS"}}
        command = [str(python), "-I", "-B", "-m", "claudlobby", "--root", str(root), "host", "setup",
                   "--wheel", str(inputs / wheel.name), "--dependency-lock", str(inputs / "dependency.lock"),
                   "--wheelhouse", str(inputs / "wheelhouse"), "--interpreter", str(interpreter)]
        if json_output:
            command.append("--json")
        process = subprocess.Popen(command, env=host_env, start_new_session=True)
        try:
            return process.wait()
        finally:
            # The canonical assembler owns already-started local work. Keep
            # its input snapshot alive through a caller interrupt, too.
            if process.poll() is None:
                process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return bootstrap(Path(__file__).absolute(), args.root, json_output=args.json)
    except (BundleError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        message = str(exc) if isinstance(exc, BundleError) else "bundle is unreadable or malformed; inspect trusted local inputs and host Python"
        if args.json:
            print(json.dumps({"ok": False, "error": {"code": "invalid_bundle", "message": message}}))
        else:
            print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
