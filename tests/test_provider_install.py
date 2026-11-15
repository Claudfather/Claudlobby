"""No vendor execution: tiny test-only bytes exercise the real publication path."""
from __future__ import annotations

import hashlib
import http.client
import io
import os
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def install(tmp_path, monkeypatch):
    from claudlobby import provider_install as p
    home = tmp_path.resolve() / "account"
    target = home / ".local/share/claudlobby-browser/provider-bin/claude"
    target.parent.mkdir(parents=True, mode=0o700)
    target.parent.chmod(0o700)
    uid = os.getuid()
    monkeypatch.setattr(p.pwd, "getpwnam", lambda account: SimpleNamespace(
        pw_name="selected", pw_uid=uid, pw_dir=str(home)))
    data = b"reviewed tiny provider fixture\n"
    artifact = p.Artifact("linux-x64", "2.1.325", len(data), hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(p, "_artifact", lambda: artifact)
    return SimpleNamespace(p=p, home=home, target=target, uid=uid, data=data, artifact=artifact)


def prepare(f, chunks=None, **kw):
    return f.p._prepare("selected", f.uid,
        downloader=lambda *args: iter([f.data] if chunks is None else chunks), **kw)


def test_status_missing_is_read_only(install, monkeypatch):
    f = install
    f.target.parent.rmdir()
    before = sorted(str(p) for p in f.home.rglob("*"))
    monkeypatch.setattr(f.p.subprocess, "Popen", lambda *a, **k: pytest.fail("status executed a process"))
    result = f.p.status("selected", f.uid)
    assert (result.state, result.reason) == ("blocked", "unsafe_path")
    assert sorted(str(p) for p in f.home.rglob("*")) == before


def test_status_absence_is_observation_not_preparation(install):
    result = install.p.status("selected", install.uid)
    assert (result.state, result.reason) == ("missing", "not_present")
    assert list(install.target.parent.iterdir()) == []


def test_streamed_create_only_publication_and_retained_noop(install):
    f = install
    result = prepare(f, [f.data[:8], f.data[8:]])
    assert (result.state, result.reason) == ("ready", "prepared")
    assert f.target.read_bytes() == f.data
    info = f.target.stat()
    assert stat.S_IMODE(info.st_mode) == 0o500
    assert info.st_nlink == 1 and info.st_uid == f.uid
    snapshot = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    assert f.p._prepare("selected", f.uid, downloader=lambda *a: pytest.fail("downloaded again")).reason == "already_present"
    assert f.p.status("selected", f.uid).state == "ready"
    after = f.target.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == snapshot
    assert list(f.target.parent.iterdir()) == [f.target]


@pytest.mark.parametrize("data,reason", [(b"x", "artifact_mismatch"),
    (b"x" * 32, "size_limit"), (b"wrong tiny provider fixture!!\n", "artifact_mismatch")])
def test_bad_download_never_becomes_executable(install, data, reason):
    result = prepare(install, [data])
    assert (result.state, result.reason) == ("blocked", reason)
    assert list(install.target.parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["bytes", "mode", "symlink", "directory", "hardlink", "owner"])
def test_existing_foreign_or_unsafe_target_is_never_changed(install, monkeypatch, kind):
    f = install
    other = f.home / "foreign"
    other.write_bytes(b"untouched")
    if kind == "symlink": f.target.symlink_to(other)
    elif kind == "directory": f.target.mkdir()
    else:
        f.target.write_bytes(f.data if kind != "bytes" else b"foreign")
        f.target.chmod(0o700 if kind == "mode" else 0o500)
        if kind == "hardlink": os.link(f.target, f.home / "alias")
        if kind == "owner":
            real = f.p.os.fstat
            def foreign(fd):
                info = real(fd)
                if stat.S_ISREG(info.st_mode):
                    values = list(info)
                    values[4] = f.uid + 1
                    return os.stat_result(values)
                return info
            monkeypatch.setattr(f.p.os, "fstat", foreign)
    before = f.target.lstat()
    result = prepare(f)
    assert result.state == "blocked"
    after = f.target.lstat()
    assert (before.st_ino, before.st_mode, before.st_size) == (after.st_ino, after.st_mode, after.st_size)
    assert other.read_bytes() == b"untouched"


@pytest.mark.parametrize("change", ["parent_mode", "ancestor_mode", "parent_symlink", "account", "root"])
def test_account_and_unredirected_private_ancestors_are_required(install, monkeypatch, change):
    f = install
    if change == "parent_mode": f.target.parent.chmod(0o755)
    elif change == "ancestor_mode": f.target.parent.parent.chmod(0o777)
    elif change == "parent_symlink":
        actual = f.target.parent.with_name("elsewhere")
        f.target.parent.rename(actual)
        f.target.parent.symlink_to(actual)
    elif change == "account":
        monkeypatch.setattr(f.p.pwd, "getpwnam", lambda account: SimpleNamespace(
            pw_name="other", pw_uid=f.uid, pw_dir=str(f.home)))
    else: monkeypatch.setattr(f.p.os, "geteuid", lambda: 0)
    result = prepare(f)
    assert result.state == "blocked"
    assert not f.target.exists()


def test_account_database_home_ignores_home_path_proxy_and_certificate_environment(install, monkeypatch):
    for name in ("HOME", "PATH", "HTTPS_PROXY", "SSL_CERT_FILE", "PYTHONPATH"):
        monkeypatch.setenv(name, "/deliberately/not/used")
    assert prepare(install).state == "ready"
    assert install.target.read_bytes() == install.data


def test_unproven_stage_stops_missing_target_without_cleanup_or_download(install):
    f = install
    stage = f.target.parent / ".claude-stage-original"
    stage.write_bytes(b"original uncertain bytes")
    result = f.p._prepare("selected", f.uid, downloader=lambda *a: pytest.fail("retried unknown"))
    assert (result.state, result.reason) == ("blocked", "inspection_required")
    assert stage.read_bytes() == b"original uncertain bytes"


def test_supported_canonical_bytes_prove_completion_after_unknown_stage(install):
    f = install
    assert prepare(f).state == "ready"
    stage = f.target.parent / ".claude-stage-unknown"
    stage.write_bytes(b"retain original stage")
    assert f.p.status("selected", f.uid).state == "ready"
    assert stage.read_bytes() == b"retain original stage"


def test_cancellation_before_and_during_stream_cleans_only_owned_stage(install):
    f = install
    cancel = threading.Event()
    cancel.set()
    assert prepare(f, cancel=cancel).state == "cancelled"
    cancel.clear()
    def chunks(*a):
        yield f.data[:4]
        cancel.set()
        yield f.data[4:]
    result = f.p._prepare("selected", f.uid, downloader=chunks, cancel=cancel)
    assert (result.state, result.reason) == ("cancelled", "cancelled")
    assert list(f.target.parent.iterdir()) == []


def test_deadline_includes_stream_and_never_publishes(install):
    f = install
    now = [0.0]
    def chunks(*a):
        yield f.data[:4]
        now[0] = 601.0
        yield f.data[4:]
    result = f.p._prepare("selected", f.uid, downloader=chunks, clock=lambda: now[0])
    assert (result.state, result.reason) == ("blocked", "deadline")
    assert list(f.target.parent.iterdir()) == []


def test_partial_write_is_completed_but_zero_write_fails_safely(install, monkeypatch):
    f = install
    write = os.write
    monkeypatch.setattr(f.p.os, "write", lambda fd, data: write(fd, data[:3]))
    assert prepare(f).state == "ready"
    assert f.target.read_bytes() == f.data
    f.target.unlink()
    monkeypatch.setattr(f.p.os, "write", lambda fd, data: 0)
    assert prepare(f).state == "blocked"
    assert list(f.target.parent.iterdir()) == []


def test_occupied_target_race_never_overwrites(install, monkeypatch):
    f = install
    link = os.link
    def race(*a, **kw):
        f.target.write_bytes(b"racing target")
        return link(*a, **kw)
    monkeypatch.setattr(f.p.os, "link", race)
    result = prepare(f)
    assert (result.state, result.reason) == ("blocked", "target_changed")
    assert f.target.read_bytes() == b"racing target"
    assert list(f.target.parent.iterdir()) == [f.target]


def test_ancestor_replacement_during_download_stops_publication(install):
    f = install
    moved = f.target.parent.with_name("original")
    def chunks(*a):
        yield f.data[:3]
        f.target.parent.rename(moved)
        f.target.parent.mkdir(mode=0o700)
        yield f.data[3:]
    result = f.p._prepare("selected", f.uid, downloader=chunks)
    assert (result.state, result.reason) == ("blocked", "unsafe_path")
    assert list(moved.iterdir()) == []
    assert list(f.target.parent.iterdir()) == []


def test_ambiguous_publication_is_unknown_and_status_does_not_replay(install, monkeypatch):
    f = install
    link = os.link
    def uncertain(*a, **kw):
        link(*a, **kw)
        raise OSError("private exception must never escape")
    monkeypatch.setattr(f.p.os, "link", uncertain)
    result = prepare(f)
    assert (result.state, result.reason) == ("unknown", "publication_uncertain")
    assert f.p.status("selected", f.uid).state == "ready"
    assert f.target.read_bytes() == f.data


def test_cancellation_after_publication_is_unknown_until_status(install, monkeypatch):
    f = install
    cancel = threading.Event()
    link = os.link
    def published(*a, **kw):
        link(*a, **kw)
        cancel.set()
    monkeypatch.setattr(f.p.os, "link", published)
    result = prepare(f, cancel=cancel)
    assert (result.state, result.reason) == ("unknown", "publication_uncertain")
    assert f.p.status("selected", f.uid).state == "ready"


def test_cleanup_does_not_unlink_replaced_stage(install, monkeypatch):
    f = install
    original = f.home / "original-stage"
    def chunks(*a):
        stage, = f.target.parent.iterdir()
        stage.rename(original)
        stage.write_bytes(b"foreign stage")
        raise OSError("private network exception")
        yield b""  # generator, deliberately fails before any bytes
    result = f.p._prepare("selected", f.uid, downloader=chunks)
    assert result.state == "blocked"
    stage, = f.target.parent.iterdir()
    assert stage.read_bytes() == b"foreign stage"


def test_stage_collision_is_retained_without_cleanup(install, monkeypatch):
    f = install
    original = f.target.parent / ".claude-stage-fixed"
    real_open = os.open
    def collision(path, *a, **kw):
        if path == ".claude-stage-fixed":
            original.write_bytes(b"unproven existing stage")
        return real_open(path, *a, **kw)
    monkeypatch.setattr(f.p.secrets, "token_hex", lambda count: "fixed")
    monkeypatch.setattr(f.p.os, "open", collision)
    assert prepare(f).state == "blocked"
    assert original.read_bytes() == b"unproven existing stage"


def test_same_size_wrong_hash_and_mutating_installed_bytes_are_refused(install, monkeypatch):
    f = install
    f.target.write_bytes(b"x" * len(f.data))
    f.target.chmod(0o500)
    assert f.p.status("selected", f.uid).reason == "existing_file_changed"
    f.target.chmod(0o600)
    f.target.write_bytes(f.data)
    f.target.chmod(0o500)
    read = os.read
    changed = [False]
    def mutate(fd, count):
        data = read(fd, count)
        if data and not changed[0]:
            changed[0] = True
            f.target.chmod(0o600)
            f.target.write_bytes(b"x" * len(f.data))
            f.target.chmod(0o500)
        return data
    monkeypatch.setattr(f.p.os, "read", mutate)
    assert f.p.status("selected", f.uid).reason == "existing_file_changed"


def test_failed_directory_fsync_after_link_is_unknown(install, monkeypatch):
    f = install
    fsync = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("private filesystem exception")
        return fsync(fd)
    monkeypatch.setattr(f.p.os, "fsync", fail_directory)
    result = prepare(f)
    assert result.state == "unknown"
    assert f.p.status("selected", f.uid).state == "ready"


@pytest.mark.parametrize("machine,key,size,digest", [
    ("x86_64", "linux-x64", 365842960, "0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf"),
    ("amd64", "linux-x64", 365842960, "0dc4f4a9a165fc60746d7ae8489712e5fc352f1e7ad4eb22e7141bd0812eb0cf"),
    ("aarch64", "linux-arm64", 364701112, "7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67"),
    ("arm64", "linux-arm64", 364701112, "7b6e484b1989a5a14a4b354709ccc8672518a3ec5b9ccd7981fc6254327fde67")])
def test_exact_production_artifact_policy_matches_reviewed_signin(machine, key, size, digest, monkeypatch):
    from claudlobby import provider_install as p
    from claudlobby.plane.provider_signin import SUPPORTED
    monkeypatch.setattr(p.platform, "system", lambda: "Linux")
    monkeypatch.setattr(p.platform, "machine", lambda: machine)
    monkeypatch.setattr(p.platform, "freedesktop_os_release", lambda: {"ID":"ubuntu", "VERSION_ID":"24.04"})
    monkeypatch.setattr(p.os, "confstr", lambda name: "glibc 2.39")
    artifact = p._artifact()
    assert (artifact.platform, artifact.version, artifact.size, artifact.sha256) == (key, "2.1.325", size, digest)
    assert artifact.url == f"https://downloads.claude.ai/claude-code-releases/2.1.325/{key}/claude"
    assert SUPPORTED[digest] == artifact.version


@pytest.mark.parametrize("system,machine,release,libc", [
    ("Darwin", "arm64", {"ID":"ubuntu", "VERSION_ID":"24.04"}, "glibc 2.39"),
    ("Linux", "riscv64", {"ID":"ubuntu", "VERSION_ID":"24.04"}, "glibc 2.39"),
    ("Linux", "x86_64", {"ID":"ubuntu", "VERSION_ID":"22.04"}, "glibc 2.35"),
    ("Linux", "x86_64", {"ID":"debian", "VERSION_ID":"12"}, "glibc 2.36"),
    ("Linux", "x86_64", {"ID":"ubuntu", "VERSION_ID":"24.04"}, None)])
def test_unsupported_host_never_selects_an_artifact(system, machine, release, libc, monkeypatch):
    from claudlobby import provider_install as p
    monkeypatch.setattr(p.platform, "system", lambda: system)
    monkeypatch.setattr(p.platform, "machine", lambda: machine)
    monkeypatch.setattr(p.platform, "freedesktop_os_release", lambda: release)
    monkeypatch.setattr(p.os, "confstr", lambda name: libc)
    assert p.status("selected", os.getuid()).reason == "unsupported_platform"


@pytest.mark.parametrize("response,ok", [
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", True),
    (b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.invalid/\r\nContent-Length: 3\r\n\r\nabc", False),
    (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 3\r\n\r\nabc", False),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nabcd", False),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n", False),
    (b"HTTP/1.1 503 No\r\nContent-Length: 3\r\n\r\nabc", False)])
def test_download_worker_validates_actual_http_response_without_following_redirects(response, ok, monkeypatch):
    from claudlobby import provider_install as p
    received = []
    class Connection:
        def __init__(self, host, **kw):
            assert host == "downloads.claude.ai"
        def request(self, method, path, headers):
            received.append((method, path, headers))
        def getresponse(self):
            class Sock:
                def makefile(self, *a, **kw): return io.BytesIO(response)
            result = http.client.HTTPResponse(Sock())
            result.begin()
            return result
        def close(self): pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(p.ssl.SSLContext, "load_verify_locations", lambda *a, **k: None)
    output = io.BytesIO()
    success = p._worker_transfer("/claude-code-releases/2.1.325/linux-x64/claude", 3, output)
    assert success is ok
    assert output.getvalue() == (b"abc" if ok else b"")
    assert len(received) == 1


def test_owned_download_child_is_bounded_and_reaped(install):
    f = install
    children = []
    def spawn(args, **kw):
        assert args[:4] == [sys.executable, "-I", "-S", "-c"]
        assert kw["env"] == {} and kw["close_fds"] is True
        assert kw["stdin"] == subprocess.DEVNULL and kw["stderr"] == subprocess.DEVNULL
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(60)"], **kw)
        children.append(child)
        return child
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = f.p._prepare("selected", f.uid, cancel=cancel,
            downloader=lambda artifact, deadline, cancel, clock: f.p._download(
                artifact, deadline, cancel, clock, spawn=spawn))
    finally:
        timer.cancel()
    assert result.state == "cancelled"
    assert len(children) == 1 and children[0].poll() is not None
    assert list(f.target.parent.iterdir()) == []


@pytest.mark.parametrize("script,reason", [
    ("import os;os.write(1,b'x'*32)", "size_limit"),
    ("import sys;sys.stderr.write('private error');sys.exit(1)", "download_failed"),
    ("import time;time.sleep(60)", "deadline")])
def test_real_download_child_output_failure_and_deadline_are_bounded(install, script, reason):
    f = install
    children = []
    def spawn(args, **kw):
        child = subprocess.Popen([sys.executable, "-I", "-c", script], **kw)
        children.append(child)
        return child
    def download(artifact, deadline, cancel, clock):
        return f.p._download(artifact, min(deadline, clock() + 0.15), cancel, clock, spawn=spawn)
    result = f.p._prepare("selected", f.uid, downloader=download)
    assert (result.state, result.reason) == ("blocked", reason)
    assert len(children) == 1 and children[0].poll() is not None
    assert list(f.target.parent.iterdir()) == []


def test_real_isolated_worker_cannot_resolve_an_arbitrary_artifact(install):
    f = install
    # The actual isolated entry point loads the source-owned allowlist, then
    # refuses this private test-only record before any connection is possible.
    artifact = f.p.Artifact("unsupported-fixture", "2.1.325", 3, "0" * 64)
    with pytest.raises(f.p._Refused) as refused:
        list(f.p._download(artifact, f.p.time.monotonic() + 2, None, f.p.time.monotonic))
    assert refused.value.reason == "download_failed"
