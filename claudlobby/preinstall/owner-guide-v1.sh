#!/bin/sh
# Immutable non-PTY admission prelude, then the ordinary installed owner guide.
# One canonical <=1024-byte admission line; no EOF and no success prefix.
# Only setup/status. Trusted authenticated account; no same-UID/root sandbox.
operation=owner-guide-v1
if [ "$#" -eq 1 ] && [ "$1" = status ]; then operation=owner-guide-status-v1; fi
refuse() {
    printf '{"code":"%s","operation":"%s","retry":false,"schema_version":1,"state":"refused"}\n' "$1" "$operation"
}
if [ "$#" -ne 1 ]; then refuse invalid_request; exit 2; fi
case "$1" in setup|status) ;; *) refuse invalid_request; exit 2 ;; esac
if [ ! -x /usr/bin/python3 ]; then refuse prerequisite_missing; exit 6; fi
exec /usr/bin/python3 -I -S -B -c '
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


def request(fd, deadline):
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
                or set(value) != {"schema_version", "expected_uid", "source_revision", "binding_sha256", "finish_setup"}
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


def account_profile(expected_uid):
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
    if (sys.implementation.name != "cpython" or sys.version_info < (3, 10)
            or sys.prefix != sys.base_prefix or python.parent != Path("/usr/bin")):
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


def git_environment(home):
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}


def source_check(source, revision, env, deadline):
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
    git_binary = Path("/usr/bin/git").resolve()
    trusted_executable(git_binary)
    def git(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("inspection_unavailable")
        result = subprocess.run([str(git_binary), *GIT_FLAGS, "-C", str(source), *args], env=env,
                                capture_output=True, timeout=min(15, remaining), check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024 or len(result.stderr) > 65536:
            raise Refusal("source_mismatch")
        return result.stdout
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


def validate(value, deadline):
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
                or state["interpreter"] != str(python) or state["interpreter_sha256"] != digest(python)
                or hashlib.sha256(json.dumps({key: state[key] for key in BINDING_FIELDS}, sort_keys=True).encode()).hexdigest() != value["binding_sha256"]
                or value["finish_setup"] is None and state["ref"] != value["source_revision"]):
            raise Refusal()
        identity = read_json(install / "installation.json", 4096)
        if identity != {"schema": 1, "data_root": str(data)} or type(identity.get("schema")) is not int:
            raise Refusal()
        source = install / "src"
        source_check(source, value["source_revision"], git_environment(home), deadline)
        environment_check(install / "env", python)
        directory(install / "bin", True)
        launcher = install / "bin/claudlobby"
        if plain_file(launcher, 1024 * 1024, 0o755) != ("#!" + str(python) + "\n").encode() + plain_file(source / "claudlobby/installation.py", 1024 * 1024, None):
            raise Refusal()
        # No installer inactive-data check: original setup/update state belongs
        # to the existing guide and its canonical continuation transactions.
        environment = {**os.environ, **inherited, "HOME": str(home), "PATH": "/usr/bin:/bin"}
        return python, launcher, data, environment
    finally:
        os.close(lock)


def main(action, fd=0):
    operation = "owner-guide-status-v1" if action == "status" else "owner-guide-v1"
    deadline = time.monotonic() + 15
    def interrupted(_number, _frame):
        raise Refusal("inspection_unavailable")
    signal.signal(signal.SIGALRM, interrupted)
    signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        if action not in {"setup", "status"} or os.isatty(fd) or os.isatty(1):
            raise Refusal("invalid_request", 2)
        value = request(fd, deadline)
        python, launcher, data, environment = validate(value, deadline)
        # Exec the canonical stable launcher normally: its existing reader
        # lock, maintenance fence, parser and operator-context checks all run.
        signal.setitimer(signal.ITIMER_REAL, 0)
        os.execve(python, [str(python), "-I", "-S", "-B", str(launcher), "--root", str(data),
            "host", "owner", "setup", "--stdio-status" if action == "status" else "--stdio"], environment)
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
' "$1" 2>/dev/null
