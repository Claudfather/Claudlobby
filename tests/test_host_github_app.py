"""Public GitHub App doors delegate to the existing packaged native owners."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby.__main__ import main


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
