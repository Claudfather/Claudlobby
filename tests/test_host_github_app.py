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


def test_issuer_cli_http_exact_grant_and_revocation(tmp_path, capsys):
    from copy import deepcopy
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
    calls = []
    def mint(binding, repository_id):
        calls.append((binding, repository_id))
        return _issuer_token()
    client = TestClient(create_app(store, mint))
    payload = {"schema": 1, "binding": grant["binding"], "revision": grant["binding"]["revision"]}
    headers = {"Authorization": "Bearer " + credential}
    result = client.post("/v1/github/token", json=payload, headers=headers)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert result.json()["revision"] == payload["revision"]
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
    assert revoked["residual_token_expires_at"] == result.json()["expires_at"]
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
    import httpx
    import jwt
    from claudlobby.github_issuer import GithubMinter

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
            return httpx.Response(201, json=_issuer_token(), request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "Client", Client)
    assert GithubMinter({12: key})(_issuer_grant()["binding"], 123)["token"] == "ghs_example_only"


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
