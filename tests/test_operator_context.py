"""Operator-context guard uses the selected native ancestry verdict."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands.operator_context import require_operator_context


@pytest.mark.parametrize("git_current", [False, True])
def test_selected_bot_ancestry_refuses_even_without_bot_environment(monkeypatch, tmp_path, git_current):
    from claudlobby import activation_state, config_plan, config_units, context, supervision_inventory

    installed = tmp_path / "worker.service"
    installed.write_text("[Service]\n")
    monkeypatch.setattr(activation_state, "read_selection", lambda _root: {"plan_id": "plan"})
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: object())
    package = SimpleNamespace(installation=object() if git_current else None, source_revision="a" * 40)
    monkeypatch.setattr(context, "resolve_paths", lambda **_: SimpleNamespace(package=package))
    monkeypatch.setattr(config_units, "current_declarations", lambda *_: (object(),))
    if git_current:
        from claudlobby import active_config
        monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(tmp_path / "installation"))
        monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail(
            "historical sealed selection must not gate Git operator commands"))
        monkeypatch.setattr(active_config, "read_current_config", lambda *_a, **_k: {})
        monkeypatch.setattr(config_units, "current_git_declarations", lambda *_: (object(),))
    else:
        monkeypatch.delenv("CLAUDLOBBY_INSTALLATION", raising=False)
    monkeypatch.setattr(supervision_inventory, "_catalog", lambda _text: ("Linux", "", [], set(), {}))
    inventory = SimpleNamespace(units=(SimpleNamespace(installed=(SimpleNamespace(path=installed),),
                                                       target="worker.service"),),
                                require_complete=lambda: None)
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(supervision_inventory, "collect_enrollment", lambda *_args, **_kwargs: inventory)

    class Native:
        def __init__(self, _package):
            self.package = _package

        def read(self, function):
            assert function == "svc_inventory_catalog"
            return "catalog"

        def call(self, function, file, target, pid):
            assert (function, Path(file), target) == (
                "svc_activation_assert_external", installed, "worker.service")
            assert int(pid) > 0
            return SimpleNamespace(returncode=1)

    monkeypatch.setattr(supervision_inventory, "Adapter", Native)
    for name in ("BOT_ID", "BOT_NAME", "BOT_DIR", "BOT_SERVICE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CommandFailure, match="operator shell"):
        require_operator_context(tmp_path)
