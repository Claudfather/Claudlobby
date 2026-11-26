#!/bin/sh
# Candidate fixed root entry, not a published/reviewed source pin.
# inspect writes root-private staging directories, transition.lock and the exact
# immutable leaf artifact. Carrier UI must disclose this before root inspection.
# apply/status never create, restage, repair, dispatch or reinterpret receipts.
# Only compiled carrier code supplies the pinned leaf as canonical base64 argv2;
# the browser supplies no source, command, path, environment or private verb.
# stdin is one canonical bounded JSON line + EOF, forwarded byte-for-byte.
# Unknown/cancelled transport outcomes require original-receipt status, no replay.
case "$1" in inspect|apply|status) [ "$#" -eq 2 ] || {
    printf '{"code":"invalid_request","schema_version":1,"state":"refused"}\n'; exit 2; } ;;
    *) printf '{"code":"invalid_request","schema_version":1,"state":"refused"}\n'; exit 2 ;;
esac
[ -x /usr/bin/python3 ] || exit 6
exec /usr/bin/python3 -I -S -B -c '
# BEGIN EMBEDDED PYTHON
import base64
import binascii
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import stat
import sys
import tempfile
import time
import uuid

# Separate leaf pin only: leaf -> digest -> entry -> core revision -> carrier.
# Provisional candidate; refresh after the concurrent leaf correction freezes.
LEAF_SHA256 = "92b7e08bcd067d73317f0d08b11330e397da0a52d08bdfc30b2f3592504be12d"
STORE = Path("/var/lib/claudlobby-browser-system/v1")
OWNER = 0
PAYLOAD_LIMIT = 65536
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C",
       "DEBIAN_FRONTEND": "noninteractive"}


class Refusal(Exception):
    def __init__(self, code):
        self.code = code


def require(ok, code):
    if not ok:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def decode_payload(text):
    require(type(text) is str and 0 < len(text) <= 4 * ((PAYLOAD_LIMIT + 2) // 3), "invalid_artifact")
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        raise Refusal("invalid_artifact") from None
    require(0 < len(raw) <= PAYLOAD_LIMIT and base64.b64encode(raw).decode("ascii") == text
            and hashlib.sha256(raw).hexdigest() == LEAF_SHA256, "artifact_mismatch")
    return raw


def validate_request(verb, raw):
    require(verb in ("inspect", "apply", "status") and type(raw) is bytes and len(raw) <= 4096
            and raw.endswith(b"\n") and raw.count(b"\n") == 1
            and all(32 <= c <= 126 for c in raw[:-1]), "invalid_request")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Refusal("invalid_request") from None
    fields = {"schema_version", "request_id", "artifact_sha256", "selection"}
    fields |= {"kind"} if verb == "inspect" else {"digest", "decision"} if verb == "apply" else set()
    require(type(value) is dict and set(value) == fields and canonical(value) + b"\n" == raw,
            "invalid_request")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["artifact_sha256"] == LEAF_SHA256, "invalid_request")
    try:
        require(type(value["request_id"]) is str and str(uuid.UUID(value["request_id"])) == value["request_id"],
                "invalid_request")
    except (ValueError, AttributeError):
        raise Refusal("invalid_request") from None
    s = value["selection"]
    selection_fields = {"account", "uid", "node_key"}
    if verb == "apply":
        selection_fields |= {"gid", "home", "profile_id"}
    require(type(s) is dict and set(s) == selection_fields,
            "invalid_selection")
    require(type(s["account"]) is str
            and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", s["account"]), "invalid_selection")
    require(type(s["uid"]) is int and 0 < s["uid"] < 2**32 - 1, "invalid_selection")
    require(type(s["node_key"]) is str and re.fullmatch(r"nodekey:[0-9a-f]{64}", s["node_key"]), "invalid_selection")
    if verb == "inspect":
        require(type(value["kind"]) is str and value["kind"] in ("system", "index-refresh", "ingress"), "invalid_request")
    if verb == "apply":
        # Framing only: the pinned leaf owns account/HOME/profile policy and
        # compares these original retained fields. Never derive them here.
        require(type(s["gid"]) is int and 0 < s["gid"] < 2**32 - 1
                and type(s["home"]) is str and 0 < len(s["home"]) < 180
                and type(s["profile_id"]) is str and 0 < len(s["profile_id"]) <= 64, "invalid_selection")
        require(value["decision"] == "approve" and type(value["digest"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", value["digest"]), "invalid_request")
    return value


def read_request(verb, fd=0):
    raw = bytearray(); deadline = time.monotonic() + 5
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0 and select.select([fd], [], [], remaining)[0], "invalid_request")
        part = os.read(fd, 4097 - len(raw))
        if not part:
            break
        raw.extend(part)
        require(len(raw) <= 4096, "invalid_request")
    value = validate_request(verb, bytes(raw))
    return bytes(raw), value


def trusted_parents(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022, "unsafe_parent")


def admit_platform():
    require(platform.system() == "Linux" and platform.machine() in ("x86_64", "aarch64")
            and sys.version_info[:2] == (3, 12), "unsupported_root_profile")
    release = Path("/etc/os-release").resolve(strict=True)
    trusted_parents(release)
    info = release.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022
            and info.st_size <= 16384, "unsupported_root_profile")
    text = release.read_text(encoding="ascii")
    require(re.search(r"^ID=ubuntu$", text, re.M) and re.search(r"^VERSION_ID=\"24.04\"$", text, re.M),
            "unsupported_root_profile")


def admit_identity():
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (0, 0, 0, 0), "root_required")
    admit_platform()


def directory(path, create):
    trusted_parents(path)
    created = False
    if create:
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and stat.S_IMODE(info.st_mode) == 0o700,
            "unsafe_store")
    if created:
        sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_file(fd, mode, limit):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "unsafe_file")
    return info


