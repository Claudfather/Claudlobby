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


# Each test below removes the one condition a single safety check in update_job.py
# exists for and leaves every other one met, so that check alone refuses. Without the
# check the case goes on to publish, enroll, launch or accept.


def _published(installation, *directories):
    """The updater definitions on disk: the installation's own copy and each native directory's."""
    label = jobs.update_job_label(installation)
    return sorted(str(path) for folder in (installation.bin, *directories) for path in folder.glob(label + ".*"))


def test_an_installation_that_differs_from_its_record_is_refused(fixture, tmp_path):
    """installation.json names the data root; a caller that names another one is refused, not re-pointed."""
    from claudlobby.installation import Installation
    installation, directory, native = fixture
    other = Installation(installation.directory, tmp_path / "other-data")
    with pytest.raises(UpdateError, match="installation record differs"):
        jobs.prepare_update_job(other, native, directory)
    assert native.calls == [] and _published(installation, directory) == []


def test_a_native_directory_another_user_owns_receives_no_definition(fixture, monkeypatch):
    """Whoever owns the directory can replace a definition in it, so the job is never published there."""
    installation, directory, native = fixture
    stat = Path.stat
    def foreign(path, *args, **kwargs):
        info = stat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1) if path == directory else info
    monkeypatch.setattr(Path, "stat", foreign)
    with pytest.raises(UpdateError, match="not owned by the current user"):
        jobs.prepare_update_job(installation, native, directory)
    assert native.calls == [] and _published(installation, directory) == []


def test_an_updater_that_cannot_execute_is_never_enrolled(fixture):
    """A job whose ExecStart cannot run would be enrolled only to fail at every start."""
    installation, directory, native = fixture
    (installation.bin / "claudlobby-update").chmod(0o644)
    with pytest.raises(UpdateError, match="independent installed updater is unavailable"):
        jobs.prepare_update_job(installation, native, directory)
    assert native.calls == [] and _published(installation, directory) == []


@pytest.mark.parametrize("separator", ["\n", "\r"], ids=["newline", "carriage_return"])
def test_a_line_break_in_the_installation_path_never_reaches_a_unit_file(fixture, separator):
    """The path is written into WorkingDirectory= and ExecStart=. A newline there would end the line,
    and the rest of this name would be read as a directive of its own: Environment=INJECTED=1."""
    from claudlobby.installation import Installation
    original, directory, _ = fixture
    moved = original.directory.with_name("managed" + separator + "Environment=INJECTED=1")
    original.directory.rename(moved)
    installation = Installation(moved, original.data_root)
    native = Native(installation, directory)
    with pytest.raises(UpdateError, match="invalid native characters"):
        jobs.prepare_update_job(installation, native, directory)
    assert native.calls == [] and _published(installation, directory) == []


def test_launchd_enrollment_refuses_another_users_domain(fixture, monkeypatch):
    """A catalog read in another user's launchd domain cannot place this user's updater there."""
    installation, directory, native = fixture
    reads = []
    def read(function, *args):
        reads.append(function)
        if function == "svc_inventory_catalog":
            return f"manager\tDarwin\ndomain\tgui/{os.getuid() + 1}\ndirectory\t{directory}\nPID\tStatus\tLabel\n"
        return "\tdisabled services = {\n\t}\n"
    monkeypatch.setattr(native, "read", read)
    with pytest.raises(UpdateError, match="launchd domain differs from current user"):
        jobs._definition(installation, native, directory)
    assert reads == ["svc_inventory_catalog"] and native.calls == []


def test_a_redirected_native_directory_receives_no_definition(fixture, tmp_path):
    """A directory reached through a symlink can be re-pointed later, so nothing is published through one."""
    installation, directory, native = fixture
    link = tmp_path / "native-link"
    link.symlink_to(directory, target_is_directory=True)
    native.directory = link  # the manager's search path names the symlink
    with pytest.raises(UpdateError, match="absolute and unredirected"):
        jobs.prepare_update_job(installation, native, link)
    assert native.calls == [] and _published(installation, directory) == []


