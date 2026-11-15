"""Fresh isolated authority and HTTP; trusted identities are synthetic, not a tailnet."""

import asyncio
import pytest
from fastapi.testclient import TestClient

from claudlobby.plane.ids import ensure_host_uid
from claudlobby.plane.owner_access import AccessDenied, OwnerAccess, PrincipalRef
from claudlobby.plane.owner_browser import create_owner_browser_app
from claudlobby.plane.owner_workspace_http import WorkspaceBootstrapConfig
from tests.package_fixtures import source_package
from tests.test_plane_owner_browser import _raw_http


HOST = "https://host.example.test"
ORIGIN = "https://plane.example.test"
PATH = "/api/owner/bootstrap/pair"
PRINCIPAL = PrincipalRef("tailscale:controlplane.tailscale.com", "1234")


@pytest.fixture
def fresh(tmp_path):
    ensure_host_uid(tmp_path / "state")
    access = OwnerAccess.initialize(tmp_path, before_first_fleet=True)
    identity = [PRINCIPAL]

    async def verify(scope):
        if isinstance(identity[0], Exception):
            raise identity[0]
        return identity[0]

    return tmp_path, access, identity, verify


def app_for(fresh, **kwargs):
    root, _, _, verify = fresh
    return create_owner_browser_app(root, verify_principal=verify,
        expected_origin=HOST, package=source_package(), pairing_origin=ORIGIN, **kwargs)


def post(client, *, headers=None, body=b"{}", path=PATH):
    values = {"Origin": ORIGIN, "Content-Type": "application/json", "X-Claudlobby-Owner": "1"}
    values.update(headers or {})
    return client.post(path, content=body, headers=values)


