"""Live permission-file installation exposes complete old/new JSON only."""

import json
import os
from pathlib import Path
import stat

import pytest

from claudlobby import composer
from claudlobby.paths import Paths
from tests.conftest import load_test_fleet


@pytest.fixture
def target(fleet_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "absent.sock"))
    fleet = load_test_fleet(fleet_dir)
    bot = fleet.bots["lead"]
    bot.telegram.handle = ""
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    path = paths.bot_runtime(bot.bot_id) / ".claude/settings.local.json"
    path.parent.mkdir(parents=True)
    old = {"permissions": {"allow": ["session-added-grant"], "deny": ["old-deny"]}}
    path.write_text(json.dumps(old))
    path.chmod(0o640)
    def compose():
        composer.compose_bot(bot, fleet, paths, log=lambda _: None, cascade={})
    return path, old, compose


def _observe_write(monkeypatch, path, *, fail=False):
    """Intercept both old Path writer and candidate fd writer at the same seam."""
    observed = []
    original_open, original_fdopen = Path.open, os.fdopen
    class Writer:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, body):
            observed.append(path.read_bytes())
            if fail:
                self.stream.write(body[:9])
                self.stream.flush()
                raise OSError("injected interrupted write")
            return self.stream.write(body)
    def path_open(self, mode="r", *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        return Writer(stream) if self == path and "w" in mode else stream
    def fd_open(fd, mode="r", *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        return Writer(stream) if "w" in mode else stream
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(os, "fdopen", fd_open)
    return observed


def test_real_compose_reader_sees_old_until_complete_replacement(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    seen = _observe_write(monkeypatch, path)
    compose()
    assert seen == [before]
    new = json.loads(path.read_text())
    assert new != old
    assert "session-added-grant" not in new["permissions"]["allow"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.local.json", "skills"]


def test_interrupted_write_preserves_bytes_mode_and_cleans_temp(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    _observe_write(monkeypatch, path, fail=True)
    with pytest.raises(OSError, match="injected interrupted write"):
        compose()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_serialization_failure_preserves_existing_file(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    monkeypatch.setattr(composer, "compose_settings_local", lambda *a: {"bad": object()})
    with pytest.raises(TypeError):
        compose()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_replace_failure_preserves_existing_file(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    def replace(src, dst):
        assert Path(src).parent == path.parent
        assert Path(dst) == path
        assert json.loads(Path(src).read_text()) != old
        assert Path(src).stat().st_mode & 0o777 == 0o640
        raise OSError("injected replace failure")
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="injected replace failure"):
        compose()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_new_settings_have_explicit_private_mode(target):
    path, old, compose = target
    path.unlink()
    compose()
    assert json.loads(path.read_text())["permissions"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_permission_failure_preserves_old_document(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    def fail_mode(*args):
        raise OSError("injected mode failure")
    monkeypatch.setattr(os, "fchmod", fail_mode)
    with pytest.raises(OSError, match="injected mode failure"):
        compose()
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_parallel_writers_use_unique_temps_and_readers_see_complete_objects(target, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    path, old, compose = target
    before = path.stat()
    values = [old, {"permissions": {"deny": ["a" * 100_000]}},
              {"permissions": {"allow": ["b" * 100_000]}}]
    barrier = threading.Barrier(2, timeout=10)
    ready, stop = threading.Event(), threading.Event()
    original_replace = os.replace
    temporary_paths = []
    def replace(src, dst):
        temporary_paths.append(Path(src))
        assert Path(src).parent == path.parent
        assert Path(dst) == path
        assert json.loads(Path(src).read_text()) in values[1:]
        barrier.wait()  # Both writers hold their own complete temp file.
        original_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    def reader():
        count = 0
        while not stop.is_set():
            assert json.loads(path.read_text()) in values
            count += 1
            ready.set()
        return count
    def writer(value):
        for _ in range(10):
            composer._atomic_write_settings(path, value)
    with ThreadPoolExecutor(max_workers=3) as pool:
        reading = pool.submit(reader)
        assert ready.wait(10), "reader did not start"
        writing = [pool.submit(writer, value) for value in values[1:]]
        try:
            for future in writing:
                future.result(timeout=20)
        finally:
            stop.set()
        assert reading.result(timeout=10) > 0
    assert len(temporary_paths) == len(set(temporary_paths)) == 20
    assert all(not p.exists() for p in temporary_paths)
    assert json.loads(path.read_text()) in values[1:]
    after = path.stat()
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode))


def test_open_failure_closes_descriptor_and_cleans_temp(target, monkeypatch):
    path, old, compose = target
    before = path.read_bytes()
    descriptors = []
    def fail_open(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("injected open failure")
    monkeypatch.setattr(os, "fdopen", fail_open)
    with pytest.raises(OSError, match="injected open failure"):
        compose()
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]
