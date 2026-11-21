"""Operator-context guard uses the selected native ancestry verdict."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby.command_result import CommandFailure
from claudlobby.commands.operator_context import require_operator_context


def test_current_bot_ancestry_refuses_even_without_bot_environment(monkeypatch, tmp_path):
    from claudlobby import activation_state, config_plan, config_units, context, supervision_inventory

    installed = tmp_path / "worker.service"
    installed.write_text("[Service]\n")
    monkeypatch.setattr(activation_state, "read_selection", lambda _root: {"plan_id": "plan"})
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: object())
    package = SimpleNamespace(installation=object(), source_revision="a" * 40)
    monkeypatch.setattr(context, "resolve_paths", lambda **_: SimpleNamespace(package=package))
    from claudlobby import active_config
    monkeypatch.setenv("CLAUDLOBBY_INSTALLATION", str(tmp_path / "installation"))
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail(
        "historical sealed selection must not gate Git operator commands"))
    monkeypatch.setattr(active_config, "read_current_config", lambda *_a, **_k: {})
    monkeypatch.setattr(config_units, "current_git_declarations", lambda *_: (object(),))
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


FAKE_SYSTEMCTL = """#!/bin/bash
# Stands in for the user manager: it answers only on the account's own bus.
uid=$(/usr/bin/id -u)
if [ "$XDG_RUNTIME_DIR" != "/run/user/$uid" ] || [ "$DBUS_SESSION_BUS_ADDRESS" != "unix:path=/run/user/$uid/bus" ]; then
    echo "Failed to connect to bus: No medium found" >&2
    exit 1
fi
[ "$1 $2" = "--user show" ] || exit 1
target="${@: -1}"
printf 'Id=%s\\nLoadState=loaded\\nActiveState=inactive\\nSubState=dead\\nUnitFileState=enabled\\nFragmentPath=%s\\nControlGroup=\\nMainPID=0\\nControlPID=0\\n' "$target" "$INSTALLED_UNIT"
"""


@pytest.mark.parametrize("built", ["update", "prepare"])
def test_the_update_actions_environment_reaches_the_user_manager_for_the_operator_check(monkeypatch, tmp_path, built):
    """Why the preparation script's update actions set XDG_RUNTIME_DIR and
    DBUS_SESSION_BUS_ADDRESS: the operator check asks systemctl --user about each
    installed unit, through the real native adapter, before host update moves
    anything. Without the user bus it cannot verify, and refuses."""
    import os
    from claudlobby import activation_state, active_config, config_plan, config_units, context, supervision_inventory
    from tests.test_host_install import payload

    installed = tmp_path / "worker.service"
    installed.write_text("[Service]\n")
    tools = tmp_path / "tools"; tools.mkdir()
    (tools / "systemctl").write_text(FAKE_SYSTEMCTL); (tools / "systemctl").chmod(0o755)
    (tools / "uname").write_text("#!/bin/sh\necho Linux\n"); (tools / "uname").chmod(0o755)
    native = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts"
    package = SimpleNamespace(installation=object(), source_revision="a" * 40, native=native)
    monkeypatch.setattr(context, "resolve_paths", lambda **_: SimpleNamespace(package=package))
    monkeypatch.setattr(activation_state, "read_selection", lambda *_: pytest.fail("no sealed selection"))
    monkeypatch.setattr(config_plan, "read_plan", lambda *_: object())
    monkeypatch.setattr(active_config, "read_current_config", lambda *_a, **_k: {})
    monkeypatch.setattr(config_units, "current_git_declarations", lambda *_: (object(),))
    monkeypatch.setattr(supervision_inventory, "_catalog", lambda _text: ("Linux", "", [], set(), {}))
    inventory = SimpleNamespace(units=(SimpleNamespace(installed=(SimpleNamespace(path=installed),),
                                                       target="worker.service"),))
    inventory.require_complete = lambda: inventory
    monkeypatch.setattr(supervision_inventory, "collect_enrollment", lambda *_args, **_kwargs: inventory)
    adapter = supervision_inventory.Adapter
    monkeypatch.setattr(supervision_inventory.Adapter, "read", lambda self, function: (
        "catalog" if function == "svc_inventory_catalog" else pytest.fail(function)))
    assert supervision_inventory.Adapter is adapter  # the real call to supervisor.sh

    script = payload()
    monkeypatch.setattr(script.platform, "system", lambda: "Linux")
    home = tmp_path / "account"
    environment = (script.update_environment if built == "update" else script.fixed_environment)(home)
    for name in list(os.environ):  # the action's map is the whole environment
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    # The stand-in manager comes first, where /usr/bin/systemctl is on a real computer.
    monkeypatch.setenv("PATH", f"{tools}:{environment['PATH']}")
    monkeypatch.setenv("INSTALLED_UNIT", str(installed))
    assert ("PLANE_EMIT_DISABLED" in os.environ) == (built == "prepare")
    if built == "update":
        require_operator_context(tmp_path)
    else:
        with pytest.raises(CommandFailure, match="operator ancestry could not be verified"):
            require_operator_context(tmp_path)
