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
import subprocess

import pytest

from tests.conftest import _write_exec

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
        self.enrolled = tmp_path / "enrolled"
        self.enrolled.write_text("")
        self.unit_files = tmp_path / "unit-files"
        self.unit_files.write_text("")
        self.active = tmp_path / "active"
        self.active.write_text("")
        self.start_fail = tmp_path / "start-fail"
        self.start_fail.write_text("")
        # is-enabled consults $STUB_ENROLLED (drift-audit tests); list-unit-files
        # / is-active / start consult their own state files, and disable removes
        # the unit from the registry like real systemd (keepalive-swap tests);
        # other verbs succeed unconditionally. The unit is always the last arg.
        _write_exec(
            self.bin / "systemctl",
            "#!/bin/bash\n"
            'echo "systemctl $*" >> "$STUB_LOG"\n'
            'case "${2:-}" in\n'
            "    is-enabled)\n"
            '        grep -qx "${3:-}" "$STUB_ENROLLED" 2>/dev/null; exit $? ;;\n'
            # Emulate systemd glob expansion for list-unit-files: the last arg
            # may be a pattern (e.g. <prefix>.briefing-*.timer); a literal name
            # still exact-matches, keeping existing callers unchanged.
            "    list-unit-files)\n"
            "        while IFS= read -r _u; do\n"
            '            [ -n "$_u" ] || continue\n'
            "            # shellcheck disable=SC2254\n"
            '            case "$_u" in ${!#}) echo "$_u enabled enabled" ;; esac\n'
            '        done < "$STUB_UNIT_FILES" 2>/dev/null\n'
            "        exit 0 ;;\n"
            "    is-active)\n"
            '        grep -qx "${!#}" "$STUB_ACTIVE" 2>/dev/null && exit 0\n'
            "        exit 3 ;;\n"
            "    start)\n"
            '        grep -qx "${!#}" "$STUB_START_FAIL" 2>/dev/null && exit 1\n'
            "        exit 0 ;;\n"
            "    disable)\n"
            '        grep -vx "${!#}" "$STUB_UNIT_FILES" > "$STUB_UNIT_FILES.tmp" 2>/dev/null || true\n'
            '        mv "$STUB_UNIT_FILES.tmp" "$STUB_UNIT_FILES"\n'
            "        exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
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
        # reconcile-fleet's missing-bot diagnostics shell out to journalctl.
        # Unstubbed it reads the HOST journal — unbounded, and a hole in this
        # harness's no-host-state contract.
        _write_exec(
            self.bin / "journalctl",
            '#!/bin/bash\necho "journalctl $*" >> "$STUB_LOG"\nexit 0\n',
        )
        # claudlobby: logs its invocation so warm-cache ordering is assertable.
        _write_exec(
            self.bin / "claudlobby",
            '#!/bin/bash\necho "claudlobby $*" >> "$STUB_LOG"\nexit 0\n',
        )

    def _populate(self, fdir, name, service_prefix, bots, timers, dormant):
        (fdir / "runtime" / "bots").mkdir(parents=True, exist_ok=True)
        lines = ["fleet:", f"  name: {name}", f"  service_prefix: {service_prefix}"]
        lines.append("  bots:")
        lines.extend(f"    {b}:" for b in bots)
        (fdir / "fleet.yaml").write_text("\n".join(lines) + "\n")
        tdir = fdir / "runtime" / "fleet" / "timers"
        tdir.mkdir(parents=True, exist_ok=True)
        for t in timers:
            base = f"{service_prefix}.{t}"
            (tdir / f"{base}.service").write_text("[Service]\n")
            (tdir / f"{base}.timer").write_text("[Timer]\n")
            (tdir / f"{base}.plist").write_text("<plist/>\n")
        manifest = ["# dormant units"] + [f"{service_prefix}.{d}" for d in dormant]
        (tdir / "DORMANT").write_text("\n".join(manifest) + "\n")
        return fdir

    def fleet(self, name, service_prefix="test.prefix", bots=(), timers=(), dormant=()):
        return self._populate(
            self.root / "local" / name, name, service_prefix, bots, timers, dormant
        )

    def root_fleet(self, service_prefix="root.prefix", bots=(), timers=(), dormant=()):
        # Root mode: fleet.yaml + runtime/ live at CLAUDLOBBY_ROOT (seed flow).
        return self._populate(self.root, "seed", service_prefix, bots, timers, dormant)

    def use_real_reconcile(self):
        shutil.copy2(
            os.path.join(LIB, "reconcile-fleet.sh"),
            self.root / "lib" / "reconcile-fleet.sh",
        )

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

    @property
    def unit_dir(self):
        ud = self.home / ".config" / "systemd" / "user"
        ud.mkdir(parents=True, exist_ok=True)
        return ud

    def legacy_keepalive(self, fleet_name, active=True):
        # A prior release's keepalive: claudlobby-<fleet>-keepalive unit files
        # in ~/.config/systemd/user + registered in the stub's unit registry.
        base = f"claudlobby-{fleet_name}-keepalive"
        (self.unit_dir / f"{base}.service").write_text("[Service]\n")
        (self.unit_dir / f"{base}.timer").write_text("[Timer]\n")
        with open(self.unit_files, "a") as f:
            f.write(f"{base}.timer\n")
        if active:
            self.mark_active(f"{base}.timer")
        return base

    def mark_active(self, unit):
        with open(self.active, "a") as f:
            f.write(unit + "\n")

    def run(self, *argv, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["CLAUDLOBBY_ROOT"] = str(self.root)
        env["HOME"] = str(self.home)
        env["STUB_LOG"] = str(self.log)
        env["TMUX_HEALTHY"] = str(self.tmux_healthy)
        env["STUB_ENROLLED"] = str(self.enrolled)
        env["STUB_UNIT_FILES"] = str(self.unit_files)
        env["STUB_ACTIVE"] = str(self.active)
        env["STUB_START_FAIL"] = str(self.start_fail)
        # Isolate the unbound-session socket walk from the host's real tmux.
        env["TMUX_TMPDIR"] = str(self.root / "no-tmux")
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

    def test_warms_npx_cache_before_spinning_up_bots(self, h):
        # WS-2: warm the fleet's npx cache (step 2/4) BEFORE bots spin up (step
        # 3/4), so a cold-cache fleet boot doesn't dogpile npm-install and leave
        # MCP servers dark on reboot. Non-fatal, so it never blocks spin-up.
        f = h.fleet("f1", bots=("b1",), timers=("fleet-pulse",))
        h.bot(f, "b1", healthy=False)  # unhealthy → spin_up_bots will spin it up
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "step 2/4: warm npx cache" in r.stdout
        log = h.stub_log()
        # warm-cache invoked for the fleet, and BEFORE the bot spun up.
        assert "claudlobby --fleet f1 warm-cache" in log
        assert log.index("warm-cache") < log.index("spin-up-bot.sh")

    def test_skips_warm_when_all_bots_healthy(self, h):
        # Skip-when-healthy discipline: if every bot is already running (its MCP
        # servers are live = cache warm), don't re-spawn npx warms on a re-apply.
        f = h.fleet("f1", bots=("b1",), timers=("fleet-pulse",))
        h.bot(f, "b1", healthy=True)  # already healthy → no spin-up, no warm
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "all bots healthy, cache already warm (skip)" in r.stdout
        assert "warm-cache" not in h.stub_log()  # warm-cache NOT invoked

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


class TestRootMode:
    def test_no_arg_enrolls_from_root_fleet_yaml(self, h):
        # The seed flow (setup skill Step 5): fleet.yaml at CLAUDLOBBY_ROOT,
        # no overlay, no bot.conf anywhere — prefix from the root fleet.yaml.
        h.root_fleet(bots=(), timers=("keepalive", "fleet-pulse"))
        r = h.run(_sf(h))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "service_prefix: root.prefix (from fleet.yaml)" in r.stdout
        log = h.stub_log()
        assert "systemctl --user enable --now root.prefix.keepalive.timer" in log
        assert "systemctl --user enable --now root.prefix.fleet-pulse.timer" in log
        # reconcile-fleet is overlay-only: skipped with a log line, not run.
        assert "reconcile audit skipped" in r.stdout
        assert "reconcile-fleet.sh" not in log

    def test_root_mode_skips_healthy_bot(self, h):
        h.root_fleet(bots=("claudfather",), timers=())
        h.bot(h.root, "claudfather", service_prefix="root.prefix", healthy=True)
        r = h.run(_sf(h))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "claudfather: already healthy — skipping (no restart)" in r.stdout
        assert "spin-up-bot.sh" not in h.stub_log()

    def test_no_root_fleet_yaml_is_an_error(self, h):
        r = h.run(_sf(h))
        assert r.returncode == 1
        assert "fleet.yaml not found" in r.stderr


class TestDormantGate:
    def test_dormant_units_composed_but_not_enrolled(self, h):
        # F4: enroll:false jobs are composed-but-dormant — the unit files
        # exist, the DORMANT manifest lists them, setup-fleet skips them.
        h.fleet(
            "f1",
            timers=("keepalive", "weekly-worker-restart"),
            dormant=("weekly-worker-restart",),
        )
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        log = h.stub_log()
        assert "enable --now test.prefix.keepalive.timer" in log
        assert "test.prefix.weekly-worker-restart" not in log
        assert "dormant (opt-in): test.prefix.weekly-worker-restart" in r.stdout
        assert "defaults.jobs.weekly-worker-restart.enroll: true" in r.stdout

    def test_opted_in_fleet_enrolls_it(self, h):
        # A fleet that opted in composes WITHOUT the manifest entry — the
        # unit enrolls like any default job.
        h.fleet("f1", timers=("keepalive", "weekly-worker-restart"), dormant=())
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "enable --now test.prefix.weekly-worker-restart.timer" in h.stub_log()


class TestReconcileJobDrift:
    def test_drift_flags_unenrolled_but_skips_dormant(self, h):
        # Real reconcile-fleet.sh: keepalive composed but NOT enrolled →
        # drift; weekly-worker-restart composed + dormant → NOT drift.
        h.use_real_reconcile()
        h.fleet(
            "f1",
            timers=("keepalive", "weekly-worker-restart"),
            dormant=("weekly-worker-restart",),
        )
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        drift_line = next(line for line in r.stdout.splitlines() if "job-drift" in line)
        assert "test.prefix.keepalive" in drift_line
        assert "weekly-worker-restart" not in drift_line

    def test_enrolled_units_are_not_drift(self, h):
        h.use_real_reconcile()
        h.fleet("f1", timers=("keepalive",), dormant=())
        h.enrolled.write_text("test.prefix.keepalive.timer\n")
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        drift_line = next(line for line in r.stdout.splitlines() if "job-drift" in line)
        assert "(none)" in drift_line


class TestReconcileBuckets:
    """Every bot declared in fleet.yaml must land in exactly one bucket.

    The classifier had three arms and no else, so the fourth cell of the
    tmux x unit matrix — no session AND no unit — matched nothing and was
    reported nowhere: present in the loop, absent from the output.
    """

    # Bucket a bot declared in fleet.yaml can land in -> its cell of the
    # matrix. unbound holds sessions matching no fleet.yaml, and job-drift
    # holds timer units, so neither can hold a declared bot.
    MATRIX = {
        "healthy": dict(healthy=True, unit=True),
        "orphan": dict(healthy=True, unit=False),
        "missing": dict(healthy=False, unit=True),
        "unsupervised-down": dict(healthy=False, unit=False),
    }

    def _bucket(self, stdout, label):
        # "  <glyph> <label>: a b c   <- hint" -> ["a", "b", "c"]. Keyed on the
        # full "<label>:" so the diagnostics block printed below the report
        # (also indented, also colon-bearing) can never match.
        line = next(ln for ln in stdout.splitlines() if f"{label}:" in ln)
        names = line.split(f"{label}:", 1)[1].split("←")[0].split()
        return [] if names == ["(none)"] else names

    def test_each_matrix_cell_lands_in_its_own_bucket(self, h):
        h.use_real_reconcile()
        f = h.fleet("f1", bots=tuple(self.MATRIX))
        for bucket, state in self.MATRIX.items():
            h.bot(f, bucket, **state)
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        # Each bot is named for the bucket it belongs in, so this asserts both
        # correct placement and — because every declared bot must appear —
        # that no cell falls through to no bucket at all.
        assert {b: self._bucket(r.stdout, b) for b in self.MATRIX} == {
            b: [b] for b in self.MATRIX
        }, f"a defined bot was reported in the wrong bucket, or in none: {r.stdout}"

    def test_unsupervised_down_reads_none_when_empty(self, h):
        # The bucket must print unconditionally — an operator scanning the
        # report needs "(none)" to mean checked-and-clear, not omitted.
        h.use_real_reconcile()
        f = h.fleet("f1", bots=("hb",))
        h.bot(f, "hb", healthy=True, unit=True)
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert self._bucket(r.stdout, "unsupervised-down") == []


class TestLegacyKeepaliveSwap:
    """Phase 6 (F6=a): setup-fleet retires a prior release's
    claudlobby-<fleet>-keepalive AFTER the composed <prefix>.keepalive is
    enrolled, active, and has completed a verification run — never before.
    Abort paths must leave the legacy unit fully untouched."""

    def _swap_fleet(self, h, new_active=True):
        h.fleet("f1", timers=("keepalive",))
        legacy = h.legacy_keepalive("f1")
        if new_active:
            h.mark_active("test.prefix.keepalive.timer")
        return legacy

    def _log_index(self, h, needle):
        log = h.stub_log()
        assert needle in log, f"missing in stub log: {needle}\n{log}"
        return log.index(needle)

    def _assert_swap_aborted_legacy_intact(self, h, r, legacy):
        assert r.returncode == 1
        assert "swap ABORTED" in r.stdout
        assert f"disable --now {legacy}.timer" not in h.stub_log()
        assert (h.unit_dir / f"{legacy}.timer").is_file()
        assert (h.unit_dir / f"{legacy}.service").is_file()

    def test_swap_orders_enable_verify_disable(self, h):
        legacy = self._swap_fleet(h)
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        enable_new = self._log_index(
            h, "systemctl --user enable --now test.prefix.keepalive.timer"
        )
        verify_run = self._log_index(
            h, "systemctl --user start test.prefix.keepalive.service"
        )
        disable_old = self._log_index(
            h, f"systemctl --user disable --now {legacy}.timer"
        )
        # The atomic ordering contract: new live + verified BEFORE old retired.
        assert enable_new < verify_run < disable_old
        assert not (h.unit_dir / f"{legacy}.timer").exists()
        assert not (h.unit_dir / f"{legacy}.service").exists()
        assert "legacy keepalive retired" in r.stdout

    def test_no_legacy_is_noop(self, h):
        h.fleet("f1", timers=("keepalive",))
        h.mark_active("test.prefix.keepalive.timer")
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "disable --now" not in h.stub_log()
        assert "legacy keepalive" not in r.stdout

    def test_new_timer_not_active_aborts_keeps_legacy(self, h):
        legacy = self._swap_fleet(h, new_active=False)
        r = h.run(_sf(h), "f1")
        self._assert_swap_aborted_legacy_intact(h, r, legacy)

    def test_verification_run_failure_aborts_keeps_legacy(self, h):
        legacy = self._swap_fleet(h)
        h.start_fail.write_text("test.prefix.keepalive.service\n")
        r = h.run(_sf(h), "f1")
        self._assert_swap_aborted_legacy_intact(h, r, legacy)

    def test_swap_is_idempotent_after_success(self, h):
        # Run 2's registry state is DERIVED: the stub's disable verb removes
        # the unit line, mirroring real systemd after disable + daemon-reload.
        self._swap_fleet(h)
        r1 = h.run(_sf(h), "f1")
        assert r1.returncode == 0, r1.stdout + r1.stderr
        h.log.write_text("")
        r2 = h.run(_sf(h), "f1")
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "disable --now" not in h.stub_log()
        assert "legacy keepalive" not in r2.stdout

    def test_root_mode_skips_swap(self, h):
        # Root mode has no fleet name, so the legacy naming scheme cannot
        # apply — the swap must not even probe.
        h.root_fleet(timers=("keepalive",))
        h.mark_active("root.prefix.keepalive.timer")
        r = h.run(_sf(h))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "list-unit-files" not in h.stub_log()


class TestBriefingReconcileGuard:
    """setup-fleet's reconcile_briefing_timers: a partial composed timers dir (an
    interrupted/torn generate) must never disable a live briefing timer. The
    guard reads the config-truth BRIEFING_EXPECTED manifest (DORMANT precedent)
    for an independent expected count — a shortfall aborts, not just a fully
    empty set. Runs the real setup-fleet end-to-end with systemctl stubbed."""

    _FOUR = [
        "briefing-kev-morning",
        "briefing-kev-evening",
        "briefing-ari-morning",
        "briefing-ari-evening",
    ]

    def _seed(self, h, fdir, *, on_disk, expected, live):
        tdir = fdir / "runtime" / "fleet" / "timers"
        tdir.mkdir(parents=True, exist_ok=True)
        for u in on_disk:
            base = f"test.prefix.{u}"
            (tdir / f"{base}.service").write_text("[Service]\n")
            (tdir / f"{base}.timer").write_text("[Timer]\n")
            (tdir / f"{base}.plist").write_text("<plist/>\n")
        if expected is not None:  # None simulates a pre-feature dir (no manifest)
            lines = ["# config-declared briefing units"]
            lines += [f"test.prefix.{u}" for u in expected]
            (tdir / "BRIEFING_EXPECTED").write_text("\n".join(lines) + "\n")
        with open(h.unit_files, "a") as f:
            for u in live:
                f.write(f"test.prefix.{u}.timer\n")

    def test_partial_composed_never_disables_live_timer(self, h):
        # 3-of-4 on disk (ari-evening dropped by a torn generate) while config
        # declares all 4 and all 4 are live — the shortfall must abort, and the
        # dropped live timer must survive untouched.
        fdir = h.fleet("f1", bots=("kev",))
        h.bot(fdir, "kev", healthy=True)
        self._seed(
            h, fdir, on_disk=self._FOUR[:3], expected=self._FOUR, live=self._FOUR
        )
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert (
            "disable --now test.prefix.briefing-ari-evening.timer" not in h.stub_log()
        )
        assert "briefing reconcile ABORTED" in r.stdout

    def test_clean_compose_still_disables_real_orphan(self, h):
        # All 4 declared compose cleanly (== expected); a 5th stale live unit is a
        # genuine config-removed orphan and IS disabled — the guard must not
        # over-abort and strand real orphans.
        fdir = h.fleet("f1", bots=("kev",))
        h.bot(fdir, "kev", healthy=True)
        self._seed(
            h,
            fdir,
            on_disk=self._FOUR,
            expected=self._FOUR,
            live=self._FOUR + ["briefing-ari-noon"],
        )
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "disable --now test.prefix.briefing-ari-noon.timer" in h.stub_log()
        assert "briefing reconcile ABORTED" not in r.stdout

    def test_confirmed_full_removal_proceeds(self, h):
        # Briefing fully removed from config: the manifest confirms zero expected,
        # so a still-live unit is a legitimate orphan and the teardown proceeds —
        # composed 0 is not a shortfall when 0 is also declared.
        fdir = h.fleet("f1", bots=("kev",))
        h.bot(fdir, "kev", healthy=True)
        self._seed(h, fdir, on_disk=[], expected=[], live=["briefing-kev-morning"])
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "disable --now test.prefix.briefing-kev-morning.timer" in h.stub_log()

    def test_missing_manifest_refuses_to_disable(self, h):
        # No BRIEFING_EXPECTED at all (a pre-feature timers dir a later generate
        # self-heals): the declared set is unproven, so the reconciler must NOT
        # disable — no backwards-compat empty-only shim that could act on a torn
        # dir it cannot verify.
        fdir = h.fleet("f1", bots=("kev",))
        h.bot(fdir, "kev", healthy=True)
        self._seed(h, fdir, on_disk=self._FOUR[:3], expected=None, live=self._FOUR)
        r = h.run(_sf(h), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "disable --now test.prefix.briefing-" not in h.stub_log()
        assert "briefing reconcile ABORTED" in r.stdout
