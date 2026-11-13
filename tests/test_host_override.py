"""THIS host's override of host.jobs: ~/.config/claudlobby/system.yaml (#1251).

Host-local config lives outside the tracked tree, so a root pull can refuse any
dirty tree. Each test is named for the failure it guards against.
"""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from claudlobby import composer as composer_mod
from claudlobby.config import _load_system_defaults, host_unit_name, load_host_jobs
from claudlobby import switches as sw
from tests.package_fixtures import source_package, git_package
from claudlobby.paths import Paths
from claudlobby.__main__ import main
from claudlobby import host_job_operations as host_run

PAUSE = "host: { jobs: { claude-update: { enroll: false } } }\n"


@pytest.fixture
def override(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "host-override.yaml"
    monkeypatch.setenv("CLAUDLOBBY_HOST_SYSTEM_YAML", str(path))
    return path


def _units(root: Path, job: str) -> list[str]:
    root.mkdir()
    out = composer_mod.compose_host_timers(Paths(root=root, package=git_package(root)))
    return sorted(p.name for p in out.iterdir() if p.name.startswith(f"claudlobby-{job}."))


def test_the_claude_update_pause_needs_no_edit_to_the_tracked_system_yaml(tmp_path, override):
    # The pause lived in a tracked edit to claudlobby/system.yaml, which the
    # 2026-09-26 pull had to carry with --autostash. Here the packaged file
    # stays as shipped, and the override alone stops the unit being composed.
    assert _load_system_defaults()["host"]["jobs"]["claude-update"].get("enroll", True) is not False
    assert _units(tmp_path / "shipped", "claude-update"), "precondition: shipped enrolled"
    override.write_text(PAUSE)
    assert load_host_jobs()["claude-update"]["enroll"] is False
    assert _units(tmp_path / "paused", "claude-update") == []


def test_two_host_roots_compose_distinct_native_labels_and_keep_private_override(tmp_path, override):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    override.write_text("host: { unit_prefix: claudlobby-canary, jobs: { plane-daemon: { enroll: true } } }\n")
    generated_a = composer_mod.compose_host_timers(Paths(root=root_a, package=git_package(root_a)))
    names_a = {p.name for p in generated_a.iterdir()}
    assert "claudlobby-canary-plane-daemon.plist" in names_a
    assert "claudlobby-plane-daemon.plist" not in names_a
    import plistlib
    daemon = plistlib.loads((generated_a / "claudlobby-canary-plane-daemon.plist").read_bytes())
    assert daemon["Label"] == "claudlobby-canary-plane-daemon"
    assert daemon["EnvironmentVariables"]["CLAUDLOBBY_HOST_SYSTEM_YAML"] == str(override)
    assert host_unit_name("plane-prune") == "claudlobby-canary-plane-prune"

    override.write_text("host: { jobs: { plane-daemon: { enroll: true } } }\n")
    generated_b = composer_mod.compose_host_timers(Paths(root=root_b, package=git_package(root_b)))
    names_b = {p.name for p in generated_b.iterdir()}
    assert "claudlobby-plane-daemon.plist" in names_b
    assert names_a.isdisjoint(names_b)


@pytest.mark.parametrize("prefix", ["", "two words", "../bad", "bad/name", "1bad", "bad\tname", "x" * 49])
def test_invalid_host_unit_prefix_is_refused(override, prefix):
    override.write_text("host: { unit_prefix: " + json.dumps(prefix) + " }\n")
    with pytest.raises(RuntimeError, match="host.unit_prefix"):
        load_host_jobs()


def test_arming_one_job_keeps_every_other_job_and_field_as_packaged(override):
    # A wholesale replacement of host.jobs would keep only the job the override
    # names, silently dropping every other job's schedule and enroll state.
    packaged = copy.deepcopy(_load_system_defaults()["host"]["jobs"])
    override.write_text("host: { jobs: { update-siblings: { enroll: true } } }\n")
    jobs = load_host_jobs()
    assert set(jobs) == set(packaged)
    for name, cfg in packaged.items():
        if name != "update-siblings":
            assert jobs[name] == cfg, name
    assert jobs["update-siblings"] == {**packaged["update-siblings"], "enroll": True}
    # ...and the cached packaged tier is not rewritten by the merge.
    assert _load_system_defaults()["host"]["jobs"] == packaged


def test_owner_control_socket_is_an_explicit_host_override_field(override):
    override.write_text("host: { jobs: { plane-owner: { tailscale_socket: /var/run/tailscaled.socket } } }\n")
    assert load_host_jobs()["plane-owner"]["tailscale_socket"] == "/var/run/tailscaled.socket"


@pytest.mark.parametrize("text", [
    'host: { jobs: { claude-update: { enroll: "false" } } }\n',  # the composer enrolls on a string
    "host: { jobs: { claude-update: { enrol: false } } }\n",     # a field nothing reads
], ids=["quoted-enroll", "misspelt-field"])
def test_a_pause_that_cannot_take_effect_is_refused_not_skipped(override, text):
    override.write_text(text)
    with pytest.raises(RuntimeError, match="claude-update"):
        load_host_jobs()


def test_a_job_this_install_does_not_ship_is_logged_not_fatal(override, caplog):
    # The file outlives the install it was written against: a pull that retires
    # a job must not stop every host-timers run on the host.
    override.write_text("host: { jobs: { claude-updte: { enroll: false } } }\n")
    assert "claude-updte" not in load_host_jobs()
    assert "did you mean claude-update" in caplog.text


def test_a_malformed_override_renders_unknown_never_the_shipped_default(tmp_path, override):
    # resolve() once caught the refusal and substituted {}, so doctor --switches
    # showed every host job at its SHIPPED default at rc 0 -- for a paused job,
    # the exact opposite of the host's intended state.
    override.write_text('host: { jobs: { claude-update: { enroll: "false" } } }\n')
    rows = sw.resolve(Paths(root=tmp_path, package=source_package()), cascade={})
    host_rows = [r for r in rows if r.switch.scope in (sw.HOST_JOB, sw.HOST_SERVICE)
                 and "extra is not installed" not in r.source]   # that row is its own fact
    assert host_rows
    for r in host_rows:
        assert r.unknown and r.unknown_reason == "host-override", r.switch.key
        assert str(override) in r.detail and "must be true or false" in r.detail
        assert r.label == "unknown"
    assert "host jobs unreadable" in sw.summary_line(rows)


def test_host_job_reads_effective_host_override_without_fleet_merge(tmp_path, override, capsys):
    override.write_text("host: { jobs: { claude-update: { enroll: false } } }\n")
    assert main(["--root", str(tmp_path), "--fleet", "absent", "host", "job", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["command"] == "host.job.list" and listing["ok"]
    assert {item["name"]: item["enroll"] for item in listing["data"]["jobs"]}["claude-update"] is False
    assert listing["data"]["source"] == "effective_host_configuration"
    assert listing["data"]["native_state"] == "not_checked"

    assert main(["--root", str(tmp_path), "host", "job", "list"]) == 0
    plain = capsys.readouterr().out
    assert "claude-update\tenroll=false" in plain
    assert "host.jobs.claude-update.enroll" in plain and "host doctor --switches" in plain
    assert "installed/running state was not checked" in plain

    assert main(["--root", str(tmp_path), "host", "job", "show", "claude-update", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["data"]["job"] == load_host_jobs()["claude-update"]
    assert "host update apply" in shown["data"]["enrollment_hint"]

    assert main(["--root", str(tmp_path), "host", "job", "show", "claude-update"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0]) == shown["data"]["job"]
    assert "host.jobs.claude-update.enroll" in " ".join(lines[1:])
    assert "installed/running state was not checked" in " ".join(lines[1:])


@pytest.mark.parametrize("platform,domain_kind", [("Linux", ""), ("Darwin", "user"), ("Darwin", "gui")])
def test_git_host_job_uses_committed_family_and_exact_installed_service(tmp_path, monkeypatch, platform, domain_kind):
    import os
    from dataclasses import replace
    import plistlib
    from claudlobby.config_units import unit_family
    from claudlobby.installation import Installation
    from claudlobby.runtime_admission import GitCurrentRuntime, wrap_unit_argv, unit_systemd_command, unit_systemd_environment
    from claudlobby.supervision_inventory import FileSnapshot

    domain = f"{domain_kind}/{os.getuid()}" if domain_kind else ""
    root, directory = tmp_path / "data", tmp_path / "units"
    root.mkdir(); (root / "state").mkdir(); directory.mkdir()
    installation = Installation(tmp_path / "installation", root)
    package = replace(source_package(), installation=installation, native=installation.native,
                      artifact_id="", source_revision="a" * 40)
    env = {"CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_INSTALLATION": str(installation.directory),
           "CLAUDLOBBY_CLI": str(installation.cli), "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
           "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
    stem = "committed-plane-prune"
    argv = wrap_unit_argv(env, unit=stem, phase="producers", mode="oneshot",
                         argv=[str(installation.native / "plane-prune.sh")])
    files = {stem + ".plist": (plistlib.dumps({"Label": stem, "WorkingDirectory": str(root),
        "EnvironmentVariables": env, "ProgramArguments": list(argv)}), 0o644),
        stem + ".service": ((f"[Service]\nWorkingDirectory={root}\nExecStart={unit_systemd_command(argv)}\n"
            + "".join(host_run_line + "\n" for host_run_line in
                      [unit_systemd_environment(k, v)
                       for k,v in env.items()])).encode(), 0o644),
        stem + ".timer": (f"[Timer]\nUnit={stem}.service\n".encode(), 0o644)}
    generated = root / "runtime/_host/timers"
    generated.mkdir(parents=True)
    rows = unit_family(files, destination=generated, scope="host", phase="producers")
    for name, (content, mode) in files.items():
        (generated / name).write_bytes(content); (generated / name).chmod(mode)
        (directory / name).write_bytes(content); (directory / name).chmod(mode)
    current = GitCurrentRuntime(installation, "a" * 40, "b" * 64,
                               {"installation": str(installation.directory), "data_root": str(root), "units": rows,
                                "supervision": {"manager": platform, "domain": domain, "directory": str(directory)}})
    @contextmanager
    def admitted(*args, **kwargs):
        yield current
    monkeypatch.setattr(host_run, "mutation_admission", admitted)
    monkeypatch.setattr(host_run.RuntimeIdentity, "current", lambda: object())
    monkeypatch.setattr(host_run, "_load_system_defaults", lambda: {"host": {"jobs": {"plane-prune": {}}}})
    calls = []
    class Native:
        response = subprocess.CompletedProcess([], 0, "invoking\nrun-requested\n", "")
        def __init__(self): self.package = package
        def read(self, function):
            assert function == "svc_inventory_catalog"
            return (f"manager\t{platform}\ndomain\t{domain}\ndirectory\t{directory}\n"
                    + ("PID\tStatus\tLabel\n" if platform == "Darwin" else ""))
        def call(self, function, *args, **kwargs):
            calls.append((function, args))
            return self.response
    def inventory(root, declarations, **kwargs):
        assert kwargs["only_names"] == frozenset({stem + ".plist"} if platform == "Darwin" else {stem + ".service", stem + ".timer"})
        units = [SimpleNamespace(declaration=d, target=domain + "/" + d.source.stem if platform == "Darwin" else d.source.name,
            installed=(FileSnapshot.read(directory / d.source.name),), properties=(("LoadState", "loaded"),))
            for d in declarations]
        return SimpleNamespace(require_complete=lambda: SimpleNamespace(units=units))
    monkeypatch.setattr(host_run, "collect_enrollment", inventory)
    result = host_run.run_host_job(root, "plane-prune", adapter=Native())
    assert result.release_id is None and result.revision == current.revision
    assert result.config_fingerprint == current.config_fingerprint
    suffix = ".plist" if platform == "Darwin" else ".service"
    target = domain + "/" + stem if platform == "Darwin" else stem + suffix
    assert calls == [("svc_host_job_run_exact", (str(directory / (stem + suffix)), target))]
    # A timed-out/failed request may already have invoked the oneshot. Report
    # uncertainty; no automatic retry or timer manipulation follows it.
    uncertain_adapter = Native()
    uncertain_adapter.response = subprocess.CompletedProcess([], 1, "invoking\n", "failed")
    with pytest.raises(host_run.HostJobError) as uncertain:
        host_run.run_host_job(root, "plane-prune", adapter=uncertain_adapter)
    assert uncertain.value.effect_attempted and uncertain.value.unavailable
    assert calls == [("svc_host_job_run_exact", (str(directory / (stem + suffix)), target))] * 2
    calls.clear()
    with pytest.raises(host_run.HostJobError, match="not packaged"):
        host_run.run_host_job(root, "prune", adapter=Native())
    if platform == "Darwin" and domain_kind == "user":
        current.snapshot["supervision"]["domain"] = f"gui/{os.getuid()}"
        with pytest.raises(host_run.HostJobError, match="GUI.*unavailable"):
            host_run.run_host_job(root, "plane-prune", adapter=Native())
        current.snapshot["supervision"]["domain"] = domain
    original_inventory = inventory
    def foreign_inventory(*args, **kwargs):
        observed = original_inventory(*args, **kwargs).require_complete()
        observed.units[0].installed = (SimpleNamespace(path=str(tmp_path / "foreign" / "unit")),)
        return SimpleNamespace(require_complete=lambda: observed)
    monkeypatch.setattr(host_run, "collect_enrollment", foreign_inventory)
    with pytest.raises(host_run.HostJobError, match="placement"):
        host_run.run_host_job(root, "plane-prune", adapter=Native())
    assert calls == []
    monkeypatch.setattr(host_run, "collect_enrollment", original_inventory)
    for row in rows:
        row["enroll"] = False
    with pytest.raises(host_run.HostJobError, match="disabled"):
        host_run.run_host_job(root, "plane-prune", adapter=Native())
    assert calls == []


@pytest.mark.parametrize("platform,suffix,target,invocation", [
    ("Linux", ".service", "claudlobby-fixture.service", "--user start claudlobby-fixture.service"),
    ("Darwin", ".plist", "gui/501/claudlobby-fixture", "kickstart gui/501/claudlobby-fixture"),
])
def test_native_host_job_run_requests_loaded_inactive_unit_only(tmp_path, platform, suffix, target, invocation):
    unit = tmp_path / ("claudlobby-fixture" + suffix)
    unit.write_text("private unit")
    calls = tmp_path / "native-calls"
    script = '''
        . "$1"
        _OS="$PLATFORM"
        _svc_activation_read() { SVC_ACT_LOAD=loaded; SVC_ACT_ACTIVE="$JOB_STATE"; }
        systemctl() { printf '%s\\n' "$*" >> "$CALLS"; }
        launchctl() { printf '%s\\n' "$*" >> "$CALLS"; }
        svc_host_job_run_exact "$2" "$3"
    '''
    import os
    for state, expected_rc in (("inactive", 0), ("active", 3)):
        result = subprocess.run(["/bin/bash", "-c", script, "job-test",
                                 str(Path(__file__).resolve().parents[1] / "claudlobby/_runtime_scripts/supervisor.sh"),
                                 str(unit), target],
                                env={**os.environ, "JOB_STATE": state, "CALLS": str(calls),
                                     "PLATFORM": platform},
                                text=True, capture_output=True)
        assert result.returncode == expected_rc
        if state == "inactive":
            assert result.stdout == "invoking\nrun-requested\n"
            assert calls.read_text() == invocation + "\n"
        else:
            assert result.stdout == ""
            assert calls.read_text() == invocation + "\n"


def test_host_job_run_refuses_generated_caller_and_retired_pull_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOT_ID", "worker")
    assert main(["--root", str(tmp_path), "host", "job", "run", "plane-prune", "--json"]) == 4
    denied = json.loads(capsys.readouterr().out)
    assert denied["command"] == "host.job.run" and denied["error"]["code"] == "conflict"
    monkeypatch.delenv("BOT_ID")
    assert main(["--root", str(tmp_path), "host", "job", "run", "pull-root", "--json"]) == 4
    retired = json.loads(capsys.readouterr().out)
    assert "retired" in retired["error"]["message"]
    assert retired["data"]["native_outcome"] == "unattempted"


def _stop_family(tmp_path, monkeypatch, *, manager="Linux", name="log-rotation", abandon=False, activity="inactive"):
    """An existing committed native family; no native process is started."""
    import os
    import plistlib
    from dataclasses import replace
    from claudlobby import job_operations as jobs
    from claudlobby.config_units import unit_family
    from claudlobby.installation import Installation
    from claudlobby.runtime_admission import GitCurrentRuntime, wrap_unit_argv, unit_systemd_command, unit_systemd_environment
    from claudlobby.supervision_inventory import FileSnapshot
    root, directory = tmp_path / "data", tmp_path / "units"
    root.mkdir(); (root / "state").mkdir(); directory.mkdir()
    installation = Installation(tmp_path / "installation", root)
    package = replace(source_package(), installation=installation, native=installation.native,
                      artifact_id="", source_revision="a" * 40)
    fleet = None if name == "plane-daemon" else "sample"
    stem = "private-" + name if fleet is None else "com.private." + name
    env = {"CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_INSTALLATION": str(installation.directory),
           "CLAUDLOBBY_CLI": str(installation.cli), "CLAUDLOBBY_NATIVE_DIR": str(installation.native),
           "CLAUDLOBBY_LIBRARY_DIR": str(installation.source / "library")}
    command = ([str(installation.cli), "--root", str(root), "plane", "serve"] if fleet is None
               else [str(installation.native / {"log-rotation": "log-rotate-fleet.sh", "keepalive": "keepalive-all.sh"}[name]), fleet])
    argv = wrap_unit_argv(env, unit=stem, phase="ingest" if fleet is None else "producers",
                         mode="exec" if fleet is None else "oneshot", argv=command)
    definition = {"Label": stem, "WorkingDirectory": str(root), "EnvironmentVariables": env,
                  "ProgramArguments": list(argv), "AbandonProcessGroup": abandon}
    if fleet:
        definition["StartInterval"] = 60
    service = (f"[Service]\nWorkingDirectory={root}\nExecStart={unit_systemd_command(argv)}\n"
               + "".join(unit_systemd_environment(k, v) + "\n" for k, v in env.items())
               + ("KillMode=process\n" if abandon else ""))
    files = {stem + ".plist": (plistlib.dumps(definition), 0o644), stem + ".service": (service.encode(), 0o644)}
    if fleet:
        files[stem + ".timer"] = (f"[Timer]\nUnit={stem}.service\n".encode(), 0o644)
    generated = root / "generated"; generated.mkdir()
    rows = unit_family(files, destination=generated, scope="host" if fleet is None else "fleet",
                       phase="ingest" if fleet is None else "producers", fleet=fleet)
    for filename, (content, mode) in files.items():
        for folder in (generated, directory):
            (folder / filename).write_bytes(content); (folder / filename).chmod(mode)
    domain = f"gui/{os.getuid()}" if manager == "Darwin" else ""
    current = GitCurrentRuntime(installation, "a" * 40, "b" * 64, {
        "installation": str(installation.directory), "data_root": str(root), "units": rows,
        "supervision": {"manager": manager, "domain": domain, "directory": str(directory)}})
    calls = []
    class Native:
        package = None
        failure = None
        def read(self, function, *args):
            if function == "svc_inventory_catalog":
                return (f"manager\t{manager}\ndomain\t{domain}\ndirectory\t{directory}\n"
                        + ("PID\tStatus\tLabel\n" if manager == "Darwin" else ""))
            assert function == "svc_job_witness"
            return "pid\t0\ngroup\t\n"
        def call(self, function, *args, **kwargs):
            calls.append((function, args))
            if self.failure:
                self.failure(function, args)
            return subprocess.CompletedProcess([], 0, "reloaded\n" if function == "svc_job_reload"
                                               else "effect-attempted\nstopped\n", "")
    native = Native(); native.package = package
    def inventory(root, declarations, **kwargs):
        units = [SimpleNamespace(declaration=d, target=(domain + "/" + d.source.stem if domain else d.source.name),
                    installed=((FileSnapshot.read(directory / d.source.name),) if (directory / d.source.name).exists() else ()),
                    properties=(("LoadState", "loaded"), ("ActiveState", activity))) for d in declarations]
        return SimpleNamespace(require_complete=lambda: SimpleNamespace(units=units))
    monkeypatch.setattr(host_run, "collect_enrollment", inventory)
    monkeypatch.setattr(jobs, "assert_quiescent", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "assert_native_absent", lambda *a, **k: None)
    return jobs, current, native, fleet, directory, calls


def test_job_stop_removes_whole_family_timer_first_and_keeps_declarations(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    result = jobs._stop(current, "log-rotation", fleet, native)
    assert [row["outcome"] for row in result.members] == ["absent", "absent"]
    stops = [args[2] for function, args in calls if function == "svc_job_stop_exact"]
    assert stops == ["com.private.log-rotation.timer", "com.private.log-rotation.service"]
    assert not list(directory.glob("*.timer")) and not list(directory.glob("*.service"))
    assert (current.installation.data_root / "generated/com.private.log-rotation.service").exists()
    assert result.configuration_changed is False


@pytest.mark.parametrize("problem", ["abandon", "partial", "foreign-link", "dormant"])
def test_job_stop_refuses_entire_family_before_any_effect(tmp_path, monkeypatch, problem):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch, name="keepalive", abandon=problem == "abandon")
    if problem == "partial":
        (directory / "com.private.keepalive.service").unlink()
    if problem == "foreign-link":
        wants = directory / "timers.target.wants"; wants.mkdir()
        (wants / "com.private.keepalive.timer").symlink_to(tmp_path / "foreign")
    if problem == "dormant":
        for row in current.snapshot["units"]:
            row["enroll"] = False
    with pytest.raises((jobs.JobStopError, host_run.HostJobError)):
        jobs._stop(current, "keepalive", fleet, native)
    assert calls == []
    assert (directory / "com.private.keepalive.timer").exists()


def test_job_stop_preserves_unknown_partial_effect_and_changed_node(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    def failure(function, args):
        if function == "svc_job_stop_exact" and args[2].endswith(".service"):
            raise subprocess.TimeoutExpired("owned unit", 1)
    native.failure = failure
    with pytest.raises(jobs.JobStopError) as error:
        jobs._stop(current, "log-rotation", fleet, native)
    assert error.value.attempted
    assert [row["outcome"] for row in error.value.data["members"]] == ["absent", "unknown"]
    assert (directory / "com.private.log-rotation.service").exists()


@pytest.mark.parametrize("replacement", ["unlink-recreate", "rename"])
def test_job_stop_does_not_unlink_definition_replaced_during_native_stop(tmp_path, monkeypatch, replacement):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    def replace_node(function, args):
        if function == "svc_job_stop_exact":
            file = Path(args[1]); content = file.read_bytes()
            if replacement == "rename":
                other = file.with_suffix(".replacement")
                other.write_bytes(content); other.replace(file)
            else:
                file.unlink(); file.write_bytes(content)
    native.failure = replace_node
    with pytest.raises(jobs.JobStopError, match="changed"):
        jobs._stop(current, "log-rotation", fleet, native)
    assert (directory / "com.private.log-rotation.timer").exists()
    assert len(calls) == 1


def test_job_run_and_stop_share_fail_fast_effect_lock(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    with host_run.job_effect_lock(current.installation.data_root):
        with pytest.raises(host_run.HostJobError, match="another job effect"):
            with host_run.job_effect_lock(current.installation.data_root):
                pytest.fail("concurrent effect admitted")
    assert calls == []


def test_job_process_witness_refuses_detached_descendant_and_surviving_group(monkeypatch):
    import os
    from claudlobby import job_operations as jobs
    uid = os.getuid()
    monkeypatch.setattr(jobs, "_process_table", lambda: {900: (1, 900, uid, "start"), 901: (900, 901, uid, "child")})
    with pytest.raises(jobs.JobStopError, match="descendant"):
        jobs._capture_group(900)
    monkeypatch.setattr(jobs, "_process_table", lambda: {900: (1, 900, uid, "start"), 901: (900, 900, uid, "child")})
    witness = jobs._capture_group(900)
    monkeypatch.setattr(jobs, "_process_table", lambda: {901: (1, 900, uid, "child")})
    with pytest.raises(jobs.JobStopError, match="remain"):
        jobs._group_quiet(witness)
    monkeypatch.setattr(jobs, "_process_table", lambda: {})
    jobs._group_quiet(witness)


def test_public_job_stop_requires_explicit_fleet_and_operator(tmp_path, monkeypatch, capsys):
    assert main(["--root", str(tmp_path), "host", "supervision", "stop", "--fleet-job", "keepalive", "--json"]) == 2
    missing = json.loads(capsys.readouterr().out)
    assert missing["error"]["code"] == "invalid_argument"
    monkeypatch.setenv("BOT_ID", "worker")
    assert main(["--root", str(tmp_path), "host", "supervision", "stop", "--host-job", "plane-daemon", "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "conflict"


def test_job_stop_refuses_stale_runtime_before_binding(tmp_path, monkeypatch):
    from claudlobby import job_operations as jobs
    from claudlobby.activation_state import ActivationError
    @contextmanager
    def fenced(*a, **k):
        raise ActivationError("runtime maintenance is active")
        yield
    monkeypatch.setattr(jobs, "mutation_admission", fenced)
    monkeypatch.setattr(jobs.RuntimeIdentity, "current", lambda: object())
    monkeypatch.setattr(jobs, "_bind", lambda *a: pytest.fail("fenced mutation reached native ownership"))
    with pytest.raises(ActivationError, match="maintenance"):
        jobs.stop_job(tmp_path, "plane-daemon")


@pytest.mark.parametrize("problem", ["none", "foreign-endpoint", "held-lock", "surviving-group", "native-observation"])
def test_darwin_recorder_stop_binds_pid_endpoint_lock_and_group_before_unlink(tmp_path, monkeypatch, problem):
    import fcntl
    import os
    from claudlobby.plane import daemon
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch, manager="Darwin", name="plane-daemon")
    sock = daemon.socket_path(current.installation.data_root)
    sock.parent.mkdir(parents=True, exist_ok=True)
    lock = daemon.lock_path(sock)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    uid = os.getuid()
    table = {900: (1, 900, uid, "original"), 901: (900, 900, uid, "child")}
    monkeypatch.setattr(jobs, "_process_table", lambda: table.copy())
    info = {"root": str(current.installation.data_root), "pid": 900 if problem != "foreign-endpoint" else 999,
            "installation": str(current.installation.directory), "source_revision": current.revision,
            "cli": str(current.installation.cli)}
    monkeypatch.setattr(daemon, "probe_daemon_info", lambda path: info)
    old_read = native.read
    native.read = lambda function, *args: "pid\t900\ngroup\t\n" if function == "svc_job_witness" else old_read(function, *args)
    endpoint_probes = []
    monkeypatch.setattr(jobs, "assert_private_endpoint_quiet", lambda path, **k: endpoint_probes.append(path))
    def stopped(function, args):
        if function == "svc_job_stop_exact":
            assert args[3] == "900"
            if problem != "held-lock":
                fcntl.flock(fd, fcntl.LOCK_UN)
            if problem != "surviving-group":
                table.clear()
    native.failure = stopped
    if problem == "native-observation":
        original_call = native.call
        def refused_after_bootout(function, *args, **kwargs):
            if function == "svc_job_stop_exact":
                stopped(function, args)
                return subprocess.CompletedProcess([], 3, "effect-attempted\n", "state after bootout\n")
            return original_call(function, *args, **kwargs)
        native.call = refused_after_bootout
    if problem == "surviving-group":
        monkeypatch.setattr(jobs, "_await_group_quiet", jobs._group_quiet)
    try:
        if problem == "none":
            result = jobs._stop(current, "plane-daemon", None, native)
            assert result.members[0]["outcome"] == "absent"
            assert not (directory / "private-plane-daemon.plist").exists()
            assert endpoint_probes == [sock]
        else:
            with pytest.raises(jobs.JobStopError) as error:
                jobs._stop(current, "plane-daemon", None, native)
            assert error.value.attempted is (problem != "foreign-endpoint")
            assert (directory / "private-plane-daemon.plist").exists()
            if problem == "native-observation":
                member, = error.value.data["members"]
                assert member["outcome"] == "unknown" and member["returncode"] == 3
                assert member["stdout"] == "effect-attempted\n"
                assert endpoint_probes == []
            if problem == "foreign-endpoint":
                assert calls == []
    finally:
        os.close(fd)


def test_public_job_run_and_stop_both_refuse_held_job_effect_lock(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    @contextmanager
    def admitted(*args, **kwargs):
        yield current
    for module in (host_run, jobs):
        monkeypatch.setattr(module, "mutation_admission", admitted)
        monkeypatch.setattr(module.RuntimeIdentity, "current", lambda: object())
    monkeypatch.setattr(host_run, "_run_git_job", lambda *a: pytest.fail("concurrent run entered binding"))
    monkeypatch.setattr(jobs, "_stop", lambda *a: pytest.fail("concurrent stop entered binding"))
    with host_run.job_effect_lock(current.installation.data_root):
        with pytest.raises(host_run.HostJobError, match="another job effect"):
            host_run.run_host_job(current.installation.data_root, "plane-prune", adapter=native)
        with pytest.raises(host_run.HostJobError, match="another job effect"):
            jobs.stop_job(current.installation.data_root, "log-rotation", fleet=fleet, adapter=native)
    assert calls == []


@pytest.mark.parametrize("has_witness", [False, True])
def test_running_oneshot_stop_requires_native_pid_and_cgroup_before_effect(tmp_path, monkeypatch, has_witness):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch, activity="activating")
    old_read = native.read
    if has_witness:
        native.read = lambda function, *args: ("pid\t900\ngroup\t/user.slice/private-job\n"
            if function == "svc_job_witness" else old_read(function, *args))
        result = jobs._stop(current, "log-rotation", fleet, native)
        assert all(member["outcome"] == "absent" for member in result.members)
    else:
        with pytest.raises(jobs.JobStopError, match="lacks native process/cgroup") as error:
            jobs._stop(current, "log-rotation", fleet, native)
        assert not error.value.attempted and calls == []
        assert (directory / "com.private.log-rotation.timer").exists()


def test_job_stop_rechecks_enablement_node_after_witness_capture_before_effect(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    wants = directory / "timers.target.wants"; wants.mkdir()
    link = wants / "com.private.log-rotation.timer"
    link.symlink_to(directory / "com.private.log-rotation.timer")
    old_read = native.read
    def changed(function, *args):
        if function == "svc_job_witness" and link.resolve() == directory / "com.private.log-rotation.timer":
            link.unlink(); link.symlink_to(tmp_path / "foreign")
        return old_read(function, *args)
    native.read = changed
    with pytest.raises(jobs.JobStopError, match="enablement node changed") as error:
        jobs._stop(current, "log-rotation", fleet, native)
    assert not error.value.attempted and calls == []
    assert (directory / "com.private.log-rotation.timer").exists()


@pytest.mark.parametrize("failure", [None, "native", "preflight"])
def test_job_stop_pins_original_definition_nodes_and_closes_on_every_exit(tmp_path, monkeypatch, failure):
    import os
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    opened = []
    real_open = os.open
    def observe_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path).parent == directory:
            opened.append((fd, os.fstat(fd)))
        return fd
    monkeypatch.setattr(jobs.os, "open", observe_open)
    def check_pins(function, args):
        if function == "svc_job_stop_exact":
            assert len(opened) == 2
            for fd, original in opened:
                assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (original.st_dev, original.st_ino)
            if failure == "native":
                raise OSError("native refusal")
    native.failure = check_pins
    if failure == "preflight":
        (directory / "default.target.wants").mkdir()
        (directory / "default.target.wants" / "com.private.log-rotation.service").symlink_to(tmp_path / "foreign")
    if failure:
        with pytest.raises(jobs.JobStopError):
            jobs._stop(current, "log-rotation", fleet, native)
    else:
        jobs._stop(current, "log-rotation", fleet, native)
    assert len(opened) == 2
    for fd, _ in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
    if failure == "preflight":
        assert calls == []


@pytest.mark.parametrize("outcome", ["success", "native", "preflight", "unlink-recreate", "rename"])
def test_job_stop_pins_enablement_symlink_until_exit(tmp_path, monkeypatch, outcome):
    import os
    import stat
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    wants = directory / "timers.target.wants"; wants.mkdir()
    target = directory / "com.private.log-rotation.timer"
    link = wants / target.name; link.symlink_to(target)
    original = link.lstat()
    pinned = []
    real_open = os.open
    def observe_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == link:
            pinned.append(fd)
            info = os.fstat(fd)
            assert stat.S_ISLNK(info.st_mode)
            assert (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino)
        return fd
    monkeypatch.setattr(jobs.os, "open", observe_open)
    def check_pin(function, args):
        if function != "svc_job_stop_exact":
            return
        assert len(pinned) == 1
        fd = pinned[0]
        assert os.fstat(fd).st_ino == original.st_ino
        if outcome == "native":
            raise OSError("native refusal")
        if Path(args[1]) == target and outcome in {"unlink-recreate", "rename"}:
            if outcome == "rename":
                other = wants / "replacement"; other.symlink_to(target); other.replace(link)
            else:
                link.unlink(); link.symlink_to(target)
            assert os.fstat(fd).st_nlink == 0
            assert link.lstat().st_ino != original.st_ino
    native.failure = check_pin
    if outcome == "preflight":
        (directory / "default.target.wants").mkdir()
        (directory / "default.target.wants" / "com.private.log-rotation.service").symlink_to(tmp_path / "foreign")
    if outcome == "success":
        jobs._stop(current, "log-rotation", fleet, native)
        assert not link.is_symlink()
    else:
        with pytest.raises(jobs.JobStopError):
            jobs._stop(current, "log-rotation", fleet, native)
        assert link.is_symlink()
    assert len(pinned) == 1
    with pytest.raises(OSError):
        os.fstat(pinned[0])
    if outcome == "preflight":
        assert calls == []


def test_job_stop_refuses_enablement_without_supported_symlink_pin(tmp_path, monkeypatch):
    jobs, current, native, fleet, directory, calls = _stop_family(tmp_path, monkeypatch)
    wants = directory / "timers.target.wants"; wants.mkdir()
    link = wants / "com.private.log-rotation.timer"
    link.symlink_to(directory / link.name)
    monkeypatch.delattr(jobs.os, "O_PATH", raising=False)
    monkeypatch.delattr(jobs.os, "O_SYMLINK", raising=False)
    with pytest.raises(jobs.JobStopError, match="symlink identity pinning is unsupported"):
        jobs._stop(current, "log-rotation", fleet, native)
    assert calls == [] and link.is_symlink()
