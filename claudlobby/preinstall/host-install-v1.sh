#!/bin/sh
# Fixed regular-user preparation/status. No root/provider/lifecycle actions.
# Caller supplies exactly one canonical ASCII JSON line (<=512 bytes), then EOF:
# expected_uid, schema_version=1, source_revision=reviewed lowercase 40-hex pin.
# A future authenticated transport hardcodes action/pin and validates the entire
# single <=8192-byte result plus exit status; the current browser door cannot
# carry this operation. Budgets: prepare 900s, status 15s including child drain.
# recorded_complete is retained preparation evidence, never fresh runtime or
# dependency readiness. After unknown dispatch, inspect only; absent is an
# observation and cannot authorize an automatic retry or prove remote shutdown.
operation=host-install-v1
if [ "$#" -eq 1 ] && [ "$1" = status ]; then operation=host-install-status-v1; fi
refuse() {
    printf '{"account_uid":null,"activated":false,"binding_sha256":null,"code":"%s","next":"%s","operation":"%s","prepared":false,"schema_version":1,"source_revision":null,"state":"%s"}\n' "$1" "$2" "$operation" "$3"
}
if [ "$#" -ne 1 ]; then refuse invalid_request stop refused; exit 2; fi
case "$1" in prepare|status) ;; *) refuse invalid_request stop refused; exit 2 ;; esac
if [ ! -x /usr/bin/python3 ]; then refuse prerequisite_missing stop unavailable; exit 6; fi
/usr/bin/python3 -I -B -c '
# BEGIN EMBEDDED PYTHON
# Embedded stdlib operation. The shell selects only prepare or status.
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
BUDGETS = {"prepare": 900, "status": 15}
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_CHILD_OUTPUT = 2 * 1024 * 1024


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


def request(fd, deadline):
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
        if (not isinstance(value, dict) or set(value) != {"schema_version", "source_revision", "expected_uid"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["source_revision"], str) or not SHA.fullmatch(value["source_revision"])
                or type(value["expected_uid"]) is not int or not 0 < value["expected_uid"] < 2**32 - 1
                or raw != (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise Refusal("invalid_request", exit_code=2) from None


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


def account_profile(expected_uid):
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
    if (sys.implementation.name != "cpython" or sys.version_info < (3, 10)
            or sys.prefix != sys.base_prefix or python.parent != Path("/usr/bin")):
        raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
    info = python.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise Refusal("prerequisite_unusable", "unavailable", exit_code=6)
    return python


def fixed_environment(home):
    return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INPUT": "1", "PLANE_EMIT_DISABLED": "1",
            "TMPDIR": str(home / ".local/share/claudlobby-browser/staging")}


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
    value = runner(["/usr/bin/git", *GIT_FLAGS, "-C", str(source), *arguments], env=env,
                   timeout=15 if arguments[0] != "fetch" else 300)
    if check and value.returncode:
        raise unavailable()
    return value


def git_available():
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


def receipt(path, expected):
    value = read_json(path)
    if (not isinstance(value, dict) or set(value) != FIELDS
            or any(value.get(key) != item for key, item in expected.items())
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


def paths(home, create=False):
    for path in (home / ".local", home / ".local/share"):
        if not directory(path, create=create):
            return None
    family = home / ".local/share/claudlobby-browser"
    if not directory(family, private=True, create=create):
        return None
    if {p.name for p in family.iterdir()} - {"install", "data", "staging"}:
        raise Refusal("existing_installation", next_step="inspect")
    install, data, staging = (family / name for name in ("install", "data", "staging"))
    for path in (install, data, staging):
        directory(path, private=True, create=create)
    return install, data, staging


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


def inspect(install, data, expected, python, runner, env):
    allowed = {"preparation.lock", "preparation.json", "installation.json", "src", "env", "bin", "cache"}
    if {p.name for p in install.iterdir()} - allowed:
        raise Refusal("existing_installation", next_step="inspect")
    record = install / "preparation.json"
    if not record.exists() and not record.is_symlink():
        if {p.name for p in install.iterdir()} - {"preparation.lock"} or names(data):
            raise Refusal("existing_installation", next_step="inspect")
        return None
    state = receipt(record, expected)
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
    uid, revision, binding_hash = None, None, None
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
        current = paths(home)
        expected = binding(install, data, revision, python)
        binding_hash = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        if action == "status" and (current is None or not (install / "preparation.lock").exists()):
            if current is not None and (names(install) or names(data) or names(staging)):
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
        paths(home)
        contents_check(install, data, staging)
        state = inspect(install, data, expected, python, runner, env)
        if action == "status":
            if state is None:
                return "absent", "none", "inspect", False, revision, uid, binding_hash
            if state["complete"]:
                return "recorded_complete", "none", "continue_onboarding", True, revision, uid, binding_hash
            return "incomplete", "preparation_incomplete", "resume_same", False, revision, uid, binding_hash
        if state is None or not state["complete"]:
            directory(data, private=True, create=True)
            directory(staging, private=True, create=True)
            stage_and_prepare(install, data, staging, expected, python, runner, env, lock)
            observed = inspect(install, data, expected, python, runner, env)
            if observed is None or not observed["complete"]:
                raise unavailable("preparation_incomplete")
        return "prepared", "none", "continue_onboarding", True, revision, uid, binding_hash
    except Refusal as error:
        error.identity = (revision, uid, binding_hash)
        raise
    except Exception:
        error = Refusal("preparation_refused", "refused", "inspect")
        error.identity = (revision, uid, binding_hash)
        raise error from None
    finally:
        runner.close()
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
    raise SystemExit(main(sys.argv[1]))
# END EMBEDDED PYTHON
' "$1" 2>/dev/null
operation_exit=$?
case "$operation_exit" in
    0|2|4|6) exit "$operation_exit" ;;
    *) refuse prerequisite_unusable inspect unavailable; exit 6 ;;
esac
