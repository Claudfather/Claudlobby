"""Canonical proposal storage/crypto with synthetic ingress and public key bytes."""
import asyncio
from dataclasses import replace
import json
import sqlite3
import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef
from claudlobby.plane.owner_browser import create_owner_browser_app
from claudlobby.plane.owner_source import bind_source
from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
from tests.test_plane_two_fleets import _Sampler, _seed
from tests.test_plane_workspace_assertion import profile, b64

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
