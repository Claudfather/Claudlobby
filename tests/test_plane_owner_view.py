"""The protected read boundary, with out-of-band synthetic caller identity.

These tests do not implement or claim a Tailscale verifier. All state and data
belong to disposable roots, and the existing runtime entry point is unchanged.
"""

import asyncio
from contextlib import closing
import http.client
import json
from pathlib import Path
import signal
import sqlite3
import threading
import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.db import db_file
from claudlobby.plane.owner_access import (
    AccessDenied, AccessUnavailable, OwnerAccess, PrincipalRef, SESSION_SECONDS,
)
from claudlobby.plane.owner_source import bind_source
from claudlobby.plane.owner_view import VerifiedReader, create_owner_app
from claudlobby.plane.view import begin_shutdown, create_app
from tests.package_fixtures import source_package
from tests.conftest import constructed_env
from tests.test_plane_two_fleets import _Sampler, _seed


OWNER = PrincipalRef("test-verifier", "human-001")
OTHER = PrincipalRef("test-verifier", "human-002")


def _add_route(app, path, endpoint, **kwargs):
    app.app.add_api_route(path, endpoint, **kwargs)
    # New routes belong before the canonical view's catch-all static mount.
    app.app.router.routes.insert(0, app.app.router.routes.pop())


@pytest.fixture
def protected(tmp_path):
    _seed(tmp_path)
    bind_source(tmp_path)
    clock = [1_800_000_000.0]
    store = OwnerAccess.initialize(tmp_path, clock=lambda: clock[0])
    challenge = store.begin_pairing(OWNER)
    grant = store.confirm_pairing(challenge.token, expected_principal=OWNER)
    session = store.open_session(OWNER)
    identity = [VerifiedReader(OWNER, session.token)]

    async def verify(_scope):
        if identity[0] is None:
            raise AccessDenied("unverified_caller")
        return identity[0]

    app = create_owner_app(tmp_path, verify_reader=verify,
                           sampler=_Sampler([]), package=source_package())
    app.access._clock = lambda: clock[0]
    return app, TestClient(app), store, grant, identity, clock


def test_every_registered_route_static_error_and_future_route_is_guarded(protected):
    app, client, _, _, identity, _ = protected
    _add_route(app, "/future-private", lambda: {"secret": "must not escape"})
    identity[0] = None
    paths = {route.path for route in app.app.routes}
    paths.update({"/style.css", "/panel-state.js", "/favicon.ico", "/missing", "/api/receipt/test"})
    for path in sorted(paths):
        for method in ("GET", "HEAD"):
            result = client.request(method, path)
            assert result.status_code == 403, (method, path, result.text)
            assert result.headers["cache-control"] == "no-store"
            if method == "GET":
                assert result.json()["state"] == "denied"
                assert "secret" not in result.text


@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/style.css",
    "/api/summary", "/api/channel", "/api/tasks", "/api/identities", "/api/fleets",
    "/api/grid", "/api/presence", "/api/overview", "/api/inventory",
    "/api/equipment?alias=bot:engineering/one", "/api/org", "/api/utilization",
    "/api/search?q=work", "/api/trust", "/healthz", "/api/stream?once=1&cursor=0"])
def test_owner_can_read_canonical_routes_with_no_store(protected, path):
    _, client, *_ = protected
    result = client.get(path)
    assert result.status_code == 200, result.text
    assert result.headers["cache-control"] == "no-store"


def test_whole_deployment_fleet_navigation_does_not_select_another_root(protected, tmp_path):
    _, client, *_ = protected
    for query in ("", "?fleet=all", "?fleet="):
        tasks = client.get("/api/tasks" + query).json()["data"]["tasks"]
        assert {task["title"] for task in tasks} == {"work for engineering", "work for data"}
    tasks = client.get("/api/tasks?fleet=engineering").json()["data"]["tasks"]
    assert [task["title"] for task in tasks] == ["work for engineering"]
    foreign = tmp_path / "other-deployment"
    _seed(foreign, fleets=(("foreign", "c"),))
    result = client.get("/api/tasks", params={"root": str(foreign), "host_uid": "foreign"})
    assert "work for foreign" not in result.text
    assert client.get("/api/tasks?fleet=foreign").json()["state"] == "unknown"


