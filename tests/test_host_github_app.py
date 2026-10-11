"""Public GitHub App doors delegate to the existing packaged native owners."""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main


def _issuer_grant():
    return {"grant_id": "grant-example", "workspace_id": "workspace-example",
            "deployment_id": "host-example", "repository_id": 123,
            "expires_at": time.time() + 86400,
            "binding": {"connection_id": "app", "attachment_id": "app",
                        "assignment_id": "project", "bot_id": "engineer", "project": "service",
                        "repo": "example/service", "mode": "managed", "app_id": 12,
                        "installation_id": 34, "permissions": {"contents": "read"},
                        "credential_refs": {"issuer_url_env": "ISSUER_URL", "issuer_token_env": "ISSUER_TOKEN"},
                        "revision": "a" * 64, "slug": None, "bot_user_id": None}}


def _issuer_token():
    from datetime import datetime, timezone
    return {"token": "ghs_example_only", "expires_at": datetime.fromtimestamp(time.time() + 300, timezone.utc).isoformat(),
            "repositories": [{"id": 123, "full_name": "example/service"}], "permissions": {"contents": "read", "metadata": "read"}}


def _consent_flow(tmp_path):
    from claudlobby.github_consent import ConsentFlow, GithubConsentProvider
    from claudlobby.github_issuer import GrantStore

    tmp_path.chmod(0o700)
    secret = tmp_path / "client-secret"
    secret.write_text("example-client-secret")
    secret.chmod(0o600)
    provider = GithubConsentProvider({"app_id": 12, "client_id": "Iv1.example",
                                     "client_secret_path": str(secret), "origin": "https://issuer.example.invalid"})
    return ConsentFlow(GrantStore(tmp_path / "grants.db"), provider)


def test_issuer_browser_consent_only_grants_after_same_browser_callback(tmp_path, capsys):
    from urllib.parse import parse_qs, urlsplit
    from fastapi.testclient import TestClient
    from claudlobby.github_issuer import create_app, IssuanceDenied

    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    for name, content in (("grant.json", json.dumps(grant)), ("credential", credential),
                          ("consent.json", json.dumps(flow.provider.config))):
        path = tmp_path / name
        path.write_text(content)
        path.chmod(0o600)
    assert main(["host", "github-app", "issuer", "invite", "--json", "--store", str(flow.store.path),
                 "--grant-file", str(tmp_path / "grant.json"), "--credential-file", str(tmp_path / "credential"),
                 "--consent-config", str(tmp_path / "consent.json")]) == 0
    output = capsys.readouterr().out
    assert credential not in output and json.loads(output)["command"] == "host.github-app.issuer.invite"
    invitation = json.loads(output)["data"]
    calls = []
    def verify(code, verifier, selected):
        calls.append((code, verifier, selected))
        return 456
    flow.provider.verify = verify
    app = create_app(flow.store, lambda *_: _issuer_token(), flow)
    client = TestClient(app, base_url=flow.provider.origin, follow_redirects=False)
    with pytest.raises(IssuanceDenied):
        flow.store.issue(credential, {"schema": 1, "binding": grant["binding"], "revision": "a" * 64}, lambda *_: pytest.fail("invitation minted"))
    page = client.get(invitation["authorization_url"])
    assert page.status_code == 200 and "example/service" in page.text and "engineer" in page.text
    assert all(flag in page.headers["set-cookie"] for flag in ("Secure", "HttpOnly", "SameSite=lax"))
    assert client.post("/v1/github/connect", data={"ticket": invitation["ticket"]}, headers={"Origin": "https://other.example"}).status_code == 403
    start = client.post("/v1/github/connect", data={"ticket": invitation["ticket"]}, headers={"Origin": flow.provider.origin})
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert start.status_code == 303 and query["code_challenge_method"] == ["S256"]
    abandoned = query["state"][0]
    returned = client.get(invitation["authorization_url"])
    assert "Authorize with GitHub" in returned.text and "set-cookie" not in returned.headers
    stranger = TestClient(app, base_url=flow.provider.origin, follow_redirects=False)
    stranger.get(invitation["authorization_url"])
    assert stranger.post("/v1/github/connect", data={"ticket": invitation["ticket"]}, headers={"Origin": flow.provider.origin}).status_code == 403
    retry = client.post("/v1/github/connect", data={"ticket": invitation["ticket"]}, headers={"Origin": flow.provider.origin})
    query = parse_qs(urlsplit(retry.headers["location"]).query)
    assert query["state"][0] != abandoned
    assert client.get("/v1/github/callback?state=" + abandoned + "&code=old-code").status_code == 403 and calls == []
    callback = "/v1/github/callback?" + "state=" + query["state"][0] + "&code=example-code"
    assert stranger.get(callback).status_code == 403 and calls == []
    complete = client.get(callback)
    assert complete.status_code == 303 and complete.headers["location"] == invitation["authorization_url"]
    assert "GitHub is connected" in client.get(complete.headers["location"]).text
    assert client.get(callback).status_code == 403 and len(calls) == 1
    assert calls[0][0] == "example-code" and calls[0][2] == grant
    from hashlib import sha256
    from base64 import urlsafe_b64encode
    assert query["code_challenge"] == [urlsafe_b64encode(sha256(calls[0][1].encode()).digest()).decode().rstrip("=")]
    result = flow.store.issue(credential, {"schema": 1, "binding": grant["binding"], "revision": "a" * 64}, lambda *_: _issuer_token())
    assert result["token"] == "ghs_example_only"
    saved = flow.store.path.read_bytes()
    assert credential.encode() not in saved and b"example-code" not in saved
    with flow.store.transaction() as db:
        assert tuple(db.execute("SELECT verifier,state_digest,browser_digest FROM github_consent").fetchone()) == (None, None, None)
    flow.store.revoke(grant["grant_id"])
    assert "Connection removed" in client.get(invitation["authorization_url"]).text


