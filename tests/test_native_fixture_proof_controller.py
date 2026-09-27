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


def inert_parent(monkeypatch, tmp_path):
    """Model native answers and socket stat modes; never allocate a socket/PID."""
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    registry = tmp_path / "pytest-root.json"
    register(root, registry)
    attempts, modeled = [], set()
    original_stat = os.stat

    def socket_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        # Placeholder regular files stand in for the native inode type only.
        if info.st_ino in modeled:
            fields = list(info)
            fields[0] = stat.S_IFSOCK | stat.S_IMODE(info.st_mode)
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "stat", socket_stat)
    original_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda path, **kw: socket_stat(path, follow_symlinks=False, **kw))
    for index in range(2):
        directory = root / f"p/test_pulse_completes_with_no_e{index}/root/tmux"
        socket = directory / f"tmux-{os.getuid()}" / "pulse610"
        socket.parent.mkdir(parents=True)
        socket.write_text("modeled inert socket, not a real endpoint")
        modeled.add(original_lstat(socket).st_ino)
        for name in ("pulse610", "pulse610-none"):
            attempts.append({"socket_dir": str(directory), "socket_name": name})
    calls = []

    def native(args, **kwargs):
        calls.append(args)
        assert args[1] == "-S" and args[3] == "list-panes", "eligible inert sockets must never be killed"
        socket = Path(args[2])
        diagnostic = (f"no server running on {socket}" if socket.exists() else
                      f"error connecting to {socket} (No such file or directory)")
        return subprocess.CompletedProcess(args, 1, "", diagnostic + "\n")

    monkeypatch.setattr(proof.subprocess, "run", native)
    monkeypatch.setattr(proof, "snapshot", lambda: [])
    endpoints = proof.inspect_endpoints("/native/tmux", attempts, {})
    residue = [str(Path(a['socket_dir']) / f'tmux-{os.getuid()}' / 'pulse610')
               for a in attempts if a['socket_name'] == 'pulse610']
    records = [{"kind": kind, "pid": 700 + i, **a}
               for i, (a, kind) in enumerate((a, k) for a in attempts
               if a['socket_name'] == 'pulse610' for k in ('#{pid}', '#{pane_pid}'))]
    result = {"label": "parent-2", "source_commit": proof.PARENT, "rc": 0,
              "timed_out": False, "valid_completed": True, "forbidden_calls": False,
              "cases": [{"node": n, "status": "passed", "detail": ""}
                        for n in sorted(proof.expected_nodes('parent', 2))],
              "cleanup": {"records": records, "owned_groups": [12345],
                  "survivors_before_emergency_cleanup": [],
                  "attempted_endpoints_before_emergency_cleanup": endpoints,
                  "socket_residue": residue}}
    return root, registry, attempts, result, calls


def test_historical_inert_parent_retains_red_and_requires_final_proof(tmp_path, monkeypatch):
    root, registry, attempts, result, calls = inert_parent(monkeypatch, tmp_path)
    plan = proof.historical_parent_socket_plan(result, root, registry)
    assert plan is not None
    assert not proof.clean(result)
    assert not proof.expected_parent_resource_leak(result)
    proof.retire_parent_sockets(result, plan, root, registry, '/native/tmux', attempts, {})
    assert not proof.clean(result), "raw historical cleanup RED must survive safe removal"
    assert not proof.expected_parent_resource_leak(result), "post-root evidence is still missing"
    assert all(not Path(p).exists() for p in result['cleanup']['socket_residue'])
    # Model the actual enclosing context removal, then exercise the real final door.
    import shutil
    registry.unlink()
    shutil.rmtree(root)
    proof.finish_parent_resource_proof(result, root, registry, '/native/tmux', attempts, {})
    assert proof.expected_parent_resource_leak(result)
    assert not proof.clean(result)
    assert len(calls) == 16  # four complete independent inventories
    assert result['expected_parent_resource_leak']['raw_clean'] is False
    assert result['expected_parent_resource_leak']['kind'] == 'historical-parent-dead-socket-inodes'


