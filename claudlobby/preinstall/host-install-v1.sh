#!/bin/sh
# Fixed regular-user preparation/status. No root/provider/lifecycle actions.
# Caller supplies exactly one canonical ASCII JSON line (<=512 bytes), then EOF:
# expected_uid, schema_version=1, source_revision=reviewed lowercase 40-hex pin.
# A future authenticated transport hardcodes action/pin and validates the entire
# single <=8192-byte result plus exit status; the current browser door cannot
# carry this operation. Budgets: prepare 900s, status 15s including child drain.
# recorded_complete is retained preparation evidence, never fresh runtime or
# dependency readiness. Status alone reports recorded_in_use for that same
# verified preparation once this product's own setup has added to it; it reads
# none of that later state, and prepare still refuses it rather than adopt or
# replace anything. After unknown dispatch, inspect only; absent is an
# observation and cannot authorize an automatic retry or prove remote shutdown.
# The update actions (update-status, update, update-continue) read and move an
# installation this script prepared: one canonical line naming expected_uid and
# the action's own keys; their answers carry the action's own names.
operation=host-install-v1
if [ "$#" -eq 1 ]; then
    case "$1" in
        status) operation=host-install-status-v1 ;;
        update-status) operation=host-update-status-v1 ;;
        update) operation=host-update-v1 ;;
        update-continue) operation=host-update-continue-v1 ;;
    esac
fi
refuse() {
    case "$operation" in
        host-install-v1|host-install-status-v1)
            printf '{"account_uid":null,"activated":false,"binding_sha256":null,"code":"%s","next":"%s","operation":"%s","prepared":false,"schema_version":1,"source_revision":null,"state":"%s"}\n' "$1" "$2" "$operation" "$3" ;;
        *) case "$1" in invalid_request) ;; *) set -- unavailable "$2" unavailable ;; esac
           printf '{"code":"%s","operation":"%s","schema_version":1,"state":"%s"}\n' "$1" "$operation" "$3" ;;
    esac
}
if [ "$#" -ne 1 ]; then refuse invalid_request stop refused; exit 2; fi
case "$1" in prepare|status|update-status|update|update-continue) ;; *) refuse invalid_request stop refused; exit 2 ;; esac
exec 3<<'FIXED_OPERATION_PYTHON'
# BEGIN EMBEDDED PYTHON
# Embedded stdlib operation. The shell selects prepare, status or an update action.
import sys
import time
STARTED = time.monotonic()
import os
import pwd
import stat
import json
import re
import hashlib
import platform
import fcntl
import signal
import selectors
import subprocess
import tempfile
import shutil
import configparser
from pathlib import Path

REMOTE = "https://github.com/Claudfather/Claudlobby.git"
FIELDS = {"schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter",
          "interpreter_sha256", "revision", "lock_sha256", "environment_owned", "complete"}
