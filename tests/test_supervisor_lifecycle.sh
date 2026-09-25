#!/usr/bin/env bash
# Exercise the production callers in disposable copies. The ONLY production
# command substitution is the reaper's absolute /bin/launchctl path, replaced
# by a test-owned executable before any script can run. PATH alone is not safe.
# SUPERVISOR_TEST_SOURCE/CAPTURE are test-only characterization controls: run
# this same matrix against an immutable parent export and compare the JSON.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$SCRIPT_DIR/.." <<'PY'
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

source = Path(os.environ.get('SUPERVISOR_TEST_SOURCE', sys.argv[1])).resolve()
results = {}
context = os.environ.get('SUPERVISOR_TEST_CONTEXT', 'dead')

def executable(path, content):
    path.write_text('#!/bin/bash\n' + content)
    path.chmod(0o755)

with tempfile.TemporaryDirectory(prefix='supervisor-callers-') as temp:
    base = Path(temp)
    def run(door, platform, shape='canonical', rc=0, fault='', purge=False):
        name = f'{door}/{platform}/{shape}/rc{rc}/{fault or "ok"}/purge{purge}'
        if context != 'dead':
            name += '/' + context
        if os.environ.get('SUPERVISOR_TEST_FILTER', '') not in name:
            return
        root = base / str(len(results))
        lib, bot, home, bindir = [root / s for s in ('lib', 'bots/slug', 'home', 'bin')]
        for p in (lib, bot, bindir, home / '.config/systemd/user/default.target.wants', home / 'Library/LaunchAgents'):
            p.mkdir(parents=True, exist_ok=True)
        (bot / 'data').mkdir()
        trace = root / 'trace'
        trace.touch()
        for file in ('supervisor.sh', 'keepalive.sh', 'spin-up-bot.sh', 'spin-down-bot.sh'):
            shutil.copy2(source / 'lib' / file, lib / file)
        shutil.copy2(source / 'lib/lib-common.sh', lib / 'lib-common-real.sh')
        # Explicit seam in a CHARACTERIZATION COPY. Production still spells
        # /bin/launchctl; the assertion prevents an accidental real invocation.
        down = lib / 'spin-down-bot.sh'
        text = down.read_text()
        assert '/bin/launchctl' in text, 'absolute teardown command disappeared'
        down.write_text(text.replace('/bin/launchctl', shlex.quote(str(bindir / 'absolute-launchctl'))))
        assert '/bin/launchctl' not in down.read_text()
        lib.joinpath('lib-common.sh').write_text('''
. "${BASH_SOURCE[0]%/*}/lib-common-real.sh"
# Host/service/transport observations only: production caller decisions and
# the real adapter, identity loader and ERR trap remain in the executed copy.
check_tmux_session() { [ "${PROBE_CONTEXT:-}" = bridge ]; }
if [ "${PROBE_CONTEXT:-}" = bridge ]; then
    _FLOCK_BIN=""
    marker_age_within() { return 1; }
    pane_is_busy() { return 1; }
    pane_is_idle() { return 0; }
    bridge_down_state() { printf no_bridge; }
fi
service_is_starting() { return 1; }
service_is_crash_looping() { return 1; }
sleep() { :; }
ts_iso() { printf TS; }
json_escape() { printf '%s' "$1"; }
emit_fleet_event() {
    if [ "${PROBE_FAULT:-}" = receipt ]; then
        printf 'event_failed:%s\\n' "$1" >> "$PROBE_TRACE"
        printf 'did NOT record receipt\\n' >&2
        return 0
    fi
    printf 'event:%s:%s\\n' "$1" "${3:-}" >> "$PROBE_TRACE"
}
eval "$(declare -f load_bot_conf | sed '1s/load_bot_conf/_probe_load_bot_conf/')"
load_bot_conf() {
    _probe_load_bot_conf "$1" || return $?
    if [ "${PROBE_SHAPE:-}" = regenerated ]; then
        printf 'BOT_SERVICE=after-regeneration\\nBOT_NAME=changed\\nTMUX_SOCKET=socket-after\\n' > "$1/bot.conf"
    fi
}
if [ "${PROBE_FAULT:-}" = socket ]; then tmux_socket_for_bot() { return 1; }; fi
if [ "${PROBE_FAULT:-}" = callback ]; then
    echo() {
        case "$*" in 'spin-up-bot: '*|'TS RESTART '*) return 17 ;; esac
        builtin echo "$@"
    }
fi
''')
        for binary in ('systemctl', 'launchctl', 'absolute-launchctl', 'tmux'):
            # PROBE_RC injects action failure. A bridge-heal tick also reads the
            # pane before it restarts; failing that unrelated read produces
            # inherited ERR receipts on bash 3.2 before the action under test.
            # Keep this observation successful; kill-server still tests rc.
            read_success = ('case " $* " in *" capture-pane "*) exit 0 ;; esac\n'
                            if binary == 'tmux' else '')
            executable(bindir / binary,
                'printf "action:%s %s\\n" "${0##*/}" "$*" >> "$PROBE_TRACE"\n'
                + read_success + 'exit "${PROBE_RC:-0}"\n')
        executable(bindir / 'uname', 'printf "%s\\n" "$PROBE_OS"\n')
        executable(bindir / 'hostname', 'printf test-host\n')
        for script in ('install-bot-systemd.sh', 'install-bot.sh', 'start-bot.sh'):
            executable(lib / script,
                'printf "action:%s %s\\n" "${0##*/}" "$*" >> "$PROBE_TRACE"\n'
                'exit "${PROBE_RC:-0}"\n')
        executable(lib / 'fleet-state-update.sh',
            'printf "action:fleet-state-update.sh %s\\n" "$*" >> "$PROBE_TRACE"\n'
            'rm -f "$FLEET_STATE_PATH"\n')
        (root / 'state-key').touch()
        (root / 'other-key').write_text('untouched')
        identity = 'BOT_SERVICE=canonical\n'
        if shape == 'expanded':
            identity = 'PREFIX=canon\nBOT_SERVICE="${PREFIX}ical"\n'
        elif shape == 'duplicate':
            identity = 'BOT_SERVICE=wrong-first\nBOT_SERVICE=canonical\n'
        elif shape == 'empty':
            identity = 'BOT_SERVICE=wrong-first\nBOT_SERVICE=\n'
        name_line = 'BOT_NAME=\n' if shape == 'empty-name' else 'BOT_NAME=alpha\n'
        bot.joinpath('bot.conf').write_text(identity + name_line +
            'TMUX_SOCKET=private-socket\nFLEET_NAME=test-fleet\n'
            f'export FLEET_STATE_PATH="{root}/state-key"\n')
        (bot / '.tmux-env').touch()
        if shape == 'gone':
            (bot / 'bot.conf').unlink()
        installed = home / ('.config/systemd/user' if platform == 'Linux' else 'Library/LaunchAgents')
        extension = '.service' if platform == 'Linux' else '.plist'
        if shape in ('canonical', 'expanded', 'duplicate', 'regenerated'):
            (installed / ('canonical' + extension)).touch()
            if platform == 'Linux':
                (installed / 'default.target.wants/canonical.service').touch()
        if shape == 'regenerated':
            (installed / ('after-regeneration' + extension)).touch()
        if shape == 'empty-name':
            (installed / extension).touch()
        if shape in ('legacy', 'migration'):
            (installed / ('alpha' + extension)).touch()
        if shape == 'migration':
            (bot / 'canonical.service').touch()
        # A stale first assignment must not override an explicitly empty label.
        if shape in ('empty', 'duplicate'):
            (installed / ('wrong-first' + extension)).touch()
        if fault == 'log-open':
            (bot / 'keepalive.log').mkdir()
        env = {
            'PATH': f'{bindir}:/usr/bin:/bin:/usr/sbin:/sbin',
            'HOME': str(home), 'TMPDIR': str(root), 'LANG': 'C.UTF-8',
            'CLAUDLOBBY_ROOT': str(root), 'PLANE_EMIT_DISABLED': '1',
            'TMUX_BIN': str(bindir / 'tmux'), 'TMUX_TMPDIR': str(root),
            'PROBE_OS': platform, 'PROBE_TRACE': str(trace), 'PROBE_RC': str(rc),
            'PROBE_SHAPE': shape, 'PROBE_FAULT': fault, 'USER': 'test-user',
            'PROBE_CONTEXT': context, 'OBSERVABILITY_BRIDGE_HEAL': '1',
        }
        argv = ['/bin/bash', str(lib / f'{door}.sh'), str(bot)]
        if purge:
            argv.append('--purge')
        proc = subprocess.run(argv, env=env, cwd=root, text=True, capture_output=True, timeout=10)
        def normalize(value):
            value = value.replace(str(root), '<ROOT>')
            value = re.sub(r'gui/\d+/', 'gui/UID/', value)
            return re.sub(r'(line )\d+', r'\1LINE', value)
        log = bot / 'keepalive.log'
        snapshot = {
            'rc': proc.returncode,
            'trace': normalize(trace.read_text()).splitlines(),
            'stdout': normalize(proc.stdout).splitlines(),
            'stderr': normalize(proc.stderr).splitlines(),
            'log': normalize(log.read_text()).splitlines() if log.is_file() else [],
            'remaining': sorted(str(p.relative_to(root)) for p in root.rglob('*')
                if p.is_file() and (p.name.endswith(('.service', '.plist'))
                    or p.name in ('.tmux-env', 'state-key', 'other-key'))),
        }
        results[name] = snapshot
        assert (root / 'other-key').read_text() == 'untouched', name
        actions = [s for s in snapshot['trace'] if s.startswith('action:') and ' capture-pane ' not in s]
        errors = [s for s in snapshot['trace'] if s.startswith('event:script_error:')]
        if door == 'spin-down-bot':
            assert proc.returncode == 0, (name, snapshot)
            if shape == 'gone':
                assert not snapshot['trace'], (name, snapshot)
                return
            assert snapshot['trace'][0].startswith(('event:bot_teardown_started:', 'event_failed:')), (name, snapshot)
            assert actions[-1] == 'action:fleet-state-update.sh delete slug alpha', (name, snapshot)
            assert sum('tmux ' in s for s in actions) == (0 if fault == 'socket' else 1), (name, snapshot)
            assert 'bots/slug/.tmux-env' not in snapshot['remaining'], (name, snapshot)
            assert 'state-key' not in snapshot['remaining'], (name, snapshot)
            if shape == 'empty':
                assert not any('systemctl ' in s or 'launchctl ' in s for s in actions), (name, snapshot)
                assert 'BOT_SERVICE unset' in snapshot['stdout'][1], (name, snapshot)
            if platform == 'Linux' and shape != 'empty':
                assert actions[0] == 'action:systemctl --user disable --now canonical.service', (name, snapshot)
                assert not (installed / 'canonical.service').exists(), (name, snapshot)
                assert not (installed / 'default.target.wants/canonical.service').exists(), (name, snapshot)
            if platform == 'Darwin' and shape != 'empty':
                assert actions[0] == 'action:absolute-launchctl bootout gui/UID/canonical', (name, snapshot)
                assert not (installed / 'canonical.plist').exists(), (name, snapshot)
            if fault == 'receipt':
                assert 'did NOT record receipt' in proc.stderr, (name, snapshot)
            if purge:
                assert not bot.exists(), (name, snapshot)
            return
        if fault in ('callback', 'log-open'):
            assert proc.returncode == (17 if fault == 'callback' else 1), (name, snapshot)
            assert not actions, (name, snapshot)
            assert len(errors) == 1, (name, snapshot)
            return
        assert proc.returncode == rc, (name, snapshot)
        assert len(actions) == 1, (name, snapshot)
        canonical = shape in ('canonical', 'expanded', 'duplicate', 'regenerated')
        legacy = shape in ('legacy', 'migration', 'empty-name')
        if platform == 'Linux' and door == 'spin-up-bot' and shape == 'migration':
            expected = 'action:install-bot-systemd.sh <ROOT>/bots/slug'
        elif platform == 'Linux' and (canonical or legacy):
            label = 'canonical' if canonical else ('' if shape == 'empty-name' else 'alpha')
            expected = f'action:systemctl --user restart {label}.service'
        elif platform == 'Darwin' and canonical:
            expected = 'action:launchctl kickstart -k gui/UID/canonical'
        elif door == 'keepalive' or platform == 'SunOS':
            expected = 'action:start-bot.sh <ROOT>/bots/slug'
        else:
            installer = 'install-bot-systemd.sh' if platform == 'Linux' else 'install-bot.sh'
            expected = f'action:{installer} <ROOT>/bots/slug'
        assert actions == [expected], (name, snapshot, expected)
        assert len(errors) == (1 if rc else 0), (name, snapshot)
        if shape in ('expanded', 'duplicate', 'regenerated'):
            assert 'canonical' in actions[0] and 'after-regeneration' not in actions[0], (name, snapshot)
        if door == 'keepalive':
            restart = next(i for i, s in enumerate(snapshot['trace']) if s.startswith(('event:keepalive_restart:', 'event_failed:keepalive_restart')))
            action = next(i for i, s in enumerate(snapshot['trace']) if s.startswith('action:') and ' capture-pane ' not in s)
            assert restart < action, (name, snapshot)
        elif platform == 'Linux' and shape == 'migration':
            assert 'install-bot-systemd.sh' in actions[0], (name, snapshot)
            assert snapshot['stdout'] == ['spin-up-bot: migrating alpha → canonical'], (name, snapshot)

    if context == 'bridge':
        for rc in (0, 1, 2, 127):
            run('keepalive', 'Linux', rc=rc)
        run('keepalive', 'Linux', fault='callback')
    else:
        for platform in ('Linux', 'Darwin', 'SunOS'):
            for door in ('keepalive', 'spin-up-bot'):
                for shape in ('canonical', 'legacy', 'migration', 'missing', 'empty'):
                    for rc in (0, 1, 2, 127):
                        run(door, platform, shape, rc)
                if platform != 'SunOS':
                    for shape in ('expanded', 'duplicate', 'regenerated'):
                        run(door, platform, shape)
                    run(door, platform, shape='empty-name')
                    run(door, platform, fault='callback')
            run('keepalive', platform, fault='log-open')
            run('keepalive', platform, fault='receipt')
            for shape in ('canonical', 'legacy', 'missing', 'empty', 'gone'):
                run('spin-down-bot', platform, shape)
            if platform != 'SunOS':
                for shape in ('expanded', 'duplicate', 'regenerated'):
                    run('spin-down-bot', platform, shape)
            for fault in ('receipt', 'socket'):
                run('spin-down-bot', platform, fault=fault)
            run('spin-down-bot', platform, purge=True)
            for rc in (1, 2, 127):
                run('spin-down-bot', platform, rc=rc)
assert results, 'no caller scenarios ran'
if os.environ.get('SUPERVISOR_CAPTURE_PATH'):
    Path(os.environ['SUPERVISOR_CAPTURE_PATH']).write_text(json.dumps(results, indent=2, sort_keys=True) + '\n')
print(f'PASS: {len(results)} production caller scenarios, including receipt/action ordering and cleanup')
PY
# The normal discovered suite also checks the bridge-heal call context. A
# characterization export/capture explicitly controls that second observation.
if [ -z "${SUPERVISOR_TEST_CONTEXT:-}" ] && [ -z "${SUPERVISOR_TEST_SOURCE:-}" ] && [ -z "${SUPERVISOR_CAPTURE_PATH:-}" ]; then
    SUPERVISOR_TEST_CONTEXT=bridge /bin/bash "$0"
fi