def test_issuer_consent_cancel_and_failed_verification_never_activate(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    from claudlobby.github_issuer import IssuanceDenied

    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    invitation = flow.invite(grant, credential)
    ticket = invitation["ticket"]
    def begin():
        _, cookie = flow.page(ticket)
        query = parse_qs(urlsplit(flow.begin(ticket, cookie)).query)
        return query["state"][0], cookie
    state, cookie = begin()
    flow.provider.verify = lambda *_: (_ for _ in ()).throw(IssuanceDenied("not an administrator"))
    flow.complete(state, cookie, "denied-code")
    assert flow.page(ticket)[0]["status"] == "invited"
    with pytest.raises(IssuanceDenied):
        flow.complete(state, cookie, "replay-code")
    state, cookie = begin()
    def cancelled(*_):
        flow.cancel(ticket)  # cancellation commits while the provider is in flight
        return 456
    flow.provider.verify = cancelled
    flow.complete(state, cookie, "cancelled-code")
    assert flow.page(ticket)[0]["status"] == "cancelled"
    with pytest.raises(IssuanceDenied):
        flow.store.issue(credential, {"schema": 1, "binding": grant["binding"], "revision": "a" * 64}, lambda *_: pytest.fail("cancelled consent minted"))


def test_issuer_consent_verifies_provider_user_installation_and_repo_admin(tmp_path, monkeypatch):
    from copy import deepcopy
    import httpx
    from claudlobby.github_issuer import IssuanceDenied

    flow, grant = _consent_flow(tmp_path), _issuer_grant()
    installation = {"id": 34, "app_id": 12, "permissions": {"contents": "read"}, "suspended_at": None}
    repository = {"id": 123, "full_name": "example/service", "permissions": {"admin": True}}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 15}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, *, headers, data):
            assert url == "https://github.com/login/oauth/access_token"
            assert data == {"client_id": "Iv1.example", "client_secret": "example-client-secret",
                            "redirect_uri": flow.provider.origin + "/v1/github/callback", "code": "example-code",
                            "code_verifier": "v" * 43, "repository_id": 123}
            return httpx.Response(200, json={"access_token": "ghu_example", "refresh_token": "unused"}, request=httpx.Request("POST", url))
        def get(self, url, *, headers):
            assert headers["Authorization"] == "Bearer ghu_example"
            bodies = {"https://api.github.com/user": {"id": 456},
                      "https://api.github.com/user/installations?per_page=100&page=1": {"installations": [installation]},
                      "https://api.github.com/user/installations/34/repositories?per_page=100&page=1": {"repositories": [repository]},
                      "https://api.github.com/repos/example/service": repository}
            return httpx.Response(200, json=bodies[url], request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "Client", Client)
    assert flow.provider.verify("example-code", "v" * 43, grant) == 456
    for target, changes in ((installation, {"app_id": 13}), (installation, {"permissions": {}}),
                            (repository, {"id": 124}), (repository, {"full_name": "other/service"}),
                            (repository, {"permissions": {"admin": False}})):
        before = deepcopy(target)
        target.update(changes)
        with pytest.raises(IssuanceDenied):
            flow.provider.verify("example-code", "v" * 43, grant)
        target.clear()
        target.update(before)


