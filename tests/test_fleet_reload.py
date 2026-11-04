"""Public reload passes only selected scope to the packaged native owner."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from claudlobby import fleet_reload


@contextmanager
def _admitted(root, **kwargs):
    yield SimpleNamespace(release_id="r-selected", native_path=Path("/selected/native"),
                          cli_path=Path("/selected/venv/bin/claudlobby"))


def _scope(**kwargs):
    paths = SimpleNamespace(lib=Path("/selected/native"), runtime_bots=Path("/data/bots"),
                            package=SimpleNamespace(installation=None, runtime_native=Path("/selected/native")))
    config = SimpleNamespace(name="demo", manager="lead", bots={"lead": object(), "worker": object()},
                             plugins=SimpleNamespace(required=("one@vendor", "two@vendor")))
    return SimpleNamespace(paths=paths, fleet=config), None


def test_reload_uses_selected_native_roster_and_reports_observed_result(monkeypatch):
    seen = {}
    monkeypatch.setattr(fleet_reload, "mutation_admission", _admitted)
    monkeypatch.setattr(fleet_reload, "resolve_operation_scope", _scope)
    monkeypatch.setattr(fleet_reload, "native_environment", lambda paths, current: {"CLAUDLOBBY_CLI": "/selected/venv/bin/claudlobby",
        "CLAUDLOBBY_NATIVE_PYTHON": "/selected/venv/bin/python", "CLAUDLOBBY_NATIVE_ADMITTED": "1"})

    def run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "marked\tlead\nrefreshed\tone@vendor\nrefreshed\ttwo@vendor\n", "")

    monkeypatch.setattr(fleet_reload.subprocess, "run", run)
    result = fleet_reload.reload_fleet(root=Path("/data"), fleet="demo")
    assert result.plugins_refreshed == ("one@vendor", "two@vendor")
    assert result.bots_marked == ("lead",)
    assert seen["command"] == ["/selected/native/reload-fleet.sh", "--admitted-runtime",
                               "--fleet", "demo", "--bots-dir", "/data/bots", "--plugin", "one@vendor",
                               "--plugin", "two@vendor", "--bot", "lead", "--bot", "worker"]
    assert seen["env"]["CLAUDLOBBY_NATIVE_PYTHON"] == "/selected/venv/bin/python"


def test_reload_rejects_unselected_native_owner_before_subprocess(monkeypatch):
    monkeypatch.setattr(fleet_reload, "mutation_admission", _admitted)
    destination, origin = _scope()
    destination.paths.lib = Path("/other/native")
    monkeypatch.setattr(fleet_reload, "resolve_operation_scope", lambda **kwargs: (destination, origin))
    monkeypatch.setattr(fleet_reload.subprocess, "run", lambda *args, **kwargs: pytest.fail("native ran"))
    with pytest.raises(fleet_reload.FleetReloadError, match="differs"):
        fleet_reload.reload_fleet(root=Path("/data"), fleet="demo")


# Private config/admission coverage; subprocesses below are synthetic, never native services.
from tests.test_activation import cold, tmp_path  # noqa: E402,F401
from tests.test_releases import installed  # noqa: E402,F401
from tests.test_task_write_cli import active  # noqa: E402,F401


@pytest.fixture
def git_native(active, monkeypatch):
    from tests.test_task_permissions import _git_cli
    return _git_cli(active, monkeypatch)


@pytest.mark.parametrize('operation', ['reload', 'pulse', 'credentials', 'repositories', 'notification'])
def test_git_native_wrappers_use_current_binding_and_honest_provenance(git_native, monkeypatch, operation):
    from claudlobby import credential_check, fleet_pulse, fleet_notification, repo_pull_operations
    from claudlobby.context import native_environment, resolve_paths
    from claudlobby.runtime_admission import RuntimeIdentity, mutation_admission
    from claudlobby.recording_alerts import ChannelOutcome, RecordingAlertOutcome
    root, package, snapshot = git_native
    with mutation_admission(root, identity=RuntimeIdentity.current()) as current:
        env = native_environment(resolve_paths(root=root, fleet='example'), current=current)
    assert env['CLAUDLOBBY_NATIVE_ADMITTED'] == '1'
    assert env['CLAUDLOBBY_INSTALLATION'] == str(package.installation.directory)
    assert env['CLAUDLOBBY_NATIVE_DIR'] == str(package.runtime_native)
    assert 'CLAUDLOBBY_RELEASE_ID' not in env
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs['env']))
        if operation == 'credentials':
            return subprocess.CompletedProcess(argv, 0, 'tick-complete\n', '')
        if operation == 'repositories':
            return subprocess.CompletedProcess(argv, 0, b'git-pull-all-v1\0one\0unchanged\0', b'')
        from claudlobby.active_config import resolve_active_context
        plugins = resolve_active_context(
            root=root, fleet='example', package=package).fleet.plugins.required
        return subprocess.CompletedProcess(argv, 0, ''.join('refreshed\t' + plugin + '\n' for plugin in plugins), '')
    monkeypatch.setattr(fleet_reload.subprocess, 'run', run)
    if operation == 'reload':
        result = fleet_reload.reload_fleet(root=root, fleet='example')
    elif operation == 'pulse':
        def sweep(argv, environment, **kwargs):
            seen.append((argv, environment))
            assert environment['CLAUDLOBBY_PRIVATE_PULSE_RUNTIME'] == '1'
            return 'Synthetic sweep\n', '', 0
        monkeypatch.setattr(fleet_pulse, '_sweep', sweep)
        result = fleet_pulse.pulse_fleet(root=root, fleet='example')
    elif operation == 'credentials':
        manifest = Path(snapshot['fleets']['example']['manifest'])
        manifest.write_text(manifest.read_text() + '\n# Pending authored change\n')
        result = credential_check.check_credentials(root=root, fleet='example')
        assert result.fleet_source_changed
    elif operation == 'repositories':
        from claudlobby.active_config import resolve_active_context
        projects = resolve_active_context(root=root, fleet='example', package=package).paths.bot_runtime('manager') / 'projects'
        projects.mkdir(parents=True, exist_ok=True)
        package.runtime_native.mkdir(parents=True, exist_ok=True)
        (package.runtime_native / 'git-pull-all.sh').write_text('# Synthetic native fixture\n')
        result = repo_pull_operations.pull_repositories(root, 'example', 'manager')
    else:
        result = fleet_notification.notify_fleet(root=root, fleet='example', level='notice', event='synthetic',
            message='Synthetic current notice', send=lambda *a, **k: RecordingAlertOutcome(
                ChannelOutcome('unconfigured', True), ChannelOutcome('unconfigured', True)))
    assert result.release_id is None
    assert result.source_revision == snapshot['revision'] and result.config_fingerprint == snapshot['fingerprint']
    if seen:
        argv, environment = seen[0]
        assert environment['CLAUDLOBBY_NATIVE_ADMITTED'] == '1'
        assert environment['CLAUDLOBBY_NATIVE_PYTHON']
        assert 'CLAUDLOBBY_RELEASE_ID' not in environment
        assert '--selected-release' not in argv
        if operation in ('reload', 'credentials'):
            assert '--admitted-runtime' in argv


def test_native_environment_refuses_foreign_git_config_before_child(git_native):
    from dataclasses import replace
    from claudlobby.context import native_environment, resolve_paths
    from claudlobby.runtime_admission import RuntimeIdentity, mutation_admission
    from claudlobby.activation_state import ActivationError
    root, package, snapshot = git_native
    with mutation_admission(root, identity=RuntimeIdentity.current()) as current:
        with pytest.raises(ActivationError):
            native_environment(resolve_paths(root=root, fleet='example'),
                               current=replace(current, config_fingerprint='f' * 64))
