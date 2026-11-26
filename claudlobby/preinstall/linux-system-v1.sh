#!/bin/sh
# Candidate Ubuntu24 root preinstall bundle, never an installed user-module import.
# Public verbs inspect/apply/status consume one canonical ASCII JSON line + EOF.
# inspect: schema_version, request_id (UUID), artifact_sha256 (this whole file),
# selection {account,uid,node_key}, kind system|index-refresh|ingress; core derives
# full retained {account,uid,gid,home,node_key,profile_id} only for a new inspect.
# apply: exact original full selection, digest and decision=approve; no edited plan.
# status: original minimal selection, UUID and artifact pin; never derives context.
# UUIDs are permanently one-shot, including unknowns.
# The team account claudlobby is adopted when it conforms, or, absent, a system
# proposal plans one fixed useradd that the worker runs once, before apt. Its
# id must be the next id this leaf computes. Other accounts are unchanged.
# Trust entry: authenticated root transport must pin these exact bytes in a
# root-owned, non-user-writable regular file before invoking this bundle.
# Private worker/hook additionally require the retained fixed artifact/receipt.
# Native apt2.8 hook coverage/order, systemd255 survival, native LocalAPI and cold
# Ubuntu behavior remain UNQUALIFIED RELEASE GATES; fixture success proves none.
case "$1" in inspect|apply|status) [ "$#" -eq 1 ] || exit 2 ;;
    _worker|_hook) [ "$#" -eq 2 ] || exit 2 ;;
    *) printf '{"code":"invalid_request","schema_version":1,"state":"refused"}\n'; exit 2 ;;
esac
[ -x /usr/bin/python3 ] || exit 6
exec /usr/bin/python3 -I -S -B -c '
# BEGIN EMBEDDED PYTHON
import os
import sys
import re
import json
import time
import uuid
import stat
import pwd
import grp
import hashlib
import fcntl
import socket
import struct
import http.client
import subprocess
import tempfile
import platform
import select
from pathlib import Path

STORE = Path("/var/lib/claudlobby-browser-system/v1")
OWNER = 0
LIMIT = 262144
TTL = 600
RECORD_BUDGET = LIMIT // 4
MANAGER_POLLS = 20
SEEDS = ("git", "python3-venv", "ca-certificates", "tmux", "jq")
NATIVE_VERSION = "1.104.7-tfb3b9b296-g0651cc45e"  # static official ELF stamp, not native proof
SOCKET = "/var/run/tailscale/tailscaled.sock"
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C",
       "DEBIAN_FRONTEND": "noninteractive"}
SHA = re.compile(r"[0-9a-f]{64}")
TOKEN = re.compile(r"[A-Za-z0-9.+:~_-]+")
TERMINAL = {"completed", "failed_partial", "conflict"}
DISCLOSURE = ("Native apt resolves, downloads, verifies and installs the exact listed transaction. "
    "Existing apt hooks and package maintainer scripts/triggers execute as root and may change "
    "or restart services. Earlier hooks may execute even if our verifier refuses. Local "
    "conffiles are kept with --force-confdef and --force-confold. Selected-user lingering may "
    "start that accounts enabled user services. No automatic repair, rollback or replay.")
TEAM = "claudlobby"
TEAM_HOME = "/home/claudlobby"
TEAM_DISCLOSURE = ("Creates or uses the local account claudlobby: its own group only, folder /home/claudlobby "
    "that only it can open, shell /bin/bash, locked password, no expiry, no subordinate IDs. Setup adds it "
    "to no other group and writes no sudo rule. Lingering starts its user services at boot. Anyone your "
    "Tailscale access controls let sign in to this computer as a non-root account can also sign in as "
    "claudlobby. Setup never removes this account.")
# Fixed root paths the team account checks read; nothing on the wire moves them.
PATHS = {"passwd": "/etc/passwd", "group": "/etc/group", "shadow": "/etc/shadow", "subuid": "/etc/subuid",
         "subgid": "/etc/subgid", "nsswitch": "/etc/nsswitch.conf", "home": TEAM_HOME,
         "mail": "/var/mail/" + TEAM, "proc": "/proc", "run": "/run/user"}


class Refusal(Exception):
    def __init__(self, code):
        self.code = code


