"""Prepare one reviewed provider executable; never invoke it or adopt PATH state.

The web/core caller owns consent, original-attempt identity and retry admission.
In particular, ``missing`` is an observation, not permission to replay an unknown
attempt. The caller creates the displayed fixed directory tree beforehand.
Only canonical verified bytes can prove completion after an unknown result.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import os
from pathlib import Path
import platform
import pwd
import secrets
import selectors
import ssl
import stat
import subprocess
import sys
import threading
import time
from typing import Literal


@dataclass(frozen=True)
class Artifact:
    platform: str
    version: str
    size: int
    sha256: str

    @property
    def path(self):
        return f"/claude-code-releases/{self.version}/{self.platform}/claude"

    @property
    def url(self):
        return "https://downloads.claude.ai" + self.path


# Official 2.1.325 manifest size/hash, independently matched to inspected npm
# package/claude bytes. The official install.sh grounds this versioned raw route.
# Additions require the same distribution/protocol inspection as provider_signin.
_ARTIFACTS = {
    "linux-x64": Artifact("linux-x64", "2.1.325", 365842960,
        "0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf"),
    "linux-arm64": Artifact("linux-arm64", "2.1.325", 364701112,
        "7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67"),
}
_SECONDS = 600
_CHUNK = 65536
_RELATIVE = ".local/share/claudlobby-browser/provider-bin/claude"


@dataclass(frozen=True)
class ProviderResult:
    state: Literal["ready", "missing", "blocked", "cancelled", "unknown"]
    reason: str
    binary: str | None = None
    sha256: str | None = None
    version: str | None = None
    platform: str | None = None


class _Refused(Exception):
    def __init__(self, reason, *, unknown=False):
        self.reason = reason
        self.unknown = unknown


def _artifact():
    if platform.system() != "Linux":
        raise _Refused("unsupported_platform")
    release = platform.freedesktop_os_release()
    if (release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04"
            or not (os.confstr("CS_GNU_LIBC_VERSION") or "").startswith("glibc ")):
        raise _Refused("unsupported_platform")
    key = {"x86_64": "linux-x64", "amd64": "linux-x64",
           "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(platform.machine())
    if key is None:
        raise _Refused("unsupported_platform")
    return _ARTIFACTS[key]


def _account(account, expected_uid):
    if (not isinstance(account, str) or not account or len(account) > 256
            or type(expected_uid) is not int or expected_uid <= 0
            or os.getuid() != expected_uid or os.geteuid() != expected_uid):
        raise _Refused("account_changed")
    entry = pwd.getpwnam(account)
    if entry.pw_name != account or entry.pw_uid != expected_uid:
        raise _Refused("account_changed")
    home = entry.pw_dir
    if not isinstance(home, str) or not home.startswith("/") or home == "/" or os.path.normpath(home) != home:
        raise _Refused("unsafe_path")
    return Path(home) / _RELATIVE


def _directory_node(info):
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


def _file_node(info):
    return (*_directory_node(info), info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


class _Directories:
    """Keep each existing component open; check every binding before publication."""
    def __init__(self, target, uid):
        self.entries = []
        self.uid = uid
        try:
            for component in ("/", *target.parent.parts[1:]):
                parent = self.entries[-1][1] if self.entries else None
                fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
                self.entries.append((component, fd, os.fstat(fd)))
            self.check()
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.entries[-1][1]

    def check(self):
        for index, (component, fd, original) in enumerate(self.entries):
            info = os.fstat(fd)
            parent = self.entries[index-1][1] if index else None
            bound = os.stat(component, dir_fd=parent, follow_symlinks=False)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, self.uid}
                    or info.st_mode & 0o022 and not sticky_root
                    or _directory_node(info) != _directory_node(original)
                    or _directory_node(info) != _directory_node(bound)):
                raise _Refused("unsafe_path")
        info = os.fstat(self.fd)
        if info.st_uid != self.uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise _Refused("unsafe_path")

    def close(self):
        for _, fd, _ in reversed(self.entries):
            os.close(fd)
        self.entries.clear()


def _checkpoint(deadline, cancel, clock):
    if cancel is not None and cancel.is_set():
        raise _Refused("cancelled")
    if clock() >= deadline:
        raise _Refused("deadline")


def _hash(fd, deadline, cancel, clock):
    os.lseek(fd, 0, os.SEEK_SET)
    hasher = hashlib.sha256()
    while True:
        _checkpoint(deadline, cancel, clock)
        data = os.read(fd, _CHUNK)
        if not data:
            return hasher.hexdigest()
        hasher.update(data)


def _inspect(directories, artifact, deadline, cancel, clock):
    directories.check()
    try:
        fd = os.open("claude", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directories.fd)
    except FileNotFoundError:
        # Retained unknown stages (and any unfamiliar content) need inspection.
        with os.scandir(directories.fd) as entries:
            if next(entries, None) is not None:
                raise _Refused("inspection_required")
        return False
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != directories.uid
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o500
                or before.st_size != artifact.size):
            raise _Refused("existing_file_changed")
        digest = _hash(fd, deadline, cancel, clock)
        if (digest != artifact.sha256 or _file_node(before) != _file_node(os.fstat(fd))
                or _file_node(before) != _file_node(os.stat("claude", dir_fd=directories.fd, follow_symlinks=False))):
            raise _Refused("existing_file_changed")
        directories.check()
        return True
    finally:
        os.close(fd)


def _worker_transfer(path, size, output):
    """Only the isolated child calls this; tests supply real HTTPResponse frames.

    http.client ignores proxy/netrc settings. The CA file is fixed to the supported
    Ubuntu system trust store; SSL_CERT_*, HOME and user configuration are unused.
    No redirect, compressed body, chunked body or manifest/latest request exists.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile="/etc/ssl/certs/ca-certificates.crt")
    connection = http.client.HTTPSConnection("downloads.claude.ai", timeout=10, context=context)
    try:
        connection.request("GET", path, headers={"Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        lengths = response.headers.get_all("Content-Length", [])
        if (response.status != 200 or lengths != [str(size)]
                or response.headers.get("Transfer-Encoding") is not None
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
            return False
        remaining = size
        while remaining:
            data = response.read(min(_CHUNK, remaining))
            if not data:
                return False
            output.write(data)
            output.flush()
            remaining -= len(data)
        return True
    finally:
        connection.close()


def _download(artifact, deadline, cancel, clock, *, spawn=subprocess.Popen):
    # -I -S ignore Python environment, user site and site initialization. The child loads only this
    # source-owned stdlib module and selects its own immutable artifact record.
    # It never receives an account, HOME, target path, credentials or writable FD.
    command = "import runpy; runpy.run_path(" + repr(str(Path(__file__).resolve())) + ", run_name='__provider_download__')"
    child = spawn([sys.executable, "-I", "-S", "-c", command, artifact.platform],
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                  env={}, close_fds=True, start_new_session=True)
    selector = None
    total = 0
    try:
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            _checkpoint(deadline, cancel, clock)
            if not selector.select(min(0.05, max(0.0, deadline-clock()))):
                continue
            data = os.read(child.stdout.fileno(), _CHUNK)
            if not data:
                break
            total += len(data)
            if total > artifact.size:
                raise _Refused("size_limit")
            yield data
        _checkpoint(deadline, cancel, clock)
        # stdout EOF need not mean child exit. Keep this wait bounded too.
        while child.poll() is None:
            _checkpoint(deadline, cancel, clock)
            try:
                child.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        if child.returncode != 0 or total != artifact.size:
            raise _Refused("download_failed")
    finally:
        try:
            if selector is not None:
                selector.close()
        finally:
            try:
                child.stdout.close()
            finally:
                try:
                    if child.poll() is None:
                        child.kill()  # actual owned child handle only; never a recorded PID
                    child.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    raise _Refused("download_uncertain", unknown=True) from None


def _cleanup_stage(directories, name, info):
    try:
        current = os.stat(name, dir_fd=directories.fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            os.unlink(name, dir_fd=directories.fd)
            os.fsync(directories.fd)
    except FileNotFoundError:
        pass


def _operate(account, expected_uid, *, write, cancel=None, downloader=_download, clock=time.monotonic):
    artifact = target = directories = stage_info = None
    stage_fd = None
    stage_name = None
    publication_possible = False
    result = None

    def answer(state, reason):
        return ProviderResult(state, reason, str(target) if target else None,
            artifact.sha256 if artifact else None, artifact.version if artifact else None,
            artifact.platform if artifact else None)

    try:
        deadline = clock() + _SECONDS
        artifact = _artifact()
        target = _account(account, expected_uid)
        _checkpoint(deadline, cancel, clock)
        directories = _Directories(target, expected_uid)
        if _inspect(directories, artifact, deadline, cancel, clock):
            return answer("ready", "already_present")
        if not write:
            return answer("missing", "not_present")
        _checkpoint(deadline, cancel, clock)
        stage_name = ".claude-stage-" + secrets.token_hex(24)
        stage_fd = os.open(stage_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=directories.fd)
        stage_info = os.fstat(stage_fd)
        if (not stat.S_ISREG(stage_info.st_mode) or stage_info.st_uid != expected_uid
                or stage_info.st_nlink != 1 or stat.S_IMODE(stage_info.st_mode) != 0o600):
            raise _Refused("unsafe_stage")
        hasher = hashlib.sha256()
        total = 0
        stream = downloader(artifact, deadline, cancel, clock)
        try:
            for data in stream:
                _checkpoint(deadline, cancel, clock)
                if not isinstance(data, bytes) or not data or len(data) > _CHUNK:
                    raise _Refused("download_failed")
                total += len(data)
                if total > artifact.size:
                    raise _Refused("size_limit")
                hasher.update(data)
                pending = memoryview(data)
                while pending:
                    _checkpoint(deadline, cancel, clock)
                    count = os.write(stage_fd, pending)
                    if count <= 0:
                        raise _Refused("write_failed")
                    pending = pending[count:]
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if total != artifact.size or hasher.hexdigest() != artifact.sha256:
            raise _Refused("artifact_mismatch")
        os.fsync(stage_fd)
        before = os.fstat(stage_fd)
        if (_file_node(before) != _file_node(os.stat(stage_name, dir_fd=directories.fd, follow_symlinks=False))
                or before.st_nlink != 1 or before.st_size != artifact.size
                or _hash(stage_fd, deadline, cancel, clock) != artifact.sha256
                or _file_node(before) != _file_node(os.fstat(stage_fd))):
            raise _Refused("unsafe_stage")
        os.fchmod(stage_fd, 0o500)
        os.fsync(stage_fd)
        directories.check()
        _checkpoint(deadline, cancel, clock)
        staged = os.fstat(stage_fd)
        if (staged.st_uid != expected_uid or staged.st_nlink != 1
                or stat.S_IMODE(staged.st_mode) != 0o500 or staged.st_size != artifact.size
                or _file_node(staged) != _file_node(os.stat(stage_name, dir_fd=directories.fd, follow_symlinks=False))):
            raise _Refused("unsafe_stage")
        # Hard-link publication is create-only. The staging link is then removed;
        # canonical content is never overwritten, chmodded, repaired or deleted.
        publication_possible = True
        try:
            os.link(stage_name, "claude", src_dir_fd=directories.fd,
                    dst_dir_fd=directories.fd, follow_symlinks=False)
        except FileExistsError:
            publication_possible = False
            raise _Refused("target_changed") from None
        _checkpoint(deadline, cancel, clock)
        directories.check()
        canonical = os.stat("claude", dir_fd=directories.fd, follow_symlinks=False)
        if (_file_node(canonical) != _file_node(os.fstat(stage_fd))
                or canonical.st_nlink != 2):
            raise _Refused("publication_uncertain", unknown=True)
        _cleanup_stage(directories, stage_name, stage_info)
        stage_name = None
        os.fsync(directories.fd)
        if not _inspect(directories, artifact, deadline, cancel, clock):
            raise _Refused("publication_uncertain", unknown=True)
        result = answer("ready", "prepared")
    except _Refused as exc:
        if publication_possible:
            result = answer("unknown", "publication_uncertain")
        else:
            result = answer("unknown" if exc.unknown else "cancelled" if exc.reason == "cancelled" else "blocked", exc.reason)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result = answer("unknown" if publication_possible else "blocked",
                        "publication_uncertain" if publication_possible else "unsafe_path" if directories is None else "operation_failed")
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if directories is not None:
            try:
                if stage_name is not None and stage_info is not None:
                    _cleanup_stage(directories, stage_name, stage_info)
            except OSError:
                result = answer("unknown", "cleanup_uncertain")
            finally:
                directories.close()
    return result


def status(account: str, expected_uid: int) -> ProviderResult:
    """Read-only local bytes/status. Never create, download, execute or repair."""
    return _operate(account, expected_uid, write=False)


def _prepare(account, expected_uid, *, cancel=None, downloader=_download, clock=time.monotonic):
    return _operate(account, expected_uid, write=True, cancel=cancel, downloader=downloader, clock=clock)


def prepare(account: str, expected_uid: int, *, cancel: threading.Event | None = None) -> ProviderResult:
    """One caller-approved attempt, no retry. Existing safe exact bytes are a no-op."""
    return _prepare(account, expected_uid, cancel=cancel)


if __name__ == "__provider_download__":
    try:
        selected = _ARTIFACTS[sys.argv[1]]
        success = _worker_transfer(selected.path, selected.size, sys.stdout.buffer)
    except Exception:
        success = False  # no provider/network exception or diagnostic output
    sys.exit(0 if success else 1)
