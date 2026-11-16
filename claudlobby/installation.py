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
import platform
import pwd
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time


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


_DARWIN_RUNTIME_SOURCE = r'''
# BEGIN CANONICAL DARWIN RUNTIME PYTHON
"""One source-pinned Apple Silicon macOS15 tool profile; no package manager.

Read-only admission is shared with the preinstall entry through exact embedding.
Only the explicit preinstall prepare/discard operations write the fixed namespace.
Installation schema1, services, provider profiles and personal tools are untouched.
"""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import posixpath
import pwd
import re
import shutil
import ssl
import stat
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse
import uuid

PROFILE_ID = "darwin15-arm64-py31216-git2561-tmux38-jq182-v1"
LIMIT = 16384
BUDGET = 900
_INHERITED_LOCK_FD = None
ALIASES = {"python3": "../python/bin/python3.12", "git": "../git/bin/git",
           "tmux": "../tmux/bin/tmux", "jq": "../jq/bin/jq"}
CA_MEMBER = "lib/python3.12/site-packages/pip/_vendor/certifi/cacert.pem"
# BEGIN FIXED DESCRIPTOR
_DESCRIPTOR = {
    "artifacts": {
        "python": {
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-install_only.tar.gz",
            "sha256": "e956e60bc9465d577ef8062059aa05cd3a2665b4b8678a2ed24e432368330b8f",
            "bytes": 25371130,
            "members": 1700,
            "unpacked_bytes": 66987437,
            "members_sha256": "75d048f97433b041ef51659aa5a2c0545ff440f6143588bdbde2a680a6770887",
            "tree_sha256": "03ae59f570105cc7292b30cdeff01877747cee805513b24296d0faa003738a46"
        },
        "git": {
            "url": "https://github.com/desktop/dugite-native/releases/download/v2.56.1/dugite-native-v2.56.1-238326e-macOS-arm64.tar.gz",
            "sha256": "a882ec6882416b57d212a22bac845ae27944f96cf73453a864f6a12c17863a79",
            "bytes": 63505330,
            "members": 488,
            "unpacked_bytes": 157153052,
            "members_sha256": "6f1be15ddb8afc34b88c6159281e7c93b7f5eb258a43d23307f86c72d6b9801b",
            "tree_sha256": "fcabc6bc008eceb0afcc3b168f678590f9f168acb506cbd90093e2867800e5bf"
        },
        "tmux": {
            "url": "https://github.com/tmux/tmux-builds/releases/download/v3.8/tmux-3.8-macos-arm64.tar.gz",
            "sha256": "ec926bbf28ddf55d0dc7c9791d43e03e251ddab392d763f7fbb38d4b8408d051",
            "bytes": 725788,
            "members": 1,
            "unpacked_bytes": 1877512,
            "members_sha256": "0281fcbee97a4faef725d304ab271c61e0ba0c82d8d5b343fb44f5557288973b",
            "tree_sha256": "b3217e73158027cf09d6ca3ecfc1a217bdb90b967d6fd06209ccb25c8d0ac843"
        },
        "jq": {
            "url": "https://github.com/jqlang/jq/releases/download/jq-1.8.2/jq-macos-arm64",
            "sha256": "2d75340ba57a4b4b4c8708a21c2dc8e958a48aaa8bba13b27f77f6e4c0eca07e",
            "bytes": 841504,
            "members": 0,
            "unpacked_bytes": 0,
            "members_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "tree_sha256": "260946517edd7da43b09a0a1473a828196af646613914a04c7840439115d4940"
        }
    },
    "python_executable_sha256": "54788b15120aa4f534da3f59b59f46658a9bfdc3d3d9c8b3af1459002e307c90",
    "ca_sha256": "2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b"
}
# END FIXED DESCRIPTOR


class RuntimeRefusal(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(value, code):
    if not value:
        raise RuntimeRefusal(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _policy():
    return _sha(_canonical({"profile_id": PROFILE_ID, "architecture": "arm64", "minimum_macos": 15,
                           "descriptor": _DESCRIPTOR, "aliases": ALIASES, "file_modes": [0o555, 0o444], "profile_mode": 0o700,
                           "git_helpers": "builtin-links-http-notice-closed-v2", "bytecode_writes": False, "receipt_schema": 1,
                           "root": "runtime/profile", "paths": ["python/bin/python3.12", "git/bin/git", "git/libexec/git-core", "tmux/bin/tmux", "jq/bin/jq", "ca-bundle.pem"]}))


@dataclass(frozen=True)
class RuntimeProfile:
    uid: int
    account: str
    home: Path
    root: Path
    bin: Path
    python: Path
    git: Path
    git_exec_path: Path
    tmux: Path
    jq: Path
    ca_bundle: Path
    policy_sha256: str


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_request(operation, value):
    _require(operation in ("prepare", "status", "discard"), "invalid_operation")
    _require(type(value) is dict and set(value) == {"schema_version", "request_id", "expected_uid", "expected_account"}, "invalid_request")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1 and _uuid(value["request_id"]), "invalid_request")
    _require(type(value["expected_uid"]) is int and 0 < value["expected_uid"] < 2**32 - 1, "invalid_request")
    _require(type(value["expected_account"]) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9._-]{0,63}", value["expected_account"]), "invalid_request")
    return value


def _parents(path, uid):
    for parent in reversed(path.parents):
        info = parent.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid) and not info.st_mode & 0o022, "unsafe_ancestry")


def _identity(*, expected_uid=None, expected_account=None):
    _require(platform.system() == "Darwin" and platform.machine() == "arm64", "unsupported_native_architecture")
    release = platform.mac_ver()[0].split(".")
    _require(release and release[0].isdigit() and int(release[0]) >= 15, "macos15_required")
    uid = os.getuid()
    _require(uid > 0 and uid == os.geteuid() and os.getgid() == os.getegid(), "ordinary_account_required")
    account = pwd.getpwuid(uid)
    _require((expected_uid is None or uid == expected_uid) and
             (expected_account is None or account.pw_name == expected_account), "account_changed")
    home = Path(account.pw_dir)
    _require(home.is_absolute() and str(home) != "/" and str(home.resolve(strict=True)) == str(home)
             and re.fullmatch(r"/[A-Za-z0-9_./-]+", str(home)), "unsupported_account_home")
    _parents(home, uid)
    info = home.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022, "unsafe_account_home")
    return RuntimeProfile(uid, account.pw_name, home, home, home, home, home, home, home, home, home, _policy())


def _base(identity):
    return identity.home / ".local/share/claudlobby-browser/runtime"


def _profile(identity):
    root = _base(identity) / "profile"
    return RuntimeProfile(identity.uid, identity.account, identity.home, root, root / "bin",
                          root / "python/bin/python3.12", root / "git/bin/git", root / "git/libexec/git-core",
                          root / "tmux/bin/tmux", root / "jq/bin/jq", root / "ca-bundle.pem", _policy())


def _directory(path, uid, mode=None, create=False):
    if create:
        try:
            path.mkdir(mode=mode or 0o700)
            os.chmod(path, mode or 0o700, follow_symlinks=False)
        except FileExistsError:
            pass
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o022
             and (mode is None or stat.S_IMODE(info.st_mode) == mode), "unsafe_directory")
    return info


def _read(path, uid, limit=LIMIT, mode=0o600):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "unsafe_file")
        raw = os.read(fd, limit + 1)
        _require(len(raw) <= limit, "oversize_file")
        return raw
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_field")
        result[key] = value
    return result


def _load(path, uid, *, mode=0o600):
    try:
        return json.loads(_read(path, uid, mode=mode), object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise RuntimeRefusal("invalid_retained_metadata") from None


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save(path, value, uid, *, new=False):
    raw = _canonical(value)
    _require(len(raw) <= LIMIT, "metadata_oversize")
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
    else:
        _read(path, uid)
        fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    _sync(path.parent)


def _binding(identity):
    return {"uid": identity.uid, "account": identity.account, "home": str(identity.home), "arch": "arm64",
            "profile_id": PROFILE_ID, "policy_sha256": _policy()}


def _tree(path, uid, *, staged=False):
    _directory(path, uid, 0o755 if staged else 0o555)
    records = []
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in sorted(directories + files):
            child = Path(directory) / name
            relative = child.relative_to(path).as_posix(); info = child.lstat()
            _require(info.st_uid == uid, "foreign_tool_tree")
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(child)
                _safe_link(relative, target)
                records.append({"name": relative, "type": "symlink", "link": target})
                if name in directories:
                    directories.remove(name)
            elif stat.S_ISDIR(info.st_mode):
                _require(stat.S_IMODE(info.st_mode) == (0o755 if staged else 0o555), "unsafe_tool_mode")
                records.append({"name": relative, "type": "directory", "mode": 0o555})
            else:
                mode = stat.S_IMODE(info.st_mode)
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and mode in ((0o644, 0o755) if staged else (0o444, 0o555)), "unsafe_tool_file")
                raw = _read(child, uid, limit=128 * 1024 * 1024, mode=mode)
                records.append({"name": relative, "type": "file", "mode": mode & ~0o222, "sha256": _sha(raw), "bytes": len(raw)})
    return _sha(_canonical(sorted(records, key=lambda row: row["name"])))


def _safe_name(name):
    _require(name and not name.startswith("/") and "\\" not in name and
             all(part not in ("", ".", "..") for part in name.split("/")), "unsafe_archive_name")
    return name


def _safe_link(name, target):
    _require(target and not target.startswith("/") and "\\" not in target, "unsafe_archive_link")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    _require(resolved not in (".", "..") and not resolved.startswith("../"), "unsafe_archive_link")
    return resolved


def _selection(member, kind):
    name = member.name.removeprefix("./").rstrip("/")
    if kind == "python":
        return name[7:] if name.startswith("python/") else None
    if kind == "tmux":
        return "bin/tmux" if name == "tmux" else None
    if kind == "git":
        if name in ("bin/git", "libexec/git-core/git", "libexec/git-core/git-remote-http", "libexec/git-core/NOTICE", "COPYING", "LICENSE", "LICENSE.txt") or name.startswith("share/git-core/templates/"):
            return name
        if name.startswith("libexec/git-core/") and member.issym() and member.linkname in ("git", "../../bin/git", "git-remote-http"):
            return name
    return None


def _inventory(archive, descriptor):
    members = archive.getmembers(); records = []; names = set(); links = {}
    _require(len(members) <= descriptor["members"] + 1, "archive_member_drift")
    for member in members:
        name = member.name.removeprefix("./").rstrip("/")
        if name in ("", "."):
            _require(member.isdir(), "unsafe_archive_name"); continue
        _safe_name(name)
        _require(name not in names and not member.mode & 0o7000, "archive_member_drift")
        names.add(name)
        kind = "directory" if member.isdir() else "file" if member.isfile() else "symlink" if member.issym() else None
        _require(kind is not None, "unsafe_archive_type")
        if member.issym():
            links[name] = _safe_link(name, member.linkname)
        records.append({"name": name, "type": kind, "mode": member.mode, "bytes": member.size, "link": member.linkname})
    _require(len(records) == descriptor["members"] and sum(row["bytes"] for row in records) == descriptor["unpacked_bytes"]
             and _sha(_canonical(sorted(records, key=lambda row: row["name"]))) == descriptor["members_sha256"], "archive_member_drift")
    for name in names:
        _require(not any(str(parent) in links for parent in PurePosixPath(name).parents), "archive_symlink_parent")
    for name in links:
        seen = {name}; target = links[name]
        while target in links:
            _require(target not in seen, "archive_link_cycle"); seen.add(target); target = links[target]
        _require(target in names, "archive_link_missing")
    return members


def _extract(path, destination, kind, descriptor, uid):
    _require(_sha(_read(path, uid, descriptor["bytes"])) == descriptor["sha256"], "artifact_mismatch")
    _directory(destination, uid, 0o755, create=True)
    with tarfile.open(path, mode="r:gz") as archive:
        members = _inventory(archive, descriptor)
        selected = {_selection(member, kind): member for member in members if _selection(member, kind)}
        for name, member in selected.items():
            if not member.issym():
                continue
            seen = {name}; target = _safe_link(name, member.linkname)
            while target in selected and selected[target].issym():
                _require(target not in seen, "archive_link_cycle")
                seen.add(target); target = _safe_link(target, selected[target].linkname)
            _require(target in selected, "selected_archive_link_missing")
        links = []
        for member in members:
            relative = _selection(member, kind)
            if not relative:
                continue
            _safe_name(relative); target = destination / relative
            for parent in reversed(target.relative_to(destination).parents):
                if str(parent) != ".":
                    _directory(destination / parent, uid, 0o755, create=True)
            if member.isdir():
                _directory(target, uid, 0o755, create=True)
            elif member.issym():
                _safe_link(relative, member.linkname); links.append((target, member.linkname))
            else:
                mode = 0o755 if member.mode & 0o111 else 0o644
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
                with archive.extractfile(member) as source, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush(); os.fsync(output.fileno())
                os.chmod(target, mode, follow_symlinks=False)
        for path, link in links:
            path.symlink_to(link)
    _require(_tree(destination, uid, staged=True) == descriptor["tree_sha256"], "extracted_tree_mismatch")


def _verify_profile(profile, *, staged=False):
    _parents(profile.root, profile.uid)
    _directory(profile.root, profile.uid, 0o700)
    _require({p.name for p in profile.root.iterdir()} == {"python", "git", "tmux", "jq", "bin", "ca-bundle.pem", "receipt.json"}, "foreign_profile_content")
    receipt = _load(profile.root / "receipt.json", profile.uid, mode=0o600 if staged else 0o444)
    _require(type(receipt) is dict and set(receipt) == {"schema_version", "binding", "request_id", "state"}
             and type(receipt["schema_version"]) is int and receipt["schema_version"] == 1 and receipt["binding"] == _binding(profile)
             and receipt["state"] == "ready" and _uuid(receipt["request_id"]), "runtime_receipt_changed")
    for kind, descriptor in _DESCRIPTOR["artifacts"].items():
        _require(_tree(profile.root / kind, profile.uid, staged=staged) == descriptor["tree_sha256"], "runtime_tree_changed")
    _require(_sha(_read(profile.ca_bundle, profile.uid, 1024 * 1024, 0o644 if staged else 0o444)) == _DESCRIPTOR["ca_sha256"], "runtime_ca_changed")
    _directory(profile.bin, profile.uid, 0o755 if staged else 0o555)
    _require({p.name for p in profile.bin.iterdir()} == set(ALIASES), "runtime_alias_changed")
    for name, target in ALIASES.items():
        info = (profile.bin / name).lstat()
        _require(stat.S_ISLNK(info.st_mode) and info.st_uid == profile.uid and os.readlink(profile.bin / name) == target, "runtime_alias_changed")
    return receipt


def _seal_profile(profile):
    # Only the complete, exact staged profile can cross to immutable modes.
    _verify_profile(profile, staged=True)
    # Darwin requires write permission on a directory moved across parents.
    # Keep only this private container0700; every component directory is0555.
    for directory, directories, files in os.walk(profile.root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name; info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, 0o555 if info.st_mode & 0o111 else 0o444, follow_symlinks=False)
        path = Path(directory)
        os.chmod(path, 0o700 if path == profile.root else 0o555, follow_symlinks=False)
    _verify_profile(profile)


def admit_profile(*, expected_uid=None, expected_account=None):
    identity = _identity(expected_uid=expected_uid, expected_account=expected_account)
    profile = _profile(identity)
    _require(profile.root.exists() or profile.root.is_symlink(), "runtime_absent")
    _parents(profile.root, profile.uid)
    _verify_profile(profile)
    return profile


def environment(profile):
    # Callers first gate the exact derived browser installation family.
    _verify_profile(profile)
    return {"HOME": str(profile.home), "PATH": str(profile.bin) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "CLAUDLOBBY_NATIVE_PYTHON": str(profile.python), "TMUX_BIN": str(profile.tmux),
            "GIT_EXEC_PATH": str(profile.git_exec_path), "GIT_TEMPLATE_DIR": str(profile.root / "git/share/git-core/templates"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false", "GIT_SSL_CAINFO": str(profile.ca_bundle),
            "SSL_CERT_FILE": str(profile.ca_bundle), "PIP_CONFIG_FILE": "/dev/null", "PIP_NO_INPUT": "1", "PLANE_EMIT_DISABLED": "1"}


def git_argv(profile, *arguments):
    _verify_profile(profile)
    return [str(profile.git), *arguments]


def _attempt(base, identity):
    value = _load(base / "attempt.json", identity.uid)
    _require(type(value) is dict and set(value) == {"schema_version", "binding", "request_id", "stage_device", "stage_inode", "phase", "files"}
             and type(value["schema_version"]) is int and value["schema_version"] == 1 and value["binding"] == _binding(identity) and _uuid(value["request_id"])
             and value["phase"] in ("bootstrap", "preparing", "ready", "failed") and type(value["files"]) is dict, "attempt_changed")
    _require(all(type(value[key]) is int and value[key] > 0 for key in ("stage_device", "stage_inode")), "attempt_changed")
    for name, inode in value["files"].items():
        _require(name in {"python.tar.gz", "git.tar.gz", "tmux.tar.gz", "jq.bin"} and type(inode) is list and len(inode) == 2
                 and all(type(part) is int and part > 0 for part in inode), "attempt_changed")
    return value


def status(value):
    validate_request("status", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    profile = _profile(identity); base = _base(identity)
    if profile.root.exists() or profile.root.is_symlink():
        receipt = _verify_profile(profile)
        return {"state": "ready", "profile_id": PROFILE_ID, "policy_sha256": _policy(), "request_id": value["request_id"],
                "completion_request_id": receipt["request_id"], "never_replay": True}
    if not base.exists():
        return {"state": "absent", "request_id": value["request_id"], "never_replay": True}
    _directory(base, identity.uid, 0o700)
    if (base / "attempt.json").exists() or (base / "attempt.json").is_symlink():
        attempt = _attempt(base, identity)
        return {"state": "unknown", "request_id": attempt["request_id"], "phase": attempt["phase"], "never_replay": True}
    return {"state": "absent", "request_id": value["request_id"], "never_replay": True}


def _lock(base, uid):
    path = base / "transition.lock"
    inherited = os.fstat(_INHERITED_LOCK_FD) if _INHERITED_LOCK_FD is not None else None
    if inherited is not None:
        actual = path.lstat()
        _require((inherited.st_dev, inherited.st_ino) == (actual.st_dev, actual.st_ino), "lock_identity_changed")
        fd = os.dup(_INHERITED_LOCK_FD)
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                 and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 0, "unsafe_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd); raise


def _stage(base, attempt, uid):
    stage = base / ("stage-" + attempt["request_id"])
    info = _directory(stage, uid, 0o700)
    _require((info.st_dev, info.st_ino) == (attempt["stage_device"], attempt["stage_inode"]), "stage_identity_changed")
    return stage


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _require(urllib.parse.urlsplit(newurl).scheme == "https", "insecure_artifact_redirect")
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(kind, stage, attempt, base, identity, deadline):
    descriptor = _DESCRIPTOR["artifacts"][kind]
    path = stage / (kind + (".bin" if kind == "jq" else ".tar.gz"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    attempt["files"][path.name] = [info.st_dev, info.st_ino]
    _save(base / "attempt.json", attempt, identity.uid)
    try:
        context = ssl.create_default_context(cafile=stage / "profile/python" / CA_MEMBER)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), _HTTPSRedirect())
        remaining = deadline - time.monotonic(); _require(remaining > 0, "preparation_deadline")
        with os.fdopen(fd, "wb") as output, opener.open(descriptor["url"], timeout=min(30, remaining)) as response:
            total = 0; digest = hashlib.sha256()
            while True:
                _require(time.monotonic() < deadline, "preparation_deadline")
                chunk = response.read1(min(65536, descriptor["bytes"] + 1 - total))
                if not chunk:
                    break
                total += len(chunk); _require(total <= descriptor["bytes"], "artifact_oversize")
                output.write(chunk); digest.update(chunk)
            output.flush(); os.fsync(output.fileno())
        _require(total == descriptor["bytes"] and digest.hexdigest() == descriptor["sha256"], "artifact_mismatch")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return path


def _publish(stage, destination):
    # Darwin renamex_np(RENAME_EXCL): never replace even an empty foreign dir.
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(stage), os.fsencode(destination), 0x4) != 0:
        raise OSError(ctypes.get_errno(), "exclusive runtime publication failed")


def prepare(value, *, bootstrap=False):
    validate_request("prepare", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    profile = _profile(identity); base = _base(identity)
    if profile.root.exists() or profile.root.is_symlink():
        return status(value)
    _require(bootstrap, "bootstrap_entry_required")
    _parents(base, identity.uid)
    _directory(base, identity.uid, 0o700)
    lock = _lock(base, identity.uid)
    try:
        attempt = _attempt(base, identity)
        _require(attempt["request_id"] == value["request_id"] and attempt["phase"] == "bootstrap", "original_attempt_required")
        stage = _stage(base, attempt, identity.uid); work = stage / "profile"
        _directory(work, identity.uid, 0o700)
        _require(_tree(work / "python", identity.uid, staged=True) == _DESCRIPTOR["artifacts"]["python"]["tree_sha256"], "bootstrap_python_changed")
        admitted = True
        attempt["phase"] = "preparing"; _save(base / "attempt.json", attempt, identity.uid)
        deadline = time.monotonic() + BUDGET
        for kind in ("git", "tmux", "jq"):
            archive = _download(kind, stage, attempt, base, identity, deadline)
            if kind == "jq":
                destination = work / "jq"; _directory(destination, identity.uid, 0o755, create=True)
                _directory(destination / "bin", identity.uid, 0o755, create=True)
                raw = _read(archive, identity.uid, _DESCRIPTOR["artifacts"][kind]["bytes"])
                _require(_sha(raw) == _DESCRIPTOR["artifacts"][kind]["sha256"], "artifact_mismatch")
                fd = os.open(destination / "bin/jq", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
                os.fchmod(fd, 0o755)
                with os.fdopen(fd, "wb") as out:
                    out.write(raw); out.flush(); os.fsync(out.fileno())
            else:
                _extract(archive, work / kind, kind, _DESCRIPTOR["artifacts"][kind], identity.uid)
        _directory(work / "bin", identity.uid, 0o755, create=True)
        for name, target in ALIASES.items():
            (work / "bin" / name).symlink_to(target)
        ca = _read(work / "python" / CA_MEMBER, identity.uid, 1024 * 1024, 0o644)
        _require(_sha(ca) == _DESCRIPTOR["ca_sha256"], "runtime_ca_changed")
        fd = os.open(work / "ca-bundle.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as out:
            out.write(ca); out.flush(); os.fsync(out.fileno())
        receipt = {"schema_version": 1, "binding": _binding(identity), "request_id": value["request_id"], "state": "ready"}
        _save(work / "receipt.json", receipt, identity.uid, new=True)
        staged_profile = RuntimeProfile(profile.uid, profile.account, profile.home, work, work / "bin", work / "python/bin/python3.12",
                                        work / "git/bin/git", work / "git/libexec/git-core", work / "tmux/bin/tmux", work / "jq/bin/jq",
                                        work / "ca-bundle.pem", _policy())
        _seal_profile(staged_profile)
        _require(time.monotonic() < deadline, "preparation_deadline")
        _publish(work, profile.root); _sync(base)
        attempt["phase"] = "ready"; _save(base / "attempt.json", attempt, identity.uid)
        return status(value)
    except Exception:
        if locals().get("admitted") and attempt.get("phase") != "ready":
            attempt["phase"] = "failed"; _save(base / "attempt.json", attempt, identity.uid)
        raise
    finally:
        os.close(lock)  # Never LOCK_UN: inherited holders share one description.


def _owned_partial(component, archive_path, kind, descriptor, uid):
    _require(_sha(_read(archive_path, uid, descriptor["bytes"])) == descriptor["sha256"], "unknown_stage_content")
    _require(stat.S_IMODE(_directory(component, uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
    with tarfile.open(archive_path, "r:gz") as archive:
        expected = {}
        for member in _inventory(archive, descriptor):
            name = _selection(member, kind)
            if not name:
                continue
            for parent in PurePosixPath(name).parents:
                if str(parent) != ".":
                    expected.setdefault(str(parent), ("directory", 0o755, None))
            expected[name] = ("directory", 0o755, None) if member.isdir() else ("symlink", 0, member.linkname) if member.issym() else ("file", 0o755 if member.mode & 0o111 else 0o644, member)
        for directory, directories, files in os.walk(component, followlinks=False):
            for name in directories + files:
                path = Path(directory) / name; relative = path.relative_to(component).as_posix(); info = path.lstat()
                _require(relative in expected and info.st_uid == uid, "unknown_stage_content")
                kind_expected, mode, value = expected[relative]
                if stat.S_ISLNK(info.st_mode):
                    _require(kind_expected == "symlink" and info.st_nlink == 1 and os.readlink(path) == value, "unknown_stage_content")
                    if name in directories:
                        directories.remove(name)
                elif stat.S_ISDIR(info.st_mode):
                    _require(kind_expected == "directory" and stat.S_IMODE(info.st_mode) in (mode, mode & ~0o222), "unknown_stage_content")
                else:
                    _require(kind_expected == "file" and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                             and stat.S_IMODE(info.st_mode) in (mode, mode & ~0o222) and info.st_size <= value.size, "unknown_stage_content")
                    # An interrupted known write may be a prefix, never foreign data.
                    actual = _read(path, uid, value.size, stat.S_IMODE(info.st_mode))
                    _require(archive.extractfile(value).read(len(actual)) == actual, "unknown_stage_content")


def discard(value):
    validate_request("discard", value)
    identity = _identity(expected_uid=value["expected_uid"], expected_account=value["expected_account"])
    base = _base(identity); _parents(base, identity.uid); _directory(base, identity.uid, 0o700)
    lock = _lock(base, identity.uid)
    try:
        attempt = _attempt(base, identity)
        _require(attempt["request_id"] == value["request_id"], "original_attempt_required")
        profile = _profile(identity)
        if profile.root.exists() or profile.root.is_symlink():
            _verify_profile(profile)
        stage = _stage(base, attempt, identity.uid)
        allowed = set(attempt["files"]) | {"profile"}
        _require({p.name for p in stage.iterdir()} <= allowed, "unknown_stage_content")
        for name, expected in attempt["files"].items():
            _require(name in {"python.tar.gz", "git.tar.gz", "tmux.tar.gz", "jq.bin"}, "attempt_changed")
            path = stage / name
            if path.exists() or path.is_symlink():
                info = path.lstat(); kind = name.split(".", 1)[0]
                _require([info.st_dev, info.st_ino] == expected and stat.S_ISREG(info.st_mode) and info.st_uid == identity.uid
                         and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
                         and info.st_size <= _DESCRIPTOR["artifacts"][kind]["bytes"], "unknown_stage_content")
        work = stage / "profile"
        if work.exists() or work.is_symlink():
            _directory(work, identity.uid, 0o700)
            _require({p.name for p in work.iterdir()} <= set(_DESCRIPTOR["artifacts"]) | {"bin", "ca-bundle.pem", "receipt.json"}, "unknown_stage_content")
            for kind in _DESCRIPTOR["artifacts"]:
                path = work / kind
                if not (path.exists() or path.is_symlink()):
                    continue
                if kind == "jq":
                    _require(stat.S_IMODE(_directory(path, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                    _require({p.name for p in path.iterdir()} <= {"bin"}, "unknown_stage_content")
                    binary_dir = path / "bin"
                    if binary_dir.exists() or binary_dir.is_symlink():
                        _require(stat.S_IMODE(_directory(binary_dir, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                        _require({p.name for p in binary_dir.iterdir()} <= {"jq"}, "unknown_stage_content")
                        binary = binary_dir / "jq"
                        if binary.exists() or binary.is_symlink():
                            expected_raw = _read(stage / "jq.bin", identity.uid, _DESCRIPTOR["artifacts"][kind]["bytes"])
                            mode = stat.S_IMODE(binary.lstat().st_mode)
                            _require(mode in (0o755, 0o555) and _sha(expected_raw) == _DESCRIPTOR["artifacts"][kind]["sha256"]
                                     and expected_raw.startswith(_read(binary, identity.uid, len(expected_raw), mode)), "unknown_stage_content")
                else:
                    _owned_partial(path, stage / (kind + ".tar.gz"), kind, _DESCRIPTOR["artifacts"][kind], identity.uid)
            aliases = work / "bin"
            if aliases.exists() or aliases.is_symlink():
                _require(stat.S_IMODE(_directory(aliases, identity.uid).st_mode) in (0o755, 0o555), "unknown_stage_content")
                for path in aliases.iterdir():
                    info = path.lstat()
                    _require(path.name in ALIASES and stat.S_ISLNK(info.st_mode) and info.st_uid == identity.uid
                             and info.st_nlink == 1 and os.readlink(path) == ALIASES[path.name], "unknown_stage_content")
            ca = work / "ca-bundle.pem"
            if ca.exists() or ca.is_symlink():
                source_ca = work / "python" / CA_MEMBER
                source_mode = stat.S_IMODE(source_ca.lstat().st_mode); mode = stat.S_IMODE(ca.lstat().st_mode)
                _require(source_mode in (0o644, 0o444) and mode in (0o644, 0o444), "unknown_stage_content")
                expected_ca = _read(source_ca, identity.uid, 1024 * 1024, source_mode)
                _require(_sha(expected_ca) == _DESCRIPTOR["ca_sha256"] and expected_ca.startswith(_read(ca, identity.uid, len(expected_ca), mode)), "unknown_stage_content")
            receipt = work / "receipt.json"
            if receipt.exists() or receipt.is_symlink():
                expected_receipt = _canonical({"schema_version": 1, "binding": _binding(identity), "request_id": value["request_id"], "state": "ready"})
                mode = stat.S_IMODE(receipt.lstat().st_mode)
                _require(mode in (0o600, 0o444) and expected_receipt.startswith(_read(receipt, identity.uid, mode=mode)), "unknown_stage_content")
        # Lock acquisition proves no surviving admitted descendant owns the stage.
        # Only exact inode-bound known content was admitted above; no generic reset.
        if work.exists():
            # All names, inodes, bytes and modes were admitted above. Restore
            # directory write permission only in this owned disposable stage.
            for directory, directories, files in os.walk(work, followlinks=False):
                os.chmod(directory, 0o700 if Path(directory) == work else 0o755, follow_symlinks=False)
            # Keep the running interpreter until all other work is removed.
            # Interruption during its final removal still requires preservation.
            for path in work.iterdir():
                if path.name != "python":
                    shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
            if (work / "python").exists():
                shutil.rmtree(work / "python")
            work.rmdir()
        for path in stage.iterdir():
            path.unlink()
        stage.rmdir(); (base / "attempt.json").unlink(); _sync(base)
        return {"state": "discarded", "request_id": value["request_id"], "installed_profile_unchanged": True}
    finally:
        os.close(lock)


def main():
    global _INHERITED_LOCK_FD
    try:
        _require(len(sys.argv) in (3, 4), "invalid_request")
        operation = sys.argv[1]; raw = sys.argv[2].encode("ascii")
        _require(len(raw) <= 2048, "invalid_request")
        value = json.loads(raw, object_pairs_hook=_unique)
        validate_request(operation, value)
        _require(raw == _canonical(value), "invalid_request")
        if operation != "status":
            _INHERITED_LOCK_FD = 9
        result = status(value) if operation == "status" else discard(value) if operation == "discard" else prepare(value, bootstrap=len(sys.argv) == 4 and sys.argv[3] == "bootstrap")
        sys.stdout.write(_canonical({"schema_version": 1, "result": result}).decode("ascii") + "\n")
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, RuntimeRefusal) else "runtime_unavailable"
        sys.stdout.write(_canonical({"schema_version": 1, "state": "unavailable", "code": code}).decode("ascii") + "\n")
        return 6


if __name__ == "__main__":
    sys.exit(main())
# END CANONICAL DARWIN RUNTIME PYTHON
'''