GIT_FLAGS = ["--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "credential.helper=", "-c", "core.askPass=/bin/false", "-c", "http.sslVerify=true",
             "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
BUDGETS = {"prepare": 900, "status": 15, "update-status": 15, "update": 900, "update-continue": 900}
UPDATE_ACTIONS = {"update-status": "host-update-status-v1", "update": "host-update-v1",
                  "update-continue": "host-update-continue-v1"}
# Required, then optional, request keys of each update action.
UPDATE_KEYS = {"update-status": ({"schema_version", "expected_uid"}, {"compare_revision"}),
               "update": ({"schema_version", "expected_uid", "expected_installed_revision", "target_revision"}, set()),
               "update-continue": ({"schema_version", "expected_uid", "request_id"}, set())}
# The updater's exit, before any dispatch, for an undispatched team preparation:
# continue --expected-request, and apply for that team's own target.
NOT_CONTINUABLE_EXIT = 7
# Each update action's refusals; any other code is answered as unavailable (exit 6),
# and a malformed request as invalid_request (exit 2).
UPDATE_REFUSALS = {"update-status": set(),
                   "update": {"not_prepared", "installed_changed", "already_current", "update_in_progress",
                              "not_forward", "needs_reset"},
                   "update-continue": {"not_prepared", "no_such_request", "nothing_to_continue", "not_continuable"}}
SHA = re.compile(r"[0-9a-f]{40}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_CHILD_OUTPUT = 2 * 1024 * 1024
# The fixed directories this product's own setup guide adds beside a completed
# preparation (commands/owner_setup_consent.py: policy, provider, account).
SETUP_FAMILY = {"host-policy", "provider-bin", "provider-profile"}


class Refusal(Exception):
    def __init__(self, code, state="refused", next_step="stop", exit_code=4):
        self.code, self.state, self.next_step, self.exit_code = code, state, next_step, exit_code


def unavailable(code="inspection_unavailable"):
    return Refusal(code, "unavailable", "inspect", 6)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def request(fd, deadline, action=None):
    raw = bytearray()
    limit = min(deadline - 4, time.monotonic() + 5)
    import select
    while True:
        remaining = limit - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise Refusal("invalid_request", exit_code=2)
        part = os.read(fd, 513 - len(raw))
        if not part:
            break
        raw.extend(part)
        if len(raw) > 512:
            raise Refusal("invalid_request", exit_code=2)
    try:
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1 or any(c < 32 or c > 126 for c in raw[:-1]):
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=unique)
        if action in UPDATE_ACTIONS:
            if (not update_request(action, value)
                    or raw != (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")):
                raise ValueError()
            return value
        if (not isinstance(value, dict) or set(value) != {"schema_version", "source_revision", "expected_uid"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["source_revision"], str) or not SHA.fullmatch(value["source_revision"])
                or type(value["expected_uid"]) is not int or not 0 < value["expected_uid"] < 2**32 - 1
                or raw != (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise Refusal("invalid_request", exit_code=2) from None


def update_request(action, value):
    required, optional = UPDATE_KEYS[action]
    return (isinstance(value, dict) and required <= set(value) <= required | optional
            and type(value["schema_version"]) is int and value["schema_version"] == 1
            and type(value["expected_uid"]) is int and 0 < value["expected_uid"] < 2**32 - 1
            and all(isinstance(value[key], str) and SHA.fullmatch(value[key]) for key in value
                    if key in {"compare_revision", "expected_installed_revision", "target_revision"})
            and ("request_id" not in value or isinstance(value["request_id"], str)
                 and UUID.fullmatch(value["request_id"]) is not None))


def plain_file(path, limit, mode=0o600, owner=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != (os.geteuid() if owner is None else owner)
                or info.st_nlink != 1 or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
                or info.st_mode & 0o022 or info.st_size > limit):
            raise Refusal("existing_installation", next_step="inspect")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise Refusal("existing_installation", next_step="inspect")
        return raw
    finally:
        os.close(fd)


def read_json(path, limit=65536):
    try:
        return json.loads(plain_file(path, limit), object_pairs_hook=unique)
    except (ValueError, TypeError, UnicodeError):
        raise Refusal("existing_installation", next_step="inspect") from None


def directory(path, private=False, create=False):
    if not path.exists() and not path.is_symlink():
        if not create:
            return False
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022
            or (private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700))):
        raise Refusal("existing_installation", next_step="inspect")
    return True


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
    uid = os.geteuid()
    if uid == 0 or uid != os.getuid() or uid != expected_uid:
        raise Refusal("account_changed")
    home = Path(pwd.getpwuid(uid).pw_dir)
    if not home.is_absolute() or home.resolve() != home:
        raise Refusal("existing_installation")
    for parent in reversed((home, *home.parents)):
        directory(parent)
    if home.stat().st_uid != uid:
        raise Refusal("existing_installation")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64", "arm64"}:
        raise Refusal("unsupported_platform")
    release = Path("/etc/os-release").resolve()
    fields = {}
    for line in plain_file(release, 8192, mode=None, owner=0).decode("ascii").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip("\"")
    if fields.get("ID") != "ubuntu" or fields.get("VERSION_ID") != "24.04":
        raise Refusal("unsupported_platform")
    return home


def independent_python():
    python = Path(sys.executable).resolve()
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if python != profile.python or sys.prefix != sys.base_prefix:
            raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
        return python
    folder = Path("/usr/bin").lstat()
    if (sys.implementation.name != "cpython" or sys.version_info < (3, 10)
            or sys.prefix != sys.base_prefix or python.parent != Path("/usr/bin")
            or not stat.S_ISDIR(folder.st_mode) or folder.st_uid != 0 or folder.st_mode & 0o022):
        raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
    info = python.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or not os.access(python, os.X_OK)):
        raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
    return python


def fixed_environment(home):
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if profile.home != home:
            raise Refusal("account_changed")
        environment = _darwin_runtime().environment(profile)
        return {**environment, "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1",
                "PLANE_EMIT_DISABLED": "1", "TMPDIR": str(home / ".local/share/claudlobby-browser/staging")}
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PLANE_EMIT_DISABLED": "1",
            "TMPDIR": str(home / ".local/share/claudlobby-browser/staging")}


def update_environment(home):
    # The update actions' own map, built fresh: the guide script's pinned
    # selectors (owner-guide-v1.sh selectors), so a render reads this
    # computer's own settings, and on Linux the two variables the account's
    # user manager needs, as the guide's profile sets them. Never PLANE_EMIT_DISABLED: a team
    # continued here runs its identity scan in this process.
    family = home / ".local/share/claudlobby-browser"
    selected = {"CLAUDLOBBY_ROOT": str(family / "data"), "CLAUDLOBBY_INSTALLATION": str(family / "install"),
                "CLAUDLOBBY_HOST_SYSTEM_YAML": str(family / "host-policy/system.yaml"),
                "CLAUDE_CONFIG_DIR": str(family / "provider-profile"), "TMPDIR": str(family / "staging"),
                "LANG": "C.UTF-8", "TERM": "dumb", "PYTHONNOUSERSITE": "1"}
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if profile.home != home:
            raise Refusal("account_changed")
        managed = _darwin_runtime().environment(profile)
        return {**{key: item for key, item in managed.items() if key != "PLANE_EMIT_DISABLED"}, **selected}
    uid = str(os.getuid())
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), **selected,
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_RUNTIME_DIR": "/run/user/" + uid, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + uid + "/bus",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1"}