def test_issuer_consent_conflicts_fail_before_login_or_require_new_invitation(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    from claudlobby.github_consent import _page
    from claudlobby.github_issuer import GrantConflict

    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    invitation = flow.invite(grant, credential)
    _, cookie = flow.page(invitation["ticket"])
    state = parse_qs(urlsplit(flow.begin(invitation["ticket"], cookie)).query)["state"][0]
    # The operator creates a conflicting grant while GitHub verification is in flight.
    def verify(*_):
        flow.store.approve(grant, credential)
        flow.store.revoke(grant["grant_id"])
        return 456
    flow.provider.verify = verify
    flow.complete(state, cookie, "example-code")
    row, _ = flow.page(invitation["ticket"])
    assert row["status"] == "needs_operator" and "new invitation" in _page(row)
    with pytest.raises(GrantConflict, match="grant ID already exists"):
        flow.invite(grant, credential)
    with pytest.raises(GrantConflict, match="another deployment"):
        flow.invite({**grant, "grant_id": "new-grant", "deployment_id": "different-host"}, credential)
    with flow.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM github_consent").fetchone()[0] == 1
        assert db.execute("SELECT revoked FROM github_grants").fetchone()[0] == 1


def _approve_consent(flow, grant, credential, user_id=456):
    from urllib.parse import parse_qs, urlsplit
    ticket = flow.invite(grant, credential)["ticket"]
    _, cookie = flow.page(ticket)
    state = parse_qs(urlsplit(flow.begin(ticket, cookie)).query)["state"][0]
    flow.provider.verify = lambda *_: user_id
    flow.complete(state, cookie, "example-code")
    return ticket


def _webhook(flow, tmp_path):
    from claudlobby.github_webhooks import GithubWebhooks
    secret = tmp_path / "webhook-secret"
    secret.write_bytes(b"example-webhook-secret-for-tests-only")
    secret.chmod(0o600)
    return GithubWebhooks(flow.store, {12: secret})


def _signed_event(payload):
    import hashlib
    import hmac
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"example-webhook-secret-for-tests-only", raw, hashlib.sha256).hexdigest()
    return raw, signature


def test_issuer_signed_user_revocation_and_replay_preserve_other_grants(tmp_path):
    from fastapi.testclient import TestClient
    from claudlobby.github_issuer import create_app, IssuanceDenied
    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    ticket = _approve_consent(flow, grant, credential)
    other = {**grant, "grant_id": "other-user", "binding": {**grant["binding"], "bot_id": "reviewer"}}
    _approve_consent(flow, other, credential, user_id=457)
    hook = _webhook(flow, tmp_path)
    client = TestClient(create_app(flow.store, lambda *_: _issuer_token(), flow, hook))
    raw, signature = _signed_event({"action": "revoked", "sender": {"id": 456}})
    headers = {"content-type": "application/json", "x-github-event": "github_app_authorization", "x-hub-signature-256": signature,
               "x-github-delivery": "00000000-0000-4000-8000-000000000001"}
    assert client.post("/v1/github/webhook/12", content=raw+b" ", headers=headers).status_code == 403
    assert client.post("/v1/github/webhook/13", content=raw, headers=headers).status_code == 403
    assert client.post("/v1/github/webhook/12", content=raw, headers=headers).json() == {"received": True}
    assert flow.page(ticket)[0]["status"] == "revoked"
    with pytest.raises(IssuanceDenied):
        flow.store.issue(credential, {"schema": 1, "binding": grant["binding"], "revision": "a"*64}, lambda *_: pytest.fail("revoked grant minted"))
    assert flow.store.issue(credential, {"schema": 1, "binding": other["binding"], "revision": "a"*64}, lambda *_: _issuer_token())
    fresh = {**grant, "grant_id": "fresh-consent"}
    fresh_ticket = _approve_consent(flow, fresh, credential)
    assert client.post("/v1/github/webhook/12", content=raw, headers=headers).status_code == 200
    assert flow.page(fresh_ticket)[0]["status"] == "approved"
    with flow.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM github_revocations").fetchone()[0] == 1
    # A later genuine revocation may have identical body bytes but a new delivery ID.
    assert client.post("/v1/github/webhook/12", content=raw, headers={**headers, "x-github-delivery": "00000000-0000-4000-8000-000000000002"}).status_code == 200
    assert flow.page(fresh_ticket)[0]["status"] == "revoked"


