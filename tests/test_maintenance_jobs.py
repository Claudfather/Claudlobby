"""Behavioral tests for the Phase 7 value-completeness maintenance jobs:
data-sweep (per-fleet weekly purge), disk-monitor + fleet-memory-check
(daily host jobs alerting via the fleet signal path), and reload-fleet's
npx-cache preflight fold.

Real scripts run against throwaway CLAUDLOBBY_ROOTs; tg-post.sh is stubbed
to capture signal delivery (the notify-behind harness pattern)."""

import os
import subprocess
import time

from tests.conftest import TG_STUB, _scrubbed_env, _write_exec, plane_emit_env, read_fleet_events

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
        CLAUDLOBBY_ROOT=str(root), TG_CAPTURE=str(tmp_path / "tg-capture"),
        **plane_emit_env(),          # the host job's receipt lands on the plane under _host
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
    # Purge is allowlist-scoped: only known-ephemeral classes may be removed.
    # Everything else under data/ is durable by default, however old —
    # including unvetted .log names (a LevelDB-style 000003.log is live
    # database state, not a text log).
    EPHEMERAL = ["events/old.jsonl", "cron.log", "ledger.json.bak"]
    DURABLE = ["scripts/audit-tracker.py", "ledger.json", "notes.md", "000003.log"]

    def _fleet_data(self, root):
        data = root / "local" / "f7" / "runtime" / "bots" / "b1" / "data"
        (data / "events").mkdir(parents=True)
        (data / "scripts").mkdir()
        stale_mtime = time.time() - 40 * 86400
        for rel in self.EPHEMERAL + self.DURABLE:
            f = data / rel
            f.write_text("stale\n")
            os.utime(f, (stale_mtime, stale_mtime))
        fresh = data / "events" / "fresh.jsonl"
        fresh.write_text("current\n")
        return data

    def test_composed_invocation_purges_old_ephemeral_keeps_fresh(self, tmp_path):
        # The composed unit runs `data-sweep.sh --purge <fleet>` — flags
        # first, positional fleet name appended by the composer.
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["--purge", "f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        for rel in self.EPHEMERAL:
            assert not (data / rel).exists(), f"{rel} should be purged"
        assert (data / "events" / "fresh.jsonl").exists()

    def test_durable_files_survive_purge(self, tmp_path):
        # The recurring incident: operational scripts/ledgers under data/
        # aged out and vanished. Durable files must survive any purge.
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["--purge", "f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        for rel in self.DURABLE:
            assert (data / rel).exists(), f"{rel} must never be swept"

    def test_report_only_deletes_nothing(self, tmp_path):
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        for rel in self.EPHEMERAL + self.DURABLE:
            assert (data / rel).exists()

    def test_days_override_spares_younger_files(self, tmp_path):
        # Retention is fleet-overridable via the job's script line — prove
        # the flag the override carries actually widens the window.
        root = tmp_path / "root"
        data = self._fleet_data(root)
        r = _run("data-sweep.sh", ["--purge", "--days", "60", "f7"], root, tmp_path)
        assert r.returncode == 0, r.stderr
        for rel in self.EPHEMERAL:
            assert (data / rel).exists()

    def test_unknown_flag_still_rejected(self, tmp_path):
        # Rejected at arg parse — no fixture needed.
        r = _run("data-sweep.sh", ["--bogus"], tmp_path / "root", tmp_path)
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

    def _harness(self, tmp_path, npx_rc, plugins_line=None):
        if plugins_line is None:
            plugins_line = 'export FLEET_PLUGINS_REQUIRED="somepkg@Somewhere"'
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
        (bot / "bot.conf").write_text(plugins_line + "\n")
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

    def test_multi_token_single_quoted_plugins_parse_clean(self, tmp_path):
        """#658: the composer emits FLEET_PLUGINS_REQUIRED via shlex.quote,
        which single-quotes any multi-token value. The reader must strip that
        wrapper so each plugin reaches `claude plugin update` clean — not as
        "'alpha@Src" with a stray leading quote (the daily plugin-update fail)."""
        root, env = self._harness(
            tmp_path,
            npx_rc=0,
            plugins_line="export FLEET_PLUGINS_REQUIRED='alpha@Src beta@Src'",
        )
        self._run_reload(root, env)
        calls = self._calls(tmp_path)
        assert "claude plugin update alpha@Src" in calls
        assert "claude plugin update beta@Src" in calls
        assert "'alpha@Src" not in calls  # no stray leading quote
        assert "beta@Src'" not in calls  # no stray trailing quote


def _source_lib_common(tmp_path, snippet, path):
    """Run `snippet` in a bash shell that has sourced lib-common with an exact
    PATH — the only way to assert on tool resolution the way a timer sees it."""
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}/lib-common.sh"\n{snippet}'],
        env=_scrubbed_env(CLAUDLOBBY_ROOT=str(tmp_path / "clroot"), PATH=path),
        capture_output=True,
        text=True,
    )