class Runner:
    """One owner of finite child groups; the canonical flock follows children."""
    def __init__(self, deadline, descriptors=()):
        self.deadline, self.descriptors = deadline, tuple(descriptors)
        self.active, self.drained = {}, True

    def remaining(self):
        value = self.deadline - time.monotonic() - 4
        if value <= 0:
            raise unavailable()
        return value

    def stop(self, process):
        # The group was created by this Popen, never discovered from a PID file.
        for number, delay in ((signal.SIGTERM, 1), (signal.SIGKILL, 2)):
            try:
                os.killpg(process.pid, number)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            end = time.monotonic() + delay
            while time.monotonic() < end:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # Some kernels report EPERM for an unreaped dying group.
                    # This is not absence: keep polling/reaping within the bound.
                    pass
                time.sleep(0.02)
            else:
                continue
            break
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self.drained = False
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            self.drained = False
        else:
            self.drained = False

    def __call__(self, arguments, *, env, timeout=300):
        end = time.monotonic() + min(timeout, self.remaining())
        process = subprocess.Popen([str(x) for x in arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/", env=env,
            pass_fds=self.descriptors, start_new_session=True)
        self.active[process.pid] = process
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        for name in buffers:
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        try:
            while selector.get_map() or process.poll() is None:
                left = min(end - time.monotonic(), self.remaining())
                if left <= 0:
                    raise unavailable()
                for key, _events in selector.select(min(left, 0.2)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        output = buffers[key.data]
                        if len(output) + len(block) > MAX_CHILD_OUTPUT:
                            raise unavailable()
                        output.extend(block)
            result = subprocess.CompletedProcess(arguments, process.returncode,
                        bytes(buffers["stdout"]), bytes(buffers["stderr"]))
            return result
        finally:
            selector.close()
            # Catch a child that exited but left same-group descendants behind.
            self.stop(process)
            for name in buffers:
                getattr(process, name).close()
            self.active.pop(process.pid, None)
            if not self.drained:
                raise unavailable()

    def close(self):
        for process in list(self.active.values()):
            self.stop(process)
        self.active.clear()


def safe_git_config(source):
    path = source / ".git/config"
    raw = plain_file(path, 65536, mode=None)
    parser = configparser.RawConfigParser(strict=True)
    try:
        parser.read_string(raw.decode("utf-8"))
        allowed = {"repositoryformatversion": {"0"}, "filemode": {"true"}, "bare": {"false"},
                   "logallrefupdates": {"true"}, "ignorecase": {"true", "false"},
                   "precomposeunicode": {"true", "false"}}
        values = dict(parser.items("core"))
        if (parser.sections() != ["core"] or parser.defaults() or not values
                or any(key not in allowed or value not in allowed[key] for key, value in values.items())):
            raise ValueError()
    except (ValueError, UnicodeError, configparser.Error):
        raise Refusal("existing_installation", next_step="inspect") from None
    if any((source / ".git" / name).exists() for name in ("commondir", "objects/info/alternates")):
        raise Refusal("existing_installation", next_step="inspect")


def git(runner, env, source, *arguments, check=True):
    if (source / ".git").exists():
        directory(source / ".git", private=True)
        safe_git_config(source)
        count = 0
        for root, directories, files in os.walk(source / ".git", followlinks=False):
            for name in directories + files:
                path = Path(root) / name
                count += 1
                if (count > 10000 or path.is_symlink() or name.endswith(".lock")
                        or (Path(root).name == "pack" and name.startswith("tmp_"))):
                    raise Refusal("existing_installation", next_step="inspect")
    binary, flags = git_tool()
    value = runner([str(binary), *flags, "-C", str(source), *arguments], env=env,
                   timeout=15 if arguments[0] != "fetch" else 300)
    if check and value.returncode:
        raise unavailable()
    return value


def git_tool():
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        return profile.git, ["core.askPass=/usr/bin/false" if item == "core.askPass=/bin/false" else item for item in GIT_FLAGS]
    return Path("/usr/bin/git"), GIT_FLAGS


def git_available():
    if platform.system() == "Darwin":
        admitted_darwin_profile()
        return
    path = Path("/usr/bin/git").resolve()
    if not path.is_file():
        raise Refusal("prerequisite_missing", "unavailable", exit_code=6)
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)


def binding(install, data, revision, python):
    return {"schema": 1, "purpose": "installation-preparation-only", "installation": str(install),
            "data_root": str(data), "remote": REMOTE, "ref": revision, "interpreter": str(python),
            "interpreter_sha256": digest(python)}


def bound(expected):
    return hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()


def receipt(path, expected):
    value = read_json(path)
    # The managed Mac Python keeps its byte pin. The Ubuntu /usr/bin Python is
    # admitted by independent_python instead: security updates replace it, so
    # its bytes are not compared and inspect keeps the digest recorded here.
    pinned = {key: item for key, item in expected.items()
              if key != "interpreter_sha256" or platform.system() == "Darwin"}
    if (not isinstance(value, dict) or set(value) != FIELDS
            or not isinstance(value["interpreter_sha256"], str) or not DIGEST.fullmatch(value["interpreter_sha256"])
            or any(value.get(key) != item for key, item in pinned.items())
            or type(value["schema"]) is not int or type(value["complete"]) is not bool
            or type(value["environment_owned"]) is not bool
            or value["revision"] not in (None, expected["ref"])
            or (value["lock_sha256"] is not None and
                (not isinstance(value["lock_sha256"], str) or not DIGEST.fullmatch(value["lock_sha256"])))
            or (value["revision"] is None and (value["complete"] or value["environment_owned"]
                                              or value["lock_sha256"] is not None))
            or (value["environment_owned"] and value["lock_sha256"] is None)
            or (value["complete"] and not value["environment_owned"])):
        raise Refusal("identity_changed", next_step="inspect")
    return value


def lock_open(path, create, exclusive):
    flags = os.O_RDWR if exclusive else os.O_RDONLY
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0), 0o600)
    try:
        info, actual = os.fstat(descriptor), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size
                or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino)):
            raise Refusal("existing_installation", next_step="inspect")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("preparation_in_progress", "busy", "inspect") from None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def data_check(data, *, complete=False):
    if not directory(data, private=True):
        if complete:
            raise Refusal("identity_changed", next_step="inspect")
        return
    if {p.name for p in data.iterdir()} - {"state"}:
        raise Refusal("existing_installation", next_step="inspect")
    state = data / "state"
    if state.exists() or state.is_symlink():
        directory(state, private=True)
        if {p.name for p in state.iterdir()} - {"runtime-use.lock"}:
            raise Refusal("existing_installation", next_step="inspect")
        runtime = state / "runtime-use.lock"
        if runtime.exists() or runtime.is_symlink():
            descriptor = lock_open(runtime, False, True)
            os.close(descriptor)
        elif complete:
            raise Refusal("identity_changed", next_step="inspect")
    elif complete:
        raise Refusal("identity_changed", next_step="inspect")