def test_claimed_headers_cookies_query_and_valid_token_do_not_verify_identity(protected):
    _, client, _, _, identity, _ = protected
    token = identity[0].token
    identity[0] = None
    response = client.get("/api/tasks", params={"token": token, "principal": OWNER.subject},
        headers={"Authorization": "Bearer " + token, "Cookie": "session=" + token,
                 "Tailscale-User-Login": OWNER.subject, "X-Forwarded-For": "127.0.0.1",
                 "Origin": "https://example.invalid", "X-Workspace": "owner"})
    assert response.status_code == 403
    assert token not in response.text and OWNER.subject not in response.text


def test_token_requires_the_verified_paired_principal(protected):
    _, client, _, _, identity, _ = protected
    identity[0] = VerifiedReader(OTHER, identity[0].token)
    assert client.get("/api/channel").status_code == 403


def test_token_from_other_deployment_is_rejected(protected, tmp_path):
    _, client, _, _, identity, _ = protected
    other = tmp_path / "other-deployment"
    ensure_host_uid(other / "state")
    store = OwnerAccess.initialize(other)
    challenge = store.begin_pairing(OWNER)
    store.confirm_pairing(challenge.token, expected_principal=OWNER)
    identity[0] = VerifiedReader(OWNER, store.open_session(OWNER).token)
    assert client.get("/api/tasks").status_code == 403


@pytest.mark.parametrize("change", ["revoke", "expire", "rotate", "end", "unavailable", "host-change"])
def test_authority_is_rechecked_on_the_next_request(protected, change):
    _, client, store, grant, identity, clock = protected
    assert client.get("/api/tasks").status_code == 200
    if change == "revoke":
        store.revoke_owner(expected_revision=grant.revision)
    elif change == "expire":
        clock[0] += SESSION_SECONDS
    elif change == "rotate":
        store.renew_session(identity[0].token, OWNER)
    elif change == "end":
        store.end_session(identity[0].token, OWNER)
    elif change == "unavailable":
        store.path.unlink()
    else:
        (store.root / "state/host-uid").write_text("host_" + "f" * 32)
    result = client.get("/api/tasks")
    assert result.status_code == (503 if change in {"unavailable", "host-change"} else 403)
    assert "work for engineering" not in result.text


def test_read_authority_never_grants_writes_or_websockets(protected):
    app, client, *_ = protected
    calls = []
    _add_route(app, "/future-write", lambda: calls.append(True), methods=["POST"])
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert client.request(method, "/future-write").status_code == 405
    assert calls == []
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect("/api/stream"):
            pytest.fail("websocket admitted")
    assert caught.value.code == 1008


def test_unprepared_authority_is_never_created_by_read(tmp_path):
    async def verify(_scope):
        return VerifiedReader(OWNER, "a" * 43)

    app = create_owner_app(tmp_path, verify_reader=verify,
                           sampler=_Sampler([]), package=source_package())
    assert TestClient(app).get("/api/summary").status_code == 503
    assert not (tmp_path / "state").exists()
    with pytest.raises(ValueError):
        create_owner_app(tmp_path, verify_reader=None, package=source_package())


def test_verifier_unavailability_is_generic_and_cannot_leak_its_error(tmp_path):
    async def verify(_scope):
        raise AccessUnavailable("private host details")

    result = TestClient(create_owner_app(tmp_path, verify_reader=verify,
                        sampler=_Sampler([]), package=source_package())).get("/")
    assert result.status_code == 503
    assert "private host details" not in result.text


def test_revoke_during_handler_refuses_before_headers_or_body(protected):
    app, client, store, grant, *_ = protected

    def late():
        store.revoke_owner(expected_revision=grant.revision)
        return Response("private response", headers={"X-Private": "private header"})

    _add_route(app, "/late", late)
    result = client.get("/late")
    assert result.status_code == 403
    assert "private" not in result.text
    assert "x-private" not in result.headers