def require(ok, code):
    if not ok:
        raise Refusal(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def unique(pairs):
    out = {}
    for key, value in pairs:
        require(key not in out, "duplicate_field")
        out[key] = value
    return out


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise Refusal("invalid_json") from None


def valid_uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate_selection(s, full=False):
    require(type(s) is dict and set(s) == ({"account", "uid", "node_key", "gid", "home", "profile_id"} if full else {"account", "uid", "node_key"}), "invalid_selection")
    require(type(s["account"]) is str and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", s["account"]), "invalid_selection")
    require(all(type(s[k]) is int and 0 < s[k] < 2**32 - 1 for k in (("uid", "gid") if full else ("uid",))), "invalid_selection")
    require(type(s["node_key"]) is str and re.fullmatch(r"nodekey:[0-9a-f]{64}", s["node_key"]), "invalid_selection")
    if full:
        require(type(s["home"]) is str and s["home"].startswith("/") and len(s["home"]) < 180
                and str(Path(s["home"])) == s["home"] and not any(p in (".", "..") for p in s["home"].split("/"))
                and s["home"] != "/" and re.fullmatch(r"/[A-Za-z0-9_./-]+", s["home"]), "invalid_selection")
        require(type(s["profile_id"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", s["profile_id"]), "invalid_selection")
    return s


def validate_request(verb, value):
    base = {"schema_version", "request_id", "artifact_sha256", "selection"}
    fields = base | ({"kind"} if verb == "inspect" else {"digest", "decision"} if verb == "apply" else set())
    require(type(value) is dict and set(value) == fields, "invalid_request")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "invalid_request")
    require(valid_uuid(value["request_id"]) and type(value["artifact_sha256"]) is str
            and SHA.fullmatch(value["artifact_sha256"]), "invalid_request")
    validate_selection(value["selection"], full=verb == "apply")
    if verb == "inspect":
        require(value["kind"] in ("system", "index-refresh", "ingress"), "invalid_request")
    if verb == "apply":
        require(value["decision"] == "approve" and type(value["digest"]) is str
                and SHA.fullmatch(value["digest"]), "invalid_request")
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
    require(raw.endswith(b"\n") and raw.count(b"\n") == 1
            and all(32 <= c <= 126 for c in raw[:-1]), "invalid_request")
    value = validate_request(verb, decode(raw))
    require(bytes(raw) == canonical(value) + b"\n", "invalid_request")
    return value


def trusted_parents(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER
                and not info.st_mode & 0o022, "unsafe_parent")


def read_file(path, limit=LIMIT, mode=0o600):
    trusted_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == mode and info.st_size <= limit, "unsafe_file")
        raw = os.read(fd, limit + 1)
        require(len(raw) <= limit, "oversize_file")
        return raw
    finally:
        os.close(fd)


def directory(path, create=False):
    trusted_parents(path)
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER
            and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_store")


def sync_directory():
    fd = os.open(STORE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, raw, mode=0o600):
    require(path.parent == STORE and len(raw) <= LIMIT, "invalid_internal_path")
    if path.exists() or path.is_symlink():
        read_file(path, mode=mode)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=STORE)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path); sync_directory()
    finally:
        if os.path.exists(name):
            os.unlink(name)


def lock_store(create=False, blocking=False):
    if create:
        for path in (STORE.parent, STORE):
            directory(path, True)
    else:
        directory(STORE)
    path = STORE / "transition.lock"
    trusted_parents(path)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600, "unsafe_lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd); raise Refusal("transition_busy") from None
    return fd


def receipt_path(request_id):
    require(valid_uuid(request_id), "invalid_request")
    return STORE / (request_id + ".json")


def load(request_id):
    value = decode(read_file(receipt_path(request_id)))
    require(type(value) is dict and value.get("schema_version") == 1
            and value.get("request_id") == request_id and value.get("digest") == digest(value.get("proposal")), "unsafe_receipt")
    return value


def save(record):
    write_file(receipt_path(record["request_id"]), canonical(record))


def artifact_path(sha):
    require(type(sha) is str and SHA.fullmatch(sha), "invalid_artifact")
    return STORE / ("bundle-" + sha + ".sh")


def verify_artifact(path, sha):
    raw = read_file(path, mode=0o500)
    require(hashlib.sha256(raw).hexdigest() == sha, "artifact_mismatch")
    return raw


def native(argv, effect=False):
    # All argv originate below; no wire field becomes a command or option.
    binary = Path(argv[0]).resolve(strict=True)
    trusted_parents(binary)
    info = binary.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022, "unsafe_native_binary")
    if effect:
        # apt/logind are manager-owned worker effects, with no frontend timeout.
        child_env = dict(ENV)
        invocation = os.environ.get("INVOCATION_ID", "")
        if re.fullmatch(r"[0-9a-f]{32}", invocation):
            child_env["INVOCATION_ID"] = invocation
        return subprocess.run(argv, env=child_env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, env=ENV, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=15)
        require(output.tell() <= 8 * 1024 * 1024, "native_output_oversize")
        output.seek(0); text = output.read().decode("utf-8", "strict")
    require(result.returncode == 0, "native_inspection_failed")
    return text


class LocalAPI:
    def __call__(self, method, endpoint, body=None, etag=None):
        require((method, endpoint) in {("GET", "status"), ("GET", "profiles/current"),
            ("GET", "serve-config"), ("POST", "serve-config")}, "invalid_native_operation")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(10)
        try:
            sock.connect(SOCKET)
            pid, uid, gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(uid == 0 and pid > 1, "not_native_root_daemon")
            executable = Path("/proc") / str(pid) / "exe"
            binary = executable.resolve(strict=True)
            require(binary.name == "tailscaled", "not_native_daemon")
            trusted_parents(binary)
            info = binary.stat()
            require(info.st_uid == 0 and not info.st_mode & 0o022, "unsafe_native_daemon")
            socket_path = Path(SOCKET).resolve(strict=True)
            trusted_parents(socket_path); socket_info = socket_path.lstat()
            require(stat.S_ISSOCK(socket_info.st_mode) and socket_info.st_uid == 0
                    and info.st_size <= 128 * 1024 * 1024, "unsafe_native_identity")
            h = hashlib.sha256()
            with binary.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            proc_stat = (Path("/proc") / str(pid) / "stat").read_text()
            start = proc_stat.rsplit(")", 1)[1].split()[19]
            self.identity = {"pid": pid, "start": start, "binary_sha256": h.hexdigest(),
                "binary": [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size],
                "socket": [socket_info.st_dev, socket_info.st_ino, socket_info.st_ctime_ns]}
            conn = http.client.HTTPConnection("local-tailscaled.sock", timeout=10); conn.sock = sock
            headers = {"Host": "local-tailscaled.sock", "Content-Type": "application/json"}
            if method == "POST":
                require(type(etag) is str and 0 < len(etag) <= 256 and "\r" not in etag and "\n" not in etag, "missing_etag")
                headers["If-Match"] = etag
            conn.request(method, "/localapi/v0/" + endpoint, body=canonical(body) if body is not None else None, headers=headers)
            response = conn.getresponse(); raw = response.read(LIMIT + 1)
            require(len(raw) <= LIMIT, "native_response_oversize")
            return response.status, response.getheader("ETag"), decode(raw) if raw and response.status == 200 else None
        finally:
            sock.close()


def api_get(api, endpoint):
    code, etag, value = api("GET", endpoint)
    require(code == 200, "native_api_unavailable")
    return etag, value


def derive_selection(selected, api):
    validate_selection(selected)
    # An absent team account has the ids it will be made with: gid = uid.
    planned = selected["account"] == TEAM and lookup(pwd.getpwnam, TEAM) is None
    if planned:
        gid, home = selected["uid"], TEAM_HOME
    else:
        if selected["account"] == TEAM:
            team_policy()  # a present team account is used only when it conforms
        account = pwd.getpwnam(selected["account"])
        require(account.pw_name == selected["account"] and account.pw_uid == selected["uid"]
                and account.pw_uid > 0 and account.pw_shell in ("/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/zsh", "/bin/zsh"), "account_changed")
        gid, home = account.pw_gid, account.pw_dir
    _, profile = api_get(api, "profiles/current")
    require(type(profile) is dict, "node_or_profile_changed")
    derived = {**selected, "gid": gid, "home": home, "profile_id": profile.get("ID")}
    validate_selection(derived, full=True)
    # Existing full binding owns native OS/account/node/profile/daemon policy.
    # Inspect repeats it while constructing the retained proposal.
    binding(derived, api, True) if planned else binding(derived, api)
    return derived