def in_use_data_check(data):
    # Setup has gone on since preparation. The skeleton that completion created
    # must still be there, checked by metadata alone; what setup and the app
    # have added under it is theirs and is neither read nor locked here.
    state = data / "state"
    if not directory(data, private=True) or not directory(state, private=True):
        raise Refusal("identity_changed", next_step="inspect")
    try:
        info = (state / "runtime-use.lock").lstat()
    except FileNotFoundError:
        raise Refusal("identity_changed", next_step="inspect") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size):
        raise Refusal("existing_installation", next_step="inspect")


def paths(home, create=False, in_use=False):
    for path in (home / ".local", home / ".local/share"):
        if not directory(path, create=create):
            return None
    family = home / ".local/share/claudlobby-browser"
    if not directory(family, private=True, create=create):
        return None
    allowed = {"install", "data", "staging"}
    if platform.system() == "Darwin":
        profile = admitted_darwin_profile()
        if profile.root != family / "runtime/profile":
            raise Refusal("identity_changed", next_step="inspect")
        allowed.add("runtime")
    later = {p.name for p in family.iterdir()} - allowed
    # Status alone may see the fixed directories setup adds beside a completed
    # preparation. Prepare refuses them, and any other name refuses for both.
    if later - (SETUP_FAMILY if in_use else set()):
        raise Refusal("existing_installation", next_step="inspect")
    for name in later:
        directory(family / name, private=True)
    install, data, staging = (family / name for name in ("install", "data", "staging"))
    for path in (install, data, staging):
        directory(path, private=True, create=create)
    return install, data, staging


def setup_started(family, data, staging):
    # Names only: anything beyond the inactive skeleton a preparation leaves.
    # The state directory is admitted before it is listed, so a link is never followed.
    state = data / "state"
    return bool(names(family) & SETUP_FAMILY or names(staging) or names(data) - {"state"}
                or directory(state, private=True) and names(state) - {"runtime-use.lock"})


def contents_check(install, data, staging):
    data_check(data)
    if staging.exists() and any(staging.iterdir()):
        raise Refusal("existing_installation", next_step="inspect")


def claim_paths(home):
    for path in (home / ".local", home / ".local/share", home / ".local/share/claudlobby-browser",
                 home / ".local/share/claudlobby-browser/install"):
        directory(path, private="claudlobby-browser" in path.parts, create=True)


def names(path):
    return {p.name for p in path.iterdir()} if path.exists() else set()


def source_check(source, expected, runner, env, incomplete=False):
    directory(source, private=True)
    if not directory(source / ".git", private=True):
        raise Refusal("source_mismatch", next_step="inspect")
    git_available()
    top = git(runner, env, source, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if top != str(source):
        raise Refusal("source_mismatch")
    head = git(runner, env, source, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if head.returncode:
        if not incomplete:
            raise Refusal("source_mismatch")
    elif head.stdout.decode().strip() != expected:
        raise Refusal("source_mismatch")
    if git(runner, env, source, "status", "--porcelain=v1", "--untracked-files=all",
           "--ignore-submodules=none").stdout:
        raise Refusal("source_mismatch")
    if git(runner, env, source, "rev-parse", "--verify", "HEAD^{commit}", check=False).stdout != head.stdout:
        raise Refusal("source_mismatch")


def startup_files_safe(environment):
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
                    or digest(path) != "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224"):
                return False
    return True


def environment_check(environment, python, runner, env):
    # Read identity metadata and the independently pinned interpreter only.
    # Never start managed Python/site initialization merely to inspect status.
    directory(environment, private=True)
    directory(environment / "bin", private=True)
    entry = environment / "bin/python"
    if not entry.exists() or entry.resolve() != python:
        raise Refusal("identity_changed", next_step="inspect")
    config = {}
    for line in plain_file(environment / "pyvenv.cfg", 8192, mode=None).decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            if key.strip() in config:
                raise Refusal("identity_changed", next_step="inspect")
            config[key.strip()] = value.strip()
    if (config.get("home") != str(python.parent) or config.get("include-system-site-packages") != "false"
            or not config.get("version", "").startswith(str(sys.version_info.major) + "." + str(sys.version_info.minor) + ".")
            or ("executable" in config and Path(config["executable"]).resolve() != python)):
        raise Refusal("identity_changed", next_step="inspect")
    if (environment / "pip.conf").exists() or (environment / "pip.conf").is_symlink():
        raise Refusal("existing_installation", next_step="inspect")


def inspect(install, data, expected, python, runner, env, in_use=False):
    allowed = {"preparation.lock", "preparation.json", "installation.json", "src", "env", "bin", "cache"}
    if {p.name for p in install.iterdir()} - allowed:
        raise Refusal("existing_installation", next_step="inspect")
    record = install / "preparation.json"
    if not record.exists() and not record.is_symlink():
        if in_use or {p.name for p in install.iterdir()} - {"preparation.lock"} or names(data):
            raise Refusal("existing_installation", next_step="inspect")
        return None
    state = receipt(record, expected)
    # The binding reported and resumed is the one recorded at preparation.
    expected["interpreter_sha256"] = state["interpreter_sha256"]
    if in_use:
        # Later state beside anything but this exact completed preparation is
        # unknown: never adopted, and never read as fresh or incomplete.
        if not state["complete"]:
            raise Refusal("existing_installation", next_step="inspect")
        in_use_data_check(data)
    else:
        # Completion requires the original inactive launcher skeleton. Inspect it
        # before prepare can create any missing data/staging paths. Never repair
        # completed state loss or reinterpret it as a fresh/incomplete preparation.
        data_check(data, complete=state["complete"])
    identity = read_json(install / "installation.json", 4096)
    if identity != {"schema": 1, "data_root": str(data)} or type(identity.get("schema")) is not int:
        raise Refusal("identity_changed", next_step="inspect")
    source = install / "src"
    if source.exists() or source.is_symlink():
        if (not (source / ".git").exists() and not (source / ".git").is_symlink()
                and state["revision"] is None and directory(source, private=True) and not names(source)):
            pass
        else:
            source_check(source, expected["ref"], runner, env, incomplete=not state["complete"])
    elif state["revision"] is not None:
        raise Refusal("source_mismatch", next_step="inspect")
    if state["lock_sha256"] is not None:
        content = git(runner, env, source, "show", expected["ref"] + ":requirements-runtime.lock").stdout
        if (hashlib.sha256(content).hexdigest() != state["lock_sha256"]
                or plain_file(source / "requirements-runtime.lock", 1024 * 1024, mode=None) != content):
            raise Refusal("identity_changed", next_step="inspect")
    cache = install / "cache"
    if cache.exists() or cache.is_symlink():
        directory(cache, private=True)
        if names(cache) - {"bootstrap-wheels", "bootstrap-requirements.lock"} or state["lock_sha256"] is None:
            raise Refusal("existing_installation", next_step="inspect")
        frozen = cache / "bootstrap-requirements.lock"
        if frozen.exists() or frozen.is_symlink():
            if hashlib.sha256(plain_file(frozen, 1024 * 1024)).hexdigest() != state["lock_sha256"]:
                raise Refusal("identity_changed", next_step="inspect")
        wheels = cache / "bootstrap-wheels"
        if wheels.exists() or wheels.is_symlink():
            directory(wheels, private=True)
            for wheel in wheels.iterdir():
                if wheel.suffix != ".whl":
                    raise Refusal("existing_installation", next_step="inspect")
                # Metadata only; dependency hashes were checked by preparation.
                info = wheel.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_nlink != 1 or info.st_mode & 0o022):
                    raise Refusal("existing_installation", next_step="inspect")
    environment = install / "env"
    if environment.exists() or environment.is_symlink():
        if not state["environment_owned"]:
            raise Refusal("existing_installation", next_step="inspect")
        environment_check(environment, python, runner, env)
        if not state["complete"] and not startup_files_safe(environment):
            raise Refusal("existing_installation", next_step="inspect")
    elif state["complete"]:
        raise Refusal("identity_changed", next_step="inspect")
    entries = install / "bin"
    if entries.exists() or entries.is_symlink():
        directory(entries, private=True)
        if {p.name for p in entries.iterdir()} - {"claudlobby", "claudlobby-update"}:
            raise Refusal("existing_installation", next_step="inspect")
        for name, module in (("claudlobby", "installation.py"), ("claudlobby-update", "git_update.py")):
            path = entries / name
            if path.exists() or path.is_symlink():
                if plain_file(path, 1024 * 1024, 0o755) != ("#!" + str(python) + "\n").encode() + plain_file(source / "claudlobby" / module, 1024 * 1024, mode=None):
                    raise Refusal("identity_changed", next_step="inspect")
            elif state["complete"]:
                raise Refusal("identity_changed", next_step="inspect")
    elif state["complete"]:
        raise Refusal("identity_changed", next_step="inspect")
    return state


