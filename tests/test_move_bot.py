"""The public move door refuses before touching retained bot state."""
import json

import pytest

from claudlobby.__main__ import main


@pytest.mark.parametrize("options", [[], ["--apply"], ["--apply", "--cleanup-source", "--force"]])
def test_move_is_explicitly_unsupported_without_files_or_native_effects(tmp_path, monkeypatch, capsys, options):
    from claudlobby import bot_operations, supervision_inventory

    root = tmp_path / "data"
    source = root / "local/source/runtime/bots/worker"
    target = root / "local/target"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / ".env").write_text("PRIVATE_FIXTURE=retained\n")
    (source / "data").mkdir()
    (source / "data/.stopped").write_text("intentional stop\n")
    (source / "memory").mkdir()
    (source / "memory/fact.md").write_text("retained context\n")
    (target / "fleet.yaml").write_text("fleet: {name: target, bots: {}}\n")
    def tree():
        return {str(p.relative_to(root)): ("directory" if p.is_dir() else p.read_bytes(),
                                          p.stat().st_mode & 0o777)
                for p in root.rglob("*")}
    before = tree()
    monkeypatch.setattr(bot_operations, "set_bot_running",
                        lambda **kw: pytest.fail("unsupported move changed native state"))
    monkeypatch.setattr(supervision_inventory, "Adapter",
                        lambda *a, **kw: pytest.fail("unsupported move queried native state"))
    assert main(["--root", str(root), "--json", "bot", "move", "worker", "--from", "source",
                 "--to", "target", *options]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "bot.move" and result["error"]["code"] == "conflict"
    assert "Git roster changes are not yet supported" in result["error"]["message"]
    assert result["data"] == {"native_outcome": "unattempted", "changed": False}
    assert "Keep existing bot state and declarations unchanged" in result["error"]["hint"]
    assert tree() == before
