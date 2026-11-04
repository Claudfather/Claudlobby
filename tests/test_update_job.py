"""Fixed updater job definitions and native no-bounce contracts."""
import os
import json
from pathlib import Path
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import update_job as jobs
from claudlobby.git_update import UpdateError
from tests.test_package_resources import _source_installation
from tests.conftest import constructed_env


class Native:
    def __init__(self, installation, directory):
        self.installation, self.directory = installation, directory
        self.calls = []
        self.active = "inactive"
        self.loaded = False
        self.foreign_override = False
        self.fail = False

    def read(self, function, *args):
        if self.fail:
            raise UpdateError("native observation unavailable")
        if function == "svc_inventory_catalog":
            return f"manager\tLinux\ndirectory\t{self.directory}\n"
        if function == "svc_update_job_exec":
            executable = str(self.installation.bin / "claudlobby-update")
            return json.dumps({"type": "a(sasbttttuii)", "data": [[executable, [executable, "_run"],
                              False, 0, 0, 0, 0, 0, 0, 0]]})
        if function == "svc_inventory_state":
            return f"static loaded {self.active}"
        if function == "svc_inventory_properties":
            target = args[0]
            values = {"Id": target, "LoadState": "loaded" if self.loaded else "not-found",
                "ActiveState": self.active, "UnitFileState": "static" if self.loaded else "",
                "FragmentPath": str(self.directory / target) if self.loaded else "",
                "DropInPaths": "/foreign/override.conf" if self.foreign_override else "",
                "NeedDaemonReload": "no", "Triggers": "", "TriggeredBy": "",
                "WorkingDirectory": str(self.installation.directory) if self.loaded else "",
                "Environment": "", "ExecStart": ("{ path=" + str(self.installation.bin / "claudlobby-update")
                    + " ; argv[]=" + str(self.installation.bin / "claudlobby-update") + " _run ; }") if self.loaded else ""}
            return "\n".join(f"{key}={value}" for key, value in values.items())
        raise AssertionError(function)

    def call(self, function, *args, timeout=30):
        self.calls.append(function)
        if function == "svc_update_job_enroll_exact":
            self.loaded = True
            output = "enrolled"
        elif function == "svc_update_job_run_exact":
            self.active = "active"
            output = "run-requested"
        else:
            raise AssertionError(function)
        return SimpleNamespace(returncode=0, stdout=output + "\n", stderr="")


@pytest.fixture
def fixture(tmp_path):
    installation = _source_installation(tmp_path)
    updater = installation.bin / "claudlobby-update"
    updater.write_text("#!/bin/sh\nexit 0\n")
    updater.chmod(0o755)
    directory = tmp_path / "native"
    directory.mkdir()
    return installation, directory, Native(installation, directory)


def test_fixed_definition_uses_only_independent_updater_and_no_autostart(fixture):
    installation, directory, native = fixture
    linux = jobs.update_job_definition(installation, manager="Linux", domain="", install_directory=directory)
    assert b"Type=exec" in linux.content and b"Restart=no" in linux.content
    assert b"[Install]" not in linux.content and str(installation.source).encode() not in linux.content
    darwin = jobs.update_job_definition(installation, manager="Darwin", domain=f"gui/{os.getuid()}", install_directory=directory)
    definition = plistlib.loads(darwin.content)
    assert definition["ProgramArguments"] == [str(installation.bin / "claudlobby-update"), "_run"]
    assert definition["RunAtLoad"] is False and definition["KeepAlive"] is False


def test_prepare_is_exact_and_never_bounces_running_job(fixture):
    installation, directory, native = fixture
    first = jobs.prepare_update_job(installation, native, directory)
    assert first["state"] == "inactive"
    native.active = "active"
    second = jobs.prepare_update_job(installation, native, directory)
    assert second["state"] == "running"
    assert native.calls == ["svc_update_job_enroll_exact"]
    job = jobs._definition(installation, native, directory)
    assert job.source.read_bytes() == job.installed.read_bytes() == job.content


def test_foreign_file_or_native_override_is_preserved(fixture):
    installation, directory, native = fixture
    job = jobs._definition(installation, native, directory)
    job.installed.write_bytes(b"foreign owned content")
    with pytest.raises(UpdateError, match="definition differs"):
        jobs.prepare_update_job(installation, native, directory)
    assert job.installed.read_bytes() == b"foreign owned content"
    assert not native.calls
    job.installed.unlink()
    jobs.prepare_update_job(installation, native, directory)
    native.foreign_override = True
    with pytest.raises(UpdateError, match="effective"):
        jobs.observe_update_job(installation, native, directory)