def stage_and_prepare(install, data, staging, expected, python, runner, env, lock):
    temporary = Path(tempfile.mkdtemp(prefix="prepare-", dir=staging))
    identity = temporary.stat()
    try:
        probe = temporary / "probe"
        value = runner([str(python), "-I", "-m", "venv", str(probe)], env=env)
        if value.returncode:
            raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
        value = runner([str(probe / "bin/python"), "-I", "-B", "-c",
            "import ssl,encodings,plistlib,pip,sys; assert sys.prefix != sys.base_prefix"], env=env)
        if value.returncode:
            raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
        source = temporary / "source"
        source.mkdir(mode=0o700)
        git(runner, env, source, "init", "--quiet", ".")
        git(runner, env, source, "fetch", "--no-tags", "--no-recurse-submodules", "--", REMOTE, expected["ref"])
        fetched = git(runner, env, source, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.decode().strip()
        if fetched != expected["ref"]:
            raise Refusal("source_mismatch", next_step="inspect")
        git(runner, env, source, "checkout", "--detach", fetched)
        source_check(source, expected["ref"], runner, env)
        # Import only after exact Git pin, plain config and clean-tree checks.
        if any(name == "claudlobby" or name.startswith("claudlobby.") for name in sys.modules):
            raise Refusal("identity_changed", next_step="inspect")
        sys.path.insert(0, str(source))
        try:
            from claudlobby.bootstrap import prepare_git_installation
            prepare_git_installation(install, data, remote=REMOTE, ref=expected["ref"],
                interpreter=python, _preparation_lock_fd=lock, _browser_runner=runner)
        finally:
            sys.path.pop(0)
    finally:
        if runner.drained and temporary.exists():
            current = temporary.lstat()
            if (not stat.S_ISDIR(current.st_mode) or current.st_uid != os.geteuid()
                    or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)):
                raise Refusal("existing_installation", next_step="inspect")
            shutil.rmtree(temporary)


