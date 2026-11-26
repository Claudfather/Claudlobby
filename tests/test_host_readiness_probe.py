"""Preinstall inventory is bounded data; prerequisites are never executed."""

import ast
import builtins
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import symtable
import sys
import time
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "claudlobby/preinstall/host-readiness-probe.sh"
LEAF = ROOT / "claudlobby/preinstall/linux-system-v1.sh"
# Read once: the leaf fixtures below patch pathlib reads while they run.
PROBE_SOURCE = PROBE.read_text()
LEAF_SOURCE = LEAF.read_text()
TOOLS = ("python3", "git", "tailscale", "claude", "tmux", "jq", "systemctl", "launchctl")
# The shipped probe runs these tools and reads these root files by fixed
# absolute path. Fixtures run a copy with each one rewritten into the test's
# own directory; the shipped bytes honour no override.
TOOL_PATHS = {"/usr/bin/uname": "uname", "/usr/bin/id": "id", "/bin/dd": "dd", "/usr/bin/tr": "tr"}
# The interpreter for the team account check. Fixtures point it at a stand-in,
# or at nothing (the default: no Python, so the check reads unknown).
PYTHON = "/usr/bin/python3"
ROOT_PATHS = {"/etc/passwd": "passwd", "/etc/group": "group",
              "/var/lib/claudlobby-browser-system/v1": "lib/claudlobby-browser-system/v1"}
STORE = ROOT_PATHS["/var/lib/claudlobby-browser-system/v1"]
LOGIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
# The codes the leaf refuses a present team account with: the only policy
# words besides conforms and unknown the website admits.
TEAM_CODES = ("team_directory_service", "team_account_uid", "team_account_group", "team_account_shell",
              "team_account_home", "team_account_password", "team_account_expiry", "team_account_ids",
              "team_account_sudo")
POLICIES = ("conforms", "unknown") + TEAM_CODES


def whole(value, low, high):
    return type(value) is int and low <= value <= high


def website_accepts(logins):
    """The website's logins rule (lib/host-readiness.ts), with the two keys
    this probe adds: team.policy and uncertain. The website's decoder today
    admits exactly {team, next_uid, prepared} and team {uid}, so it must adopt
    these before it pins this probe. An answer it refuses makes the whole
    check unusable."""
    if logins is None:
        return True
    if type(logins) is not dict or set(logins) != {"team", "next_uid", "prepared", "uncertain"}:
        return False
    team, next_uid, prepared = logins["team"], logins["next_uid"], logins["prepared"]
    if logins["uncertain"] not in (True, False, None):
        return False
    if team is not None and not (type(team) is dict and set(team) == {"uid", "policy"}
                                 and whole(team["uid"], 0, 9999999999)
                                 and team["policy"] in POLICIES):
        return False
    if next_uid is not None and not whole(next_uid, 1000, 59999):
        return False
    if prepared is None:
        return True
    return (type(prepared) is list and len(prepared) <= 2
            and all(type(entry) is dict and set(entry) == {"name", "uid"}
                    and type(entry["name"]) is str and LOGIN_NAME.fullmatch(entry["name"])
                    and entry["name"] != "claudlobby" and whole(entry["uid"], 1000, 59999)
                    for entry in prepared)
            and len({entry["name"] for entry in prepared}) == len(prepared)
            and len({entry["uid"] for entry in prepared}) == len(prepared))


def rewritten_probe(path, stubs, files=None, python=None):
    source = PROBE_SOURCE
    for real, name in TOOL_PATHS.items():
        assert real + " " in source
        source = source.replace(real + " ", str(stubs / name) + " ")
    for real, name in ROOT_PATHS.items():
        if files is not None:
            source = source.replace(real, str(files / name))
    assert source.count(PYTHON + " ") == 3  # the header, the -x test and the one run
    source = source.replace(PYTHON, str(python or path.parent / "no-python3"))
    assert not any(real + " " in source for real in TOOL_PATHS)
    assert files is None or not any(real in source for real in ROOT_PATHS)
    path.write_text(source)
    return path