def _darwin_runtime():
    """Load the mechanically shared standalone policy, never mutable app code."""
    import types
    module = types.ModuleType("_claudlobby_fixed_darwin_runtime")
    sys.modules[module.__name__] = module
    exec(compile(_DARWIN_RUNTIME_SOURCE, "<fixed Darwin runtime>", "exec"), module.__dict__)
    return module


def managed_browser_runtime(installation):
    """Read-only admission of the fixed Darwin browser installation, else None."""
    if platform.system() != "Darwin":
        return None
    account = pwd.getpwuid(os.geteuid())
    family = Path(account.pw_dir) / ".local/share/claudlobby-browser"
    if (installation.directory != family / "install"
            or installation.data_root != family / "data"):
        return None
    policy = _darwin_runtime()
    try:
        return policy.admit_profile(expected_uid=os.geteuid(), expected_account=account.pw_name)
    except policy.RuntimeRefusal as exc:
        raise InstallationError("managed Darwin runtime refused: " + exc.code) from None


def managed_browser_environment(installation):
    """Construct the verified fixed tool environment without ambient selectors."""
    profile = managed_browser_runtime(installation)
    if profile is None:
        return None
    return _managed_environment(profile)


def _managed_environment(profile):
    policy = _darwin_runtime()
    try:
        return policy.environment(profile)
    except policy.RuntimeRefusal as exc:
        raise InstallationError("managed Darwin runtime refused: " + exc.code) from None


