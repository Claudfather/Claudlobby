"""Behavioral tests for the Phase 7 value-completeness maintenance jobs:
data-sweep (per-fleet weekly purge), disk-monitor + fleet-memory-check
(daily host jobs alerting via the fleet signal path), and reload-fleet's
npx-cache preflight fold.

Real scripts run against throwaway CLAUDLOBBY_ROOTs; tg-post.sh is stubbed
to capture signal delivery (the notify-behind harness pattern)."""

import os
import subprocess
import time

from tests.conftest import TG_STUB, _scrubbed_env, _write_exec, read_fleet_events

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib")

def _signal_root(tmp_path, bots_at="runtime/bots"):
    """Throwaway CLAUDLOBBY_ROOT with a tg-post stub + a chat-declaring bot,
    so emit_failure_alert's Telegram leg is observable."""
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    _write_exec(str(root / "lib" / "tg-post.sh"), TG_STUB)
    bot = root / bots_at / "tbot"
    bot.mkdir(parents=True)
    (bot / "bot.conf").write_text('export TELEGRAM_GROUP_CHAT_ID="-100123"\n')
    return root


def _run(script, args, root, tmp_path, extra_env=None):
    env = _scrubbed_env(
        CLAUDLOBBY_ROOT=str(root), TG_CAPTURE=str(tmp_path / "tg-capture")
    )
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", os.path.join(LIB, script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def _captured(tmp_path):
    cap = tmp_path / "tg-capture"
    return cap.read_text() if cap.exists() else ""


_events = read_fleet_events