@pytest.mark.parametrize("shadow", ["file", "dangling_symlink"])
def test_another_definition_in_the_search_path_stops_enrollment(fixture, tmp_path, monkeypatch, shadow):
    """The manager loads one definition per name from its search path, and another copy could be that one."""
    installation, directory, native = fixture
    other = tmp_path / "other-native"
    other.mkdir()
    duplicate = other / (jobs.update_job_label(installation) + ".service")
    if shadow == "file":
        duplicate.write_text("[Service]\nExecStart=/bin/true\n")
    else:
        duplicate.symlink_to(tmp_path / "absent")
    read = native.read
    monkeypatch.setattr(native, "read", lambda function, *args: (
        f"manager\tLinux\ndirectory\t{other}\ndirectory\t{directory}\n"
        if function == "svc_inventory_catalog" else read(function, *args)))
    with pytest.raises(UpdateError, match="shadows the updater job"):
        jobs.prepare_update_job(installation, native, directory)
    assert native.calls == [] and _published(installation, directory) == []


def test_a_directory_outside_the_observed_search_path_receives_no_definition(fixture, tmp_path):
    """The manager never loads a definition from outside its search path, so none is published there."""
    installation, directory, native = fixture
    unread = tmp_path / "unread"
    unread.mkdir()
    with pytest.raises(UpdateError, match="absent from the observed search path"):
        jobs.prepare_update_job(installation, native, unread)
    assert native.calls == [] and _published(installation, directory, unread) == []


def test_a_definition_with_another_mode_is_never_launched(fixture, monkeypatch):
    """A definition others can write is no longer the fixed job, whatever its bytes say now."""
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    jobs._definition(installation, native, directory).installed.chmod(0o666)
    monkeypatch.setattr(jobs, "read_update", lambda _: {"phase": "quiesce", "finalized": True, "request_id": "original"})
    with pytest.raises(UpdateError, match="definition differs from the fixed installation job"):
        jobs.run_update_job(installation, native, directory)
    assert native.calls == ["svc_update_job_enroll_exact"]


def test_a_symlinked_definition_is_never_launched(fixture, tmp_path, monkeypatch):
    """A symlink can be re-pointed after any check, so one even to identical bytes is refused."""
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    job = jobs._definition(installation, native, directory)
    copy = tmp_path / "elsewhere" / job.installed.name
    copy.parent.mkdir()
    copy.write_bytes(job.content)
    copy.chmod(0o644)
    job.installed.unlink()
    job.installed.symlink_to(copy)
    monkeypatch.setattr(jobs, "read_update", lambda _: {"phase": "quiesce", "finalized": True, "request_id": "original"})
    with pytest.raises(UpdateError, match="definition differs from the fixed installation job"):
        jobs.run_update_job(installation, native, directory)
    assert native.calls == ["svc_update_job_enroll_exact"]


def test_run_refuses_an_operation_still_in_preparation(fixture, monkeypatch):
    """finalize_prepare saves phase prepare with finalized true, and run reads the record without the
    update lock, so it can meet that state. The job never starts for an operation still being prepared."""
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    monkeypatch.setattr(jobs, "read_update", lambda _: {"phase": "prepare", "finalized": True, "request_id": "original"})
    with pytest.raises(UpdateError, match="no prepared update requires the independent job"):
        jobs.run_update_job(installation, native, directory)
    assert native.calls == ["svc_update_job_enroll_exact"]


def test_the_runner_reobserves_its_own_definition_before_it_acts(fixture, monkeypatch):
    """The caller is inside the running job, but a drop-in changed what that job runs: the runner refuses
    before it asks about membership. Without the drop-in, the same runner is accepted."""
    installation, directory, native = fixture
    jobs.prepare_update_job(installation, native, directory)
    native.active = "active"
    native.foreign_override = True
    asked = []
    def call(function, *_args, **_kwargs):
        asked.append(function)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(native, "call", call)
    with pytest.raises(UpdateError, match="effective native definition differs"):
        jobs.verify_update_job_runner(installation, native, directory)
    assert asked == []
    native.foreign_override = False
    assert jobs.verify_update_job_runner(installation, native, directory)["native_member"]
    assert asked == ["svc_activation_assert_external"]