def binding(selection, api, planned=False):
    require(os.geteuid() == 0 and platform.system() == "Linux" and platform.machine() in ("x86_64", "aarch64"), "unsupported_root_profile")
    os_release = Path("/etc/os-release").read_text()
    require(re.search(r"^ID=ubuntu$", os_release, re.M) and re.search(r"^VERSION_ID=\"24.04\"$", os_release, re.M), "unsupported_os")
    if planned and selection["account"] == TEAM and lookup(pwd.getpwnam, TEAM) is None:
        # Planned: the worker makes it with exactly these ids. The output is
        # the same, so bindings before and after useradd compare equal.
        require((selection["gid"], selection["home"]) == (selection["uid"], TEAM_HOME), "account_changed")
    else:
        account = pwd.getpwnam(selection["account"])
        require((account.pw_uid, account.pw_gid, account.pw_dir) == (selection["uid"], selection["gid"], selection["home"])
                and account.pw_shell in ("/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/zsh", "/bin/zsh"), "account_changed")
    _, status = api_get(api, "status")
    daemon = dict(getattr(api, "identity", {}))
    _, profile = api_get(api, "profiles/current")
    require(daemon and daemon == getattr(api, "identity", None), "native_daemon_changed")
    require(type(status) is dict and type(profile) is dict and status.get("BackendState") == "Running"
            and status.get("TUN") is True and status.get("Self", {}).get("PublicKey") == selection["node_key"]
            and profile.get("ID") == selection["profile_id"], "node_or_profile_changed")
    version = status.get("Version", "")
    require(version == NATIVE_VERSION, "native_version_unqualified")
    dns = status["Self"].get("DNSName", "").rstrip(".")
    require(re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,240}[a-z0-9])?\.ts\.net", dns), "native_dns_unavailable")
    require(dns in (status.get("CertDomains") or []), "native_https_capability_required")
    return {"selection": selection, "dns": dns, "version": version,
            "profile_digest": digest(profile), "daemon": daemon, "machine": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()}


def boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    require(valid_uuid(value), "boot_identity_unavailable")
    return value


def unit_name(request_id):
    require(valid_uuid(request_id), "invalid_request")
    return "claudlobby-preinstall-" + request_id + ".service"


def unit_state(name):
    raw = native(["/usr/bin/systemctl", "show", name, "--property=LoadState,ActiveState,SubState,InvocationID,MainPID,Result,ExecMainStatus,DropInPaths,FragmentPath"])
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def manager_plan(selection):
    require(Path("/run/systemd/system").is_dir(), "system_manager_required")
    require(native(["/usr/bin/systemctl", "--version"]).startswith("systemd 255"), "systemd_version_unqualified")
    units = {name: unit_state(name) for name in ("user@" + str(selection["uid"]) + ".service",
        "user-runtime-dir@" + str(selection["uid"]) + ".service")}
    # Ubuntu24 systemd owns these regular vendor templates. An instance or
    # /etc, /run, generated or redirected fragment is unsupported existing state.
    for name, value in units.items():
        template = name.split("@", 1)[0] + "@.service"
        fragment = Path("/usr/lib/systemd/system") / template
        drops = value.get("DropInPaths", "").split()
        allowed = {str(fragment) + ".d/" + suffix for suffix in
                   ("10-login-barrier.conf", "timeout.conf")} if template == "user@.service" else set()
        require(value.get("LoadState") == "loaded" and value.get("FragmentPath") == str(fragment)
                and len(drops) == len(set(drops)) and set(drops) <= allowed, "manager_masked_or_custom")
        # Only the two shipped ordinary-user drop-ins are admitted, never an
        # operator override. Preserve stable fragment/drop-in policy for recheck.
        for path in (fragment, *(Path(drop) for drop in drops)):
            trusted_parents(path)
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and not info.st_mode & 0o022, "manager_masked_or_custom")
    linger_file = Path("/var/lib/systemd/linger") / selection["account"]
    if linger_file.exists() or linger_file.is_symlink():
        trusted_parents(linger_file); info = linger_file.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022, "unsafe_linger_state")
    stable = {name: {key: value.get(key, "") for key in ("LoadState", "DropInPaths", "FragmentPath")}
              for name, value in units.items()}
    return {"linger": linger_file.exists(), "units": stable, "ordinary_user_bus": "unproved"}


def manager_runtime(selection):
    return {name: unit_state(name) for name in ("user@" + str(selection["uid"]) + ".service",
            "user-runtime-dir@" + str(selection["uid"]) + ".service")}


def wait_manager(selection, expected):
    for attempt in range(MANAGER_POLLS):
        config = manager_plan(selection)
        require(config["units"] == expected["units"], "manager_changed")
        runtime = manager_runtime(selection)
        if config["linger"] and all(value.get("ActiveState") == "active" for value in runtime.values()):
            return {"configuration": config, "runtime": runtime, "ordinary_user_bus": "unproved"}
        if attempt + 1 < MANAGER_POLLS:
            time.sleep(0.25)
    raise Refusal("manager_postcondition_unproved")


def package_health():
    require(not native(["/usr/bin/dpkg", "--audit"]).strip(), "package_intervention_required")
    raw = native(["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Abbrev} ${binary:Package} ${Version} ${Architecture}\\n"])
    installed = {}
    for line in raw.splitlines():
        require(len(line) >= 4 and line[:3] in ("ii ", "hi ", "rc "), "package_intervention_required")
        fields = line[4:].split()
        require(len(fields) == 3, "package_state_unavailable")
        if line[:3] in ("ii ", "hi "):
            installed[fields[0].split(":")[0]] = {"version": fields[1], "architecture": fields[2]}
    return installed


def critical(package):
    return any(package == name or package.startswith(name + "-") for name in
               ("systemd", "dbus", "libc6", "openssh-server", "tailscale", "linux-image"))


def parse_simulation(text, architecture):
    out = []; seen = set()
    for line in text.splitlines():
        require(not line.startswith(("Remv ", "Purg ")), "package_removal_refused")
        if not line.startswith("Inst "):
            continue
        match = re.fullmatch(r"Inst ([a-z0-9][a-z0-9+.-]*)(?::([a-z0-9]+))?(?: \[([^ ]+)\])? \(([^ ]+) .*\[([a-z0-9]+)\]\)", line)
        require(match is not None, "unparseable_package_plan")
        name, qualifier, old, new, arch = match.groups()
        require(not critical(name) and arch in (architecture, "all") and qualifier in (None, architecture)
                and TOKEN.fullmatch(old or "-") and TOKEN.fullmatch(new) and name not in seen, "unsupported_package_plan")
        out.append({"package": name, "old": old or "-", "new": new, "architecture": arch}); seen.add(name)
    return sorted(out, key=lambda row: row["package"])


