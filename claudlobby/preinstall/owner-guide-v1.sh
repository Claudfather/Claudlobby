#!/bin/sh
# Immutable non-PTY admission prelude, then the ordinary installed owner guide.
# One canonical <=1024-byte admission line; no EOF and no success prefix.
# Only setup/status/api/computer. Trusted authenticated account; no same-UID/root sandbox.
# api: one typed owner operation; the server SSH_CONNECTION passes through unread.
operation=owner-guide-v1
if [ "$#" -eq 1 ] && [ "$1" = status ]; then operation=owner-guide-status-v1; fi
if [ "$#" -eq 1 ] && [ "$1" = api ]; then operation=owner-api-v1; fi
if [ "$#" -eq 1 ] && [ "$1" = computer ]; then operation=owner-guide-computer-v1; fi
refuse() {
    printf '{"code":"%s","operation":"%s","retry":false,"schema_version":1,"state":"refused"}\n' "$1" "$operation"
}
if [ "$#" -ne 1 ]; then refuse invalid_request; exit 2; fi
case "$1" in setup|status|api|computer) ;; *) refuse invalid_request; exit 2 ;; esac
exec 3<<'FIXED_OPERATION_PYTHON'
# BEGIN EMBEDDED PYTHON
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import select
import signal
import stat
import subprocess
import sys
import time

REMOTE = "https://github.com/Claudfather/Claudlobby.git"
BINDING_FIELDS = {"schema", "purpose", "installation", "data_root", "remote", "ref",
                  "interpreter", "interpreter_sha256"}
GENERATED = {"BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE", "FLEET_ROOT",
             "FLEET_NAME", "CLAUDLOBBY_FLEET", "CLAUDLOBBY_TIMER_CONTEXT"}
GIT_FLAGS = ["--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "credential.helper=", "-c", "core.askPass=/bin/false", "-c", "http.sslVerify=true",
             "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
SHA = r"[0-9a-f]{40}"
DIGEST = r"[0-9a-f]{64}"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"


class Refusal(Exception):
    def __init__(self, code="installation_changed", exit_code=4):
        self.code, self.exit_code = code, exit_code


def wire(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                       allow_nan=False) + "\n").encode("ascii")


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def request(fd, deadline, action="setup"):
    # A single-byte read deliberately leaves all guide input on inherited stdin.
    raw = bytearray()
    end = min(deadline, time.monotonic() + 5)
    while not raw.endswith(b"\n"):
        remaining = end - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise Refusal("invalid_request", 2)
        part = os.read(fd, 1)
        if not part or len(raw) >= 1024:
            raise Refusal("invalid_request", 2)
        raw.extend(part)
    try:
        if any(c < 32 or c > 126 for c in raw[:-1]):
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict)
                or set(value) != {"schema_version", "expected_uid", "source_revision", "binding_sha256", "finish_setup"} | ({"update_record"} if action == "computer" else set())
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["expected_uid"]) is not int or not 0 < value["expected_uid"] < 2**32 - 1
                or not isinstance(value["source_revision"], str) or not re.fullmatch(SHA, value["source_revision"])
                or not isinstance(value["binding_sha256"], str) or not re.fullmatch(DIGEST, value["binding_sha256"])
                or wire(value) != raw):
            raise ValueError()
        finish = value["finish_setup"]
        if finish is not None and (type(finish) is not list or len(finish) != 4
                or not all(isinstance(v, str) for v in finish)
                or not all(re.fullmatch(UUID, v) for v in finish[:2])
                or not re.fullmatch(DIGEST, finish[2]) or not re.fullmatch(SHA + r"|[0-9a-f]{64}", finish[3])):
            raise ValueError()
        known = value.get("update_record")  # The computer run's only extra key.
        if known is not None and (type(known) is not list or len(known) != 2
                or not re.fullmatch(UUID, known[0]) or not re.fullmatch(SHA, known[1])):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Refusal("invalid_request", 2) from None


