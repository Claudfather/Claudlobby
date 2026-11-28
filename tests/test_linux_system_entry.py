"""Root-entry admission and real private filesystem effects, never native effects."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "claudlobby/preinstall/linux-system-entry-v1.sh"
LEAF = ROOT / "claudlobby/preinstall/linux-system-v1.sh"


def payload():
    assert SCRIPT.is_file(), "immutable root entry is missing"
    body = SCRIPT.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    module = types.ModuleType("linux_entry_fixture")
    exec(compile(body, str(SCRIPT), "exec"), module.__dict__)
    return module


def wire(module, verb="inspect", **changes):
    value = {"schema_version": 1, "request_id": "00112233-4455-6677-8899-aabbccddeeff",
             "artifact_sha256": module.LEAF_SHA256,
             "selection": {"account": "example", "uid": 1001, "node_key": "nodekey:" + "a" * 64}}
    if verb == "inspect":
        value["kind"] = "system"
    if verb == "apply":
        value["selection"].update(gid=1001, home="/home/example", profile_id="abcd")
        value.update(digest="c" * 64, decision="approve")
    value.update(changes)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def private_store(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "STORE", tmp_path / "entry" / "v1")
    monkeypatch.setattr(module, "OWNER", os.geteuid())
    # Only real root ancestors are unavailable in a non-root test process.
    monkeypatch.setattr(module, "trusted_parents", lambda path: None)
    return module.STORE / ("bundle-" + module.LEAF_SHA256 + ".sh")


def test_exact_whole_leaf_is_the_only_admitted_payload():
    module = payload()
    raw = LEAF.read_bytes()
    assert module.decode_payload(base64.b64encode(raw).decode()) == raw
    for text in (base64.b64encode(raw + b"\n").decode(), "!", "eA==\n", "eA==" * 30000):
        with pytest.raises(module.Refusal):
            module.decode_payload(text)


def test_the_pin_is_the_current_leaf_within_the_payload_limit():
    # Only LEAF_SHA256 changes when the leaf does; the bound and the uid gate stay.
    module = payload()
    raw = LEAF.read_bytes()
    assert module.LEAF_SHA256 == hashlib.sha256(raw).hexdigest()
    assert module.PAYLOAD_LIMIT == 65536 and len(raw) <= 61440


@pytest.mark.parametrize("verb", ["inspect", "apply", "status"])
def test_canonical_request_is_preserved_without_plan_replacement(verb):
    module = payload(); raw = wire(module, verb)
    assert module.validate_request(verb, raw)["selection"]["uid"] == 1001
    changed_inputs = [raw + b"\n", raw.replace(b":", b": ", 1), raw[:-1],
                    raw.replace(b'"schema_version":1', b'"schema_version":true'),
                    raw.replace(b'"uid":1001', b'"uid":0'),
                    wire(module, verb, artifact_sha256="d" * 64), wire(module, verb, command="id")]
    selected = json.loads(raw)["selection"]
    if verb == "apply":
        changed_inputs.extend([raw.replace(b'"gid":1001', b'"gid":true'),
                               wire(module, verb, selection={k: v for k, v in selected.items() if k != "home"})])
    else:
        changed_inputs.append(wire(module, verb, selection={**selected, "home": "/home/example"}))
    for changed in changed_inputs:
        with pytest.raises(module.Refusal):
            module.validate_request(verb, changed)


@pytest.mark.parametrize("verb", ["_worker", "_hook", "shell", "prepare", "inspect;id"])
def test_private_and_generic_verbs_cannot_reach_staging(verb):
    module = payload()
    with pytest.raises(module.Refusal):
        module.validate_request(verb, wire(module))


def test_inspect_exclusively_stages_and_reuses_exact_root_artifact(tmp_path, monkeypatch):
    module = payload(); target = private_store(module, tmp_path, monkeypatch)
    body = LEAF.read_bytes()
    assert module.stage("inspect", body) == target
    inode = target.stat().st_ino
    assert target.read_bytes() == body and target.stat().st_mode & 0o777 == 0o500
    assert target.stat().st_nlink == 1
    assert module.STORE.stat().st_mode & 0o777 == 0o700
    assert (module.STORE / "transition.lock").stat().st_mode & 0o777 == 0o600
    assert module.stage("inspect", body) == target and target.stat().st_ino == inode
    assert sorted(p.name for p in module.STORE.iterdir()) == [target.name, "transition.lock"]


@pytest.mark.parametrize("verb", ["apply", "status"])
def test_recovery_does_not_create_missing_store_or_restage(tmp_path, monkeypatch, verb):
    module = payload(); target = private_store(module, tmp_path, monkeypatch)
    with pytest.raises((module.Refusal, OSError)):
        module.stage(verb, LEAF.read_bytes())
    assert not module.STORE.parent.exists()
    module.stage("inspect", LEAF.read_bytes()); target.unlink()
    before = {p.name: p.read_bytes() for p in module.STORE.iterdir()}
    with pytest.raises((module.Refusal, OSError)):
        module.stage(verb, LEAF.read_bytes())
    assert before == {p.name: p.read_bytes() for p in module.STORE.iterdir()}


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "mode", "bytes", "directory", "lock", "leftover"])
def test_unsafe_existing_state_is_preserved_without_repair(tmp_path, monkeypatch, unsafe):
    module = payload(); target = private_store(module, tmp_path, monkeypatch)
    body = LEAF.read_bytes(); module.stage("inspect", body)
    if unsafe == "symlink":
        target.unlink(); target.symlink_to(LEAF)
    elif unsafe == "hardlink":
        os.link(target, module.STORE / "alias")
    elif unsafe == "mode":
        target.chmod(0o700)
    elif unsafe == "bytes":
        target.chmod(0o700); target.write_bytes(b"foreign"); target.chmod(0o500)
    elif unsafe == "directory":
        target.unlink(); target.mkdir()
    elif unsafe == "lock":
        (module.STORE / "transition.lock").chmod(0o644)
    else:
        (module.STORE / ".entry-uncertain").write_bytes(b"preserve")
    before = {p.name: (p.lstat().st_ino, p.lstat().st_mode) for p in module.STORE.iterdir()}
    with pytest.raises((module.Refusal, OSError)):
        module.stage("inspect", body)
    assert before == {p.name: (p.lstat().st_ino, p.lstat().st_mode) for p in module.STORE.iterdir()}


def test_transition_lock_is_shared_nonblocking_and_released_before_handoff(tmp_path, monkeypatch):
    import fcntl
    module = payload(); target = private_store(module, tmp_path, monkeypatch)
    module.stage("inspect", LEAF.read_bytes())
    with (module.STORE / "transition.lock").open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.Refusal) as exc:
            module.stage("inspect", LEAF.read_bytes())
        assert exc.value.code == "transition_busy"
    module.stage("status", LEAF.read_bytes())
    with (module.STORE / "transition.lock").open("r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert target.read_bytes() == LEAF.read_bytes()


def test_exclusive_publication_does_not_replace_a_concurrent_target(tmp_path, monkeypatch):
    module = payload(); target = private_store(module, tmp_path, monkeypatch)
    link = module.os.link
    def raced(source, destination, **kwargs):
        target.write_bytes(b"foreign concurrent state"); target.chmod(0o500)
        return link(source, destination, **kwargs)
    monkeypatch.setattr(module.os, "link", raced)
    with pytest.raises((module.Refusal, OSError)):
        module.stage("inspect", LEAF.read_bytes())
    assert target.read_bytes() == b"foreign concurrent state"
    assert not list(module.STORE.glob(".entry-*"))


def test_root_admission_does_not_lookup_or_derive_ordinary_account_policy(monkeypatch):
    import pwd
    module = payload()
    monkeypatch.setattr(module, "admit_platform", lambda: None)
    monkeypatch.setattr(module.os, "getuid", lambda: 0)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "getgid", lambda: 0)
    monkeypatch.setattr(module.os, "getegid", lambda: 0)
    monkeypatch.setattr(pwd, "getpwuid", lambda *args: pytest.fail("entry looked up account policy"))
    monkeypatch.setattr(pwd, "getpwnam", lambda *args: pytest.fail("entry looked up account policy"))
    module.admit_identity()
    monkeypatch.setattr(module.os, "getuid", lambda: 1001)
    with pytest.raises(module.Refusal):
        module.admit_identity()


def test_apply_forwards_original_selection_without_rederiving_leaf_policy():
    module = payload(); selected = json.loads(wire(module, "apply"))["selection"]
    # These finite strings remain data. The leaf alone admits their semantic
    # account/HOME/profile meaning against the original retained receipt.
    selected.update(home="/home/example/../example", profile_id="original-profile")
    raw = wire(module, "apply", selection=selected)
    assert module.validate_request("apply", raw)["selection"] == selected


@pytest.mark.parametrize("raw", [b"{}", b"{}\n{}\n", b"x" * 4097, b"\xff\n"])
def test_stdin_requires_bounded_ascii_line_and_eof(raw):
    module = payload(); read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw); os.close(write_fd); write_fd = -1
        with pytest.raises(module.Refusal):
            module.read_request("inspect", read_fd)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_stdin_validates_original_bytes_and_requires_eof(monkeypatch):
    module = payload(); raw = wire(module); read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw); os.close(write_fd); write_fd = -1
        actual, value = module.read_request("inspect", read_fd)
        assert actual == raw and value["request_id"] == "00112233-4455-6677-8899-aabbccddeeff"
    finally:
        os.close(read_fd)
    monkeypatch.setattr(module.select, "select", lambda *args: ([], [], []))
    with pytest.raises(module.Refusal):
        module.read_request("inspect")


@pytest.mark.parametrize("mutation", ["symlink", "writable", "foreign"])
def test_root_ancestor_gate_refuses_redirected_or_user_owned_paths(tmp_path, monkeypatch, mutation):
    module = payload(); parent = tmp_path / "parent"; parent.mkdir(mode=0o700)
    if mutation == "symlink":
        actual = tmp_path / "actual"; parent.rename(actual); parent.symlink_to(actual)
    elif mutation == "writable":
        parent.chmod(0o777)
    # Check the actual ancestor gate, isolating only unavoidable outer /tmp ownership.
    lstat = module.Path.lstat
    def isolated(path):
        info = lstat(path)
        if path == parent:
            return types.SimpleNamespace(st_mode=info.st_mode,
                                         st_uid=1 if mutation == "foreign" else 0)
        return types.SimpleNamespace(st_mode=0o40755, st_uid=0)
    monkeypatch.setattr(module.Path, "lstat", isolated)
    with pytest.raises(module.Refusal):
        module.trusted_parents(parent / "artifact")


def test_malformed_input_refuses_before_identity_staging_or_exec(monkeypatch, capsys):
    module = payload()
    monkeypatch.setattr(module.sys, "argv", ["entry", "inspect", base64.b64encode(LEAF.read_bytes()).decode()])
    monkeypatch.setattr(module, "read_request", lambda verb: module.validate_request(verb, b"{}\n"))
    monkeypatch.setattr(module, "stage", lambda *args: pytest.fail("staged malformed input"))
    monkeypatch.setattr(module, "admit_identity", lambda *args: pytest.fail("admitted malformed input"))
    monkeypatch.setattr(module, "handoff", lambda *args: pytest.fail("executed malformed input"))
    assert module.main() == 4
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_request"


def test_fixed_exec_forwards_exact_stdin_and_clean_environment(tmp_path, monkeypatch):
    module = payload(); raw = wire(module)
    target = private_store(module, tmp_path, monkeypatch)
    module.stage("inspect", LEAF.read_bytes())
    # Exercise real dup/pipe/exec in a disposable child. The fixed shell is
    # replaced only at the final boundary; no leaf/native operation executes.
    child = os.fork()
    if child == 0:
        def capture(binary, argv, env):
            okay = (binary == "/bin/sh" and argv == ["/bin/sh", str(target), "inspect"]
                    and env == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
                                "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
                    and os.read(0, 4097) == raw and os.read(0, 1) == b"" and os.getcwd() == "/")
            os._exit(0 if okay else 93)
        try:
            module.os.execve = capture
            module.handoff("inspect", raw, target)
        finally:
            os._exit(94)
    _, result = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(result) == 0


def test_shell_refuses_extra_arguments_without_root_or_native_calls():
    assert SCRIPT.is_file(), "immutable root entry is missing"
    result = subprocess.run(["/bin/sh", str(SCRIPT), "status", "eA==", "unexpected"],
                            input=b"{}\n", env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "invalid_request" and not result.stderr
