"""Composed defaults that keep an unattended bot from stalling (#1388, #941).

Once auto mode has blocked several actions, Claude Code ends a turn with a
dialog offering `/auto-mode-setup`, and clauDNA's PreCompact hook refuses every
other compaction until `/claudna:capture` runs. Nobody sits at a bot's pane to
answer either. So every bot composes `skillOverrides` with `auto-mode-setup`
"off" into its settings.local.json and `CLAUDNA_PRECOMPACT_REFLECT=0` into its
bot.conf. A fleet overrides either under `defaults:`, and a bot under its own
entry.

On a model with a native 1M window, Claude Code can also end a turn past 200k
tokens on an offer to autocompact earlier. A window set outside user settings
stops it, so every bot composes `modelSettings` naming each such model "auto":
Claude Code's own window, the one it uses with nothing set. A fleet or a bot
sets `auto_compact_window` to a number to choose a window instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import yaml

from claudlobby.known_values import NATIVE_1M_MODELS
from tests.conftest import load_test_fleet, make_paths

BOTS = ("lead", "worker-1")
AUTO = {model: {"autoCompactWindow": "auto"} for model in NATIVE_1M_MODELS}


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


def _windows(fleet_dir: Path) -> dict:
    """Each bot's composed (modelSettings, top-level autoCompactWindow)."""
    from claudlobby.composer import compose_settings_local

    fleet = load_test_fleet(fleet_dir)
    paths = make_paths(fleet_dir)
    out = {}
    for b in BOTS:
        settings = compose_settings_local(fleet.bots[b], fleet, paths)
        out[b] = (settings.get("modelSettings"), settings.get("autoCompactWindow"))
    return out


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


def test_every_bot_names_claude_codes_own_window_for_each_1m_model(fleet_dir: Path):
    # "auto" is the value Claude Code reads as its own window, so the window is
    # the one it uses with nothing set; no top-level key, which would cap every
    # model's window instead.
    assert "claude-sonnet-5-5" in NATIVE_1M_MODELS
    assert _windows(fleet_dir) == {b: (AUTO, None) for b in BOTS}


def test_a_number_sets_the_window_and_a_bot_entry_wins(fleet_dir: Path):
    _write(fleet_dir, "defaults", "auto_compact_window: 400000")
    assert _windows(fleet_dir) == {b: (None, 400000) for b in BOTS}
    _write(fleet_dir, "worker-1", "auto_compact_window: auto")
    assert _windows(fleet_dir) == {"lead": (None, 400000), "worker-1": (AUTO, None)}


@pytest.mark.parametrize("value", [100000, 1000000])
def test_the_bounds_claude_code_accepts_are_accepted(fleet_dir: Path, value):
    _write(fleet_dir, "worker-1", f"auto_compact_window: {value}")
    assert _windows(fleet_dir)["worker-1"] == (None, value)


@pytest.mark.parametrize("value", [
    # Claude Code drops each of these without a word, and the offer would return.
    "99999", "1000001", "400k", '"400000"', "450000.5", "true", "null", "AUTO",
])
def test_a_window_claude_code_would_drop_is_refused(fleet_dir: Path, value):
    _write(fleet_dir, "worker-1", f"auto_compact_window: {value}")
    with pytest.raises(ValueError, match=r"bots\.worker-1\.auto_compact_window"):
        load_test_fleet(fleet_dir)


def test_a_refused_fleet_default_names_the_defaults_key(fleet_dir: Path):
    _write(fleet_dir, "defaults", "auto_compact_window: 50000")
    with pytest.raises(ValueError, match=r"'defaults\.auto_compact_window'"):
        load_test_fleet(fleet_dir)


@pytest.mark.parametrize("tier", ["bot", "fleet.defaults", "built_in"])
def test_config_explain_names_where_the_window_came_from(tmp_path: Path, tier):
    from claudlobby.config import load_fleet, scalar_config_origin

    raw = {"name": "canary", "manager": "b", "bots": {"b": {"expertise": ["x"]}}}
    if tier == "bot":
        raw["bots"]["b"]["auto_compact_window"] = 400000
    if tier == "fleet.defaults":
        raw["defaults"] = {"auto_compact_window": 400000}
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({"fleet": raw}))
    _, defaults = load_fleet(path)
    assert scalar_config_origin(path, defaults, "auto_compact_window", bot="b") == (
        tier,
        None if tier == "built_in"
        else f"fleet.{'bots.b' if tier == 'bot' else 'defaults'}.auto_compact_window",
    )