async def _drive(app, path, on_body, *, spec="2.4", query=b""):
    messages = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            await on_body(message.get("body", b""))

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec},
             "method": "GET", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query, "root_path": "",
             "headers": [], "client": ("127.0.0.1", 12345),
             "server": ("127.0.0.1", 12346), "http_version": "1.1",
             "extensions": {"http.response.pathsend": {}, "http.response.zerocopysend": {}}}
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return messages


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
@pytest.mark.parametrize("change", ["revoke", "expire", "unavailable"])
def test_held_real_sse_ends_before_next_private_delivery(protected, spec, change):
    app, _, store, grant, _, clock = protected
    seen = []

    async def on_body(body):
        if b"data:" in body:
            seen.append(body)
            if change == "revoke":
                store.revoke_owner(expected_revision=grant.revision)
            elif change == "expire":
                clock[0] += SESSION_SECONDS
            else:
                store.path.unlink()

    messages = asyncio.run(_drive(app, "/api/stream", on_body, spec=spec, query=b"cursor=0"))
    assert len(seen) == 1
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert messages[0]["status"] == 200  # headers already sent; termination, not a new status
    assert not any(b": ping" in item.get("body", b"") for item in messages)


def test_revocation_after_stream_headers_blocks_first_private_frame(protected):
    app, _, store, grant, *_ = protected

    async def on_body(body):
        if b"retry:" in body:
            store.revoke_owner(expected_revision=grant.revision)

    messages = asyncio.run(_drive(app, "/api/stream", on_body, query=b"cursor=0"))
    assert not any(b"data:" in item.get("body", b"") for item in messages)


def test_static_bytes_pass_through_guard_even_with_sendfile_extension(protected):
    app, *_ = protected

    async def ignore(_body):
        pass

    messages = asyncio.run(_drive(app, "/style.css", ignore))
    assert all(item["type"] in {"http.response.start", "http.response.body"} for item in messages)
    assert any(item.get("body") for item in messages)


def test_chunked_response_cannot_send_a_second_private_chunk_after_revoke(protected):
    app, _, store, grant, *_ = protected

    async def chunks():
        yield b"first"
        yield b"private later chunk"

    _add_route(app, "/chunks", lambda: StreamingResponse(chunks()))

    async def on_body(body):
        if body == b"first":
            store.revoke_owner(expected_revision=grant.revision)

    messages = asyncio.run(_drive(app, "/chunks", on_body))
    assert b"".join(item.get("body", b"") for item in messages) == b"first"


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("content_type", ["text/event-stream", "application/octet-stream"])
def test_refusal_is_latched_when_a_producer_swallows_it(protected, monkeypatch,
                                                        started, content_type):
    from claudlobby.plane.owner_view import _ReadStopped

    app, *_ = protected
    admit = app._admit
    calls = []
    refusal_call = 3 if started else 2

    def transient_failure(reader):
        calls.append(True)
        if len(calls) == refusal_call:
            raise AccessUnavailable("transient outage")
        admit(reader)

    monkeypatch.setattr(app, "_admit", transient_failure)

    async def producer(scope, receive, send):
        headers = {"type": "http.response.start", "status": 200,
                   "headers": [(b"content-type", content_type.encode())]}
        await send(headers)
        if started:
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
        with pytest.raises(_ReadStopped):
            await send({"type": "http.response.body", "body": b"private", "more_body": True})
        # Even a recovered authority cannot reopen this refused response.
        for message in (headers, {"type": "http.response.body", "body": b"resumed"}):
            with pytest.raises(_ReadStopped):
                await send(message)

    async def ignore(_body):
        pass

    app.app = producer
    messages = asyncio.run(_drive(app, "/swallowed", ignore))
    assert len(calls) == refusal_call
    assert messages[0]["status"] == (200 if started else 503)
    assert not any(b"private" in m.get("body", b"") or b"resumed" in m.get("body", b"")
                   for m in messages)
    if started:
        assert messages[-1].get("more_body") is (False if content_type == "text/event-stream" else True)


def test_lifespan_and_existing_shutdown_signal_pass_through(protected):
    app, *_ = protected

    async def exercise():
        async with app.app.router.lifespan_context(app.app):
            async def on_body(body):
                if b"retry:" in body:
                    begin_shutdown(app)

            messages = await _drive(app, "/api/stream", on_body)
            assert app.state.shutting_down.is_set()
            assert messages[-1].get("more_body") is False

    asyncio.run(exercise())


def test_existing_factory_does_not_silently_enable_owner_access(tmp_path):
    app = create_app(tmp_path, sampler=_Sampler([]), package=source_package())
    assert TestClient(app).get("/api/summary").json()["state"] == "absent"
    assert not (tmp_path / "state/plane/owner-access.db").exists()


def test_reader_repr_does_not_expose_session(protected):
    _, _, _, _, identity, _ = protected
    assert identity[0].token not in repr(identity[0])


