"""Unsupported roster removal leaves retained state and native supervision alone."""
import json

import pytest

from claudlobby.__main__ import main


@pytest.mark.parametrize("purge", [False, True])
def test_remove_refuses_before_any_retained_state_or_native_effect(tmp_path, monkeypatch, capsys, purge):
    from claudlobby import bot_operations, supervision_inventory

    root = tmp_path / "data"
    bot = root / "local/example/runtime/bots/worker"
    (bot / "data").mkdir(parents=True)
    (bot / "memory").mkdir()
    (bot / "data/.stopped").write_text("intentionally stopped\n")
    (bot / "memory/context.md").write_text("keep this context\n")
    (bot / ".env").write_text("FIXTURE_SECRET=retained\n")
    (bot / ".env").chmod(0o600)
    def snapshot():
        return {str(path.relative_to(root)): ("directory" if path.is_dir() else path.read_bytes(),
                                              path.stat().st_mode & 0o777)
                for path in root.rglob("*")}
    before = snapshot()
    monkeypatch.setattr(bot_operations, "set_bot_running",
                        lambda **kwargs: pytest.fail("unsupported removal changed native state"))
    monkeypatch.setattr(supervision_inventory, "Adapter",
                        lambda *args, **kwargs: pytest.fail("unsupported removal queried native state"))
    assert main(["--root", str(root), "--fleet", "example", "--json", "bot", "remove", "worker",
                 *(["--purge"] if purge else [])]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "conflict"
    assert "Git roster changes are not yet supported" in result["error"]["message"]
    assert result["data"] == {"native_outcome": "unattempted", "changed": False}
    assert snapshot() == before
