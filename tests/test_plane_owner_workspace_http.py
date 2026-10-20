"""Canonical proposal storage/crypto with synthetic ingress and public key bytes."""
import asyncio
from dataclasses import replace
import json
import sqlite3

import jwt
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef
from claudlobby.plane.owner_browser import create_owner_browser_app
from claudlobby.plane.owner_source import bind_source
from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
from tests.deadline_clock import bounded, move_clock, status
from tests.test_plane_two_fleets import _Sampler, _seed
from tests.test_plane_workspace_assertion import profile

HOST = "https://host.example.test"
PLANE = "https://plane.example.test"
ISSUER = "https://accounts.example.test"
OWNER = PrincipalRef("tailscale:controlplane.tailscale.com", "1001")


@pytest.fixture
def http(tmp_path, profile):
    _seed(tmp_path)
    bind_source(tmp_path)
    clock = [1800000000.0]
    store = OwnerAccess.initialize(tmp_path, clock=lambda: clock[0])
    pair = store.begin_pairing(OWNER)
    owner = store.confirm_pairing(pair.token, expected_principal=OWNER)
    store.workspace().initialize(expected_owner=owner)
    identity = [OWNER]
    async def verify(scope):
        return identity[0]
    app = create_owner_browser_app(tmp_path, verify_principal=verify,
        expected_origin=HOST, sampler=_Sampler({}), workspace_bootstrap=WorkspaceBootstrapConfig(ISSUER, PLANE))
    app.access._clock = lambda: clock[0]
    async def keys(_):
        return json.dumps({"keys": [profile[1]]}).encode()
    app.workspace_bootstrap.keys.get = keys  # no network; actual PyJWT below
    return app, store, identity, clock, profile


def post(client, path, data=None, **kwargs):
    headers = {"Origin": PLANE, "X-Claudlobby-Owner": "1", "Content-Type": "application/json",
               "Sec-Fetch-Site": "cross-site"}
    headers.update(kwargs.pop("headers", {}))
    return client.post("/api/owner/workspace/" + path, content=json.dumps(data or {}), headers=headers, **kwargs)


def token(http, challenge, **extra):
    profile = http[4]
    values = {**profile[3], "iss": ISSUER, "host_uid": challenge["host_uid"], "nonce": challenge["nonce"],
              "operational_origin": PLANE,
              "aud": f"urn:claudfather:host:{challenge['host_uid']}:workspace-read-bootstrap:v1", **extra}
    return jwt.encode(values, profile[0], algorithm="EdDSA", headers={"kid": profile[1]["kid"]})


def test_actual_crypto_stage_local_approval_only_and_no_session(http):
    app, store, _, _, _ = http
    with TestClient(app, base_url=HOST) as client:
        c = post(client, "challenge")
        assert c.status_code == 202
        assert c.headers["access-control-allow-origin"] == PLANE
        assert "access-control-allow-credentials" not in c.headers and "set-cookie" not in c.headers
        result = post(client, "stage", {"nonce": c.json()["nonce"], "assertion": token(http, c.json())})
        assert result.status_code == 202 and result.json()["state"] == "awaiting_local_approval"
        assert store.workspace().status() == ("absent", None)
        proposal = store.workspace().inspect(c.json()["nonce"])
        assert proposal.attestation.subject == http[4][3]["sub"]
        with sqlite3.connect(store.path) as conn:
            assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        binding = store.workspace().confirm(expected_proposal=proposal)
        assert binding.workspace_id == proposal.attestation.workspace_id


@pytest.mark.parametrize("headers", [{"Origin": "null"}, {"Origin": HOST},
    {"Origin": "https://foreign.example"}, {"Authorization": "Bearer fake"}, {"Cookie": "fake=value"},
    {"X-Claudlobby-Owner": "0"}])