@pytest.fixture
def inventory(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    files = tmp_path / "root"
    (files / STORE).parent.mkdir(parents=True)
    work = tmp_path / "cwd"
    work.mkdir()
    marker = tmp_path / "executed-prerequisite"
    real = {name: path for path, name in TOOL_PATHS.items()}
    for name in ("dd", "tr"):
        (stubs / name).symlink_to(real[name])

    def run(*, system="Linux", architecture=b"aarch64\n", uid=b"1001\n",
            user=b"example-user\n", present=TOOLS, args=(), failed=None, reader=None,
            root_paths=True, env=(), python=None):
        if reader:
            name, mode = reader
            path = stubs / name
            path.unlink()
            if mode != "missing":
                command = "'" + real[name] + "' \"$@\"\n" if mode == "failed-after-output" else ""
                path.write_text("#!/bin/sh\n" + command + "exit 1\n")
                path.chmod(0o755)
        # uname/id accept exactly the operation's fixed fact arguments.
        for name, variants in (("uname", {"-s": system.encode() + b"\n", "-m": architecture}),
                               ("id", {"-u": uid, "-un": user})):
            lines = ["#!/bin/sh", 'case "$*" in']
            for argument, value in variants.items():
                data = tmp_path / (name + argument + "-output")
                data.write_bytes(value)
                lines.append(argument + ") /bin/cat '" + str(data) + "'; exit "
                             + ("1" if failed == name + argument else "0") + ";;")
            lines.extend(["*) exit 99;;", "esac", ""])
            path = stubs / name
            path.write_text("\n".join(lines))
            path.chmod(0o755)
        for name in TOOLS:
            path = stubs / name
            if path.exists():
                path.unlink()
            if name in present:
                path.write_text("#!/bin/sh\nprintf invoked > '" + str(marker) + "'\nexit 99\n")
                path.chmod(0o755)
        probe = rewritten_probe(tmp_path / "probe.sh", stubs, files if root_paths else None, python)
        result = subprocess.run(["/bin/sh", str(probe), *args], cwd=work,
            env=constructed_env(PATH=stubs, USER="forged", LOGNAME="forged", **dict(env)),
            input=b"ignored input", capture_output=True, timeout=30)
        run.last = result
        assert result.stderr == b""
        assert len(result.stdout) <= 8192
        assert result.stdout.endswith(b"\n") and result.stdout.count(b"\n") == 1
        assert not marker.exists(), "inventory executed a prerequisite or native-manager CLI"
        assert not any(work.iterdir()), "inventory executed something it read"
        data = json.loads(result.stdout)
        if "logins" in data:
            assert website_accepts(data["logins"]), data["logins"]
        return result, data

    run.files, run.stubs, run.last = files, stubs, None
    return run


@pytest.mark.parametrize("system,expected", [("Linux", "systemd_user"), ("Darwin", "launchd_user")])
def test_presence_never_claims_functional_readiness(inventory, system, expected):
    result, data = inventory(system=system)
    assert result.returncode == 0
    assert data["schema_version"] == 1
    assert data["operation"] == "host-readiness-v1"
    assert data["os"] == {"Linux": "linux", "Darwin": "macos"}[system]
    assert data["architecture"] == "aarch64"
    assert data["account"] == {"uid": 1001, "name": "example-user"}
    for name in TOOLS[:6]:
        assert data["tools"][name] == {"present": True, "version": None, "readiness": "unknown"}
    assert data["native_manager"] == {"expected": expected,
        "systemctl_present": True, "launchctl_present": True,
        "readiness": "unknown", "lingering": "unknown"}
    assert data["checks"] == {"python_minimum_version": "unknown", "python_venv": "unknown",
        "python_ssl": "unknown", "provider_authentication": "unknown",
        "tailscale_access": "unknown"}
    assert data["logins"] is None


def test_absent_tools_do_not_hide_unknown_functional_checks(inventory):
    result, data = inventory(present=())
    assert result.returncode == 0
    assert all(value == {"present": False, "version": None, "readiness": "unknown"}
               for value in data["tools"].values())
    assert data["native_manager"]["systemctl_present"] is False
    assert data["native_manager"]["launchctl_present"] is False
    assert data["native_manager"]["readiness"] == "unknown"


@pytest.mark.parametrize("change", [
    {"uid": b"01\n"}, {"uid": b"-1\n"}, {"uid": b"1001,\"root\":true\n"},
    {"user": b'bad"user\n'}, {"user": b"name\nother\n"}, {"user": b"x" * 1024},
    {"user": b"a\x00lice\n"}, {"user": b"\xc3\xa9\n"}, {"user": b"\n"}, {"architecture": b"arm64\nextra\n"},
    {"architecture": b"x" * 1024}, {"system": 'Linux"'},
    {"failed": "id-u"}, {"failed": "id-un"}, {"failed": "uname-s"}, {"failed": "uname-m"},
], ids=["leading-zero-uid", "negative-uid", "uid-injection", "quote-user", "multiline-user",
        "oversize-user", "nul-user", "non-ascii-user", "empty-user", "multiline-arch", "oversize-arch",
        "invalid-os", "uid-failure", "user-failure", "os-failure", "arch-failure"])
def test_malformed_or_failed_facts_refuse_without_echoing_output(inventory, change):
    result, data = inventory(**change)
    assert result.returncode == 1
    assert data == {"schema_version": 1, "operation": "host-readiness-v1", "error": "inventory_unavailable"}


def test_arguments_cannot_select_an_operation_or_command(inventory):
    result, data = inventory(args=("--command", "claude"))
    assert result.returncode == 2
    assert data["error"] == "arguments_not_supported"


def test_inventory_runs_in_standalone_posix_shell_with_real_local_facts(tmp_path):
    result = subprocess.run(["/bin/sh", str(PROBE)], cwd=tmp_path,
        env=constructed_env(), input=b"", capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    data = json.loads(result.stdout)
    assert data["account"]["uid"] == os.getuid()
    if os.getuid() != 0:
        assert data["logins"] is None
    assert len(result.stdout) <= 8192 and result.stdout.count(b"\n") == 1


@pytest.mark.parametrize("user", [b"alice\n\n0" + b"\n" * 1024,
                                 b"alice\n\nS0" + b"\n" * 1024],
                         ids=["legacy-status-spoof", "framed-status-spoof"])
def test_truncated_newline_suffix_cannot_forge_a_complete_fact(inventory, user):
    result, data = inventory(user=user)
    assert result.returncode == 1
    assert data["error"] == "inventory_unavailable"


@pytest.mark.parametrize("name", ["dd", "tr"])
@pytest.mark.parametrize("mode", ["missing", "failed-empty", "failed-after-output"])
def test_reader_failure_refuses_even_after_complete_output(inventory, name, mode):
    result, data = inventory(reader=(name, mode))
    assert result.returncode == 1
    assert data["error"] == "inventory_unavailable"


@pytest.mark.parametrize("length,expected_status", [(64, 0), (65, 1)])
def test_account_name_length_limit(inventory, length, expected_status):
    result, data = inventory(user=b"u" * length + b"\n")
    assert result.returncode == expected_status
    if expected_status == 0:
        assert data["account"]["name"] == "u" * 64
    else:
        assert data["error"] == "inventory_unavailable"


# What the team check block uses, all of it read-only: each module's members,
# the methods it calls on values (str, bytes, set and Path.lstat), and the
# builtins it names.
BLOCK_MODULES = {
    "os": {"open", "O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "fstat", "read", "close", "lstat", "listdir",
           "getgrouplist", "geteuid"},
    "re": {"I", "fullmatch", "match", "search", "sub"},
    "signal": {"signal", "SIGALRM", "SIG_DFL", "pthread_sigmask", "SIG_UNBLOCK", "alarm"},
    "stat": {"S_ISDIR", "S_ISREG"},
    "pwd": {"getpwall", "getpwnam"},
    "grp": {"getgrall", "getgrgid"},
}
BLOCK_METHODS = {"add", "encode", "endswith", "lower", "lstat", "replace", "split", "startswith", "strip"}
BLOCK_BUILTINS = {"Exception", "FileNotFoundError", "KeyError", "SystemExit", "__name__", "all", "bytes", "int", "len",
                  "print", "reversed", "set", "sorted", "str"}
# Every name it calls: its own definitions, lookup's function argument, Path
# and those builtins.
BLOCK_CALLS = {"Refusal", "require", "trusted_parents", "lookup", "lstat", "read_root", "directory_service",
               "subordinate", "team_lines", "sudo_rules", "team_policy", "main", "function", "Path", "SystemExit", "all",
               "bytes", "int", "len", "print", "reversed", "set", "sorted", "str"}


def read_only_problems(block):
    """Why block may do more than read; empty when it cannot. The module
    members it uses, the methods it calls, the names it calls and the
    builtins it names are exactly those above, and it opens a file once,
    read-only. Any other (os.remove, os.truncate, os.utime, os.link,
    os.symlink, os.kill, Path.write_text, getattr, open, ...) is refused
    until it is shown to only read."""
    tree = ast.parse(block)
    members = {f"{node.value.id}.{node.attr}" for node in ast.walk(tree) if isinstance(node, ast.Attribute)
               and isinstance(node.value, ast.Name) and node.value.id in BLOCK_MODULES}
    found = {
        "module members": (members, {f"{module}.{name}" for module, names in BLOCK_MODULES.items() for name in names}),
        "methods": ({node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and root_name(node.func) not in BLOCK_MODULES},
                    BLOCK_METHODS),
        "calls": ({node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Name)}, BLOCK_CALLS),
        "builtins": ({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} & set(dir(builtins)),
                     BLOCK_BUILTINS),
        "opens": ((block.count("open("), block.count("os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)")),
                  (1, 1)),
    }
    return [f"{kind}: {sorted(used - allowed)} added, {sorted(allowed - used)} gone" if isinstance(used, set)
            else f"{kind}: {used}, not {allowed}" for kind, (used, allowed) in found.items() if used != allowed]


@pytest.mark.parametrize("call", ["os.remove(path)", "os.truncate(path, 0)", "os.utime(path)", "os.link(path, path)",
                                  "os.symlink(path, path)", "os.kill(0, 9)", "Path(path).write_text('')",
                                  "getattr(os, 'remove')(path)", "open(path, 'w')", "os.path.exists(path)",
                                  "os.chmod(path, 0o777)", "os.open(path, os.O_WRONLY)", "__import__('os')"])
def test_the_read_only_check_refuses_anything_else(call):
    block = team_check_block()
    old = "def lstat(path):\n"
    assert block.count(old) == 1
    drifted = block.replace(old, old + "    " + call + "\n")
    ast.parse(drifted)
    assert read_only_problems(drifted) != []


def test_the_probe_as_sent_stays_within_its_bounds():
    # The website's carrier sends the probe as one argument to the login
    # shell: sh -c '<probe>'. Linux takes at most 131,072 bytes in one
    # argument (MAX_ARG_STRLEN). The probe is 25,912 bytes as sent, so a
    # 32 KiB budget leaves about 6.8 KB for growth, and the budget itself is
    # a quarter of the hard limit.
    command = b"sh -c '" + PROBE.read_bytes().replace(b"'", b"'\\''") + b"'"
    assert len(command) <= 32768
    assert len(command) < 131072


def test_shipped_probe_runs_fixed_tools_and_reads_fixed_files():
    source = PROBE_SOURCE
    block = team_check_block()
    assert source.count(block) == 1
    shell = source.replace(block, "")
    code = [line for line in shell.splitlines()[1:] if not line.lstrip().startswith("#")]
    text = "\n".join(code)
    # Every executable the shell names is one of four tools or the Python
    # interpreter, by absolute path, and those four hold the real tools on
    # macOS and Ubuntu alike (Python runs only as root on Linux).
    assert set(re.findall(r"/(?:usr/)?s?bin/[\w.-]+", text)) == set(TOOL_PATHS) | {PYTHON}
    for path in TOOL_PATHS:
        assert re.search(r"(?<![\w/.-])" + re.escape(path) + " ", text)
        assert os.access(path, os.X_OK)
    # Python runs once, isolated, on the fixed block and nothing else.
    assert re.findall(r"(?<![\w/.-])" + re.escape(PYTHON) + r"\b.*", text) == [
        PYTHON + ' -I -S -B -c "$team_check" </dev/null ;;', PYTHON + " ] || return 0"]
    # In the block, /bin/bash is only the shell the account must have.
    assert re.findall(r"/(?:usr/)?s?bin/[\w.-]+", block) == ["/bin/bash"]
    # It only reads (read_only_problems).
    assert read_only_problems(block) == []
    # command is only ever a lookup, never a way to run a tool by name.
    assert re.findall(r"\bcommand\b(?! -v )", text) == []
    # The files are fixed assignments. No ${name-default} form exists, so the
    # environment cannot move them, and the store is the leaf's own.
    for line in ("passwd_file=/etc/passwd", "group_file=/etc/group",
                 "store=/var/lib/claudlobby-browser-system/v1"):
        assert code.count(line) == 1
    assert re.findall(r"\$\{\w+:?[-=?+]", text) == []
    assert 'STORE = Path("/var/lib/claudlobby-browser-system/v1")' in LEAF_SOURCE


# Root mode. Fixtures write passwd, group and setup's store; the copy reads them.

BASE_PASSWD = ["root:x:0:0:root:/root:/bin/bash", "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin",
               "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin"]
BASE_GROUP = ["root:x:0:", "sudo:x:27:", "nogroup:x:65534:"]
NODE_KEY = "nodekey:" + "a" * 64
# The probe never reads home, so every account here shares one placeholder.
HOME = "/home/user"


def user(name, uid, gid=None):
    return f"{name}:x:{uid}:{uid if gid is None else gid}:{name}:{HOME}:/bin/bash"


def selection(account, uid):
    return {"account": account, "gid": uid, "home": HOME, "node_key": NODE_KEY,
            "profile_id": "p001", "uid": uid}


def canonical(value):
    # The leaf's own encoding (linux-system-v1.sh canonical()).
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def receipt(account="demo", uid=1000, *, kind="system", state="completed", hooks=(), home=None, extra=()):
    """A receipt shaped like the leaf's: full proposal, binding and completion
    fields. extra adds top-level keys the leaf never writes."""
    chosen = selection(account, uid)
    if home is not None:
        chosen["home"] = home
    binding = {"selection": chosen, "dns": "box.example.ts.net", "version": "1.104.7",
               "machine": "9" * 64, "profile_digest": "e" * 64,
               "daemon": {"pid": 812, "start": "4242", "binary_sha256": "f" * 64,
                          "binary": [1, 2, 3, 4], "socket": [5, 6, 7]}}
    if kind == "ingress":
        proposal = {"after": {"TCP": {"443": {"HTTPS": True}}}, "before": None, "etag": "e1", "no_op": False}
    else:
        proposal = {"manager": {"linger": True, "units": {}, "ordinary_user_bus": "unproved"}}
        apt = {"config_digest": "d" * 64, "hooks": list(hooks)}
        if kind == "system":
            proposal["packages"] = {"seeds": [], "actions": [], "architecture": "amd64", **apt}
        else:
            proposal["apt"] = apt
    proposal.update(kind=kind, binding=binding, disclosure="Fixture disclosure.")
    record = {"schema_version": 1, "request_id": "11111111-1111-4111-8111-111111111111",
              "selection": chosen, "artifact_sha256": "b" * 64, "proposal": proposal,
              "digest": "c" * 64, "created_at": 1700000000, "expires_at": 1700000600, "state": state}
    if state != "planned":
        record.update(dispatch={"boot_id": "22222222-2222-4222-8222-222222222222",
                                "submitted_at": 1700000001}, submission="exec_acknowledged")
    if state in ("running", "completed", "failed_partial"):
        record["invocation_id"] = "1" * 32
    if state in ("completed", "failed_partial"):
        record["native_returncode"] = 0
    if state == "failed_partial":
        record["code"] = "linger_failed"
    record.update(extra)
    return canonical(record)


def sized(size, **changes):
    base = len(receipt(hooks=[""], **changes))
    data = receipt(hooks=["x" * (size - base)], **changes)
    assert len(data) == size
    return data


@pytest.fixture
def host(inventory):
    """Run as root on Linux. passwd and group lines are added to a base set
    (None leaves the file as it is); receipts, when given, replace the store."""
    files = inventory.files
    store = files / STORE

    def run(passwd=(), group=(), receipts=None, **changes):
        for name, base, lines in (("passwd", BASE_PASSWD, passwd), ("group", BASE_GROUP, group)):
            if lines is not None:
                (files / name).write_text("".join(line + "\n" for line in [*base, *lines]))
        if receipts is not None:
            store.mkdir(mode=0o700, exist_ok=True)
            for old in store.glob("*.json"):
                old.unlink()
            for index, data in enumerate(receipts):
                (store / f"{index:08d}-0000-4000-8000-000000000000.json").write_bytes(data)
        result, data = inventory(**{"uid": b"0\n", "user": b"root\n", **changes})
        assert result.returncode == 0
        return data["logins"]

    run.files, run.store, run.inventory = files, store, inventory
    return run


DEMO = {"name": "demo", "uid": 1000}


@pytest.mark.parametrize("system,uid", [("Linux", b"1001\n"), ("Darwin", b"0\n"), ("Darwin", b"501\n")],
                         ids=["linux-non-root", "mac-root", "mac-non-root"])
def test_logins_are_read_only_by_root_on_linux(host, system, uid):
    accounts = {"passwd": [user("demo", 1000), user("claudlobby", 1001)], "receipts": [receipt()]}
    assert host(**accounts) == {"team": {"uid": 1001, "policy": "unknown"}, "next_uid": 1002,
                                "prepared": [DEMO], "uncertain": False}
    assert host(**accounts, system=system, uid=uid) is None


def test_an_untouched_computer_offers_the_first_free_id(host):
    # No team line: team stays null, with no policy, and Python never runs.
    assert host() == {"team": None, "next_uid": 1000, "prepared": [], "uncertain": False}
    host.store.mkdir(mode=0o700)
    assert host() == {"team": None, "next_uid": 1000, "prepared": [], "uncertain": False}


@pytest.mark.parametrize("ids,expected", [((), 1000), ((999,), 1000), ((1000,), 1001), ((59998,), 59999),
                                          ((59999,), None), ((60000, 1500), 1501), ((4294967295, 1200), 1201)],
                         ids=["none", "999", "1000", "59998", "59999", "60000", "ten-digits"])
def test_next_uid_is_one_past_the_highest_id_from_1000_to_59999(host, ids, expected):
    assert host(passwd=[user(f"u{n}", n) for n in ids])["next_uid"] == expected


RULE = json.loads((ROOT / "tests/fixtures/next-uid-rule.json").read_text())


def rule_bytes(parts):
    return b"".join(text.encode("latin-1") * count for text, count in parts)


def next_uid_rule(passwd, group):
    """The probe's next-uid rule in Python, as the leaf must compute it.
    Returns (readable, next_uid); readable False is logins:null."""
    highest = 999
    for data, indexes in ((passwd, (2, 3)), (group, (2,))):
        lines = data.split(b"\n")
        if lines[-1] == b"":
            lines.pop()
        if len(data) > 65536 or len(lines) > 512:
            return False, None
        for line in lines:
            fields = line.split(b":")
            for index in indexes:
                if index < len(fields) and re.fullmatch(rb"[0-9]+", fields[index]):
                    if len(fields[index]) > 10:
                        return False, None
                    if 1000 <= int(fields[index]) <= 59999:
                        highest = max(highest, int(fields[index]))
    return True, (highest + 1 if highest < 59999 else None)


@pytest.mark.parametrize("case", RULE["cases"], ids=[case["case"] for case in RULE["cases"]])
def test_the_shared_next_uid_rule_table(host, case):
    passwd, group = rule_bytes(case["passwd"]), rule_bytes(case["group"])
    expected = (not case["logins_null"], case["next_uid"])
    assert next_uid_rule(passwd, group) == expected
    (host.files / "passwd").write_bytes(passwd)
    (host.files / "group").write_bytes(group)
    logins = host(passwd=None, group=None)
    assert (logins is not None, logins and logins["next_uid"]) == expected


@pytest.mark.parametrize("passwd,group,expected", [
    ([], ["staff:x:3000:"], 3001),
    (["svc:x:100:4000::/:/bin/false"], [], 4001),
    (["Bad.Name:x:2000:100::/:/bin/sh", "x" * 40 + ":x:2100:100::/:/bin/sh"], [], 2101),
    (["#old:x:2500:2500"], [], 2501),
    (["short:x:1700"], [], 1701),
    (["lead:x:0001500:100::/:/bin/sh"], ["pad:x:0001400:"], 1501),
    (["empty:x:::", "words:x:abc:def::/:/bin/sh"], [""], 1000),
], ids=["group-only", "passwd-gid", "bad-names", "comment-line", "short-line", "leading-zeros", "not-numbers"])
def test_every_numeric_id_counts_whatever_its_line_looks_like(host, passwd, group, expected):
    assert host(passwd=passwd, group=group)["next_uid"] == expected


@pytest.mark.parametrize("name", ["passwd", "group"])
@pytest.mark.parametrize("shape,readable", [("65536 bytes", True), ("65537 bytes", False),
                                            ("512 lines", True), ("513 lines", False)])
def test_account_files_over_64_kib_or_512_lines_read_as_unknown(host, name, shape, readable):
    count, unit = shape.split()
    count = int(count)
    if unit == "lines":
        # Blank lines count too: every other line here is blank.
        text = "".join(f"u{n}:x:2:2:\n" if n % 2 == 0 else "\n" for n in range(count))
        assert text.count("\n") == count
    else:
        head = "root:x:0:0:"
        text = head + "x" * (count - len(head) - 1) + "\n"
        assert len(text) == count
    host(passwd=[], group=[])
    (host.files / name).write_text(text)
    logins = host(passwd=None, group=None)
    assert (logins is not None) is readable


@pytest.mark.parametrize("name", ["passwd", "group"])
@pytest.mark.parametrize("damage", ["missing", "directory"])
def test_missing_account_files_read_as_unknown(host, name, damage):
    host(passwd=[], group=[])
    (host.files / name).unlink()
    if damage == "directory":
        (host.files / name).mkdir()
    assert host(passwd=None, group=None) is None


@pytest.mark.parametrize("name,line,expected", [
    ("passwd", "big:x:12345678901:100::/:/bin/sh", None),
    ("passwd", "big:x:1000:12345678901::/:/bin/sh", None),
    ("group", "big:x:12345678901:", None),
    ("passwd", "zeros:x:00000001500:1::/:/bin/sh", None),
    ("passwd", "huge:x:" + "9" * 40 + ":1::/:/bin/sh", None),
    ("passwd", "ten:x:9999999999:1::/:/bin/sh", 1000),
], ids=["uid", "passwd-gid", "group-gid", "padded", "forty-digits", "ten-digits-allowed"])
def test_ids_over_ten_digits_read_as_unknown_before_any_numeric_test(host, name, line, expected):
    # The fixture also asserts stderr stays empty: no test ever sees the value.
    logins = host(**{name: [line]})
    assert (logins if expected is None else logins["next_uid"]) == expected


def test_the_claudlobby_account_is_the_team_and_never_a_prepared_account(host):
    logins = host(passwd=[user("demo", 1000), user("claudlobby", 1001), user("claudlobby", 1500)],
                  receipts=[receipt("claudlobby", 1001), receipt("demo", 1000)])
    assert logins == {"team": {"uid": 1001, "policy": "unknown"}, "next_uid": 1501, "prepared": [DEMO],
                      "uncertain": False}
    # An id setup cannot use is still reported; the website refuses it.
    assert host(passwd=[f"claudlobby:x:0:0::{HOME}:/bin/bash"])["team"] == {"uid": 0, "policy": "unknown"}


@pytest.mark.parametrize("line", [f"claudlobby:x:abc:1001::{HOME}:/bin/bash", "claudlobby:x",
                                  "claudlobby", "claudlobby:x::1001::/:/bin/sh"])
def test_a_claudlobby_line_without_a_numeric_uid_reads_as_unknown(host, line):
    assert host(passwd=[line]) is None


@pytest.mark.parametrize("kind,state,kept", [
    ("system", "completed", True), ("system", "planned", False), ("system", "dispatched", False),
    ("system", "running", False), ("system", "failed_partial", False), ("system", "unknown", False),
    ("ingress", "completed", False), ("index-refresh", "completed", False)])
def test_only_completed_system_receipts_count(host, kind, state, kept):
    logins = host(passwd=[user("demo", 1000)], receipts=[receipt(kind=kind, state=state)])
    assert logins["prepared"] == ([DEMO] if kept else [])


@pytest.mark.parametrize("passwd,account,uid", [
    ([], "demo", 1000), ([user("demo", 1005)], "demo", 1000),
    ([user("demo", 1005), user("demo", 1000)], "demo", 1000),
    ([user("low", 999)], "low", 999), ([user("high", 60000)], "high", 60000),
], ids=["deleted", "uid-changed", "first-line-wins", "uid-below-1000", "uid-above-59999"])
def test_receipts_for_removed_changed_or_ineligible_accounts_are_ignored(host, passwd, account, uid):
    assert host(passwd=passwd, receipts=[receipt(account, uid)])["prepared"] == []


def test_text_inside_a_receipt_cannot_forge_a_completed_one(host):
    forged = ('{"account":"evil","gid":1001,"home":"' + HOME + '","node_key":"' + NODE_KEY
              + '","profile_id":"p001","uid":1001},"state":"completed"}')
    planned = receipt(state="planned", hooks=[',"kind":"system","schema_version":1,"selection":' + forged])
    assert b'\\"state\\":\\"completed\\"' in planned
    accounts = [user("demo", 1000), user("evil", 1001)]
    assert host(passwd=accounts, receipts=[planned])["prepared"] == []
    hidden = receipt(hooks=['{"selection":' + forged, "{" * 64, "}" * 64])
    assert host(passwd=accounts, receipts=[hidden])["prepared"] == [DEMO]
    # Only the top-level selection, followed by the top-level state, counts.
    evil = selection("evil", 1001)
    nested = receipt(state="failed_partial", extra={"proposal_note": {"inner": {}, "state": "completed"}})
    assert b'},"state":"completed"' in nested
    shadowed = receipt(extra={"shadow": evil})
    assert host(passwd=accounts, receipts=[nested, shadowed])["prepared"] == []


def test_a_selection_longer_than_any_the_leaf_writes_is_not_read(host):
    # The leaf caps home under 180 characters; a longer selection is skipped
    # unparsed, so no prefix scan ever runs over a long string.
    receipts = [receipt(home=HOME + "/" + "d" * 1000), receipt(home="/" + "d" * 40000)]
    assert host(passwd=[user("demo", 1000)], receipts=receipts)["prepared"] == []


def test_prepared_stops_at_the_second_distinct_account_and_uncertain_reads_on(host):
    accounts = [user("demo", 1000), user("alice", 1001), user("bob", 1002)]
    oversize = sized(98305, account="bob", uid=1002)
    logins = host(passwd=accounts, receipts=[receipt(), receipt(), receipt("alice", 1001), oversize])
    # The oversize receipt no longer unsets prepared, but it is doubt.
    assert logins["prepared"] == [DEMO, {"name": "alice", "uid": 1001}] and logins["uncertain"] is None
    logins = host(passwd=accounts, receipts=[receipt(), oversize, receipt("alice", 1001)])
    assert logins["prepared"] is None and logins["uncertain"] is None
    running = receipt("bob", 1002, state="running")
    logins = host(passwd=accounts, receipts=[receipt(), receipt("alice", 1001), running, oversize])
    assert logins["prepared"] == [DEMO, {"name": "alice", "uid": 1001}] and logins["uncertain"] is True
    # A receipt that cannot be read before two accounts are found still unsets
    # prepared; uncertain reads on.
    for receipts in ([running, oversize, receipt()], [oversize, receipt(), running]):
        logins = host(passwd=accounts, receipts=receipts)
        assert logins["prepared"] is None and logins["uncertain"] is True


def test_two_accounts_sharing_a_uid_are_reported_once(host):
    # Two passwd names can share a uid; the website refuses prepared entries
    # that do, so the second is skipped and scanning goes on.
    accounts = [user("demo", 1000), user("twin", 1000), user("alice", 1001)]
    receipts = [receipt(), receipt("twin", 1000), receipt("alice", 1001)]
    assert host(passwd=accounts, receipts=receipts)["prepared"] == [DEMO, {"name": "alice", "uid": 1001}]


@pytest.mark.parametrize("count,expected", [(128, [DEMO]), (129, None)])
def test_more_than_128_receipt_names_read_as_unknown(host, count, expected):
    receipts = [receipt(state="planned")] * (count - 1) + [receipt()]
    logins = host(passwd=[user("demo", 1000)], receipts=receipts)
    assert logins["prepared"] == expected and logins["uncertain"] is (False if expected else None)


@pytest.mark.parametrize("size,expected", [(98304, [DEMO]), (98305, None)])
def test_a_receipt_over_96_kib_reads_as_unknown(host, size, expected):
    logins = host(passwd=[user("demo", 1000)], receipts=[sized(size)])
    assert logins["prepared"] == expected and logins["uncertain"] is (False if expected else None)


def test_symlinks_and_non_files_in_the_store_are_skipped(host, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(receipt())
    host.store.mkdir(mode=0o700)
    (host.store / "11111111-1111-4111-8111-111111111111.json").symlink_to(outside)
    (host.store / "22222222-2222-4222-8222-222222222222.json").symlink_to(tmp_path / "dangling")
    (host.store / "33333333-3333-4333-8333-333333333333.json").mkdir()
    # Skipped for prepared; the leaf would refuse to read each, so doubt.
    assert host(passwd=[user("demo", 1000)]) == {"team": None, "next_uid": 1001, "prepared": [], "uncertain": None}


@pytest.mark.parametrize("damage", ["store-link", "parent-link", "store-file", "parent-file"])
def test_a_redirected_store_reads_as_unknown(host, tmp_path, damage):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "v1").mkdir(parents=True)
    (elsewhere / "v1" / "11111111-1111-4111-8111-111111111111.json").write_bytes(receipt())
    if damage == "store-link":
        host.store.symlink_to(elsewhere / "v1")
    elif damage == "parent-link":
        host.store.parent.rmdir()
        host.store.parent.symlink_to(elsewhere)
    elif damage == "store-file":
        host.store.write_bytes(receipt())
    else:
        host.store.parent.rmdir()
        host.store.parent.write_bytes(receipt())
    logins = host(passwd=[user("demo", 1000)])
    assert logins["prepared"] is None and logins["uncertain"] is None


def test_nothing_read_is_executed(host):
    # The fixture runs the probe in an empty directory and asserts it stays empty.
    evil = "$(printf x >ran1)`printf y >ran2`;printf z >ran3;*"
    passwd = [f"demo:x:1000:1000:{evil}:{HOME}:/bin/bash", f"{evil}:x:1001:1001::/:/bin/sh"]
    group = [f"{evil}:x:1002:{evil}"]
    host.store.mkdir(mode=0o700)
    (host.store / (evil + ".json")).write_bytes(receipt(hooks=[evil]))
    # The evil name is no receipt name, so whether an operation is pending is doubt.
    assert host(passwd=passwd, group=group) == {"team": None, "next_uid": 1003, "prepared": [DEMO],
                                                "uncertain": None}


def test_the_environment_cannot_move_the_files_it_reads(inventory, tmp_path):
    decoy = tmp_path / "decoy"
    (decoy / "v1").mkdir(parents=True)
    (decoy / "passwd").write_text(user("claudlobby", 54320) + "\n" + user("decoy", 54321) + "\n")
    (decoy / "group").write_text("decoy:x:54321:\n")
    (decoy / "v1" / "11111111-1111-4111-8111-111111111111.json").write_bytes(receipt("decoy", 54321))
    # Only the tools are rewritten: this copy reads this computer's own files.
    result, data = inventory(uid=b"0\n", user=b"root\n", root_paths=False,
        env={"passwd_file": decoy / "passwd", "group_file": decoy / "group", "store": decoy / "v1"}.items())
    assert result.returncode == 0
    logins = data["logins"]
    real = [Path("/etc/passwd"), Path("/etc/group")]
    if all(path.is_file() and path.stat().st_size <= 65536 and path.read_bytes().count(b"\n") <= 512
           for path in real):
        assert logins is not None
    if logins is not None:
        assert logins["team"] is None and logins["next_uid"] != 54322
        assert {"name": "decoy", "uid": 54321} not in (logins["prepared"] or [])


def test_the_worst_case_stays_small_and_fast(host, tmp_path):
    # 512-line, 64 KiB account files; 128 receipts of 96 KiB, each shaped like
    # the leaf's with its full 16,000 characters of brace-heavy hook text. Every
    # receipt is read: the two that count come last. The team check answers
    # its longest word at once; its own worst case is timed further down.
    longest = {"a" * 31 + "b": 59998, "a" * 31 + "c": 59997}
    passwd = [f"claudlobby:x:9999999999:1::{HOME}:/bin/bash",
              *(user(name, uid) for name, uid in longest.items())]
    passwd += [f"u{n}:x:{1000 + n}:{1000 + n}:" + "g" * 100 for n in range(512 - len(BASE_PASSWD) - len(passwd))]
    group = [f"g{n}:x:{1000 + n}:" + "m" * 100 for n in range(512 - len(BASE_GROUP))]
    hooks = ["{}" * 8000]
    host(passwd=passwd, group=group, receipts=[])
    for n in range(128):
        name, uid = list(longest.items())[n - 126] if n >= 126 else (f"gone{n}", 2000 + n)
        base = len(receipt(name, uid, hooks=[*hooks, ""]))
        data = receipt(name, uid, hooks=[*hooks, "x" * (98304 - base)])
        assert len(data) == 98304
        (host.store / f"{n:08d}-0000-4000-8000-000000000000.json").write_bytes(data)
    longest_word = max(TEAM_CODES, key=len)
    python = stand_in(tmp_path, "printf '%s\\n'", longest_word)
    started = time.monotonic()
    logins = host(passwd=None, group=None, architecture=b"a" * 32 + b"\n", user=b"r" * 64 + b"\n",
                  python=python)
    elapsed = time.monotonic() - started
    # The longest answer: well inside the 8 KiB promised (16 KiB the cap).
    output = host.inventory.last.stdout
    assert len(output) < 2048, len(output)
    for name in ("passwd", "group"):
        text = (host.files / name).read_bytes()
        assert len(text) <= 65536 and text.count(b"\n") == 512
    assert logins == {"team": {"uid": 9999999999, "policy": longest_word}, "next_uid": 59999,
                      "prepared": [{"name": name, "uid": uid} for name, uid in longest.items()],
                      "uncertain": False}
    # Root mode runs under dash on Ubuntu: 3 seconds. bash 3.2, /bin/sh on a
    # Mac (where root mode never runs), splits braces more slowly, and a shared
    # macOS runner took 6.2 seconds: 15 there, which still catches a run whose
    # time grows with the square of the input (minutes at this size).
    bash = subprocess.run(["/bin/sh", "-c", 'printf %s "${BASH_VERSION-}"'],
                          capture_output=True, timeout=5).stdout
    budget = 15 if bash else 3
    assert elapsed < budget, f"worst case took {elapsed:.2f}s under {'bash' if bash else 'dash'}"


def leaf_module():
    module = types.ModuleType("linux_system_leaf")
    body = leaf_body()
    exec(compile(body, str(LEAF), "exec"), module.__dict__)
    return module


def test_a_receipt_the_leaf_writes_is_one_the_probe_reads(host, tmp_path, monkeypatch):
    """The leaf's own inspect, apply and worker write the receipt; only native
    root facts and effects are fixtures, as in test_linux_system_preinstall."""
    leaf = leaf_module()
    store = tmp_path / "leaf-store"
    store.mkdir(mode=0o700)
    monkeypatch.setattr(leaf, "STORE", store)
    monkeypatch.setattr(leaf, "OWNER", os.geteuid())
    monkeypatch.setattr(leaf, "trusted_parents", lambda path: None)
    source = tmp_path / "incoming.sh"
    source.write_bytes(LEAF.read_bytes())
    source.chmod(0o500)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    request_id = "44444444-4444-4444-8444-444444444444"
    chosen = {"account": "demo", "uid": 1000, "gid": 1000, "home": HOME,
              "node_key": NODE_KEY, "profile_id": "p001"}
    bound = {"selection": chosen, "dns": "box.example.ts.net", "version": leaf.NATIVE_VERSION,
             "profile_digest": "e" * 64, "machine": "9" * 64,
             "daemon": {"pid": 812, "start": "4242", "binary_sha256": "f" * 64,
                        "binary": [64768, 131, 1700000000000000000, 30000000], "socket": [25, 1020, 1]}}
    hooks = ['DPkg::Post-Invoke {"if [ -x /usr/bin/x ]; then /usr/bin/x; fi";};',
             'APT::Update::Post-Invoke:: "echo },"state":"completed"}";']
    packages = {"seeds": [], "actions": [], "architecture": "amd64", "config_digest": "d" * 64, "hooks": hooks}
    units = {name: {"DropInPaths": "", "FragmentPath": "/usr/lib/systemd/system/" + name.split("@")[0] + "@.service",
                    "LoadState": "loaded"} for name in ("user@1000.service", "user-runtime-dir@1000.service")}
    manager = {"linger": False, "units": units, "ordinary_user_bus": "unproved"}
    plan = {"kind": "system", "binding": bound, "packages": packages, "manager": manager,
            "disclosure": leaf.DISCLOSURE}
    invocation = "1" * 32
    monkeypatch.setattr(leaf, "boot_id", lambda: "22222222-2222-4222-8222-222222222222")
    monkeypatch.setattr(leaf, "derive_selection", lambda selected, api: dict(chosen))
    monkeypatch.setattr(leaf, "binding", lambda selected, api: copy.deepcopy(bound))
    monkeypatch.setattr(leaf, "inspect_plan", lambda kind, selected, api: copy.deepcopy(plan))
    monkeypatch.setattr(leaf, "package_health", lambda: {})
    monkeypatch.setattr(leaf, "package_plan", lambda: copy.deepcopy(packages))
    monkeypatch.setattr(leaf, "manager_plan", lambda selected: copy.deepcopy(manager))
    monkeypatch.setattr(leaf, "manager_runtime", lambda selected: {
        name: {"ActiveState": "active", "SubState": "running"} for name in units})
    monkeypatch.setattr(leaf, "unit_state", lambda name: {"InvocationID": invocation, "MainPID": str(os.getpid())})
    monkeypatch.setattr(leaf, "private_record", lambda rid, path: leaf.load(rid))
    monkeypatch.setenv("INVOCATION_ID", invocation)
    effects = []

    def native(argv, effect=False):
        effects.append(argv)
        if argv[:2] == ["/usr/bin/loginctl", "enable-linger"]:
            manager["linger"] = True
            return 0
        assert argv[0] == "/usr/bin/systemd-run" and not effect
        return ""

    monkeypatch.setattr(leaf, "native", native)
    minimal = {key: chosen[key] for key in ("account", "uid", "node_key")}
    request = {"schema_version": 1, "request_id": request_id, "artifact_sha256": sha,
               "selection": minimal, "kind": "system"}
    record = leaf.execute("inspect", request, source, None)
    planned = (store / (request_id + ".json")).read_bytes()
    approval = {**{k: v for k, v in request.items() if k != "kind"}, "selection": record["selection"],
                "digest": record["digest"], "decision": "approve"}
    assert leaf.execute("apply", approval, source, None)["submission"] == "exec_acknowledged"
    dispatched = (store / (request_id + ".json")).read_bytes()
    leaf.private_worker(request_id, leaf.artifact_path(sha), None)
    completed = (store / (request_id + ".json")).read_bytes()
    assert json.loads(completed)["state"] == "completed" and len(effects) == 2
    assert b"Post-Invoke {" in completed and b'\\"state\\":\\"completed\\"' in completed
    accounts = [user("demo", 1000)]
    assert host(passwd=accounts, receipts=[planned]) == {"team": None, "next_uid": 1001, "prepared": [],
                                                         "uncertain": False}
    assert host(passwd=accounts, receipts=[completed]) == {"team": None, "next_uid": 1001, "prepared": [DEMO],
                                                           "uncertain": False}
    # Dispatched and not yet finished: an operation with no recorded end.
    assert json.loads(dispatched)["state"] == "dispatched"
    assert host(passwd=accounts, receipts=[dispatched])["uncertain"] is True


# The team account check: as root on Linux, after a claudlobby line, the probe
# runs Python on a copy of the leaf's own team_policy and reports one word.

from tests.test_linux_system_preinstall import (  # noqa: E402
    POLICY_REFUSALS, TEAM_HOME, UBUNTU_NSSWITCH, UBUNTU_SUDOERS, Computer, _set, _team, _then, directory_node, node,
    payload)

# What the copy holds, each byte for byte the leaf's, beside its own main.
COPIED = ("OWNER", "LIMIT", "TEAM", "TEAM_HOME", "PATHS", "Refusal", "require", "trusted_parents", "lookup",
          "lstat", "read_root", "directory_service", "subordinate", "team_lines", "sudo_rules", "team_policy")
IMPORTS = ["import os", "import re", "import signal", "import stat", "import pwd", "import grp",
           "from pathlib import Path"]


def leaf_body():
    return LEAF_SOURCE.split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]


