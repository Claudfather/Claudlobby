"""Actual canonical reads/crypto over synthetic current native authority."""
import asyncio
import json
import sqlite3

import jwt
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_browser import COOKIE_NAME
from claudlobby.plane.session_assertion import PURPOSE
from claudlobby.plane.workspace_assertion import verify_workspace_assertion
from tests.test_plane_owner_workspace_http import http, post as bootstrap_post, token as enrollment_token, OWNER, HOST, PLANE
from tests.test_plane_workspace_assertion import profile

PREFIX = "/api/owner/connected/"


def approve_workspace(http):
    app, access, _, _, profile = http
    c = access.workspace().begin(OWNER, issuer=app.workspace_bootstrap.config.issuer, operational_origin=PLANE)
    a = verify_workspace_assertion(enrollment_token(http, {"nonce": c.nonce, "host_uid": c.owner.host_uid}),
        json.dumps({"keys": [profile[1]]}).encode(), expected=access.workspace().expected(c.nonce, OWNER))
    proposal = access.workspace().stage(a, OWNER)
    access.workspace().confirm(expected_proposal=proposal)
    return c.owner


@pytest.fixture
def connected_http(http):
    owner = approve_workspace(http)
    http[1].connected().initialize(expected_owner=owner)
    return http


def headers(token=None):
    return {"Origin": PLANE, **({"Authorization": "Bearer " + token} if token else {})}


def post(client, action, body=None, token=None, extra=None):
    return client.post(PREFIX + action, content=json.dumps(body if body is not None else {}),
        headers={**headers(token), "Content-Type": "application/json", "X-Claudlobby-Owner": "1", **(extra or {})})


def assertion(fixture, c, **changes):
    app, _, _, _, profile = fixture
    claims = {**profile[3], "iss": app.workspace_bootstrap.config.issuer,
        "host_uid": c["host_uid"], "nonce": c["nonce"], "operational_origin": PLANE,
        "purpose": PURPOSE, "aud": f"urn:claudfather:host:{c['host_uid']}:workspace-read-session:v1",
        "binding_revision": c["binding_revision"], "binding_generation": c["binding_generation"], **changes}
    return jwt.encode(claims, profile[0], algorithm="EdDSA", headers={"kid": profile[1]["kid"]})


def login(client, fixture):
    challenge = post(client, "challenge")
    assert challenge.status_code == 202, challenge.text
    c = challenge.json()
    result = post(client, "issue", {"nonce": c["nonce"], "assertion": assertion(fixture, c)})
    assert result.status_code == 200, result.text
    return result.json()


