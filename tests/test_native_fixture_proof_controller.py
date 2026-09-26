"""Controller regressions only: every native execution callback is replaced."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import types

import pytest


SPEC = importlib.util.spec_from_file_location(
    "native_proof", Path(__file__).parent / "fixtures" / "native_fixture_proof.py"
)
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)


@pytest.fixture(autouse=True)
def no_native_execution(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("pure controller test attempted native execution")

    monkeypatch.setattr(proof.subprocess, "Popen", refuse)
    monkeypatch.setattr(proof.subprocess, "run", refuse)
    monkeypatch.setattr(proof.subprocess, "check_output", refuse)
    monkeypatch.setattr(proof, "snapshot", refuse)
    monkeypatch.setattr(proof.os, "killpg", refuse)
    for name, value in (("GITHUB_ACTIONS", "true"),
                        ("RUNNER_ENVIRONMENT", "github-hosted"),
                        ("CLAUDLOBBY_NATIVE_FIXTURE_PROOF", "1")):
        monkeypatch.setenv(name, value)


def test_cleanup_queries_exact_endpoint_after_private_directory_disappears(tmp_path, monkeypatch):
    directory = tmp_path / "removed"
    directory.mkdir()
    directory.rmdir()
    socket = directory / f"tmux-{os.getuid()}" / "pulse610"
    calls = []

    def native(args, **kwargs):
        calls.append(args)
        # Native -L falls back to the default directory if TMUX_TMPDIR is gone.
        selected = args[2] if args[1] == "-S" else "/outside/tmux/pulse610"
        return subprocess.CompletedProcess(args, 1, "", f"error connecting to {selected} (No such file or directory)\n")

    monkeypatch.setattr(proof.subprocess, "run", native)
    rows = proof.inspect_endpoints("/native/tmux", [
        {"socket_dir": str(directory), "socket_name": "pulse610"}], {})
    assert not rows[0]["query_invalid"]
    assert calls[0][:3] == ["/native/tmux", "-S", str(socket)]


def stub_arm(monkeypatch, tmp_path, *, endpoint=False):
    """Run the real controller; model only its process/native-tool boundaries."""
    base = tmp_path / ("long-hosted-runner-path-" * 5)
    (base / "evidence").mkdir(parents=True)
    source = base / "source"
    source.mkdir()
    fake_package = types.ModuleType("claudlobby")
    fake_package.__file__ = str(source / "claudlobby" / "__init__.py")
    monkeypatch.setitem(sys.modules, "claudlobby", fake_package)
    pytest_args, calls, roots = [], [], []

    def tools(state, *args):
        path = state / "bin"
        path.mkdir()
        return path, {}

    def pytest_main(args):
        pytest_args.extend(args)
        xml = Path(next(x.split("=", 1)[1] for x in args if x.startswith("--junitxml=")))
        xml.write_text('<testsuites><testsuite><testcase classname="fixture" name="control"/></testsuite></testsuites>')
        return 0

    class Child:
        pid = 12345  # A modeled group only; os.killpg is forbidden above.

        def __init__(self, args, **kwargs):
            state = Path(args[4])
            if endpoint:
                directory = state / "already-removed"
                proof.append(state / "tmux-attempts.jsonl", {
                    "socket_dir": str(directory), "socket_name": "pulse610"})
            with monkeypatch.context() as child_env:
                for name, value in kwargs["env"].items():
                    child_env.setenv(name, value)
                self.rc = proof.child_pytest(args[3], args[4], args[5], args[6:])
            registry = state / "pytest-root.json"
            if registry.exists():
                roots.append(json.loads(registry.read_text())["path"])

        def wait(self, **kwargs):
            return self.rc

    def native(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "unrecognized query failure")

    monkeypatch.setattr(proof, "make_tools", tools)
    monkeypatch.setattr(proof, "snapshot", lambda: [])
    monkeypatch.setattr(proof.subprocess, "Popen", Child)
    monkeypatch.setattr(proof.subprocess, "run", native)
    monkeypatch.setattr(pytest, "main", pytest_main)
    return base, source, pytest_args, calls, roots


def test_emergency_cleanup_uses_exact_endpoint_and_retains_invalid_receipt(tmp_path, monkeypatch):
    base, source, _, calls, _ = stub_arm(monkeypatch, tmp_path, endpoint=True)
    result = proof.run_arm("candidate-2", source, Path("/unused/python"), base, ["fixture"], "/native/tmux")
    socket = base / "runs/candidate-2/already-removed" / f"tmux-{os.getuid()}" / "pulse610"
    assert calls[-1] == ["/native/tmux", "-S", str(socket), "kill-server"]
    assert not proof.clean(result), "emergency cleanup cannot erase an invalid query"
    assert result["cleanup"]["attempted_endpoints_before_emergency_cleanup"][0]["query_invalid"]


@pytest.mark.parametrize("role", ["parent", "candidate"])
def test_each_arm_uses_short_registered_pytest_root_and_removes_it(tmp_path, monkeypatch, role):
    base, source, args, _, roots = stub_arm(monkeypatch, tmp_path)
    proof.run_arm(role + "-2", source, Path("/unused/python"), base, ["fixture"], "/native/tmux")
    basetemp = Path(next(x.split("=", 1)[1] for x in args if x.startswith("--basetemp=")))
    # pytest truncates the node component to 30 characters before its suffix.
    endpoint = basetemp / "test_pulse_completes_with_no_e0" / "root/tmux" / f"tmux-{os.getuid()}" / "pulse610-none"
    assert len(os.fsencode(endpoint.resolve())) < 100
    assert roots == [str(basetemp.parent)]
    assert not basetemp.parent.exists()
    assert not (base / "pytest-root.json").exists()
    receipt = json.loads((base / "evidence" / (role + "-2") / "pytest-root-cleanup.json").read_text())
    assert receipt["removed"] and receipt["unregistered"]


def register(root, registry):
    root = root.resolve()
    info = root.stat()
    proof.write_json(registry, {"path": str(root), "device": info.st_dev, "inode": info.st_ino})


@pytest.mark.parametrize("defect", ["missing", "malformed", "wrong-inode", "public-mode", "symlink"])
def test_registration_refuses_unowned_or_replaced_roots(tmp_path, defect):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    registry = tmp_path / "pytest-root.json"
    register(root, registry)
    assert proof.registered_pytest_root(registry) == root.resolve()
    if defect == "missing":
        registry.unlink()
    elif defect == "malformed":
        registry.write_text("[]")
    elif defect == "wrong-inode":
        record = json.loads(registry.read_text())
        record["inode"] += 1
        proof.write_json(registry, record)
    elif defect == "public-mode":
        root.chmod(0o755)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        record = json.loads(registry.read_text())
        record["path"] = str(alias)
        proof.write_json(registry, record)
    assert proof.registered_pytest_root(registry) is None


def test_tmux_short_pytest_root_requires_exact_registration(tmp_path, monkeypatch):
    base = tmp_path / "proof"
    base.mkdir()
    root = tmp_path / "np-private"
    root.mkdir(mode=0o700)
    directory = root / "p/root/tmux"
    directory.mkdir(parents=True)
    monkeypatch.setenv("TMUX_TMPDIR", str(directory))
    ledger = base / "tmux.jsonl"
    with pytest.raises(AssertionError, match="unowned"):
        proof.tmux_passthrough("/native/tmux", ledger, base, ["-L", "pulse610", "has-session"])
    register(root, base / "pytest-root.json")
    calls = []
    monkeypatch.setattr(proof.subprocess, "run", lambda args, **kw: (
        calls.append(args) or subprocess.CompletedProcess(args, 0)))
    args = ["-L", "pulse610", "has-session"]
    assert proof.tmux_passthrough("/native/tmux", ledger, base, args) == 0
    assert calls == [["/native/tmux", *args]], "native invocation itself is unchanged"
    (base / "pytest-root.json").unlink()
    with pytest.raises(AssertionError, match="unowned"):
        proof.tmux_passthrough("/native/tmux", ledger, base, args)
    assert len(calls) == 1


def test_network_guard_allows_only_active_registered_root(tmp_path, monkeypatch):
    site, base, root = tmp_path / "site", tmp_path / "proof", tmp_path / "np-private"
    site.mkdir()
    (base / "evidence").mkdir(parents=True)
    root.mkdir(mode=0o700)
    monkeypatch.setattr(proof.subprocess, "check_output", lambda *a, **kw: str(site))
    proof.install_network_guard(Path("/unused/python"), base)
    code = (site / "_native_fixture_network_guard.py").read_text()
    namespace = {}
    exec(code.replace("sys.addaudithook(check)", ""), namespace)
    check = namespace["check"]
    endpoint = str(root / "p" / "owned.sock")
    with pytest.raises(PermissionError):
        check("socket.connect", (object(), endpoint))
    register(root, base / "pytest-root.json")
    check("socket.connect", (object(), endpoint))
    for event, address in (("socket.connect", ("192.0.2.1", 443)),
                           ("socket.getaddrinfo", "example.invalid"),
                           ("socket.bind", str(tmp_path / "np-unregistered" / "socket"))):
        with pytest.raises(PermissionError):
            check(event, (object(), address))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        check("socket.connect", (object(), str(root / "escape/socket")))
    (base / "pytest-root.json").unlink()
    with pytest.raises(PermissionError):
        check("socket.connect", (object(), endpoint))
    assert (base / "evidence/forbidden-network.jsonl").read_text().count("\n") == 6


def test_controller_exception_still_cleans_and_unregisters_short_root(tmp_path, monkeypatch):
    base, source, _, _, _ = stub_arm(monkeypatch, tmp_path)
    allocated = []

    def fail_after_allocation(*args):
        root = proof.registered_pytest_root(base / "pytest-root.json")
        assert root is not None
        allocated.append(root)
        raise RuntimeError("modeled controller failure")

    monkeypatch.setattr(proof, "make_tools", fail_after_allocation)
    with pytest.raises(RuntimeError, match="modeled controller failure"):
        proof.run_arm("parent-2", source, Path("/unused/python"), base, ["fixture"], "/native/tmux")
    assert len(allocated) == 1 and not allocated[0].exists()
    assert not (base / "pytest-root.json").exists()
    receipt = json.loads((base / "evidence/parent-2/pytest-root-cleanup.json").read_text())
    assert receipt["removed"] and receipt["unregistered"]


@pytest.mark.parametrize("destination", ["state", "registry"])
def test_partial_registration_write_preserves_error_and_removes_own_record(tmp_path, monkeypatch, destination):
    base, source, _, _, _ = stub_arm(monkeypatch, tmp_path)
    target = (base / "pytest-root.json" if destination == "registry" else
              base / "runs/parent-2/pytest-root.json")
    original_open = Path.open

    class InterruptedWrite:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def fileno(self):
            return self.stream.fileno()
        def write(self, text):
            self.stream.write(text[:3])
            self.stream.flush()
            raise OSError("modeled registration write failure")

    def open_file(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return InterruptedWrite(stream) if path == target and mode in ("w", "x") else stream

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OSError, match="modeled registration write failure"):
        proof.run_arm("parent-2", source, Path("/unused/python"), base, ["fixture"], "/native/tmux")
    assert not (base / "pytest-root.json").exists()
    receipt = json.loads((base / "evidence/parent-2/pytest-root-cleanup.json").read_text())
    assert receipt["removed"] and receipt["unregistered"]


def test_cleanup_never_unlinks_a_replacement_registration(tmp_path, monkeypatch):
    base, source, _, _, _ = stub_arm(monkeypatch, tmp_path)
    registry = base / "pytest-root.json"

    def replace_then_fail(*args):
        replacement = base / "other-owner.json"
        replacement.write_text('{"other": "owner"}\n')
        replacement.replace(registry)
        raise OSError("modeled controller failure")

    monkeypatch.setattr(proof, "make_tools", replace_then_fail)
    with pytest.raises((OSError, AssertionError)):
        proof.run_arm("parent-2", source, Path("/unused/python"), base, ["fixture"], "/native/tmux")
    assert registry.read_text() == '{"other": "owner"}\n'
    receipt = json.loads((base / "evidence/parent-2/pytest-root-cleanup.json").read_text())
    assert receipt["removed"] and not receipt["unregistered"]


@pytest.mark.parametrize("name", ["python", "python3"])
def test_python_entrypoint_keeps_venv_spelling_for_site_guards(tmp_path, monkeypatch, name):
    state = tmp_path / "state"
    state.mkdir()
    python = tmp_path / "private venv" / "bin/python"
    monkeypatch.setattr(proof.shutil, "which", lambda utility, **kwargs: "/never-run/" + utility)
    tools, _ = proof.make_tools(state, python, "/never-run/tmux", tmp_path)
    entry = tools / name
    assert not entry.is_symlink(), "an external symlink bypasses the venv's site guards"
    lines = entry.read_text().splitlines()
    assert lines[0] == "#!/bin/bash"
    assert shlex.split(lines[1]) == ["exec", str(python), "$@"]
    assert stat.S_IMODE(entry.stat().st_mode) == 0o755
