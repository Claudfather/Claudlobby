"""Tests for the rolling code-audit sweep feature.

Covers config coercion (fleet.sweep block), opt-in timer emission via
compose_fleet_timers, the SWEEP_* env block in the owner's bot.conf, opt-out
(no block ⇒ nothing emitted), and validator errors/warnings.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

from claudlobby.config import SweepConfig, _coerce_sweep, load_fleet
from claudlobby.composer import compose_bot_conf, compose_fleet_timers
from claudlobby.paths import Paths
from claudlobby.validator import validate


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root)


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fleet.yaml").write_text(dedent(body))
    return root / "fleet.yaml"


def _env_val(conf: str, key: str) -> str | None:
    """Return the shell-unquoted value of an `export KEY=...` line, or None.

    Quote-agnostic: _shq (shlex.quote) only quotes values that need it, so the
    test asserts on the logical value rather than the exact quoting.
    """
    prefix = f"export {key}="
    for line in conf.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return " ".join(shlex.split(line[len(prefix) :]))
    return None


# A fleet with an enabled sweep block pointing at owner bot "astrid".
_SWEEP_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        astrid:
          expertise: [eng]
          scope:
            repos: [acme/alpha, acme/beta]
        mason:
          expertise: [eng]
      sweep:
        owner_bot: astrid
        repos: [acme/alpha, acme/beta]
        audit_types: [tech-debt, security-audit]
        schedule: "*-*-* 02:30:00"
"""

# A fleet with no sweep block at all (opt-out).
_NO_SWEEP_FLEET = """\
    fleet:
      name: test-fleet
      service_prefix: com.test
      system_defaults: false
      bots:
        astrid:
          expertise: [eng]
"""


class TestSweepConfigCoercion:
    def test_coerce_none_when_absent(self):
        assert _coerce_sweep(None) is None
        assert _coerce_sweep({}) is None

    def test_present_block_is_opt_in(self):
        sw = _coerce_sweep({"owner_bot": "astrid", "repos": ["o/a"]})
        assert isinstance(sw, SweepConfig)
        assert sw.enabled is True  # presence of the block = opt-in
        assert sw.owner_bot == "astrid"
        assert sw.repos == ["o/a"]
        assert sw.label == "auto-audit"
        assert sw.audit_types == ["tech-debt"]

    def test_enabled_false_respected(self):
        sw = _coerce_sweep({"owner_bot": "astrid", "enabled": False})
        assert sw is not None and sw.enabled is False

    def test_load_fleet_parses_block(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _SWEEP_FLEET))
        assert fleet.sweep is not None
        assert fleet.sweep.enabled is True
        assert fleet.sweep.owner_bot == "astrid"
        assert fleet.sweep.schedule == "*-*-* 02:30:00"
        assert fleet.sweep.audit_types == ["tech-debt", "security-audit"]

    def test_load_fleet_none_when_absent(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _NO_SWEEP_FLEET))
        assert fleet.sweep is None


class TestSweepTimerEmission:
    def test_sweep_timer_emitted_when_enabled(self, tmp_path):
        root = tmp_path / "f"
        fleet, md = load_fleet(_write(root, _SWEEP_FLEET))
        paths = _make_paths(root)
        compose_fleet_timers(fleet, paths, md)

        timers = paths.runtime_fleet / "timers"
        svc = timers / "com.test.code-audit-sweep.service"
        timer = timers / "com.test.code-audit-sweep.timer"
        plist = timers / "com.test.code-audit-sweep.plist"
        assert svc.is_file() and timer.is_file() and plist.is_file()
        # schedule flows through to OnCalendar
        assert "OnCalendar=*-*-* 02:30:00" in timer.read_text()
        # the unit runs the selector script
        assert "lib/code-audit-sweep.sh" in svc.read_text()

    def test_no_units_when_no_sweep_block(self, tmp_path):
        root = tmp_path / "f"
        fleet, md = load_fleet(_write(root, _NO_SWEEP_FLEET))
        paths = _make_paths(root)
        compose_fleet_timers(fleet, paths, md)

        timers = paths.runtime_fleet / "timers"
        # system_defaults is off and there is no sweep ⇒ no sweep units (and,
        # since defaults are off, no units at all).
        assert not (timers / "com.test.code-audit-sweep.timer").exists()


class TestSweepBotConf:
    def test_owner_gets_sweep_env(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _SWEEP_FLEET))
        conf = compose_bot_conf(
            fleet.bots["astrid"], fleet, _make_paths(tmp_path / "f")
        )
        assert _env_val(conf, "SWEEP_OWNER_BOT") == "astrid"
        assert _env_val(conf, "SWEEP_REPOS") == "acme/alpha acme/beta"
        assert _env_val(conf, "SWEEP_LABEL") == "auto-audit"
        assert _env_val(conf, "SWEEP_AUDIT_TYPES") == "tech-debt security-audit"

    def test_non_owner_has_no_sweep_env(self, tmp_path):
        fleet, _md = load_fleet(_write(tmp_path / "f", _SWEEP_FLEET))
        conf = compose_bot_conf(fleet.bots["mason"], fleet, _make_paths(tmp_path / "f"))
        assert "SWEEP_OWNER_BOT" not in conf

    def test_repos_default_to_owner_scope(self, tmp_path):
        body = """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                astrid:
                  expertise: [eng]
                  scope:
                    repos: [acme/only]
              sweep:
                owner_bot: astrid
        """
        fleet, _md = load_fleet(_write(tmp_path / "f", body))
        conf = compose_bot_conf(
            fleet.bots["astrid"], fleet, _make_paths(tmp_path / "f")
        )
        assert _env_val(conf, "SWEEP_REPOS") == "acme/only"


