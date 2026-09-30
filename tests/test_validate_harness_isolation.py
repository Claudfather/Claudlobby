"""Run the harness's real Plane setup and emitter without native lifecycle work."""
from pathlib import Path
import shlex
import subprocess

from tests.conftest import constructed_env, read_fleet_events

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "harness/validate-bot-change.sh"


def _setup():
    source = HARNESS.read_text()
    start = source.index('VAL_REPO="')
    end = source.index('# Every plane read below', start)
    daemon = source.index('val_start_fixture_daemon()')
    started = source.index('val_start_fixture_daemon "$ROOT" "$PLANE_SOCKET"', daemon)
    return source[start:end] + '\n' + source[daemon:started + len('val_start_fixture_daemon "$ROOT" "$PLANE_SOCKET"')]


def _run(root, socket_dir, env):
    body = '\n'.join([
        'set -euo pipefail', '. "$1/lib-common.sh"',
        'LIB_DIR="$1"; ROOT="$2"; TMUX_TMPDIR="$3"; FLEET=isolated-validation',
        'trap \'for p in ${VAL_PRIVATE_PLANE_PIDS:-}; do kill "$p" 2>/dev/null || true; wait "$p" 2>/dev/null || true; done\' EXIT',
        _setup(),
        '[ "$PLANE_SOCKET" = "$TMUX_TMPDIR/plane.sock" ]',
        'emit_fleet_event validate_started harness \'{}\' ""',
    ])
    return subprocess.run(
        ["/bin/bash", "-c", body, "_", str(REPO / "claudlobby/_runtime_scripts"), str(root), str(socket_dir)],
        env=env, text=True, capture_output=True, timeout=30,
    )


def test_validation_setup_uses_owned_transport_and_preflighted_cli(
    tmp_path, scratch_plane_env, built_test_cli
):
    root = tmp_path / "root"
    root.mkdir()
    socket_dir = scratch_plane_env.socket_dir()
    cli_calls = tmp_path / "cli-calls"
    cli = tmp_path / "recording-cli"
    cli.write_text(
        '#!/bin/bash\n'
        f'printf "called\\n" >> {shlex.quote(str(cli_calls))}\n'
        f'exec {shlex.quote(str(built_test_cli))} "$@"\n'
    )
    cli.chmod(0o755)
    (tmp_path / "python").symlink_to(built_test_cli.parent / "python")
    env = constructed_env(HOME=tmp_path, TMPDIR=tmp_path,
                          **scratch_plane_env(root, socket=socket_dir / "plane.sock", cli=cli))
    native_guard = (REPO / "claudlobby/_runtime_scripts/runtime-admission.sh").read_bytes()
    result = _run(root, socket_dir, env)
    daemon_log = root / "state/plane/fixture-daemon.log"
    assert result.returncode == 0, result.stdout + result.stderr + (
        daemon_log.read_text() if daemon_log.exists() else ""
    )
    assert (REPO / "claudlobby/_runtime_scripts/runtime-admission.sh").read_bytes() == native_guard
    assert (root / "claudlobby/_runtime_scripts/runtime-admission.sh").read_text() == (
        'native_admission() { _NATIVE_ADMISSION_PYTHON="${PLANE_EMIT_CLI%/*}/python"; '
        '[ -x "$_NATIVE_ADMISSION_PYTHON" ]; }\n'
    )
    assert (root / "claudlobby").resolve() == REPO / "claudlobby"
    assert cli_calls.is_file(), "harness ignored the preflighted CLI"
    assert '"type":"validate_started"' in read_fleet_events(root)


def test_validation_setup_refuses_unexecutable_explicit_cli(tmp_path, scratch_plane_env):
    root = tmp_path / "root"
    root.mkdir()
    socket_dir = scratch_plane_env.socket_dir()
    env = constructed_env(HOME=tmp_path, TMPDIR=tmp_path,
                          **scratch_plane_env(root, socket=socket_dir / "plane.sock",
                                              cli=tmp_path / "missing-cli"))
    result = _run(root, socket_dir, env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "executable absolute test CLI path" in result.stderr
    assert not (root / "state/plane/plane.db").exists()
