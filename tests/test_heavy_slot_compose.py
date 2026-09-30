"""The heavy-job slot is composed per bot, opt-in (#1686).

A composed hook is live on every bot the moment `generate` writes it (#1310),
so the manifest key is the only place one bot can go first. The composer reads
it and composes the hook for that bot only: an unarmed bot runs no hook at all,
not a hook that checks a switch and exits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import load_test_fleet, make_paths

HOOK = "$CLAUDLOBBY_ROOT/lib/heavy-slot-guard.sh"


def _arm(fleet_dir: Path, *, where: str = "lead", value: str = "true") -> None:
    """Set `heavy_slot` on one bot, or on `defaults` with where='defaults'."""
    fy = fleet_dir / "fleet.yaml"
    before = fy.read_text()
    if where == "defaults":
        anchor = "  defaults:\n    model: opus\n"
        after = before.replace(anchor, anchor + f"    heavy_slot: {value}\n")
    else:
        anchor = f"    {where}:\n"
        after = before.replace(anchor, anchor + f"      heavy_slot: {value}\n", 1)
    assert after != before, "fixture anchor moved: the key was not written"
    fy.write_text(after)


def _settings(fleet_dir: Path, bot: str) -> dict:
    from claudlobby.composer import compose_settings_local

    fleet = load_test_fleet(fleet_dir)
    return compose_settings_local(fleet.bots[bot], fleet, make_paths(fleet_dir))


def _guard_groups(settings: dict) -> list[str]:
    """The matcher of every PreToolUse group that runs the guard."""
    return [
        group.get("matcher", "")
        for group in settings.get("hooks", {}).get("PreToolUse", [])
        for hook in group.get("hooks", [])
        if hook.get("command") == HOOK
    ]


class TestTheKeyIsOptInPerBot:
    def test_a_bot_that_does_not_set_it_is_off(self, fleet_dir: Path):
        assert load_test_fleet(fleet_dir).bots["lead"].heavy_slot is False

    def test_one_bot_can_go_first(self, fleet_dir: Path):
        _arm(fleet_dir, where="lead")
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].heavy_slot is True
        assert fleet.bots["worker-1"].heavy_slot is False

    def test_defaults_widen_it_and_a_bot_can_stay_out(self, fleet_dir: Path):
        _arm(fleet_dir, where="defaults")
        _arm(fleet_dir, where="worker-1", value="false")
        fleet = load_test_fleet(fleet_dir)
        assert fleet.bots["lead"].heavy_slot is True
        assert fleet.bots["worker-1"].heavy_slot is False

    def test_a_string_is_refused_rather_than_read_as_true(self, fleet_dir: Path):
        _arm(fleet_dir, where="lead", value='"yes"')
        with pytest.raises(ValueError, match="heavy_slot"):
            load_test_fleet(fleet_dir)


class TestComposition:
    def test_an_unarmed_bot_composes_no_hook(self, fleet_dir: Path):
        assert _guard_groups(_settings(fleet_dir, "lead")) == []

    def test_an_armed_bot_runs_the_hook_on_bash_calls_and_no_other_bot_does(
        self, fleet_dir: Path
    ):
        _arm(fleet_dir, where="lead")
        assert _guard_groups(_settings(fleet_dir, "lead")) == ["Bash"]
        assert _guard_groups(_settings(fleet_dir, "worker-1")) == []

    def test_arming_adds_the_hook_and_nothing_else(self, fleet_dir: Path):
        before = _settings(fleet_dir, "lead")
        _arm(fleet_dir, where="lead")
        after = _settings(fleet_dir, "lead")
        for group in after["hooks"]["PreToolUse"]:
            group["hooks"] = [h for h in group["hooks"] if h.get("command") != HOOK]
        after["hooks"]["PreToolUse"] = [
            g for g in after["hooks"]["PreToolUse"] if g["hooks"]
        ]
        assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)


class TestTheSwitch:
    def test_it_is_registered_opt_in_per_bot(self):
        from claudlobby import switches

        (sw,) = [s for s in switches.SWITCHES if s.key == "heavy-slot"]
        assert sw.polarity == switches.OPT_IN
        assert sw.carrier == switches.COMPOSE_BOT
        assert sw.config == "heavy_slot"
        assert "deployment gate" in sw.why_opt_in

    def test_the_registry_reads_each_bots_own_value(self, fleet_dir: Path):
        from claudlobby import switches

        _arm(fleet_dir, where="lead")
        fleet = load_test_fleet(fleet_dir)
        assert switches._bot_config_value(fleet.bots["lead"], "heavy_slot") is True
        assert switches._bot_config_value(fleet.bots["worker-1"], "heavy_slot") is False


class TestDeclaredScripts:
    """`heavy_slot: {scripts: [...]}` (#2039): the slot, plus declared scripts."""

    def _declare(self, fleet_dir, value, where="lead"):
        fy = fleet_dir / "fleet.yaml"
        before = fy.read_text()
        anchor = f"    {where}:\n"
        after = before.replace(anchor, anchor + f"      heavy_slot: {value}\n", 1)
        assert after != before
        fy.write_text(after)

    def test_the_mapping_turns_the_slot_on_and_keeps_the_scripts(self, fleet_dir: Path):
        self._declare(fleet_dir, '{scripts: [render.py, "tools/*.py"]}')
        bot = load_test_fleet(fleet_dir).bots["lead"]
        assert bot.heavy_slot is True
        assert bot.heavy_slot_scripts == ("render.py", "tools/*.py")
        assert _guard_groups(_settings(fleet_dir, "lead")) == ["Bash"]

    def test_true_still_means_the_slot_with_no_scripts(self, fleet_dir: Path):
        self._declare(fleet_dir, "true")
        bot = load_test_fleet(fleet_dir).bots["lead"]
        assert bot.heavy_slot is True and bot.heavy_slot_scripts == ()

    @pytest.mark.parametrize("value", ['{scripts: render.py}', '{script: [render.py]}', '{scripts: [""]}'])
    def test_a_malformed_mapping_is_refused(self, fleet_dir: Path, value):
        self._declare(fleet_dir, value)
        with pytest.raises(ValueError, match="heavy_slot"):
            load_test_fleet(fleet_dir)

    def test_generate_writes_the_fleets_declared_scripts_as_absolute_patterns(self, fleet_dir: Path):
        from claudlobby.composer import compose_heavy_slot_scripts

        self._declare(fleet_dir, '{scripts: [render.py, "tools/*.py", /opt/shared/job.py]}')
        self._declare(fleet_dir, "true", where="worker-1")
        fleet = load_test_fleet(fleet_dir)
        paths = make_paths(fleet_dir)
        out = compose_heavy_slot_scripts(fleet, paths)
        data = json.loads(out.read_text())
        bot_dir = paths.runtime_bots / "lead"
        assert data["fleet"] == fleet.name
        assert data["bots"] == {"lead": [str(bot_dir / "render.py"), str(bot_dir / "tools" / "*.py"),
                                         "/opt/shared/job.py"]}
        names = out.with_suffix(".names").read_text().split()
        assert names == ["*.py", "job.py", "render.py"]

    def test_no_declared_script_removes_the_fleets_files(self, fleet_dir: Path):
        from claudlobby.composer import compose_heavy_slot_scripts

        self._declare(fleet_dir, '{scripts: [render.py]}')
        out = compose_heavy_slot_scripts(load_test_fleet(fleet_dir), make_paths(fleet_dir))
        assert out.exists()
        fy = fleet_dir / "fleet.yaml"
        fy.write_text(fy.read_text().replace("      heavy_slot: {scripts: [render.py]}\n", ""))
        compose_heavy_slot_scripts(load_test_fleet(fleet_dir), make_paths(fleet_dir))
        assert not out.exists() and not out.with_suffix(".names").exists()

    def test_validate_warns_when_a_declared_script_matches_no_file(self, fleet_dir: Path):
        from claudlobby.validator import validate

        self._declare(fleet_dir, '{scripts: [render.py]}')
        paths = make_paths(fleet_dir)
        fleet = load_test_fleet(fleet_dir)
        report = validate(fleet, paths)
        assert "heavy-slot-script" in report.warning_categories, report.categorized()
        bot_dir = paths.runtime_bots / "lead"
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "render.py").write_text("print(1)\n")
        assert "heavy-slot-script" not in validate(fleet, paths).warning_categories
