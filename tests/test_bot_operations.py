"""Selected bot placement remains readable after deliberate de-enrollment."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from claudlobby import activation, bot_operations, context
from claudlobby.__main__ import main
from claudlobby.activation_enrollment import selected_bot_entry
from claudlobby.config_plan import ConfigPlanBuilder
from claudlobby.config_units import planned_units
from tests.package_fixtures import source_package
from tests.test_activation import cold, tmp_path  # noqa: F401 — real short-root activation fixtures
from tests.test_releases import installed  # noqa: F401 — cold fixture dependency


def test_selected_bot_placement_survives_removal_of_installed_unit(cold):  # noqa: F811
    root, _, plan, host = cold
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    entry = selected_bot_entry(root, "example", "worker", "Linux")
    installed_unit = Path(entry["installed"])
    assert installed_unit.is_file() and entry["target"] == "com.example.worker.service"
    installed_unit.unlink()
    assert selected_bot_entry(root, "example", "worker", "Linux") == entry


def test_public_bot_start_stop_decisions_use_selected_placement(cold, monkeypatch, capsys):  # noqa: F811
    root, release, plan, host = cold
    # Add the real selected bot.conf output absent from the general activation
    # fixture. Its other sealed inputs/units remain unchanged.
    builder = ConfigPlanBuilder(root, plan.release_id, plan.release_seal, plan.fleets,
                                effects=deepcopy(plan.effects))
    builder.inputs = deepcopy(plan.inputs)
    builder.contents = {path.name: path.read_bytes() for path in (plan.directory / "files").iterdir()}
    builder.changes = {change.target: change for change in plan.changes}
    builder.file(root / "runtime/bots/worker/bot.conf",
                 b"BOT_ID=worker\nBOT_NAME=worker\nFLEET_NAME=example\n"
                 b"BOT_SERVICE=com.example.worker\nTMUX_SOCKET=com.example.worker\n")
    plan = builder.seal()
    host.plan = plan
    host.by_name = {decl.source.name: (decl, item) for decl, item in planned_units(plan, "Linux")}
    activation.bootstrap_activation(root, "cold", plan.plan_id, host.directory, adapter=host)
    package = replace(source_package(), native=release.native_path,
                      artifact_id=release.inputs.artifact_id)
    monkeypatch.setattr(context, "get_resources", lambda: package)

    class Native:
        def __init__(self):
            self.package = package
            self.session = "ready"
            self.actions = []
            self.calls = []
            self.handoff_rc = 0
            self.fence_args = []
            self.target = "com.example.worker.service"
            self.manager = "Linux"

        def call(self, function, *args, timeout=30):
            self.calls.append(function)
            target = self.target
            if function == "svc_inventory_catalog":
                return host.call(function, *args, timeout=timeout)
            if function == "svc_inventory_state":
                value = host.states.get(args[1], "not-found not-found inactive")
            elif function == "svc_bot_session_observe":
                value = self.session
            elif function == "svc_activation_quiet":
                value = "inactive\tno-cgroup-witness"
            elif function == "svc_activation_bot_fence":
                self.fence_args.append(args)
                value = "0\tprivate-fence"
            elif function == "svc_activation_bot_ready":
                value = "bridge-ready"
            elif function == "svc_activation_handoff":
                value = ""
            elif function == "svc_bot_disenroll_exact":
                self.actions.append("stop")
                Path(args[1]).unlink()
                host.states[target] = "not-found not-found inactive"
                value = ""
            elif function == "svc_bot_enroll_exact":
                self.actions.append("start")
                Path(args[1]).write_bytes(Path(args[0]).read_bytes())
                host.states[target] = ("unchanged loaded inactive" if self.manager == "Darwin"
                                       else "enabled loaded active")
                value = ""
            else:
                raise AssertionError(f"unexpected native call: {function}")
            return subprocess.CompletedProcess([function],
                                               self.handoff_rc if function == "svc_activation_handoff" else 0,
                                               value + "\n", "")

        def read(self, function, *args):
            return self.call(function, *args).stdout

    native = Native()
    monkeypatch.setattr(bot_operations, "Adapter", lambda _: native)

    def observed(_root, _declarations, _adapter, target, installed_path):
        assert target == native.target
        installed_rows = (SimpleNamespace(path=str(installed_path)),) if installed_path.exists() else ()
        active = host.states[target].split()[-1]
        return SimpleNamespace(installed=installed_rows, properties=(("ActiveState", active),))

    monkeypatch.setattr(bot_operations, "_observed", observed)
    monkeypatch.setattr(bot_operations, "assert_quiescent", lambda *a, **kw: None)

    def call(*argv, expected=0):
        assert main(["--root", str(root), "--json", *argv]) == expected
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is (expected == 0)
        return result

    healthy = call("bot", "start", "worker")["data"]
    assert healthy["state"] == "running" and healthy["changed"] is False
    assert healthy["readiness"] == "current_session_ready" and native.actions == []

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == []

    native.session = "absent"  # exact active/exited unit with missing private socket
    recovered = call("bot", "start", "worker")["data"]
    assert recovered["changed"] is True and recovered["readiness"] == "bridge_ready"
    assert native.actions == ["start"]

    stopped = call("bot", "stop", "worker")["data"]
    assert stopped["state"] == "stopped" and stopped["changed"] is True
    assert native.actions == ["start", "stop"]
    restarted = call("bot", "start", "worker")["data"]
    assert restarted["state"] == "running" and restarted["changed"] is True
    assert native.actions == ["start", "stop", "start"]

    for caller, target in (("worker", "manager"), ("manager", "manager")):
        monkeypatch.setenv("FLEET_NAME", "example")
        monkeypatch.setenv("BOT_ID", caller)
        monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
        monkeypatch.setenv("FLEET_ROOT", str(root))
        monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots" / caller))
        monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
        refused = call("bot", "stop", target, expected=4)
        assert refused["error"]["code"] == "conflict"
        assert native.actions == ["start", "stop", "start"]

    # launchd's selected launcher has exited, but its exact private tmux
    # session is still ready. Native inactive alone must not trigger a restart.
    for key in ("BOT_ID", "FLEET_NAME", "BOT_DIR", "FLEET_ROOT",
                "CLAUDLOBBY_ROOT", "CLAUDLOBBY_RELEASE_ID"):
        monkeypatch.delenv(key)
    native.manager = "Darwin"
    native.target = f"gui/501/com.example.worker"
    host.states[native.target] = "unchanged loaded inactive"
    declaration, item = next((decl, item) for decl, item in planned_units(plan, "Linux")
                             if decl.bot == "worker" and item["enroll"])
    entry = dict(selected_bot_entry(root, "example", "worker", "Linux"))
    entry["target"] = native.target
    monkeypatch.setattr(bot_operations, "_selected_unit",
                        lambda *_: ("Darwin", declaration, entry, (declaration,)))
    native.session = "ready"
    current = call("bot", "start", "worker")["data"]
    assert current["state"] == "running" and current["changed"] is False
    assert current["readiness"] == "current_session_ready"
    assert native.actions == ["start", "stop", "start"]

    host.states[native.target] = "unchanged unloaded inactive"
    assert call("bot", "start", "worker", expected=4)["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start"]
    host.states[native.target] = "unchanged loaded inactive"

    native.session = "unknown"
    assert call("bot", "start", "worker", expected=6)["error"]["code"] == "unavailable"
    assert native.actions == ["start", "stop", "start"]

    native.session = "ready"
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    started = call("bot", "start", "worker")["data"]
    assert started["changed"] is True and started["readiness"] == "bridge_ready"
    assert host.states[native.target] == "unchanged loaded inactive"
    assert native.actions == ["start", "stop", "start", "stop", "start"]

    # Restart is an intentional bounce even when launchd's old private
    # session is ready. It must not re-enroll a deliberately stopped bot.
    native.calls.clear()
    bounced = call("bot", "restart", "worker")["data"]
    assert bounced["state"] == "running" and bounced["changed"] is True
    assert bounced["readiness"] == "bridge_ready"
    assert bounced["handoff"] == "attempted"
    assert native.calls.index("svc_bot_session_observe") < native.calls.index("svc_activation_handoff")
    assert native.calls.index("svc_activation_handoff") < native.calls.index("svc_activation_bot_fence")
    assert native.calls.index("svc_activation_bot_fence") < native.calls.index("svc_bot_enroll_exact")
    assert native.calls.index("svc_bot_enroll_exact") < native.calls.index("svc_activation_bot_ready")
    assert native.actions == ["start", "stop", "start", "stop", "start", "start"]
    assert call("bot", "stop", "worker")["data"]["state"] == "stopped"
    refused = call("bot", "restart", "worker", expected=4)
    assert refused["error"]["code"] == "conflict"
    assert native.actions == ["start", "stop", "start", "stop", "start", "start", "stop"]

    call("bot", "start", "worker")
    monkeypatch.setenv("FLEET_NAME", "example")
    monkeypatch.setenv("BOT_ID", "manager")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    monkeypatch.setenv("FLEET_ROOT", str(root))
    monkeypatch.setenv("BOT_DIR", str(root / "runtime/bots/manager"))
    monkeypatch.setenv("CLAUDLOBBY_RELEASE_ID", release.release_id)
    manager_bounce = call("bot", "restart", "worker")["data"]
    assert manager_bounce["changed"] is True
    assert native.actions[-2:] == ["start", "start"]
    assert call("bot", "restart", "manager", expected=4)["error"]["code"] == "conflict"

    native.handoff_rc = 3
    incomplete_handoff = call("bot", "restart", "worker")["data"]
    assert incomplete_handoff["handoff"] == "unavailable"
    assert incomplete_handoff["readiness"] == "bridge_ready"
    assert call("bot", "restart", "worker", "--ceiling", "0", expected=2)["error"]["code"] == "invalid_argument"
    before = len(native.actions)
    assert call("bot", "restart", "worker", "--ceiling", "37")["data"]["readiness"] == "bridge_ready"
    assert native.fence_args[-1][-1] == "37"
    assert len(native.actions) == before + 1

    # The generated weekly timer carries fleet/root scope, not BOT_ID. Run the
    # actual public CLI through selected admission and this recorded native
    # adapter; a shell CLI stub would miss the origin-binding regression.
    for key in ("FLEET_NAME", "BOT_ID", "BOT_DIR"):
        monkeypatch.delenv(key)
    monkeypatch.setenv("CLAUDLOBBY_FLEET", "example")
    monkeypatch.setenv("FLEET_ROOT", str(root))
    before = len(native.actions)
    timer_bounce = call("bot", "restart", "worker", "--ceiling", "37")["data"]
    assert timer_bounce["readiness"] == "bridge_ready"
    assert len(native.actions) == before + 1 and native.fence_args[-1][-1] == "37"
    monkeypatch.setenv("FLEET_ROOT", str(root / "other-fleet"))
    assert call("bot", "restart", "worker", expected=4)["error"]["code"] == "conflict"
    assert len(native.actions) == before + 1