@pytest.mark.parametrize('defect', [
    'candidate', 'mutant', 'other-parent', 'other-source', 'missing-source', 'timeout',
    'invalid', 'forbidden', 'rc', 'case-missing', 'case-duplicate', 'case-failed',
    'survivor', 'extra-endpoint', 'missing-endpoint', 'live', 'invalid-query',
    'denied', 'unknown', 'extra-stdout', 'missing-inode', 'replaced-inode',
    'wrong-uid', 'wrong-device', 'regular', 'symlink', 'extra-socket',
    'missing-socket', 'none-socket', 'root-mode', 'root-inode', 'missing-registry',
    'missing-record', 'extra-record',
])
def test_historical_exception_refuses_every_unproved_boundary(tmp_path, monkeypatch, defect):
    root, registry, attempts, result, calls = inert_parent(monkeypatch, tmp_path)
    endpoint = result['cleanup']['attempted_endpoints_before_emergency_cleanup'][0]
    identity = endpoint.get('identity', {})
    if defect == 'candidate': result['label'] = 'candidate-2'
    elif defect == 'mutant': result['label'] = 'long-socket'
    elif defect == 'other-parent': result['label'] = 'parent-1'
    elif defect == 'other-source': result['source_commit'] = proof.CANDIDATE
    elif defect == 'missing-source': result.pop('source_commit')
    elif defect == 'timeout': result['timed_out'] = True
    elif defect == 'invalid': result['valid_completed'] = False
    elif defect == 'forbidden': result['forbidden_calls'] = True
    elif defect == 'rc': result['rc'] = 1
    elif defect == 'case-missing': result['cases'].pop()
    elif defect == 'case-duplicate': result['cases'][0] = result['cases'][1]
    elif defect == 'case-failed': result['cases'][0]['status'] = 'failure'
    elif defect == 'survivor': result['cleanup']['survivors_before_emergency_cleanup'] = [{'pid': 700}]
    elif defect == 'extra-endpoint': result['cleanup']['attempted_endpoints_before_emergency_cleanup'].append(dict(endpoint))
    elif defect == 'missing-endpoint': result['cleanup']['attempted_endpoints_before_emergency_cleanup'].pop()
    elif defect == 'live': endpoint['pids'] = [700]
    elif defect == 'invalid-query': endpoint['query_invalid'] = True
    elif defect == 'denied': endpoint['stderr'] = 'Permission denied'
    elif defect == 'unknown': endpoint['stderr'] = 'unknown failure'
    elif defect == 'extra-stdout': endpoint['stdout'] = 'surprise'
    elif defect == 'missing-inode': endpoint.pop('identity', None)
    elif defect == 'replaced-inode': identity['inode'] += 1
    elif defect == 'wrong-uid': identity['uid'] += 1
    elif defect == 'wrong-device': identity['device'] += 1
    elif defect == 'regular': identity['mode'] = stat.S_IFREG | 0o600
    elif defect == 'symlink':
        path = Path(result['cleanup']['socket_residue'][0]); path.unlink(); path.symlink_to(registry)
    elif defect == 'extra-socket': result['cleanup']['socket_residue'].append(str(root / 'extra'))
    elif defect == 'missing-socket': Path(result['cleanup']['socket_residue'][0]).unlink()
    elif defect == 'none-socket': result['cleanup']['attempted_endpoints_before_emergency_cleanup'][1]['socket_exists'] = True
    elif defect == 'root-mode': root.chmod(0o755)
    elif defect == 'root-inode':
        record = json.loads(registry.read_text()); record['inode'] += 1; proof.write_json(registry, record)
    elif defect == 'missing-registry': registry.unlink()
    elif defect == 'missing-record': result['cleanup']['records'].pop()
    elif defect == 'extra-record': result['cleanup']['records'].append({'pid': 123, 'kind': 'unexpected'})
    assert proof.historical_parent_socket_plan(result, root, registry) is None
    assert not proof.expected_parent_resource_leak(result)
    assert len(calls) == 4, 'classification must never repair or query a native resource'


