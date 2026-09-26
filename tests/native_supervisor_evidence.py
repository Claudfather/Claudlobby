"""Opt-in hosted evidence for phase 04; never collected by the ordinary suite.

Run with the source export's own venv. Native services and tmux are real;
CLAUDE_BIN alone is an auth-free session stub. No lifecycle script is patched.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import pwd
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
import uuid


def process_group_members(pgid: int) -> dict[int, str]:
    """Only identities/states are read; no process arguments or env are logged."""
    listing = subprocess.run(['/bin/ps', '-eo', 'pid=,pgid=,stat='],
                             text=True, capture_output=True, check=True, timeout=10)
    return {int(row[0]): row[2] for line in listing.stdout.splitlines()
            if len(row := line.split()) == 3 and int(row[1]) == pgid}



def signal_live_group(proc, sig: int) -> list[int]:
    """Reap an exited leader; never hide a permission failure for live members."""
    proc.poll()
    members = process_group_members(proc.pid)
    live = [pid for pid, state in members.items() if not state.startswith('Z')]
    if not live:
        return []
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can report EPERM when the shell exits between observation
        # and signaling, leaving only its unreaped zombie. Absence must be
        # demonstrated again; EPERM with any live member still propagates.
        proc.poll()
        if any(not state.startswith('Z') for state in process_group_members(proc.pid).values()):
            raise
    return sorted(live)


def tagged_processes(token: str, excluded_group: int) -> dict[int, dict[str, int | str]]:
    """Find only this invocation's detached children; never persist argv/env.

    A private inherited token also follows setsid descendants after they escape
    the leader's process group. A UID-wide scan is read-only; mutations require
    that exact random token, not a command name or an ambient HOME match.
    """
    listing = subprocess.run(
        ['/bin/ps', 'eww', '-U', str(os.getuid()), '-o', 'pid=,ppid=,pgid=,stat=,command='],
        text=True, capture_output=True, check=True, timeout=10)
    marker = re.compile(r'(?:^| )CLAUDLOBBY_NATIVE_RUN_TOKEN=' + re.escape(token) + r'(?: |$)')
    result = {}
    for line in listing.stdout.splitlines():
        row = line.split(None, 4)
        if len(row) == 5 and int(row[2]) != excluded_group and marker.search(row[4]):
            result[int(row[0])] = {'ppid': int(row[1]), 'pgid': int(row[2]), 'state': row[3]}
    return result


def reap_detached(token: str, excluded_group: int, grace: float) -> dict:
    """Stop leaves first so still-live parents can wait/reap their children."""
    terminated = set()
    end = time.monotonic() + grace
    while members := tagged_processes(token, excluded_group):
        terminated.update(members)
        parents = {value['ppid'] for value in members.values()}
        leaves = members.keys() - parents
        sig = signal.SIGTERM if time.monotonic() < end else signal.SIGKILL
        for pid in leaves:
            # Recheck ownership immediately before each signal (PID reuse).
            if pid not in tagged_processes(token, excluded_group):
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        if time.monotonic() >= end + 5:
            break
        time.sleep(.1)
    return {'terminated': sorted(terminated), 'remaining': tagged_processes(token, excluded_group)}


def run_owned_session(argv, *, cwd, env, timeout, stdout_path, stderr_path, grace=20):
    """Bound a separate process group, allowing TERM/EXIT cleanup before KILL.

    File-backed capture cannot hang on a pipe kept open by an orphan. Normal
    returns also audit the group: plain Python/Bun/sampler children must not
    survive and contaminate the next revision's observation.
    """
    timed_out = False
    cancelled = False
    previous_term = signal.getsignal(signal.SIGTERM)

    def cancel_controller(_signum, _frame):
        raise InterruptedError('native validation controller cancelled')

    token = uuid.uuid4().hex
    child_env = {**env, 'CLAUDLOBBY_NATIVE_RUN_TOKEN': token}
    cleanup = {'terminated_children': [], 'remaining': {},
               'detached_terminated': [], 'detached_remaining': {}}
    with stdout_path.open('w+') as out, stderr_path.open('w+') as err:
        proc = subprocess.Popen([str(value) for value in argv], cwd=cwd, env=child_env,
                                stdout=out, stderr=err, text=True, start_new_session=True)
        signal.signal(signal.SIGTERM, cancel_controller)
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
            except (InterruptedError, KeyboardInterrupt):
                cancelled = True
            if not timed_out and not cancelled:
                end = time.monotonic() + 1
                while process_group_members(proc.pid) and time.monotonic() < end:
                    time.sleep(.1)
        finally:
            # On timeout or an interrupted controller, signal the entire owned
            # group. BASH_ENV installs a TERM -> exit trap so the harness's
            # existing EXIT handler retains its native-unit cleanup ownership.
            # Do not let a second cancellation interrupt the bounded reap.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                # Kill escaped leaves while the shell is still alive to reap
                # them. The harness's setsid scope probe is one such child.
                detached = reap_detached(token, proc.pid, min(grace, 5))
                cleanup['detached_terminated'] = detached['terminated']
                proc.poll()  # Detached-child teardown may have completed the shell's wait/EXIT.
                members = process_group_members(proc.pid)
                if members:
                    cleanup['terminated_children'] = signal_live_group(proc, signal.SIGTERM)
                    end = time.monotonic() + (min(grace, 5) if cancelled else grace)
                    while time.monotonic() < end:
                        proc.poll()  # Reap the leader before testing group absence.
                        if not process_group_members(proc.pid):
                            break
                        time.sleep(.1)
                    else:
                        signal_live_group(proc, signal.SIGKILL)
                proc.wait(timeout=5)
                end = time.monotonic() + 5
                while process_group_members(proc.pid) and time.monotonic() < end:
                    time.sleep(.1)
                cleanup['remaining'] = process_group_members(proc.pid)
                # EXIT cleanup may briefly spawn another detached helper.
                detached = reap_detached(token, proc.pid, min(grace, 5))
                cleanup['detached_terminated'] = sorted(set(cleanup['detached_terminated']) | set(detached['terminated']))
                cleanup['detached_remaining'] = detached['remaining']
            finally:
                signal.signal(signal.SIGTERM, previous_term)
        out.seek(0)
        err.seek(0)
        rc = 124 if timed_out else (130 if cancelled else proc.returncode)
        completed = subprocess.CompletedProcess(argv, rc,
                                                out.read(), err.read())
    return completed, {'timed_out': timed_out, 'cancelled': cancelled, **cleanup}


def start_records(directory: Path) -> list[str]:
    marker = directory / 'data/native-starts'
    return marker.read_text().splitlines() if marker.is_file() else []


def recorded_new_start(directory: Path, pid: str | None, before: list[str]) -> bool:
    """A live pane is insufficient: the configured stub must record its PID."""
    return bool(pid) and start_records(directory) == before + [pid]



def persistent_launch_agents(directories: list[Path]) -> dict:
    """Snapshot definitions, not the PIDs of macOS's on-demand services."""
    files = {}
    labels = set()
    for directory in directories:
        for path in directory.glob('*.plist'):
            raw = path.read_bytes()
            files[str(path)] = hashlib.sha256(raw).hexdigest()
            label = plistlib.loads(raw).get('Label')
            if isinstance(label, str):
                labels.add(label)
    return {'files': files, 'labels': sorted(labels)}



