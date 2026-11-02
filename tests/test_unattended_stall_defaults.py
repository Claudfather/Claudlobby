"""Two composed defaults that keep an unattended bot from stalling (#1388, #941).

Once auto mode has blocked several actions, Claude Code ends a turn with a
dialog offering `/auto-mode-setup`, and clauDNA's PreCompact hook refuses every
other compaction until `/claudna:capture` runs. Nobody sits at a bot's pane to
answer either. So every bot composes `skillOverrides` with `auto-mode-setup`
"off" into its settings.local.json and `CLAUDNA_PRECOMPACT_REFLECT=0` into its
bot.conf. A fleet overrides either under `defaults:`, and a bot under its own
entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import load_test_fleet, make_paths

BOTS = ("lead", "worker-1")


def _write(fleet_dir: Path, where: str, *lines: str) -> None:
    """Write each line verbatim under `defaults:` or under one bot."""
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    if where == "defaults":
        anchor = "  defaults:\n    model: opus\n"
        after = before.replace(anchor, anchor + "".join(f"    {ln}\n" for ln in lines))
    else:
        anchor = f"    {where}:\n"
        after = before.replace(anchor, anchor + "".join(f"      {ln}\n" for ln in lines), 1)
    assert after != before, "fixture anchor moved: the key was not written"
    fy.write_text(after)


def _skill_overrides(fleet_dir: Path) -> dict:
    from claudlobby.composer import compose_settings_local

    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    return {b: compose_settings_local(fleet.bots[b], fleet, paths).get("skillOverrides")
            for b in BOTS}


def _reflect_lines(fleet_dir: Path) -> dict:
    from claudlobby.composer import compose_bot_conf

    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    return {b: [ln for ln in compose_bot_conf(fleet.bots[b], fleet, paths).splitlines()
                if "CLAUDNA_PRECOMPACT_REFLECT" in ln]
            for b in BOTS}


def test_every_bot_composes_auto_mode_setup_off(fleet_dir: Path):
    assert _skill_overrides(fleet_dir) == {b: {"auto-mode-setup": "off"} for b in BOTS}


def test_a_fleet_default_wins_and_a_bot_entry_wins_over_it(fleet_dir: Path):
    _write(fleet_dir, "defaults", 'skill_overrides: {auto-mode-setup: "on"}')
    assert _skill_overrides(fleet_dir) == {b: {"auto-mode-setup": "on"} for b in BOTS}
    _write(fleet_dir, "worker-1", 'skill_overrides: {auto-mode-setup: "off", deploy: "name-only"}')
    assert _skill_overrides(fleet_dir) == {
        "lead": {"auto-mode-setup": "on"},
        "worker-1": {"auto-mode-setup": "off", "deploy": "name-only"},
    }


def test_an_override_of_another_skill_keeps_the_default(fleet_dir: Path):
    _write(fleet_dir, "defaults", 'skill_overrides: {deploy: "off"}')
    assert _skill_overrides(fleet_dir) == {
        b: {"auto-mode-setup": "off", "deploy": "off"} for b in BOTS
    }


@pytest.mark.parametrize("value, match", [
    # YAML reads a bare on or off as a boolean, which Claude Code would not accept.
    ("{auto-mode-setup: on}", "quote"),
    ('{auto-mode-setup: "hidden"}', "hidden"),
    ("[auto-mode-setup]", "mapping"),
])
def test_a_state_claude_code_does_not_accept_is_refused(fleet_dir: Path, value, match):
    _write(fleet_dir, "worker-1", f"skill_overrides: {value}")
    with pytest.raises(ValueError, match=match):
        load_test_fleet(fleet_dir)


def test_every_bot_composes_the_capture_prompt_off(fleet_dir: Path):
    assert _reflect_lines(fleet_dir) == {
        b: ["export CLAUDNA_PRECOMPACT_REFLECT=0"] for b in BOTS
    }


def test_a_fleet_default_arms_the_capture_prompt_and_a_bot_entry_wins(fleet_dir: Path):
    _write(fleet_dir, "defaults", "claudna: {precompact_reflect: true}")
    assert _reflect_lines(fleet_dir) == {
        b: ["export CLAUDNA_PRECOMPACT_REFLECT=1"] for b in BOTS
    }
    _write(fleet_dir, "worker-1", "claudna: {precompact_reflect: false}")
    assert _reflect_lines(fleet_dir) == {
        "lead": ["export CLAUDNA_PRECOMPACT_REFLECT=1"],
        "worker-1": ["export CLAUDNA_PRECOMPACT_REFLECT=0"],
    }