def execute(action, value, deadline):
    descriptors = []
    runner = Runner(deadline)
    uid, revision, binding_hash, expected = None, None, None, None
    try:
        home = account_profile(value["expected_uid"])
        uid, revision = value["expected_uid"], value["source_revision"]
        python = independent_python()
        env = fixed_environment(home)
        family = home / ".local/share/claudlobby-browser"
        install, data, staging = (family / name for name in ("install", "data", "staging"))
        foreign = shutil.which("claudlobby", path=os.environ.get("PATH", ""))
        if foreign and Path(foreign) != install / "bin/claudlobby":
            raise Refusal("existing_installation")
        current = paths(home, in_use=action == "status")
        expected = binding(install, data, revision, python)
        binding_hash = bound(expected)
        if action == "status" and (current is None or not (install / "preparation.lock").exists()):
            if current is not None and (names(install) or names(data) or names(staging)
                                        or names(family) & SETUP_FAMILY):
                raise Refusal("existing_installation", next_step="inspect")
            return "absent", "none", "inspect", False, revision, uid, binding_hash
        if action == "prepare":
            git_available()
            try:
                import ssl
                import venv
                import ensurepip
            except ImportError:
                raise Refusal("prerequisite_missing", "unavailable", exit_code=6) from None
            claim_paths(home)
        lock = lock_open(install / "preparation.lock", action == "prepare", action == "prepare")
        descriptors.append(lock)
        runner.descriptors = tuple(descriptors)
        # Only lock-path ancestors precede this flock. Staging/data creation
        # and all network or child work stay under its exclusive ownership.
        paths(home, in_use=action == "status")
        # Read-only status tells a completed preparation that setup has since
        # used from unknown state. Prepare keeps requiring the untouched skeleton.
        in_use = action == "status" and setup_started(family, data, staging)
        if not in_use:
            contents_check(install, data, staging)
        state = inspect(install, data, expected, python, runner, env, in_use=in_use)
        binding_hash = bound(expected)
        if action == "status":
            if state is None:
                return "absent", "none", "inspect", False, revision, uid, binding_hash
            if state["complete"]:
                return ("recorded_in_use" if in_use else "recorded_complete", "none", "continue_onboarding", True,
                        revision, uid, binding_hash)
            return "incomplete", "preparation_incomplete", "resume_same", False, revision, uid, binding_hash
        if state is None or not state["complete"]:
            directory(data, private=True, create=True)
            directory(staging, private=True, create=True)
            stage_and_prepare(install, data, staging, expected, python, runner, env, lock)
            observed = inspect(install, data, expected, python, runner, env)
            if observed is None or not observed["complete"]:
                raise unavailable("preparation_incomplete")
            binding_hash = bound(expected)
        return "prepared", "none", "continue_onboarding", True, revision, uid, binding_hash
    except Refusal as error:
        error.identity = (revision, uid, binding_hash if expected is None else bound(expected))
        raise
    except Exception:
        error = Refusal("preparation_refused", "refused", "inspect")
        error.identity = (revision, uid, binding_hash if expected is None else bound(expected))
        raise error from None
    finally:
        runner.close()
        for descriptor in reversed(descriptors):
            os.close(descriptor)


BINDING_KEYS = ("schema", "purpose", "installation", "data_root", "remote", "ref", "interpreter",
                "interpreter_sha256")
PHASES = {"prepare", "quiesce", "git", "environment", "publish", "migrate", "commit", "restore", "done"}
# The updater's own stages, a stage error names one of them; the stages that work
# through units and the field a finished unit sets; how a job run ended.
STAGES = {"authorize", "retire-render", "render", "prepare", "conversion-check", "quiesce", "quiet", "publish",
          "restore"}
UNIT_FIELDS = {"quiesce": "stop", "restore": "restore"}
RUNNERS = {"pending", "entered", "finished"}
OUTCOMES = {"finished", "unfinished", "refused"}


def installed(home, python, runner, env):
    # A completed preparation as setup and the updater have left it. Its receipt
    # is checked against itself, never against a revision a client sent; the
    # installed revision is whatever the updater moved the checkout to. Holds
    # the preparation lock shared for these reads only.
    family = home / ".local/share/claudlobby-browser"
    install, data, staging = (family / name for name in ("install", "data", "staging"))
    current = paths(home, in_use=True)
    if current is None or not (install / "preparation.lock").exists():
        if current is not None and (names(install) or names(data) or names(staging) or names(family) & SETUP_FAMILY):
            raise Refusal("existing_installation", next_step="inspect")
        return None
    lock = lock_open(install / "preparation.lock", False, False)
    try:
        paths(home, in_use=True)
        record = install / "preparation.json"
        if not record.exists() and not record.is_symlink():
            raise Refusal("not_prepared")
        value = read_json(record)
        if not isinstance(value, dict) or not isinstance(value.get("ref"), str) or not SHA.fullmatch(value["ref"]):
            raise Refusal("identity_changed", next_step="inspect")
        state = receipt(record, binding(install, data, value["ref"], python))
        if not state["complete"]:
            raise Refusal("not_prepared")
        touched = setup_started(family, data, staging)
        if touched:
            in_use_data_check(data)
        else:
            data_check(data, complete=True)
        if read_json(install / "installation.json", 4096) != {"schema": 1, "data_root": str(data)}:
            raise Refusal("identity_changed", next_step="inspect")
        source = install / "src"
        directory(source, private=True)
        if not directory(source / ".git", private=True):
            raise Refusal("source_mismatch", next_step="inspect")
        git_available()
        head = git(runner, env, source, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
        if not SHA.fullmatch(head):
            raise Refusal("source_mismatch", next_step="inspect")
        directory(install / "bin", private=True)
        for name in ("claudlobby", "claudlobby-update"):
            if not plain_file(install / "bin" / name, 1024 * 1024, 0o755).startswith(("#!" + str(python) + "\n").encode()):
                raise Refusal("identity_changed", next_step="inspect")
        setup = ("started" if os.path.lexists(data / "state/config.json") else "in_use" if touched else "untouched")
        return {"receipt": state, "head": head, "setup": setup, "install": install, "data": data, "source": source,
                "binding": bound({key: state[key] for key in BINDING_KEYS}),
                "interpreter": "same" if digest(python) == state["interpreter_sha256"] else "changed"}
    finally:
        os.close(lock)


def progress(record, dispatch, now):
    # Where the update is, from the record alone and this computer's clock; nothing
    # looks at the native job. A key the updater did not write, or wrote in another
    # shape, reads as null or false and refuses nothing.
    error = record.get("stage_error")
    named = error.get("stage") if isinstance(error, dict) else None
    stage = None if not error else named if isinstance(named, str) and named in STAGES else "other"
    units, field, consumers = None, UNIT_FIELDS.get(record["phase"]), record.get("consumers")
    if field and isinstance(consumers, dict) and consumers:
        # A unit with a restore error has not finished: the updater's own count.
        units = {"done": sum(isinstance(row, dict) and row.get(field) == "done" and "restore_error" not in row
                             for row in consumers.values()), "total": len(consumers)}
    state = dispatch.get("state") if isinstance(dispatch, dict) else None
    attempt = dispatch.get("attempt") if isinstance(dispatch, dict) else None
    clock = record.get("job_clock") if isinstance(record.get("job_clock"), dict) else {}
    def age(key):
        at = clock.get(key)
        return max(0, int(now) - at) if type(at) is int else None
    outcome = record.get("job_outcome")
    # Only the outcome of the current dispatch speaks for it; with no dispatch, none does.
    outcome = (outcome["result"] if isinstance(outcome, dict) and isinstance(outcome.get("result"), str)
               and outcome["result"] in OUTCOMES and type(attempt) is int and outcome.get("attempt") == attempt
               else None)
    waits = record.get("runtime_waits")
    latest = waits[-1] if isinstance(waits, list) and waits else None
    errors = record.get("health_errors") or {}
    return {"stage": stage, "units": units, "runner": state if isinstance(state, str) and state in RUNNERS else "none",
            "pending_seconds": age("pending_at"), "entered_seconds": age("entered_at"), "outcome": outcome,
            "waiting": isinstance(record.get("runtime_wait"), dict),
            "runtime_busy": isinstance(latest, dict) and latest.get("outcome") == "refused",
            "unhealthy_units": len(errors)}


def summary(record, now=None):
    # The update record as `host update status --json` prints it, in the names a client reads.
    if record is None:
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("request_id"), str)
            or not UUID.fullmatch(record["request_id"]) or record.get("phase") not in PHASES
            or not isinstance(record.get("target"), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["target"])
            or type(record.get("maintenance")) is not bool or not isinstance(record.get("health_errors") or {}, dict)):
        raise Refusal("unavailable", "unavailable", "inspect", 6)
    scope, dispatch = record.get("initialization_scope"), record.get("job_dispatch")
    kind = ("addition" if record.get("addition_scope") is not None else "update" if not isinstance(scope, dict)
            else "first_team" if scope.get("manifests") else "start")
    errors = record.get("health_errors") or {}
    found = progress(record, dispatch, time.time() if now is None else now)
    # Stopped, by the record alone: a job that ended or was refused before the
    # update was done, a stage error, or done with health errors.
    failed = bool(record.get("stage_error") or record["phase"] == "done" and errors
                  or isinstance(dispatch, dict) and dispatch.get("state") == "finished" and record["phase"] != "done"
                  or found["outcome"] in {"unfinished", "refused"} and record["phase"] != "done")
    return {"request_id": record["request_id"], "kind": kind, "phase": record["phase"], "target": record["target"],
            "maintenance": record["maintenance"], "dispatched": dispatch is not None, "failed": failed,
            "health_errors": sorted(errors), **found}