@pytest.mark.parametrize('stage', ['before-unlink', 'after-unlink', 'after-root'])
@pytest.mark.parametrize('defect', ['live-pid', 'live-group', 'query-invalid'])
def test_independent_cleanup_observations_cannot_be_skipped(tmp_path, monkeypatch, stage, defect):
    root, registry, attempts, result, calls = inert_parent(monkeypatch, tmp_path)
    plan = proof.historical_parent_socket_plan(result, root, registry)
    native = proof.subprocess.run
    snapshots = 0
    def observe():
        nonlocal snapshots
        snapshots += 1
        if snapshots == {'before-unlink': 1, 'after-unlink': 2, 'after-root': 3}[stage]:
            if defect == 'live-pid': return [{'pid': 700, 'pgid': 999}]
            if defect == 'live-group': return [{'pid': 999, 'pgid': 12345}]
        return []
    def query(args, **kw):
        number = (len(calls) - 4) // 4
        r = native(args, **kw)
        if number == {'before-unlink': 0, 'after-unlink': 1, 'after-root': 2}[stage] and defect == 'query-invalid':
            return subprocess.CompletedProcess(args, 1, '', 'unknown query failure')
        return r
    monkeypatch.setattr(proof, 'snapshot', observe)
    monkeypatch.setattr(proof.subprocess, 'run', query)
    proof.retire_parent_sockets(result, plan, root, registry, '/native/tmux', attempts, {})
    import shutil
    registry.unlink(); shutil.rmtree(root)
    proof.finish_parent_resource_proof(result, root, registry, '/native/tmux', attempts, {})
    assert not proof.expected_parent_resource_leak(result)
    assert not proof.clean(result)


@pytest.mark.parametrize('defect', ['inode', 'ancestor-symlink', 'root-registration'])
def test_unlink_rechecks_identity_after_absence_queries(tmp_path, monkeypatch, defect):
    root, registry, attempts, result, calls = inert_parent(monkeypatch, tmp_path)
    plan = proof.historical_parent_socket_plan(result, root, registry)
    native = proof.subprocess.run
    socket = Path(result['cleanup']['socket_residue'][0])
    def query(args, **kw):
        r = native(args, **kw)
        if len(calls) == 8:
            if defect == 'inode':
                replacement = socket.with_name('replacement'); replacement.write_text('other owner'); replacement.replace(socket)
            elif defect == 'ancestor-symlink':
                parent = socket.parent; target = parent.with_name('moved'); parent.rename(target); parent.symlink_to(target, target_is_directory=True)
            else: registry.unlink()
        return r
    monkeypatch.setattr(proof.subprocess, 'run', query)
    proof.retire_parent_sockets(result, plan, root, registry, '/native/tmux', attempts, {})
    assert socket.exists(), 'replacement resource must survive refused explicit unlink'
    assert not proof.expected_parent_resource_leak(result)


def test_candidate_residue_never_uses_historical_acceptance(tmp_path, monkeypatch):
    root, registry, attempts, result, calls = inert_parent(monkeypatch, tmp_path)
    plan = proof.historical_parent_socket_plan(result, root, registry)
    proof.retire_parent_sockets(result, plan, root, registry, '/native/tmux', attempts, {})
    import shutil
    registry.unlink(); shutil.rmtree(root)
    proof.finish_parent_resource_proof(result, root, registry, '/native/tmux', attempts, {})
    assert proof.expected_parent_resource_leak(result)
    result['label'] = 'candidate-2'
    assert not proof.expected_parent_resource_leak(result)
    assert not proof.cleanup_accepted(result), 'candidate cleanup must stay strictly clean'