def retained(access):
    with access._connection() as conn:
        return {"tables": {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                "grants": conn.execute("SELECT count(*) FROM grants").fetchone()[0],
                "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
                "challenges": conn.execute("SELECT count(*) FROM challenges").fetchone()[0]}


def test_fresh_pairing_mounts_without_workspace_and_only_retains_challenge(fresh):
    _, access, _, _ = fresh
    before = retained(access)
    with TestClient(app_for(fresh), base_url=HOST) as client:
        result = post(client)
        assert result.status_code == 202, result.text
        body = result.json()
        assert body["state"] == "awaiting_local_approval"
        assert body["principal"] == {"namespace": PRINCIPAL.namespace, "subject": PRINCIPAL.subject}
        assert len(body["challenge"]) == 43 and body["expires_at"] > access._now()
        assert "set-cookie" not in result.headers
        assert "location" not in result.headers
        assert "no-store" in result.headers["cache-control"]
        # Local inspection is test-only: there is no corresponding HTTP door.
        assert access.inspect_pairing(body["challenge"]).principal == PRINCIPAL
        for route in ("/api/owner/bootstrap/inspect", "/api/owner/bootstrap/confirm"):
            assert post(client, path=route).status_code != 202
    after = retained(access)
    assert after == {**before, "challenges": before["challenges"] + 1}
    assert not any(name.startswith(("workspace_", "connected_", "setup_")) for name in after["tables"])
    assert access.current_grant() is None


@pytest.mark.parametrize("headers", [
    {"Host": "other.example.test"}, {"Origin": "https://other.example.test"},
    {"Origin": HOST}, {"Origin": ORIGIN + "/"}, {"X-Claudlobby-Owner": "0"},
    {"Content-Type": "text/plain"}, {"Cookie": "unrelated=x"}, {"Authorization": "Bearer synthetic"},
])
def test_pairing_refuses_foreign_or_credential_headers_without_effect(fresh, headers):
    _, access, _, _ = fresh
    with TestClient(app_for(fresh), base_url=HOST) as client:
        assert post(client, headers=headers).status_code == 403
    assert retained(access)["challenges"] == 0 and access.current_grant() is None


@pytest.mark.parametrize("body", [b"", b"null", b"[]", b"true", b'{"principal":"1234"}',
    b'{"x":1,"x":2}', b"{}{}", b"\xff", b" " * 1024 + b"{}"])
def test_pairing_requires_one_bounded_empty_utf8_json_object(fresh, body):
    _, access, _, _ = fresh
    with TestClient(app_for(fresh), base_url=HOST) as client:
        assert post(client, body=body).status_code == 400
    assert retained(access)["challenges"] == 0


@pytest.mark.parametrize("field", ["host", "origin", "content-type", "x-claudlobby-owner", "cookie", "authorization"])
def test_pairing_rejects_missing_or_duplicated_security_headers(fresh, field):
    _, access, _, _ = fresh
    headers = [(b"host", b"host.example.test"), (b"origin", ORIGIN.encode()),
               (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]
    app = app_for(fresh)
    variants = [headers + [(field.encode(), b"unexpected")]]
    if field not in {"cookie", "authorization"}:
        variants.append([item for item in headers if item[0] != field.encode()])
    for raw in variants:
        messages = asyncio.run(_raw_http(app, PATH, raw_headers=raw))
        assert next(x for x in messages if x["type"] == "http.response.start")["status"] == 403
    assert retained(access)["challenges"] == 0


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT"])
def test_pairing_post_only_and_query_refusal(fresh, method):
    _, access, _, _ = fresh
    with TestClient(app_for(fresh), base_url=HOST) as client:
        result = client.request(method, PATH, headers={"Origin": ORIGIN})
        assert result.status_code == 405 and result.headers["allow"] == "POST"
        assert "access-control-allow-methods" not in result.headers
        assert post(client, path=PATH + "?principal=1234").status_code == 403
    assert retained(access)["challenges"] == 0


@pytest.mark.parametrize("identity", [None, PrincipalRef("untrusted", "1234"),
    PrincipalRef(PRINCIPAL.namespace, "01234"), AccessDenied("synthetic_refusal")])
def test_pairing_requires_verified_native_human_principal(fresh, identity):
    _, access, selected, _ = fresh
    app = app_for(fresh)
    selected[0] = identity
    with TestClient(app, base_url=HOST) as client:
        assert post(client).status_code == 403
    assert retained(access)["challenges"] == 0


def test_pairing_refuses_active_owner_at_mount_and_after_mount(fresh):
    _, access, _, _ = fresh
    app = app_for(fresh)
    challenge = access.begin_pairing(PRINCIPAL)
    owner = access.confirm_pairing(challenge.token, expected_principal=PRINCIPAL)
    before = retained(access)
    with pytest.raises(ValueError, match="active owner"):
        app_for(fresh)
    with TestClient(app, base_url=HOST) as client:
        assert post(client).status_code == 403
    assert retained(access) == before and access.current_grant() == owner


def test_pairing_mixed_configuration_refuses_without_initializing_workspace(fresh):
    _, access, _, _ = fresh
    before = retained(access)
    with pytest.raises(ValueError, match="pairing.*workspace"):
        app_for(fresh, workspace_bootstrap=WorkspaceBootstrapConfig("https://site.example.test", ORIGIN))
    assert retained(access) == before


@pytest.mark.parametrize("origin", ["http://plane.example.test", "https://*.example.test",
    ORIGIN + "/", "https://user@plane.example.test", ORIGIN + "?other=1"])
def test_pairing_configuration_requires_one_exact_https_origin(fresh, origin):
    root, access, _, verify = fresh
    before = retained(access)
    with pytest.raises(ValueError):
        create_owner_browser_app(root, verify_principal=verify, expected_origin=HOST,
            package=source_package(), pairing_origin=origin)
    assert retained(access) == before


def test_pairing_rechecks_source_and_principal_before_disclosing_challenge(fresh):
    root, access, selected, _ = fresh
    app = app_for(fresh)
    calls = []

    async def drift(_scope):
        calls.append(1)
        return PRINCIPAL if len(calls) == 1 else PrincipalRef(PRINCIPAL.namespace, "5678")

    app.pairing_bootstrap.verify_principal = drift
    with TestClient(app, base_url=HOST) as client:
        response = post(client)
        assert response.status_code == 403 and "challenge" not in response.json()
    assert access.current_grant() is None
    changed = app_for(fresh)
    (root / "state/config.json").write_text("{}")
    selected[0] = PRINCIPAL
    with TestClient(changed, base_url=HOST) as client:
        assert post(client).status_code in {403, 503}
    assert access.current_grant() is None


def test_pairing_timeout_retains_and_drains_started_challenge_without_grant(fresh, monkeypatch):
    import threading
    _, access, _, _ = fresh
    app = app_for(fresh)
    handler = app.pairing_bootstrap
    entered, release = threading.Event(), threading.Event()
    original = handler.access.begin_pairing

    def held(principal):
        challenge = original(principal)
        entered.set()
        assert release.wait(5)
        return challenge

    monkeypatch.setattr(handler.access, "begin_pairing", held)
    monkeypatch.setattr(handler, "response_seconds", lambda scope: 0.05)

    async def scenario():
        request = asyncio.create_task(_raw_http(app, PATH, raw_headers=[
            (b"host", b"host.example.test"), (b"origin", ORIGIN.encode()),
            (b"content-type", b"application/json"), (b"x-claudlobby-owner", b"1")]))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            messages = await asyncio.wait_for(request, 2)
            assert next(x for x in messages if x["type"] == "http.response.start")["status"] == 503
            stopping = asyncio.create_task(handler.shutdown())
            await asyncio.sleep(0.02)
            assert not stopping.done()
            release.set()
            await asyncio.wait_for(stopping, 2)
            assert not handler._workers
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)

    asyncio.run(scenario())
    assert retained(access)["challenges"] == 1
    assert access.current_grant() is None and retained(access)["sessions"] == 0