def plain_file(path, limit=65536, mode=0o600, owner=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        linked = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != (os.getuid() if owner is None else owner)
                or info.st_nlink != 1 or info.st_mode & 0o022
                or mode is not None and stat.S_IMODE(info.st_mode) != mode or info.st_size > limit
                or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)):
            raise Refusal()
        raw = os.read(fd, limit + 1)
        if len(raw) > limit:
            raise Refusal()
        return raw
    finally:
        os.close(fd)


def read_json(path, limit=65536):
    return json.loads(plain_file(path, limit), object_pairs_hook=unique)


def directory(path, private=False):
    info = path.lstat()
    if (path.resolve() != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022
            or private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700)):
        raise Refusal()


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


def admitted_darwin_profile(expected_uid=None):
    uid = os.geteuid()
    if uid == 0 or uid != os.getuid() or expected_uid is not None and uid != expected_uid:
        raise Refusal("account_changed")
    policy = _darwin_runtime()
    try:
        account = pwd.getpwuid(os.geteuid())
        return policy.admit_profile(expected_uid=expected_uid if expected_uid is not None else os.geteuid(),
                                    expected_account=account.pw_name)
    except policy.RuntimeRefusal as error:
        raise Refusal(error.code) from None


def account_profile(expected_uid):
    if platform.system() == "Darwin":
        return admitted_darwin_profile(expected_uid).home
    uid = os.getuid()
    if uid == 0 or uid != os.geteuid() or uid != expected_uid:
        raise Refusal("account_changed")
    home = Path(pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute() or home.resolve() != home:
        raise Refusal("account_changed")
    for path in reversed((home, *home.parents)):
        directory(path)
    if home.stat().st_uid != uid:
        raise Refusal("account_changed")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64", "arm64"}:
        raise Refusal("unsupported_platform")
    fields = {}
    for line in plain_file(Path("/etc/os-release").resolve(), 8192, None, 0).decode("ascii").splitlines():
        key, separator, item = line.partition("=")
        if separator:
            fields[key] = item.strip("\"")
    if fields.get("ID") != "ubuntu" or fields.get("VERSION_ID") != "24.04":
        raise Refusal("unsupported_platform")
    return home


def selectors(home):
    if any(key in os.environ for key in GENERATED):
        raise Refusal("operator_required")
    family = home / ".local/share/claudlobby-browser"
    expected = {"CLAUDLOBBY_ROOT": str(family / "data"), "CLAUDLOBBY_INSTALLATION": str(family / "install"),
                "CLAUDLOBBY_HOST_SYSTEM_YAML": str(family / "host-policy/system.yaml"),
                "CLAUDE_CONFIG_DIR": str(family / "provider-profile")}
    if any(key in os.environ and os.environ[key] != value for key, value in expected.items()):
        raise Refusal("operator_required")
    return expected


def independent_python():
    python = Path(sys.executable).resolve()
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if python != profile.python or sys.prefix != sys.base_prefix:
            raise Refusal("prerequisite_unusable", 6)
        return python
    folder = Path("/usr/bin").lstat()
    if (sys.implementation.name != "cpython" or sys.version_info < (3, 10)
            or sys.prefix != sys.base_prefix or python.parent != Path("/usr/bin")
            or not stat.S_ISDIR(folder.st_mode) or folder.st_uid != 0 or folder.st_mode & 0o022):
        raise Refusal("prerequisite_unusable", 6)
    trusted_executable(python)
    return python


def trusted_executable(path):
    # Platform executables may legitimately have multiple root-owned links.
    # Single-link admission remains required for private identity/code records.
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise Refusal("prerequisite_unusable", 6)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recorded_python(state, python):
    # The receipt names this running interpreter. The managed Mac Python keeps
    # its byte pin. The Ubuntu /usr/bin Python is admitted by trusted_executable
    # instead: security updates replace it, so its bytes are not compared and
    # the digest recorded at preparation stays in the receipt and binding.
    recorded = state["interpreter_sha256"]
    return (state["interpreter"] == str(python) and isinstance(recorded, str)
            and re.fullmatch(DIGEST, recorded) is not None
            and (platform.system() != "Darwin" or recorded == digest(python)))


def git_environment(home):
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if profile.home != home:
            raise Refusal("account_changed")
        environment = _darwin_runtime().environment(profile)
        return environment
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}