class TestDataSweep:
    def _fleet_data(self, root):
        data = root / "local" / "f7" / "runtime" / "bots" / "b1" / "data"
        data.mkdir(parents=True)
        old = data / "old.jsonl"
        old.write_text("stale\n")
        stale_mtime = time.time() - 40 * 86400
        os.utime(old, (stale_mtime, stale_mtime))
        fresh = data / "fresh.jsonl"
        fresh.write_text("current\n")
        return data

    def test_composed_invocation_purges_old_keeps_fresh(self, tmp_path):
        # The composed unit runs `data-sweep.sh --purge <fleet>` — flags
        # first, positional fleet name appended by the composer.
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["--purge", "f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert not (data / "old.jsonl").exists()
        assert (data / "fresh.jsonl").exists()

    def test_report_only_deletes_nothing(self, tmp_path):
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert (data / "old.jsonl").exists()

    def test_days_override_spares_younger_files(self, tmp_path):
        # Retention is fleet-overridable via the job's script line — prove
        # the flag the override carries actually widens the window.
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["--purge", "--days", "60", "f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert (data / "old.jsonl").exists()

    def test_unknown_flag_still_rejected(self, tmp_path):
        root = tmp_path / "root"
        self._fleet_data(root)
        r = _run("data-sweep.sh", ["--bogus"], root, tmp_path)
        assert r.returncode == 2


class TestDiskMonitor:
    def test_high_usage_raises_disk_high_signal(self, tmp_path):
        # --threshold 1 makes any real disk exceed it deterministically.
        root = _signal_root(tmp_path)
        r = _run("disk-monitor.sh", ["--threshold", "1"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert '"type":"disk_high"' in _events(root)
        cap = _captured(tmp_path)
        assert "FLEET ALERT [disk_high]" in cap
        assert "disk usage" in cap

    def test_ok_usage_is_silent(self, tmp_path):
        root = _signal_root(tmp_path)
        r = _run("disk-monitor.sh", ["--threshold", "100"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert _captured(tmp_path) == ""
        assert "disk_high" not in _events(root)

    def test_fleetless_reports_bot_data_sizes_across_fleets(self, tmp_path):
        # Host jobs run fleet-less; the sizes report must still find bots
        # under local/*/runtime/bots.
        root = _signal_root(tmp_path, bots_at="local/eng/runtime/bots")
        data = root / "local" / "eng" / "runtime" / "bots" / "tbot" / "data"
        data.mkdir(parents=True)
        (data / "x").write_text("x\n")
        r = _run("disk-monitor.sh", ["--threshold", "100"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        log = (root / "lib" / "disk-monitor.log").read_text()
        assert "tbot/data:" in log


class TestFleetMemoryCheck:
    def test_pressure_raises_memory_high_signal(self, tmp_path):
        # --threshold 1 → reserve floor 99% of RAM → any real host is
        # "below reserve" deterministically.
        root = _signal_root(tmp_path)
        r = _run("fleet-memory-check.sh", ["--threshold", "1"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert '"type":"memory_high"' in _events(root)
        assert "FLEET ALERT [memory_high]" in _captured(tmp_path)

    def test_ok_is_silent_and_exits_zero(self, tmp_path):
        root = _signal_root(tmp_path)
        r = _run("fleet-memory-check.sh", ["--threshold", "99"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "memory_high" not in _events(root)


class TestReloadFleetNpxPreflight:
    """check-npx-cache runs BEFORE plugin updates, inside the reload lock; a
    degraded cache warms best-effort (once per episode) and never aborts the
    reload."""

    def _harness(self, tmp_path, npx_rc):
        root = tmp_path / "root"
        libdir = root / "lib"
        libdir.mkdir(parents=True)
        for script in ("reload-fleet.sh", "lib-common.sh"):
            with open(os.path.join(LIB, script)) as f:
                content = f.read()
            _write_exec(str(libdir / script), content)
        _write_exec(
            str(libdir / "check-npx-cache.sh"),
            f'#!/bin/bash\necho "check-npx-cache $*" >> "$CALL_LOG"\nexit {npx_rc}\n',
        )
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for tool in ("claude", "claudlobby"):
            _write_exec(
                str(bindir / tool),
                f'#!/bin/bash\necho "{tool} $*" >> "$CALL_LOG"\nexit 0\n',
            )
        # A bot declaring plugins so the plugin-update leg actually runs.
        bot = root / "runtime" / "bots" / "tbot"
        bot.mkdir(parents=True)
        (bot / "bot.conf").write_text(
            'export FLEET_PLUGINS_REQUIRED="somepkg@Somewhere"\n'
        )
        env = _scrubbed_env(
            CLAUDLOBBY_ROOT=str(root),
            CALL_LOG=str(tmp_path / "calls.log"),
            PATH=f"{bindir}:{os.environ['PATH']}",
            TMUX_TMPDIR=str(tmp_path / "no-tmux"),
        )
        return root, env

    def _run_reload(self, root, env):
        r = subprocess.run(
            ["bash", str(root / "lib" / "reload-fleet.sh")],
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr + r.stdout

    def _calls(self, tmp_path):
        log = tmp_path / "calls.log"
        return log.read_text() if log.exists() else ""

    def test_preflight_runs_before_plugin_update(self, tmp_path):
        root, env = self._harness(tmp_path, npx_rc=0)
        self._run_reload(root, env)
        calls = self._calls(tmp_path)
        assert calls.index("check-npx-cache") < calls.index("claude plugin update")
        assert "warm-cache" not in calls

    def test_degraded_cache_warms_and_reload_continues(self, tmp_path):
        root, env = self._harness(tmp_path, npx_rc=1)
        self._run_reload(root, env)
        calls = self._calls(tmp_path)
        assert "warm-cache" in calls
        assert calls.index("check-npx-cache") < calls.index("warm-cache")
        assert calls.index("warm-cache") < calls.index("claude plugin update")

    def test_warm_is_debounced_within_a_degradation_episode(self, tmp_path):
        # A permanently-missing package (e.g. a stale MCP fragment) must not
        # become a daily warm loop: one warm attempt per episode, re-armed
        # only after the check passes again.
        root, env = self._harness(tmp_path, npx_rc=1)
        self._run_reload(root, env)
        self._run_reload(root, env)
        assert self._calls(tmp_path).count("warm-cache") == 1
