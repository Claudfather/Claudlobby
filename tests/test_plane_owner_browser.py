"""Synthetic browser-session contract; this is not a Tailscale ingress test.

All identities are supplied by trusted test code and all authority is under a
disposable root. The production Plane view command still uses its old factory.
"""

import asyncio
from contextlib import closing
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from claudlobby.plane import owner_browser
from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import (
    AccessDenied, OwnerAccess, PrincipalRef, SESSION_SECONDS,
)
from claudlobby.plane.owner_browser import COOKIE_NAME, create_owner_browser_app
from tests.conftest import constructed_env
from tests.package_fixtures import source_package
from tests.test_plane_two_fleets import _Sampler, _seed


ORIGIN = "https://plane.example.test"
OWNER = PrincipalRef("test-verifier", "human-001")
OTHER = PrincipalRef("test-verifier", "human-002")


@pytest.fixture
def browser(tmp_path):
    _seed(tmp_path)
    clock = [1_800_000_000.0]
    store = OwnerAccess.initialize(tmp_path, clock=lambda: clock[0])
    identity = [OWNER]

    async def verify_principal(_scope):
        if identity[0] is None:
            raise AccessDenied("synthetic_verifier_refused")
        return identity[0]

    app = create_owner_browser_app(tmp_path, verify_principal=verify_principal,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=source_package())
    app.access._clock = lambda: clock[0]
    return app, TestClient(app, base_url=ORIGIN), store, identity, clock


def _post(client, action, *, headers=None, body=b"{}", drop=()):
    request_headers = {"Origin": ORIGIN, "X-Claudlobby-Owner": "1",
                       "Content-Type": "application/json",
                       "Sec-Fetch-Site": "same-origin"}
    for name in drop:
        request_headers.pop(name)
    request_headers.update(headers or {})
    return client.post("/api/owner/" + action, content=body, headers=request_headers)


def _pair_locally(client, store, principal=OWNER):
    requested = _post(client, "pair")
    assert requested.status_code == 202, requested.text
    assert requested.json()["state"] == "awaiting_local_approval"
    challenge = requested.json()["challenge"]
    assert challenge not in requested.headers.get("location", "")
    assert requested.json()["principal"] == {
        "namespace": principal.namespace, "subject": principal.subject,
    }
    return store.confirm_pairing(challenge, expected_principal=principal)


def _session_cookie(client):
    token = client.cookies.get(COOKIE_NAME)
    assert token is not None and len(token) == 43
    return token


async def _raw_http(app, path, *, method="POST", headers=(), chunks=None,
                    raw_headers=None, receive_override=None, before_send=None):
    """Drive one ASGI request without httpx normalizing duplicate headers/body chunks."""
    request_chunks = iter(chunks if chunks is not None else [
        {"type": "http.request", "body": b"{}", "more_body": False},
    ])
    messages = []

    async def receive():
        if receive_override is not None:
            return await receive_override()
        try:
            return next(request_chunks)
        except StopIteration:
            await asyncio.Event().wait()

    async def send(message):
        if before_send is not None:
            await before_send(message)
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "method": method, "scheme": "https", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": raw_headers if raw_headers is not None else [
                 (b"host", b"plane.example.test"),
                 (b"origin", ORIGIN.encode()),
                 (b"x-claudlobby-owner", b"1"),
                 (b"sec-fetch-site", b"same-origin"),
                 (b"content-type", b"application/json"), *headers],
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 443),
             "http_version": "1.1", "extensions": {}}
    await app(scope, receive, send)
    return messages


def _raw_start(messages):
    return next(message for message in messages if message["type"] == "http.response.start")


def _raw_headers(messages):
    return {key.lower(): value for key, value in _raw_start(messages)["headers"]}