def test_actual_crypto_read_profile_status_canonical_reads_and_cookie_separation(connected_http):
    app, access, _, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        assert set(ready) == {"version", "state", "token", "expires_in", "read_profile", "session_context"}
        assert ready["read_profile"]["profile"] == "direct-owner-read-v1"
        assert set(ready["session_context"]) == {"issuer", "subject", "workspace_id", "binding_revision", "binding_generation", "operational_origin"}
        status = client.get(PREFIX + "status", headers=headers(ready["token"]))
        assert status.status_code == 200 and "token" not in status.json()
        for suffix in ("fleets", "tasks?fleet=data", "channel?fleet=data&limit=120", "overview", "summary"):
            result = client.get(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            assert result.status_code == 200 and result.json()["state"] == "ok", result.text
            assert result.headers["access-control-allow-origin"] == PLANE
            assert "set-cookie" not in result.headers and "access-control-allow-credentials" not in result.headers
        # Copying a bearer into the direct cookie never authorizes the other door.
        assert client.get("/api/tasks", headers={"Cookie": COOKIE_NAME + "=" + ready["token"]}).status_code == 403
        direct = access.open_session(OWNER)
        assert client.get(PREFIX + "status", headers=headers(direct.token)).status_code == 403
        assert client.get(PREFIX + "read/actions/send", headers=headers(ready["token"])).status_code == 400
        assert post(client, "read/tasks", token=ready["token"]).status_code == 405


@pytest.mark.parametrize("extra", [{"Origin": "null"}, {"Origin": HOST}, {"Host": "wrong.example"},
    {"Cookie": "any=value"}, {"Authorization": "Bearer fake"}, {"X-Claudlobby-Owner": "0"}])
def test_wrong_boundary_or_credential_door_never_creates_nonce(connected_http, extra):
    with TestClient(connected_http[0], base_url=HOST) as client:
        result = post(client, "challenge", extra=extra)
        assert result.status_code == 403
        if "Origin" in extra or "Host" in extra or "Cookie" in extra:
            assert "access-control-allow-origin" not in result.headers
    with sqlite3.connect(connected_http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 0


def test_explicit_init_missing_unavailable_not_cookie_or_status_repair(http):
    app = http[0]
    approve_workspace(http)
    with TestClient(app, base_url=HOST) as client:
        result = post(client, "challenge")
        assert result.status_code == 503  # optional connected schema not initialized
    # Tables still absent; native pairing and direct cookie path are unaffected.
    with sqlite3.connect(http[1].path) as conn:
        assert "connected_metadata" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


@pytest.mark.parametrize("change", [{"purpose": "claudfather-workspace-host-bootstrap-v1"},
    {"binding_revision": 2}, {"sub": "00000000-0000-4000-8000-000000000001"}])
def test_wrong_signed_session_evidence_retains_nonce(connected_http, change):
    with TestClient(connected_http[0], base_url=HOST) as client:
        c = post(client, "challenge").json()
        refused = post(client, "issue", {"nonce": c["nonce"], "assertion": assertion(connected_http, c, **change)})
        assert refused.status_code == 403
        assert connected_http[1].connected().expected(c["nonce"], OWNER).nonce == c["nonce"]


def test_precise_methods_and_preflight_legacy_unchanged(connected_http):
    with TestClient(connected_http[0], base_url=HOST) as client:
        legacy = client.get("/api/owner/workspace/challenge", headers=headers())
        assert legacy.status_code == 405 and legacy.headers["allow"] == "POST, OPTIONS"
        assert client.get(PREFIX + "issue", headers=headers()).status_code == 405
        pre = {"Origin": PLANE, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "authorization"}
        result = client.options(PREFIX + "status", headers=pre)
        assert result.status_code == 204 and result.headers["access-control-allow-methods"] == "GET"
        assert "access-control-allow-credentials" not in result.headers
        assert client.options(PREFIX + "issue", headers=pre).status_code == 403
        pre["Access-Control-Request-Method"] = "HEAD"
        assert client.options(PREFIX + "read/tasks", headers=pre).status_code == 204


def test_rotate_logout_and_source_outage_preflight_preserves_token(connected_http):
    app, access, _, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        first = login(client, connected_http)
        old_source = app.connected_reads._source
        def unavailable():
            raise AccessUnavailable()
        app.connected_reads._source = unavailable
        assert post(client, "renew", token=first["token"]).status_code == 503
        app.connected_reads._source = old_source
        assert client.get(PREFIX + "status", headers=headers(first["token"])).status_code == 200
        # Renewal needs neither issuer availability nor another attestation.
        async def issuer_down(_):
            raise RuntimeError("not disclosed")
        app.connected_reads.keys.get = issuer_down
        renewed = post(client, "renew", token=first["token"])
        assert renewed.status_code == 200 and renewed.json()["token"] != first["token"]
        assert client.get(PREFIX + "status", headers=headers(first["token"])).status_code == 403
        token = renewed.json()["token"]
        assert post(client, "logout", token=token).json() == {"version": 1, "state": "signed_out"}
        assert client.get(PREFIX + "status", headers=headers(token)).status_code == 403
        assert access.current_grant().active and access.workspace().status()[0] == "active"


def test_current_native_loss_and_binding_revoke_gate_both_status_and_read(connected_http):
    app, access, identity, _, _ = connected_http
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        assert client.get(PREFIX + "status", headers=headers(ready["token"])).status_code == 403
        assert client.get(PREFIX + "read/tasks", headers=headers(ready["token"])).status_code == 403
        identity[0] = OWNER
        _, b = access.workspace().status()
        access.workspace().revoke(expected_binding=b, expected_owner=b.owner)
        assert client.get(PREFIX + "status", headers=headers(ready["token"])).status_code == 403
        assert client.get(PREFIX + "read/tasks", headers=headers(ready["token"])).status_code == 403


def test_closing_native_check_fences_held_private_read(connected_http):
    app, access, identity, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    s = access.connected().issue(evidence(access.connected(), clock), OWNER)
    called = []
    async def producer(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        await send({"type": "http.response.body", "body": b"private held body"})
    app.connected_reads.read_app.app = producer
    async def run():
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/tasks", "query_string": b"",
                 "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                             (b"authorization", ("Bearer " + s.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            called.append(message)
        await app.connected_reads(scope, receive, send)
    asyncio.run(run())
    assert called[0]["status"] == 403
    assert b"private held body" not in b"".join(m.get("body", b"") for m in called)


def test_real_canonical_sse_stops_after_revocation_without_next_chunk(connected_http):
    app, access, identity, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    s = access.connected().issue(evidence(access.connected(), clock), OWNER)
    frames = []
    async def run():
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/stream", "query_string": b"cursor=0",
            "http_version": "1.1", "scheme": "https", "root_path": "", "server": ("host.example.test", 443),
            "asgi": {"spec_version": "2.4"}, "headers": [(b"host", b"host.example.test"),
                (b"origin", PLANE.encode()), (b"authorization", ("Bearer " + s.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            frames.append(message)
            if message["type"] == "http.response.body" and message.get("more_body"):
                identity[0] = type(OWNER)(OWNER.namespace, "1002")
        await asyncio.wait_for(app.connected_reads(scope, receive, send), 5)
    asyncio.run(run())
    bodies = [m for m in frames if m["type"] == "http.response.body"]
    assert bodies[0]["body"] == b"retry: 3000\n\n"
    assert bodies[-1]["body"] == b"" and not bodies[-1]["more_body"]
    assert not any(b"rows" in m["body"] for m in bodies)


def test_query_credentials_duplicate_headers_and_unknown_doors_refused(connected_http):
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        for path in ("read/tasks?token=x", "read/tasks?fleet=data&fleet=support", "read/index.html", "unknown"):
            assert client.get(PREFIX + path, headers=headers(ready["token"])).status_code == 400
        dup = [("Origin", PLANE), ("Authorization", "Bearer " + ready["token"]),
               ("Authorization", "Bearer " + ready["token"])]
        assert client.get(PREFIX + "status", headers=dup).status_code == 403


def test_default_and_connected_deadline_roster(connected_http):
    app = connected_http[0]
    assert app.workspace_bootstrap.response_seconds({}) == 35
    for action in ("challenge", "status", "renew", "logout", "issue"):
        method = "GET" if action == "status" else "POST"
        assert app.connected_reads.response_seconds({"path": PREFIX + action, "method": method}) == (35 if action == "issue" else 15)
    assert app.connected_reads.response_seconds({"path": PREFIX + "issue", "method": "OPTIONS"}) == 15


def test_queued_deadline_no_nonce_late_effect_and_shutdown_drains(connected_http, monkeypatch):
    import claudlobby.plane.owner_workspace_http as module
    consumer = connected_http[0].connected_reads
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = module.run_in_threadpool
        async def delayed(fn, *args, **kwargs):
            # The shipped _blocking wrapper determines whether its queued
            # guarded callable is abandoned before actually starting.
            entered.set()
            await release.wait()
            return await original(fn, *args, **kwargs)
        monkeypatch.setattr(module, "run_in_threadpool", delayed)
        monkeypatch.setattr(consumer, "response_seconds", lambda _: .03)
        scope = {"type": "http", "method": "POST", "path": PREFIX + "challenge", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        await entered.wait()
        await asyncio.wait_for(request, .5)
        assert output[0]["status"] == 503
        release.set()
        await consumer.shutdown()
        assert not consumer._workers and not consumer._operations
        with sqlite3.connect(connected_http[1].path) as conn:
            assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 0
    asyncio.run(scenario())


def test_started_work_retains_capacity_after_timeout_until_drained(connected_http, monkeypatch):
    import threading
    consumer = connected_http[0].connected_reads
    entered, release = threading.Event(), threading.Event()
    original = consumer.connected.begin
    def held(principal):
        entered.set()
        assert release.wait(3)
        return original(principal)
    monkeypatch.setattr(consumer.connected, "begin", held)
    monkeypatch.setattr(consumer, "response_seconds", lambda _: .1)
    async def scenario():
        scope = {"type": "http", "method": "POST", "path": PREFIX + "challenge", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()),
                (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        output = []
        async def send(message):
            output.append(message)
        request = asyncio.create_task(consumer(scope, receive, send))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        await asyncio.wait_for(request, .5)
        assert output[0]["status"] == 503 and len(consumer._workers) == 1
        draining = asyncio.create_task(consumer.shutdown())
        await asyncio.sleep(.01)
        assert not draining.done()  # started transaction is still owned
        release.set()
        await asyncio.wait_for(draining, 1)
        assert not consumer._workers and not consumer._operations
        with sqlite3.connect(connected_http[1].path) as conn:
            assert conn.execute("SELECT count(*) FROM connected_challenges").fetchone()[0] == 1
        # The response stayed unknown; no nonce is recovered or automatically retried.
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_finite_read_deadline_no_private_body_and_shutdown_stream_ownership(connected_http, monkeypatch):
    import claudlobby.plane.owner_connected_http as module
    consumer = connected_http[0].connected_reads
    async def held(scope, receive, send):
        await asyncio.Event().wait()
    consumer.read_app = held
    monkeypatch.setattr(module, "READ_SECONDS", .03)
    with TestClient(connected_http[0], base_url=HOST) as client:
        result = client.get(PREFIX + "read/tasks", headers=headers("A"*43))
        assert result.status_code == 503 and not consumer._readers


def test_head_runs_real_canonical_get_queries_not_static_fallback(connected_http, monkeypatch):
    import claudlobby.plane.view as view
    calls = []
    original = view._fetch_tasks
    def observed(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(view, "_fetch_tasks", observed)
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        for suffix in ("tasks?fleet=data", "tasks?fleet=unknown", "summary", "fleets", "channel?fleet=data&limit=1"):
            get = client.get(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            head = client.head(PREFIX + "read/" + suffix, headers=headers(ready["token"]))
            assert head.status_code == get.status_code == 200
            assert head.headers["content-type"] == get.headers["content-type"]
            assert head.headers["content-length"] == get.headers["content-length"]
            assert head.content == b""
            assert head.headers["access-control-allow-origin"] == PLANE
        assert len(calls) == 4  # the same canonical query ran for each GET/HEAD pair
        task = client.get(PREFIX + "read/tasks?fleet=data", headers=headers(ready["token"])).json()["data"]["tasks"][0]
        path = PREFIX + "read/tasks/" + task["task_id"] + "?fleet=data"
        get = client.get(path, headers=headers(ready["token"]))
        head = client.head(path, headers=headers(ready["token"]))
        assert head.status_code == get.status_code == 200
        assert head.headers["content-type"] == "application/json" and head.content == b""


def test_head_real_query_closing_native_loss_denies_without_any_body(connected_http, monkeypatch):
    import claudlobby.plane.view as view
    app, _, identity, _, _ = connected_http
    original = view._fetch_tasks
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        identity[0] = type(OWNER)(OWNER.namespace, "1002")
        return result
    with TestClient(app, base_url=HOST) as client:
        ready = login(client, connected_http)
        monkeypatch.setattr(view, "_fetch_tasks", changed)
        head = client.head(PREFIX + "read/tasks?fleet=data", headers=headers(ready["token"]))
        assert head.status_code == 403 and head.content == b""


def test_head_stream_uses_actual_bounded_canonical_producer_and_zero_asgi_body(connected_http):
    app, access, _, clock, _ = connected_http
    from tests.test_plane_owner_connected import evidence
    session = access.connected().issue(evidence(access.connected(), clock), OWNER)
    frames = []
    async def run():
        scope = {"type": "http", "method": "HEAD", "path": PREFIX + "read/stream", "query_string": b"cursor=0&once=0",
            "http_version": "1.1", "scheme": "https", "root_path": "", "server": ("host.example.test", 443),
            "asgi": {"spec_version": "2.4"}, "headers": [(b"host", b"host.example.test"),
                (b"origin", PLANE.encode()), (b"authorization", ("Bearer " + session.token).encode())]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            frames.append(message)
        await asyncio.wait_for(app.connected_reads(scope, receive, send), 2)
        assert not app.connected_reads._readers
    asyncio.run(run())
    assert frames[0]["status"] == 200
    assert b"text/event-stream" in dict(frames[0]["headers"])[b"content-type"]
    bodies = [m for m in frames if m["type"] == "http.response.body"]
    assert bodies and all(m.get("body", b"") == b"" for m in bodies)
    assert not bodies[-1].get("more_body", False)


def test_missing_bearer_refuses_before_native_lookup_and_head_refusal_has_no_asgi_body(connected_http):
    consumer = connected_http[0].connected_reads
    calls = []
    async def should_not_verify(scope):
        calls.append(scope)
        return OWNER
    consumer.verify_principal = should_not_verify
    async def run():
        for action, method in (("read/tasks", "HEAD"), ("status", "GET")):
            output = []
            scope = {"type": "http", "method": method, "path": PREFIX + action, "query_string": b"",
                "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode())]}
            async def receive():
                raise AssertionError("no body read expected")
            async def send(message):
                output.append(message)
            await consumer(scope, receive, send)
            assert output[0]["status"] == 403
            if method == "HEAD":
                assert not b"".join(m.get("body", b"") for m in output)
    asyncio.run(run())
    assert not calls


def test_shutdown_cancelled_owned_stream_does_not_escape_request(connected_http):
    consumer = connected_http[0].connected_reads
    async def run():
        entered = asyncio.Event()
        async def held(scope, receive, send):
            entered.set()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.Event().wait()
        consumer.read_app = held
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/stream", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()), (b"authorization", b"Bearer " + b"A"*43)]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            pass
        request = asyncio.create_task(consumer(scope, receive, send))
        await entered.wait()
        await consumer.shutdown()
        await request
        assert not consumer._readers
    asyncio.run(run())


def test_connected_selected_route_allowlist_matches_canonical_query_contract(connected_http):
    from claudlobby.plane.owner_connected_http import _READS
    app = connected_http[0].read_app.app
    routes = {r.path: r for r in app.routes if hasattr(r, "dependant")}
    for suffix, fields in _READS.items():
        route = routes["/api/" + suffix]
        assert "GET" in route.methods
        assert {p.name for p in route.dependant.query_params} == set(fields), suffix
    detail = routes["/api/tasks/{task_id}"]
    assert {p.name for p in detail.dependant.query_params} == {"fleet"}


def test_caller_cancellation_still_propagates_and_cleans_owned_reader(connected_http):
    consumer = connected_http[0].connected_reads
    async def run():
        entered = asyncio.Event()
        async def held(scope, receive, send):
            entered.set()
            await asyncio.Event().wait()
        consumer.read_app = held
        scope = {"type": "http", "method": "GET", "path": PREFIX + "read/tasks", "query_string": b"",
            "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode()), (b"authorization", b"Bearer " + b"A"*43)]}
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            pass
        request = asyncio.create_task(consumer(scope, receive, send))
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not consumer._readers
    asyncio.run(run())


@pytest.mark.parametrize("query", ["cursor=", "once=invalid"])
def test_head_stream_preserves_canonical_get_query_validation(connected_http, query):
    with TestClient(connected_http[0], base_url=HOST) as client:
        ready = login(client, connected_http)
        path = PREFIX + "read/stream?" + query
        get = client.get(path, headers=headers(ready["token"]))
        head = client.head(path, headers=headers(ready["token"]))
        assert head.status_code == get.status_code == 422
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.content == b""