def unfinished(update):
    # Core's own rule: not done, or done with health errors.
    return update is not None and (update["phase"] != "done" or bool(update["health_errors"]))


def undispatched_team(update):
    # A Preview, a prepared first team or a prepared addition: only a Launch dispatches it.
    return update["kind"] in {"first_team", "addition"} and not update["dispatched"]


def update_refusal(update, head, shown, target, started):
    if head != shown:
        return "installed_changed"
    if unfinished(update):
        # The same target in flight is asked for again: apply attaches or continues.
        return "update_in_progress" if undispatched_team(update) or update["target"] != target else None
    if head == target:
        return "already_current"
    return None if started else "needs_reset"


def continue_refusal(update, request_id):
    if update is None or update["request_id"] != request_id:
        return "no_such_request"
    if not unfinished(update):
        return "nothing_to_continue"
    return "not_continuable" if undispatched_team(update) else None


def launch(runner, env, python, found, *arguments, timeout=15):
    # The stable launcher, as the guide script starts it; host update goes round maintenance.
    return runner([python, "-I", "-S", "-B", found["install"] / "bin/claudlobby", "--root", found["data"],
                   "host", "update", *arguments], env=env, timeout=timeout)


def read_record(runner, env, python, found):
    result = launch(runner, env, python, found, "status", "--json")
    try:
        value = json.loads(result.stdout, object_pairs_hook=unique)
        if result.returncode or not isinstance(value, dict) or value.get("read_only") is not True:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise Refusal("unavailable", "unavailable", "inspect", 6) from None
    return summary(value.get("update"))