def _venv_stub(tmp_path, body="#!/bin/bash\necho venv\n"):
    """A `claudlobby` console script in the repo-local venv — the install shape
    that is invisible to every system PATH."""
    venv_bin = tmp_path / "clroot" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    _write_exec(str(venv_bin / "claudlobby"), body)
    return venv_bin


class TestOwnToolPath:
    """#805: systemd/launchd hand a script a minimal PATH, so a timer-invoked
    lib/ script resolves neither `claude` nor `claudlobby` and dies with a bare
    command-not-found. own_tool_path is the fallback resolution that fixes it."""

    MINIMAL = "/usr/bin:/bin:/usr/sbin:/sbin"

    def test_resolves_a_tool_a_minimal_path_cannot_see(self, tmp_path):
        """The bug itself: a repo-venv console script is invisible to a timer."""
        venv_bin = _venv_stub(tmp_path)
        r = _source_lib_common(
            tmp_path,
            "command -v claudlobby >/dev/null 2>&1 && echo BEFORE_FOUND || echo BEFORE_MISSING\n"
            "own_tool_path\n"
            "command -v claudlobby",
            self.MINIMAL,
        )
        assert "BEFORE_MISSING" in r.stdout, r.stdout
        assert str(venv_bin / "claudlobby") in r.stdout, r.stdout + r.stderr

    def test_appends_so_it_never_shadows_the_callers_path(self, tmp_path):
        """APPEND, not prepend. An operator-pinned (or test-stubbed) binary must
        keep winning: prepending would silently re-point a job at a shadow user
        copy while the fleet runs the system one — the #635 failure class."""
        venv_bin = _venv_stub(tmp_path)
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        _write_exec(str(pinned / "claudlobby"), "#!/bin/bash\necho pinned\n")
        r = _source_lib_common(
            tmp_path, "own_tool_path\ncommand -v claudlobby", f"{pinned}:{self.MINIMAL}"
        )
        assert str(pinned / "claudlobby") in r.stdout, r.stdout
        assert str(venv_bin) not in r.stdout.strip().splitlines()[-1]

    def test_is_idempotent(self, tmp_path):
        """keepalive-style repeat invocation must not grow PATH without bound."""
        r = _source_lib_common(
            tmp_path,
            'own_tool_path; a="$PATH"; own_tool_path; own_tool_path\n'
            '[ "$a" = "$PATH" ] && echo STABLE || echo GREW',
            self.MINIMAL,
        )
        assert "STABLE" in r.stdout, r.stdout