@pytest.mark.parametrize("response_kind", ["sse", "fixed", "chunked"])
def test_real_http_process_honors_out_of_process_revocation(tmp_path, response_kind):
    """Loopback sample with an explicitly synthetic verifier, no host ingress.

    A test-only bearer adapter lets the client select its preverified fixture
    context. Neither the credential nor private payload goes into argv/logs.
    """
    pytest.importorskip("uvicorn")
    _seed(tmp_path)
    bind_source(tmp_path)
    store = OwnerAccess.initialize(tmp_path)
    challenge = store.begin_pairing(OWNER)
    grant = store.confirm_pairing(challenge.token, expected_principal=OWNER)
    session = store.open_session(OWNER)
    credential = tmp_path / "test-reader"
    credential.write_text(session.token)
    credential.chmod(0o600)
    program = '''
import asyncio, socket, sys
from pathlib import Path
import uvicorn
from fastapi.responses import StreamingResponse
from claudlobby.plane.owner_access import AccessDenied, PrincipalRef
from claudlobby.plane.owner_view import VerifiedReader, create_owner_app
from tests.package_fixtures import source_package
from tests.test_plane_two_fleets import _Sampler
root = Path(sys.argv[1])
token = (root / "test-reader").read_text()
async def synthetic_verifier(scope):
    values = [v for k,v in scope["headers"] if k.lower() == b"authorization"]
    if values != [("Bearer " + token).encode()]:
        raise AccessDenied("synthetic_verifier_refused")
    return VerifiedReader(PrincipalRef("test-verifier", "human-001"), token)
app = create_owner_app(root, verify_reader=synthetic_verifier,
                       sampler=_Sampler([]), package=source_package())
async def private_chunks():
    yield b"first"
    for _ in range(500):
        if (root / "continue-response").exists():
            break
        await asyncio.sleep(.01)
    else:
        raise RuntimeError("test response was not released")
    yield b"private later chunk"
async def chunks(fixed: bool = False):
    headers = {"Content-Length": "24"} if fixed else {}
    return StreamingResponse(private_chunks(), media_type="application/octet-stream", headers=headers)
app.app.add_api_route("/private-chunks", chunks)
app.app.router.routes.insert(0, app.app.router.routes.pop())
server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
server.run(sockets=[socket.socket(fileno=int(sys.argv[2]))])
'''
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    log = tmp_path / "server.log"
    env = constructed_env()
    env["HOME"] = str(tmp_path / "home")
    env["TMPDIR"] = str(tmp_path / "tmp")
    Path(env["HOME"]).mkdir()
    Path(env["TMPDIR"]).mkdir()
    headers = {"Authorization": "Bearer " + session.token}
    with log.open("w") as output:
        proc = subprocess.Popen([sys.executable, "-c", program, str(tmp_path), str(listener.fileno())],
            pass_fds=(listener.fileno(),), env=env, stdout=output, stderr=subprocess.STDOUT)
    listener.close()
    held = None
    try:
        for _ in range(80):
            try:
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)) as client:
                    client.request("GET", "/api/tasks")
                    result = client.getresponse()
                    assert result.status == 403
                    result.read()
                    break
            except OSError:
                assert proc.poll() is None, log.read_text()
                time.sleep(0.05)
        else:
            pytest.fail("loopback sample did not start")
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request("GET", "/api/tasks?fleet=all", headers=headers)
            result = client.getresponse()
            assert result.status == 200
            assert len(json.loads(result.read())["data"]["tasks"]) == 2
        held = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        path = ("/api/stream?cursor=0" if response_kind == "sse"
                else "/private-chunks?fixed=" + ("true" if response_kind == "fixed" else "false"))
        held.request("GET", path, headers=headers)
        response = held.getresponse()
        assert response.status == 200
        if response_kind == "sse":
            # Consume a complete frame before changing durable authority.
            for _ in range(10):
                line = response.readline()
                assert line, "stream ended before its first data frame"
                if line.startswith(b"data:"):
                    break
            else:
                pytest.fail("stream did not deliver its initial data frame")
            assert response.readline() == b"\n"
        else:
            assert response.read(5) == b"first"
            if response_kind == "fixed":
                assert response.getheader("Content-Length") == "24"
        store.revoke_owner(expected_revision=grant.revision)
        if response_kind == "sse":
            assert response.read() == b""
        else:
            (tmp_path / "continue-response").write_text("ready")
            with pytest.raises(http.client.IncompleteRead) as caught:
                response.read()
            assert caught.value.partial == b""
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
            client.request("GET", "/api/tasks", headers=headers)
            result = client.getresponse()
            assert result.status == 403
            assert b"work for engineering" not in result.read()
    finally:
        if held:
            held.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    # Current uvicorn restores/re-raises the captured signal after graceful
    # cleanup. A timeout/forced SIGKILL is never an acceptable completion.
    assert proc.returncode in {0, -signal.SIGTERM}, log.read_text()
    assert "Traceback" not in log.read_text()
    assert session.token not in log.read_text()