def test_issuer_installation_and_repository_revocations_are_scoped(tmp_path):
    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    flow.store.approve(grant, credential)
    other = {**grant, "grant_id": "other-repo", "repository_id": 124,
             "binding": {**grant["binding"], "repo": "example/other"}}
    flow.store.approve(other, credential)
    hook = _webhook(flow, tmp_path)
    def event(kind, payload):
        import uuid
        raw, signature = _signed_event(payload)
        hook.receive(12, kind, str(uuid.uuid4()), signature, raw)
    event("installation_repositories", {"action": "removed", "installation": {"id": 34, "app_id": 12}, "repositories_removed": [{"id": 123}]})
    with flow.store.transaction() as db:
        assert dict(db.execute("SELECT grant_id,revoked FROM github_grants")) == {"grant-example": 1, "other-repo": 0}
    event("installation", {"action": "suspend", "installation": {"id": 34, "app_id": 12}})
    event("installation", {"action": "unsuspend", "installation": {"id": 34, "app_id": 12}})
    with flow.store.transaction() as db:
        assert all(row[0] == 1 for row in db.execute("SELECT revoked FROM github_grants"))
    with pytest.raises(ValueError):
        event("installation", {"action": "deleted", "installation": {"id": 34, "app_id": 13}})


def test_issuer_revocation_during_consent_prevents_late_approval(tmp_path):
    from urllib.parse import parse_qs, urlsplit
    flow, grant, credential = _consent_flow(tmp_path), _issuer_grant(), "c" * 43
    invitation = flow.invite(grant, credential)
    _, cookie = flow.page(invitation["ticket"])
    state = parse_qs(urlsplit(flow.begin(invitation["ticket"], cookie)).query)["state"][0]
    hook = _webhook(flow, tmp_path)
    def verify(*_):
        raw, signature = _signed_event({"action": "revoked", "sender": {"id": 456}})
        hook.receive(12, "github_app_authorization", "00000000-0000-4000-8000-000000000001", signature, raw)
        return 456
    flow.provider.verify = verify
    flow.complete(state, cookie, "example-code")
    assert flow.page(invitation["ticket"])[0]["status"] == "needs_operator"
    with flow.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM github_grants").fetchone()[0] == 0
    fresh_ticket = _approve_consent(flow, {**grant, "grant_id": "new-grant"}, credential)
    assert flow.page(fresh_ticket)[0]["status"] == "approved"


def test_issuer_cli_http_exact_grant_and_revocation(tmp_path, capsys):
    from copy import deepcopy
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    from fastapi.testclient import TestClient
    from claudlobby.github_issuer import GrantStore, create_app

    tmp_path.chmod(0o700)
    grant, credential = _issuer_grant(), "example_" + "a" * 43
    for name, text in (("grant.json", json.dumps(grant)), ("credential", credential)):
        path = tmp_path / name
        path.write_text(text)
        path.chmod(0o600)
    store = GrantStore(tmp_path / "grants.db")
    assert main(["host", "github-app", "issuer", "approve", "--json", "--store", str(store.path),
                 "--grant-file", str(tmp_path / "grant.json"), "--credential-file", str(tmp_path / "credential")]) == 0
    output = capsys.readouterr()
    assert credential not in output.out + output.err
    assert json.loads(output.out)["command"] == "host.github-app.issuer.approve"
    calls, provider_expiries = [], []
    def mint(binding, repository_id):
        calls.append((binding, repository_id))
        result = _issuer_token()
        result["_provider_expires_at"] = datetime.fromisoformat(result["expires_at"]).timestamp() + 45
        provider_expiries.append(datetime.fromtimestamp(result["_provider_expires_at"], timezone.utc).isoformat())
        return result
    client = TestClient(create_app(store, mint))
    payload = {"schema": 1, "binding": grant["binding"], "revision": grant["binding"]["revision"]}
    headers = {"Authorization": "Bearer " + credential}
    result = client.post("/v1/github/token", json=payload, headers=headers)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["revision"] == payload["revision"]
    assert 0 < (datetime.fromisoformat(result.json()["expires_at"]) - parsedate_to_datetime(result.headers["date"])).total_seconds() <= 3600
    assert calls == [(grant["binding"], 123)]
    # A valid credential cannot assert authority over another bot/repo/App/scope.
    for key, value in (("bot_id", "other"), ("repo", "other/service"), ("installation_id", 35),
                       ("app_id", 13), ("permissions", {"contents": "write"}), ("revision", "b" * 64)):
        changed = deepcopy(payload)
        changed["binding"][key] = value
        changed["revision"] = changed["binding"]["revision"]
        assert client.post("/v1/github/token", json=changed, headers=headers).status_code == 403
    assert client.post("/v1/github/token", json=payload).status_code == 403
    assert client.post("/v1/github/token", json=payload, headers={**headers, "Origin": "https://example.invalid"}).status_code == 403
    assert len(calls) == 1
    assert main(["host", "github-app", "issuer", "revoke", "--json", "--store", str(store.path), "--grant-id", grant["grant_id"]]) == 0
    revoked = json.loads(capsys.readouterr().out)["data"]
    assert revoked["new_issuance"] == "blocked" and revoked["host_enforcement"] == "unconfirmed"
    assert revoked["residual_token_expires_at"] == provider_expiries[0]
    assert client.post("/v1/github/token", json=payload, headers=headers).status_code == 403
    assert len(calls) == 1
    assert credential.encode() not in store.path.read_bytes()
    assert b"ghs_example_only" not in store.path.read_bytes()


