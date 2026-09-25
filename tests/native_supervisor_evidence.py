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
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
import uuid


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
        env.update(XDG_RUNTIME_DIR=f'/run/user/{uid}',
                   DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{uid}/bus')
    validate_env = dict(env)  # Do not leak the lifecycle fixture's state path into the full harness.
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

    preexisting_tmux_pids = tmux_server_pids()
    before = {'unit_files': unit_files(), 'registrations': registrations(), 'tmux': tmux_snapshot()}
    (evidence / 'before.json').write_text(json.dumps(before, indent=2) + '\n')
    result = {'platform': system, 'source': str(source), 'python': sys.version,
              'bash': run(['/bin/bash', '--version']).stdout.splitlines()[0],
              'lifecycle': {'passed': False}, 'validate': {'passed': False}}
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
        run(['/bin/bash', source / 'lib/spin-up-bot.sh', directory])
        return wait(lambda: pane_pid(directory, label, old_pid), f'{label} failed to start a new stub session')

    def pane_pid(directory, label, old_pid=None):
        probe = run(['tmux', '-L', label, 'list-panes', '-t', directory.name,
                     '-F', '#{pane_pid}'], check=False)
        pid = probe.stdout.strip()
        return pid if probe.returncode == 0 and pid and pid != old_pid else None

    def settled(label):
        if system == 'Linux':
            return run(['systemctl', '--user', 'show', '--value', '-p', 'SubState', label], check=False).stdout.strip() == 'exited'
        probe = run(['/bin/launchctl', 'print', f'gui/{uid}/{label}'], check=False)
        return probe.returncode == 0 and 'state = not running' in probe.stdout and 'last exit code = 0' in probe.stdout

    def down(directory, label, purge=False):
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

    try:
        guard, guard_label = make_bot('guard')
        guard_pid = up(guard, guard_label)
        wait(lambda: settled(guard_label), 'guard launcher did not settle')
        target, label = make_bot('target')
        first = up(target, label)
        wait(lambda: settled(label), 'first launcher did not settle')
        second = up(target, label, first)
        wait(lambda: settled(label), 'restarted launcher did not settle')
        run(['tmux', '-L', label, 'kill-session', '-t', target.name])
        run(['/bin/bash', source / 'lib/keepalive.sh', target], timeout=90)
        third = wait(lambda: pane_pid(target, label, second), 'keepalive did not restore the stub session')
        wait(lambda: settled(label), 'keepalive launcher did not settle')
        assert 'RESTART' in (target / 'keepalive.log').read_text()
        down(target, label)
        down(target, label)  # installed state already absent; cleanup is idempotent
        up(target, label)
        wait(lambda: settled(label), 're-enrolled launcher did not settle')
        down(target, label, purge=True)
        down(target, label, purge=True)  # already-purged bot is a no-op
        assert pane_pid(guard, guard_label) == guard_pid, 'unrelated native bot was disturbed'
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
        result['lifecycle'] = {'passed': True, 'pids': [first, second, third], 'receipts': len(receipts),
            'cases': ['fresh enrollment', 'installed restart', 'keepalive dead-session restart',
                      'teardown', 'repeat teardown', 're-enrollment', 'purge', 'repeat purge',
                      'unrelated native bot and state key preserved']}
    except Exception:
        result['lifecycle']['error'] = traceback.format_exc()
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
        proc = run(['/bin/bash', source / 'lib/validate-bot-change.sh'], check=False, timeout=1800,
                   run_env=validate_env)
        output = proc.stdout + proc.stderr
        (evidence / 'validate-bot-change.log').write_text(output)
        failures = re.findall(r'^\s*FAIL\s+(.+)$', output, re.MULTILINE)
        skips = re.findall(r'^\s*SKIP\s+(.+)$', output, re.MULTILINE)
        summary = re.findall(r'^=== (\d+) passed, (\d+) failed ===$', output, re.MULTILINE)
        result['validate'] = {'passed': proc.returncode == 0 and bool(summary) and not failures,
                              'rc': proc.returncode, 'summary': summary, 'failures': failures, 'skips': skips}
        if system == 'Linux' and any('#1002' in item for item in skips):
            result['validate']['passed'] = False
            result['validate']['native_bus_missing'] = True
    except Exception:
        result['validate']['error'] = traceback.format_exc()
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
        after = {'unit_files': unit_files(), 'registrations': registrations(), 'tmux': tmux_snapshot()}
        (evidence / 'after.json').write_text(json.dumps(after, indent=2) + '\n')
        assert after['unit_files'] == before['unit_files'], 'preexisting user unit files changed or probe files leaked'
        for label, status in before['registrations'].items():
            assert after['registrations'].get(label) == status, f'preexisting service changed: {label}'
        assert after['tmux'] == before['tmux'], 'preexisting tmux changed or servers leaked'
        assert json.loads(state.read_text())['bots']['unrelated'] == preserved_key
        result['preserved_existing_state'] = True
    except Exception:
        result['preserved_existing_state'] = False
        result['preservation_error'] = traceback.format_exc()
    (evidence / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if all((result['lifecycle']['passed'], result['validate']['passed'],
                    result['preserved_existing_state'], not result.get('cleanup_errors'))) else 1


if __name__ == '__main__':
    raise SystemExit(main())
