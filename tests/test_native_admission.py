"""Guard placement and refusal use real entry scripts; no lifecycle is reached."""

from pathlib import Path
import shlex
import subprocess


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
        result = subprocess.run(["/bin/bash", str(ROOT / "lib" / name), str(bot)],
                                env=env, text=True, capture_output=True, timeout=5)
        assert result.returncode == 7, (result.stdout, result.stderr)
        assert "selected release interpreter/CLI unavailable" in result.stderr
        assert {str(path.relative_to(tmp_path)): path.read_bytes()
                for path in tmp_path.rglob("*") if path.is_file()} == before
        assert list(scratch.iterdir()) == []
        assert not (root / "state").exists()


def test_guard_is_only_at_native_start_and_watchdog_boundary():
    for name, operation in (("start-bot.sh", "start-bot"), ("keepalive.sh", "keepalive")):
        source = (ROOT / "lib" / name).read_text()
        assert source.index('load_bot_conf "$BOT_DIR"') < source.index(f"native_admission {operation}")
        assert source.index(f"native_admission {operation}") < source.index('install_error_trap "$BOT_DIR"')
    for name in ("lib-common.sh", "spin-down-bot.sh", "pre-stop-handoff.sh"):
        assert "runtime-admission.sh" not in (ROOT / "lib" / name).read_text()