def _managed_git_flags():
    return ["--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "credential.helper=", "-c", "core.askPass=/usr/bin/false", "-c", "http.sslVerify=true",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0"]


def checkout_revision(checkout: Path, *, clean: bool = False, _run_git=None, _installation=None) -> str:
    """Read only this explicit Git worktree; dirty scans are update preflight."""
    checkout = _path(checkout)
    # Only the exact account-derived source opts into managed Darwin tools.
    account = pwd.getpwuid(os.geteuid()) if platform.system() == "Darwin" else None
    family = Path(account.pw_dir) / ".local/share/claudlobby-browser" if account else None
    if (_installation is None and family is not None
            and checkout == family / "install/src"):
        _installation = read_installation(family / "install")
    profile = managed_browser_runtime(_installation) if _installation is not None else None
    # Ambient Git selectors must not redirect a check to another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    if profile is not None:
        env = _managed_environment(profile)

    def git(*args):
        try:
            command = ([str(profile.git), *_managed_git_flags()] if profile is not None
                       else ["git", "--no-optional-locks"])
            command.extend(["-C", str(checkout), *args])
            if _run_git is None:
                result = subprocess.run(command, env=env, capture_output=True,
                                        text=True, timeout=15, check=True)
            else:
                result = _run_git(command)
                if result.returncode:
                    raise subprocess.CalledProcessError(result.returncode, command)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallationError("explicit installation Git checkout is unavailable") from exc
        return (result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout).strip()

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

    def preflight(self, *, expected_revision: str | None = None, _run_git=None) -> str:
        """Verify the managed checkout before effects; never repair local edits."""
        revision = checkout_revision(self.source, clean=True, _run_git=_run_git, _installation=self)
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
    profile = managed_browser_runtime(installation)
    if profile is not None and interpreter != profile.python:
        raise InstallationError("launcher requires the exact managed Darwin Python")
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


