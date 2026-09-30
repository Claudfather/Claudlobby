"""Guard placement and refusal use real entry scripts; no lifecycle is reached."""

from pathlib import Path
import fcntl
import os
import shlex
import signal
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_native_entrypoints_refuse_before_runtime_effects(tmp_path):
    home, scratch, root = (tmp_path / name for name in ("home", "tmp", "data"))
    for path in (home, scratch, root):
        path.mkdir()
    bot = root / "runtime/bots/worker"
    bot.mkdir(parents=True)
    conf = {"CLAUDLOBBY_ROOT": str(root), "CLAUDLOBBY_CLI": str(tmp_path / "missing/claudlobby"),
            "CLAUDLOBBY_RELEASE_ID": "r-stale", "CLAUDLOBBY_ARTIFACT_ID": "stale",
            "BOT_SERVICE": "isolated-worker", "BOT_NAME": "worker"}
    (bot / "bot.conf").write_text("".join(f"{key}={shlex.quote(value)}\n" for key, value in conf.items()))
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(scratch),
           "PLANE_EMIT_DISABLED": "1"}
    for name in ("start-bot.sh", "keepalive.sh"):
        result = subprocess.run(["/bin/bash", str(ROOT / "claudlobby/_runtime_scripts" / name), str(bot)],
                                env=env, text=True, capture_output=True, timeout=5)
        assert result.returncode == 7, (result.stdout, result.stderr)
        assert "selected release interpreter/CLI unavailable" in result.stderr
        assert {str(path.relative_to(tmp_path)): path.read_bytes()
                for path in tmp_path.rglob("*") if path.is_file()} == before
        assert list(scratch.iterdir()) == []
        assert not (root / "state").exists()


def test_guard_is_only_at_native_start_and_watchdog_boundary():
    for name, operation in (("start-bot.sh", "start-bot"), ("keepalive.sh", "keepalive")):
        source = (ROOT / "claudlobby/_runtime_scripts" / name).read_text()
        assert source.index('load_bot_conf "$BOT_DIR"') < source.index(f"native_admission {operation}")
        assert source.index(f"native_admission {operation}") < source.index('install_error_trap "$BOT_DIR"')
    for name in ("lib-common.sh", "spin-down-bot.sh", "pre-stop-handoff.sh"):
        assert "runtime-admission.sh" not in (ROOT / "claudlobby/_runtime_scripts" / name).read_text()


def test_shell_guard_preserves_temporary_pause_exit(tmp_path):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    (root / "state/activation.lock").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("python", "claudlobby"):
        tool = bindir / name
        tool.write_text("#!/bin/sh\nexit 75\n" if name == "python" else "#!/bin/sh\n")
        tool.chmod(0o755)
    script = (f'CLAUDLOBBY_ROOT={shlex.quote(str(root))}\n'
              f'BOT_DIR={shlex.quote(str(root / "bot"))}\n'
              f'CLAUDLOBBY_CLI={shlex.quote(str(bindir / "claudlobby"))}\n'
              f'LIB_DIR={shlex.quote(str(ROOT / "claudlobby/_runtime_scripts"))}\n'
              'CLAUDLOBBY_RELEASE_ID=r-test\nCLAUDLOBBY_ARTIFACT_ID=a-test\n'
              f'. {shlex.quote(str(ROOT / "claudlobby/_runtime_scripts/runtime-admission.sh"))}\n'
              'native_admission keepalive\n')
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 75


def test_private_tmux_child_cannot_retain_native_activation_descriptor(tmp_path):
    """A reaped starter cannot leave its admission lease in a surviving server."""
    lock = tmp_path / "activation.lock"
    lock.write_text("")
    child_pid = tmp_path / "server.pid"
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text("#!/bin/bash\n"
                         "sleep 5 >/dev/null 2>&1 &\n"
                         f"echo $! > {shlex.quote(str(child_pid))}\n")
    fake_tmux.chmod(0o755)
    script = f"""
set -eu
TMUX_BIN={shlex.quote(str(fake_tmux))}
. {shlex.quote(str(ROOT / 'claudlobby/_runtime_scripts/lib-common.sh'))}
exec 9<{shlex.quote(str(lock))}
{shlex.quote(sys.executable)} -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_SH)'
bot_tmux private new-session
echo ready
read -r finish
"""
    process = subprocess.Popen(["/bin/bash", "-c", script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    server = None
    try:
        assert process.stdout.readline().strip() == "ready"
        server = int(child_pid.read_text())
        os.kill(server, 0)  # The detached server is still alive.
        os.kill(process.pid, signal.SIGKILL)  # EXIT cleanup cannot run.
        process.wait(timeout=5)
        os.kill(server, 0)
        with lock.open("rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if server is not None:
            try:
                os.kill(server, signal.SIGTERM)
            except ProcessLookupError:
                pass
