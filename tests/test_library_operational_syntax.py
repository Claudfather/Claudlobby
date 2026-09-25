"""Mechanical syntax gate, not a proof of contextual/behavioral coherence.

Scope: expertise, protocols and delegate. Wider status skills are excluded
per #1640. The lessons manual tmux fallback is also outside this scope.
Forbidden command examples may be quoted in prose; fenced blocks are recipes.
Unit mutation commands are checked in prose as well as fences.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def operational_errors(text):
    errors = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence:
            for match in re.finditer(r'\btmux\s+([^;|`\n]+)', line):
                command = match.group(1)
                if re.search(r'\bsend-keys\b', command):
                    errors.append((number, 'dispatch through lib/dispatch.sh or dispatch-task.sh'))
                else:
                    read = re.search(r'\b(capture-pane|list-sessions|has-session)\b', command)
                    if read and not re.search(r'(?:^|\s)-(?:L|S)\s*\S+', command[:read.start()]):
                        errors.append((number, 'read the explicit bot socket with tmux -L/-S'))
        for match in re.finditer(r'\b(?:sudo\s+)?systemctl\s+([^`\n]+)', line):
            whole, tail = match.group(0), match.group(1)
            action = re.search(r'\b(restart|stop|start)\b(?:\s+(\S+))?', tail)
            if whole.startswith('sudo '):
                errors.append((number, 'use lib/spin-up-bot.sh; bot services use user scope'))
            elif action:
                if not re.search(r'(?:^|\s)--user(?:\s|$)', tail):
                    errors.append((number, 'systemctl bot commands require --user'))
                target = action.group(2)
                if target and not any(token in target for token in ('{{SERVICE_PREFIX}}', '$BOT_SERVICE', '${BOT_SERVICE}')):
                    errors.append((number, 'bot unit must carry {{SERVICE_PREFIX}} or BOT_SERVICE'))
    return errors


def test_library_operational_commands_use_their_doors():
    paths = sorted((ROOT/'library/expertise').glob('*.md')) + sorted((ROOT/'library/protocols').glob('*.md'))
    paths.append(ROOT/'library/skills/delegate/SKILL.md')
    errors = [(str(path.relative_to(ROOT)), line, why) for path in paths
              for line, why in operational_errors(path.read_text())]
    assert not errors, '\n'.join(f'{path}:{line}: {why}' for path, line, why in errors)


@pytest.mark.parametrize('command', [
    'tmux send-keys -t worker task Enter',
    'tmux -L private send-keys -t worker task Enter',
    'tmux capture-pane -t worker -p', 'tmux capture-pane -t worker -L too-late', 'tmux list-sessions', 'tmux has-session -t worker',
    'sudo systemctl restart worker', 'systemctl restart worker',
    'systemctl --user restart worker',
])
def test_reintroduced_bad_recipe_is_rejected(command):
    assert operational_errors('```bash\n'+command+'\n```')


@pytest.mark.parametrize('text', [
    'Never hand-type `tmux send-keys -t`.',
    '```bash\ntmux -L "$BOT_SERVICE" capture-pane -t worker -p\n```',
    '```bash\ntmux -S /tmp/private.sock has-session -t worker\n```',
    '`systemctl --user restart {{SERVICE_PREFIX}}.<bot>`',
    '`systemctl --user restart "$BOT_SERVICE.service"`',
    '```bash\n"$CLAUDLOBBY_ROOT/lib/dispatch.sh" worker "task"\n```',
])
def test_valid_recipe_and_prohibition_quotes_are_accepted(text):
    assert not operational_errors(text)


def test_prose_sudo_is_rejected_and_dispatch_protocol_quotes_pass():
    assert operational_errors('Linux: `sudo systemctl restart <bot>`')
    assert not operational_errors((ROOT/'library/protocols/dispatch.md').read_text())


def test_rendered_manager_and_worker_use_the_same_operational_doors(fleet_dir, tmp_path, monkeypatch):
    import shutil
    from claudlobby.composer import compose_bot
    from claudlobby.paths import Paths
    from tests.conftest import install_real_template, load_test_fleet

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PLANE_EMIT_DISABLED', '1')
    for relative in ('expertise/orchestration.md', 'expertise/software-engineering.md',
                     'protocols/context-management.md', 'protocols/safe-worker-restart.md',
                     'protocols/dispatch.md'):
        shutil.copy2(ROOT/'library'/relative, fleet_dir/'library'/relative)
    for skill in ('restart', 'delegate'):
        shutil.copytree(ROOT/'library/skills'/skill, fleet_dir/'library/skills'/skill)
    install_real_template(fleet_dir)
    fleet = load_test_fleet(fleet_dir)
    paths = Paths(root=fleet_dir, fleet_dir=fleet_dir)
    for name in ('lead', 'worker-1'):
        bot = fleet.bots[name]
        bot.telegram.handle = ''
        bot.protocols = ['context-management', 'safe-worker-restart', 'dispatch']
        bot.skills = ['restart', 'delegate']
        output = compose_bot(bot, fleet, paths, log=lambda _: None)
        rendered = (output/'CLAUDE.md').read_text()
        assert not operational_errors(rendered)
        assert 'sudo systemctl' not in rendered
        assert 'lib/dispatch.sh' in rendered and 'lib/dispatch-task.sh' in rendered
        assert 'lib/spin-up-bot.sh' in rendered
        assert 'preserve context first' in rendered
        assert 'tmux -L <bot-service> capture-pane' in rendered
        assert (output/'.claude/skills/restart').is_symlink()
        assert (output/'.claude/skills/delegate').is_symlink()
    assert not (fleet_dir/'state/plane').exists()
    assert not (home/'.claude/channels').exists()