class TestSweepValidation:
    def _errors(self, tmp_path, body):
        root = tmp_path / "f"
        fleet, _md = load_fleet(_write(root, body))
        return validate(fleet, _make_paths(root))

    def test_unknown_owner_errors(self, tmp_path):
        body = """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                astrid:
                  expertise: [eng]
                  scope: {repos: [o/a]}
              sweep:
                owner_bot: nobody
        """
        report = self._errors(tmp_path, body)
        assert any("sweep.owner_bot" in e for e in report.errors)

    def test_no_repos_no_scope_errors(self, tmp_path):
        body = """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                astrid:
                  expertise: [eng]
              sweep:
                owner_bot: astrid
        """
        report = self._errors(tmp_path, body)
        assert any("no repos" in e for e in report.errors)

    def test_bad_repo_format_warns(self, tmp_path):
        body = """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                astrid:
                  expertise: [eng]
              sweep:
                owner_bot: astrid
                repos: [not-a-repo]
        """
        report = self._errors(tmp_path, body)
        assert any("does not match <org>/<repo>" in w for w in report.warnings)

    def test_disabled_sweep_no_sweep_errors(self, tmp_path):
        body = """\
            fleet:
              name: test-fleet
              service_prefix: com.test
              system_defaults: false
              bots:
                astrid:
                  expertise: [eng]
              sweep:
                enabled: false
                owner_bot: nobody
        """
        report = self._errors(tmp_path, body)
        assert not any("sweep" in e for e in report.errors)


class TestSweepSelector:
    """Hermetic end-to-end of lib/code-audit-sweep.sh with gh + tmux shimmed.

    Runs the real selector (and real bot-sweep-cron.sh) against a fixture fleet:
    `gh` returns canned staleness (acme/beta oldest ⇒ stalest) and `tmux` fakes
    an idle session. No live GitHub or real bot — so it runs in CI offline.
    """

    def _run(self, tmp_path: Path):
        repo_root = Path(__file__).resolve().parents[1]
        selector = repo_root / "lib" / "code-audit-sweep.sh"

        root = tmp_path / "root"
        owner = root / "local" / "tf" / "runtime" / "bots" / "owner"
        (owner / "data" / "events").mkdir(parents=True)
        (owner / "bot.conf").write_text(
            # BOT_SERVICE doubles as the per-bot tmux socket name; the sweep
            # resolves it to dispatch on the owner's own server.
            "export BOT_SERVICE=com.tf.owner\n"
            "export SWEEP_OWNER_BOT=owner\n"
            "export SWEEP_REPOS='acme/alpha acme/beta'\n"
            "export SWEEP_LABEL=auto-audit\n"
            "export SWEEP_AUDIT_TYPES=tech-debt\n"
        )

        bindir = tmp_path / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(
            dedent("""\
            #!/usr/bin/env bash
            repo=""
            while [ $# -gt 0 ]; do
                case "$1" in --repo) repo="$2"; shift 2;; *) shift;; esac
            done
            case "$repo" in
                *beta)  echo "2026-01-01T00:00:00Z";;
                *alpha) echo "2026-06-10T00:00:00Z";;
                *)      echo "NONE";;
            esac
            """)
        )
        tmux = bindir / "tmux"
        tmux.write_text(
            dedent("""\
            #!/usr/bin/env bash
            # Skip a leading "-L <socket>" (per-bot server selection) to reach
            # the real subcommand.
            [ "$1" = "-L" ] && shift 2
            case "$1" in
                has-session) exit 0;;
                capture-pane) echo "idle - at prompt";;
                send-keys) exit 0;;
            esac
            """)
        )
        gh.chmod(0o755)
        tmux.chmod(0o755)

        env = dict(os.environ)
        env["CLAUDLOBBY_ROOT"] = str(root)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["TMUX_BIN"] = str(tmux)
        proc = subprocess.run(
            ["bash", str(selector), "tf"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        events = sorted((owner / "data" / "events").glob("fleet-*.jsonl"))
        return proc, (events[0] if events else None)

    def test_selects_stalest_and_dispatches(self, tmp_path):
        proc, events_file = self._run(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert events_file is not None, "no events file written"
        log = events_file.read_text()
        # acme/beta is the oldest audit -> stalest -> selected
        assert '"type":"audit_selected"' in log
        assert '"repo":"acme/beta"' in log
        # staleness_days must be a real integer, not null: the selector routes the
        # createdAt (Z-suffixed) through iso_to_epoch, so a null here means the
        # portable parse failed on this runner (#711 site 2 regression guard).
        m = re.search(r'"staleness_days":(null|-?\d+)', log)
        assert m, f"no staleness_days field in event: {log}"
        assert m.group(1) != "null", (
            "staleness_days null — iso_to_epoch failed to parse createdAt"
        )
        assert int(m.group(1)) > 0, f"expected positive staleness, got {m.group(1)}"
        # tmux shim is idle (not busy) -> dispatch fires
        assert '"type":"audit_dispatched"' in log
