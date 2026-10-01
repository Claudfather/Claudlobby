"""Fleet prefix and reconcile checks against a private systemd model."""

import os
import shutil
import subprocess

import pytest

from tests.conftest import _write_exec, constructed_env

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(REPO_ROOT, "claudlobby", "_runtime_scripts")

# Copied verbatim into the harness claudlobby/_runtime_scripts/ — these are the scripts under test.
# supervisor.sh rides along as a required sibling: lib-common.sh unconditionally
# sources it from its own directory (#1573 task 6).
REAL_SCRIPTS = [
    "lib-common.sh",
    "cli-context.sh",
    "supervisor.sh",
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

        # This fixture models systemd units and their registry, not launchd.
        # Child scripts source lib-common.sh and detect the OS afresh, so
        # select Linux through their inherited PATH rather than setting _OS.
        _write_exec(self.bin / "uname", '#!/bin/bash\nprintf "Linux\\n"\n')

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
            # still exact-matches, keeping existing callers unchanged. Like
            # systemd (252, measured), a pattern or name that matches nothing
            # exits 1 with no output, which is what fires an unguarded
            # caller's ERR trap (#1707).
            "    list-unit-files)\n"
            "        _rc=1\n"
            "        while IFS= read -r _u; do\n"
            '            [ -n "$_u" ] || continue\n'
            "            # shellcheck disable=SC2254\n"
            '            case "$_u" in ${!#}) echo "$_u enabled enabled"; _rc=0 ;; esac\n'
            '        done < "$STUB_UNIT_FILES" 2>/dev/null\n'
            "        exit $_rc ;;\n"
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

    def run(self, *argv, env_extra=None):
        # Allowlist, never an os.environ copy — a new isolation-sensitive var
        # is absent by construction (#846; doctrine on constructed_env, gate in
        # TestHarnessEnvIsConstructed). Scenario vars arrive via env_extra.
        env = constructed_env(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            HOME=self.home,
            TMPDIR=self.root,
            CLAUDLOBBY_ROOT=self.root,
            CLAUDLOBBY_CLI=self.bin / "claudlobby",
            STUB_LOG=self.log,
            TMUX_HEALTHY=self.tmux_healthy,
            STUB_ENROLLED=self.enrolled,
            STUB_UNIT_FILES=self.unit_files,
            STUB_ACTIVE=self.active,
            STUB_START_FAIL=self.start_fail,
            # Isolate the unbound-session socket walk from the host's real tmux.
            TMUX_TMPDIR=self.root / "no-tmux",
        )
        env.update(env_extra or {})
        return subprocess.run(
            list(argv), capture_output=True, text=True, env=env, timeout=30
        )

    def stub_log(self):
        return self.log.read_text()


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


class TestHarnessEnvIsConstructed:
    """The child env is built from scratch, never copied from os.environ (#846).

    The canary is a NOVEL variable: a pop-list can only ever cover variables
    already known to be dangerous, so the discriminating property is that an
    arbitrary ambient variable — one no denylist mentions — never reaches the
    child. FLEET_STATE_PATH rode exactly that gap from a bot-session env into
    the real fleet-state.json during the #829 validation.
    """

    def test_ambient_variables_do_not_reach_the_child(self, h, monkeypatch):
        monkeypatch.setenv("CLAUDLOBBY_CANARY_846", "escaped")
        monkeypatch.setenv("FLEET_STATE_PATH", "/nonexistent/fleet-state.json")
        r = h.run(
            "/bin/sh",
            "-c",
            'printf \'%s|%s\' "${CLAUDLOBBY_CANARY_846:-}" "${FLEET_STATE_PATH:-}"',
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout == "|"


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


class TestReconcileEnroll:
    """--enroll must dispatch spin-up-bot.sh once per orphan; audit mode none.

    The --enroll consumer fed the space-separated orphan accumulator through a
    line-oriented here-string, so the loop ran once with every orphan glued
    into one value, reported it SKIPPED (no such runtime dir), and never
    called spin-up-bot.sh — the documented repair path for an unsupervised
    bot had never enrolled anything.
    """

    def _two_orphans(self, h):
        h.use_real_reconcile()
        f = h.fleet("f1", bots=("o1", "o2"))
        for b in ("o1", "o2"):
            h.bot(f, b, healthy=True, unit=False)
        return f

    def test_enroll_dispatches_spinup_once_per_orphan(self, h):
        f = self._two_orphans(h)
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1", "--enroll")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKIPPED" not in r.stdout, r.stdout
        calls = [
            ln for ln in h.stub_log().splitlines() if ln.startswith("spin-up-bot.sh ")
        ]
        assert calls == [
            f"spin-up-bot.sh {f}/runtime/bots/o1",
            f"spin-up-bot.sh {f}/runtime/bots/o2",
        ]

    def test_audit_mode_dispatches_nothing(self, h):
        self._two_orphans(h)
        r = h.run(str(h.root / "lib" / "reconcile-fleet.sh"), "f1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "spin-up-bot.sh" not in h.stub_log()
