"""Source and composed manager guidance must agree on count versus symptoms."""

import shutil
from pathlib import Path

from claudlobby.composer import compose_bot
from claudlobby.paths import Paths
from tests.conftest import install_real_template, load_test_fleet


SOURCE = Path(__file__).resolve().parents[1]


def _assert_decisions(text):
    row = next(line for line in text.splitlines() if line.startswith("| Reviewer has ~3+"))
    before = text.split("**Before dispatching:**", 1)[1].split("\n\n", 1)[0]
    reviewer = text.split("**Reviewers (Sonnet-sensitive):**", 1)[1].split("\n", 1)[0]
    for repetition in (row, before, reviewer):
        assert "self-check" in repetition
        assert "count alone does not justify a restart" in repetition
        assert "context-management" in repetition
    degradation = next(line for line in text.splitlines() if line.startswith("| Reviewer reports `context-degraded`"))
    assert "~3" not in degradation
    assert "`/restart`" in degradation
    assert "safe" in degradation


def _assert_reviewer_protocol(text):
    reviewer = next(line for line in text.splitlines() if line.startswith("For reviewers"))
    assert "~3 completed rows" in reviewer
    assert "self-check" in reviewer
    assert "count alone does not justify a restart" in reviewer
    assert "context-management" in reviewer
    assert "`context-degraded`" in reviewer
    assert "safe" in reviewer and "`/restart`" in reviewer
    assert "handoff" in reviewer and "notification" in reviewer


def test_source_and_composed_manager_keep_count_as_selfcheck(fleet_dir, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANE_EMIT_DISABLED", "1")
    monkeypatch.setenv("PLANE_SOCKET", str(tmp_path / "absent.sock"))
    for relative in ("expertise/orchestration.md", "protocols/context-management.md",
                     "protocols/safe-worker-restart.md"):
        shutil.copy2(SOURCE / "library" / relative, fleet_dir / "library" / relative)
    shutil.copytree(SOURCE / "library/skills/restart", fleet_dir / "library/skills/restart")
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    bot = fleet.bots["lead"]
    bot.telegram.handle = ""
    bot.protocols = ["context-management", "safe-worker-restart"]
    bot.skills = ["restart"]
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    output = compose_bot(bot, fleet, paths, log=lambda message: None)
    source = (fleet_dir / "library/expertise/orchestration.md").read_text()
    rendered = (output / "CLAUDE.md").read_text()
    _assert_decisions(source)
    _assert_decisions(rendered)
    _assert_reviewer_protocol((fleet_dir / "library/protocols/safe-worker-restart.md").read_text())
    _assert_reviewer_protocol(rendered)
    assert "not as a threshold in itself" in rendered
    linked = output / ".claude/skills/restart"
    assert linked.is_symlink()
    assert linked.resolve() == (fleet_dir / "library/skills/restart").resolve()
    assert not (fleet_dir / "state/plane").exists()
    assert not (home / ".claude/channels").exists()