def team_check_block():
    return PROBE_SOURCE.split("# BEGIN TEAM POLICY COPY\n", 1)[1].split("# END TEAM POLICY COPY\n", 1)[0]


def top_level(source):
    """Each top-level definition or single-name assignment, by its source text."""
    named = {}
    for item in ast.parse(source).body:
        if isinstance(item, (ast.FunctionDef, ast.ClassDef)):
            named[item.name] = ast.get_source_segment(source, item)
        elif isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            named[item.targets[0].id] = ast.get_source_segment(source, item)
    return named


# The copy's own lines, beside the leaf's definitions: its imports, its main
# (by source text) and the guard that runs it, last.
MAIN = '''def main():
    # How the probe runs this copy: as root, with an alarm whose default
    # action ends the process after 4 seconds, whatever it waits on, and one
    # word on stdout. Any other exception exits nonzero, which reads unknown.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.alarm(4)
    if os.geteuid() != 0:
        raise SystemExit(3)
    try:
        team_policy()
        code = "conforms"
    except Refusal as exc:
        code = exc.code
    print(code)'''
GUARD = "if __name__ == '__main__':\n    main()"
DICT_CHANGES = {"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"}


def bound(item):
    """Every name a top-level statement binds, in any form."""
    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [item.name]
    if isinstance(item, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in item.names]
    return [node.id for node in ast.walk(item) if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))]