def test_pairing_requires_separate_local_approval_then_cookie_login(browser):
    _, client, store, _, _ = browser
    assert client.get("/api/owner/status").json() == {"state": "needs_pairing"}
    assert client.get("/api/tasks").status_code == 403
    assert client.get("/style.css").status_code == 403
    assert _post(client, "login").status_code == 403

    requested = _post(client, "pair")
    assert requested.status_code == 202
    assert requested.headers["cache-control"] == "no-store"
    assert requested.headers["referrer-policy"] == "no-referrer"
    assert requested.json()["state"] == "awaiting_local_approval"
    assert requested.json()["expires_at"] > 0
    assert client.get("/api/owner/status").json() == {"state": "needs_pairing"}
    assert _post(client, "login").status_code == 403
    assert client.get("/api/tasks").status_code == 403
    grant = store.confirm_pairing(requested.json()["challenge"], expected_principal=OWNER)

    assert client.get("/api/owner/status").json() == {"state": "sign_in_required"}
    login = _post(client, "login")
    assert login.status_code == 200
    assert login.json()["state"] == "ready"
    token = _session_cookie(client)
    assert token not in login.text and token not in repr(login.json())
    cookie = login.headers["set-cookie"].lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/", "max-age=900"):
        assert attribute in cookie
    assert "domain=" not in cookie
    assert client.get("/api/owner/status").json() == {"state": "ready"}
    tasks = client.get("/api/tasks?fleet=all")
    assert tasks.status_code == 200
    assert {task["title"] for task in tasks.json()["data"]["tasks"]} == {
        "work for engineering", "work for data",
    }
    assert tasks.headers["cache-control"] == "no-store"
    assert client.get("/style.css").status_code == 200
    assert client.get("/api/stream?once=1&cursor=0").status_code == 200
    assert grant.active
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'message_grants'").fetchone() is None


@pytest.mark.parametrize("case", [
    "wrong_host", "missing_origin", "wrong_origin", "missing_intent", "wrong_intent",
    "cross_site", "wrong_content_type", "array_body", "non_json_body", "large_body",
    "query", "duplicate_cookie",
])
def test_pairing_post_rejects_bad_browser_context_without_mutation(browser, case):
    _, client, store, _, _ = browser
    before = store.path.read_bytes()
    headers, body, action = {}, b"{}", "pair"
    drop = ()
    if case == "wrong_host":
        headers["Host"] = "elsewhere.example.test"
    elif case == "missing_origin":
        drop = ("Origin",)
    elif case == "wrong_origin":
        headers["Origin"] = "https://elsewhere.example.test"
    elif case == "missing_intent":
        drop = ("X-Claudlobby-Owner",)
    elif case == "wrong_intent":
        headers["X-Claudlobby-Owner"] = "0"
    elif case == "cross_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif case == "wrong_content_type":
        headers["Content-Type"] = "text/plain"
    elif case == "array_body":
        body = b"[]"
    elif case == "non_json_body":
        body = b"not-json"
    elif case == "large_body":
        body = b" " * 1025
    elif case == "query":
        action += "?principal=human-001"
    else:
        headers["Cookie"] = f"{COOKIE_NAME}={'a' * 43}; {COOKIE_NAME}={'b' * 43}"
    response = _post(client, action, headers=headers, body=body, drop=drop)
    assert response.status_code == 403, (case, response.text)
    assert response.json() == {"state": "sign_in_required" if case == "duplicate_cookie" else "denied"}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert store.path.read_bytes() == before
    assert store.current_grant() is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM challenges").fetchone()[0] == 0