def test_wrong_origin_or_credential_channel_never_begins(http, headers):
    with TestClient(http[0], base_url=HOST) as client:
        response = post(client, "challenge", headers=headers)
        assert response.status_code == 403
        if headers.get("Origin"):
            assert "access-control-allow-origin" not in response.headers
    with sqlite3.connect(http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 0


def test_preflight_native_owner_required_and_no_mutation(http):
    with TestClient(http[0], base_url=HOST) as client:
        headers = {"Origin": PLANE, "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "x-claudlobby-owner,content-type"}
        assert client.options("/api/owner/workspace/challenge", headers=headers).status_code == 204
        http[2][0] = PrincipalRef(OWNER.namespace, "1002")
        assert client.options("/api/owner/workspace/challenge", headers=headers).status_code == 403
    with sqlite3.connect(http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 0


def test_direct_lifecycle_not_widened_and_methods_preserved(http):
    with TestClient(http[0], base_url=HOST) as client:
        assert client.post("/api/owner/login", json={}, headers={"Origin": PLANE}).status_code == 403
        response = client.get("/api/owner/workspace/challenge", headers={"Origin": PLANE})
        assert response.status_code == 405 and response.headers["allow"] == "POST, OPTIONS"


@pytest.mark.parametrize("body", [b'{"nonce":1,"nonce":2}', b'[]', b'{"nonce":NaN}', b'{"extra":1}', b'x' * 1025], ids=['duplicate','array','nonfinite','unknown','oversize'])
def test_strict_challenge_body(http, body):
    with TestClient(http[0], base_url=HOST) as client:
        result = client.post("/api/owner/workspace/challenge", content=body,
            headers={"Origin": PLANE, "Content-Type": "application/json", "X-Claudlobby-Owner": "1"})
        assert result.status_code == 400


def test_signature_tuple_refusal_leaves_pending_and_no_binding(http):
    with TestClient(http[0], base_url=HOST) as client:
        challenge = post(client, "challenge").json()
        result = post(client, "stage", {"nonce": challenge["nonce"],
            "assertion": token(http, challenge, operational_origin="https://other.example")})
        assert result.status_code == 403
        assert http[1].workspace().status() == ("absent", None)


def test_held_closing_identity_change_denies_but_staged_proposal_remains(http, monkeypatch):
    app, store, identity, _, _ = http
    original = app.workspace_bootstrap.workspace.stage
    def stage(*args):
        result = original(*args)
        identity[0] = PrincipalRef(OWNER.namespace, "1002")
        return result
    monkeypatch.setattr(app.workspace_bootstrap.workspace, "stage", stage)
    with TestClient(app, base_url=HOST) as client:
        challenge = post(client, "challenge").json()
        result = post(client, "stage", {"nonce": challenge["nonce"], "assertion": token(http, challenge)})
        assert result.status_code == 403 and set(result.json()) == {"state"}
        assert store.workspace().inspect(challenge["nonce"]).attestation.nonce == challenge["nonce"]


def test_worker_saturation_cancel_retains_capacity_until_completion(http, monkeypatch):
    async def scenario():
        consumer = http[0].workspace_bootstrap
        entered, release = asyncio.Event(), asyncio.Event()
        async def held(scope, receive):
            if len(consumer._workers) == 8:
                entered.set()
            await release.wait()
            return {"state": "unavailable"}, 503
        monkeypatch.setattr(consumer, "_response", held)
        scope = {"type": "http", "method": "POST", "path": "/api/owner/workspace/challenge", "query_string": b"",
                 "headers": [(b"host", b"host.example.test"), (b"origin", PLANE.encode())]}
        async def receive():
            return {"type": "http.request", "body": b"{}"}
        responses = []
        async def send(message):
            responses.append(message)
        requests = [asyncio.create_task(consumer(scope, receive, send)) for _ in range(8)]
        await entered.wait()
        requests[0].cancel()
        await asyncio.gather(requests[0], return_exceptions=True)
        assert len(consumer._workers) == 8
        await consumer(scope, receive, send)
        assert responses[-2]["status"] == 503
        release.set()
        await asyncio.gather(*requests[1:])
        await asyncio.sleep(0)
        assert not consumer._workers
    asyncio.run(scenario())


def test_readonly_admission_exact_deadline_principal_and_staged_challenge(http):
    store = http[1]
    workspace = store.workspace()
    c = workspace.begin(OWNER, issuer=ISSUER, operational_origin=PLANE)
    before = store.path.read_bytes()
    workspace.admit_challenge(c, OWNER)
    assert store.path.read_bytes() == before
    with pytest.raises(AccessDenied):
        workspace.admit_challenge(replace(c, expires_at=c.expires_at + 1), OWNER)
    with pytest.raises(AccessDenied):
        workspace.admit_challenge(c, PrincipalRef(OWNER.namespace, "1002"))
    from claudlobby.plane.workspace_assertion import verify_workspace_assertion
    expected = workspace.expected(c.nonce, OWNER)
    info = {"nonce": c.nonce, "host_uid": c.owner.host_uid}
    attestation = verify_workspace_assertion(token(http, info),
        json.dumps({"keys": [http[4][1]]}).encode(), expected=expected)
    proposal = workspace.stage(attestation, OWNER)
    with pytest.raises(AccessDenied):
        workspace.admit_challenge(c, OWNER)
    before = store.path.read_bytes()
    workspace.admit_proposal(proposal, OWNER)
    assert store.path.read_bytes() == before
    with pytest.raises(AccessDenied):
        workspace.admit_proposal(replace(proposal, created_at=proposal.created_at - 1), OWNER)
    http[3][0] = proposal.expires_at
    with pytest.raises(AccessDenied):
        workspace.admit_proposal(proposal, OWNER)


@pytest.mark.parametrize("issuer,origin", [(ISSUER, None), (None, PLANE), ("http://issuer.example", PLANE)])
def test_config_requires_both_exact_https(issuer, origin):
    with pytest.raises(ValueError):
        WorkspaceBootstrapConfig(issuer, origin)


@pytest.mark.parametrize("state", ["absent", "revoked"])
def test_preflight_requires_active_current_owner(http, state):
    app, store, _, _, _ = http
    owner = store.current_grant()
    if state == "revoked":
        store.revoke_owner(expected_revision=owner.revision)
    else:
        with sqlite3.connect(store.path) as conn:
            conn.execute("DELETE FROM grants")
    with TestClient(app, base_url=HOST) as client:
        response = client.options("/api/owner/workspace/challenge", headers={"Origin": PLANE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-claudlobby-owner"})
        assert response.status_code == 403 and response.json() == {"state": "denied"}


def test_challenge_capacity_is_unavailable_not_policy_denial(http):
    from claudlobby.plane.owner_workspace import MAX_PENDING
    for _ in range(MAX_PENDING):
        http[1].workspace().begin(OWNER, issuer=ISSUER, operational_origin=PLANE)
    with TestClient(http[0], base_url=HOST) as client:
        result = post(client, "challenge")
        assert result.status_code == 503 and result.json() == {"state": "unavailable"}


async def _raw_challenge(consumer, responses):
    scope = {"type": "http", "method": "POST", "path": "/api/owner/workspace/challenge",
             "query_string": b"", "headers": [(b"host", b"host.example.test"),
             (b"origin", PLANE.encode()), (b"content-type", b"application/json"),
             (b"x-claudlobby-owner", b"1")]}
    async def receive():
        return {"type": "http.request", "body": b"{}"}
    async def send(message):
        responses.append(message)
    await consumer(scope, receive, send)


def test_pool_wait_is_in_deadline_and_never_begins_late(http, monkeypatch):
    import threading
    import anyio
    from starlette.concurrency import run_in_threadpool
    from claudlobby.plane import owner_workspace_http as module
    monkeypatch.setattr(module, "RESPONSE_SECONDS", 0.05, raising=False)
    entered, release = threading.Event(), threading.Event()
    def occupied():
        entered.set()
        release.wait(3)
    async def scenario():
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        blocker = asyncio.create_task(run_in_threadpool(occupied))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            responses = []
            await asyncio.wait_for(_raw_challenge(http[0].workspace_bootstrap, responses), 0.5)
            assert responses[0]["status"] == 503
            release.set()
            await blocker
            await http[0].workspace_bootstrap.shutdown()
        finally:
            release.set()
            await blocker
            limiter.total_tokens = original
    asyncio.run(scenario())
    with sqlite3.connect(http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 0


def test_late_source_never_starts_begin_and_retains_capacity(http, monkeypatch):
    import threading
    from claudlobby.plane import owner_workspace_http as module
    consumer = http[0].workspace_bootstrap
    entered, release = threading.Event(), threading.Event()
    original = consumer._source
    def held():
        value = original()
        entered.set()
        assert release.wait(30)
        return value
    monkeypatch.setattr(consumer, "_source", held)
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        responses = []
        request = asyncio.create_task(_raw_challenge(consumer, responses))
        def state():
            return (f"request={status(request)}, entered={entered.is_set()}, "
                    f"sent={[m.get('status', m['type']) for m in responses]}, "
                    f"workers={len(consumer._workers)}, operations={len(consumer._operations)}")
        try:
            assert await asyncio.to_thread(entered.wait, 10), "waited 10 s for the source to run: " + state()
            # Its deadline passes while the source is held. A 50 ms deadline could
            # answer first on a slow runner, and then no source ran (#2374, #2409).
            advance(module.RESPONSE_SECONDS)
            with paused():  # the answer is due now (#2406)
                await bounded(request, "the response (loop clock held)", state)
            await asyncio.sleep(.01)  # a job that ended with its answer would be gone by now
            assert responses[0]["status"] == 503 and len(consumer._workers) == 1, state()
            release.set()
            with paused():  # the drain ends with the source
                await bounded(consumer.shutdown(), "shutdown (loop clock held)", state)
            assert not consumer._workers and not consumer._operations, state()
        finally:
            release.set()
    asyncio.run(scenario())
    with sqlite3.connect(http[1].path) as conn:
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 0


def test_started_begin_finishes_unknown_and_lifespan_drains_before_read_shutdown(http, monkeypatch):
    import threading
    from claudlobby.plane import owner_workspace_http as module
    app, store, _, _, _ = http
    consumer = app.workspace_bootstrap
    entered, release = threading.Event(), threading.Event()
    original = consumer.workspace.begin
    def held(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(30)
        return value
    monkeypatch.setattr(consumer.workspace, "begin", held)
    observed = []
    async def read_lifespan(scope, receive, send):
        assert (await receive())["type"] == "lifespan.shutdown"
        observed.append((consumer._stopping, len(consumer._workers)))
        await send({"type": "lifespan.shutdown.complete"})
    monkeypatch.setattr(app, "read_app", read_lifespan)
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        responses = []
        request = asyncio.create_task(_raw_challenge(consumer, responses))
        def state():
            return (f"request={status(request)}, entered={entered.is_set()}, "
                    f"sent={[m.get('status', m['type']) for m in responses]}, "
                    f"workers={len(consumer._workers)}, stopping={consumer._stopping}, observed={observed}")
        try:
            assert await asyncio.to_thread(entered.wait, 10), "waited 10 s for begin to run: " + state()
            # Its deadline passes once begin has run. With a short deadline the
            # setup before begin raced it on a slow runner (#2409).
            advance(module.RESPONSE_SECONDS)
            with paused():  # the answer is due now (#2406)
                await bounded(request, "the response (loop clock held)", state)
            assert responses[0]["status"] == 503 and len(consumer._workers) == 1, state()
            async def receive():
                return {"type": "lifespan.shutdown"}
            async def send(message):
                observed.append(message["type"])
            shutdown = asyncio.create_task(app({"type": "lifespan"}, receive, send))
            await asyncio.sleep(0.02)
            assert consumer._stopping and not shutdown.done() and not observed, state()
            denied = []
            await bounded(_raw_challenge(consumer, denied), "the refusal while stopping", state)
            assert denied[0]["status"] == 503
            release.set()
            with paused():  # the drain ends with begin
                await bounded(shutdown, "the lifespan shutdown (loop clock held)", state)
            assert observed == [(True, 0), "lifespan.shutdown.complete"]
        finally:
            release.set()
    asyncio.run(scenario())
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM workspace_challenges").fetchone()[0] == 1


def test_enabled_missing_workspace_schema_has_local_initialization_remedy(http):
    from claudlobby.plane.owner_server import serve, OwnerServerConfigurationError
    app, store, _, _, _ = http
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE workspace_challenges")
    with pytest.raises(OwnerServerConfigurationError, match="host owner initialize-workspace"):
        serve(store.root, origin=HOST, tailscale_binary=__import__('pathlib').Path(__import__('sys').executable),
              workspace_issuer=ISSUER, workspace_origin=PLANE)


def test_started_stage_survives_response_deadline_as_unknown(http, monkeypatch):
    import threading
    import httpx
    from claudlobby.plane import owner_workspace_http as module
    consumer = http[0].workspace_bootstrap
    entered, release = threading.Event(), threading.Event()
    original = consumer.workspace.stage
    def held(*args):
        proposal = original(*args)
        entered.set()
        assert release.wait(30)
        return proposal
    async def scenario():
        advance, paused = move_clock(monkeypatch, module)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http[0]), base_url=HOST) as client:
            challenge = (await post(client, "challenge")).json()
            monkeypatch.setattr(consumer.workspace, "stage", held)
            request = asyncio.create_task(post(client, "stage",
                {"nonce": challenge["nonce"], "assertion": token(http, challenge)}))
            def state():
                return (f"request={status(request)}, entered={entered.is_set()}, "
                        f"workers={len(consumer._workers)}, operations={len(consumer._operations)}")
            try:
                assert await asyncio.to_thread(entered.wait, 10), "waited 10 s for the stage to run: " + state()
                # Its deadline passes once the stage has run. A 50 ms deadline could
                # answer first on a slow runner, before the stage was entered (#2409).
                advance(module.RESPONSE_SECONDS)
                with paused():  # the answer is due now (#2406)
                    response = await bounded(request, "the response (loop clock held)", state)
                assert response.status_code == 503 and response.json() == {"state": "unavailable"}, state()
                await asyncio.sleep(.01)  # a job that ended with its answer would be gone by now
                assert len(consumer._workers) == 1, state()
                assert http[1].workspace().inspect(challenge["nonce"]).nonce == challenge["nonce"]
                release.set()
                with paused():  # the drain ends with the stage
                    await bounded(consumer.shutdown(), "shutdown (loop clock held)", state)
                assert not consumer._workers and not consumer._operations, state()
            finally:
                release.set()
    asyncio.run(scenario())


def test_shutdown_cancels_owned_identity_wait_before_read_lifespan_completion(http, monkeypatch):
    app = http[0]
    consumer = app.workspace_bootstrap
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def held(scope):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        monkeypatch.setattr(consumer, "verify_principal", held)
        responses = []
        request = asyncio.create_task(_raw_challenge(consumer, responses))
        await entered.wait()
        await asyncio.wait_for(consumer.shutdown(), 1)
        await request
        assert cancelled.is_set() and not consumer._workers and not consumer._operations
        assert responses[0]["status"] == 503
    asyncio.run(scenario())
