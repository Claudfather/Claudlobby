"""Enrollment evidence on private files and recorded manager observations only."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

import pytest

from claudlobby import supervision_inventory as inventory
from claudlobby.supervision_inventory import (
    Adapter, InventoryError, UnitDeclaration, _darwin_disabled, _darwin_source, collect_enrollment,
    legacy_linux_declarations,
)
from tests.package_fixtures import source_package


FIXTURES = Path(__file__).parent / "fixtures"


def _apple_xml(*, duplicate=False):
    extra = b"<key>Label</key><string>shadow</string>" if duplicate else b""
    return (b"<?xml version=1.0 encoding=UTF-8?>\n"
            b"<!DOCTYPE plist PUBLIC -//Apple//DTD PLIST 1.0//EN "
            b"http://www.apple.com/DTDs/PropertyList-1.0.dtd>\n"
            b"<plist version=1.0><dict><key>Label</key><string>com.apple.foreign</string>"
            + extra + b"<key>Program</key><string>/System/Library/foreign</string></dict></plist>")


def test_nonstandard_apple_xml_remains_raw_and_classifies_foreign(tmp_path, monkeypatch):
    raw = _apple_xml()
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    foreign = obs.installed / "com.apple.foreign.plist"
    foreign.write_bytes(raw)
    if sys.platform != "darwin":
        normalized = plistlib.dumps({"Label": "com.apple.foreign", "Program": "/System/Library/foreign"})
        monkeypatch.setattr(inventory.sys, "platform", "darwin")
        monkeypatch.setattr(inventory.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, normalized, b""))
    observed = collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                                  bootstrap_empty=True).require_complete()
    assert observed.foreign == (str(foreign),)
    assert observed.observed_files[0].content == raw
    assert foreign.read_bytes() == raw
    obs.add("com.fixture.owned.plist", scope="bot")
    assert obs.collect().require_complete().foreign == (str(foreign),)


def test_native_plist_normalization_cannot_erase_duplicate_keys(monkeypatch):
    raw = _apple_xml(duplicate=True)
    if sys.platform != "darwin":
        normalized = plistlib.dumps({"Label": "shadow", "Program": "/System/Library/foreign"})
        monkeypatch.setattr(inventory.sys, "platform", "darwin")
        monkeypatch.setattr(inventory.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, normalized, b""))
    with pytest.raises(InventoryError, match="lost keys"):
        _darwin_source(raw)


def test_foreign_launchd_jobs_use_bounded_identity_and_ignore_pid_churn(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.add("com.fixture.owned.plist", scope="bot")
    foreign = obs.installed / "com.fixture.foreign.plist"
    foreign.write_bytes(plistlib.dumps({"Label": "com.fixture.foreign", "MachServices": {"foreign": True}}))
    external = obs.installed / "com.fixture.external.plist"
    external.write_bytes(plistlib.dumps({"Label": "com.fixture.external", "Program": "/Applications/Foreign",
                                        "WorkingDirectory": str(tmp_path / "external")}))
    obs.launchd["application.fixture.plist"] = (
        "gui/501/application.fixture = {\n\tpath = (submitted by runningboardd.1)\n"
        "\ttype = Submitted\n\tstate = running\n\tprogram = /Applications/Foreign\n"
        "\tdomain = gui/501 [1]\n\tpid = 123\n}\n")
    obs.launchd["com.fixture.xpc.plist"] = (
        "user/501/com.fixture.xpc = {\n\tpath = /Applications/Foreign/XPC\n"
        "\ttype = XPCService\n\tstate = running\n\tprogram = /Applications/Foreign/XPC\n"
        "\tdomain = user/501\n\tpid = 124\n}\n")
    obs.launchd["com.fixture.submitted.plist"] = (
        "gui/501/com.fixture.submitted = {\n\tpath = (submitted by runningboardd.1)\n"
        "\ttype = Submitted\n\tmanaged_by = runningboardd\n\tstate = not running\n"
        "\tprogram identifier = com.fixture.foreign\n\tdomain = gui/501 [1]\n}\n")
    catalog_reads = 0

    def runner(command, **kwargs):
        nonlocal catalog_reads
        if command[5] == "svc_inventory_properties" and command[6] == "gui/501/com.fixture.xpc":
            return subprocess.CompletedProcess(command, 113, "", 'Could not find service "com.fixture.xpc"')
        if command[5] == "svc_inventory_catalog":
            catalog_reads += 1
            text = obs.catalog()
            if catalog_reads == 2:
                text = text.replace("123\t0\tapplication.fixture", "999\t0\tapplication.fixture")
            return subprocess.CompletedProcess(command, 0, text, "")
        return obs.runner(command, **kwargs)

    result = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                runner=runner).require_complete()
    assert str(foreign) in result.foreign
    assert str(external) in result.foreign
    assert not any(function == "svc_bot_unit_owned_by" and args[0] == str(foreign)
                   for function, args in obs.calls)
    assert sum(function == "svc_bot_unit_owned_by" and args[0] == str(external)
               for function, args in obs.calls) == 1
    assert catalog_reads == 2
    assert any(function == "svc_inventory_properties" and args[0] == "user/501/com.fixture.xpc"
               for function, args in obs.calls)
    obs.launchd["application.fixture.plist"] = obs.launchd["application.fixture.plist"].replace(
        "/Applications/Foreign", str(obs.root / "private-program"))
    with pytest.raises(InventoryError, match="reviewed root or release"):
        collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                           runner=runner).require_complete()


def test_exact_bot_inventory_ignores_unrelated_timer_activity_but_refuses_target_drift(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    bot_name = "com.fixture.worker.plist"
    timer_name = "com.fixture.keepalive.plist"
    bot_installed = obs.add(bot_name, scope="bot")
    obs.add(timer_name, scope="host")
    timer_source = plistlib.loads((obs.installed / timer_name).read_bytes())
    obs.launchd[timer_name] = observed_print("gui/501/com.fixture.keepalive", timer_source,
                                             obs.installed / timer_name, active=False)
    # The catalog saw an active watchdog; its print saw the tick finish.
    with pytest.raises(InventoryError, match="launchd list/print activity changed"):
        obs.collect().require_complete()
    before = len(obs.calls)
    exact = frozenset({bot_name})
    observed = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                  runner=obs.runner, only_names=exact).require_complete()
    assert len(observed.units) == 1 and observed.units[0].installed[0].path == str(bot_installed)
    assert not any(function == "svc_inventory_properties" and "keepalive" in " ".join(args)
                   for function, args in obs.calls[before:])

    bot_source = plistlib.loads(bot_installed.read_bytes())
    obs.launchd[bot_name] = observed_print("gui/501/com.fixture.worker", bot_source,
                                           bot_installed, active=False)
    with pytest.raises(InventoryError, match="launchd list/print activity changed"):
        collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                           runner=obs.runner, only_names=exact).require_complete()


def test_transient_foreign_names_need_fresh_absence_or_bounded_new_identity(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.add("com.fixture.owned.plist", scope="bot")
    for name in ("vanished", "arrived"):
        obs.launchd[f"com.fixture.{name}.plist"] = (
            f"user/501/com.fixture.{name} = {{\n\tpath = /Applications/Foreign/XPC\n"
            "\ttype = XPCService\n\tstate = running\n\tprogram = /Applications/Foreign/XPC\n"
            "\tdomain = user/501\n\tpid = 124\n}\n")
    reads = 0

    def runner(command, **kwargs):
        nonlocal reads
        function, target = command[5], command[6] if len(command) > 6 else ""
        if function == "svc_inventory_catalog":
            reads += 1
            removed = "arrived" if reads == 1 else "vanished"
            rows = [row for row in obs.catalog().splitlines() if f"com.fixture.{removed}" not in row]
            return subprocess.CompletedProcess(command, 0, "\n".join(rows) + "\n", "")
        if function == "svc_inventory_properties" and target.endswith("com.fixture.vanished"):
            return subprocess.CompletedProcess(command, 113, "", 'Could not find service "com.fixture.vanished"')
        if function == "svc_inventory_properties" and target.startswith("gui/501/com.fixture.arrived"):
            return subprocess.CompletedProcess(command, 113, "", 'Could not find service "com.fixture.arrived"')
        return obs.runner(command, **kwargs)

    collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                       runner=runner).require_complete()
    obs.launchd["com.fixture.arrived.plist"] = obs.launchd["com.fixture.arrived.plist"].replace(
        "/Applications/Foreign/XPC", str(obs.root / "owned"))
    reads = 0
    with pytest.raises(InventoryError, match="new loaded ownership is unknown"):
        collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                           runner=runner).require_complete()


def test_managed_launchd_allows_only_observed_semaphore_diagnostic(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.add("com.fixture.owned.plist", scope="bot")
    text = obs.launchd["com.fixture.owned.plist"]
    obs.launchd["com.fixture.owned.plist"] = text.replace(
        "\n}", "\n\tsemaphores = {\n\t\tsuccessful exit => 0\n\t}\n}")
    obs.collect().require_complete()
    obs.launchd["com.fixture.owned.plist"] = obs.launchd["com.fixture.owned.plist"].replace(
        "successful exit", "unknown signal")
    with pytest.raises(InventoryError, match="semaphore diagnostic"):
        obs.collect().require_complete()


def test_selected_adapter_ownership_uses_its_interpreter_not_path_python(tmp_path, monkeypatch):
    bot = tmp_path / "bot"
    bot.mkdir()
    unit = tmp_path / "private.plist"
    unit.write_bytes(plistlib.dumps({"Label": "private", "WorkingDirectory": str(bot)}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invoked = tmp_path / "ambient-python-invoked"
    fake_python = fake_bin / "python3"
    fake_python.write_text(f"#!/bin/sh\ntouch '{invoked}'\nexit 88\n")
    fake_python.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")
    adapter = Adapter(source_package())

    assert adapter.call("svc_bot_unit_owned_by", unit, bot).returncode == 0
    other = tmp_path / "other"
    other.mkdir()
    assert adapter.call("svc_bot_unit_owned_by", unit, other).returncode == 1
    assert not invoked.exists()

    with monkeypatch.context() as patch:
        patch.setattr(inventory.sys, "executable", str(tmp_path / "missing-selected-python"))
        result = adapter.call("svc_bot_unit_owned_by", unit, bot)
    assert result.returncode == 3
    assert not invoked.exists()


def observed_print(target, source, installed, *, active=True, calendar=False):
    """Actual macOS 26.1 captures, with private values replaced by test identity.

    Both captures came from read-only exact-domain queries on 2026-09-28. Argument
    and environment rows retain the observed raw/tab/arrow grammar. No native
    bootstrap, bootout or enable/disable was used to produce these fixtures.
    """
    fixture = "launchctl-print-26.1-calendar.txt" if calendar else "launchctl-print-26.1-active.txt" if active else "launchctl-print-26.1-idle.txt"
    text = (FIXTURES / fixture).read_text().replace("gui/501/com.fixture.observed", target)
    values = {"path": str(installed), "program": source["ProgramArguments"][0],
              "working directory": source["WorkingDirectory"], "domain": "gui/501 [100002]"}
    for key, value in values.items():
        text = text.replace(f"\t{key} = <redacted>", f"\t{key} = {value}")
    start = text.index("\targuments = {\n")
    end = text.index("\t}\n", start) + len("\t}\n")
    text = text[:start] + "\targuments = {\n" + "".join("\t\t" + arg + "\n" for arg in source["ProgramArguments"]) + "\t}\n" + text[end:]
    start = text.index("\tenvironment = {\n")
    end = text.index("\t}\n", start) + len("\t}\n")
    env = {**source["EnvironmentVariables"], "OSLogRateLimit": "64", "XPC_SERVICE_NAME": source["Label"]}
    return text[:start] + "\tenvironment = {\n" + "".join(f"\t\t{key} => {value}\n" for key, value in env.items()) + "\t}\n" + text[end:]


class Observations:
    def __init__(self, tmp_path):
        self.root = tmp_path / "data"
        self.root.mkdir()
        self.installed = tmp_path / "home/config/systemd/user"
        self.installed.mkdir(parents=True)
        self.search_dirs = [self.installed]
        self.package = source_package()
        self.declarations, self.properties, self.calls = [], {}, []
        # Catalog rows/properties outside add(): ghosts, templates, instances.
        self.rows, self.extra, self.failures = [], {}, {}
        self.manager, self.domain = "Linux", ""
        self.disabled = '\n\tdisabled services = {\n\t}\n'
        self.launchd = {}
        self.launchd_active = {}
        self.env = {
            "CLAUDLOBBY_ROOT": str(self.root), "FLEET_ROOT": str(self.root / "local/alpha"),
            "CLAUDLOBBY_NATIVE_DIR": str(self.package.native),
            "CLAUDLOBBY_LIBRARY_DIR": str(self.package.library),
            "CLAUDLOBBY_CLI": str(tmp_path / "release/bin/claudlobby"),
            "CLAUDLOBBY_ARTIFACT_ID": "old-artifact",
        }
        spec = importlib.util.spec_from_file_location("inventory_owner", self.package.native / "bot-unit-owner.py")
        self.owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.owner)

    def add(self, name, scope="host", *, working=None, declared=True, service=None):
        working = working or (self.root / "runtime/alpha/bots/worker" if scope == "bot" else self.root)
        working.mkdir(parents=True, exist_ok=True)
        source = self.root / "generated" / name
        source.parent.mkdir(exist_ok=True)
        if name.endswith(".plist"):
            content = plistlib.dumps({"Label": name[:-6], "WorkingDirectory": str(working),
                                     "ProgramArguments": [str(self.package.native / "start-bot.sh"), str(working)],
                                     "EnvironmentVariables": self.env})
        elif service:
            content = f"[Timer]\nUnit={service}\n".encode()
        else:
            content = (f"[Service]\nWorkingDirectory={working}\nExecStart={self.package.native}/job.sh\n"
                       + "".join(f"Environment={key}={value}\n" for key, value in self.env.items())).encode()
        source.write_bytes(content)
        target = self.installed / name
        target.write_bytes(content)
        target.chmod(0o640)
        if name.endswith(".plist"):
            self.launchd[name] = observed_print("gui/501/" + name[:-6], plistlib.loads(content), target)
            self.launchd_active[name] = True
        self.properties[name] = {
            "Id": name, "LoadState": "loaded", "ActiveState": "active", "UnitFileState": "enabled",
            "FragmentPath": str(target), "WorkingDirectory": str(working) if not service else "",
            "Environment": " ".join(shlex.quote(f"{key}={value}") for key, value in self.env.items()) if not service else "",
            "ExecStart": f"{{ path={self.package.native}/job.sh ; argv[]={self.package.native}/job.sh ; }}" if not service else "",
            "DropInPaths": "", "NeedDaemonReload": "no", "Triggers": service or "", "TriggeredBy": "",
        }
        declaration = UnitDeclaration(source, scope, working, "reviewed-release", tuple(self.env.items()),
                                      fleet="alpha" if scope != "host" else None,
                                      bot="worker" if scope == "bot" else None, service=service)
        if declared:
            self.declarations.append(declaration)
        return target

    def catalog(self):
        lines = [f"manager\t{self.manager}", *(f"directory\t{path}" for path in self.search_dirs)]
        if self.manager == "Darwin":
            lines += ["domain\tgui/501", "PID\tStatus\tLabel"]
            lines += [("710" if self.launchd_active.get(name, True) else "-") + "\t0\t" + name[:-6] for name in sorted(self.launchd)]
        else:
            lines += [f"{kind}\t{name}" for kind in ("installed", "loaded") for name in sorted(self.properties)]
            lines += self.rows
        return "\n".join(lines) + "\n"

    def runner(self, command, **kwargs):
        assert command[:2] == ["/bin/bash", "-c"]
        assert command[4] == str(self.package.native)
        function, args = command[5], command[6:]
        self.calls.append((function, args))
        assert function in {"svc_inventory_catalog", "svc_inventory_properties", "svc_inventory_disabled", "svc_bot_unit_owned_by"}
        if function == "svc_inventory_catalog":
            output, rc = self.catalog(), 0
        elif function == "svc_inventory_properties" and args[0] in self.failures:
            rc, stderr = self.failures[args[0]]
            return subprocess.CompletedProcess(command, rc, "", stderr)
        elif function == "svc_inventory_properties":
            output = (self.launchd[args[0].split("/")[-1] + ".plist"] if self.manager == "Darwin" else
                      "".join(f"{key}={value}\n" for key, value in
                              {**self.extra, **self.properties}[args[0]].items()))
            rc = 0
        elif function == "svc_inventory_disabled":
            assert args == ["gui/501"]
            output, rc = self.disabled, 0
        else:
            output, rc = "", self.owner.main(*args)  # actual shared predicate; no cloned parser
        return subprocess.CompletedProcess(command, rc, output, "")

    def collect(self):
        return collect_enrollment(self.root, tuple(self.declarations), package=self.package, runner=self.runner)


@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
def test_explicit_empty_bootstrap_proves_full_catalog_with_foreign_units(tmp_path, manager):
    obs = Observations(tmp_path)
    obs.manager = manager
    obs.env = {"CLAUDLOBBY_ROOT": str(tmp_path / "foreign-root")}
    foreign = obs.add("foreign.service" if manager == "Linux" else "com.foreign.plist",
                      working=tmp_path / "foreign-root", declared=False)
    original = foreign.read_bytes()
    with pytest.raises(InventoryError, match="empty generated manifest"):
        obs.collect()
    inventory = collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                                   bootstrap_empty=True).require_complete()
    assert inventory.bootstrap_empty is True and inventory.units == ()
    assert inventory.foreign == (str(foreign),) and inventory.observed_files[0].content == original
    assert sum(function == "svc_inventory_catalog" for function, _ in obs.calls) == 2
    assert any(function == "svc_inventory_properties" for function, _ in obs.calls)
    assert foreign.read_bytes() == original
    if manager == "Darwin":
        obs.launchd.clear()  # installed but unloaded foreign jobs still coexist
        collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                           bootstrap_empty=True).require_complete()
    with pytest.raises(InventoryError, match="incomplete"):
        replace(inventory, bootstrap_empty=False).require_complete()
    # A changed/unknown managed observation is not another empty host.
    obs.env = {"CLAUDLOBBY_ROOT": str(obs.root)}
    obs.add("owned.service" if manager == "Linux" else "com.owned.plist", "bot", declared=False)
    if manager == "Darwin":
        obs.launchd.clear()  # no loaded definition to reveal this owned bot
    with pytest.raises(InventoryError, match="owned consumer"):
        collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                           bootstrap_empty=True).require_complete()


def test_linux_bootstrap_classifies_stock_alias_mask_and_shadowed_vendor_units(tmp_path):
    obs = Observations(tmp_path)
    stock = obs.add("systemd-exit.service", declared=False)
    stock.write_text("[Unit]\nDescription=stock\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n")
    obs.properties[stock.name].update(WorkingDirectory="", Environment="", ExecStart="/usr/bin/true")

    alias = obs.add("exit-alias.service", declared=False)
    alias.unlink()
    alias.symlink_to(stock)
    obs.properties[alias.name].update(Id=stock.name, FragmentPath=str(stock),
                                      WorkingDirectory="", Environment="", ExecStart="/usr/bin/true")

    masked = obs.add("pulseaudio.service", declared=False)
    masked.unlink()
    masked.symlink_to("/dev/null")
    obs.properties[masked.name].update(LoadState="masked", UnitFileState="masked",
                                        FragmentPath="/dev/null", WorkingDirectory="",
                                        Environment="", ExecStart="")

    override = obs.add("desktop.service", declared=False)
    override.write_text(stock.read_text())
    vendor = tmp_path / "usr/lib/systemd/user"
    vendor.mkdir(parents=True)
    (vendor / override.name).write_text(stock.read_text())
    obs.search_dirs.append(vendor)
    obs.properties[override.name].update(WorkingDirectory="", Environment="", ExecStart="/usr/bin/true",
                                          DropInPaths=str(tmp_path / "override.conf"))

    inventory = collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                                   bootstrap_empty=True).require_complete()
    assert set(inventory.foreign) == {str(stock), str(alias), str(masked), str(override),
                                      str(vendor / override.name)}
    assert any(item.path == str(masked) and item.link == "/dev/null"
               for item in inventory.observed_files)
    inventory.check_files()
    # A foreign definition that actually names this root still refuses.
    stock.write_text(stock.read_text().replace("ExecStart=/usr/bin/true",
                                               f"ExecStart={obs.root}/owned"))
    with pytest.raises(InventoryError, match="owned consumer"):
        collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                           bootstrap_empty=True).require_complete()


def test_linux_bootstrap_classifies_pi_ghost_special_template_and_continued_units(tmp_path):
    """Classes observed read-only on a Raspberry Pi user manager (2026-09-30)."""
    obs = Observations(tmp_path)
    blank = {"WorkingDirectory": "", "Environment": "", "ActiveState": "inactive"}
    # A dependency names a unit that exists nowhere; systemctl omits ExecStart.
    ghost = "pipewire-media-session.service"
    obs.rows.append(f"loaded\t{ghost}")
    obs.extra[ghost] = {"Id": ghost, "LoadState": "not-found", "ActiveState": "inactive",
                        "UnitFileState": "", "FragmentPath": "", "WorkingDirectory": "",
                        "Environment": "", "DropInPaths": "", "NeedDaemonReload": "no",
                        "Triggers": "", "TriggeredBy": ""}
    # A valid special unit with [Service] but no ExecStart.
    special = obs.add("systemd-exit.service", declared=False)
    special.write_text("[Unit]\nDescription=Exit the Session\n[Service]\nType=oneshot\n")
    obs.properties[special.name].update(blank)
    del obs.properties[special.name]["ExecStart"]
    # An uninstantiated template: show refuses the name, the file is still read.
    template = obs.installed / "wireplumber@.service"
    template.write_text("[Service]\nExecStart=/usr/bin/wireplumber\n")
    obs.rows.append(f"installed\t{template.name}")
    obs.failures[template.name] = (1, f"Failed to get properties: Unit name {template.name} "
                                      "is neither a valid invocation ID nor unit name.")
    instance = "wireplumber@main.service"
    obs.rows.append(f"loaded\t{instance}")
    obs.extra[instance] = {**obs.extra[ghost], "Id": instance, "LoadState": "loaded",
                           "UnitFileState": "static", "FragmentPath": str(template),
                           "ExecStart": "{ path=/usr/bin/wireplumber ; argv[]=/usr/bin/wireplumber ; }"}
    # An unrelated oneshot whose ExecStart continues across lines.
    story = obs.add("storydump-scheduling-monitor.service", declared=False)
    continued = ("[Unit]\nDescription=monitor\n[Service]\nType=oneshot\n"
                 "ExecStart=/usr/bin/python3 %h/ops/storydump/scripts/scheduling_monitor.py \\\n"
                 "    --once \\\n    --quiet\n")
    story.write_text(continued)
    obs.properties[story.name].update(blank, ExecStart="{ path=/usr/bin/python3 ; }")

    def bootstrap():
        return collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                                  bootstrap_empty=True).require_complete()

    inventory = bootstrap()
    assert set(inventory.foreign) == {str(special), str(template), str(story)}
    assert any(item.path == str(template) for item in inventory.observed_files)
    assert ("svc_inventory_properties", [template.name]) not in obs.calls
    assert ("svc_inventory_properties", [instance]) in obs.calls

    # Negative controls: each class still refuses when it could hide ownership.
    obs.extra[instance]["ExecStart"] = f"{{ path={obs.root}/owned ; }}"
    with pytest.raises(InventoryError, match=f"{instance}: owned consumer"):
        bootstrap()
    obs.extra[instance]["ExecStart"] = "{ path=/usr/bin/wireplumber ; }"
    template.write_text(f"[Service]\nExecStart={obs.root}/owned\n")
    with pytest.raises(InventoryError, match="wireplumber@.service: owned consumer"):
        bootstrap()
    template.write_text("[Service]\nExecStart=/usr/bin/wireplumber\n")
    obs.extra[ghost]["ActiveState"] = "active"  # a running unit is never an absent reference
    with pytest.raises(InventoryError, match=f"{ghost}: "):
        bootstrap()
    obs.extra[ghost]["ActiveState"] = "inactive"
    for hidden in (continued + f"WorkingDirectory={obs.root} \\\n    /bots\n",
                   continued.replace("    --once \\\n", "    --once \\\n# note\n")):
        story.write_text(hidden)
        with pytest.raises(InventoryError, match=f"{story.name}: installed ownership is unknown"):
            bootstrap()
    story.write_text(continued)
    bootstrap()

    # A declared unit never gets the empty-ExecStart reading.
    (tmp_path / "declared").mkdir()
    owned = Observations(tmp_path / "declared")
    owned.add("owned.service")
    owned.collect().require_complete()
    del owned.properties["owned.service"]["ExecStart"]
    with pytest.raises(InventoryError, match="missing/unknown native unit properties"):
        owned.collect().require_complete()


def test_declared_bot_deenrolled_by_stop_reads_absent_without_execstart(tmp_path):
    """Actual Pi f68 output after bot stop: systemd omits ExecStart entirely."""
    obs = Observations(tmp_path)
    name = "can1747.1745.c1747w.service"
    obs.add(name, "bot").unlink()  # de-enrolled: no installed file, no catalogue rows
    del obs.properties[name]
    absent = {"Environment": "", "WorkingDirectory": "", "Id": name, "Triggers": "",
              "TriggeredBy": "", "LoadState": "not-found", "ActiveState": "inactive",
              "FragmentPath": "", "DropInPaths": "", "UnitFileState": "", "NeedDaemonReload": "no"}
    obs.extra[name] = dict(absent)
    inventory = obs.collect().require_complete()
    (unit,) = inventory.units
    assert unit.installed == () and dict(unit.properties)["ExecStart"] == ""
    only = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                              runner=obs.runner, only_names=frozenset({name})).require_complete()
    assert only.units[0].installed == ()

    # Missing execution identity is never normalized for a present or bound unit.
    for changed in ({"LoadState": "loaded"}, {"ActiveState": "active"},
                    {"FragmentPath": str(obs.installed / name)},
                    {"Environment": f"CLAUDLOBBY_ROOT={obs.root}"},
                    {"WorkingDirectory": str(obs.root)}):
        obs.extra[name] = {**absent, **changed}
        with pytest.raises(InventoryError, match="missing/unknown native unit properties"):
            obs.collect().require_complete()
    # The absent form still refuses while the catalogue reports it loaded.
    obs.extra[name] = dict(absent)
    obs.rows.append(f"loaded\t{name}")
    with pytest.raises(InventoryError, match="loaded consumer lacks installed source bytes"):
        obs.collect().require_complete()


def test_empty_bootstrap_refuses_unknown_catalog_and_prior_selection(tmp_path):
    obs = Observations(tmp_path)
    foreign = obs.add("foreign.service", working=tmp_path / "foreign-root", declared=False)
    obs.properties[foreign.name]["Environment"] = ""
    del obs.properties[foreign.name]["Id"]
    with pytest.raises(InventoryError, match="cannot observe"):
        collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner,
                           bootstrap_empty=True).require_complete()
    obs.root.joinpath("state").mkdir()
    obs.root.joinpath("state/selected-release.json").write_text(json.dumps(
        {"schema": 1, "activation_id": "prior", "release_id": "prior", "plan_id": "prior"}))
    with pytest.raises(InventoryError, match="prior release selection"):
        collect_enrollment(obs.root, (), package=obs.package, runner=obs.runner, bootstrap_empty=True)


def test_unsealed_darwin_source_keeps_exact_unit_ownership_without_release_claim(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.env = {"CLAUDLOBBY_ROOT": str(obs.root)}
    installed = obs.add("com.legacy.owned.plist")
    obs.declarations[0] = replace(obs.declarations[0], release_id="")
    with pytest.raises(InventoryError, match="incomplete declaration"):
        obs.collect()
    inventory = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                   runner=obs.runner, legacy_source=True).require_complete()
    assert inventory.legacy_source is True
    assert inventory.units[0].installed[0].content == installed.read_bytes()
    installed_source = plistlib.loads(installed.read_bytes())
    installed_source["EnvironmentVariables"]["PATH"] = "/reviewed/legacy/bin"
    installed.write_bytes(plistlib.dumps(installed_source))
    obs.launchd[installed.name] = observed_print("gui/501/com.legacy.owned", installed_source, installed)
    sealed_env = {**obs.env, "CLAUDLOBBY_NATIVE_DIR": str(obs.package.native),
                  "CLAUDLOBBY_LIBRARY_DIR": str(obs.package.library),
                  "CLAUDLOBBY_CLI": str(tmp_path / "release/bin/claudlobby"),
                  "CLAUDLOBBY_ARTIFACT_ID": "reviewed", "FLEET_ROOT": str(obs.root / "local/alpha")}
    with pytest.raises(InventoryError, match="installed bytes differ"):
        collect_enrollment(obs.root, (replace(obs.declarations[0], release_id="reviewed-release",
                                              environment=tuple(sealed_env.items())),),
                           package=obs.package, runner=obs.runner).require_complete()
    inventory = collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                                   runner=obs.runner, legacy_source=True).require_complete()
    assert inventory.units[0].generated.content != inventory.units[0].installed[0].content
    obs.launchd[installed.name] = obs.launchd[installed.name].replace("/reviewed/legacy/bin", "/other/bin")
    with pytest.raises(InventoryError, match="loaded launchd definition differs"):
        collect_enrollment(obs.root, tuple(obs.declarations), package=obs.package,
                           runner=obs.runner, legacy_source=True).require_complete()
    installed.write_bytes(installed.read_bytes() + b"\n")
    with pytest.raises(InventoryError, match="enrollment file changed"):
        inventory.check_files()


def test_unsealed_linux_sources_preserve_owned_host_fleet_bot_and_foreign_units(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from claudlobby import config_units

    obs = Observations(tmp_path)
    obs.env = {"CLAUDLOBBY_ROOT": str(obs.root)}
    obs.add("claudlobby-plane.service", scope="host", working=obs.root)
    obs.env.update(FLEET_ROOT=str(obs.root / "local/alpha"), CLAUDLOBBY_FLEET="alpha")
    obs.add("alpha-sweep.service", scope="fleet", working=obs.root)
    timer = obs.add("alpha-sweep.timer", scope="fleet", working=obs.root,
                    service="alpha-sweep.service")
    # The shipped compositor omits Unit= when the service has the same stem.
    (obs.root / "generated/alpha-sweep.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    timer.write_bytes((obs.root / "generated/alpha-sweep.timer").read_bytes())
    obs.env.pop("CLAUDLOBBY_FLEET")
    obs.env["TMUX_TMPDIR"] = "/tmp"
    bot = obs.add("alpha.worker.service", scope="bot")
    foreign = obs.add("pipewire.service", working=tmp_path / "unrelated", declared=False)
    foreign.write_text("[Service]\nWorkingDirectory=/unrelated\nExecStart=/usr/bin/true\n")
    obs.properties[foreign.name].update(WorkingDirectory="/unrelated", Environment="",
                                        ExecStart="/usr/bin/true")

    plan = SimpleNamespace(data_root=obs.root, check_fresh=lambda: None)
    monkeypatch.setattr(config_units, "planned_units", lambda _plan, manager:
                        tuple((item, {}) for item in obs.declarations) if manager == "Linux" else ())
    declarations = legacy_linux_declarations(plan)
    inventory = collect_enrollment(obs.root, declarations, package=obs.package,
                                   runner=obs.runner, legacy_source=True).require_complete()
    assert {unit.target for unit in inventory.units} == {
        "claudlobby-plane.service", "alpha-sweep.service", "alpha-sweep.timer", "alpha.worker.service"}
    assert str(foreign) in inventory.foreign
    assert dict(next(item.declaration.environment for item in inventory.units
                     if item.target == "alpha.worker.service"))["TMUX_TMPDIR"] == "/tmp"

    # An old installed service cannot carry unreviewed bytes into the pause journal.
    bot.write_bytes(bot.read_bytes() + b"\n# changed after source composition\n")
    with pytest.raises(InventoryError, match="installed bytes differ"):
        collect_enrollment(obs.root, declarations, package=obs.package,
                           runner=obs.runner, legacy_source=True).require_complete()
    bot.write_bytes((obs.root / "generated/alpha.worker.service").read_bytes())
    obs.properties["alpha.worker.service"]["Environment"] = "CLAUDLOBBY_ROOT=/other-root"
    with pytest.raises(InventoryError, match="loaded data/fleet/release identity differs"):
        collect_enrollment(obs.root, declarations, package=obs.package,
                           runner=obs.runner, legacy_source=True).require_complete()
    fleet_source = obs.root / "generated/alpha-sweep.service"
    fleet_source.write_bytes(fleet_source.read_bytes().replace(
        b"Environment=CLAUDLOBBY_FLEET=alpha", b"Environment=CLAUDLOBBY_FLEET=other"))
    with pytest.raises(InventoryError, match="different root or fleet owner"):
        legacy_linux_declarations(plan)


def test_legacy_linux_units_without_fleet_root_adopt_only_proven_environment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from claudlobby import config_units

    # 8bc588a-generated fleet and bot units carry no FLEET_ROOT; the candidate
    # roster still declares one.
    obs = Observations(tmp_path)
    fleet_root = str(obs.root / "local/alpha")
    obs.env = {"CLAUDLOBBY_ROOT": str(obs.root), "CLAUDLOBBY_FLEET": "alpha"}
    obs.add("alpha-sweep.service", scope="fleet", working=obs.root)
    obs.add("alpha-sweep.timer", scope="fleet", working=obs.root, service="alpha-sweep.service")
    obs.env = {"CLAUDLOBBY_ROOT": str(obs.root), "TMUX_TMPDIR": "/tmp"}
    bot = obs.add("alpha.worker.service", scope="bot")
    candidate = [replace(item, environment=(*item.environment, ("FLEET_ROOT", fleet_root)))
                 for item in obs.declarations]
    plan = SimpleNamespace(data_root=obs.root, check_fresh=lambda: None)
    monkeypatch.setattr(config_units, "planned_units", lambda _plan, manager:
                        tuple((item, {}) for item in candidate) if manager == "Linux" else ())
    declarations = legacy_linux_declarations(plan)
    assert all("FLEET_ROOT" not in dict(item.environment) for item in declarations)
    inventory = collect_enrollment(obs.root, declarations, package=obs.package,
                                   runner=obs.runner, legacy_source=True).require_complete()
    assert {unit.target for unit in inventory.units} == {
        "alpha-sweep.service", "alpha-sweep.timer", "alpha.worker.service"}

    # An effective FLEET_ROOT the reviewed source does not carry is not adopted.
    effective = obs.properties["alpha.worker.service"]["Environment"]
    obs.properties["alpha.worker.service"]["Environment"] = effective + " FLEET_ROOT=/other"
    with pytest.raises(InventoryError, match="loaded data/fleet/release identity differs"):
        collect_enrollment(obs.root, declarations, package=obs.package,
                           runner=obs.runner, legacy_source=True).require_complete()
    obs.properties["alpha.worker.service"]["Environment"] = effective

    # A present but different fleet root still refuses.
    source = obs.root / "generated/alpha.worker.service"
    source.write_bytes(source.read_bytes() + b"Environment=FLEET_ROOT=/other\n")
    with pytest.raises(InventoryError, match="different root or fleet owner"):
        legacy_linux_declarations(plan)
    source.write_bytes(bot.read_bytes())

    # Without FLEET_ROOT, a bot working directory shared with another unit is ambiguous.
    candidate.append(replace(candidate[-1], source=obs.root / "generated/alpha.other.service", bot="other"))
    with pytest.raises(InventoryError, match="no unique working directory"):
        legacy_linux_declarations(plan)


def test_all_scopes_bytes_links_and_exact_candidate_cleanup(tmp_path):
    obs = Observations(tmp_path)
    obs.add("host.service")
    obs.add("alpha.watch.service", "fleet")
    obs.add("alpha.watch.timer", "fleet", service="alpha.watch.service")
    bot = obs.add("alpha.worker.service", "bot")
    foreign = obs.add("foreign.worker.service", working=tmp_path / "unrelated", declared=False)
    obs.properties[foreign.name]["Environment"] = f"CLAUDLOBBY_ROOT={tmp_path / 'unrelated'}"
    foreign.write_bytes(foreign.read_bytes().replace(
        f"Environment=CLAUDLOBBY_ROOT={obs.root}\n".encode(),
        f"Environment=CLAUDLOBBY_ROOT={tmp_path / 'unrelated'}\n".encode()))
    # Native timer interfaces omit Service-only properties rather than emitting
    # empty values. Do not certify a contract native systemd never supplies.
    for key in ("Environment", "WorkingDirectory", "ExecStart"):
        del obs.properties["alpha.watch.timer"][key]
    # An installed symlink is part of the saved source, not a copied file.
    bot.unlink()
    bot.symlink_to(obs.declarations[-1].source)
    inventory = obs.collect().require_complete()
    assert {unit.declaration.scope for unit in inventory.units} == {"host", "fleet", "bot"}
    assert inventory.foreign == (str(foreign),)
    worker = next(unit for unit in inventory.units if unit.target == bot.name)
    assert worker.installed[0].link == str(obs.declarations[-1].source)
    assert worker.installed[0].content == worker.generated.content
    assert inventory.units[0].installed[0].mode == 0o640
    assert inventory.digest == obs.collect().digest
    assert inventory.payload()["units"][0]["generated"]["content"]["base64"]
    recovery = replace(inventory, units=inventory.units[:1])
    assert {unit.target for unit in inventory.candidate_only(recovery)} == {
        "alpha.watch.service", "alpha.watch.timer", "alpha.worker.service"}
    assert foreign.read_bytes()  # inventory never writes or removes
    bot.unlink()
    bot.write_text("changed")
    with pytest.raises(InventoryError, match="changed"):
        inventory.check_files()


@pytest.mark.parametrize("fault, expected", [
    ("torn", "installed bytes differ"), ("reload", "stale"),
    ("dropin", "overridden"), ("release", "release identity differs"),
    ("missing", "lacks installed source"), ("extra", "absent from generated manifest"),
    ("timer", "different service"), ("empty", "empty generated manifest"),
    ("unbound", "absent from generated manifest"), ("query", "missing/unknown native unit properties"),
])
def test_incomplete_or_foreign_evidence_never_becomes_cleanup_authority(tmp_path, fault, expected):
    obs = Observations(tmp_path)
    unit = obs.add("worker.service", "bot")
    if fault == "torn":
        unit.write_text(f"[Service]\nWorkingDirectory={tmp_path / 'foreign'}\n")
    elif fault == "reload":
        obs.properties[unit.name]["NeedDaemonReload"] = "yes"
    elif fault == "dropin":
        obs.properties[unit.name]["DropInPaths"] = "/override.conf"
    elif fault == "release":
        obs.properties[unit.name]["Environment"] = obs.properties[unit.name]["Environment"].replace("old-artifact", "new-artifact")
    elif fault == "missing":
        unit.unlink()  # still loaded: never read absence as no consumer
    elif fault == "extra":
        obs.add("renamed-old-prefix.service", "bot", declared=False)
    elif fault == "timer":
        timer = obs.add("worker.timer", "bot", service=unit.name)
        obs.properties[timer.name]["Triggers"] = "foreign.service"
    elif fault == "unbound":
        extra = obs.add("unbound.service", working=tmp_path / "unknown", declared=False)
        obs.properties[extra.name]["Environment"] = ""
    elif fault == "query":
        del obs.properties[unit.name]["Id"]
    else:
        obs.declarations.clear()
    with pytest.raises(InventoryError, match=expected):
        obs.collect().require_complete()


@pytest.mark.parametrize("override", ["enabled", "disabled", "unset"])
def test_launchd_proves_reviewed_effective_identity_and_disabled_override(tmp_path, override):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    target = obs.add("alpha.worker.plist", "bot")
    if override != "unset":
        obs.disabled = f'\n\tdisabled services = {{\n\t\t"alpha.worker" => {override}\n\t}}\n'
    inventory = obs.collect().require_complete()
    props = dict(inventory.units[0].properties)
    assert props["ActiveState"] == "active"
    assert props["DisabledOverride"] == override
    assert props["EnabledState"] == (override if override != "unset" else "enabled")
    assert props["UnitFileState"] == "unchanged"  # shared pause token preserves overrides
    assert inventory.units[0].target == "gui/501/alpha.worker"
    assert props["FragmentPath"] == str(target)
    source = plistlib.loads(target.read_bytes())
    obs.launchd[target.name] = observed_print("gui/501/alpha.worker", source, target, active=False)
    obs.launchd_active[target.name] = False
    assert dict(obs.collect().require_complete().units[0].properties)["ActiveState"] == "inactive"
    obs.launchd[target.name] = observed_print("gui/501/alpha.worker", source, target, active=False, calendar=True)
    assert dict(obs.collect().require_complete().units[0].properties)["ActiveState"] == "inactive"
    # A genuinely unloaded installed job has a known override/default without
    # inventing an effective running definition or interpreting print failure.
    obs.launchd.clear()
    props = dict(obs.collect().require_complete().units[0].properties)
    assert props["LoadState"] == "unloaded" and props["ActiveState"] == "inactive"
    if override == "unset":
        source["Disabled"] = True
        target.write_bytes(plistlib.dumps(source))
        obs.declarations[0].source.write_bytes(target.read_bytes())
        assert dict(obs.collect().require_complete().units[0].properties)["EnabledState"] == "disabled"


def test_launchd_preserves_unrelated_plist_without_working_directory(tmp_path):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    obs.add("alpha.worker.plist", "bot")
    other = obs.add("com.fixture.unrelated.plist", working=tmp_path / "other", declared=False)
    source = {"Label": other.stem, "ProgramArguments": ["/usr/bin/true"], "EnvironmentVariables": {}}
    other.write_bytes(plistlib.dumps(source))
    printed = observed_print("gui/501/" + other.stem, {**source, "WorkingDirectory": str(tmp_path / "other")}, other)
    obs.launchd[other.name] = printed.replace(f"\tworking directory = {tmp_path / 'other'}\n", "")
    assert obs.collect().require_complete().foreign == (str(other),)


@pytest.mark.parametrize("field", ["path", "program", "arguments", "environment", "domain", "unknown", "duplicate"])
def test_launchd_loaded_drift_or_unknown_shape_never_matches_installed_plist(tmp_path, field):
    obs = Observations(tmp_path)
    obs.manager = "Darwin"
    target = obs.add("alpha.worker.plist", "bot")
    value = obs.launchd[target.name]
    if field in ("path", "program"):
        value = value.replace(f"\t{field} = ", f"\t{field} = /foreign", 1)
    elif field == "arguments":
        value = value.replace("\targuments = {\n", "\targuments = {\n\t\tunexpected-arg\n")
    elif field == "environment":
        value = value.replace("CLAUDLOBBY_ARTIFACT_ID => old-artifact", "CLAUDLOBBY_ARTIFACT_ID => stale-artifact")
    elif field == "domain":
        value = value.replace("domain = gui/501", "domain = user/501")
    elif field == "unknown":
        value = value.replace("\tprogram = ", "\tnew launch override = unrecognized\n\tprogram = ")
    else:
        value = value.replace("\tstate = running", "\tstate = running\n\tstate = not running")
    obs.launchd[target.name] = value
    with pytest.raises(InventoryError, match="incomplete enrollment"):
        obs.collect().require_complete()


def test_disabled_capture_uses_observed_enums_and_refuses_duplicate_or_unknown_rows():
    captured = (FIXTURES / "launchctl-disabled-26.1.txt").read_text()
    assert set(_darwin_disabled(captured).values()) == {"enabled", "disabled"}
    with pytest.raises(InventoryError, match="unknown"):
        _darwin_disabled(captured.replace("=> enabled", "=> false"))
    with pytest.raises(InventoryError, match="duplicate"):
        _darwin_disabled('\tdisabled services = {\n\t\t"duplicate" => enabled\n\t\t"duplicate" => disabled\n\t}\n')


def test_selected_adapter_catalog_uses_recording_native_functions(tmp_path):
    # Exercise the real shared shell adapter without installing fake executables
    # or giving any code a chance to call the host's supervisor.
    trace = tmp_path / "calls"
    package = source_package()
    prologue = f'''
uname() {{ printf 'Linux\\n'; }}
systemd-analyze() {{ printf '%s\\n' {shlex.quote(str(tmp_path))}; }}
systemctl() {{
    printf '%s\\n' "$*" >> {shlex.quote(str(trace))}
    case "$2" in
        list-unit-files) printf 'worker.service enabled enabled\\n' ;;
        list-units) printf 'worker.service loaded active running Fixture\\n' ;;
        *) return 99 ;;
    esac
}}
'''

    def recording(command, **kwargs):
        command[2] = prologue + command[2]
        return subprocess.run(command, **kwargs)

    adapter = Adapter(package, runner=recording)
    assert adapter.read("svc_inventory_catalog") == (
        f"manager\tLinux\ndirectory\t{tmp_path}\ninstalled\tworker.service\nloaded\tworker.service\n")
    assert trace.read_text().splitlines() == [
        "--user list-unit-files --no-legend --no-pager --plain",
        "--user list-units --all --no-legend --no-pager --plain"]
    with pytest.raises(InventoryError, match="unsupported"):
        adapter.call("arbitrary_shell")


def test_linux_catalog_keeps_escaped_services_but_skips_non_consumers(tmp_path):
    text = (f"manager\tLinux\ndirectory\t{tmp_path}\n"
            "loaded\tdev-disk-by\\x2dpartuuid-02.device\n"
            "loaded\tapp-browser\\x2dworker.scope\n"
            "installed\tdesktop-foo\\x2dbar.service\n"
            "loaded\tdesktop-foo\\x2dbar.service\n")
    manager, _, _, names, loaded = inventory._catalog(text)
    assert manager == "Linux"
    assert names == {r"desktop-foo\x2dbar.service"}
    assert set(loaded) == names
    with pytest.raises(InventoryError, match="unknown adapter catalog row"):
        inventory._catalog(text.replace(r"foo\x2dbar", r"foo\x2gbar"))


def test_linux_native_properties_accepts_only_complete_systemd_escapes():
    prologue = '''
uname() { printf 'Linux\n'; }
systemctl() {
    [ "$2" = show ] || return 99
    printf 'Id=%s\n' "$4"
}
'''

    def recording(command, **kwargs):
        command[2] = prologue + command[2]
        return subprocess.run(command, **kwargs)

    adapter = Adapter(source_package(), runner=recording)
    assert adapter.read("svc_inventory_properties", r"desktop-foo\x2dbar.service") == (
        "Id=desktop-foo\\x2dbar.service\n")
    with pytest.raises(InventoryError, match="failed"):
        adapter.read("svc_inventory_properties", r"desktop-foo\x2gbar.service")


def test_selected_adapter_queries_only_same_uid_launchd_domains(tmp_path):
    trace = tmp_path / "native-reads"
    captured = (FIXTURES / "launchctl-print-26.1-active.txt").read_text()
    disabled = (FIXTURES / "launchctl-disabled-26.1.txt").read_text()
    prologue = f'''
uname() {{ printf 'Darwin\\n'; }}
launchctl() {{
    printf '%s\\n' "$*" >> {shlex.quote(str(trace))}
    case "$1" in
        manageruid) printf '501\\n' ;;
        managername) printf 'Aqua\\n' ;;
        print) printf '%s' "$OBSERVED_PRINT" ;;
        print-disabled) printf '%s' "$OBSERVED_DISABLED" ;;
        *) return 99 ;;
    esac
}}
'''

    def recording(command, **kwargs):
        command[2] = prologue + command[2]
        kwargs["env"].update(OBSERVED_PRINT=captured, OBSERVED_DISABLED=disabled)
        return subprocess.run(command, **kwargs)

    adapter = Adapter(source_package(), runner=recording)
    assert adapter.read("svc_inventory_properties", "gui/501/com.fixture.observed") == captured
    assert adapter.read("svc_inventory_disabled", "gui/501") == disabled
    assert adapter.read("svc_inventory_properties", "user/501/com.fixture.observed") == captured
    assert adapter.call("svc_inventory_properties", "user/502/com.fixture.observed").returncode == 3
    assert adapter.call("svc_inventory_disabled", "user/501").returncode == 3
    assert trace.read_text().splitlines() == [
        "manageruid", "managername", "print gui/501/com.fixture.observed",
        "manageruid", "managername", "print-disabled gui/501", "manageruid", "managername",
        "manageruid", "managername", "print user/501/com.fixture.observed",
        "manageruid", "managername", "manageruid", "managername", "manageruid", "managername"]