def modeled_pulse_arm(monkeypatch, tmp_path, label='parent-2'):
    """Exercise run_arm end to end; model only native pytest/process/stat answers."""
    base, source, _, _, _ = stub_arm(monkeypatch, tmp_path)
    calls, modeled = [], set()
    original_stat = os.stat
    def socket_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if info.st_ino in modeled:
            fields = list(info); fields[0] = stat.S_IFSOCK | stat.S_IMODE(info.st_mode)
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(os, 'stat', socket_stat)
    monkeypatch.setattr(os, 'lstat', lambda path, **kw: socket_stat(path, follow_symlinks=False, **kw))
    def pytest_main(args):
        from xml.etree import ElementTree as ET
        basetemp = Path(next(x.split('=', 1)[1] for x in args if x.startswith('--basetemp=')))
        state = base / 'runs' / label
        for index in range(2):
            directory = basetemp / f'test_pulse_completes_with_no_e{index}/root/tmux'
            socket = directory / f'tmux-{os.getuid()}' / 'pulse610'
            socket.parent.mkdir(parents=True); socket.write_text('modeled socket')
            modeled.add(original_stat(socket).st_ino)
            for name in ('pulse610', 'pulse610-none'):
                proof.append(state / 'tmux-attempts.jsonl', {'socket_dir': str(directory), 'socket_name': name})
            for offset, kind in enumerate(('#{pid}', '#{pane_pid}')):
                proof.append(state / 'tmux.jsonl', {'socket_dir': str(directory), 'socket_name': 'pulse610',
                                                  'pid': 700 + index * 2 + offset, 'kind': kind})
        xml = Path(next(x.split('=', 1)[1] for x in args if x.startswith('--junitxml=')))
        suite = ET.Element('testsuite')
        for node in sorted(proof.expected_nodes('parent', 2)):
            classname, name = node.rsplit('::', 1)
            ET.SubElement(suite, 'testcase', classname=classname, name=name)
        ET.ElementTree(suite).write(xml)
        return 0
    def native(args, **kwargs):
        calls.append(args)
        socket = Path(args[2])
        assert args[1] == '-S'
        diagnostic = (f'no server running on {socket}' if socket.exists() else
                      f'error connecting to {socket} (No such file or directory)')
        return subprocess.CompletedProcess(args, 1, '', diagnostic + '\n')
    monkeypatch.setattr(pytest, 'main', pytest_main)
    monkeypatch.setattr(proof.subprocess, 'run', native)
    return base, source, calls


def test_real_arm_reports_expected_historical_red_only_after_final_cleanup(tmp_path, monkeypatch):
    import inspect
    base, source, calls = modeled_pulse_arm(monkeypatch, tmp_path)
    # Keep this regression executable on exact839, which had no source-pin kwarg.
    options = {'source_commit': proof.PARENT} if 'source_commit' in inspect.signature(proof.run_arm).parameters else {}
    result = proof.run_arm('parent-2', source, Path('/unused/python'), base, [proof.MODULES[2]], '/native/tmux', **options)
    assert result['rc'] == 0 and len(result['cases']) == 4
    assert not proof.clean(result), 'the original historical cleanup RED is retained'
    assert result.get('expected_parent_resource_leak'), 'historical dead sockets need explicit independently verified classification'
    assert proof.expected_parent_resource_leak(result)
    assert proof.cleanup_accepted(result)
    assert len(calls) == 16 and all(c[3] == 'list-panes' for c in calls)
    evidence = base / 'evidence/parent-2'
    assert json.loads((evidence / 'result.json').read_text()) == result
    raw = json.loads((evidence / 'pre-cleanup-result.json').read_text())
    assert not proof.clean(raw) and 'expected_parent_resource_leak' not in raw
    assert json.loads((evidence / 'pytest-root-cleanup.json').read_text())['removed']