@pytest.mark.parametrize("name,value", [
    (b"host", b"plane.example.test"),
    (b"origin", ORIGIN.encode()),
    (b"x-claudlobby-owner", b"1"),
    (b"sec-fetch-site", b"same-origin"),
    (b"content-type", b"application/json"),
])
def test_duplicate_security_headers_refuse_before_pairing_write(browser, name, value):
    app, _, store, _, _ = browser
    before = store.path.read_bytes()
    messages = asyncio.run(_raw_http(app, "/api/owner/pair", headers=[(name, value)]))
    assert _raw_start(messages)["status"] == 403
    assert b"set-cookie" not in _raw_headers(messages)
    assert b"access-control-allow-origin" not in _raw_headers(messages)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("method,path,allowed", [
    ("OPTIONS", "/api/owner/pair", b"POST"),
    ("GET", "/api/owner/login", b"POST"),
    ("POST", "/api/owner/status", b"GET"),
    ("PUT", "/api/owner/logout", b"POST"),
])
def test_preflight_and_method_mismatch_have_no_cors_or_authority_write(
        browser, method, path, allowed):
    app, _, store, _, _ = browser
    before = store.path.read_bytes()
    preflight = [(b"host", b"plane.example.test"), (b"origin", ORIGIN.encode()),
                 (b"access-control-request-method", b"POST"),
                 (b"access-control-request-headers", b"x-claudlobby-owner,content-type")]
    messages = asyncio.run(_raw_http(app, path, method=method,
        raw_headers=preflight if method == "OPTIONS" else None))
    assert _raw_start(messages)["status"] == 405
    headers = _raw_headers(messages)
    assert headers[b"allow"] == allowed
    assert b"access-control-allow-origin" not in headers
    assert b"set-cookie" not in headers
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("body", [
    b'{"principal":"human-001"}',
    b'{"host_uid":"host_00000000000000000000000000000000"}',
    b'{"token":"browser-claim"}',
    b'{"fleet":"engineering"}',
])
def test_nonempty_json_claims_cannot_start_pairing(browser, body):
    _, client, store, _, _ = browser
    before = store.path.read_bytes()
    response = _post(client, "pair", body=body)
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("case,expected", [("chunked_oversized", 403), ("slow", 503)])
def test_chunked_oversized_and_slow_bodies_fail_without_pairing(
        browser, monkeypatch, case, expected):
    app, _, store, _, _ = browser
    before = store.path.read_bytes()
    if case == "slow":
        monkeypatch.setattr(owner_browser, "_BODY_SECONDS", 0.01)

        async def slow_receive():
            await asyncio.sleep(0.05)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        messages = asyncio.run(_raw_http(app, "/api/owner/pair",
                                        receive_override=slow_receive))
    else:
        messages = asyncio.run(_raw_http(app, "/api/owner/pair", chunks=[
            {"type": "http.request", "body": b" " * 600, "more_body": True},
            {"type": "http.request", "body": b" " * 425, "more_body": False},
        ]))
    assert _raw_start(messages)["status"] == expected
    assert b"set-cookie" not in _raw_headers(messages)
    assert store.path.read_bytes() == before


def test_forged_identity_headers_cannot_replace_injected_verifier(browser):
    _, client, store, identity, _ = browser
    identity[0] = None
    forged = {"Tailscale-User-Login": OWNER.subject,
              "X-Forwarded-User": OWNER.subject,
              "Authorization": "Bearer a-browser-claim"}
    assert client.get("/api/owner/status", headers=forged).status_code == 403
    assert client.get("/api/tasks", headers=forged).status_code == 403
    assert _post(client, "pair", headers=forged).status_code == 403
    assert store.current_grant() is None
    identity[0] = OWNER
    _pair_locally(client, store)
    # local confirmation does not silently create a session
    assert not client.cookies.get(COOKIE_NAME)
    identity[0] = OTHER
    assert _post(client, "login", headers=forged).status_code == 403


@pytest.mark.parametrize("bad_cookie", [
    f"{COOKIE_NAME}=short",
    f"{COOKIE_NAME}={'a' * 43}; {COOKIE_NAME}={'b' * 43}",
])
def test_explicit_login_recovers_malformed_cookie_after_fresh_principal_proof(
        browser, bad_cookie):
    _, client, store, identity, _ = browser
    _pair_locally(client, store)
    identity[0] = OTHER
    refused = _post(client, "login", headers={"Cookie": bad_cookie})
    assert refused.status_code == 403 and "set-cookie" not in refused.headers
    identity[0] = OWNER
    recovered = _post(client, "login", headers={"Cookie": bad_cookie})
    assert recovered.status_code == 200
    token = _session_cookie(client)
    assert token not in recovered.text
    assert client.get("/api/tasks").status_code == 200