def apt_base():
    policies = ("APT::Get::Fix-Broken", "APT::Get::Fix-Missing", "APT::Get::AllowUnauthenticated",
                "Acquire::AllowInsecureRepositories", "Acquire::AllowWeakRepositories",
                "Acquire::AllowDowngradeToInsecureRepositories", "APT::Get::allow-Downgrades",
                "APT::Get::allow-Change-Held-Packages")
    return ["/usr/bin/apt-get", "--no-install-recommends", "--no-remove"] + [part for key in policies for part in ("-o", key + "=false")]


def apt_configuration():
    require(native(["/usr/bin/apt-get", "--version"]).startswith("apt 2.8."), "apt_version_unqualified")
    config = native(["/usr/bin/apt-config", "dump"])
    truth = r"(?:1|true|yes|with|on|enable)"
    require(not re.search(r"(?:Fix-Broken|Fix-Missing).*\"" + truth + r"\"", config, re.I), "unsupported_apt_repair_or_conffile_policy")
    require(not re.search(r"(?:AllowUnauthenticated|AllowInsecureRepositories|AllowWeakRepositories|AllowDowngradeToInsecureRepositories).*\"" + truth + r"\"", config, re.I), "insecure_apt_configuration")
    for line in config.splitlines():
        match = re.fullmatch(r"\s*DPkg::Options(?:::[^ ]*)? \"([^\"]*)\";", line, re.I)
        if match and "force" in match[1].lower():
            option = match[1]
            allowed = option in ("--force-confdef", "--force-confold")
            if option.startswith("--force="):
                parts = option[8:].split(",")
                allowed = bool(parts) and set(parts) <= {"confdef", "confold"}
            require(allowed, "unsupported_apt_repair_or_conffile_policy")
    hooks = [line for line in config.splitlines() if any(k in line.lower() for k in ("pre-invoke", "post-invoke", "pre-install-pkgs"))]
    require(sum(len(x) for x in hooks) <= 16000, "hook_disclosure_oversize")
    return {"config_digest": hashlib.sha256(config.encode()).hexdigest(), "hooks": hooks}


def package_plan():
    configuration = apt_configuration()
    installed = package_health(); arch = native(["/usr/bin/dpkg", "--print-architecture"]).strip()
    require(arch in ("amd64", "arm64") and not native(["/usr/bin/dpkg", "--print-foreign-architectures"]).strip(), "foreign_architecture_refused")
    seeds = [name for name in SEEDS if name not in installed]
    rows = parse_simulation(native(apt_base() + ["--simulate", "install", "--"] + seeds), arch) if seeds else []
    require(len(rows) <= 64 and set(seeds) <= {row["package"] for row in rows}, "unsupported_package_plan")
    holds = set(native(["/usr/bin/apt-mark", "showhold"]).split())
    for row in rows:
        require(row["package"] not in holds, "held_package_refused")
        require(row["old"] == "-" or native(["/usr/bin/dpkg", "--compare-versions", row["old"], "lt", row["new"]]) == "", "downgrade_refused")
        actual = installed.get(row["package"])
        require((actual is None and row["old"] == "-") or (actual is not None and actual["version"] == row["old"]), "package_plan_state_changed")
    return {"seeds": seeds, "actions": rows, "architecture": arch, **configuration}


def verify_hook(text, expected):
    lines = text.splitlines()
    require(lines and lines[0] == "VERSION 3", "apt_hook_protocol_refused")
    try:
        boundary = lines.index("", 1)
    except ValueError:
        raise Refusal("apt_hook_protocol_refused") from None
    require(all("=" in line for line in lines[1:boundary]), "apt_hook_protocol_refused")
    wanted = {row["package"]: row for row in expected}; actions = {"unpack": set(), "configure": set()}
    for line in lines[boundary + 1:]:
        fields = line.split()
        require(len(fields) == 9, "apt_hook_protocol_refused")
        name, old, oldarch, oldmulti, direction, new, arch, multi, action = fields
        require(name in wanted and not critical(name), "apt_action_drift")
        row = wanted[name]
        require((old, new, arch) == (row["old"], row["new"], row["architecture"])
                and direction == "<" and oldarch in ("-", arch) and oldmulti in ("none", "no", "same", "foreign", "allowed")
                and multi in ("none", "no", "same", "foreign", "allowed"), "apt_action_drift")
        kind = "configure" if action == "**CONFIGURE**" else "unpack"
        require(action == "**CONFIGURE**" or (action.startswith("/var/cache/apt/archives/") and action.endswith(".deb")
                and ".." not in action.split("/")), "apt_action_drift")
        require(name not in actions[kind], "apt_action_drift"); actions[kind].add(name)
    require(all(names == set(wanted) for names in actions.values()), "apt_action_drift")
    return {name: len(value) for name, value in actions.items()}


def closed_map(value, allowed):
    require(type(value) is dict and set(value) <= allowed, "unknown_serve_schema")


def serve_schema(value, service=False):
    closed_map(value, {"TCP", "Web", "Tun"} if service else {"TCP", "Web", "Services", "AllowFunnel", "Foreground"})
    for key in ("TCP", "Web", "Services", "AllowFunnel", "Foreground"):
        if key in value:
            require(value[key] is None or type(value[key]) is dict, "unknown_serve_schema")
    for port, handler in (value.get("TCP") or {}).items():
        require(re.fullmatch(r"[1-9][0-9]{0,4}", port) and int(port) < 65536, "unknown_serve_schema")
        closed_map(handler, {"HTTPS", "HTTP", "TCPForward", "TerminateTLS", "ProxyProtocol"})
        require(all(type(v) is bool for k, v in handler.items() if k in ("HTTPS", "HTTP"))
                and all(type(v) is str for k, v in handler.items() if k in ("TCPForward", "TerminateTLS"))
                and ("ProxyProtocol" not in handler or type(handler["ProxyProtocol"]) is int), "unknown_serve_schema")
    for host, web in (value.get("Web") or {}).items():
        require(type(host) is str and re.fullmatch(r"[A-Za-z0-9.-]+:[1-9][0-9]{0,4}", host), "unknown_serve_schema")
        closed_map(web, {"Handlers"}); require(type(web.get("Handlers")) is dict, "unknown_serve_schema")
        for mount, handler in web["Handlers"].items():
            require(type(mount) is str and mount.startswith("/"), "unknown_serve_schema")
            closed_map(handler, {"Path", "Proxy", "Text", "AcceptAppCaps", "Redirect"})
            require(all(type(v) is str for k, v in handler.items() if k != "AcceptAppCaps")
                    and ("AcceptAppCaps" not in handler or type(handler["AcceptAppCaps"]) is list
                    and all(type(x) is str for x in handler["AcceptAppCaps"])), "unknown_serve_schema")
    for config in (value.get("Services") or {}).values():
        serve_schema(config, True)
    for enabled in (value.get("AllowFunnel") or {}).values():
        require(type(enabled) is bool, "unknown_serve_schema")
    require(not value.get("Foreground"), "foreground_serve_refused")
    if "Tun" in value:
        require(type(value["Tun"]) is bool, "unknown_serve_schema")


