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
from tests.package_fixtures import source_package
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
    out = composer_mod.compose_host_timers(Paths(root=root, package=source_package()))
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
    generated_a = composer_mod.compose_host_timers(Paths(root=root_a, package=source_package()))
    names_a = {p.name for p in generated_a.iterdir()}
    assert "claudlobby-canary-plane-daemon.plist" in names_a
    assert "claudlobby-plane-daemon.plist" not in names_a
    import plistlib
    daemon = plistlib.loads((generated_a / "claudlobby-canary-plane-daemon.plist").read_bytes())
    assert daemon["Label"] == "claudlobby-canary-plane-daemon"
    assert daemon["EnvironmentVariables"]["CLAUDLOBBY_HOST_SYSTEM_YAML"] == str(override)
    assert host_unit_name("plane-prune") == "claudlobby-canary-plane-prune"

    override.write_text("host: { jobs: { plane-daemon: { enroll: true } } }\n")
    generated_b = composer_mod.compose_host_timers(Paths(root=root_b, package=source_package()))
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

    assert main(["--root", str(tmp_path), "host", "job", "list"]) == 0
    assert "claude-update\tenroll=false" in capsys.readouterr().out

    assert main(["--root", str(tmp_path), "host", "job", "show", "claude-update", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["data"]["job"] == load_host_jobs()["claude-update"]

    assert main(["--root", str(tmp_path), "host", "job", "show", "claude-update"]) == 0
    assert json.loads(capsys.readouterr().out) == shown["data"]["job"]


def test_host_job_run_requires_selected_enabled_unit_and_reports_only_request(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    native = tmp_path / "native"
    native.mkdir()
    generated = tmp_path / "generated"
    installed_dir = tmp_path / "user-units"
    generated.mkdir()
    installed_dir.mkdir()
    name = "claudlobby-plane-prune"
    release = SimpleNamespace(release_id="selected", native_path=native, seal_sha256="seal")
    plan = SimpleNamespace(release_id="selected", release_seal="seal", blob=lambda _: b"unit")
    declarations, items, entries, units = [], [], [], []
    for suffix in (".service", ".timer"):
        source = generated / (name + suffix)
        source.write_text("reviewed unit")
        installed = installed_dir / source.name
        installed.write_text("reviewed unit")
        declaration = SimpleNamespace(source=source, scope="host", working_directory=root,
                                      environment=(), service=name + ".service" if suffix == ".timer" else None)
        item = {"phase": "producers", "enroll": True, "sha256": suffix, "mode": 0o640}
        target = source.name
        entry = {"source": str(source), "target": target, "installed": str(installed),
                 "after": {"kind": "file", "sha256": suffix, "mode": 0o640},
                 "working_directory": str(root), "environment": {}}
        declarations.append(declaration)
        items.append((declaration, item))
        entries.append(entry)
        units.append(SimpleNamespace(declaration=declaration, target=target,
                                     installed=(SimpleNamespace(path=str(installed)),),
                                     properties=(("LoadState", "loaded"),)))
    calls = []
    class Native:
        package = SimpleNamespace(native=native)
        response = subprocess.CompletedProcess([], 0, "invoking\nrun-requested\n", "")
        def read(self, function):
            assert function == "svc_inventory_catalog"
            return f"manager\tLinux\ndirectory\t{installed_dir}\n"
        def call(self, function, *args, timeout=30):
            calls.append((function, args))
            return self.response
    @contextmanager
    def admitted(*_args, **_kwargs):
        yield release
    monkeypatch.setattr(host_run, "mutation_admission", admitted)
    monkeypatch.setattr(host_run.RuntimeIdentity, "current", lambda: object())
    monkeypatch.setattr(host_run, "load_host_jobs", lambda: {"plane-prune": {"type": "oneshot"},
                                                              "pull-root": {"enroll": True}})
    monkeypatch.setattr(host_run, "read_selection", lambda *_: {"release_id": "selected", "plan_id": "plan"})
    monkeypatch.setattr(host_run, "read_plan", lambda *_: plan)
    monkeypatch.setattr(host_run, "planned_units", lambda *_: tuple(items))
    monkeypatch.setattr(host_run, "current_declarations", lambda *_: tuple(declarations))
    monkeypatch.setattr(host_run, "selected_phase_entries", lambda *_: tuple(entries))
    monkeypatch.setattr(host_run, "validate_unit_admission", lambda *_: None)
    def inventory(*_args, **kwargs):
        assert kwargs["only_names"] == frozenset({name + ".service", name + ".timer"})
        return SimpleNamespace(require_complete=lambda: SimpleNamespace(units=units))
    monkeypatch.setattr(host_run, "collect_enrollment", inventory)
    adapter = Native()

    result = host_run.run_host_job(root, "plane-prune", adapter=adapter)
    assert (result.native_outcome, result.completion) == ("requested", "unobserved")
    assert calls == [("svc_host_job_run_exact", (str(installed_dir / (name + ".service")), name + ".service"))]

    adapter.response = subprocess.CompletedProcess([], 1, "invoking\n", "failed")
    with pytest.raises(host_run.HostJobError) as uncertain:
        host_run.run_host_job(root, "plane-prune", adapter=adapter)
    assert uncertain.value.effect_attempted and uncertain.value.unavailable

    calls.clear()
    with pytest.raises(host_run.HostJobError, match="retired"):
        host_run.run_host_job(root, "pull-root", adapter=adapter)
    assert calls == []
    monkeypatch.setattr(host_run, "load_host_jobs", lambda: {"plane-prune": {"type": "oneshot", "enroll": False}})
    with pytest.raises(host_run.HostJobError, match="disabled"):
        host_run.run_host_job(root, "plane-prune", adapter=adapter)
    assert calls == []

    monkeypatch.setattr(host_run, "load_host_jobs", lambda: {"plane-prune": {"type": "oneshot"}})
    entries[0]["installed"] = str(tmp_path / "foreign" / (name + ".service"))
    with pytest.raises(host_run.HostJobError, match="placement"):
        host_run.run_host_job(root, "plane-prune", adapter=adapter)
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
