"""Real private-socket server; only the native Tailscale CLI is a synthetic stand-in."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from claudlobby.__main__ import main
from claudlobby.plane.owner_access import OwnerAccess, PrincipalRef
from claudlobby.plane.owner_browser import COOKIE_NAME
from claudlobby.plane.owner_server import private_listener
from tests.conftest import constructed_env
from tests.test_plane_two_fleets import _seed


@pytest.fixture
def socket_path():
    # AF_UNIX path limits are much shorter than prepared export / pytest paths.
    # This owned private directory contains only the disposable socket.
    with tempfile.TemporaryDirectory(prefix="cl-owner-", dir="/tmp") as directory:
        yield Path(directory) / "owner.sock"


def test_private_listener_permissions_and_owned_cleanup(socket_path):
    with private_listener(socket_path) as listener:
        assert listener.family == socket.AF_UNIX
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(socket_path))
            peer, _ = listener.accept()
            peer.close()
    assert not socket_path.exists()


def test_private_listener_preserves_existing_and_replacement_paths(socket_path):
    socket_path.write_text("foreign path")
    with pytest.raises(ValueError, match="exists"):
        with private_listener(socket_path):
            pytest.fail("clobbered an existing path")
    assert socket_path.read_text() == "foreign path"
    socket_path.unlink()
    with private_listener(socket_path):
        socket_path.unlink()
        socket_path.write_text("replacement")
    assert socket_path.read_text() == "replacement"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770])
def test_private_listener_rejects_accessible_parent(socket_path, mode):
    socket_path.parent.chmod(mode)
    with pytest.raises(ValueError, match="owner-only"):
        with private_listener(socket_path):
            pytest.fail("opened accessible listener")
    assert not socket_path.exists()


def test_private_listener_rejects_symlink_parent_and_relative_path(socket_path):
    link = socket_path.parent / "alias"
    link.symlink_to(socket_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="owner-only"):
        with private_listener(link / "owner.sock"):
            pytest.fail("followed final parent symlink")
    with pytest.raises(ValueError, match="absolute"):
        with private_listener(Path("owner.sock")):
            pytest.fail("accepted relative path")


def test_private_listener_failed_bind_does_not_remove_unowned_path(socket_path, monkeypatch):
    class FailedSocket:
        def bind(self, path):
            Path(path).write_text("raced before bind")
            raise OSError("bind refused")
        def close(self):
            pass
    monkeypatch.setattr(socket, "socket", lambda *a: FailedSocket())
    with pytest.raises(OSError, match="bind refused"):
        with private_listener(socket_path):
            pytest.fail("bound failed socket")
    assert socket_path.read_text() == "raced before bind"


def test_cli_serve_requires_explicit_configuration_and_refuses_json(tmp_path):
    assert main(["--json", "--root", str(tmp_path), "host", "owner", "serve",
                 "--origin", "https://plane.example.test", "--tailscale", "/not/a/binary"]) == 2
    assert not list(tmp_path.iterdir())


def test_missing_server_dependency_has_actionable_cli_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert main(["--root", str(tmp_path), "host", "owner", "serve",
                 "--origin", "https://plane.example.test", "--tailscale", "/not/a/binary"]) == 6
    captured = capsys.readouterr()
    assert "[plane-ui]" in captured.err
    assert "Traceback" not in captured.err
    assert not list(tmp_path.iterdir())


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("plane.example.test", timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


@contextmanager
def running_server(root, socket_path, binary):
    with (root / "server.log").open("w+") as log:
        process = subprocess.Popen([sys.executable, "-m", "claudlobby", "--root", str(root),
            "host", "owner", "serve", "--socket", str(socket_path),
            "--origin", "https://plane.example.test", "--tailscale", str(binary)],
            env=constructed_env(), stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            log.flush()
            assert process.poll() is None and socket_path.exists(), (root / "server.log").read_text()
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("owner server did not shut down")
        assert process.returncode == 0, (root / "server.log").read_text()
        assert not socket_path.exists()


def request(path, route, *, action=False, cookie=None, headers=None):
    values = {"Host": "plane.example.test", "X-Forwarded-For": "100.64.0.42"}
    if action:
        values.update({"Origin": "https://plane.example.test", "Content-Type": "application/json",
                       "X-Claudlobby-Owner": "1"})
    if cookie:
        values["Cookie"] = cookie
    values.update(headers or {})
    connection = UnixHTTP(path)
    try:
        connection.request("POST" if action else "GET", route,
                           body=b"{}" if action else None, headers=values)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_foreground_cli_pair_read_revoke_and_restart_over_real_unix_socket(tmp_path, socket_path):
    _seed(tmp_path)
    from claudlobby.plane.owner_source import bind_source
    bind_source(tmp_path)
    store = OwnerAccess.initialize(tmp_path)
    # Shape grounded in an installed native WhoIs capture; every value here is
    # synthetic. No real daemon, proxy configuration or external HTTPS is used.
    identity = {"Node": {"User": 1234, "Addresses": ["100.64.0.42/32"]},
                "UserProfile": {"ID": 1234, "LoginName": "example@example.test"}, "CapMap": {}}
    binary = tmp_path / "synthetic-tailscale"
    binary.write_text("#!/bin/sh\ncase \"$1 $2\" in\n"
        "'debug prefs') printf '%s\\n' '{\"ControlURL\":\"https://controlplane.tailscale.com\"}';;\n"
        "'whois --json') printf '%s\\n' '" + json.dumps(identity) + "';;\n*) exit 1;;\nesac\n")
    binary.chmod(0o700)
    principal = PrincipalRef("tailscale:controlplane.tailscale.com", "1234")
    with running_server(tmp_path, socket_path, binary):
        assert request(socket_path, "/owner")[0] == 200
        assert request(socket_path, "/api/summary")[0] == 403
        code, _, body = request(socket_path, "/api/owner/pair", action=True)
        assert code == 202, body
        grant = store.confirm_pairing(json.loads(body)["challenge"], expected_principal=principal)
        code, headers, body = request(socket_path, "/api/owner/login", action=True)
        assert code == 200, body
        assert "Secure" in headers["set-cookie"] and "HttpOnly" in headers["set-cookie"]
        cookie = headers["set-cookie"].split(";", 1)[0]
        assert cookie.startswith(COOKIE_NAME + "=")
        assert request(socket_path, "/api/summary", cookie=cookie)[0] == 200
        assert request(socket_path, "/api/summary", cookie=cookie,
                       headers={"X-Forwarded-For": "100.64.0.43"})[0] == 403
        assert request(socket_path, "/api/summary", cookie=cookie,
                       headers={"Tailscale-Funnel-Request": "?1"})[0] == 403
    with running_server(tmp_path, socket_path, binary):
        assert request(socket_path, "/api/summary", cookie=cookie)[0] == 200
        store.revoke_owner(expected_revision=grant.revision)
        assert request(socket_path, "/api/summary", cookie=cookie)[0] == 403