def ingress_after(old, dns, home):
    value = {} if old is None else decode(canonical(old)); serve_schema(value)
    target = dns + ":443"
    require(not (value.get("AllowFunnel") or {}).get(target), "target_funnel_refused")
    desired_tcp = {"HTTPS": True}
    desired_web = {"Handlers": {"/": {"Proxy": "unix:" + home + "/.local/share/claudlobby-browser/data/state/owner/owner.sock"}}}
    tcp = value.get("TCP") or {}; web = value.get("Web") or {}
    require("443" not in tcp or tcp["443"] == desired_tcp, "occupied_443")
    require(all(not key.endswith(":443") or key == target and entry == desired_web for key, entry in web.items()), "occupied_443")
    value["TCP"] = dict(tcp); value["TCP"]["443"] = desired_tcp
    value["Web"] = dict(web); value["Web"][target] = desired_web
    return value


def lookup(function, key):
    try:
        return function(key)
    except KeyError:
        return None


def lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def read_root(name, limit=65536):
    # One fixed root file, at most limit + 1 bytes so callers see oversize;
    # None when it is absent. Never follows a link.
    path = Path(PATHS[name])
    trusted_parents(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022, "unsafe_file")
        raw = b""
        while len(raw) <= limit:
            part = os.read(fd, limit + 1 - len(raw))
            if not part:
                break
            raw += part
        return raw
    finally:
        os.close(fd)


def next_uid(passwd, group):
    # The readiness probe next-uid rule, byte for byte, held to it by
    # tests/fixtures/next-uid-rule.json: lines split on newline only; passwd
    # fields 3 and 4 and group field 3 count when they are 1-10 ASCII digits;
    # each file is at most 64 KiB and 512 lines. 1 + the highest id from 1000
    # to 59999, at least 1000; None past 59999. Unreadable refuses.
    highest = 999
    for raw, indexes in ((passwd, (2, 3)), (group, (2,))):
        require(raw is not None, "team_uid_changed")
        lines = raw.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        require(len(raw) <= 65536 and len(lines) <= 512, "team_uid_changed")
        for line in lines:
            fields = line.split(b":")
            for index in indexes:
                if index < len(fields) and re.fullmatch(rb"[0-9]+", fields[index]):
                    require(len(fields[index]) <= 10, "team_uid_changed")
                    value = int(fields[index].lstrip(b"0") or b"0")
                    if 1000 <= value <= 59999:
                        highest = max(highest, value)
    return highest + 1 if highest < 59999 else None


def directory_service():
    # Accounts come from local files only: passwd and group each appear once
    # in nsswitch.conf and list nothing but files and systemd.
    raw = read_root("nsswitch")
    require(raw is not None and len(raw) <= 65536, "team_directory_service")
    seen = []
    for line in raw.split(b"\n"):
        name, colon, sources = line.split(b"#", 1)[0].partition(b":")
        if name.strip() in (b"passwd", b"group"):
            seen.append(name.strip())
            require(colon and sources.split() and set(sources.split()) <= {b"files", b"systemd"}, "team_directory_service")
    require(sorted(seen) == [b"group", b"passwd"], "team_directory_service")


def subordinate(uid):
    # A subuid or subgid line for the team or its id, or a range holding it.
    for name in ("subuid", "subgid"):
        raw = read_root(name)
        if raw is None:
            continue
        if len(raw) > 65536:
            return True
        for line in raw.split(b"\n"):
            fields = line.split(b":")
            if fields[0] in (TEAM.encode(), str(uid).encode()):
                return True
            if (len(fields) == 3 and all(re.fullmatch(rb"[0-9]{1,10}", field) for field in fields[1:])
                    and int(fields[1]) <= uid < int(fields[1]) + int(fields[2])):
                return True
    return False


def running(uid):
    # Any process with this id as a real, effective, saved or file uid or gid.
    wanted = str(uid).encode()
    for pid in os.listdir(PATHS["proc"]):
        if not re.fullmatch(r"[0-9]+", pid):
            continue
        try:
            with open(os.path.join(PATHS["proc"], pid, "status"), "rb") as stream:
                text = stream.read(65536)
        except OSError:
            continue  # it exited
        if any(line.split()[:1] in ([b"Uid:"], [b"Gid:"]) and wanted in line.split()[1:] for line in text.split(b"\n")):
            return True
    return False


def team_policy():
    # A present team account is used only as useradd below makes it.
    directory_service()
    account = pwd.getpwnam(TEAM)
    uid, gid = account.pw_uid, account.pw_gid
    users = pwd.getpwall()
    require(1000 <= uid <= 59999 and [p.pw_name for p in users if p.pw_uid == uid or p.pw_name == TEAM] == [TEAM], "team_account_uid")
    group = lookup(grp.getgrgid, gid)
    require(gid == uid and group is not None and group.gr_name == TEAM and not group.gr_mem
            and [p.pw_name for p in users if p.pw_gid == gid] == [TEAM]
            and [g.gr_name for g in grp.getgrall() if g.gr_gid == gid or g.gr_name == TEAM] == [TEAM]
            and set(os.getgrouplist(TEAM, gid)) == {gid}, "team_account_group")
    require(account.pw_shell == "/bin/bash", "team_account_shell")
    trusted_parents(Path(PATHS["home"]))
    home, mail = lstat(PATHS["home"]), lstat(PATHS["mail"])
    require(account.pw_dir == TEAM_HOME and home is not None and stat.S_ISDIR(home.st_mode) and home.st_uid == uid
            and not home.st_mode & 0o077 and (mail is None or stat.S_ISREG(mail.st_mode) and mail.st_uid == uid
            and mail.st_size == 0 and mail.st_nlink == 1), "team_account_home")
    raw = read_root("shadow", LIMIT)
    lines = [line.split(b":") for line in (raw or b"").split(b"\n") if line.split(b":")[0] == TEAM.encode()]
    require(raw is not None and len(raw) <= LIMIT and len(lines) == 1 and len(lines[0]) == 9
            and account.pw_passwd == "x" and re.fullmatch(rb"[!*]+", lines[0][1]), "team_account_password")
    # No forced change, no maximum age, no inactivity limit, no expiry.
    require(lines[0][2] != b"0" and all(lines[0][i] in (b"", b"-1") for i in (4, 6, 7)), "team_account_expiry")
    require(not subordinate(uid), "team_account_ids")