def test_issuer_scope_checks_and_revoke_serializes_with_mint(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from claudlobby.github_issuer import GrantStore, IssuanceDenied, IssuerUnavailable

    tmp_path.chmod(0o700)
    store, grant, credential = GrantStore(tmp_path / "grants.db"), _issuer_grant(), "a" * 43
    store.approve(grant, credential)
    payload = {"schema": 1, "binding": grant["binding"], "revision": grant["binding"]["revision"]}
    for changed in ({"repositories": [{"id": 456, "full_name": "example/service"}]},
                    {"permissions": {"contents": "write"}}, {"expires_at": "invalid"}):
        with pytest.raises(IssuerUnavailable):
            store.issue(credential, payload, lambda *_: {**_issuer_token(), **changed})
    with pytest.raises(IssuanceDenied):
        store.approve({**grant, "grant_id": "other", "deployment_id": "another-host"}, credential)
    started, release, revoking = Event(), Event(), Event()
    def mint(*_):
        started.set()
        assert release.wait(5)
        return _issuer_token()
    def revoke():
        revoking.set()
        return store.revoke(grant["grant_id"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        issued = executor.submit(store.issue, credential, payload, mint)
        assert started.wait(5)
        revoked = executor.submit(revoke)
        assert revoking.wait(5)
        assert not revoked.done()
        release.set()
        result = issued.result(5)
        assert revoked.result(5)["residual_token_expires_at"] == result["expires_at"]
    with pytest.raises(IssuanceDenied):
        store.issue(credential, payload, lambda *_: pytest.fail("revoked grant minted"))
    with pytest.raises(IssuanceDenied):
        store.approve(grant, credential)


def test_issuer_minter_uses_server_key_repository_id_and_no_proxy(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from email.utils import formatdate
    import httpx
    import jwt
    from claudlobby.github_issuer import GithubMinter

    # GitHub is 45 seconds ahead of the issuer. A full-lifetime token must
    # still work, while retaining the actual provider expiry for revocation.
    provider_now = int(time.time()) + 45
    raw = {**_issuer_token(), "expires_at": datetime.fromtimestamp(provider_now + 3600, timezone.utc).isoformat()}

    key = tmp_path / "app.pem"
    key.write_bytes(b"test-key-only")
    key.chmod(0o600)
    monkeypatch.setattr(jwt, "encode", lambda claims, contents, algorithm: "test-jwt" if contents == b"test-key-only" and claims["iss"] == "12" and algorithm == "RS256" else pytest.fail("wrong key"))
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 15}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, *, headers, json):
            assert url == "https://api.github.com/app/installations/34/access_tokens"
            assert headers["Authorization"] == "Bearer test-jwt"
            assert json == {"repository_ids": [123], "permissions": {"contents": "read"}}
            return httpx.Response(201, json=raw, headers={"Date": formatdate(provider_now, usegmt=True)}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "Client", Client)
    result = GithubMinter({12: key})(_issuer_grant()["binding"], 123)
    assert result["token"] == "ghs_example_only"
    assert 3590 < datetime.fromisoformat(result["expires_at"]).timestamp() - time.time() < 3600
    assert result["_provider_expires_at"] == provider_now + 3600


def test_setup_uses_packaged_native_owner_and_does_not_echo_secret(tmp_path, monkeypatch, capsys):
    from claudlobby import context
    from claudlobby.commands import host_github_app

    root = tmp_path / "root"
    native = tmp_path / "packaged" / "lib"
    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=root, lib=native))
    calls = []

    def invoke(script, argv, selected_root, *, timeout):
        calls.append((script, argv, selected_root, timeout))
        return subprocess.CompletedProcess(argv, 0, "6/6 done. Wire the fleet:\n", "")

    monkeypatch.setattr(host_github_app, "_invoke", invoke)
    assert main(["--root", str(root), "host", "github-app", "setup", "--json",
                 "--app-id", "123", "--installation-id", "456",
                 "--private-key", str(tmp_path / "private.pem"), "--slug", "fleet-app",
                 "--no-write-config"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "host.github-app.setup"
    assert result["data"]["configured"] is False
    assert calls == [(native / "setup-github-app.sh",
                      ["--app-id", "123", "--installation-id", "456",
                       "--private-key", str(tmp_path / "private.pem"),
                       "--slug", "fleet-app", "--no-write-config"], root, 180)]


def test_token_is_explicit_and_failed_mint_does_not_disclose_native_output(
    tmp_path, monkeypatch, capsys
):
    from claudlobby import context
    from claudlobby.commands import host_github_app

    root = tmp_path / "root"
    native = tmp_path / "packaged" / "lib"
    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(root=root, lib=native))
    calls = []

    def mint(script, argv, selected_root, *, timeout):
        calls.append((script, argv, selected_root, timeout))
        return subprocess.CompletedProcess(argv, 0, "ghs_private-test-token", "")

    monkeypatch.setattr(host_github_app, "_invoke", mint)
    assert main(["--root", str(root), "host", "github-app", "token"]) == 0
    assert capsys.readouterr().out == "ghs_private-test-token\n"
    assert calls == [(native / "mint-github-token.sh", [], root, 60)]

    monkeypatch.setattr(host_github_app, "_invoke", lambda *a, **k:
                        subprocess.CompletedProcess([], 1, "ghs_private-test-token", "key path or provider secret"))
    assert main(["--root", str(root), "host", "github-app", "token", "--json"]) == 6
    output = capsys.readouterr()
    assert "ghs_private-test-token" not in output.out + output.err
    assert "provider secret" not in output.out + output.err
    assert json.loads(output.out)["data"] == {}


def test_setup_refuses_generated_bot_before_native_effect(tmp_path, monkeypatch, capsys):
    from claudlobby.commands import host_github_app

    monkeypatch.setenv("BOT_ID", "worker")
    monkeypatch.setattr(host_github_app, "_invoke", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("native setup was invoked")))
    assert main(["--root", str(tmp_path), "host", "github-app", "setup",
                 "--app-id", "123", "--installation-id", "456",
                 "--private-key", "key.pem", "--slug", "fleet-app"]) == 4
    assert "operator shell" in capsys.readouterr().err


def test_setup_redacts_provider_body_but_keeps_jwt_diagnostic(tmp_path, monkeypatch, capsys):
    from claudlobby import context
    from claudlobby.commands import host_github_app

    monkeypatch.setattr(context, "resolve_paths", lambda **kwargs: SimpleNamespace(
        root=tmp_path, lib=tmp_path / "packaged" / "lib"))
    monkeypatch.setattr(host_github_app, "_invoke", lambda *a, **k:
                        subprocess.CompletedProcess([], 1, "", "Most likely causes: private-provider-body"))
    assert main(["--root", str(tmp_path), "host", "github-app", "setup", "--json",
                 "--app-id", "123", "--installation-id", "456",
                 "--private-key", "key.pem", "--slug", "fleet-app"]) == 4
    output = capsys.readouterr()
    assert "private-provider-body" not in output.out + output.err
    assert "JWT" in json.loads(output.out)["error"]["message"]


def test_setup_syntax_error_does_not_echo_key_argument(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--json", "host", "github-app", "setup",
              "--private-key", "private-key-location",
              "--unsupported", "private-value"])
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "private-key-location" not in output.out + output.err
    assert "private-value" not in output.out + output.err
    assert json.loads(output.out)["command"] == "host.github-app.setup"