@pytest.mark.parametrize("action", ["status", "pair", "renew", "logout"])
def test_malformed_cookie_other_lifecycle_routes_do_not_recover_or_clear(browser, action):
    _, client, store, _, _ = browser
    before = store.path.read_bytes()
    headers = {"Cookie": f"{COOKIE_NAME}=short"}
    response = (client.get("/api/owner/status", headers=headers) if action == "status"
                else _post(client, action, headers=headers))
    assert response.status_code == 403
    assert response.json() == {"state": "sign_in_required"}
    assert "set-cookie" not in response.headers
    assert store.path.read_bytes() == before


def test_delayed_stale_status_and_failed_renew_cannot_delete_new_cookie(browser):
    app, client, store, _, _ = browser
    _pair_locally(client, store)
    assert _post(client, "login").status_code == 200
    token_a = _session_cookie(client)
    old_cookie = (b"cookie", f"{COOKIE_NAME}={token_a}".encode())
    renewed = _post(client, "renew")
    assert renewed.status_code == 200
    token_b = _session_cookie(client)
    assert token_b != token_a

    async def exercise():
        status_started, status_release = asyncio.Event(), asyncio.Event()
        renew_started, renew_release = asyncio.Event(), asyncio.Event()

        def hold(started, release):
            async def before_send(message):
                if message["type"] == "http.response.start":
                    started.set()
                    await release.wait()
            return before_send

        status_task = asyncio.create_task(_raw_http(app, "/api/owner/status", method="GET",
            headers=[old_cookie], before_send=hold(status_started, status_release)))
        await asyncio.wait_for(status_started.wait(), timeout=5)

        failed_task = asyncio.create_task(_raw_http(app, "/api/owner/renew",
            headers=[old_cookie], before_send=hold(renew_started, renew_release)))
        await asyncio.wait_for(renew_started.wait(), timeout=5)
        status_release.set()
        renew_release.set()
        delayed_status, delayed_renew = await asyncio.wait_for(
            asyncio.gather(status_task, failed_task), timeout=5)
        assert _raw_start(delayed_status)["status"] == 200
        assert _raw_start(delayed_renew)["status"] == 403
        assert b'"state":"sign_in_required"' in b"".join(
            message.get("body", b"") for message in delayed_status)
        assert b"set-cookie" not in _raw_headers(delayed_status)
        assert b"set-cookie" not in _raw_headers(delayed_renew)
        assert _session_cookie(client) == token_b

    asyncio.run(exercise())
    assert client.get("/api/tasks").status_code == 200