def source_check(source, revision, env, deadline, differs=False):
    # Match the canonical preparer: explicit private clone, finite local Git,
    # no selectors, includes, filters, remotes, hooks or interrupted Git state.
    directory(source, True)
    directory(source / ".git", True)
    parser = configparser.RawConfigParser(strict=True)
    parser.read_string(plain_file(source / ".git/config", mode=None).decode("utf-8"))
    allowed = {"repositoryformatversion": {"0"}, "filemode": {"true"}, "bare": {"false"},
               "logallrefupdates": {"true"}, "ignorecase": {"true", "false"},
               "precomposeunicode": {"true", "false"}}
    values = dict(parser.items("core"))
    if (parser.sections() != ["core"] or parser.defaults() or not values
            or any(key not in allowed or value not in allowed[key] for key, value in values.items())
            or any(os.path.lexists(source / ".git" / name) for name in ("commondir", "objects/info/alternates"))):
        raise Refusal("source_mismatch")
    count = 0
    for root, directories, files in os.walk(source / ".git", followlinks=False):
        for name in directories + files:
            count += 1
            if (count > 10000 or (Path(root) / name).is_symlink() or name.endswith(".lock")
                    or Path(root).name == "pack" and name.startswith("tmp_")):
                raise Refusal("source_mismatch")
    flags = GIT_FLAGS
    if platform.system() == "Darwin":
        git_binary = admitted_darwin_profile().git
        flags = ["core.askPass=/usr/bin/false" if item == "core.askPass=/bin/false" else item for item in GIT_FLAGS]
    else:
        git_binary = Path("/usr/bin/git").resolve()
        trusted_executable(git_binary)
    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("inspection_unavailable")
        result = subprocess.run([str(git_binary), *flags, "-C", str(source), *args], env=env,
                                capture_output=True, timeout=min(15, remaining), check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024 or len(result.stderr) > 65536:
            raise Refusal("source_mismatch")
        return result.stdout
    if differs and git("rev-parse", "--verify", "HEAD^{commit}").decode().strip() != revision:
        raise Refusal("revision_differs")
    if (git("rev-parse", "--show-toplevel").decode().strip() != str(source)
            or git("rev-parse", "--verify", "HEAD^{commit}").decode().strip() != revision
            or git("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
            or git("rev-parse", "--verify", "HEAD^{commit}").decode().strip() != revision):
        raise Refusal("source_mismatch")


def environment_check(environment, python):
    directory(environment, True)
    directory(environment / "bin", True)
    entry = environment / "bin/python"
    if not entry.exists() or entry.resolve() != python:
        raise Refusal()
    config = {}
    for line in plain_file(environment / "pyvenv.cfg", 8192, None).decode("utf-8").splitlines():
        key, separator, item = line.partition("=")
        if separator:
            if key.strip() in config:
                raise Refusal()
            config[key.strip()] = item.strip()
    if (config.get("home") != str(python.parent) or config.get("include-system-site-packages") != "false"
            or not config.get("version", "").startswith(str(sys.version_info.major) + "." + str(sys.version_info.minor) + ".")
            or "executable" in config and Path(config["executable"]).resolve() != python
            or os.path.lexists(environment / "pip.conf")):
        raise Refusal()


def validate(value, deadline, action="setup"):
    home = account_profile(value["expected_uid"])
    inherited = selectors(home)  # Preserve evidence before any normalization.
    python = independent_python()
    family = home / ".local/share/claudlobby-browser"
    for path in (home / ".local", home / ".local/share", family):
        directory(path, path == family)
    install, data, staging = (family / name for name in ("install", "data", "staging"))
    for path in (install, data, staging):
        directory(path, True)
    lock = os.open(install / "preparation.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        plain_file(install / "preparation.lock", 0)
        opened, linked = os.fstat(lock), (install / "preparation.lock").lstat()
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            raise Refusal()
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("preparation_busy") from None
        state = read_json(install / "preparation.json")
        if (not isinstance(state, dict) or set(state) != BINDING_FIELDS | {"revision", "lock_sha256", "environment_owned", "complete"}
                or type(state["schema"]) is not int or state["schema"] != 1
                or state["purpose"] != "installation-preparation-only" or state["remote"] != REMOTE
                or state["installation"] != str(install) or state["data_root"] != str(data)
                or not isinstance(state["ref"], str) or not re.fullmatch(SHA, state["ref"])
                or state["revision"] != state["ref"] or state["complete"] is not True or state["environment_owned"] is not True
                or not isinstance(state["lock_sha256"], str) or not re.fullmatch(DIGEST, state["lock_sha256"])
                or not recorded_python(state, python)
                or hashlib.sha256(json.dumps({key: state[key] for key in BINDING_FIELDS}, sort_keys=True).encode()).hexdigest() != value["binding_sha256"]
                or value["finish_setup"] is None and action not in {"api", "computer"} and state["ref"] != value["source_revision"]):
            # Only the fixed api mode carries no finish tuple: it is admitted at the
            # exact current installed source (source_check below) while the original
            # preparation binding, ref and receipt above stay verified unchanged.
            raise Refusal()
        identity = read_json(install / "installation.json", 4096)
        if identity != {"schema": 1, "data_root": str(data)} or type(identity.get("schema")) is not int:
            raise Refusal()
        source = install / "src"
        source_check(source, value["source_revision"], git_environment(home), deadline, action == "computer")
        environment_check(install / "env", python)
        directory(install / "bin", True)
        launcher = install / "bin/claudlobby"
        if plain_file(launcher, 1024 * 1024, 0o755) != ("#!" + str(python) + "\n").encode() + plain_file(source / "claudlobby/installation.py", 1024 * 1024, None):
            raise Refusal()
        # No installer inactive-data check: original setup/update state belongs
        # to the existing guide and its canonical continuation transactions.
        environment = {**os.environ, **inherited, "HOME": str(home), "PATH": "/usr/bin:/bin"}
        if platform.system() == "Darwin":
            # Keep guide protocol context, replacing selectors with canonical tools.
            environment = {key: item for key, item in environment.items()
                           if not key.startswith(("GIT_", "PYTHON", "PIP_", "BASH_FUNC_", "DYLD_", "LD_"))
                           and key not in {"BASH_ENV", "ENV", "SSL_CERT_FILE", "SSL_CERT_DIR",
                                           "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                                           "https_proxy", "http_proxy", "all_proxy"}}
            environment.update(_darwin_runtime().environment(admitted_darwin_profile()))
        return python, launcher, data, environment
    finally:
        os.close(lock)


def main(action, fd=0):
    operation = {"status": "owner-guide-status-v1", "api": "owner-api-v1", "computer": "owner-guide-computer-v1"}.get(action, "owner-guide-v1")
    deadline = time.monotonic() + 15
    def interrupted(_number, _frame):
        raise Refusal("inspection_unavailable")
    signal.signal(signal.SIGALRM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        if action not in {"setup", "status", "api", "computer"} or os.isatty(fd) or os.isatty(1):
            raise Refusal("invalid_request", 2)
        if action == "api" and not os.environ.get("SSH_CONNECTION"):
            # Presence only. The admitted CLI alone parses and binds the server
            # tuple; nothing here supplies, rewrites or defaults it.
            raise Refusal("ssh_source_missing")
        value = request(fd, deadline, action)
        python, launcher, data, environment = validate(value, deadline, action)
        # Exec the canonical stable launcher normally: its existing reader
        # lock, maintenance fence, parser and operator-context checks all run.
        signal.setitimer(signal.ITIMER_REAL, 0)
        route = (["api", "--stdio"] if action == "api" else
                 ["setup", {"status": "--stdio-status", "computer": "--stdio-computer"}.get(action, "--stdio")])
        os.execve(python, [str(python), "-I", "-S", "-B", str(launcher), "--root", str(data),
            "host", "owner", *route], environment)
    except Refusal as error:
        code, exit_code = error.code, error.exit_code
    except (Exception, KeyboardInterrupt):
        code, exit_code = "inspection_unavailable", 4
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    raw = wire({"schema_version": 1, "operation": operation, "state": "refused", "code": code, "retry": False})
    end = time.monotonic() + 5
    try:
        if select.select([], [1], [], max(0, end - time.monotonic()))[1]:
            os.write(1, raw)
    except OSError:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
# END EMBEDDED PYTHON
FIXED_OPERATION_PYTHON
if [ "$(/usr/bin/uname -s 2>/dev/null)" = Darwin ]; then
    unset BASH_ENV ENV
    exec /bin/bash /dev/fd/4 "$1" 4<<'DARWIN_OPERATION_LAUNCHER' 2>/dev/null
# BEGIN DARWIN EXISTING PYTHON SHELL
# Reuse this exact block in installer/guide; it consumes no standard input.
DARWIN_RUNTIME_PROFILE=darwin15-arm64-py31216-git2561-tmux38-jq182-v1
DARWIN_RUNTIME_PYTHON_SHA=54788b15120aa4f534da3f59b59f46658a9bfdc3d3d9c8b3af1459002e307c90
darwin_clean_loader_environment() {
    local selector
    for selector in "${!DYLD_@}" "${!LD_@}"; do
        [ -z "$selector" ] || unset "$selector" || return 6
    done
    unset PYTHONPATH PYTHONHOME BASH_ENV ENV
}
darwin_native() {
    local path="$1" actual="$1" facts mode
    if [ "$path" = /usr/bin/tar ] && [ -L "$path" ]; then
        [ "$(/usr/bin/readlink "$path" </dev/null)" = bsdtar ] || [ "$(/usr/bin/readlink "$path" </dev/null)" = /usr/bin/bsdtar ] || return 6
        actual=/usr/bin/bsdtar
    else
        [ ! -L "$path" ] || return 6
    fi
    [ -f "$actual" ] && [ -x "$actual" ] || return 6
    facts=$(/usr/bin/stat -L -f '%u:%Lp' "$actual" </dev/null) || return 6
    case "$facts" in 0:*) ;; *) return 6 ;; esac
    mode=${facts#*:}
    [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 8#022) == 0 )) || return 6
    /usr/bin/codesign --verify --strict -R '=anchor apple' "$actual" </dev/null >/dev/null 2>&1 || return 6
}
darwin_hash() {
    local output
    output=$(/usr/bin/openssl dgst -sha256 "$1" </dev/null) || return 6
    output=${output##* }
    [[ "$output" =~ ^[0-9a-f]{64}$ ]] || return 6
    printf '%s' "$output"
}
darwin_identity() {
    local expected_uid="$1" expected_account="${2-}" record key value native_uid native_account major
    darwin_clean_loader_environment || return 6
    for value in /usr/bin/stat /usr/bin/codesign /usr/bin/id /usr/bin/dscl /usr/bin/uname /usr/bin/sw_vers /usr/bin/openssl /usr/bin/false /usr/bin/env; do
        darwin_native "$value" || return 6
    done
    [ "$(/usr/bin/uname -s </dev/null)" = Darwin ] && [ "$(/usr/bin/uname -m </dev/null)" = arm64 ] || return 6
    major=$(/usr/bin/sw_vers -productVersion </dev/null) || return 6; major=${major%%.*}
    [[ "$major" =~ ^[0-9]{1,3}$ ]] && [ "$major" -ge 15 ] || return 6
    native_uid=$(/usr/bin/id -u </dev/null) || return 6; [ "$native_uid" != 0 ] && [ "$native_uid" = "$(/usr/bin/id -ru </dev/null)" ] && [ "$native_uid" = "$expected_uid" ] || return 6
    native_account=$(/usr/bin/id -un </dev/null) || return 6
    [[ "$native_account" =~ ^[A-Za-z_][A-Za-z0-9._-]{0,63}$ ]] || return 6
    [ -z "$expected_account" ] || [ "$native_account" = "$expected_account" ] || return 6
    DARWIN_RUNTIME_HOME=; DARWIN_RUNTIME_UID=; DARWIN_RUNTIME_ACCOUNT="$native_account"
    record=$(/usr/bin/dscl . -read "/Users/$native_account" NFSHomeDirectory UniqueID </dev/null) || return 6
    while IFS=' ' read -r key value; do
        case "$key" in NFSHomeDirectory:) DARWIN_RUNTIME_HOME="$value" ;; UniqueID:) DARWIN_RUNTIME_UID="$value" ;; *) return 6 ;; esac
    done <<< "$record"
    [ "$DARWIN_RUNTIME_UID" = "$native_uid" ] || return 6
    [[ "$DARWIN_RUNTIME_HOME" =~ ^/[A-Za-z0-9_./-]+$ ]] && [ "$DARWIN_RUNTIME_HOME" != / ] || return 6
    case "$DARWIN_RUNTIME_HOME/" in */../*|*/./*|*//*) return 6 ;; esac
    [ -d "$DARWIN_RUNTIME_HOME" ] && [ ! -L "$DARWIN_RUNTIME_HOME" ] || return 6
    [ "$(/usr/bin/stat -f %u "$DARWIN_RUNTIME_HOME" </dev/null)" = "$native_uid" ] || return 6
    local ancestor="$DARWIN_RUNTIME_HOME" facts perms
    while [ "$ancestor" != / ]; do
        [ -d "$ancestor" ] && [ ! -L "$ancestor" ] || return 6
        facts=$(/usr/bin/stat -f '%u:%Lp' "$ancestor" </dev/null) || return 6
        case "$facts" in "$native_uid":*|0:*) ;; *) return 6 ;; esac
        perms=${facts#*:}; [[ "$perms" =~ ^[0-7]{3,4}$ ]] && (( (8#$perms & 8#022) == 0 )) || return 6
        ancestor=${ancestor%/*}; [ -n "$ancestor" ] || ancestor=/
    done
    DARWIN_RUNTIME_BASE="$DARWIN_RUNTIME_HOME/.local/share/claudlobby-browser/runtime"
    DARWIN_RUNTIME_ROOT="$DARWIN_RUNTIME_BASE/profile"
}
darwin_python_identity() {
    local parent="$DARWIN_RUNTIME_PYTHON" facts mode
    while [ "$parent" != / ]; do
        [ ! -L "$parent" ] || return 6
        facts=$(/usr/bin/stat -f '%u:%Lp' "$parent" </dev/null) || return 6
        case "$facts" in "$DARWIN_RUNTIME_UID":*|0:*) ;; *) return 6 ;; esac
        mode=${facts#*:}; [[ "$mode" =~ ^[0-7]{3,4}$ ]] && (( (8#$mode & 8#022) == 0 )) || return 6
        parent=${parent%/*}; [ -n "$parent" ] || parent=/
    done
    [ -f "$DARWIN_RUNTIME_PYTHON" ] && [ -x "$DARWIN_RUNTIME_PYTHON" ] || return 6
    [ "$(/usr/bin/stat -f %l "$DARWIN_RUNTIME_PYTHON" </dev/null)" = 1 ] || return 6
    [ "$(darwin_hash "$DARWIN_RUNTIME_PYTHON")" = "$DARWIN_RUNTIME_PYTHON_SHA" ] || return 6
}
darwin_existing_python() {
    darwin_identity "$1" "${2-}" || return 6
    DARWIN_RUNTIME_PYTHON="$DARWIN_RUNTIME_ROOT/python/bin/python3.12"
    darwin_python_identity || return 6
    [ -f "$DARWIN_RUNTIME_ROOT/receipt.json" ] && [ ! -L "$DARWIN_RUNTIME_ROOT/receipt.json" ] || return 6
}
# END DARWIN EXISTING PYTHON SHELL
operation=owner-guide-v1
[ "$1" != status ] || operation=owner-guide-status-v1
[ "$1" != api ] || operation=owner-api-v1
[ "$1" != computer ] || operation=owner-guide-computer-v1
if ! darwin_existing_python "$(/usr/bin/id -u)"; then
    printf '{"code":"runtime_not_admitted","operation":"%s","retry":false,"schema_version":1,"state":"refused"}\n' "$operation"
    exit 6
fi
exec "$DARWIN_RUNTIME_PYTHON" -I -S -B /dev/fd/3 "$1" 4<&-
DARWIN_OPERATION_LAUNCHER
fi
if [ ! -x /usr/bin/python3 ]; then refuse prerequisite_missing; exit 6; fi
exec /usr/bin/python3 -I -S -B /dev/fd/3 "$1" 2>/dev/null