def _native_lock(installation, *, inherited=None):
    """A native start's shared runtime lock. The fence is read before the lock as well as under it, so a fenced
    start is turned away even while the updater holds the lock, and is no reader in its way."""
    _check_maintenance(installation, native=True)
    return _runtime_lock(installation, inherited=inherited)


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
            fd = _native_lock(installation, inherited=fd)
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
        fd = _native_lock(installation, inherited=fd)
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
        _exec_git_unit(installation, target, environment)
    except FencedError as exc:
        # A scheduled job that meets the fence ends clean, so its timer raises no failure; a resident
        # unit must stay a failure, or one with RemainAfterExit=yes would read active with nothing running.
        if target.mode != "oneshot":
            raise
        print(f"claudlobby: skipped {target.unit}: {exc}")
        return 0


def _exec_git_unit(installation, target, environment):
    fd = _native_lock(installation)
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



_SETUP_FIELDS = ("generation", "request_id", "config_fingerprint", "source_revision")


def _setup_binding(values):
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if (len(values) != 4 or not all(isinstance(v, str) for v in values)
            or not all(re.fullmatch(uuid, v) for v in values[:2])
            or not re.fullmatch(r"[0-9a-f]{64}", values[2])
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", values[3])):
        raise InstallationError("setup continuation requires the exact original identifiers")
    return tuple(values)


def _entry_python(installation):
    path = installation.cli
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        content = os.read(fd, 1024 * 1024)
        linked = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022 or not info.st_mode & 0o111 or info.st_size != len(content)
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
            raise InstallationError("stable setup launcher is unavailable or redirected")
    finally:
        os.close(fd)
    first, separator, body = content.partition(b"\n")
    try:
        host = Path(first[2:].decode("utf-8"))
    except UnicodeError:
        raise InstallationError("stable launcher host Python is invalid") from None
    if (not separator or not first.startswith(b"#!") or not host.is_absolute()
            or any(c.isspace() for c in str(host)) or host.resolve() != host
            or not host.is_file() or not os.access(host, os.X_OK)
            or host.is_relative_to(installation.source) or host.is_relative_to(installation.environment)):
        raise InstallationError("setup continuation requires its bound host Python")
    profile = managed_browser_runtime(installation)
    if profile is not None and host != profile.python:
        raise InstallationError("setup continuation requires the exact managed Darwin Python")
    return host, body


def setup_handoff_argv(installation, data):
    """Called only after imported guide/terminal/children have drained.

    An older installed launcher cannot interpret the new private route. Refuse
    before exec rather than accidentally waiting in imported application code.
    """
    if not isinstance(data, dict) or set(data) != {"state", *_SETUP_FIELDS} or data["state"] != "launch_handoff":
        raise InstallationError("setup handoff has no original operation")
    original = _setup_binding([data[key] for key in _SETUP_FIELDS])
    host, body = _entry_python(installation)
    if body != Path(__file__).read_bytes():
        raise InstallationError("installed stable launcher cannot finish setup automatically; preserve the original update and reopen host owner setup after DONE")
    return [str(host), "-I", "-S", "-B", str(installation.cli), "--root",
            str(installation.data_root), "_setup-wait", *original]


def _setup_recovery(installation, original):
    print("Original setup request: " + original[1] + "; configuration " + original[2] + "; source " + original[3] + ".", file=sys.stderr)
    print("Inspect with the same installation CLI: host update status --json. Continue only that original operation locally if required; do not launch again.", file=sys.stderr)


def _setup_status(installation, environment, cancelled, *, seconds=10):
    """One owned read-only child of the existing independent update entry."""
    host, _ = _entry_python(installation)
    updater = installation.bin / "claudlobby-update"
    info = updater.lstat()
    if (updater.resolve() != updater or not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111
            or info.st_mode & 0o022 or info.st_uid not in {0, os.getuid()}):
        raise InstallationError("independent update status entry is unavailable")
    process = None
    try:
        if cancelled():
            raise InterruptedError
        process = subprocess.Popen([str(host), "-I", "-S", "-B", str(updater), "--root", str(installation.data_root),
                                    "host", "update", "status", "--json"],
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        deadline = time.monotonic() + seconds
        output = bytearray()
        while True:
            if cancelled():
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise InstallationError("original update status read timed out")
            if select.select([process.stdout], [], [], min(.1, max(0, deadline - time.monotonic())))[0]:
                part = os.read(process.stdout.fileno(), 65536)
                if not part:
                    break
                output.extend(part)
                if len(output) > 4 * 1024 * 1024:
                    raise InstallationError("original update status is too large")
        if process.wait(timeout=max(.1, deadline - time.monotonic())) != 0:
            raise InstallationError("original update status was refused")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        try:
            result = json.loads(output, object_pairs_hook=unique,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise InstallationError("original update status was malformed") from None
        if type(result) is not dict or set(result) != {"read_only", "update"} or result["read_only"] is not True or type(result["update"]) is not dict:
            raise InstallationError("original update status is unavailable")
        return result["update"]
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()  # ONLY our status-read child, never the updater job/fleet.
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()


def _setup_wait(installation, values, environment, *, seconds=600, read_seconds=10, interval=1):
    """Stable stdlib-only wait: no source/env import, reader lease or writes."""
    original = _setup_binding(values)
    cancelled = [0]
    def stop(number, _frame):
        cancelled[0] = number
    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if cancelled[0]:
                raise InterruptedError
            record = _setup_status(installation, environment, lambda: bool(cancelled[0]),
                                   seconds=min(read_seconds, max(.1, deadline - time.monotonic())))
            if (record.get("request_id") != original[1] or record.get("config_fingerprint") != original[2]
                    or record.get("target") != original[3] or type(record.get("schema")) is not int or record["schema"] != 1
                    or not isinstance(record.get("phase"), str) or record.get("phase") not in {"prepare","quiesce","git","environment","publish","migrate","commit","restore","done"}):
                raise InstallationError("the retained setup operation changed; no replacement was followed")
            if record.get("stage_error") or record.get("health_errors"):
                raise InstallationError("the original setup needs local attention")
            if record["phase"] == "done":
                if (record.get("maintenance") is not False or record.get("config_committed") is not True
                        or type(record.get("health_errors")) is not dict):
                    raise InstallationError("the original setup is not healthy DONE")
                host, _ = _entry_python(installation)
                if cancelled[0]:
                    raise InterruptedError
                # Normal launcher admission reacquires a NEW current reader and
                # imports current source; no inherited old lease is reused.
                os.execve(host, [str(host), "-I", "-S", "-B", str(installation.cli), "--root", str(installation.data_root),
                                 "host", "owner", "setup", "--_finish-setup", *original], environment)
            until = min(deadline, time.monotonic() + interval)
            while time.monotonic() < until:
                if cancelled[0]:
                    raise InterruptedError
                time.sleep(min(.1, max(0, until - time.monotonic())))
        raise InstallationError("waiting for the original setup timed out")
    except InterruptedError:
        _setup_recovery(installation, original)
        print("Setup waiting cancelled; the original update and workloads were not stopped.", file=sys.stderr)
        return 128 + (cancelled[0] or signal.SIGINT)
    except (InstallationError, OSError, subprocess.SubprocessError):
        _setup_recovery(installation, original)
        raise InstallationError("setup completion could not be observed; inspect the original operation") from None
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


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
    if "_setup-wait" in arguments:
        if runtime or len(arguments) != 7 or arguments[2] != "_setup-wait":
            raise InstallationError("setup continuation has an invalid argument vector")
        return _setup_wait(installation, arguments[3:], environment)
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
    profile = managed_browser_runtime(installation)
    if profile is not None and python.resolve() != profile.python:
        raise InstallationError("runtime interpreter differs from the exact managed Darwin Python")
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
    except (InstallationError, OSError) as exc:
        print(f"claudlobby: {exc}", file=sys.stderr)
        sys.exit(75 if isinstance(exc, FencedError) else 6)  # 75: an update's fence turned it away; a later start can pass