def root_name(node):
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def definition(item):
    """The name a definition binds: a def, a class, or an assignment of
    exactly one plain name. None for any other statement."""
    if isinstance(item, (ast.FunctionDef, ast.ClassDef)):
        return item.name
    if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
        return item.targets[0].id
    return None


def copy_problems(block, leaf):
    """Why block is not the leaf's team check plus the probe's own lines;
    empty when it is. Each top-level statement is one of IMPORTS, a
    definition, or GUARD as the last; no name is bound twice; the definitions
    are COPIED, each the leaf's text, and MAIN; and the leaf binds each copied
    name once and never changes one after."""
    problems = []
    body = ast.parse(block).body
    names, defined, imports = [], {}, []
    for index, item in enumerate(body):
        names += bound(item)
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            imports.append(ast.unparse(item))
        elif definition(item):
            defined[definition(item)] = ast.get_source_segment(block, item)
        elif not (index == len(body) - 1 and ast.unparse(item) == GUARD):
            problems.append(f"line {item.lineno}, not a statement the copy holds: {ast.unparse(item).splitlines()[0]}")
    if imports != IMPORTS:
        problems.append(f"imports {imports}")
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        problems.append(f"bound more than once: {twice}")
    if set(defined) != set(COPIED) | {"main"}:
        problems.append(f"defines {sorted(defined)}")
    if defined.get("main") != MAIN:
        problems.append("main is not the probe's own")
    if not body or ast.unparse(body[-1]) != GUARD:
        problems.append("the guard is not the last statement")
    tree = ast.parse(leaf)
    original = {definition(item): ast.get_source_segment(leaf, item) for item in tree.body if definition(item)}
    leaf_names = [name for item in tree.body for name in bound(item)]
    for name in COPIED:
        if defined.get(name) != original.get(name):
            problems.append(f"{name} is not the leaf's, byte for byte")
        if leaf_names.count(name) != 1:
            problems.append(f"the leaf binds {name} {leaf_names.count(name)} times")
    for node in ast.walk(tree):
        changed = set()
        if isinstance(node, ast.Global):
            changed = set(node.names)
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            changed = {root_name(node)}
        elif isinstance(node, ast.AugAssign):
            changed = {root_name(node.target)}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in DICT_CHANGES:
            changed = {root_name(node.func.value)}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr"):
            changed = {root_name(node.args[0])} if node.args else set()
        if changed & set(COPIED):
            problems.append(f"the leaf changes {sorted(changed & set(COPIED))} at line {node.lineno}")
    return problems


