"""Environment previews reveal keys and destinations, never value fragments."""

from argparse import Namespace
from types import SimpleNamespace

from claudlobby.commands import env_migrate


def test_preview_never_prints_any_secret_bytes(tmp_path, monkeypatch, caplog):
    secret = "very-private-token-517"
    paths = SimpleNamespace(root=tmp_path, fleet_dir=None,
                            bot_runtime=lambda name: tmp_path / "runtime" / name)
    fleet = SimpleNamespace(name="example")
    monkeypatch.setattr(env_migrate, "_migration_preamble",
                        lambda args: (paths, fleet, tmp_path / "legacy", {}))
    monkeypatch.setattr(env_migrate, "_discover_legacy_bot_dirs",
                        lambda source: {"bot": source / "bot"})
    monkeypatch.setattr(env_migrate, "_extract_secrets",
                        lambda source, bots: ({}, {}, {}))
    monkeypatch.setattr(env_migrate, "_resolve_fleet_vars",
                        lambda *args: {"API_TOKEN": secret})
    monkeypatch.setattr(env_migrate, "_resolve_bot_vars",
                        lambda *args: ({"bot": {"BOT_TOKEN": secret}}, []))

    with caplog.at_level("INFO", logger="claudlobby"):
        assert env_migrate.cmd_env_migrate(Namespace(apply=False)) == 0
    assert "API_TOKEN=<set>" in caplog.text
    assert "BOT_TOKEN=<set>" in caplog.text
    assert secret not in caplog.text
    assert "very" not in caplog.text and "=17" not in caplog.text