def verify_artifact(path):
    trusted_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = check_file(fd, 0o500, PAYLOAD_LIMIT)
        raw = bytearray()
        while len(raw) <= PAYLOAD_LIMIT:
            part = os.read(fd, PAYLOAD_LIMIT + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
        after = check_file(fd, 0o500, PAYLOAD_LIMIT)
        require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and len(raw) == before.st_size
                and hashlib.sha256(raw).hexdigest() == LEAF_SHA256, "artifact_mismatch")
    finally:
        os.close(fd)


def stage(verb, body):
    require(verb in ("inspect", "apply", "status") and type(body) is bytes
            and len(body) <= PAYLOAD_LIMIT and hashlib.sha256(body).hexdigest() == LEAF_SHA256, "invalid_artifact")
    create = verb == "inspect"
    for path in (STORE.parent, STORE):
        directory(path, create)
    lock = STORE / "transition.lock"
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(lock, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    except FileExistsError:
        fd = os.open(lock, flags)
    try:
        check_file(fd, 0o600, 0)
        if create:
            os.fsync(fd); sync_directory(STORE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal("transition_busy") from None
        require(not any(p.name.startswith(".entry-") for p in STORE.iterdir()), "uncertain_staging")
        path = STORE / ("bundle-" + LEAF_SHA256 + ".sh")
        if not path.exists() and not path.is_symlink():
            require(create, "original_artifact_unavailable")
            temp_fd, name = tempfile.mkstemp(prefix=".entry-", dir=STORE)
            try:
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(body); stream.flush(); os.fchmod(stream.fileno(), 0o500); os.fsync(stream.fileno())
                # Exclusive publication: never replace/adopt a concurrently created target.
                os.link(name, path, follow_symlinks=False)
            finally:
                os.unlink(name)
            sync_directory(STORE)
        verify_artifact(path)
        return path
    finally:
        # The leaf takes this same flock: close it BEFORE exec, never around it.
        os.close(fd)


def handoff(verb, raw, path):
    require(verb in ("inspect", "apply", "status") and path == STORE / ("bundle-" + LEAF_SHA256 + ".sh"),
            "invalid_request")
    validate_request(verb, raw)
    verify_artifact(path)
    shell = Path("/bin/sh").resolve(strict=True)
    trusted_parents(shell)
    info = shell.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022, "unsafe_shell")
    read_fd, write_fd = os.pipe()
    try:
        # A canonical request is <=4096 bytes; no child/native execution yet.
        offset = 0
        while offset < len(raw):
            offset += os.write(write_fd, raw[offset:])
        os.close(write_fd); write_fd = -1
        os.dup2(read_fd, 0)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    os.chdir("/"); os.umask(0o077)
    os.closerange(3, 1048576)
    os.execve("/bin/sh", ["/bin/sh", str(path), verb], dict(ENV))


def main():
    try:
        require(len(sys.argv) == 3 and sys.argv[1] in ("inspect", "apply", "status"), "invalid_request")
        verb = sys.argv[1]
        body = decode_payload(sys.argv[2])
        raw, value = read_request(verb)
        admit_identity()
        os.umask(0o077)
        handoff(verb, raw, stage(verb, body))
        return 4
    except (Refusal, OSError, ValueError, KeyError, TypeError) as exc:
        # Entry refusal never asserts that an earlier original operation had no effect.
        code = exc.code if isinstance(exc, Refusal) else "entry_unavailable"
        sys.stdout.buffer.write(canonical({"schema_version": 1, "state": "refused", "code": code}) + b"\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
# END EMBEDDED PYTHON
' "$1" "$2"