def refusal_codes(source):
    """Every code the source can refuse with: require(..., "code"),
    Refusal("code") and sudo_rules("code")."""
    codes = set()
    for item in ast.walk(ast.parse(source)):
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
            position = {"require": 1, "Refusal": 0, "sudo_rules": 0}.get(item.func.id)
            if position is not None and len(item.args) > position and isinstance(item.args[position], ast.Constant):
                codes.add(item.args[position].value)
    return codes


def stand_in(tmp_path, body, *words):
    """An interpreter in Python's place: a shell script that ignores its
    arguments and runs body, then each word quoted."""
    path = tmp_path / "interpreter" / "python3"
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "".join(" '" + word + "'" for word in words) + "\n")
    path.chmod(0o755)
    return path


# Python's place for the copy itself: the real interpreter, isolated as the
# probe runs it, on the very block the probe passes, with the account lookup
# and file metadata of a fixture Computer from the leaf's own tests. The copy's
# definitions run unchanged; only the fixture's paths, owner and lstat replace
# its globals, as the leaf tests replace the leaf's, and then main runs.
FAKE_SYSTEM = '''
import json
import os
import sys
import time
import types

spec = json.loads(open(sys.argv[1], "rb").read())
code = sys.argv[2]
users, groups, nodes = spec["users"], spec["groups"], spec["nodes"]


def fault():
    if spec["fault"] == "hang":
        time.sleep(60)
    if spec["fault"] == "raise":
        raise RuntimeError("fixture")


def user(name):
    fault()
    return types.SimpleNamespace(**{"pw_name": name, "pw_shell": "/bin/bash", "pw_passwd": "x", **users[name]})


def group(name):
    entry = groups[name]
    return types.SimpleNamespace(gr_name=name, gr_gid=entry["gid"], gr_mem=list(entry["mem"]),
                                 gr_passwd=entry.get("passwd", "x"))


def by_uid(uid):
    for name, entry in users.items():
        if entry["pw_uid"] == uid:
            return user(name)
    if uid in spec["nss_uids"]:
        return types.SimpleNamespace(pw_name="dynamic", pw_uid=uid, pw_gid=uid, pw_dir="/", pw_shell="/bin/false")
    raise KeyError(uid)


def by_gid(gid):
    for name, entry in groups.items():
        if entry["gid"] == gid:
            return group(name)
    if gid in spec["nss_gids"]:
        return types.SimpleNamespace(gr_name="dynamic", gr_gid=gid, gr_mem=[], gr_passwd="x")
    raise KeyError(gid)


pwd = types.ModuleType("pwd")
pwd.getpwnam, pwd.getpwuid, pwd.getpwall = user, by_uid, lambda: [user(name) for name in users]
grp = types.ModuleType("grp")
grp.getgrnam, grp.getgrgid, grp.getgrall = group, by_gid, lambda: [group(name) for name in groups]
sys.modules.update(pwd=pwd, grp=grp)
os.getgrouplist = lambda name, gid: [gid] + [entry["gid"] for entry in groups.values() if name in entry["mem"]]
if spec["root"]:
    os.geteuid = lambda: 0
if spec["alarm"]:
    code = code.replace("signal.alarm(4)", "signal.alarm(%d)" % spec["alarm"])
namespace = {"__name__": "team_check_fixture"}
exec(compile(code, "<team check>", "exec"), namespace)
namespace.update(OWNER=spec["owner"], PATHS=spec["paths"], trusted_parents=lambda path: None,
                 lstat=lambda path: types.SimpleNamespace(**nodes[str(path)]) if str(path) in nodes else None)
namespace["main"]()
'''


