"""Public reload passes only selected scope to the packaged native owner."""

from dataclasses import replace
from pathlib import Path
import subprocess
import pytest

from claudlobby import fleet_reload
from claudlobby.active_config import resolve_active_context
from tests.fixtures.current_runtime import current_runtime, tmp_path  # noqa: F401


@pytest.fixture
def git_native(current_runtime):
    root,installation,rendered,host=current_runtime
    return root,host.package,host.snapshot


def test_reload_uses_committed_native_roster_and_reports_observed_result(git_native,monkeypatch):
    root,package,snapshot=git_native
    context=resolve_active_context(root=root,fleet="example",package=package)
    plugins=tuple(context.fleet.plugins.required)
    seen=[]
    original=subprocess.run
    def run(command,**kwargs):
        if command[0]=="git":return original(command,**kwargs)
        assert command[0]==str(package.runtime_native/"reload-fleet.sh")
        seen.append((command,kwargs["env"]))
        return subprocess.CompletedProcess(command,0,"marked\tmanager\n"+
            "".join("refreshed\t"+plugin+"\n" for plugin in plugins),"")
    monkeypatch.setattr(fleet_reload.subprocess,"run",run)
    result=fleet_reload.reload_fleet(root=root,fleet="example")
    assert result.plugins_refreshed==plugins and result.bots_marked==("manager",)
    expected=[str(package.runtime_native/"reload-fleet.sh"),"--admitted-runtime",
              "--fleet","example","--bots-dir",str(context.paths.runtime_bots)]
    for plugin in plugins:expected.extend(("--plugin",plugin))
    expected.extend(("--bot","manager","--bot","worker"))
    assert seen[0][0]==expected
    assert seen[0][1]["CLAUDLOBBY_NATIVE_ADMITTED"]=="1"
    assert seen[0][1]["CLAUDLOBBY_NATIVE_PYTHON"]
    assert result.release_id is None and result.source_revision==snapshot["revision"]
    assert result.config_fingerprint==snapshot["fingerprint"]


def test_reload_rejects_foreign_native_owner_before_subprocess(git_native,monkeypatch):
    root,package,snapshot=git_native
    context=resolve_active_context(root=root,fleet="example",package=package)
    foreign=replace(context,paths=replace(context.paths,package=replace(package,native=root/"foreign")))
    monkeypatch.setattr(fleet_reload,"resolve_operation_scope",lambda **_: (foreign,None))
    monkeypatch.setattr(fleet_reload.subprocess,"run",lambda *a,**k:pytest.fail("native ran"))
    with pytest.raises(fleet_reload.FleetReloadError,match="differs"):
        fleet_reload.reload_fleet(root=root,fleet="example")


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
    original_run=subprocess.run
    def native_run(argv,**kwargs):
        if argv[0]=='git':return original_run(argv,**kwargs)
        if argv[:2] == ['bash', str(package.runtime_native / 'env-tiers.sh')]:
            # The read-only resolver deliberately gets a sanitized tier context,
            # not the admitted-runtime marker used for private CLI re-entry.
            assert 'CLAUDLOBBY_NATIVE_ADMITTED' not in kwargs['env']
            return original_run(argv, **kwargs)
        return run(argv,**kwargs)
    monkeypatch.setattr(fleet_reload.subprocess, 'run', native_run)
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
        assert (package.runtime_native / 'git-pull-all.sh').is_file()
        result = repo_pull_operations.pull_repositories(root, 'example', 'manager')
    else:
        result = fleet_notification.notify_fleet(root=root, fleet='example', level='notice', event='synthetic',
            message='Synthetic current notice', send=lambda *a, **k: RecordingAlertOutcome(
                ChannelOutcome('unconfigured', True), ChannelOutcome('unconfigured', True)))
    assert result.release_id is None
    assert result.source_revision == snapshot['revision'] and result.config_fingerprint == snapshot['fingerprint']
    if operation == 'notification':
        assert not seen  # Its carrier is the supplied send adapter, not CLI re-entry.
        assert result.manager.status == result.telegram.status == 'unconfigured'
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