@pytest.mark.parametrize("path", ["/api/tasks", "/api/channel", "/api/identities",
    "/api/fleets", "/api/summary", "/api/grid", "/api/presence", "/api/overview",
    "/api/inventory", "/api/equipment?alias=foreign", "/api/org", "/api/utilization",
    "/api/search?q=foreign", "/api/trust", "/healthz", "/api/stream?once=1&cursor=0"])
def test_foreign_source_is_generic_before_any_private_response(protected, tmp_path, path):
    _, client, *_ = protected
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("UPDATE work_items SET host_uid='foreign-host', title='foreign secret'")
    response = client.get(path)
    assert response.status_code == 403
    assert response.json()["state"] == "denied"
    assert "foreign" not in response.text and "plane.db" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_unbound_source_is_generic_unavailable_without_read_repair(protected, tmp_path):
    _, client, *_ = protected
    with sqlite3.connect(db_file(tmp_path)) as conn:
        conn.execute("DROP TABLE owner_source_binding")
    response = client.get("/api/tasks")
    assert response.status_code == 503
    assert "plane.db" not in response.text
    with sqlite3.connect(db_file(tmp_path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='owner_source_binding'").fetchone()


def test_held_stream_rechecks_source_snapshot_and_stops_without_foreign_details(protected, tmp_path):
    app, *_ = protected

    async def on_body(body):
        if b"retry:" in body:
            with sqlite3.connect(db_file(tmp_path)) as conn:
                conn.execute("UPDATE work_items SET host_uid='foreign-host', title='foreign secret'")

    messages = asyncio.run(_drive(app, "/api/stream", on_body, query=b"cursor=0"))
    assert messages[0]["status"] == 200
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert b"data:" not in b"".join(m.get("body", b"") for m in messages)
    assert b"foreign" not in b"".join(m.get("body", b"") for m in messages)


@pytest.mark.parametrize("query", [b"", b"cursor=0"])
def test_stream_envelopes_run_wholly_off_loop_without_redundant_initial_admission(protected, monkeypatch, query):
    from claudlobby.plane import view
    app, *_ = protected
    real_envelope = view._envelope
    calls = []
    async def run():
        loop_thread = threading.get_ident()
        def measured(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            calls.append(True)
            return real_envelope(*args, **kwargs)
        monkeypatch.setattr(view, "_envelope", measured)
        async def ignore(_body):
            pass
        result = await _drive(app, "/api/stream", ignore, query=query + (b"&" if query else b"") + b"once=1")
        assert result[0]["status"] == 200
        # Head or cursor preflight, then one tail read; no third admission.
        assert len(calls) == 2
    asyncio.run(run())


def test_slow_stream_query_does_not_block_event_loop(protected, monkeypatch):
    from claudlobby.plane import view
    app, *_ = protected
    entered, release = threading.Event(), threading.Event()
    real_envelope = view._envelope
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(2), "event loop did not release slow query"
        return real_envelope(*args, **kwargs)
    monkeypatch.setattr(view, "_envelope", slow)
    async def run():
        async def ignore(_body):
            pass
        task = asyncio.create_task(_drive(app, "/api/stream", ignore, query=b"once=1"))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.005)
            assert entered.is_set()
            release.set()
            assert (await task)[0]["status"] == 200
        finally:
            release.set()
    asyncio.run(run())


def test_gate_host_read_oserror_is_generic_before_bytes(protected, monkeypatch):
    from claudlobby.plane import owner_source
    _, client, *_ = protected
    def unavailable(_path):
        raise OSError("private host path")
    monkeypatch.setattr(owner_source, "read_host_uid", unavailable)
    response = client.get("/api/tasks")
    assert response.status_code == 503 and "private" not in response.text
