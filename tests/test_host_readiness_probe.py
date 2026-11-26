"""Preinstall inventory is bounded data; prerequisites are never executed."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "claudlobby/preinstall/host-readiness-probe.sh"
LEAF = ROOT / "claudlobby/preinstall/linux-system-v1.sh"
TOOLS = ("python3", "git", "tailscale", "claude", "tmux", "jq", "systemctl", "launchctl")
# The shipped probe runs these tools and reads these root files by fixed
# absolute path. Fixtures run a copy with each one rewritten into the test's
# own directory; the shipped bytes honour no override.
TOOL_PATHS = {"/usr/bin/uname": "uname", "/usr/bin/id": "id", "/bin/dd": "dd", "/usr/bin/tr": "tr"}
ROOT_PATHS = {"/etc/passwd": "passwd", "/etc/group": "group",
              "/var/lib/claudlobby-browser-system/v1": "lib/claudlobby-browser-system/v1"}
STORE = ROOT_PATHS["/var/lib/claudlobby-browser-system/v1"]
LOGIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def whole(value, low, high):
    return type(value) is int and low <= value <= high


def website_accepts(logins):
    """A Python copy of the website's logins rule (lib/host-readiness.ts on
    its main branch). An answer it refuses makes the whole check unusable."""
    if logins is None:
        return True
    if type(logins) is not dict or set(logins) != {"team", "next_uid", "prepared"}:
        return False
    team, next_uid, prepared = logins["team"], logins["next_uid"], logins["prepared"]
    if team is not None and not (type(team) is dict and set(team) == {"uid"}
                                 and whole(team["uid"], 0, 9999999999)):
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


def rewritten_probe(path, stubs, files=None):
    source = PROBE.read_text()
    for real, name in TOOL_PATHS.items():
        assert real + " " in source
        source = source.replace(real + " ", str(stubs / name) + " ")
    for real, name in ROOT_PATHS.items():
        if files is not None:
            source = source.replace(real, str(files / name))
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
            root_paths=True, env=()):
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
        probe = rewritten_probe(tmp_path / "probe.sh", stubs, files if root_paths else None)
        result = subprocess.run(["/bin/sh", str(probe), *args], cwd=work,
            env=constructed_env(PATH=stubs, USER="forged", LOGNAME="forged", **dict(env)),
            input=b"ignored input", capture_output=True, timeout=10)
        assert result.stderr == b""
        assert len(result.stdout) <= 8192
        assert result.stdout.endswith(b"\n") and result.stdout.count(b"\n") == 1
        assert not marker.exists(), "inventory executed a prerequisite or native-manager CLI"
        assert not any(work.iterdir()), "inventory executed something it read"
        data = json.loads(result.stdout)
        if "logins" in data:
            assert website_accepts(data["logins"]), data["logins"]
        return result, data

    run.files = files
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


def test_shipped_probe_runs_fixed_tools_and_reads_fixed_files():
    source = PROBE.read_text()
    code = [line for line in source.splitlines()[1:] if not line.lstrip().startswith("#")]
    text = "\n".join(code)
    # Every executable it names is one of four tools, by absolute path, and
    # those paths hold the real tools on macOS and Ubuntu alike.
    assert set(re.findall(r"/(?:usr/)?s?bin/[\w.-]+", text)) == set(TOOL_PATHS)
    for path in TOOL_PATHS:
        assert re.search(r"(?<![\w/.-])" + re.escape(path) + " ", text)
        assert os.access(path, os.X_OK)
    # command is only ever a lookup, never a way to run a tool by name.
    assert re.findall(r"\bcommand\b(?! -v )", text) == []
    # The files are fixed assignments. No ${name-default} form exists, so the
    # environment cannot move them, and the store is the leaf's own.
    for line in ("passwd_file=/etc/passwd", "group_file=/etc/group",
                 "store=/var/lib/claudlobby-browser-system/v1"):
        assert code.count(line) == 1
    assert re.findall(r"\$\{\w+:?[-=?+]", text) == []
    assert 'STORE = Path("/var/lib/claudlobby-browser-system/v1")' in LEAF.read_text()


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

    run.files, run.store = files, store
    return run


DEMO = {"name": "demo", "uid": 1000}


@pytest.mark.parametrize("system,uid", [("Linux", b"1001\n"), ("Darwin", b"0\n"), ("Darwin", b"501\n")],
                         ids=["linux-non-root", "mac-root", "mac-non-root"])
def test_logins_are_read_only_by_root_on_linux(host, system, uid):
    accounts = {"passwd": [user("demo", 1000), user("claudlobby", 1001)], "receipts": [receipt()]}
    assert host(**accounts) == {"team": {"uid": 1001}, "next_uid": 1002, "prepared": [DEMO]}
    assert host(**accounts, system=system, uid=uid) is None


def test_an_untouched_computer_offers_the_first_free_id(host):
    assert host() == {"team": None, "next_uid": 1000, "prepared": []}
    host.store.mkdir(mode=0o700)
    assert host() == {"team": None, "next_uid": 1000, "prepared": []}


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
    assert logins == {"team": {"uid": 1001}, "next_uid": 1501, "prepared": [DEMO]}
    # An id setup cannot use is still reported; the website refuses it.
    assert host(passwd=[f"claudlobby:x:0:0::{HOME}:/bin/bash"])["team"] == {"uid": 0}


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


def test_scanning_stops_at_the_second_distinct_account(host):
    accounts = [user("demo", 1000), user("alice", 1001), user("bob", 1002)]
    oversize = sized(98305, account="bob", uid=1002)
    logins = host(passwd=accounts, receipts=[receipt(), receipt(), receipt("alice", 1001), oversize])
    assert logins["prepared"] == [DEMO, {"name": "alice", "uid": 1001}]
    assert host(passwd=accounts, receipts=[receipt(), oversize, receipt("alice", 1001)])["prepared"] is None


def test_two_accounts_sharing_a_uid_are_reported_once(host):
    # Two passwd names can share a uid; the website refuses prepared entries
    # that do, so the second is skipped and scanning goes on.
    accounts = [user("demo", 1000), user("twin", 1000), user("alice", 1001)]
    receipts = [receipt(), receipt("twin", 1000), receipt("alice", 1001)]
    assert host(passwd=accounts, receipts=receipts)["prepared"] == [DEMO, {"name": "alice", "uid": 1001}]


@pytest.mark.parametrize("count,expected", [(128, [DEMO]), (129, None)])
def test_more_than_128_receipt_names_read_as_unknown(host, count, expected):
    receipts = [receipt(state="planned")] * (count - 1) + [receipt()]
    assert host(passwd=[user("demo", 1000)], receipts=receipts)["prepared"] == expected


@pytest.mark.parametrize("size,expected", [(98304, [DEMO]), (98305, None)])
def test_a_receipt_over_96_kib_reads_as_unknown(host, size, expected):
    assert host(passwd=[user("demo", 1000)], receipts=[sized(size)])["prepared"] == expected


def test_symlinks_and_non_files_in_the_store_are_skipped(host, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(receipt())
    host.store.mkdir(mode=0o700)
    (host.store / "11111111-1111-4111-8111-111111111111.json").symlink_to(outside)
    (host.store / "22222222-2222-4222-8222-222222222222.json").symlink_to(tmp_path / "dangling")
    (host.store / "33333333-3333-4333-8333-333333333333.json").mkdir()
    assert host(passwd=[user("demo", 1000)])["prepared"] == []


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
    assert host(passwd=[user("demo", 1000)])["prepared"] is None


def test_nothing_read_is_executed(host):
    # The fixture runs the probe in an empty directory and asserts it stays empty.
    evil = "$(printf x >ran1)`printf y >ran2`;printf z >ran3;*"
    passwd = [f"demo:x:1000:1000:{evil}:{HOME}:/bin/bash", f"{evil}:x:1001:1001::/:/bin/sh"]
    group = [f"{evil}:x:1002:{evil}"]
    host.store.mkdir(mode=0o700)
    (host.store / (evil + ".json")).write_bytes(receipt(hooks=[evil]))
    assert host(passwd=passwd, group=group) == {"team": None, "next_uid": 1003, "prepared": [DEMO]}


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


def test_the_worst_case_stays_small_and_fast(host):
    # 512-line, 64 KiB account files; 128 receipts of 96 KiB, each shaped like
    # the leaf's with its full 16,000 characters of brace-heavy hook text. Every
    # receipt is read: the two that count come last.
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
    started = time.monotonic()
    logins = host(passwd=None, group=None, architecture=b"a" * 32 + b"\n", user=b"r" * 64 + b"\n")
    elapsed = time.monotonic() - started
    for name in ("passwd", "group"):
        text = (host.files / name).read_bytes()
        assert len(text) <= 65536 and text.count(b"\n") == 512
    assert logins == {"team": {"uid": 9999999999}, "next_uid": 59999,
                      "prepared": [{"name": name, "uid": uid} for name, uid in longest.items()]}
    # Root mode runs under dash on Ubuntu: 3 seconds. bash 3.2, /bin/sh on a
    # Mac (where root mode never runs), splits braces more slowly, and a shared
    # macOS runner took 6.2 seconds: 15 there, which still catches a run whose
    # time grows with the square of the input (minutes at this size).
    bash = subprocess.run(["/bin/sh", "-c", 'printf %s "${BASH_VERSION-}"'],
                          capture_output=True, timeout=5).stdout
    budget = 15 if bash else 3
    assert elapsed < budget, f"worst case took {elapsed:.2f}s under {'bash' if bash else 'dash'}"


def leaf_module():
    body = LEAF.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    module = types.ModuleType("linux_system_leaf")
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
    leaf.private_worker(request_id, leaf.artifact_path(sha), None)
    completed = (store / (request_id + ".json")).read_bytes()
    assert json.loads(completed)["state"] == "completed" and len(effects) == 2
    assert b"Post-Invoke {" in completed and b'\\"state\\":\\"completed\\"' in completed
    accounts = [user("demo", 1000)]
    assert host(passwd=accounts, receipts=[planned])["prepared"] == []
    assert host(passwd=accounts, receipts=[completed])["prepared"] == [DEMO]