class TestClaudlobbyCli:
    """getting-started.md supports two invocations — the `claudlobby` console
    script and `python3 -m claudlobby`. Where pip puts the console script
    depends on which python installed it, so PATH alone is not a contract."""

    def test_prefers_the_console_script_when_present(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        _write_exec(str(bindir / "claudlobby"), '#!/bin/bash\necho CONSOLE "$@"\n')
        r = _source_lib_common(
            tmp_path, "claudlobby_cli generate", f"{bindir}:/usr/bin:/bin"
        )
        assert "CONSOLE generate" in r.stdout, r.stdout + r.stderr

    def test_falls_back_to_python_module_from_the_repo_root(self, tmp_path):
        """No console script anywhere: an importable checkout must still run,
        and must resolve from CLAUDLOBBY_ROOT rather than the caller's cwd —
        the systemd units set no WorkingDirectory."""
        root = tmp_path / "clroot"
        pkg = root / "claudlobby"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text("import sys; print('MODULE', *sys.argv[1:])")
        # claudlobby_cli probes `import claudlobby.composer` rather than the bare
        # package, because the bare import succeeds from cwd with no dependencies
        # installed and so cannot tell a usable checkout from an unusable one.
        # An empty file satisfies that probe; the fixture stands in for "some
        # importable checkout", and a real one always has a composer module.
        (pkg / "composer.py").write_text("")
        r = subprocess.run(
            ["bash", "-c", f'. "{LIB}/lib-common.sh"\nclaudlobby_cli generate'],
            env=_scrubbed_env(CLAUDLOBBY_ROOT=str(root), PATH="/usr/bin:/bin"),
            capture_output=True,
            text=True,
            cwd="/",  # deliberately NOT the repo root
        )
        assert "MODULE generate" in r.stdout, r.stdout + r.stderr

    def test_unresolvable_says_so_diagnosably(self, tmp_path):
        r = _source_lib_common(tmp_path, "claudlobby_cli generate", "/usr/bin:/bin")
        assert r.returncode == 127
        assert "unresolvable" in r.stderr, r.stderr


class TestReloadFailureReasonIsTheRealError:
    """#805: the alert said `claude plugin update failed: example-skills@...`,
    which reads as a broken plugin. The plugin was fine — `claude` was not on
    the PATH. The reason must carry what actually failed, not the last command
    name; that misdirection is what cost the triage."""

    def _harness(self, tmp_path, claude_body=None, sysbin=None, **env_extra):
        """claude_body=None omits the `claude` stub entirely — the literal #805
        state, where the tool is absent rather than merely failing.

        `sysbin` replaces the real system bin dirs on the harness PATH. Omitting
        a stub is only half of "the tool is absent": the system dirs below still
        resolve whatever the host installed there, so a test asserting absence
        MUST pass a mirror that excludes the tool (see the sysbin_excluding
        fixture). Tests that write their own stub shadow the host copy from
        `bindir` and do not need one."""
        root = tmp_path / "root"
        libdir = root / "lib"
        libdir.mkdir(parents=True)
        for script in ("reload-fleet.sh", "lib-common.sh"):
            with open(os.path.join(LIB, script)) as f:
                _write_exec(str(libdir / script), f.read())
        _write_exec(str(libdir / "check-npx-cache.sh"), "#!/bin/bash\nexit 0\n")
        _write_exec(str(libdir / "tg-post.sh"), TG_STUB)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        if claude_body is not None:
            _write_exec(str(bindir / "claude"), claude_body)
        _write_exec(str(bindir / "claudlobby"), "#!/bin/bash\nexit 0\n")
        bot = root / "runtime" / "bots" / "tbot"
        bot.mkdir(parents=True)
        (bot / "bot.conf").write_text(
            'export FLEET_PLUGINS_REQUIRED="alpha@Src"\n'
            'export TELEGRAM_GROUP_CHAT_ID="-100123"\n'
        )
        r = subprocess.run(
            ["bash", str(libdir / "reload-fleet.sh")],
            env=_scrubbed_env(
                CLAUDLOBBY_ROOT=str(root),
                PATH=f"{bindir}:{sysbin or '/usr/bin:/bin:/usr/sbin:/sbin'}",
                TG_CAPTURE=str(tmp_path / "tg-capture"),
                TMUX_TMPDIR=str(tmp_path / "no-tmux"),
                **env_extra,
            ),
            capture_output=True,
            text=True,
        )
        log = root / "state" / "reload-fleet.log"
        return r, (log.read_text() if log.exists() else "")

    def _reason(self, log):
        lines = [ln for ln in log.splitlines() if "reload_failed:" in ln]
        assert lines, log
        return lines[0]

    def test_reason_carries_exit_code_and_the_commands_own_error(self, tmp_path):
        r, log = self._harness(
            tmp_path,
            '#!/bin/bash\necho "Plugin \\"alpha\\" not found" >&2\nexit 3\n',
        )
        assert r.returncode == 1
        reason = self._reason(log)
        assert "exit 3" in reason, reason
        assert 'Plugin "alpha" not found' in reason, reason

    def test_command_not_found_is_reported_as_a_path_fault(self, tmp_path):
        """The #805 signature: exit 127 must name the command and the PATH it
        was not found on, not blame the plugin."""
        r, log = self._harness(
            tmp_path, '#!/bin/bash\necho "claude: command not found" >&2\nexit 127\n'
        )
        assert r.returncode == 1
        reason = self._reason(log)
        assert "exit 127" in reason, reason
        assert "claude not found on PATH=" in reason, reason

    def test_absent_claude_names_the_tool_not_the_plugin(
        self, tmp_path, sysbin_excluding
    ):
        """The literal #805 case: no `claude` anywhere. The preflight must fail
        on the missing TOOL, so the alert is actionable — the old code blamed
        whichever plugin the loop happened to reach first.

        Absence has to be built, on both halves of the PATH: `sysbin` mirrors the
        system dirs without `claude` (a host with Claude Code at /usr/bin/claude
        otherwise satisfies the preflight and this test never runs its branch),
        and CLAUDLOBBY_TOOL_PREFIXES pins own_tool_path's fallback list empty so
        it cannot append a prefix that puts the tool back."""
        r, log = self._harness(
            tmp_path,
            claude_body=None,
            sysbin=sysbin_excluding("claude"),
            CLAUDLOBBY_TOOL_PREFIXES="",
        )
        assert r.returncode == 1
        reason = self._reason(log)
        assert "claude not found" in reason, reason
        assert "alpha@Src" not in reason, reason  # must NOT blame the plugin