def test_login_rotation_renewal_expiry_logout_and_revoke(browser):
    _, client, store, _, clock = browser
    grant = _pair_locally(client, store)
    first = _post(client, "login")
    assert first.status_code == 200
    token1 = _session_cookie(client)
    second = _post(client, "login")
    assert second.status_code == 200
    token2 = _session_cookie(client)
    assert token2 != token1
    with TestClient(client.app, base_url=ORIGIN) as replay:
        replay.headers["Cookie"] = f"{COOKIE_NAME}={token1}"
        assert replay.get("/api/tasks").status_code == 403
        denied = _post(replay, "renew")
        assert denied.status_code == 403 and "set-cookie" not in denied.headers
    renewed = _post(client, "renew")
    assert renewed.status_code == 200
    token3 = _session_cookie(client)
    assert token3 not in {token1, token2}
    assert token3 not in renewed.text
    with TestClient(client.app, base_url=ORIGIN) as replay:
        replay.headers["Cookie"] = f"{COOKIE_NAME}={token2}"
        assert replay.get("/api/tasks").status_code == 403
    clock[0] += SESSION_SECONDS
    assert client.get("/api/tasks").status_code == 403
    status = client.get("/api/owner/status")
    assert status.json() == {"state": "sign_in_required"}
    assert _session_cookie(client) == token3
    assert "set-cookie" not in status.headers
    expired_renewal = _post(client, "renew")
    assert expired_renewal.status_code == 403
    assert "set-cookie" not in expired_renewal.headers
    assert _post(client, "login").status_code == 200
    assert _session_cookie(client) != token3
    assert client.get("/api/tasks").status_code == 200
    signed_out = _post(client, "logout")
    assert signed_out.status_code == 200
    assert signed_out.json() == {"state": "signed_out"}
    assert COOKIE_NAME not in client.cookies
    assert client.get("/api/tasks").status_code == 403
    assert _post(client, "login").status_code == 200
    store.revoke_owner(expected_revision=grant.revision)
    assert client.get("/api/tasks").status_code == 403
    revoked_status = client.get("/api/owner/status")
    assert revoked_status.json() == {"state": "needs_pairing"}
    assert "set-cookie" not in revoked_status.headers
    assert COOKIE_NAME in client.cookies
    revoked_login = _post(client, "login")
    assert revoked_login.status_code == 403 and "set-cookie" not in revoked_login.headers


def test_other_deployment_cookie_and_wrong_principal_are_denied(browser, tmp_path):
    _, client, store, identity, _ = browser
    _pair_locally(client, store)
    assert _post(client, "login").status_code == 200
    token = _session_cookie(client)
    identity[0] = OTHER
    assert client.get("/api/tasks").status_code == 403
    assert client.get("/api/owner/status").status_code == 403
    identity[0] = OWNER
    other = tmp_path / "other-deployment"
    ensure_host_uid(other / "state")
    other_store = OwnerAccess.initialize(other)
    challenge = other_store.begin_pairing(OWNER)
    other_store.confirm_pairing(challenge.token, expected_principal=OWNER)
    other_token = other_store.open_session(OWNER).token
    with TestClient(client.app, base_url=ORIGIN) as foreign:
        foreign.headers["Cookie"] = f"{COOKIE_NAME}={other_token}"
        assert foreign.get("/api/tasks").status_code == 403
    assert client.get("/api/tasks", headers={"Cookie": f"{COOKIE_NAME}={token}"}).status_code == 200


@pytest.mark.parametrize("path", ["/api/owner/confirm", "/api/owner/grant",
                                  "/api/owner/revoke", "/api/owner/bot/restart"])
def test_unknown_owner_action_paths_are_guarded_private_routes(browser, path):
    _, client, store, _, _ = browser
    before = store.path.read_bytes()
    assert client.get(path).status_code == 403
    assert _post(client, path.removeprefix("/api/owner/")).status_code == 403
    assert store.path.read_bytes() == before
    _pair_locally(client, store)
    assert _post(client, "login").status_code == 200
    assert client.get(path).status_code in {404, 405}
    assert _post(client, path.removeprefix("/api/owner/")).status_code == 405


def test_unprepared_factory_fails_closed_without_initializing_authority(tmp_path):
    async def synthetic(_scope):
        return OWNER

    app = create_owner_browser_app(tmp_path, verify_principal=synthetic,
        expected_origin=ORIGIN, sampler=_Sampler([]), package=source_package())
    client = TestClient(app, base_url=ORIGIN)
    assert client.get("/api/owner/status").status_code == 503
    assert _post(client, "pair").status_code == 503
    assert client.get("/api/tasks").status_code == 403
    assert not (tmp_path / "state").exists()
    with pytest.raises(ValueError):
        create_owner_browser_app(tmp_path, verify_principal=None,
            expected_origin=ORIGIN, sampler=_Sampler([]), package=source_package())
    for bad_origin in ("http://plane.example.test", "https://plane.example.test/",
                       "https://plane.example.test@evil.test", "https://plane.example.test:443"):
        with pytest.raises(ValueError):
            create_owner_browser_app(tmp_path, verify_principal=synthetic,
                expected_origin=bad_origin, sampler=_Sampler([]), package=source_package())