def team_plan(uid, api):
    # The team account is absent: plan it with the id the check offered,
    # which must be the next id this leaf computes, and free everywhere.
    directory_service()
    require(lookup(grp.getgrnam, TEAM) is None, "team_leftovers")
    trusted_parents(Path(PATHS["home"]))
    require(lstat(PATHS["home"]) is None, "team_home_exists")
    require(uid == next_uid(read_root("passwd"), read_root("group"))
            and lookup(pwd.getpwuid, uid) is None and lookup(grp.getgrgid, uid) is None, "team_uid_changed")
    require(not subordinate(uid) and lstat(PATHS["mail"]) is None, "team_leftovers")
    require(not running(uid) and unit_state("user@" + str(uid) + ".service").get("ActiveState") == "inactive"
            and lstat(os.path.join(PATHS["run"], str(uid))) is None, "team_uid_unavailable")
    # Never made beside another setup that already holds port 443.
    _, served = api_get(api, "serve-config")
    served = {} if served is None else served
    require(type(served) is dict and all(type(served.get(key) or {}) is dict for key in ("TCP", "Web")), "unknown_serve_schema")
    require("443" not in (served.get("TCP") or {}) and not any(key.endswith(":443") for key in served.get("Web") or {}), "occupied_443")


def team_block(selection, kind, api):
    # The proposal login block: exactly these six keys (settled at W1 review).
    present = lookup(pwd.getpwnam, TEAM) is not None
    if present:
        team_policy()
    else:
        require(kind != "ingress", "account_changed")
        if kind == "system":
            team_plan(selection["uid"], api)
    return {"account": TEAM, "uid": selection["uid"], "gid": selection["gid"], "home": TEAM_HOME,
            "shell": "/bin/bash", "state": "present" if present else "create"}


def team_state():
    try:
        if lookup(pwd.getpwnam, TEAM) is None:
            return "absent"
        team_policy()
        return "conforming"
    except Exception:
        return "unexpected"


def inspect_plan(kind, selection, api):
    team = selection["account"] == TEAM
    bound = binding(selection, api, True) if team else binding(selection, api)
    login = team_block(selection, kind, api) if team else None
    if kind == "ingress":
        etag, before = api_get(api, "serve-config")
        require(type(etag) is str and 0 < len(etag) <= 256 and "\r" not in etag and "\n" not in etag, "missing_etag")
        after = ingress_after(before, bound["dns"], selection["home"])
        return {"kind": kind, "binding": bound, "before": before, "etag": etag,
                "after": after, "no_op": before == after,
                "disclosure": "One native Serve CAS publishes the fixed selected-user Unix target. That user controls its listener. Config ETag CAS is atomic; native profile selection is checked before/after, not jointly atomic. The selected administrator must keep the native profile stable during setup. Browser reachability and native principal are unproved."}
    manager = manager_plan(selection)
    if kind == "index-refresh":
        package_health()
        return {"kind": kind, "binding": bound, "manager": manager, "apt": apt_configuration(),
                "disclosure": "Explicit apt-get update changes package indices and executes configured apt update hooks. It does not install packages."}
    plan = {"kind": kind, "binding": bound, "packages": package_plan(), "manager": manager, "disclosure": DISCLOSURE}
    if team:
        plan.update(login=login, disclosure=DISCLOSURE + " " + TEAM_DISCLOSURE)
    return plan


def observe(record, api):
    result = {"receipt_state": record["state"], "never_replay": record["state"] != "planned"}
    if record["state"] == "planned":
        return result
    if record["proposal"]["kind"] == "ingress":
        result["causality"] = "not_proved_by_status"
        try:
            current = binding(record["selection"], api)
            result["context_continuity_observed"] = current == record["proposal"]["binding"]
        except Exception:
            result["context_continuity_observed"] = "unknown"
        try:
            _, after = api_get(api, "serve-config")
            result["postcondition_observed"] = after == record["proposal"]["after"]
        except Exception:
            result["postcondition_observed"] = "unknown"
        return result
    result["state"] = record["state"] if record["state"] in TERMINAL else "unknown"
    try:
        unit = unit_state(unit_name(record["request_id"])); result["native_unit"] = unit
        result["same_boot"] = boot_id() == record["dispatch"]["boot_id"]
        if record["state"] not in TERMINAL:
            if result["same_boot"] and unit.get("ActiveState") in ("active", "activating"):
                invocation = record.get("invocation_id")
                result["state"] = "running" if invocation and invocation == unit.get("InvocationID") else "unknown"
    except Exception:
        result["native_unit_unavailable"] = True
    if result["state"] == "running":
        result["package_health"] = "in_progress"
    else:
        try:
            package_health(); result["package_health"] = "clean"
        except Exception:
            result["package_health"] = "unknown"
    if "login" in record["proposal"]:
        result["team_account"] = team_state()  # conforming, absent or unexpected
    return result


def no_pending(api=None):
    remnants = 0
    for path in STORE.iterdir():
        name = path.name
        if re.fullmatch(r"\.write-[a-z0-9_]{8}", name):
            remnants += 1
            require(remnants <= 16, "foreign_store_content")
            read_file(path)  # validate private bounded staging file, preserve it
            continue
        require(name == "transition.lock" or re.fullmatch(r"bundle-[0-9a-f]{64}\.sh", name)
                or name.endswith(".json") and valid_uuid(name[:-5]), "foreign_store_content")
        if not name.endswith(".json"):
            continue
        record = load(name[:-5])
        if record["state"] == "planned":
            continue
        if record["proposal"]["kind"] == "ingress":
            if record["state"] not in TERMINAL:
                observed = observe(record, api)
                require(observed.get("postcondition_observed") is True
                        and observed.get("context_continuity_observed") is True, "prior_operation_uncertain")
            continue
        require("dispatch" in record, "prior_operation_uncertain")
        unit = unit_state(unit_name(record["request_id"]))
        same_boot = boot_id() == record["dispatch"]["boot_id"]
        loaded_inactive = unit.get("LoadState") == "loaded" and unit.get("ActiveState") in ("inactive", "failed") and unit.get("MainPID") == "0"
        # Completed native units may be collected on this boot. Only a retained
        # terminal result permits same-boot absence; uncertain dispatch does not.
        absent_admitted = (record["state"] in TERMINAL or not same_boot) and unit.get("LoadState") == "not-found" and unit.get("ActiveState") == "inactive" and unit.get("MainPID") == "0"
        require(loaded_inactive or absent_admitted, "prior_operation_not_inactive")
        package_health()  # a new ingress UUID cannot bypass dirty prior packages