def test_run_attaches_same_request_and_unknown_state_does_not_launch(fixture, monkeypatch):
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    monkeypatch.setattr(jobs, "read_update", lambda _: {"phase": "quiesce", "finalized": True, "request_id": "original"})
    first = jobs.run_update_job(installation, native, directory)
    assert first["request_id"] == "original" and first["state"] == "running"
    jobs.run_update_job(installation, native, directory)
    assert native.calls.count("svc_update_job_run_exact") == 1
    native.fail = True
    with pytest.raises(UpdateError, match="unavailable"):
        jobs.run_update_job(installation, native, directory)
    assert native.calls.count("svc_update_job_run_exact") == 1


@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
def test_native_run_never_restarts_active_and_uses_only_exact_target(tmp_path, manager):
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    label = "com.claudlobby.update." + "a" * 16
    installed = tmp_path / (label + (".service" if manager == "Linux" else ".plist"))
    installed.write_text("fixed fixture")
    target = installed.name if manager == "Linux" else f"gui/{os.getuid()}/{label}"
    # Shell functions replace every native binary before any adapter verb runs.
    command = '''set -euo pipefail
. "$1"
_OS="$2"; file="$3"; target="$4"; trace="$5"
_svc_inventory_domain() { return 0; }
_svc_activation_read() { SVC_ACT_LOAD=loaded; SVC_ACT_ACTIVE="$active"; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$trace"; }
launchctl() { printf 'launchctl %s\\n' "$*" >> "$trace"; }
active=active
svc_update_job_run_exact "$file" "$target"
[ ! -s "$trace" ]
active=inactive
svc_update_job_run_exact "$file" "$target"
'''
    trace = tmp_path / "trace"
    outcome = subprocess.run(["/bin/bash", "-c", command, "test-update-job", str(script), manager,
                              str(installed), target, str(trace)], env=constructed_env(), capture_output=True, text=True, timeout=10)
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout.splitlines() == ["already-running", "run-requested"]
    actions = trace.read_text()
    assert "restart" not in actions and "bootout" not in actions and "-k " not in actions
    assert ("start --no-block" in actions) if manager == "Linux" else ("kickstart " in actions)


@pytest.mark.parametrize("manager", ["Linux", "Darwin"])
def test_native_enrollment_does_not_execute_job(tmp_path, manager):
    script = Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"
    label = "com.claudlobby.update." + "b" * 16
    installed = tmp_path / (label + (".service" if manager == "Linux" else ".plist"))
    installed.write_text("fixture")
    target = installed.name if manager == "Linux" else f"gui/{os.getuid()}/{label}"
    command = '''set -euo pipefail
. "$1"
_OS="$2"; file="$3"; target="$4"; trace="$5"
_svc_inventory_domain() { return 0; }
loaded=unloaded
_svc_activation_read() { SVC_ACT_LOAD="$loaded"; SVC_ACT_ACTIVE=inactive; }
systemctl() { printf 'systemctl %s\\n' "$*" >> "$trace"; loaded=loaded; }
launchctl() { printf 'launchctl %s\\n' "$*" >> "$trace"; loaded=loaded; }
svc_update_job_enroll_exact "$file" "$target"
'''
    trace = tmp_path / "trace"
    outcome = subprocess.run(["/bin/bash", "-c", command, "test-update-enroll", str(script), manager,
                              str(installed), target, str(trace)], env=constructed_env(),
                             capture_output=True, text=True, timeout=10)
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout.strip() == "enrolled"
    actions = trace.read_text()
    assert " start " not in actions and "kickstart" not in actions and "enable" not in actions and "bootout" not in actions
    assert ("daemon-reload" in actions) if manager == "Linux" else ("bootstrap " in actions)


def test_runner_membership_uses_native_ancestry_and_refuses_external(fixture, monkeypatch):
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    native.active = "active"
    monkeypatch.setattr(native, "call", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    with pytest.raises(UpdateError, match="external"):
        jobs.verify_update_job_runner(installation, native, directory)
    monkeypatch.setattr(native, "call", lambda *_args, **_kwargs: SimpleNamespace(returncode=3))
    with pytest.raises(UpdateError, match="unknown"):
        jobs.verify_update_job_runner(installation, native, directory)
    monkeypatch.setattr(native, "call", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))
    assert jobs.verify_update_job_runner(installation, native, directory)["native_member"]