def fake_python(tmp_path, computer, fault=None, root=True, alarm=None):
    """Writes the interpreter for one run of the copy against computer."""
    folder = tmp_path / "fake-python"
    folder.mkdir(exist_ok=True)
    spec = {"users": computer.users, "groups": computer.groups, "nss_uids": sorted(computer.nss_uids),
            "nss_gids": sorted(computer.nss_gids), "owner": computer.module.OWNER, "paths": computer.module.PATHS,
            "nodes": {path: vars(entry) for path, entry in computer.nodes.items()},
            "fault": fault, "root": root, "alarm": alarm}
    (folder / "spec.json").write_text(json.dumps(spec))
    (folder / "system.py").write_text(FAKE_SYSTEM)
    interpreter = folder / "python3"
    interpreter.write_text("#!/bin/sh\n"
                           '[ "$#" -eq 5 ] && [ "$1 $2 $3 $4" = "-I -S -B -c" ] || exit 97\n'
                           f"exec '{sys.executable}' -I -S -B '{folder / 'system.py'}' '{folder / 'spec.json'}' \"$5\"\n")
    interpreter.chmod(0o755)
    return interpreter


def raw_leaf_verdict(module):
    try:
        module.team_policy()
    except module.Refusal as error:
        return error.code
    except Exception as error:  # KeyError from NSS, OSError from a file
        return type(error).__name__
    return "conforms"


def as_reported(code):
    """The word the probe reports for a leaf answer."""
    return code if code in ("conforms",) + TEAM_CODES else "unknown"


def judge_both(computer, inventory, tmp_path, after=None, **spec):
    """The leaf's own team_policy, then the probe end to end, on one computer."""
    computer.write()
    if after:
        after(computer)
    leaf = raw_leaf_verdict(computer.module)
    python = fake_python(tmp_path, computer, **spec)
    computer.monkeypatch.undo()  # the leaf fixture patches pathlib and os
    started = time.monotonic()
    _result, data = inventory(uid=b"0\n", user=b"root\n", python=python)
    return leaf, data["logins"], time.monotonic() - started


def sudo_never_read(c):
    # sudo skips these names in sudoers.d, and reads none of these as a rule.
    c.sudoers_d.update({"team~": "claudlobby ALL=(ALL) NOPASSWD:ALL\n", "team.disabled": "claudlobby ALL=(ALL) ALL\n",
                        "90-team.dpkg-old": "claudlobby ALL=(ALL) ALL\n", "another": "claudlobby_old ALL=(ALL) ALL\n",
                        "includes": "#includedir /etc/sudoers.d\n@includedir   /etc/sudoers.d  \n#includes no other\n"})


def writable(name):
    return lambda c: (c.files / "sudoers.d" / name).chmod(0o666)


def linked(name):
    return lambda c: (c.files / "sudoers.d" / name).symlink_to(c.files / "sudoers")


def lines_up_to(limit, line):
    """Whole lines from line(n), as many as fit in limit bytes."""
    text, n = "", 0
    while len(text) + len(line(n)) <= limit:
        text, n = text + line(n), n + 1
    return text