def prune():
    # Receipt hygiene before a new record is saved: remove planned receipts
    # past their expiry, which can never be applied. Every other state stays.
    # load reads through the store checks and never follows a link.
    for path in STORE.iterdir():
        if path.name.endswith(".json") and valid_uuid(path.name[:-5]):
            record = load(path.name[:-5])
            if record["state"] == "planned" and type(record.get("expires_at")) is int and time.time() > record["expires_at"]:
                os.unlink(path)


def execute(verb, value, source, api):
    validate_request(verb, value)
    require(source.is_absolute(), "absolute_artifact_required")
    # Source identity is external to its bytes, never a self-referential pin.
    raw = verify_artifact(source, value["artifact_sha256"])
    if verb == "status" and not STORE.exists():
        return {"state": "absent", "never_replay": True}
    lock = lock_store(verb == "inspect")
    try:
        path = receipt_path(value["request_id"])
        if path.exists() or path.is_symlink():
            record = load(value["request_id"])
            matches = record["selection"] == value["selection"] if verb == "apply" else all(
                record["selection"].get(key) == item for key, item in value["selection"].items())
            require(matches and record["artifact_sha256"] == value["artifact_sha256"], "request_identity_conflict")
            if verb == "inspect":
                require(record["proposal"]["kind"] == value["kind"], "request_identity_conflict")
                return record
            if verb == "status":
                return {"record": record, "observation": observe(record, api)}
        else:
            require(verb == "inspect", "original_receipt_absent")
            no_pending(api)
            selected = derive_selection(value["selection"], api)
            proposal = inspect_plan(value["kind"], selected, api)
            require(len(canonical(proposal)) <= RECORD_BUDGET - 4096, "proposal_oversize")
            artifact = artifact_path(value["artifact_sha256"])
            if artifact.exists() or artifact.is_symlink():
                verify_artifact(artifact, value["artifact_sha256"])
            else:
                write_file(artifact, raw, 0o500)
            record = {"schema_version": 1, "request_id": value["request_id"], "selection": selected,
                "artifact_sha256": value["artifact_sha256"], "proposal": proposal, "digest": digest(proposal),
                "created_at": int(time.time()), "expires_at": int(time.time()) + TTL, "state": "planned"}
            require(len(canonical(record)) <= RECORD_BUDGET, "proposal_oversize")
            prune(); save(record); return record
        require(record["digest"] == value["digest"] and value["decision"] == "approve", "proposal_conflict")
        if record["state"] != "planned":
            return {"record": record, "observation": observe(record, api)}
        require(time.time() <= record["expires_at"], "proposal_expired")
        no_pending(api)
        verify_artifact(artifact_path(record["artifact_sha256"]), record["artifact_sha256"])
        try:
            current = inspect_plan(record["proposal"]["kind"], record["selection"], api)
        except Refusal as exc:
            # After approval, a team account refusal means the computer changed.
            require(not exc.code.startswith("team_") and (exc.code != "occupied_443"
                    or record["proposal"]["kind"] != "system"), "proposal_changed")
            raise
        require(current == record["proposal"], "proposal_changed")
        if record["proposal"].get("no_op"):
            record["state"] = "completed"; record["effect"] = "none_existing_exact_route"
            save(record); return record
        record["state"] = "dispatched"
        record["dispatch"] = {"boot_id": boot_id(), "submitted_at": int(time.time())}
        save(record)  # fsync intent BEFORE the first potentially effective call
        if record["proposal"]["kind"] == "ingress":
            post_attempted = False
            try:
                binding_now = binding(record["selection"], api)
                require(binding_now == record["proposal"]["binding"], "node_or_profile_changed")
                post_attempted = True
                code, _, _ = api("POST", "serve-config", record["proposal"]["after"], record["proposal"]["etag"])
                record["response_code"] = code
                if code == 412:
                    record["state"] = "conflict"
                elif code == 200:
                    after_binding = binding(record["selection"], api)
                    after_etag, after = api_get(api, "serve-config")
                    record["after_etag"] = after_etag
                    record["after_digest"] = digest(after)
                    record["state"] = "completed" if after_binding == binding_now and after == record["proposal"]["after"] else "unknown"
                else:
                    record["state"] = "unknown"
            except (OSError, Refusal, http.client.HTTPException):
                record["state"] = "unknown" if post_attempted else "failed_partial"
                record["post_attempted"] = post_attempted
            save(record); return record
        try:
            native(["/usr/bin/systemd-run", "--unit=" + unit_name(record["request_id"]), "--service-type=exec",
                "--property=Restart=no", "--property=TimeoutStartSec=infinity", "--property=RuntimeMaxSec=infinity",
                "--property=RemainAfterExit=no", "--property=StandardOutput=null", "--property=StandardError=null",
                "/bin/sh", str(artifact_path(record["artifact_sha256"])), "_worker", record["request_id"]])
            record["submission"] = "exec_acknowledged"
        except (OSError, Refusal, subprocess.SubprocessError):
            record["submission"] = "unknown"
        save(record); return record
    finally:
        os.close(lock)


def private_record(request_id, source):
    require(os.geteuid() == 0, "root_required")
    record = load(request_id)
    require(source == artifact_path(record["artifact_sha256"]), "private_artifact_required")
    verify_artifact(source, record["artifact_sha256"])
    return record


