"""The public-write guard is composed per bot, opt-in, like the heavy-job slot.

A composed hook is live on every bot the moment `generate` writes it (#1310), so
the manifest key is the only place one bot can go first. The composer reads it
and composes the hook for that bot only, matched on Bash and on the GitHub MCP
tools; an unarmed bot runs no hook at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import load_test_fleet, make_paths

HOOK = "$CLAUDLOBBY_NATIVE_DIR/public-write-guard.sh"


def _arm(fleet_dir: Path, *, where: str = "lead", value: str = "true") -> None:
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    if where == "defaults":
        anchor = "  defaults:\n    model: opus\n"
        after = before.replace(anchor, anchor + f"    public_write_guard: {value}\n")
    else:
        anchor = f"    {where}:\n"
        after = before.replace(anchor, anchor + f"      public_write_guard: {value}\n", 1)
    assert after != before, "fixture anchor moved: the key was not written"
    fy.write_text(after)


def _settings(fleet_dir: Path, bot: str) -> dict:
    from claudlobby.composer import compose_settings_local

    fleet = load_test_fleet(fleet_dir)
    return compose_settings_local(fleet.bots[bot], fleet, make_paths(fleet_dir))


def _guard_matchers(settings: dict) -> list[str]:
    return [
        group.get("matcher", "")
        for group in settings.get("hooks", {}).get("PreToolUse", [])
        for hook in group.get("hooks", [])
        if hook.get("command") == HOOK
    ]


def test_off_unless_set(fleet_dir: Path):
    assert load_test_fleet(fleet_dir).bots["lead"].public_write_guard is False
    assert _guard_matchers(_settings(fleet_dir, "lead")) == []


def test_one_bot_can_go_first(fleet_dir: Path):
    _arm(fleet_dir, where="lead")
    assert _guard_matchers(_settings(fleet_dir, "lead")) != []
    assert _guard_matchers(_settings(fleet_dir, "worker-1")) == []


def test_defaults_widen_it_and_a_bot_can_stay_out(fleet_dir: Path):
    _arm(fleet_dir, where="defaults")
    _arm(fleet_dir, where="worker-1", value="false")
    fleet = load_test_fleet(fleet_dir)
    assert fleet.bots["lead"].public_write_guard is True
    assert fleet.bots["worker-1"].public_write_guard is False


def test_a_string_is_refused_rather_than_read_as_true(fleet_dir: Path):
    _arm(fleet_dir, where="lead", value='"yes"')
    with pytest.raises(ValueError, match="public_write_guard"):
        load_test_fleet(fleet_dir)


@pytest.mark.parametrize("tool, runs", [
    ("Bash", True),
    ("mcp__github__create_issue", True),
    ("mcp__github__push_files", True),
    ("Read", False),
    ("mcp__plugin_telegram_telegram__reply", False),
])
def test_the_matcher_covers_bash_and_the_github_mcp_tools(fleet_dir: Path, tool, runs):
    """Claude Code reads a matcher as a regular expression over the tool name."""
    _arm(fleet_dir, where="lead")
    (matcher,) = _guard_matchers(_settings(fleet_dir, "lead"))
    assert bool(re.fullmatch(matcher, tool)) is runs, (matcher, tool)