# The shared table: every case the leaf tests refuse a present team account
# with, and these, each (what the probe reports, change, after writing).
TEAM_CASES = {
    **{name: (code, change, None) for name, (code, _uid, change) in POLICY_REFUSALS.items()},
    "conforms": ("conforms", _team(), None),
    "conforms-star-no-ageing": ("conforms", _then(_team(), _set(
        "shadow", claudlobby="claudlobby:*:20000:0:-1:7:-1:-1:")), None),
    "conforms-no-last-change": ("conforms", _then(_team(), _set("shadow", claudlobby="claudlobby:!::0::7:::")), None),
    "conforms-padded-last-change": ("conforms", _then(_team(), _set(
        "shadow", claudlobby="claudlobby:!:010:0::7:::")), None),
    "conforms-empty-mail-spool": ("conforms", _then(_team(), lambda c: c.nodes.update(
        {"/var/mail/claudlobby": node(0o100000, 0o660, 1500)})), None),
    "conforms-no-gshadow-line": ("conforms", _then(_team(), lambda c: c.gshadow.pop("claudlobby")), None),
    "conforms-gshadow-star": ("conforms", _then(_team(), _set("gshadow", claudlobby="claudlobby:*::")), None),
    "conforms-sudo-never-read": ("conforms", _then(_team(), sudo_never_read), None),
    "conforms-ubuntu-nsswitch": ("conforms", _then(_team(), _set("extra", nsswitch=UBUNTU_NSSWITCH)), None),
    "home-0755": ("team_account_home", _then(_team(), lambda c: c.nodes.update(
        {TEAM_HOME: directory_node(0o755, 1500)})), None),
    "home-a-file": ("team_account_home", _then(_team(), lambda c: c.nodes.update(
        {TEAM_HOME: node(0o100000, 0o700, 1500)})), None),
    "subuid-range": ("team_account_ids", _then(_team(), _set("extra", subuid="fixture:1000:1000\n")), None),
    "subuid-its-id": ("team_account_ids", _then(_team(), _set("extra", subuid="1500:165536:65536\n")), None),
    "subgid-its-name": ("team_account_ids", _then(_team(), _set("extra", subgid="claudlobby:165536:65536\n")), None),
    "shadow-over-limit": ("team_account_password", _then(_team(), _set("shadow", big="big:!:" + "x" * 262144)), None),
    "gshadow-over-limit": ("team_account_group", _then(_team(), _set("gshadow", big="big:!::" + "x" * 262144)), None),
    "nsswitch-over-64-kib": ("team_directory_service", _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\n" + "#" * 65536 + "\n")), None),
    "sudoers-over-64-kib": ("team_account_sudo", _then(_team(), _set(
        "extra", sudoers=UBUNTU_SUDOERS + "#" * 65536 + "\n")), None),
    "sudo-hex-escaped-capital": ("team_account_sudo", _then(_team(), _set(
        "sudoers_d", team="\\x43laudlobby ALL=(ALL) ALL\n")), None),
    "sudo-continued-then-escaped": ("team_account_sudo", _then(_team(), _set(
        "sudoers_d", team="claud\\\n\\x6cobby ALL=(ALL) ALL\n")), None),
    "sudo-include-one-file": ("team_account_sudo", _then(_team(), _set(
        "sudoers_d", team="@include /etc/sudoers.d/team\n")), None),
    "sudoers-a-last-backslash": ("team_account_sudo", _then(_team(), _set("sudoers_d", team="# done \\\n")), None),
    # What the leaf refuses with a code the website does not take, or raises:
    # the probe reports unknown.
    "sudoers-d-writable": ("unknown", _team(), writable("README")),
    "sudoers-d-link": ("unknown", _team(), linked("teamlink")),
    "nss-misses-the-line": ("unknown", lambda c: setattr(
        c, "passwd_tail", f"claudlobby:x:1500:1500::{TEAM_HOME}:/bin/bash\n"), None),
}
RAW = {"sudoers-d-writable": "unsafe_file", "sudoers-d-link": "OSError", "nss-misses-the-line": "KeyError"}


def test_the_team_check_is_the_leafs_own_byte_for_byte():
    block = team_check_block()
    # Nothing else runs: its imports, the leaf's definitions, main and its
    # guard, each name bound once, and the leaf never changes one later.
    assert copy_problems(block, leaf_body()) == []
    # Nothing is missing either: every global a definition reads is in the
    # copy, imported by it, or a builtin.
    copied = top_level(block)
    known = set(copied) | {"os", "re", "signal", "stat", "pwd", "grp", "Path"} | set(dir(builtins))

    def read_globals(table):
        names = set(table.get_globals()) if isinstance(table, symtable.Function) else set()
        for child in table.get_children():
            names |= read_globals(child)
        return names
    assert read_globals(symtable.symtable(block, "team check", "exec")) <= known
    # The shell hands Python exactly this block, between its marker lines.
    assert PROBE_SOURCE.count("team_check='") == 1
    value = PROBE_SOURCE.split("\nteam_check='", 1)[1].split("'", 1)[0]
    assert value == "\n# BEGIN TEAM POLICY COPY\n" + block + "# END TEAM POLICY COPY\n"
    assert block.count("signal.alarm(4)") == 1
    # The leaf is unchanged by this copy and still under its limit.
    assert len(LEAF_SOURCE.encode()) <= 61440


# Edits that would change what the copy checks while every definition still
# matched the leaf: each must fail the parity check above. Block lines go in
# before main; leaf lines go at the end of the leaf's Python.
BLOCK_DRIFTS = {
    "a path moved": 'PATHS["sudoers.d"] = "/nonexistent"\n',
    "a chained rebinding": "trusted_parents = _unused = lambda path: None\n",
    "an augmented assignment": "LIMIT += 1\n",
    "an annotated assignment": "OWNER: int = 1000\n",
    "a tuple rebinding": '(TEAM, _spare) = ("someone", 0)\n',
    "a call at top level": 'PATHS.update(shadow="/dev/null")\n',
    "an attribute set": 'Refusal.code = "conforms"\n',
    "a definition repeated": "def lstat(path):\n    return None\n",
    "an import rebinding": "import os as stat\n",
    "a loop": "for _name in ():\n    pass\n",
}
LEAF_DRIFTS = {
    "the leaf moves a path later": 'PATHS["sudoers.d"] = "/nonexistent"\n',
    "the leaf adds a path later": 'PATHS.update(shadow="/dev/null")\n',
    "the leaf rebinds a copied name in a function": "def later():\n    global LIMIT\n    LIMIT = 0\n",
    "the leaf defines one twice": "def lstat(path):\n    return None\n",
}


@pytest.mark.parametrize("name", sorted(BLOCK_DRIFTS))
def test_the_parity_check_refuses_a_copy_that_drifts(name):
    block, leaf = team_check_block(), leaf_body()
    assert block.count("\ndef main():\n") == 1
    drifted = block.replace("\ndef main():\n", "\n" + BLOCK_DRIFTS[name] + "\n\ndef main():\n")
    ast.parse(drifted)
    assert copy_problems(drifted, leaf) != []


@pytest.mark.parametrize("name", sorted(LEAF_DRIFTS))
def test_the_parity_check_refuses_a_leaf_that_changes_a_copied_name(name):
    leaf = leaf_body() + "\n\n" + LEAF_DRIFTS[name]
    ast.parse(leaf)
    assert copy_problems(team_check_block(), leaf) != []


@pytest.mark.parametrize("old,new", [
    ("    signal.alarm(4)\n", "    signal.alarm(400)\n"),
    ("        team_policy()\n        code", "        code"),
    ("\nif __name__ == \"__main__\":\n    main()\n", "\nif __name__ == \"__main__\":\n    main()\nTEAM = \"x\"\n"),
    ("\nif __name__ == \"__main__\":\n    main()\n", "\nif __name__ == \"__main__\":\n    main()\n    main()\n"),
], ids=["main-alarm", "main-skips-the-check", "after-the-guard", "guard-runs-twice"])
def test_the_parity_check_refuses_a_changed_main_or_guard(old, new):
    block = team_check_block()
    assert block.count(old) == 1
    assert copy_problems(block.replace(old, new), leaf_body()) != []


# Where the leaf meets a present team account before using it. On a visit
# that skips the leaf's root step, the probe's copy of team_policy is the
# only check of that account, so a check added to either place must be added
# to team_policy, and so to the copy and its case table, in the same change.
PRESENT_TEAM = ("the leaf now checks a present team account outside team_policy: move the check into "
                "team_policy, so the probe's copy ($team_check in host-readiness-probe.sh) and the case table "
                "here carry it, in the same change")


def test_a_present_team_account_meets_only_team_policy_in_the_leaf():
    """What the leaf checks on a present team account today, and nothing else:
    - team_block, when the account is present: team_policy(). Its other
      branch, for an account it will create, is free.
    - derive_selection, for any account not being created: team_policy()
      when it is the team account, then the account_changed comparison every
      account meets, of the passwd entry with the selection (its name and
      uid, a uid above 0, a login shell). The website makes the selection
      from logins.team.uid, and team_policy already requires /bin/bash.
      binding() repeats that comparison for every account."""
    leaf = leaf_body()
    functions = {item.name: item for item in ast.parse(leaf).body if isinstance(item, ast.FunctionDef)}

    def text(items):
        return [ast.get_source_segment(leaf, item) for item in items]
    team = functions["team_block"].body
    assert [type(item).__name__ for item in team] == ["Assign", "If", "Return"], PRESENT_TEAM
    assert text([team[0], team[1].test]) == ["present = lookup(pwd.getpwnam, TEAM) is not None", "present"], \
        PRESENT_TEAM
    assert text(team[1].body) == ["team_policy()"], PRESENT_TEAM
    assert text(team[2:]) == ['return {"account": TEAM, "uid": selection["uid"], "gid": selection["gid"], '
                              '"home": TEAM_HOME,\n            "shell": "/bin/bash", '
                              '"state": "present" if present else "create"}'], PRESENT_TEAM
    derive = functions["derive_selection"].body
    planned = [item for item in derive if isinstance(item, ast.Assign) and definition(item) == "planned"]
    branch = [item for item in derive if isinstance(item, ast.If) and text([item.test]) == ["planned"]]
    assert len(planned) == len(branch) == 1, PRESENT_TEAM
    assert text(planned) == ['planned = selected["account"] == TEAM and lookup(pwd.getpwnam, TEAM) is None'], \
        PRESENT_TEAM
    assert text(branch[0].orelse) == [
        'if selected["account"] == TEAM:\n            team_policy()',
        'account = pwd.getpwnam(selected["account"])',
        'require(account.pw_name == selected["account"] and account.pw_uid == selected["uid"]\n'
        '                and account.pw_uid > 0 and account.pw_shell in ("/bin/bash", "/bin/sh", "/usr/bin/bash", '
        '"/usr/bin/zsh", "/bin/zsh"), "account_changed")',
        "gid, home = account.pw_gid, account.pw_dir"], PRESENT_TEAM
    # derive_selection names the team account nowhere else.
    pinned = {id(node) for item in planned + branch[0].orelse for node in ast.walk(item)}
    assert [node.lineno for item in derive for node in ast.walk(item)
            if isinstance(node, ast.Name) and node.id == "TEAM" and id(node) not in pinned] == [], PRESENT_TEAM


def test_a_present_team_check_added_in_the_leaf_fails_the_pin(monkeypatch):
    # The check the review added, in team_block's present branch.
    old = "    if present:\n        team_policy()\n"
    assert LEAF_SOURCE.count(old) == 1
    drifted = LEAF_SOURCE.replace(old, old + '        require(not running(selection["uid"]), "team_busy")\n')
    monkeypatch.setitem(globals(), "LEAF_SOURCE", drifted)
    with pytest.raises(AssertionError, match="outside team_policy"):
        test_a_present_team_account_meets_only_team_policy_in_the_leaf()
    # And one beside derive_selection's branch, for the team account alone.
    old = "        gid, home = account.pw_gid, account.pw_dir\n    _, profile = api_get(api, \"profiles/current\")\n"
    assert LEAF_SOURCE.count(old) == 1
    drifted = LEAF_SOURCE.replace(old, old.replace(
        "    _, profile", '    require(selected["account"] != TEAM or quiet(), "team_busy")\n    _, profile'))
    monkeypatch.setitem(globals(), "LEAF_SOURCE", drifted)
    with pytest.raises(AssertionError, match="outside team_policy"):
        test_a_present_team_account_meets_only_team_policy_in_the_leaf()


def test_the_table_covers_every_code_and_the_probe_takes_only_the_team_codes():
    copied = "\n\n".join(top_level(team_check_block())[name] for name in COPIED)
    codes = refusal_codes(copied)
    # The nine team codes, and two file checks reported as unknown.
    assert codes == set(TEAM_CODES) | {"unsafe_parent", "unsafe_file"}
    assert {expected for expected, _change, _after in TEAM_CASES.values()} == set(POLICIES)
    # The shell admits exactly those nine and conforms.
    admitted = re.search(r"case \"\$word\" in\n(.*?)\n        \*\) return 0", PROBE_SOURCE, re.S)[1]
    assert set(re.findall(r"[a-z_]+", admitted.replace(";;", " "))) == {"conforms", *TEAM_CODES}


@pytest.mark.parametrize("name", sorted(TEAM_CASES))
def test_the_probe_and_the_leaf_judge_the_team_account_alike(name, inventory, tmp_path, monkeypatch):
    expected, change, after = TEAM_CASES[name]
    computer = Computer(payload(), monkeypatch, tmp_path)
    change(computer)
    uid = computer.users.get("claudlobby", {"pw_uid": 1500})["pw_uid"]
    leaf, logins, _elapsed = judge_both(computer, inventory, tmp_path, after)
    assert leaf == RAW.get(name, expected)
    assert logins["team"] == {"uid": uid, "policy": as_reported(leaf)}
    assert as_reported(leaf) == expected


@pytest.mark.parametrize("body,expected", [
    ("printf '%s\\n'", "conforms"), ("printf '%s\\n' team_account_shell; :", "team_account_shell"),
    ("printf '%s\\n' conforms; exit 1", "unknown"), ("exit 0", "unknown"), ("printf '%s'", "unknown"),
    ("printf '%s\\n%s\\n' conforms", "unknown"), ("printf '%s\\n' conforming; :", "unknown"),
    ("printf '%s\\n' unsafe_file; :", "unknown"), ("printf '%s\\n' unsafe_parent; :", "unknown"),
    ("printf '%s\\n' unknown; :", "unknown"), ("printf '%s\\n' Conforms; :", "unknown"),
    ("printf '%s\\n' team_account_; :", "unknown"), ("printf '%0300d\\n' 0; :", "unknown"),
    ("printf '%s\\n' team_account_uid; printf '%0300d' 0; :", "unknown"),
    ("printf '%s\\n' conforms >&2; :", "unknown"), ("kill -ALRM $$; printf '%s\\n'", "unknown"),
], ids=["conforms", "a-code", "then-fails", "silent", "no-newline", "two-lines", "another-word", "unsafe-file",
        "unsafe-parent", "unknown-itself", "capital", "a-prefix", "oversize", "then-oversize", "stderr-only",
        "stopped"])
def test_only_one_admitted_word_and_a_clean_exit_is_an_answer(host, tmp_path, body, expected):
    python = stand_in(tmp_path, body, "conforms")
    logins = host(passwd=[user("claudlobby", 1500)], python=python)
    assert logins["team"] == {"uid": 1500, "policy": expected}


@pytest.mark.parametrize("damage", ["missing", "not-executable", "a-folder"])
def test_no_runnable_python_is_unknown_and_nothing_runs(host, tmp_path, damage):
    marker = tmp_path / "ran"
    python = stand_in(tmp_path, f"printf x >'{marker}'; printf '%s\\n'", "conforms")
    if damage == "missing":
        python.unlink()
    elif damage == "not-executable":
        python.chmod(0o644)
    else:
        python.unlink()
        python.mkdir()
    assert host(passwd=[user("claudlobby", 1500)], python=python)["team"] == {"uid": 1500, "policy": "unknown"}
    assert not marker.exists()


def test_without_a_team_line_python_never_runs_and_uncertain_is_still_reported(host, tmp_path):
    marker = tmp_path / "ran"
    python = stand_in(tmp_path, f"printf x >'{marker}'; printf '%s\\n'", "conforms")
    logins = host(passwd=[user("demo", 1000)], receipts=[receipt(state="running")], python=python)
    assert logins == {"team": None, "next_uid": 1001, "prepared": [], "uncertain": True}
    assert not marker.exists()
    # Off Linux or not root, no logins at all, and no Python.
    for changes in ({"system": "Darwin"}, {"uid": b"1001\n"}):
        result, data = host.inventory(user=b"root\n", python=python, **{"uid": b"0\n", **changes})
        assert data["logins"] is None and not marker.exists()


@pytest.mark.parametrize("fault,root,expected", [(None, True, "conforms"), ("raise", True, "unknown"),
                                                 ("hang", True, "unknown"), (None, False, "unknown")],
                         ids=["answers", "raises", "hangs", "not-root"])
def test_the_copy_fails_closed(inventory, tmp_path, monkeypatch, fault, root, expected):
    if not root and os.geteuid() == 0:
        pytest.skip("the suite runs as root, so the copy cannot run as another account")
    computer = Computer(payload(), monkeypatch, tmp_path)
    computer.add_team(1500)
    # The shipped alarm is 4 seconds; here 1, so the hang ends quickly.
    leaf, logins, elapsed = judge_both(computer, inventory, tmp_path, fault=fault, root=root, alarm=1)
    assert leaf == "conforms" and logins["team"] == {"uid": 1500, "policy": expected}
    assert elapsed < 10
    if fault == "hang":
        assert elapsed >= 1


def test_the_real_interpreter_runs_the_block_isolated(host, tmp_path):
    """The real interpreter on the shipped block, as the probe runs it. Not root
    here, so the block answers nothing; whatever the environment says, Python
    imports nothing from it and runs no startup file."""
    marker = tmp_path / "injected"
    evil = tmp_path / "evil"
    evil.mkdir()
    for name in ("pwd", "grp", "stat", "signal", "sitecustomize", "usercustomize"):
        (evil / f"{name}.py").write_text(f"open({str(marker)!r}, 'a').write({name!r})\n")
    (evil / "startup.py").write_text(f"open({str(marker)!r}, 'a').write('startup')\n")
    env = {"PYTHONPATH": evil, "PYTHONSTARTUP": evil / "startup.py", "PYTHONINSPECT": "1", "PYTHONUSERBASE": evil}
    host(passwd=[user("claudlobby", 1500)])  # writes the account files
    result, data = host.inventory(uid=b"0\n", user=b"root\n", python=Path(sys.executable), env=env.items())
    assert result.returncode == 0 and not marker.exists()
    if os.geteuid() != 0:
        assert data["logins"]["team"] == {"uid": 1500, "policy": "unknown"}
        # It compiled, imported its modules and stopped at its root check.
        ran = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", team_check_block()], cwd=tmp_path,
                             env=constructed_env(**{k: str(v) for k, v in env.items()}), stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=30)
        assert (ran.returncode, ran.stdout, ran.stderr, marker.exists()) == (3, b"", b"", False)