def private_hook(request_id, source):
    record = private_record(request_id, source)
    require(record["state"] == "running" and record["proposal"]["kind"] == "system"
            and record.get("invocation_id") == os.environ.get("INVOCATION_ID") and record["dispatch"]["boot_id"] == boot_id(), "hook_not_admitted")
    require(os.environ.get("APT_HOOK_INFO_FD") == "0", "apt_hook_fd_refused")
    raw = sys.stdin.buffer.read(LIMIT + 1)
    require(len(raw) <= LIMIT, "apt_hook_oversize")
    require(apt_configuration() == {k: record["proposal"]["packages"][k] for k in ("config_digest", "hooks")}, "apt_configuration_changed")
    verified = verify_hook(raw.decode("utf-8", "strict"), record["proposal"]["packages"]["actions"])
    lock = lock_store(blocking=True)
    try:
        current = private_record(request_id, source)
        require(current["state"] == "running" and current.get("invocation_id") == record["invocation_id"], "hook_receipt_changed")
        current["hook_verified"] = {"invocation_id": record["invocation_id"], "actions_digest": digest(record["proposal"]["packages"]["actions"]), "counts": verified}
        save(current)
    finally:
        os.close(lock)
    # A fresh whole transaction for every invocation is deliberate fail-closed.
    # Whether apt emits this whole set once is a required cold-interface gate.


def private_worker(request_id, source, api):
    lock = lock_store(blocking=True)
    try:
        record = private_record(request_id, source)
        require(record["state"] == "dispatched" and record["proposal"]["kind"] != "ingress", "worker_not_admitted")
        unit = unit_state(unit_name(request_id))
        invocation = os.environ.get("INVOCATION_ID", "")
        require(re.fullmatch(r"[0-9a-f]{32}", invocation) and unit.get("InvocationID") == invocation
                and unit.get("MainPID") == str(os.getpid()), "worker_invocation_mismatch")
        record["state"] = "running"; record["invocation_id"] = invocation; save(record)
    finally:
        os.close(lock)
    rc = None
    try:
        require(record["dispatch"]["boot_id"] == boot_id(), "worker_boot_changed")
        team = record["selection"]["account"] == TEAM
        require((binding(record["selection"], api, True) if team else binding(record["selection"], api))
                == record["proposal"]["binding"], "binding_changed")
        if record["proposal"]["kind"] == "index-refresh":
            require(inspect_plan("index-refresh", record["selection"], api) == record["proposal"], "proposal_changed")
            rc = native(apt_base() + ["update"], effect=True)
        else:
            packages = record["proposal"]["packages"]
            require(package_plan() == packages and manager_plan(record["selection"]) == record["proposal"]["manager"], "proposal_changed")
            if team:
                login = team_block(record["selection"], "system", api)
                require(login == record["proposal"].get("login"), "proposal_changed")
                if login["state"] == "create":
                    # The one account effect, once and before apt: fixed
                    # arguments, and the uid team_block just held to the
                    # next id this leaf computes from the same files.
                    rc = native(["/usr/sbin/useradd", "--uid", str(login["uid"]), "--user-group", "--create-home",
                        "--home-dir", TEAM_HOME, "--shell", "/bin/bash", "--comment", "Claudlobby team",
                        "--expiredate", "", "--inactive", "-1", "--key", "HOME_MODE=0700", "--key", "SUB_UID_COUNT=0",
                        "--key", "SUB_GID_COUNT=0", "--key", "PASS_MAX_DAYS=-1", "--", TEAM], effect=True)
                    require(rc == 0, "useradd_failed")
                    team_policy()
            if packages["actions"]:
                hook = str(source) + " _hook " + request_id
                # apt selects Tools options by executable name (first hook token).
                argv = apt_base() + ["--assume-yes", "-o", "DPkg::Options::=--force-confdef",
                    "-o", "DPkg::Options::=--force-confold", "-o", "DPkg::Pre-Install-Pkgs::=" + hook,
                    "-o", "DPkg::Tools::Options::" + str(source) + "::Version=3", "-o", "DPkg::Tools::Options::" + str(source) + "::InfoFD=0",
                    "install", "--"] + [name + "=" + next(row["new"] for row in packages["actions"] if row["package"] == name) for name in packages["seeds"]]
                rc = native(argv, effect=True)
            else:
                rc = 0
            require(rc == 0, "package_effect_failed")
            current = load(request_id)
            if packages["actions"]:
                expected_verification = {"invocation_id": record["invocation_id"], "actions_digest": digest(packages["actions"]),
                    "counts": {"unpack": len(packages["actions"]), "configure": len(packages["actions"])}}
                require(current.get("hook_verified") == expected_verification, "apt_verifier_not_observed")
                record["hook_verified"] = current["hook_verified"]
            package_health()
            require(binding(record["selection"], api) == record["proposal"]["binding"], "binding_changed")
            manager = manager_plan(record["selection"])
            require(manager == record["proposal"]["manager"], "manager_changed")
            if not manager["linger"] or any(v.get("ActiveState") != "active" for v in manager_runtime(record["selection"]).values()):
                require(native(["/usr/bin/loginctl", "enable-linger", record["selection"]["account"]], effect=True) == 0, "linger_failed")
            record["manager_observation"] = wait_manager(record["selection"], manager)
        require(rc == 0, "native_effect_failed"); package_health()
        record["state"] = "completed"
    except Exception as exc:
        record["state"] = "failed_partial"; record["code"] = exc.code if isinstance(exc, Refusal) else "worker_internal_failure"
    record["native_returncode"] = rc
    lock = lock_store(blocking=True)
    try:
        current = load(request_id)
        require(current["state"] == "running" and current.get("invocation_id") == record["invocation_id"], "worker_receipt_changed")
        # Hooks update the same retained receipt after worker admission. Merge
        # final state into current bytes so failure preserves their proof too.
        verified = current.get("hook_verified")
        current.update(record)
        if verified and verified.get("invocation_id") == record["invocation_id"]:
            current["hook_verified"] = verified
        save(current)
    finally:
        os.close(lock)


def main():
    try:
        verb = sys.argv[1]; source = Path(sys.argv[2])
        require(source.is_absolute(), "absolute_artifact_required")
        if verb in ("_worker", "_hook"):
            require(len(sys.argv) == 4, "invalid_request")
            (private_worker(sys.argv[3], source, LocalAPI()) if verb == "_worker" else private_hook(sys.argv[3], source))
            return 0
        require(os.geteuid() == 0, "root_required")
        value = read_request(verb)
        result = execute(verb, value, source, LocalAPI())
        raw = canonical({"schema_version": 1, "result": result})
        require(len(raw) <= LIMIT, "result_oversize")
        sys.stdout.buffer.write(raw + b"\n"); return 0
    except (Refusal, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, http.client.HTTPException) as exc:
        code = exc.code if isinstance(exc, Refusal) else "inspection_unavailable"
        sys.stdout.buffer.write(canonical({"schema_version": 1, "state": "refused", "code": code}) + b"\n")
        return 4


if __name__ == "__main__":
    sys.exit(main())
# END EMBEDDED PYTHON
' "$1" "$0" "${2-}"
