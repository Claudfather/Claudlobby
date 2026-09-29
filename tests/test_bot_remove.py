"""Retired bot cleanup reads committed ancestry without touching a runtime."""

from types import SimpleNamespace
import subprocess

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands import bot_remove
from claudlobby.commands.bot_remove import _last_declaration


def test_retained_declaration_survives_unrelated_activations(monkeypatch, tmp_path):
    from claudlobby import activation_state, active_config, config_plan, releases

    def selection(name):
        return {"activation_id": name, "plan_id": f"plan-{name}", "release_id": "same-release"}

    chain = {"new": selection("middle"), "middle": selection("old"), "old": None}
    plans = {name: SimpleNamespace(plan_id=f"plan-{name}", release_id="same-release",
                                   release_seal="sealed",
                                   fleets=("example",)) for name in chain}
    monkeypatch.setattr(activation_state, "read_activation", lambda _root, name: SimpleNamespace(
        status="active", body={"intent": {"plan_id": f"plan-{name}",
                                            "release_id": "same-release"},
                               "previous_selection": chain[name]}))
    monkeypatch.setattr(config_plan, "read_plan", lambda _root, plan_id: plans[plan_id.removeprefix("plan-")])
    monkeypatch.setattr(releases, "read_release", lambda *_args, **_kwargs: SimpleNamespace(seal_sha256="sealed"))
    monkeypatch.setattr(active_config, "context_from_plan", lambda plan, _fleet, package: SimpleNamespace(
        fleet=SimpleNamespace(bots={"retired": object()} if plan is plans["old"] else {})))

    plan, _ = _last_declaration(tmp_path, selection("new"), "example", "retired", object())
    assert plan is plans["old"]


def test_teardown_timeout_kills_private_process_group_and_reports_unknown(monkeypatch):
    calls = []

    class TimedOut:
        pid = 43210

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired("spin-down-bot.sh", timeout)
            return "", ""

    def launch(_command, **kwargs):
        assert kwargs["start_new_session"] is True
        return TimedOut()

    monkeypatch.setattr(bot_remove.subprocess, "Popen", launch)
    monkeypatch.setattr(bot_remove.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    with pytest.raises(CommandFailure) as raised:
        bot_remove._teardown(["spin-down-bot.sh"], {})
    assert raised.value.error.code == "commit_unknown"
    assert raised.value.data["native_outcome"] == "unknown"
    assert calls[1][:2] == ("killpg", 43210)


def test_native_effect_marker_distinguishes_preflight_refusal():
    assert not bot_remove._effect_attempted("spin-down[worker]: receipt: submitted\n")
    assert bot_remove._effect_attempted("spin-down[worker]: effect-attempted\n")


@pytest.mark.parametrize("declared_in", ("active", "authored"))
def test_remove_refuses_declared_bot_before_native_teardown(monkeypatch, tmp_path, declared_in):
    from claudlobby import active_config, config_plan, context
    from claudlobby import supervision_inventory

    plan = SimpleNamespace(release_id="release", release_seal="seal", fleets=("fleet",))
    release = SimpleNamespace(release_id="release", seal_sha256="seal")
    paths = SimpleNamespace(bot_runtime=lambda bot: tmp_path / bot)
    active = SimpleNamespace(fleet=SimpleNamespace(
        bots={"worker": object()} if declared_in == "active" else {}, manager="lead"), paths=paths)
    authored = SimpleNamespace(fleet=SimpleNamespace(
        bots={"worker": object()} if declared_in == "authored" else {}, manager="lead"))
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: plan)
    monkeypatch.setattr(active_config, "context_from_plan", lambda *_args, **_kwargs: active)
    monkeypatch.setattr(context, "load_context", lambda *_: authored)
    monkeypatch.setattr(supervision_inventory, "Adapter",
                        lambda *_: pytest.fail("native inventory must not run"))
    with pytest.raises(CommandFailure, match="before removal"):
        bot_remove._preflight(tmp_path, "fleet", "worker", object(),
                              {"plan_id": "plan"}, release)