def ancestor(runner, env, source, older, newer):
    return git(runner, env, source, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def dispatched(result, request_id=None, target=None):
    try:
        update = summary(json.loads(result.stdout, object_pairs_hook=unique)["update"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        update = None
    if (result.returncode or update is None or request_id is not None and update["request_id"] != request_id
            or target is not None and update["target"] != target):
        return None
    return {"state": "updating", "request_id": update["request_id"], "target": update["target"]}


def update_execute(action, value, deadline):
    runner = Runner(deadline)
    try:
        home = account_profile(value["expected_uid"])
        python = independent_python()
        foreign = shutil.which("claudlobby", path=os.environ.get("PATH", ""))
        family = home / ".local/share/claudlobby-browser"
        if foreign and Path(foreign) != family / "install/bin/claudlobby":
            raise Refusal("existing_installation")
        reads, env = fixed_environment(home), update_environment(home)
        found = installed(home, python, runner, reads)
        if found is None:
            if action != "update-status":
                raise Refusal("not_prepared")
            return {"state": "absent", "recorded_revision": None, "installed_revision": None,
                    "receipt_is_ancestor": None, "binding_sha256": None, "setup": None, "interpreter": None,
                    "update": None, **({"relation": None} if "compare_revision" in value else {})}
        head, source = found["head"], found["source"]
        update = read_record(runner, env, python, found)
        if action == "update-status":
            answer = {"state": "recorded", "recorded_revision": found["receipt"]["ref"], "installed_revision": head,
                      "receipt_is_ancestor": ancestor(runner, reads, source, found["receipt"]["ref"], head),
                      "binding_sha256": found["binding"], "setup": found["setup"],
                      "interpreter": found["interpreter"], "update": update}
            if "compare_revision" in value:
                compare = value["compare_revision"]
                answer["relation"] = ("same" if compare == head else "ahead"
                                      if ancestor(runner, reads, source, compare, head) else "other")
            return answer
        if action == "update":
            target = value["target_revision"]
            code = update_refusal(update, head, value["expected_installed_revision"], target,
                                  found["setup"] == "started")
            if code:
                raise Refusal(code)
            result = launch(runner, env, python, found, "apply", target, timeout=BUDGETS[action])
            answer = dispatched(result, target=target)
            if answer is not None:
                return answer
            if result.returncode == NOT_CONTINUABLE_EXIT:
                # A team preparation for this target appeared after the read above.
                raise Refusal("update_in_progress")
            # The updater refuses anything but a fast-forward to a commit it fetched.
            if (git(runner, reads, source, "cat-file", "-e", target + "^{commit}", check=False).returncode == 0
                    and not ancestor(runner, reads, source, head, target)):
                raise Refusal("not_forward")
            raise Refusal("unavailable", "unavailable", "inspect", 6)
        request_id = value["request_id"]
        code = continue_refusal(update, request_id)
        if code:
            raise Refusal(code)
        result = launch(runner, env, python, found, "continue", "--expected-request", request_id, timeout=BUDGETS[action])
        answer = dispatched(result, request_id=request_id)
        if answer is not None:
            return answer
        if result.returncode == NOT_CONTINUABLE_EXIT:
            raise Refusal("not_continuable")
        if continue_refusal(read_record(runner, env, python, found), request_id) == "no_such_request":
            raise Refusal("no_such_request")
        raise Refusal("unavailable", "unavailable", "inspect", 6)
    except Refusal:
        raise
    except Exception:
        raise Refusal("unavailable", "unavailable", "inspect", 6) from None
    finally:
        runner.close()


def update_main(action, fd=0):
    # update-status reads; update and update-continue have the updater already on
    # this computer move it, dispatch and exit. None waits for the updater,
    # rewrites the receipt or asks anything.
    os.umask(0o077)
    operation = UPDATE_ACTIONS[action]
    deadline = STARTED + BUDGETS[action]
    exit_code = 0
    def interrupted(_number, _frame):
        raise Refusal("unavailable", "unavailable", "inspect", 6)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic() - 4))
    try:
        answer = update_execute(action, request(fd, deadline, action), deadline)
    except Refusal as error:
        if error.code == "invalid_request":
            answer, exit_code = {"state": "refused", "code": "invalid_request"}, 2
        elif error.state == "refused" and error.code in UPDATE_REFUSALS[action]:
            answer, exit_code = {"state": "refused", "code": error.code}, 4
        else:
            answer, exit_code = {"state": "unavailable", "code": "unavailable"}, 6
    except Exception:
        answer, exit_code = {"state": "unavailable", "code": "unavailable"}, 6
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    output = (json.dumps({"schema_version": 1, "operation": operation, **answer}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(output) > 8192:
        output, exit_code = (json.dumps({"schema_version": 1, "operation": operation, "state": "unavailable",
                                         "code": "unavailable"}, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"), 6
    sys.stdout.buffer.write(output)
    return exit_code


def main(action, fd=0):
    os.umask(0o077)
    operation = "host-install-v1" if action == "prepare" else "host-install-status-v1"
    deadline = STARTED + BUDGETS[action]
    revision, uid, binding_hash = None, None, None
    exit_code = 0
    def interrupted(_number, _frame):
        raise unavailable()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic() - 4))
    try:
        value = request(fd, deadline)
        state, code, next_step, prepared, revision, uid, binding_hash = execute(action, value, deadline)
    except Refusal as error:
        state, code, next_step, prepared = error.state, error.code, error.next_step, False
        revision, uid, binding_hash = getattr(error, "identity", (None, None, None))
        exit_code = error.exit_code
    except Exception:
        state, code, next_step, prepared, exit_code = "unavailable", "inspection_unavailable", "inspect", False, 6
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    result = {"schema_version": 1, "operation": operation, "source_revision": revision,
              "account_uid": uid, "binding_sha256": binding_hash, "state": state, "code": code,
              "prepared": prepared, "activated": False, "next": next_step}
    output = (json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(output) > 8192:
        raise RuntimeError()
    sys.stdout.buffer.write(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(update_main(sys.argv[1]) if sys.argv[1] in UPDATE_ACTIONS else main(sys.argv[1]))
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
operation=host-install-v1
case "$1" in
    status) operation=host-install-status-v1 ;;
    update-status) operation=host-update-status-v1 ;;
    update) operation=host-update-v1 ;;
    update-continue) operation=host-update-continue-v1 ;;
esac
if ! darwin_existing_python "$(/usr/bin/id -u)"; then
    case "$operation" in
        host-install-v1|host-install-status-v1)
            printf '{"account_uid":null,"activated":false,"binding_sha256":null,"code":"runtime_not_admitted","next":"inspect","operation":"%s","prepared":false,"schema_version":1,"source_revision":null,"state":"unavailable"}\n' "$operation" ;;
        *) printf '{"code":"unavailable","operation":"%s","schema_version":1,"state":"unavailable"}\n' "$operation" ;;
    esac
    exit 6
fi
exec "$DARWIN_RUNTIME_PYTHON" -I -B /dev/fd/3 "$1" 4<&-
DARWIN_OPERATION_LAUNCHER
fi
if [ ! -x /usr/bin/python3 ]; then refuse prerequisite_missing stop unavailable; exit 6; fi
/usr/bin/python3 -I -B /dev/fd/3 "$1" 2>/dev/null
