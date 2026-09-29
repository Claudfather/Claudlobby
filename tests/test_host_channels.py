"""The public channel approval path never touches real managed settings."""

from __future__ import annotations

import json
import os
import stat

import pytest

from claudlobby.__main__ import main


@pytest.fixture(autouse=True)
def _operator(monkeypatch):
    from claudlobby.commands import operator_context

    monkeypatch.setattr(operator_context, "require_operator_context", lambda _root: None)


def _call(tmp_path, capsys, action):
    code = main(["--root", str(tmp_path), "host", "channels", action, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_managed_approvals_preserve_policy_and_are_idempotent(tmp_path, monkeypatch, capsys):
    from claudlobby.commands import host_channels

    target = tmp_path / "managed-settings.json"
    monkeypatch.setattr(host_channels, "_managed_path", lambda: target)
    original = {"permissions": {"deny": ["Read(./secrets/**)"]},
                "channelsEnabled": False,
                "allowedChannelPlugins": [{"marketplace": "acme", "plugin": "internal"},
                                          {"marketplace": "claude-plugins-official", "plugin": "telegram"}]}
    target.write_text(json.dumps(original))
    os.chmod(target, 0o640)
    identity = target.stat()
    before = target.read_bytes()
    code, result = _call(tmp_path, capsys, "check")
    assert code == 0 and result["command"] == "host.channels.check"
    assert result["data"]["approved"] is False
    assert result["data"]["missing"] == [{"marketplace": "claudfather-plugins", "plugin": "telegram"}]
    assert target.read_bytes() == before

    code, result = _call(tmp_path, capsys, "approve")
    assert code == 0 and result["data"]["changed"] is True
    written = json.loads(target.read_text())
    assert written["permissions"] == original["permissions"]
    assert written["channelsEnabled"] is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert (target.stat().st_uid, target.stat().st_gid) == (identity.st_uid, identity.st_gid)
    assert written["allowedChannelPlugins"] == [*original["allowedChannelPlugins"],
                                                 {"marketplace": "claudfather-plugins", "plugin": "telegram"}]
    first = target.read_bytes()
    code, result = _call(tmp_path, capsys, "approve")
    assert code == 0 and result["data"]["changed"] is False
    assert target.read_bytes() == first


def test_invalid_existing_policy_refuses_without_write(tmp_path, monkeypatch, capsys):
    from claudlobby.commands import host_channels

    target = tmp_path / "managed-settings.json"
    monkeypatch.setattr(host_channels, "_managed_path", lambda: target)
    target.write_text('{"allowedChannelPlugins": [')
    before = target.read_bytes()
    code, result = _call(tmp_path, capsys, "approve")
    assert code == 4 and result["error"]["code"] == "conflict"
    assert target.read_bytes() == before
    target.write_text('{"allowedChannelPlugins": "wrong"}')
    before = target.read_bytes()
    code, result = _call(tmp_path, capsys, "approve")
    assert code == 4 and result["error"]["code"] == "conflict"
    assert target.read_bytes() == before


def test_approve_requires_admin_writable_directory_and_operator(tmp_path, monkeypatch, capsys):
    from claudlobby.commands import host_channels

    target = tmp_path / "missing" / "managed-settings.json"
    monkeypatch.setattr(host_channels, "_managed_path", lambda: target)
    code, result = _call(tmp_path, capsys, "approve")
    assert code == 6 and "host administrator" in result["error"]["hint"]
    assert not target.parent.exists()
    target.parent.mkdir()
    code, result = _call(tmp_path, capsys, "approve")
    assert code == 0 and result["data"]["approved"] is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    monkeypatch.setenv("BOT_ID", "worker")
    code, result = _call(tmp_path, capsys, "check")
    assert code == 4 and "operator shell" in result["error"]["hint"]
