"""#2700: bot start of a bot whose unit file is gone while its enablement link stayed.

systemd keeps listing a unit that has no file while a ``default.target.wants`` link
still names it (not-found), and the enrollment inventory refused bot start for such a
bot as a loaded consumer lacking its installed bytes. This drives the worker CLI over
the fake native adapter of ``tests/test_bot_operations.py`` with the real inventory
read that bot start makes before and after it enrolls the unit.
"""

import importlib.util
import shlex
import subprocess

from claudlobby import bot_operations
from claudlobby.config_units import current_git_declarations
from tests.fixtures.current_runtime import current_runtime as cold, tmp_path  # noqa: F401
from tests.test_bot_operations import _worker_cli

# Bound at import, before _worker_cli replaces it with the harness's own observation.
REAL_OBSERVED = bot_operations._observed


def test_bot_start_installs_a_unit_listed_only_through_a_dangling_wants_link(cold, monkeypatch, capsys):
    worker = _worker_cli(cold, monkeypatch, capsys)
    native, host = worker.native, worker.host
    name = native.target
    declaration = next(row for row in current_git_declarations(host.snapshot, "Linux")
                       if row.source.name == name)
    unit = host.directory / name
    link = host.directory / "default.target.wants" / name
    link.parent.mkdir()
    link.symlink_to(unit)
    unit.unlink()  # The file is gone and its link stays, as on the host in #2700.
    host.states[name] = "not-found not-found inactive"  # still listed: the catalogue's loaded row
    loader = importlib.util.spec_from_file_location("unit_owner", host.package.native / "bot-unit-owner.py")
    owner = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(owner)
    serve = native.call

    def call(function, *args, timeout=30):
        if function == "svc_bot_unit_owned_by":
            native.calls.append(function)
            return subprocess.CompletedProcess([function], owner.main(*map(str, args)), "", "")
        if function != "svc_inventory_properties":
            return serve(function, *args, timeout=timeout)
        native.calls.append(function)
        assert args == (name,)
        if unit.exists():  # Enrolled: what systemd shows for the installed, started unit.
            props = {"LoadState": "loaded", "ActiveState": host.states[name].split()[-1],
                     "UnitFileState": "enabled", "FragmentPath": str(unit),
                     "WorkingDirectory": str(declaration.working_directory),
                     "Environment": " ".join(shlex.quote(f"{key}={value}")
                                             for key, value in declaration.environment),
                     "ExecStart": "{ path=/bin/true ; argv[]=/bin/true ; }"}
        else:  # Listed with no file: systemctl show omits ExecStart for such a unit.
            props = {"LoadState": "not-found", "ActiveState": "inactive", "UnitFileState": "",
                     "FragmentPath": "", "WorkingDirectory": "", "Environment": ""}
        props.update(Id=name, DropInPaths="", NeedDaemonReload="no", Triggers="", TriggeredBy="")
        return subprocess.CompletedProcess([function], 0, "".join(f"{key}={value}\n"
                                                                  for key, value in props.items()), "")

    native.call = call
    monkeypatch.setattr(bot_operations, "_observed", REAL_OBSERVED)
    started = worker.call("bot", "start", "worker")["data"]
    assert started["state"] == "running" and started["changed"] is True
    assert native.actions == ["start"]
    # The inventory proved the listed unit quiet before anything was enrolled, and
    # enrollment put the file back where the link points.
    assert native.calls.index("svc_activation_quiet") < native.calls.index("svc_bot_enroll_exact")
    assert link.exists() and unit.read_bytes() == declaration.source.read_bytes()
