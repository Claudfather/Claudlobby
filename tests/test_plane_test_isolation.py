"""Behavioral isolation pins: silence is useful only if scratch recording works."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

from tests.conftest import constructed_env, _scrubbed_env, read_fleet_events
from tests.test_plane_events_door import _serving

REPO = Path(__file__).resolve().parents[1]
SHIM = REPO / "claudlobby/_runtime_scripts" / "plane-emit.sh"
MSG_ID = "msg_" + "1" * 32
BATCH = json.dumps({"events": [{
    "event_type": "communication", "emitter": "isolation-test", "fleet": "scratch",
    "payload": {"msg_id": MSG_ID, "sender": "bot:scratch/test", "message_class": "notice"},
}]})


def _emit(env):
    return subprocess.run(["bash", str(SHIM)], input=BATCH, env=env,
                          capture_output=True, text=True, timeout=30)


def test_negative_event_read_refuses_an_unserved_channel(tmp_path):
    with pytest.raises(AssertionError, match="database is absent"):
        read_fleet_events(tmp_path)
    staged = tmp_path / "state/plane/staged"
    staged.mkdir(parents=True)
    (staged / "pending.batch").write_bytes(b"uncommitted")
    with pytest.raises(AssertionError, match="staged but not committed"):
        read_fleet_events(tmp_path)


def test_implicit_root_discovery_refuses_the_checkout(monkeypatch):
    from claudlobby.paths import Paths
    from tests.package_fixtures import source_package

    monkeypatch.chdir(REPO)
    with pytest.raises(AssertionError, match="cwd discovery reached the source checkout"):
        Paths.detect(package=source_package())


def _artifacts(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.name in ("plane.db", "plane.db-wal", "plane.db-shm")
                  or "spool" in p.parts)


@pytest.fixture
def sentinel(tmp_path, scratch_plane_env):
    """An actual listening socket and CLI recorder, both private to this test."""
    root = tmp_path / "ambient"
    root.mkdir()
    calls = root / "cli-called"
    cli = root / "trap-cli"
    cli.write_text(f"#!/bin/sh\nprintf called >> '{calls}'\nexit 0\n")
    cli.chmod(0o755)
    path = scratch_plane_env.socket_dir() / "s"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    listener.settimeout(0.05)
    seen = []
    stop = threading.Event()

    def receive():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                seen.append(True)
                conn.settimeout(1)
                try:
                    conn.recv(65536)
                    conn.sendall(b'{"status":"unavailable"}\n')
                except (OSError, TimeoutError):
                    pass

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    yield {"root": root, "socket": path, "cli": cli, "calls": calls, "seen": seen}
    stop.set()
    thread.join(timeout=2)
    listener.close()
    assert not thread.is_alive()


def _assert_untouched(sentinel):
    assert sentinel["seen"] == [], "ambient socket received a request"
    assert not sentinel["calls"].exists(), "ambient CLI was invoked"
    assert _artifacts(sentinel["root"]) == []


@pytest.mark.parametrize("builder", [constructed_env, _scrubbed_env])
@pytest.mark.parametrize("explicit_root", [False, True])
def test_child_builders_silence_real_shim(builder, explicit_root, sentinel, monkeypatch):
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(sentinel["root"]))
    monkeypatch.setenv("PLANE_SOCKET", str(sentinel["socket"]))
    monkeypatch.setenv("PLANE_EMIT_CLI", str(sentinel["cli"]))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "0")
    env = builder()
    assert "PLANE_SOCKET" not in env and "PLANE_EMIT_CLI" not in env
    assert "CLAUDLOBBY_ROOT" not in env
    # The trap transports are explicit here: this measures the guard, not a
    # missing-root refusal or the removal of an inherited socket alone.
    env.update(PLANE_SOCKET=str(sentinel["socket"]), PLANE_EMIT_CLI=str(sentinel["cli"]))
    if explicit_root:
        env["CLAUDLOBBY_ROOT"] = str(sentinel["root"])
    before = _artifacts(REPO)
    result = _emit(env)
    assert result.returncode == 0, result.stderr
    _assert_untouched(sentinel)
    assert result.stdout == result.stderr == ""
    assert _artifacts(REPO) == before


def test_builder_overrides_are_explicit_and_coerced(tmp_path):
    for builder in (constructed_env, _scrubbed_env):
        env = builder(PLANE_EMIT_DISABLED=0, CLAUDLOBBY_ROOT=tmp_path)
        assert env["PLANE_EMIT_DISABLED"] == "0"
        assert env["CLAUDLOBBY_ROOT"] == str(tmp_path)


@pytest.mark.parametrize("initial_guard", ["0", None])
def test_nested_pytest_guards_session_and_function(initial_guard, sentinel, tmp_path):
    """Use the real conftest, with a poisoned parent rather than inherited 1."""
    with tempfile.TemporaryDirectory(prefix="isolation-probe-", dir=REPO / "tests") as directory:
        probe = Path(directory)
        (probe / "conftest.py").write_text('''import os, subprocess
import pytest
from pathlib import Path

@pytest.fixture(scope="session")
def incidental_session(_isolate_plane_session):
    assert "PYTHONPATH" not in os.environ
    result = subprocess.run(["bash", os.environ["ISOLATION_SHIM"]],
                            input=os.environ["ISOLATION_BATCH"], text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    assert os.environ["PLANE_EMIT_DISABLED"] == "1"
    assert "CLAUDLOBBY_ROOT" not in os.environ
    assert "PLANE_SOCKET" not in os.environ and "PLANE_EMIT_CLI" not in os.environ
''')
        (probe / "test_probe.py").write_text('''import os, subprocess

def test_session(incidental_session, tmp_path_factory, _isolate_claudlobby_root):
    assert os.environ["PLANE_EMIT_DISABLED"] == "1"
    base = tmp_path_factory.getbasetemp()
    assert not base.is_relative_to(_isolate_claudlobby_root)
    (base / "session-lifetime").write_text("survives")

def test_function(tmp_path, tmp_path_factory):
    assert (tmp_path_factory.getbasetemp() / "session-lifetime").read_text() == "survives"
    assert tmp_path.is_dir()
    result = subprocess.run(["bash", os.environ["ISOLATION_SHIM"]],
                            input=os.environ["ISOLATION_BATCH"], text=True,
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    assert os.environ["PLANE_EMIT_DISABLED"] == "1"
    assert "CLAUDLOBBY_ROOT" not in os.environ
    assert "PLANE_SOCKET" not in os.environ and "PLANE_EMIT_CLI" not in os.environ
''')
        env = constructed_env(HOME=tmp_path / "home", PYTHONPATH=REPO, ISOLATION_SHIM=SHIM,
                              ISOLATION_BATCH=BATCH, CLAUDLOBBY_ROOT=sentinel["root"],
                              PLANE_SOCKET=sentinel["socket"], PLANE_EMIT_CLI=sentinel["cli"])
        if initial_guard is None:
            env.pop("PLANE_EMIT_DISABLED")
        else:
            env["PLANE_EMIT_DISABLED"] = initial_guard
        before = _artifacts(REPO)
        result = subprocess.run([sys.executable, "-m", "pytest", "-v", "--override-ini=addopts=",
                                 str(probe / "test_probe.py")],
                                cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        relative = probe.relative_to(REPO)
        for name in ("test_session", "test_function"):
            assert f"{relative}/test_probe.py::{name} PASSED" in result.stdout
        assert "2 passed" in result.stdout
        _assert_untouched(sentinel)
        assert _artifacts(REPO) == before


@pytest.mark.parametrize("default_on", [False, True])
def test_owned_recording_reaches_selected_daemon(tmp_path, scratch_plane_env, sentinel, monkeypatch, default_on):
    monkeypatch.setenv("PLANE_SOCKET", str(sentinel["socket"]))
    monkeypatch.setenv("PLANE_EMIT_CLI", str(sentinel["cli"]))
    root = tmp_path / "recording"
    root.mkdir()
    stale = tmp_path / "stale-bin"
    stale.mkdir()
    (stale / "claudlobby").symlink_to(sentinel["cli"])
    with _serving(root, scratch_plane_env) as socket:
        env = constructed_env(PATH=f"{stale}:{os.environ['PATH']}",
                              **scratch_plane_env(root, socket=socket))
        assert shutil.which("claudlobby", path=env["PATH"]) == str(stale / "claudlobby")
        if default_on:
            env.pop("PLANE_EMIT_DISABLED")
        result = _emit(env)
    assert result.returncode == 0, result.stderr
    from claudlobby.plane.db import connect_ro, db_path
    assert db_path(root).is_file(), "explicitly initialized recording database disappeared"
    with connect_ro(db_path(root)) as conn:
        rows = conn.execute("SELECT msg_id, sender_alias, message_class FROM communications").fetchall()
    assert [tuple(row) for row in rows] == [(MSG_ID, "bot:scratch/test", "notice")]
    _assert_untouched(sentinel)


def test_child_home_and_config_are_private(tmp_path, _isolate_claudlobby_root):
    env = constructed_env()
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR"):
        assert Path(env[key]).is_relative_to(_isolate_claudlobby_root)
        assert not Path(env[key]).is_relative_to(tmp_path)
        assert Path(env[key]).is_dir()
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == env["HOME"]


def test_wrong_package_origin_refuses_before_collection(tmp_path):
    # A preloaded/stale package must not let cwd disguise the tree being tested.
    probe = (
        "import runpy, sys, types\n"
        "package = types.ModuleType('claudlobby')\n"
        f"package.__file__ = {str(tmp_path / 'other/claudlobby/__init__.py')!r}\n"
        "sys.modules['claudlobby'] = package\n"
        f"runpy.run_path({str(REPO / 'tests/conftest.py')!r})\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path,
                            env=constructed_env(), capture_output=True, text=True)
    assert result.returncode != 0
    assert "test package origin does not match the tree under test" in result.stderr


def test_factory_rejects_unowned_and_symlink_destinations(tmp_path, scratch_plane_env):
    with tempfile.TemporaryDirectory(prefix="plane-unowned-", dir="/tmp") as other:
        outside = Path(other)
        escape = tmp_path / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        for root in (outside, escape):
            with pytest.raises(ValueError, match="fixture-owned"):
                scratch_plane_env(root)
        with pytest.raises(ValueError, match="socket"):
            scratch_plane_env(tmp_path / "root", socket=outside / "s")
        with pytest.raises(ValueError, match="CLI"):
            scratch_plane_env(tmp_path / "root", cli=outside / "cli")
        assert list(outside.iterdir()) == []


def test_factory_uses_distinct_socket_destinations(tmp_path, scratch_plane_env):
    first = scratch_plane_env(tmp_path / "first")
    second = scratch_plane_env(tmp_path / "second")
    assert first["PLANE_SOCKET"] != second["PLANE_SOCKET"]
    assert not Path(first["PLANE_SOCKET"]).exists()
    short = scratch_plane_env.socket_dir() / "daemon.sock"
    assert scratch_plane_env(tmp_path / "first", socket=short)["PLANE_SOCKET"] == str(short.resolve())


def test_factory_refuses_an_occupied_default_socket(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    (root / "no-daemon.sock").touch()
    with pytest.raises(ValueError, match="must be absent"):
        scratch_plane_env(root)
