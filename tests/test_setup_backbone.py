"""Stub-harness tests for the setup backbone (setup-fleet / setup-fleets /
install_fleet_timer.sh env overrides / fleet_service_prefix).

The real scripts run against a throwaway CLAUDLOBBY_ROOT with systemctl, tmux,
spin-up-bot.sh, and reconcile-fleet.sh stubbed (PATH-first binaries or tmp lib
copies), so cold-start prefix resolution, enrollment fan-out, and skip-healthy
behavior are asserted on actual execution — without touching the host's
systemd or tmux state.
"""

import os
import shutil
import stat
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "lib")

# Copied verbatim into the harness lib/ — these are the scripts under test.
REAL_SCRIPTS = [
    "setup-fleet",
    "setup-fleets",
    "lib-common.sh",
    "install_fleet_timer.sh",
]
# Replaced with invocation-logging stubs — their behavior is not under test.
STUB_SCRIPTS = ["spin-up-bot.sh", "reconcile-fleet.sh"]


def _write_exec(path, content):
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class Harness:
    def __init__(self, tmp_path):
        self.root = tmp_path / "root"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "stub.log"
        self.tmux_healthy = tmp_path / "tmux-healthy"
        for d in (self.root / "lib", self.home, self.bin):
            d.mkdir(parents=True)
        self.tmux_healthy.write_text("")
        self.log.write_text("")

        for name in REAL_SCRIPTS:
            shutil.copy2(os.path.join(LIB, name), self.root / "lib" / name)
        for name in STUB_SCRIPTS:
            _write_exec(
                self.root / "lib" / name,
                f'#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\nexit 0\n',
            )
        _write_exec(
            self.bin / "systemctl",
            '#!/bin/bash\necho "systemctl $*" >> "$STUB_LOG"\nexit 0\n',
        )
        # has-session succeeds iff the -t target is listed in $TMUX_HEALTHY.
        _write_exec(
            self.bin / "tmux",
            "#!/bin/bash\n"
            'echo "tmux $*" >> "$STUB_LOG"\n'
            'session=""; prev=""\n'
            'for a in "$@"; do [ "$prev" = "-t" ] && session="$a"; prev="$a"; done\n'
            'grep -qx "$session" "$TMUX_HEALTHY" 2>/dev/null\n',
        )

    def fleet(self, name, service_prefix="test.prefix", bots=(), timers=()):
        fdir = self.root / "local" / name
        (fdir / "runtime" / "bots").mkdir(parents=True)
        lines = ["fleet:", f"  name: {name}", f"  service_prefix: {service_prefix}"]
        lines.append("  bots:")
        lines.extend(f"    {b}:" for b in bots)
        (fdir / "fleet.yaml").write_text("\n".join(lines) + "\n")
        tdir = fdir / "runtime" / "fleet" / "timers"
        tdir.mkdir(parents=True)
        for t in timers:
            base = f"{service_prefix}.{t}"
            (tdir / f"{base}.service").write_text("[Service]\n")
            (tdir / f"{base}.timer").write_text("[Timer]\n")
            (tdir / f"{base}.plist").write_text("<plist/>\n")
        return fdir

    def bot(
        self, fleet_dir, name, service_prefix="test.prefix", healthy=False, unit=True
    ):
        bdir = fleet_dir / "runtime" / "bots" / name
        bdir.mkdir(parents=True)
        svc = f"{service_prefix}.{name}"
        # Unquoted values: bot_conf_get strips double quotes only while
        # extract_bot_conf_var strips singles — bare values satisfy both.
        (bdir / "bot.conf").write_text(
            f"export BOT_NAME={name}\n"
            f"export BOT_SERVICE={svc}\n"
            f"export TMUX_SOCKET={svc}\n"
            f"export SERVICE_PREFIX={service_prefix}\n"
        )
        if unit:
            ud = self.home / ".config" / "systemd" / "user"
            ud.mkdir(parents=True, exist_ok=True)
            (ud / f"{svc}.service").write_text("[Service]\n")
        if healthy:
            with open(self.tmux_healthy, "a") as f:
                f.write(name + "\n")
        return bdir

    def run(self, *argv, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CLAUDLOBBY_ROOT"] = str(self.root)
        env["HOME"] = str(self.home)
        env["STUB_LOG"] = str(self.log)
        env["TMUX_HEALTHY"] = str(self.tmux_healthy)
        for k in (
            "FLEET_NAME",
            "CLAUDLOBBY_FLEET",
            "SERVICE_PREFIX",
            "TIMER_DIR",
            "UNIT_NAME",
            "TMUX_BIN",
        ):
            env.pop(k, None)
        env.update(env_extra or {})
        return subprocess.run(
            list(argv), capture_output=True, text=True, env=env, timeout=30
        )

    def stub_log(self):
        return self.log.read_text()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def _sf(h):
    return str(h.root / "lib" / "setup-fleet")


class TestSetupFleetColdStart:
    def test_enrolls_jobs_with_prefix_from_fleet_yaml(self, h):
        # No bot.conf exists anywhere — the prefix MUST come from fleet.yaml
        # (the bot.conf scan broke fresh hosts: synthesis finding #2).
        h.fleet("f1", bots=(), timers=("fleet-pulse", "keepalive"))
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "service_prefix: test.prefix (from fleet.yaml)" in r.stdout
        log = h.stub_log()
        assert "systemctl --user enable --now test.prefix.fleet-pulse.timer" in log
        assert "systemctl --user enable --now test.prefix.keepalive.timer" in log
        unit_dir = h.home / ".config" / "systemd" / "user"
        assert (unit_dir / "test.prefix.fleet-pulse.timer").is_file()
        assert (unit_dir / "test.prefix.fleet-pulse.service").is_file()

    def test_missing_timers_dir_fails_with_generate_pointer(self, h):
        f = h.fleet("f1", bots=(), timers=())
        shutil.rmtree(f / "runtime" / "fleet")
        r = h.run(_sf(h), "f1")
        assert r.returncode == 1
        assert "claudlobby generate" in r.stdout


class TestSetupFleetSkipHealthy:
    def test_healthy_bot_not_bounced(self, h):
        f = h.fleet("f1", bots=("bota", "botb"), timers=("keepalive",))
        h.bot(f, "bota", healthy=True, unit=True)
        h.bot(f, "botb", healthy=False, unit=True)
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        log = h.stub_log()
        assert "spin-up-bot.sh" in log
        assert "bots/botb" in log
        assert "bots/bota" not in log
        assert "bota: already healthy — skipping (no restart)" in r.stdout

    def test_rerun_with_all_healthy_never_restarts(self, h):
        f = h.fleet("f1", bots=("bota",), timers=())
        h.bot(f, "bota", healthy=True, unit=True)
        r1 = h.run(_sf(h), "f1")
        r2 = h.run(_sf(h), "f1")
        assert r1.returncode == 0, r1.stdout + r1.stderr
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "spin-up-bot.sh" not in h.stub_log()

    def test_session_dead_unit_present_is_respun(self, h):
        # The classic keepalive case: unit file present but tmux session gone
        # → NOT healthy → spin-up (which restarts) is the correct repair.
        f = h.fleet("f1", bots=("bota",), timers=())
        h.bot(f, "bota", healthy=False, unit=True)
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "spin-up-bot.sh" in h.stub_log()


class TestSetupFleets:
    def test_loops_all_fleets(self, h):
        h.fleet("f1", timers=("keepalive",))
        h.fleet("f2", timers=("keepalive",))
        r = h.run(str(h.root / "lib" / "setup-fleets"))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "=== setup-fleet f1 ===" in r.stdout
        assert "=== setup-fleet f2 ===" in r.stdout
        assert "all fleets applied" in r.stdout

    def test_no_fleets_is_an_error(self, h):
        r = h.run(str(h.root / "lib" / "setup-fleets"))
        assert r.returncode == 1
        assert "no fleets found" in r.stderr


class TestInstallFleetTimerEnvOverrides:
    def test_timer_dir_and_unit_name_bypass_fleet_resolution(self, h):
        # Host-job mode (setup-system's phase 8): no fleet arg, no bot.conf —
        # TIMER_DIR + UNIT_NAME fully determine the enrollment.
        hostdir = h.root / "runtime" / "_host" / "timers"
        hostdir.mkdir(parents=True)
        (hostdir / "claudlobby-claude-update.service").write_text("[Service]\n")
        (hostdir / "claudlobby-claude-update.timer").write_text("[Timer]\n")
        r = h.run(
            str(h.root / "lib" / "install_fleet_timer.sh"),
            "claude-update",
            env_extra={
                "TIMER_DIR": str(hostdir),
                "UNIT_NAME": "claudlobby-claude-update",
            },
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "enable --now claudlobby-claude-update.timer" in h.stub_log()
        unit_dir = h.home / ".config" / "systemd" / "user"
        assert (unit_dir / "claudlobby-claude-update.timer").is_file()

    def test_fleet_arg_path_unchanged_without_overrides(self, h):
        f = h.fleet("f1", bots=("bota",), timers=("fleet-pulse",))
        h.bot(f, "bota")
        r = h.run(str(h.root / "lib" / "install_fleet_timer.sh"), "fleet-pulse", "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "enable --now test.prefix.fleet-pulse.timer" in h.stub_log()


class TestFleetServicePrefixHelper:
    def _prefix(self, h, yaml_text):
        fy = h.root / "x.yaml"
        fy.write_text(yaml_text)
        r = h.run(
            "bash",
            "-c",
            f'. "{h.root}/lib/lib-common.sh"; fleet_service_prefix "{fy}"',
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_plain_value(self, h):
        assert self._prefix(h, "fleet:\n  service_prefix: com.x.y\n") == "com.x.y"

    def test_quoted_value_with_trailing_comment(self, h):
        yaml = 'fleet:\n  service_prefix: "com.x.y"  # note\n'
        assert self._prefix(h, yaml) == "com.x.y"

    def test_default_when_missing(self, h):
        assert self._prefix(h, "fleet:\n  name: z\n") == "claudlobby"