def test_the_team_checks_worst_case_is_fast(inventory, tmp_path, monkeypatch):
    """Every file the copy reads at its bound, each shaped to cost the most:
    sudoers files of escapes and continued lines, 64 of them, 512 accounts."""
    computer = Computer(payload(), monkeypatch, tmp_path)
    computer.add_team(1500)
    for n in range(500):
        computer.users[f"u{n}"] = {"pw_uid": 2000 + n, "pw_gid": 2000 + n, "pw_dir": "/home/user"}
        computer.groups[f"u{n}"] = {"gid": 2000 + n, "mem": [f"u{m}" for m in range(n % 8)]}
    tail = "# end\n"
    unit = "\\x41\\\n"
    computer.sudoers_d = {f"r{n:02d}": unit * ((65536 - len(tail)) // len(unit)) + tail for n in range(64)}
    padding = "# " + "\\x41" * ((65536 - len(UBUNTU_SUDOERS) - 3) // 4) + "\n"
    computer.extra["sudoers"] = padding + UBUNTU_SUDOERS
    lines = "passwd: files systemd\ngroup: files systemd\n"
    computer.extra["nsswitch"] = lines * (65536 // len(lines))
    computer.extra["subuid"] = computer.extra["subgid"] = lines_up_to(
        65536, lambda n: f"r{n}:{100000 + n * 65536}:65536\n")
    computer.shadow["filler"] = lines_up_to(262144 - 4096, lambda n: f"x{n}:!:20000:0:99999:7:::\n")[:-1]
    computer.gshadow["filler"] = lines_up_to(262144 - 4096, lambda n: f"g{n}:!::x{n}\n")[:-1]
    computer.write()
    sizes = {name: (computer.files / name).stat().st_size for name in ("sudoers", "nsswitch", "subuid", "shadow",
                                                                     "gshadow", "passwd")}
    assert sizes["sudoers"] <= 65536 and sizes["nsswitch"] <= 65536 and sizes["subuid"] <= 65536
    assert sizes["shadow"] <= 262144 and sizes["gshadow"] <= 262144 and sizes["passwd"] <= 65536
    assert all((computer.files / "sudoers.d" / name).stat().st_size <= 65536 for name in computer.sudoers_d)
    leaf, logins, elapsed = judge_both(computer, inventory, tmp_path)
    assert leaf == "conforms" and logins["team"] == {"uid": 1500, "policy": "conforms"}
    # The copy ends itself at 4 seconds; this case, the whole probe with its
    # interpreter start, took about a second on a laptop.
    assert elapsed < 3.5, f"the team check's worst case took {elapsed:.2f}s"


# Whether setup's root store holds an uncertain operation, as the leaf's
# no_pending judges each receipt: (kind, state, dispatch record, uncertain).
SHAPES = {
    "system-planned": ("system", "planned", False, False),
    "system-completed": ("system", "completed", True, False),
    "system-failed": ("system", "failed_partial", True, False),
    "system-conflict": ("system", "conflict", True, False),
    "system-dispatched": ("system", "dispatched", True, True),
    "system-running": ("system", "running", True, True),
    "system-unknown": ("system", "unknown", True, True),
    "system-completed-undispatched": ("system", "completed", False, True),
    "system-running-undispatched": ("system", "running", False, True),
    "refresh-planned": ("index-refresh", "planned", False, False),
    "refresh-completed": ("index-refresh", "completed", True, False),
    "refresh-running": ("index-refresh", "running", True, True),
    "refresh-failed-undispatched": ("index-refresh", "failed_partial", False, True),
    "ingress-planned": ("ingress", "planned", False, False),
    "ingress-no-op": ("ingress", "completed", False, False),
    "ingress-completed": ("ingress", "completed", True, False),
    "ingress-conflict": ("ingress", "conflict", True, False),
    "ingress-failed": ("ingress", "failed_partial", True, False),
    "ingress-dispatched": ("ingress", "dispatched", True, True),
    "ingress-unknown": ("ingress", "unknown", True, True),
    "ingress-unknown-undispatched": ("ingress", "unknown", False, True),
}
BOOT = "22222222-2222-4222-8222-222222222222"
# The native unit as no_pending may find it: at rest, collected, or running.
UNITS = {"quiet": {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"},
         "collected": {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
         "busy": {"LoadState": "loaded", "ActiveState": "active", "MainPID": "4242"}}


def leaf_receipt(leaf, kind, state, dispatched, request_id):
    """A receipt the leaf itself loads: its request id and its digest."""
    record = json.loads(receipt(kind=kind, state=state))
    if not dispatched:
        record.pop("dispatch", None)
        record.pop("submission", None)
    record.update(request_id=request_id, digest=leaf.digest(record["proposal"]))
    if kind == "ingress" and state == "completed" and not dispatched:
        record["effect"] = "none_existing_exact_route"
    return leaf.canonical(record)


def leaf_no_pending(leaf, folder, monkeypatch, data, request_id, unit):
    """The leaf's own no_pending over a store holding only data, with the unit
    as given and nothing live observable (no route, no binding)."""
    folder.mkdir(mode=0o700)
    path = folder / (request_id + ".json")
    path.write_bytes(data)
    path.chmod(0o600)

    def unobservable(*_args):
        raise leaf.Refusal("native_unavailable")
    for name, value in (("STORE", folder), ("OWNER", os.geteuid()), ("trusted_parents", lambda p: None),
                        ("boot_id", lambda: BOOT), ("package_health", lambda: {}),
                        ("unit_state", lambda name: dict(UNITS[unit])), ("binding", unobservable),
                        ("api_get", unobservable)):
        monkeypatch.setattr(leaf, name, value)
    try:
        leaf.no_pending(None)
    except leaf.Refusal as error:
        return error.code
    return "admitted"


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_uncertain_mirrors_how_the_leaf_judges_each_receipt(name, host, tmp_path, monkeypatch):
    kind, state, dispatched, expected = SHAPES[name]
    leaf = leaf_module()
    request_id = "44444444-4444-4444-8444-444444444444"
    data = leaf_receipt(leaf, kind, state, dispatched, request_id)
    verdicts = {unit: leaf_no_pending(leaf, tmp_path / ("leaf-" + unit), monkeypatch, data, request_id, unit)
                for unit in UNITS}
    assert host(passwd=[user("demo", 1000)], receipts=[data])["uncertain"] is expected
    if expected:
        # Some native state makes the leaf refuse it as unfinished.
        assert verdicts["busy"] in ("prior_operation_uncertain", "prior_operation_not_inactive"), verdicts
    else:
        # Finished and at rest, or collected: the leaf admits a new operation.
        assert verdicts["quiet"] == verdicts["collected"] == "admitted", verdicts
    # Wherever the leaf calls it uncertain with nothing observable live, so
    # does the probe. (The other way, a dispatched system operation whose unit
    # is at rest, the leaf admits and the probe still says true.)
    if "prior_operation_uncertain" in verdicts.values():
        assert expected


DOUBT = {
    "a-link": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").symlink_to(
        tmp_path / "outside.json"),
    "a-folder": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").mkdir(),
    "another-name": lambda store, tmp_path: (store / "notes.json").write_bytes(receipt()),
    "capital-name": lambda store, tmp_path: (store / "AAAAAAAA-1111-4111-8111-111111111111.json").write_bytes(
        receipt()),
    "not-json": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(b"x"),
    "another-shape": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(
        receipt(extra={"shadow": selection("evil", 1001)})),
    "two-kinds": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(
        receipt(extra={"note": {"a": 1, "kind": "ingress"}})),
    "no-kind": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(
        receipt().replace(b',"kind":"system"', b"")),
    "a-state-not-a-word": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(
        receipt(state="Completed")),
    "oversize": lambda store, tmp_path: (store / "11111111-1111-4111-8111-111111111111.json").write_bytes(
        sized(98305)),
}


@pytest.mark.parametrize("name", sorted(DOUBT))
def test_doubt_reads_null_unless_an_operation_is_uncertain(host, tmp_path, name):
    (tmp_path / "outside.json").write_bytes(receipt())
    host.store.mkdir(mode=0o700)
    DOUBT[name](host.store, tmp_path)
    assert host(passwd=[user("demo", 1000)])["uncertain"] is None
    # One uncertain receipt beside it, read before or after: true, whatever
    # else is doubt.
    for name in ("00000000-0000-4000-8000-000000000000", "ffffffff-ffff-4fff-8fff-ffffffffffff"):
        (host.store / (name + ".json")).write_bytes(receipt(state="running"))
        assert host(passwd=[user("demo", 1000)])["uncertain"] is True
        (host.store / (name + ".json")).unlink()


@pytest.mark.parametrize("receipts,expected", [
    (None, False), ([], False), ([receipt(state="planned")] * 3, False),
    ([receipt(), receipt(kind="ingress"), receipt(kind="index-refresh", state="failed_partial")], False),
    ([receipt(), receipt(state="paused")], True), ([receipt(kind="ingress", state="unknown"), receipt()], True),
], ids=["no-store", "empty-store", "only-planned", "only-finished", "a-state-never-written", "ingress-unknown"])
def test_uncertain_over_the_whole_store(host, receipts, expected):
    assert host(passwd=[user("demo", 1000)], receipts=receipts)["uncertain"] is expected