@pytest.mark.parametrize("defect", ["executable", "extra", "single_argument", "multiple", "ignore", "type", "malformed"])
def test_effective_native_exec_refuses_wrong_or_ambiguous_argv(fixture, monkeypatch, defect):
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    read = native.read
    executable = str(installation.bin / "claudlobby-update")
    command = [executable, [executable, "_run"], False, 0, 0, 0, 0, 0, 0, 0]
    payload = {"type": "a(sasbttttuii)", "data": [command]}
    if defect == "executable":
        command[0] = "/foreign/updater"
    elif defect == "extra":
        command[1].append("--foreign")
    elif defect == "single_argument":
        # This has exactly the same flattened systemctl-show argv text.
        command[1] = [executable + " _run"]
    elif defect == "multiple":
        payload["data"].append(command)
    elif defect == "ignore":
        command[2] = True
    elif defect == "type":
        payload["type"] = "s"
    text = "not JSON" if defect == "malformed" else json.dumps(payload)
    monkeypatch.setattr(native, "read", lambda function, *args:
        text if function == "svc_update_job_exec" else read(function, *args))
    with pytest.raises(UpdateError, match="argv differs"):
        jobs.observe_update_job(installation, native, directory)
    assert native.calls == ["svc_update_job_enroll_exact"]


def test_typed_exec_preserves_spaces_and_quotes_in_executable():
    executable = Path('/installation with spaces/and "quotes"/bin/claudlobby-update')
    value = str(executable)
    payload = {"type": "a(sasbttttuii)", "data": [[value, [value, "_run"], False, 0, 0, 0, 0, 0, 0, 0]]}
    jobs._check_linux_exec(json.dumps(payload), executable)


def test_exact_retained_launchd_target_selects_its_same_uid_domain(fixture, monkeypatch):
    installation, directory, native = fixture
    uid = os.getuid()
    target = f"user/{uid}/" + jobs.update_job_label(installation)
    reads = []
    disabled = False
    def read(function, *args):
        reads.append((function, args))
        if function == "svc_inventory_catalog":
            return f"manager\tDarwin\ndomain\tgui/{uid}\ndirectory\t{directory}\nPID\tStatus\tLabel\n"
        assert function == "svc_inventory_disabled" and args == (f"user/{uid}",)
        row = f'\t\t"{jobs.update_job_label(installation)}" => disabled\n' if disabled else ""
        return "\tdisabled services = {\n" + row + "\t}\n"
    monkeypatch.setattr(native, "read", read)
    job = jobs._definition(installation, native, directory, native_target=target)
    assert job.target == target
    assert job.installed.name == jobs.update_job_label(installation) + ".plist"
    assert reads[-1] == ("svc_inventory_disabled", (f"user/{uid}",))
    disabled = True
    with pytest.raises(UpdateError, match="explicit disabled"):
        jobs._definition(installation, native, directory, native_target=target)
    assert native.calls == []


@pytest.mark.parametrize("defect", ["uid", "label", "manager"])
def test_exact_updater_target_cannot_change_user_installation_or_manager(fixture, monkeypatch, defect):
    installation, directory, native = fixture
    label = jobs.update_job_label(installation)
    target = f"user/{os.getuid()}/{label}"
    manager = "Darwin"
    if defect == "uid":
        target = f"user/{os.getuid() + 1}/{label}"
    elif defect == "label":
        target += "-foreign"
    else:
        manager = "Linux"
    with pytest.raises(UpdateError, match="exact native target"):
        jobs.update_job_definition(installation, manager=manager, domain=f"gui/{os.getuid()}",
                                   install_directory=directory, native_target=target)
    assert native.calls == []


@pytest.mark.parametrize("name", ["managed", 'managed with spaces "quotes" %n \\ $literal', "managed ", "managed\\"])
def test_systemd_working_directory_is_a_literal_native_path(fixture, name):
    """Pi systemd252 rejected quoted WorkingDirectory as a non-absolute path.

    This field is one native path, not an ExecStart argument or shell word.
    Native unit-file whitespace/continuation handling precedes specifier expansion.
    """
    from claudlobby.installation import Installation
    original, directory, _ = fixture
    moved = original.directory.with_name(name)
    original.directory.rename(moved)
    installation = Installation(moved, original.data_root)
    job = jobs.update_job_definition(installation, manager="Linux", domain="", install_directory=directory)
    assignments = [line.partition("=")[2] for line in job.content.decode().splitlines()
                   if line.startswith("WorkingDirectory=")]
    assert len(assignments) == 1
    native = assignments[0].strip()
    assert native.startswith("/") and not native.endswith("\\")
    assert "%" not in native.replace("%%", ""), "literal percent must not become a native specifier"
    assert Path(native.replace("%%", "%")).resolve() == installation.directory
    assert job.content.decode().splitlines().count("[Service]") == 1
