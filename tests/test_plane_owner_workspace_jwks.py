"""Maintained HTTPX MockTransport proves finite client/cache mechanics, no network."""
import asyncio
import json

import httpx
import pytest

from claudlobby.plane.owner_workspace_jwks import WorkspaceKeyset
from claudlobby.plane.workspace_assertion import AssertionUnavailable, validate_workspace_jwks
from tests.test_plane_workspace_assertion import profile


def test_public_validation_roster_and_private_material_refused(profile):
    key = profile[1]
    assert validate_workspace_jwks(json.dumps({"keys": [key]}).encode()) == frozenset({key["kid"]})
    with pytest.raises(AssertionUnavailable):
        validate_workspace_jwks(json.dumps({"keys": [{**key, "d": key["x"]}]}).encode())


@pytest.mark.parametrize("status,headers,body", [(302, {}, b''),
    (200, {"content-encoding": "gzip"}, b'{}'), (200, {}, b'x' * 65537),
    (200, {}, b'{"keys":[]}')], ids=['redirect','encoding','oversize','empty'])
def test_fetch_refuses_redirect_encoding_size_and_bad_keys(profile, monkeypatch, status, headers, body):
    original = httpx.AsyncClient
    def handler(request):
        assert request.url == "https://accounts.example/api/auth/jwks"
        assert not any(name in request.headers for name in ["cookie", "authorization", "referer"])
        return httpx.Response(status, headers={"content-type": "application/json", **headers}, stream=httpx.ByteStream(body))
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["verify"] is True and kwargs["follow_redirects"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async def scenario():
        with pytest.raises(AssertionUnavailable):
            await WorkspaceKeyset("https://accounts.example").get("invalid")
    asyncio.run(scenario())


def test_singleflight_cache_and_unknown_hint_cooldown(profile, monkeypatch):
    original = httpx.AsyncClient
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(json.dumps({"keys": [profile[1]]}).encode()))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))
    async def scenario():
        cache = WorkspaceKeyset("https://accounts.example")
        values = await asyncio.gather(*(cache.get("invalid") for _ in range(8)))
        assert len(calls) == 1 and len(set(values)) == 1
        await cache.get("invalid")
        assert len(calls) == 1
        cache._fetched = (cache._fetched[0] - 301, cache._fetched[1] - 301)
        with pytest.raises(AssertionUnavailable):
            await cache.get("invalid")
    asyncio.run(scenario())
