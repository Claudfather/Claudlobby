"""The failed-units read (#2532): which owned units are failed, or running, now.

The selected plan's unit manifest is played by planned_units returning
declarations, and the native state by a fake adapter's svc_inventory_failed
text. The systemd blocks keep the shape `systemctl --user show` printed on a
systemd 252 host (Result and ExecMainStatus first); the launchd rows are a test
double in launchctl list's grammar, since no Mac ran this.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from claudlobby import activation_state, config_plan, config_units
from claudlobby.failed_units import read_failed_units
from claudlobby.supervision_inventory import InventoryError, UnitDeclaration

PLAN, RELEASE = "p-" + "a" * 64, "r-test"


def _declared(name, scope, fleet=None, bot=None):
    return UnitDeclaration(Path("/generated") / name, scope, Path("/work"), RELEASE, (), fleet, bot)


LINUX = (_declared("com.t.creds-check.service", "fleet", "F"), _declared("com.t.creds-check.timer", "fleet", "F"),
         _declared("com.t.job-failed@.service", "fleet", "F"), _declared("claudlobby-disk-monitor.service", "host"),
         _declared("com.t.b1.service", "bot", "F", "b1"))
DARWIN = (_declared("com.t.creds-check.plist", "fleet", "F"), _declared("claudlobby-disk-monitor.plist", "host"),
          _declared("com.t.b1.plist", "bot", "F", "b1"))


class FakeAdapter:
    def __init__(self, text="", error=None):
        self.text, self.error, self.calls = text, error, []

    def read(self, function, *args):
        self.calls.append((function, *args))
        if self.error:
            raise self.error
        return self.text


@pytest.fixture
def selected(monkeypatch):
    scene = {"selection": {"plan_id": PLAN, "release_id": RELEASE, "activation_id": "a-1"},
             "plan_release": RELEASE, "platforms": []}
    monkeypatch.setattr(activation_state, "read_selection", lambda root: scene["selection"])
    monkeypatch.setattr(config_plan, "read_plan",
                        lambda root, plan_id: SimpleNamespace(release_id=scene["plan_release"]))

    def planned(plan, platform):
        scene["platforms"].append(platform)
        return tuple((declaration, {}) for declaration in (LINUX if platform == "Linux" else DARWIN))

    monkeypatch.setattr(config_units, "planned_units", planned)
    return scene


def _unit(name, active, sub, result="success", status=None):
    block = {"Result": result} | ({"ExecMainStatus": status} if status is not None else {})
    return block | {"Id": name, "ActiveState": active, "SubState": sub}


def _systemd(*blocks):
    return "manager\tLinux\n" + "".join(
        "".join(f"{key}={value}\n" for key, value in block.items()) + "\n" for block in blocks)


def _read(text):
    return read_failed_units(Path("/root"), adapter=FakeAdapter(text))


def test_owned_host_and_fleet_units_failed_now_are_listed_with_what_decided_it(selected):
    data = _read(_systemd(_unit("com.t.creds-check.service", "failed", "failed", "exit-code", 4),
                          _unit("claudlobby-disk-monitor.service", "failed", "failed", "exit-code", 1)))
    assert data == {"release_id": RELEASE, "platform": "Linux", "declared": 5, "units": [
        {"unit": "com.t.creds-check.service", "scope": "fleet", "fleet": "F", "bot": None, "surface": "systemd",
         "state": "failed", "detail": "ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=4",
         "template": None},
        {"unit": "claudlobby-disk-monitor.service", "scope": "host", "fleet": None, "bot": None,
         "surface": "systemd", "state": "failed",
         "detail": "ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=1", "template": None}]}
    assert selected["platforms"] == ["Linux"]


def test_ownership_is_the_plans_declaration_never_a_units_spelling(selected):
    # Each of these is failed, and each is spelt like something the plan owns:
    # the fleet's own prefix, an instance of a template it never declared, an
    # operator's own unit. None is owned, so none is listed.
    data = _read(_systemd(_unit("com.t.other.service", "failed", "failed", "exit-code", 1),
                          _unit("unit-failed@operator-monitor.service", "failed", "failed", "exit-code", 1),
                          _unit("operator-monitor.service", "failed", "failed", "exit-code", 1)))
    assert data["units"] == []


def test_a_bot_unit_is_listed_with_its_scope_for_the_reader_to_choose(selected):
    data = _read(_systemd(_unit("com.t.b1.service", "failed", "failed", "exit-code", 1)))
    assert [(unit["unit"], unit["scope"], unit["fleet"], unit["bot"]) for unit in data["units"]] == [
        ("com.t.b1.service", "bot", "F", "b1")]


def test_an_instance_of_a_declared_template_is_listed_with_its_template(selected):
    data = _read(_systemd(_unit("com.t.job-failed@com.t.creds-check.service.service", "failed", "failed",
                                "exit-code", 3)))
    assert [(unit["unit"], unit["scope"], unit["fleet"], unit["template"]) for unit in data["units"]] == [
        ("com.t.job-failed@com.t.creds-check.service.service", "fleet", "F", "com.t.job-failed@.service")]


def test_a_unit_between_states_is_running_and_a_settled_one_is_not_listed(selected):
    data = _read(_systemd(_unit("com.t.creds-check.service", "activating", "start"),
                          _unit("com.t.creds-check.timer", "deactivating", "stop"),
                          _unit("claudlobby-disk-monitor.service", "reloading", "reload"),
                          _unit("com.t.job-failed@x.service", "inactive", "dead", status=0),
                          _unit("com.t.job-failed@y.service", "active", "running", status=0)))
    assert [(unit["unit"], unit["state"]) for unit in data["units"]] == [
        ("com.t.creds-check.service", "running"), ("com.t.creds-check.timer", "running"),
        ("claudlobby-disk-monitor.service", "running")]


def test_launchd_lists_a_job_not_running_after_a_non_zero_exit(selected):
    data = _read("manager\tDarwin\nPID\tStatus\tLabel\n-\t1\tcom.t.creds-check\n"
                 "123\t78\tclaudlobby-disk-monitor\n-\t-9\tcom.t.b1\n-\t2\tforeign.job\n")
    assert selected["platforms"] == ["Darwin"]
    assert data["platform"] == "Darwin" and data["declared"] == 3
    assert data["units"] == [
        {"unit": "com.t.b1", "scope": "bot", "fleet": "F", "bot": "b1", "surface": "launchd", "state": "failed",
         "detail": "last exit status -9", "template": None},
        {"unit": "com.t.creds-check", "scope": "fleet", "fleet": "F", "bot": None, "surface": "launchd",
         "state": "failed", "detail": "last exit status 1", "template": None},
        {"unit": "claudlobby-disk-monitor", "scope": "host", "fleet": None, "bot": None, "surface": "launchd",
         "state": "running", "detail": "last exit status 78, running as pid 123", "template": None}]


def test_a_launchd_job_whose_last_exit_was_zero_is_not_listed(selected):
    data = _read("manager\tDarwin\nPID\tStatus\tLabel\n-\t0\tcom.t.creds-check\n"
                 "456\t0\tclaudlobby-disk-monitor\n")
    assert data["units"] == []


@pytest.mark.parametrize("text, message", [
    ("", "named no manager"),
    ("manager\tSunOS\n", "named no manager"),
    ("manager\tLinux\nId=a.service\nActiveState\n", "ambiguous native unit properties"),
    ("manager\tLinux\nId=a.service\nId=b.service\nActiveState=failed\n", "ambiguous native unit properties"),
    ("manager\tLinux\nResult=exit-code\nId=a.service\n", "no Id or ActiveState"),
    ("manager\tDarwin\n-\t1\tcom.t.creds-check\n", "printed no header"),
    ("manager\tDarwin\nPID\tStatus\tLabel\n-\tx\tcom.t.creds-check\n", "unknown launchctl list row"),
])
def test_output_it_cannot_read_refuses_rather_than_reading_as_nothing_failed(selected, text, message):
    with pytest.raises(InventoryError, match=message):
        _read(text)


def test_a_host_with_no_selected_release_owns_no_unit_yet(selected):
    selected["selection"] = None
    adapter = FakeAdapter(_systemd())
    with pytest.raises(InventoryError, match="no release is selected"):
        read_failed_units(Path("/root"), adapter=adapter)
    assert adapter.calls == []


def test_a_plan_of_another_release_refuses(selected):
    selected["plan_release"] = "r-other"
    with pytest.raises(InventoryError, match="differs from the selected release"):
        _read(_systemd())


def test_an_adapter_that_cannot_read_refuses(selected):
    adapter = FakeAdapter(error=InventoryError("svc_inventory_failed failed (3): no user manager"))
    with pytest.raises(InventoryError, match="no user manager"):
        read_failed_units(Path("/root"), adapter=adapter)
    assert adapter.calls == [("svc_inventory_failed",)]