def test_real_candidate_arm_keeps_generic_emergency_cleanup_and_refuses_residue(tmp_path, monkeypatch):
    import inspect
    base, source, calls = modeled_pulse_arm(monkeypatch, tmp_path, 'candidate-2')
    options = {'source_commit': proof.CANDIDATE} if 'source_commit' in inspect.signature(proof.run_arm).parameters else {}
    result = proof.run_arm('candidate-2', source, Path('/unused/python'), base, [proof.MODULES[2]], '/native/tmux', **options)
    assert not proof.clean(result)
    assert 'expected_parent_resource_leak' not in result
    assert len([c for c in calls if c[3] == 'kill-server']) == 2
    if hasattr(proof, 'cleanup_accepted'):
        assert not proof.cleanup_accepted(result)
    assert result['cleanup']['socket_residue'], 'subsequent root removal cannot erase raw candidate failure'


def test_replaced_root_is_not_removed_by_context_cleanup(tmp_path, monkeypatch):
    root = tmp_path / 'allocation'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(proof.tempfile, 'mkdtemp', lambda **kw: str(root))
    with pytest.raises(RuntimeError, match='replaced pytest root'):
        with proof.private_pytest_directory() as allocated:
            assert allocated == root
            root.rename(tmp_path / 'original')
            root.mkdir(mode=0o700)
            (root / 'other-owner').write_text('preserve')
    assert (root / 'other-owner').read_text() == 'preserve'


@pytest.mark.parametrize('defect', ['root-not-removed', 'root-still-registered',
    'root-invalid-before-removal', 'post-unlink-residue', 'lost-unlink-receipt',
    'missing-final-query', 'raw-clean-forged', 'wrong-kind', 'emergency-used',
    'missing-checked-pid', 'missing-checked-group'])
def test_typed_resource_proof_requires_every_final_receipt(tmp_path, monkeypatch, defect):
    root, registry, attempts, result, _ = inert_parent(monkeypatch, tmp_path)
    plan = proof.historical_parent_socket_plan(result, root, registry)
    proof.retire_parent_sockets(result, plan, root, registry, '/native/tmux', attempts, {})
    import shutil
    registry.unlink(); shutil.rmtree(root)
    proof.finish_parent_resource_proof(result, root, registry, '/native/tmux', attempts, {})
    assert proof.expected_parent_resource_leak(result)
    receipt = result['expected_parent_resource_leak']
    if defect == 'root-not-removed': receipt['root_removed'] = False
    elif defect == 'root-still-registered': receipt['root_unregistered'] = False
    elif defect == 'root-invalid-before-removal': receipt['root_valid_after_unlink'] = False
    elif defect == 'post-unlink-residue': receipt['socket_inventory_after_unlink'] = ['unexpected']
    elif defect == 'lost-unlink-receipt': receipt['unlinked'].pop()
    elif defect == 'missing-final-query': receipt['after_root_cleanup']['endpoints'].pop()
    elif defect == 'raw-clean-forged': receipt['raw_clean'] = True
    elif defect == 'wrong-kind': receipt['kind'] = 'generic cleanup waiver'
    elif defect == 'emergency-used': receipt['emergency_cleanup_skipped'] = False
    elif defect == 'missing-checked-pid': receipt['after_root_cleanup']['checked_pids'].pop()
    elif defect == 'missing-checked-group': receipt['after_root_cleanup']['checked_groups'] = []
    assert not proof.expected_parent_resource_leak(result)
    assert not proof.cleanup_accepted(result)


def test_incomplete_socket_inventory_is_not_absence(tmp_path, monkeypatch):
    root, registry, _, result, _ = inert_parent(monkeypatch, tmp_path)
    def unreadable(*args, **kwargs):
        kwargs['onerror'](PermissionError('modeled inaccessible owned subtree'))
        return iter(())
    monkeypatch.setattr(proof.os, 'walk', unreadable)
    assert proof.historical_parent_socket_plan(result, root, registry) is None
    assert not proof.expected_parent_resource_leak(result)
