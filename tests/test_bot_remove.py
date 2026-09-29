"""Retired bot cleanup reads committed ancestry without touching a runtime."""

from types import SimpleNamespace

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