def test_held_sse_stops_before_another_private_chunk_after_revoke(browser):
    app, client, store, _, _ = browser
    grant = _pair_locally(client, store)
    assert _post(client, "login").status_code == 200
    token = _session_cookie(client)
    messages = []
    private_frames = []

    async def drive():
        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)
            body = message.get("body", b"")
            if b"data:" in body:
                private_frames.append(body)
                store.revoke_owner(expected_revision=grant.revision)

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "method": "GET", "scheme": "https", "path": "/api/stream",
                 "raw_path": b"/api/stream", "query_string": b"cursor=0",
                 "root_path": "", "headers": [(b"host", b"plane.example.test"),
                                             (b"cookie", f"{COOKIE_NAME}={token}".encode())],
                 "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 443),
                 "http_version": "1.1", "extensions": {"http.response.pathsend": {}}}
        await asyncio.wait_for(app(scope, receive, send), timeout=5)

    asyncio.run(drive())
    assert len(private_frames) == 1
    assert messages[0]["status"] == 200
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert not any(b": ping" in item.get("body", b"") for item in messages)


def test_loopback_http_synthetic_principal_and_cookie_revocation(tmp_path):
    """HTTP mechanics behind a synthetic proxy; not browser HTTPS or Tailscale proof."""
    pytest.importorskip("uvicorn")
    _seed(tmp_path)
    store = OwnerAccess.initialize(tmp_path)
    challenge = store.begin_pairing(OWNER)
    grant = store.confirm_pairing(challenge.token, expected_principal=OWNER)
    program = '''
import socket, sys
from pathlib import Path
import uvicorn
from claudlobby.plane.owner_access import PrincipalRef
from claudlobby.plane.owner_browser import create_owner_browser_app
from tests.package_fixtures import source_package
from tests.test_plane_two_fleets import _Sampler
async def synthetic_verifier(_scope):
    return PrincipalRef("test-verifier", "human-001")
app = create_owner_browser_app(Path(sys.argv[1]), verify_principal=synthetic_verifier,
    expected_origin="https://plane.example.test", sampler=_Sampler([]), package=source_package())
server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
server.run(sockets=[socket.socket(fileno=int(sys.argv[2]))])
'''
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    env = constructed_env()
    env["HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path / "tmp")
    Path(env["HOME"]).mkdir()
    Path(env["TMPDIR"]).mkdir()
    log = tmp_path / "server.log"
    with log.open("w") as output:
        proc = subprocess.Popen([sys.executable, "-c", program, str(tmp_path), str(listener.fileno())],
            pass_fds=(listener.fileno(),), env=env, stdout=output, stderr=subprocess.STDOUT)
    listener.close()

    def request(method, path, *, headers=None, body=None):
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
            conn.request(method, path, body=body, headers={"Host": "plane.example.test", **(headers or {})})
            response = conn.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()

    try:
        for _ in range(80):
            try:
                status, _, body = request("GET", "/api/owner/status")
                assert status == 200 and json.loads(body)["state"] == "sign_in_required"
                break
            except OSError:
                assert proc.poll() is None, log.read_text()
                time.sleep(0.05)
        else:
            pytest.fail("synthetic loopback server did not start")
        status, headers, body = request("POST", "/api/owner/login", body=b"{}", headers={
            "Origin": ORIGIN, "X-Claudlobby-Owner": "1", "Content-Type": "application/json"})
        assert status == 200 and json.loads(body)["state"] == "ready"
        cookie = headers["set-cookie"].split(";", 1)[0]
        assert cookie.startswith(COOKIE_NAME + "=")
        assert cookie not in body.decode()
        status, _, body = request("GET", "/api/tasks", headers={"Cookie": cookie})
        assert status == 200 and len(json.loads(body)["data"]["tasks"]) == 2
        store.revoke_owner(expected_revision=grant.revision)
        status, _, body = request("GET", "/api/tasks", headers={"Cookie": cookie})
        assert status == 403 and b"work for engineering" not in body
    finally:
        proc.terminate()
        proc.communicate(timeout=5)


@pytest.mark.parametrize("action", ["renew", "logout"])
def test_missing_cookie_requests_explicit_sign_in_without_deleting_cookie(browser, action):
    _, client, store, _, _ = browser
    _pair_locally(client, store)
    response = _post(client, action)
    assert response.status_code == 403
    assert response.json() == {"state": "sign_in_required"}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("duplicate_session", [False, True])
def test_split_cookie_fields_accept_one_session_but_refuse_duplicates(browser, duplicate_session):
    app, client, store, _, _ = browser
    _pair_locally(client, store)
    assert _post(client, "login").status_code == 200
    token = _session_cookie(client)
    session = (COOKIE_NAME + "=" + token).encode()
    messages = asyncio.run(_raw_http(app, "/api/owner/status", method="GET", headers=[
        (b"cookie", session), (b"cookie", session if duplicate_session else b"display=compact"),
    ]))
    assert _raw_start(messages)["status"] == (403 if duplicate_session else 200)
    body = b"".join(m.get("body", b"") for m in messages)
    assert json.loads(body) == {"state": "sign_in_required" if duplicate_session else "ready"}
    assert b"set-cookie" not in _raw_headers(messages)


@pytest.mark.parametrize('path,media_type', [
    ('/owner', 'text/html'), ('/owner-entry.js', 'text/javascript'),
    ('/owner-entry.css', 'text/css'),
])
def test_exact_owner_entry_assets_are_public_without_private_authority(browser, path, media_type):
    _, client, store, identity, _ = browser
    identity[0] = None  # Public shell does not request or invent an identity.
    store.path.unlink()  # Authority unavailable must not hide the entry page.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith(media_type)
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-content-type-options'] == 'nosniff'
    policy = response.headers['content-security-policy']
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert 'unsafe-inline' not in policy and 'unsafe-eval' not in policy
    assert 'set-cookie' not in response.headers
    assert 'human-001' not in response.text and 'work for engineering' not in response.text
    head = client.head(path)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['content-length'] == response.headers['content-length']


@pytest.mark.parametrize('path', ['/owner', '/owner-entry.js', '/owner-entry.css'])
def test_owner_entry_rejects_query_method_and_foreign_boundary(browser, path):
    _, client, _, _, _ = browser
    assert client.get(path + '?next=/').status_code == 403
    assert client.get(path, headers={'Host': 'other.example.test'}).status_code == 403
    assert client.get(path, headers={'Origin': 'https://other.example.test'}).status_code == 403
    for method in ['POST', 'PUT', 'DELETE', 'OPTIONS']:
        response = client.request(method, path)
        assert response.status_code == 405
        assert response.headers['allow'] == 'GET, HEAD'
        assert 'access-control-allow-origin' not in response.headers


def test_owner_entry_does_not_open_other_static_or_private_routes(browser):
    _, client, store, _, _ = browser
    for path in ['/', '/style.css', '/api/tasks', '/owner-other.js', '/owner-entry.html']:
        assert client.get(path).status_code == 403
    assert client.get('/owner').status_code == 200
    _pair_locally(client, store)
    assert _post(client, 'login').status_code == 200
    assert _post(client, 'logout').status_code == 200
    assert client.get('/owner').status_code == 200
    assert client.get('/api/owner/status').json() == {'state': 'sign_in_required'}
    assert client.get('/api/tasks').status_code == 403


def test_owner_entry_controller_regressions(tmp_path):
    import shutil
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is unavailable')
    env = constructed_env(tmp_path, extra_keys=())
    Path(env['HOME']).mkdir()
    Path(env['TMPDIR']).mkdir()
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('plane_owner_entry.test.mjs'))],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