def verify_launchd_preservation(before: dict, after: dict) -> dict:
    """Protect persistent jobs while disclosing autonomous ambient liveness."""
    assert after['persistent_launch_agents'] == before['persistent_launch_agents'], 'persistent launch-agent definitions changed'
    assert after['launchd_disabled'] == before['launchd_disabled'], 'launchd disabled overrides changed'
    protected = set(before['persistent_launch_agents']['labels']) & before['registrations'].keys()
    assert protected <= after['registrations'].keys(), 'preexisting persistent launch agent was unloaded'
    return {
        label: {'before': before['registrations'].get(label), 'after': after['registrations'].get(label)}
        for label in before['registrations'].keys() | after['registrations'].keys()
        if after['registrations'].get(label) != before['registrations'].get(label)}


def prepare_session_home(home: Path) -> None:
    """Supply the installed-session prerequisite, without pre-accepting consent.

    start-bot locks settings.json before its callback creates the parent. The
    complete validation harness documents the same empty-HOME prerequisite.
    """
    (home / '.claude').mkdir(exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    assert os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'Hosted runners only'
    owned = Path(os.environ['CLAUDLOBBY_NATIVE_DISPOSABLE_ROOT']).resolve(strict=True)
    assert owned.parent == Path('/tmp').resolve() and owned.name.startswith('cl-native-')
    assert owned.stat().st_uid == os.getuid(), 'Disposable tree must belong to this runner user'
    source, scratch, evidence = (p.resolve() for p in (args.source, args.scratch, args.evidence))
    assert all(p.is_relative_to(owned) for p in (source, scratch, evidence))
    assert source != scratch
    scratch.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=True)
    assert Path(sys.prefix).resolve() == source / '.venv', 'Use this export\'s own venv'
    import claudlobby
    assert Path(claudlobby.__file__).resolve().is_relative_to(source)
    home = Path.home().resolve()
    assert home.is_relative_to(owned), 'Native HOME must be inside the disposable tree'
    prepare_session_home(home)
    system = platform.system()
    assert system in ('Linux', 'Darwin')
    uid = os.getuid()
    env = {
        'HOME': str(home), 'USER': os.environ['USER'], 'LOGNAME': os.environ['USER'],
        'PATH': f'{source}/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin',
        'TMPDIR': str(scratch), 'LANG': 'en_US.UTF-8',
        'CLAUDLOBBY_ROOT': str(source), 'PLANE_EMIT_DISABLED': '0',
        'PLANE_EMIT_CLI': str(source / '.venv/bin/claudlobby'),
        'PLANE_SOCKET': str(owned / 'no-plane.sock'),
        'TMUX_TMPDIR': str(owned / 'tmux'),
    }
    assert not Path(env['PLANE_SOCKET']).exists()
    Path(env['TMUX_TMPDIR']).mkdir(exist_ok=True)
    assert len(str(Path(env['TMUX_TMPDIR']) / f'tmux-{uid}' / ('n' * 45))) < 104
    if system == 'Linux':
        env.update(XDG_CONFIG_HOME=str(home / '.config'), XDG_RUNTIME_DIR=f'/run/user/{uid}',
                   DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus')
    validate_env = dict(env)  # Do not leak the lifecycle fixture's state path into the full harness.
    cancel_trap = scratch / 'cancel-harness.sh'
    cancel_trap.write_text("trap 'exit 124' TERM\n")
    validate_env['BASH_ENV'] = str(cancel_trap)
    commands = []

    def run(argv, *, check=True, timeout=60, run_env=None):
        values = [str(x) for x in argv]
        try:
            proc = subprocess.run(values, cwd=source, env=run_env or env,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            def decoded(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
            proc = subprocess.CompletedProcess(values, 124, decoded(error.stdout),
                                               decoded(error.stderr) + f'\nTIMEOUT after {timeout}s\n')
        commands.append({'argv': [str(x) for x in argv], 'rc': proc.returncode,
                         'stdout': proc.stdout, 'stderr': proc.stderr})
        (evidence / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if check:
            assert proc.returncode == 0, commands[-1]
        return proc

    def wait(predicate, message, seconds=30):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(.2)
        raise AssertionError(message)

    unit_dir = home / ('.config/systemd/user' if system == 'Linux' else 'Library/LaunchAgents')
    unit_dir.mkdir(parents=True, exist_ok=True)
    extension = '.service' if system == 'Linux' else '.plist'
    if system == 'Linux':
        run(['systemctl', '--user', 'show-environment'])
        assert run(['systemctl', '--user', 'show', '-p', 'Environment', '--value']).returncode == 0
        # Establish the disposable manager's bus before recording the baseline;
        # the first unit install activates it otherwise (inactive -> running).
        run(['systemctl', '--user', 'start', 'dbus.service'])
        assert run(['systemctl', '--user', 'is-active', 'dbus.service']).stdout.strip() == 'active'
    else:
        # The production scripts address gui/<uid>, so a user/<uid> fallback
        # would certify a different contract and is deliberately refused.
        run(['/bin/launchctl', 'version'])
        run(['/bin/launchctl', 'print', f'gui/{uid}'])

    def registrations():
        if system == 'Linux':
            result = run(['systemctl', '--user', 'list-units', '--all', '--type=service',
                          '--plain', '--no-legend', '--no-pager'])
            return {line.split()[0]: line.split()[1:4] for line in result.stdout.splitlines()
                    if line.strip() and line.split()[0].endswith('.service')}
        result = run(['/bin/launchctl', 'list'])
        return {row[2]: row[:2] for line in result.stdout.splitlines()[1:]
                if len(row := line.split()) == 3}

    def unit_files():
        return {str(p.relative_to(unit_dir)): (('symlink', os.readlink(p)) if p.is_symlink()
                else ('sha256', hashlib.sha256(p.read_bytes()).hexdigest()))
                for p in unit_dir.rglob('*') if p.is_symlink() or p.is_file()}

    def tmux_snapshot():
        result = {}
        for parent in (Path('/tmp'), Path(env['TMUX_TMPDIR'])):
            directory = parent / f'tmux-{uid}'
            if directory.is_dir():
                for sock in directory.iterdir():
                    probe = run(['tmux', '-S', sock, 'list-panes', '-a', '-F',
                                 '#{session_id}:#{session_name}:#{pane_id}:#{pane_pid}'], check=False)
                    if probe.returncode == 0:
                        result[str(sock)] = sorted(probe.stdout.splitlines())
        return result

    def tmux_server_pids():
        listing = run(['ps', '-U', str(uid), '-o', 'pid=', '-o', 'comm='])
        return {int(parts[0]) for line in listing.stdout.splitlines()
                if len(parts := line.strip().split(None, 1)) == 2
                and parts[1].split('/')[-1] in ('tmux', 'tmux: server')}

    def preservation_snapshot():
        snapshot = {'unit_files': unit_files(), 'registrations': registrations(), 'tmux': tmux_snapshot()}
        if system == 'Darwin':
            # The GUI domain also contains unrelated on-demand Apple jobs.
            # Their PIDs/exit status are observations, not stable definitions.
            # Preserve actual agent definitions and loaded persistent labels.
            account_home = Path(pwd.getpwuid(uid).pw_dir)
            snapshot['persistent_launch_agents'] = persistent_launch_agents([
                unit_dir, account_home / 'Library/LaunchAgents',
                Path('/Library/LaunchAgents'), Path('/System/Library/LaunchAgents')])
            disabled = run(['/bin/launchctl', 'print-disabled', f'gui/{uid}']).stdout
            assert 'disabled services = {' in disabled, 'unrecognized launchctl disabled-state output'
            snapshot['launchd_disabled'] = dict(re.findall(r'"([^"\n]+)"\s*=>\s*(true|false)', disabled))
        return snapshot

    preexisting_tmux_pids = tmux_server_pids()
    before = preservation_snapshot()
    (evidence / 'before.json').write_text(json.dumps(before, indent=2) + '\n')
    result = {'platform': system, 'source': str(source), 'python': sys.version,
              'bash': run(['/bin/bash', '--version']).stdout.splitlines()[0],
              'lifecycle': {'passed': False}, 'validate': {'passed': False}}
    result['tooling'] = {}
    for tool in ('tmux', 'jq', 'realpath', 'setsid', 'flock'):
        probe = run(['/bin/bash', '-c', 'command -v "$1"', 'probe', tool], check=False)
        result['tooling'][tool] = {'rc': probe.returncode, 'path': probe.stdout.strip()}
    if result['tooling']['realpath']['rc'] == 0:
        realpath_probe = run(['realpath', '-m', scratch / 'not-created'], check=False)
        result['tooling']['realpath_missing_components'] = {'rc': realpath_probe.returncode, 'stderr': realpath_probe.stderr}
    else:
        result['tooling']['realpath_missing_components'] = {'rc': 127, 'stderr': 'realpath unavailable'}
    bot_dirs = []
    labels = []
    prefix = 'n' + uuid.uuid4().hex[:12]
    fleet = prefix
    fleet_dir = source / 'local' / fleet
    state = source / 'state' / 'native-fleet-state.json'
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({'bots': {'unrelated': {'status': 'unchanged', 'sentinel': prefix}}}))
    preserved_key = json.loads(state.read_text())['bots']['unrelated']
    env.update(FLEET_STATE_PATH=str(state), CLAUDLOBBY_FLEET=fleet)
    stub = scratch / 'claude-stub'
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$$" >> "$BOT_DIR/data/native-starts"\n'
                    'printf "native session > "\nexec /bin/cat\n')
    stub.chmod(0o755)

    from claudlobby.config import BotConfig, FleetConfig
    from claudlobby.paths import Paths
    from claudlobby.supervision import build_supervision_spec, render_launchd_plist, render_systemd_unit

    def make_bot(name):
        bot = BotConfig(bot_id=name, name=name, expertise=['eng'])
        config = FleetConfig(name=fleet, service_prefix=prefix, bots={name: bot})
        paths = Paths(root=source, fleet_dir=fleet_dir)
        directory = paths.bot_runtime(name)
        (directory / 'data').mkdir(parents=True)
        (directory / 'logs').mkdir()
        (source / 'lib/logs').mkdir(exist_ok=True)
        label = f'{prefix}.{name}'
        assert not (unit_dir / (label + extension)).exists()
        assert label not in before['registrations'], 'fixture label is already registered'
        conf = {**env, 'BOT_DIR': str(directory), 'BOT_NAME': name, 'BOT_ID': name,
                'BOT_LABEL': name, 'BOT_SERVICE': label, 'TMUX_SOCKET': label,
                'FLEET_NAME': fleet, 'CLAUDE_BIN': str(stub), 'CLAUDE_FLAGS': '',
                'FLEET_PLUGINS_REQUIRED': '', 'BOOT_LOCK_HOLD_S': '0',
                'PANE_READY_POLL_S': '.05', 'PANE_RECOVER_TICKS': '2',
                'PANE_SEND_SETTLE_S': '0', 'STARTUP_PROMPT': ''}
        directory.joinpath('bot.conf').write_text(''.join(
            f'export {key}={shlex.quote(value)}\n' for key, value in conf.items()))
        spec = build_supervision_spec(bot, config, paths)
        # Explicit fixture isolation only: the shipped renderers and launcher
        # are used unchanged, with the test-owned root/socket/HOME environment.
        spec = replace(spec, environment={**spec.environment, **env},
                       launchd_environment_extra={'PATH': env['PATH'], 'HOME': env['HOME']})
        text = render_systemd_unit(spec) if system == 'Linux' else render_launchd_plist(spec)
        directory.joinpath(label + extension).write_text(text)
        bot_dirs.append(directory)
        labels.append(label)
        return directory, label

    def up(directory, label, old_pid=None):
        assert (directory, label) in list(zip(bot_dirs, labels)), 'not a fixture-owned native target'
        before_starts = start_records(directory)
        run(['/bin/bash', source / 'lib/spin-up-bot.sh', directory])
        return wait_for_stub(directory, label, before_starts, old_pid)

    def wait_for_stub(directory, label, before_starts, old_pid=None, seconds=30):
        def observed():
            pid = pane_pid(directory, label, old_pid)
            return pid if recorded_new_start(directory, pid, before_starts) else None
        return wait(observed, f'{label}: configured session stub did not record the new pane PID', seconds)

    def pane_pid(directory, label, old_pid=None):
        probe = run(['tmux', '-L', label, 'list-panes', '-t', directory.name,
                     '-F', '#{pane_pid}'], check=False)
        pid = probe.stdout.strip()
        return pid if probe.returncode == 0 and pid and pid != old_pid else None

    def capture_bot_diagnostics(directory, label):
        # Capture before purge, while the failed launch's files still exist.
        # The fixture has no credentials; only its own logs/markers are saved.
        diagnostic = {'label': label, 'starts': start_records(directory),
                      'tmux_env_exists': (directory / '.tmux-env').exists(), 'logs': {}}
        paths = list((directory / 'logs').glob('*.log')) + [
            source / 'lib/logs' / f'{directory.name}.out.log',
            source / 'lib/logs' / f'{directory.name}.err.log']
        for path in paths:
            if path.is_file():
                diagnostic['logs'][str(path.relative_to(source))] = path.read_text(errors='replace')[-65536:]
        run(['tmux', '-L', label, 'list-panes', '-a', '-F',
             '#{session_name}:#{pane_pid}:#{pane_current_command}'], check=False)
        run(['tmux', '-L', label, 'capture-pane', '-t', directory.name, '-p'], check=False)
        if system == 'Linux':
            run(['systemctl', '--user', 'show', label, '--property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,MainPID,FragmentPath'], check=False)
            run(['journalctl', '--user-unit', label, '-n', '80', '--no-pager', '-o', 'cat'], check=False)
        else:
            run(['/bin/launchctl', 'print', f'gui/{uid}/{label}'], check=False)
        (evidence / f'{label}-launch.json').write_text(json.dumps(diagnostic, indent=2) + '\n')

    def settled(label):
        if system == 'Linux':
            return run(['systemctl', '--user', 'show', '--value', '-p', 'SubState', label], check=False).stdout.strip() == 'exited'
        probe = run(['/bin/launchctl', 'print', f'gui/{uid}/{label}'], check=False)
        return probe.returncode == 0 and 'state = not running' in probe.stdout and 'last exit code = 0' in probe.stdout

    def down(directory, label, purge=False):
        assert (directory, label) in list(zip(bot_dirs, labels)), 'not a fixture-owned native target'
        had_conf = (directory / 'bot.conf').exists()
        teardown = run(['/bin/bash', '-x', source / 'lib/spin-down-bot.sh', directory,
                        '--reason', 'hosted native phase04 evidence', *(['--purge'] if purge else [])])
        if had_conf:
            expected = (f'/bin/launchctl bootout gui/{uid}/{label}' if system == 'Darwin'
                        else f'systemctl --user disable --now {label}.service')
            assert expected in teardown.stderr, 'native teardown command not observed in shell trace'
        wait(lambda: not pane_pid(directory, label), f'{label} tmux survived teardown')
        assert not (unit_dir / (label + extension)).exists()
        if system == 'Linux':
            probe = run(['systemctl', '--user', 'show', '--value', '-p', 'LoadState', label], check=False)
            assert probe.stdout.strip() == 'not-found', commands[-1]
        else:
            assert run(['/bin/launchctl', 'print', f'gui/{uid}/{label}'], check=False).returncode != 0
        assert not (directory / '.tmux-env').exists()
        assert directory.name not in json.loads(state.read_text())['bots']
        if purge:
            assert not directory.exists()

    # A live pane that bypasses CLAUDE_BIN must fail the same positive check.
    impostor = scratch / 'impostor'
    (impostor / 'data').mkdir(parents=True)
    impostor_label = f'{prefix}.impostor'
    try:
        run(['tmux', '-L', impostor_label, 'new-session', '-d', '-s', impostor.name, '/bin/sleep 60'])
        assert pane_pid(impostor, impostor_label), 'negative control did not create a live pane'
        try:
            wait_for_stub(impostor, impostor_label, [], seconds=1)
        except AssertionError as error:
            assert 'configured session stub did not record' in str(error)
            result['live_pane_without_stub_rejected'] = True
        else:
            raise AssertionError('live pane without the configured stub was accepted')
    finally:
        run(['tmux', '-L', impostor_label, 'kill-server'], check=False)

    try:
        guard, guard_label = make_bot('guard')
        guard_pid = up(guard, guard_label)
        wait(lambda: settled(guard_label), 'guard launcher did not settle')
        target, label = make_bot('target')
        first = up(target, label)
        wait(lambda: settled(label), 'first launcher did not settle')
        second = up(target, label, first)
        wait(lambda: settled(label), 'restarted launcher did not settle')
        before_heal = start_records(target)
        run(['tmux', '-L', label, 'kill-session', '-t', target.name])
        run(['/bin/bash', source / 'lib/keepalive.sh', target], timeout=90)
        third = wait_for_stub(target, label, before_heal, second)
        wait(lambda: settled(label), 'keepalive launcher did not settle')
        assert 'RESTART' in (target / 'keepalive.log').read_text()
        down(target, label)
        down(target, label)  # installed state already absent; cleanup is idempotent
        fourth = up(target, label, third)
        wait(lambda: settled(label), 're-enrolled launcher did not settle')
        starts = {'guard': start_records(guard), 'target': start_records(target)}
        assert starts == {'guard': [guard_pid], 'target': [first, second, third, fourth]}
        (evidence / 'stub-starts.json').write_text(json.dumps(starts, indent=2) + '\n')
        down(target, label, purge=True)
        down(target, label, purge=True)  # already-purged bot is a no-op
        assert pane_pid(guard, guard_label) == guard_pid, 'unrelated native bot was disturbed'
        assert start_records(guard) == [guard_pid], 'unrelated native bot was restarted'
        assert json.loads(state.read_text())['bots']['unrelated'] == preserved_key
        readers_spec = importlib.util.spec_from_file_location('native_plane_readers', source / 'lib/plane-readers.py')
        readers = importlib.util.module_from_spec(readers_spec)
        sys.modules[readers_spec.name] = readers
        readers_spec.loader.exec_module(readers)
        conn = readers.connect(source)
        receipts = readers.fleet_events(conn, fleet, event_type='bot_teardown_started')
        conn.close()
        assert len(receipts) >= 3, 'intentional scratch recording did not produce teardown receipts'
        assert any(row['data']['action'] == 'spin-down --purge' for row in receipts)
        (evidence / 'receipts.json').write_text(json.dumps(receipts, indent=2) + '\n')
        result['lifecycle'] = {'passed': True, 'starts': starts, 'receipts': len(receipts),
            'cases': ['fresh enrollment', 'installed restart', 'keepalive dead-session restart',
                      'teardown', 'repeat teardown', 're-enrollment', 'purge', 'repeat purge',
                      'unrelated native bot and state key preserved']}
    except Exception:
        result['lifecycle']['error'] = traceback.format_exc()
        for directory, label in zip(bot_dirs, labels):
            try:
                capture_bot_diagnostics(directory, label)
            except Exception:
                result.setdefault('diagnostic_errors', []).append(traceback.format_exc())
    finally:
        # Never use a wildcard/native-domain sweep. Reap only labels created
        # above, even when an assertion or boot failed half way through.
        for directory, label in reversed(list(zip(bot_dirs, labels))):
            try:
                down(directory, label, purge=True)
            except Exception:
                result.setdefault('cleanup_errors', []).append(traceback.format_exc())
                if system == 'Linux':
                    run(['systemctl', '--user', 'disable', '--now', label + extension], check=False)
                else:
                    run(['/bin/launchctl', 'bootout', f'gui/{uid}/{label}'], check=False)
                (unit_dir / (label + extension)).unlink(missing_ok=True)
                run(['tmux', '-L', label, 'kill-server'], check=False)
        if system == 'Linux':
            run(['systemctl', '--user', 'daemon-reload'])

    try:
        # Whole harness: no selector, rewrite, disabled recorder, or xfail.
        proc, process_cleanup = run_owned_session(
            ['/bin/bash', source / 'lib/validate-bot-change.sh'], cwd=source, env=validate_env,
            timeout=1800, stdout_path=evidence / 'validate.stdout.log',
            stderr_path=evidence / 'validate.stderr.log')
        (evidence / 'validate-process-cleanup.json').write_text(json.dumps(process_cleanup, indent=2) + '\n')
        if (process_cleanup['remaining'] or process_cleanup['detached_remaining']
                or ((process_cleanup['terminated_children'] or process_cleanup['detached_terminated'])
                    and not process_cleanup['timed_out'] and not process_cleanup['cancelled'])):
            result.setdefault('cleanup_errors', []).append({'validate_process_group': process_cleanup})
        output = proc.stdout + proc.stderr
        (evidence / 'validate-bot-change.log').write_text(output)
        failures = re.findall(r'^\s*FAIL\s+(.+)$', output, re.MULTILINE)
        skips = re.findall(r'^\s*SKIP\s+(.+)$', output, re.MULTILINE)
        summary = re.findall(r'^=== (\d+) passed, (\d+) failed ===$', output, re.MULTILINE)
        aborts = re.findall(r'^=== ABORTED \(rc (\d+)\) after (\d+) checks, before the summary: (.+) ===$', output, re.MULTILINE)
        scenarios = re.findall(r'^(?:=== validate.*|--- #.*)$', proc.stdout, re.MULTILINE)
        result['validate'] = {'passed': proc.returncode == 0 and bool(summary) and not failures,
                              'rc': proc.returncode, 'summary': summary, 'failures': failures, 'skips': skips,
                              'aborted_before_summary': not bool(summary),
                              'abort_details': [{'rc': int(rc), 'checks': int(count), 'note': note}
                                                for rc, count, note in aborts],
                              'last_scenario': scenarios[-1] if scenarios else None}
        if system == 'Linux' and any('#1002' in item for item in skips):
            result['validate']['passed'] = False
            result['validate']['native_bus_missing'] = True
    except Exception:
        result['validate']['error'] = traceback.format_exc()
        result.setdefault('cleanup_errors', []).append('Validation controller failed; complete process cleanup was not established')
    # The complete harness has its own EXIT cleanup. Audit leaks independently;
    # on failure reap only newly observed servers carrying this owned HOME.
    # Detached tmux can outlive removal of its socket directory, so checking
    # the directory alone is insufficient evidence of cleanup.
    leaked = []
    for pid in tmux_server_pids() - preexisting_tmux_pids:
        # Inspect only to establish ownership; do not persist a process's
        # complete environment in evidence.
        command = subprocess.run(['ps', 'eww', '-p', str(pid), '-o', 'command='],
                                 env=env, capture_output=True, text=True, timeout=10).stdout
        match = re.search(r'(?:^| )HOME=(\S+)', command)
        if match and Path(match.group(1)).resolve().is_relative_to(owned):
            leaked.append(pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    if leaked:
        result.setdefault('cleanup_errors', []).append(f'harness leaked owned tmux servers: {leaked}')
        for pid in leaked:
            for _ in range(30):
                if pid not in tmux_server_pids():
                    break
                time.sleep(.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    if system == 'Linux':
        for unit in unit_dir.glob('claudlobby-vbc-*.service'):
            if str(owned) not in unit.read_text() and str(owned).replace('/private/tmp/', '/tmp/') not in unit.read_text():
                continue
            result.setdefault('cleanup_errors', []).append(f'harness leaked owned native unit: {unit.name}')
            run(['systemctl', '--user', 'disable', '--now', unit.name], check=False)
            unit.unlink()
            run(['systemctl', '--user', 'reset-failed', unit.name], check=False)
        run(['systemctl', '--user', 'daemon-reload'])
    try:
        after = preservation_snapshot()
        (evidence / 'after.json').write_text(json.dumps(after, indent=2) + '\n')
        assert after['unit_files'] == before['unit_files'], 'preexisting user unit files changed or probe files leaked'
        if system == 'Darwin':
            result['ambient_registration_changes'] = verify_launchd_preservation(before, after)
            result['preservation_scope'] = ('Exact persistent launch-agent definitions and disabled overrides; '
                'previously loaded plist-backed agents remain registered. Ambient PID/status changes are disclosed, '
                'not required to remain fixed. Owned guard PID, tmux, unit files and state key are checked exactly.')
        else:
            for label, status in before['registrations'].items():
                assert after['registrations'].get(label) == status, f'preexisting service changed: {label}'
        assert after['tmux'] == before['tmux'], 'preexisting tmux changed or servers leaked'
        assert json.loads(state.read_text())['bots']['unrelated'] == preserved_key
        result['preserved_existing_state'] = True
    except Exception:
        result['preserved_existing_state'] = False
        result['preservation_error'] = traceback.format_exc()
    result['mutation_scope'] = {'lifecycle_labels': labels,
        'lifecycle_bot_dirs': [str(path) for path in bot_dirs],
        'native_domain': f'gui/{uid}' if system == 'Darwin' else f'systemd-user/{uid}'}
    (evidence / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if all((result['lifecycle']['passed'], result['validate']['passed'],
                    result['preserved_existing_state'], not result.get('cleanup_errors'))) else 1


if __name__ == '__main__':
    raise SystemExit(main())
