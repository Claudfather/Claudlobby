"""Tests for the rolling code-audit sweep feature.

Covers config coercion (fleet.sweep block), opt-in timer emission via
compose_fleet_timers, the SWEEP_* env block in the owner's bot.conf, opt-out
(no block ⇒ nothing emitted), and validator errors/warnings.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from claudlobby import known_values
from claudlobby.__main__ import main
from claudlobby.config import SweepConfig, _coerce_sweep, load_fleet
from claudlobby.composer import compose_bot_conf, compose_fleet_timers
from tests.package_fixtures import git_package
from claudlobby.paths import Paths
from claudlobby.validator import validate
from tests.test_plane_events_door import _serving


def _make_paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=root, package=git_package(root))


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
      manager: mason
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
      manager: astrid
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
        assert "claudlobby/_runtime_scripts/code-audit-sweep.sh" in svc.read_text()

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
              manager: astrid
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
              manager: astrid
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
              manager: astrid
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
              manager: astrid
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
              manager: astrid
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


# fleet.sweep.audit_types is the list of audit types the rotation hands the
# code-audit-sweep skill, one per run. The skill runs only the types in its
# table; for any other name it audits nothing and emits audit_failed (#2992).
_SKILL = Path(__file__).resolve().parents[1] / "library/skills/code-audit-sweep/SKILL.md"
_TYPES_RUN = "tech-debt, security, docs, product-enhance"


def _skill_text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _skill_table_types() -> list[str]:
    """The first cell of each row of the skill's `| audit-type | Finds |` table."""
    types: list[str] = []
    in_table = False
    for line in _skill_text().splitlines():
        if re.match(r"\|\s*audit-type\s*\|", line):
            in_table = True
        elif in_table and line.startswith("|"):
            cell = line.split("|")[1].strip()
            if cell.strip("-: "):  # the |---|---| rule has nothing else in it
                types.append(cell.strip("`"))
        elif in_table:
            break
    return types


class TestSweepAuditTypes:
    """`fleet.sweep.audit_types` is checked against the types the skill runs."""

    def _report(self, tmp_path, audit_types, *, enabled=True):
        root = tmp_path / "f"
        body = (
            "fleet:\n"
            "  manager: astrid\n"
            "  name: test-fleet\n"
            "  service_prefix: com.test\n"
            "  system_defaults: false\n"
            "  bots:\n"
            "    astrid:\n"
            "      expertise: [software-engineering]\n"
            "  sweep:\n"
            f"    enabled: {'true' if enabled else 'false'}\n"
            "    owner_bot: astrid\n"
            "    repos: [acme/alpha]\n"
        )
        if audit_types is not None:
            body += f"    audit_types: {audit_types}\n"
        fleet, _md = load_fleet(_write(root, body))
        return validate(fleet, _make_paths(root))

    @staticmethod
    def _warned(report):
        return [message for kind, message in report.categorized() if kind == "audit-type"]

    def test_a_name_the_skill_does_not_run_is_a_warning_never_a_refusal(self, tmp_path):
        report = self._report(tmp_path, "[tech-debt, security-audit]")
        warned = self._warned(report)
        assert len(warned) == 1, report.categorized()
        assert "'security-audit'" in warned[0]
        assert _TYPES_RUN in warned[0]
        assert "emits audit_failed" in warned[0]
        assert not report.errors, report.errors

    @pytest.mark.parametrize(
        "name, closest",
        [
            ("tech_debt", "tech-debt"),
            ("Security", "security"),
            ("product-enhancement", "product-enhance"),
        ],
    )
    def test_the_warning_names_the_closest_type(self, tmp_path, name, closest):
        (warned,) = self._warned(self._report(tmp_path, f"[{name}]"))
        assert warned.endswith(f"\u2014 did you mean '{closest}'?"), warned

    def test_a_name_with_nothing_close_gets_no_suggestion(self, tmp_path):
        (warned,) = self._warned(self._report(tmp_path, "[system]"))
        assert "'system'" in warned and "did you mean" not in warned, warned

    @pytest.mark.parametrize(
        "old, successor",
        [("security-audit", "security"), ("docs-review", "docs")],
    )
    def test_a_name_the_old_table_listed_names_the_type_that_replaced_it(
        self, tmp_path, old, successor
    ):
        (warned,) = self._warned(self._report(tmp_path, f"[{old}]"))
        assert f"'{old}' is not a type code-audit-sweep runs ({_TYPES_RUN})" in warned, warned
        assert warned.endswith(f"\u2014 the skill renamed it to '{successor}'"), warned
        assert "did you mean" not in warned, warned

    def test_the_old_data_model_name_gets_the_by_hand_line(self, tmp_path):
        (warned,) = self._warned(self._report(tmp_path, "[data-model-audit]"))
        assert "'data-model-audit' runs only by hand" in warned, warned
        assert "as /claudna:audit data-model in an attended session" in warned, warned
        assert _TYPES_RUN in warned
        for other in ("did you mean", "renamed it", "is not a type"):
            assert other not in warned, warned

    def test_data_model_is_named_as_a_lens_that_runs_only_by_hand(self, tmp_path):
        (warned,) = self._warned(self._report(tmp_path, "[tech-debt, data-model]"))
        assert "'data-model' runs only by hand" in warned, warned
        assert "/claudna:audit data-model" in warned
        assert _TYPES_RUN in warned
        assert "did you mean" not in warned

    def test_every_type_the_skill_runs_passes(self, tmp_path):
        report = self._report(tmp_path, "[tech-debt, security, docs, product-enhance]")
        assert self._warned(report) == [], report.categorized()

    def test_the_default_types_pass(self, tmp_path):
        assert self._warned(self._report(tmp_path, None)) == []

    def test_a_disabled_sweep_is_not_checked(self, tmp_path):
        report = self._report(tmp_path, "[security-audit]", enabled=False)
        assert self._warned(report) == []

    def test_a_name_listed_twice_warns_once(self, tmp_path):
        report = self._report(tmp_path, "[security-audit, docs-review, security-audit]")
        warned = self._warned(report)
        assert [w.split("'")[1] for w in warned] == ["security-audit", "docs-review"], warned

    def test_validate_exits_zero_and_tags_the_line(self, fleet_dir, capsys):
        with (fleet_dir / "fleet.yaml").open("a") as f:
            f.write(
                "  sweep:\n"
                "    owner_bot: lead\n"
                "    repos: [acme/alpha]\n"
                "    audit_types: [tech-debt, security-audit]\n"
            )
        rc = main(["--root", str(fleet_dir), "--json", "config", "validate"])
        result = json.loads(capsys.readouterr().out)
        assert rc == 0 and result["ok"] is True, result
        assert result["data"]["warning_categories"].get("audit-type") == 1, result["data"]
        assert any(
            w.startswith("[audit-type] sweep.audit_types entry 'security-audit'")
            for w in result["data"]["warnings"]
        ), result["data"]["warnings"]


class TestSweepAuditTypeList:
    """The validator's list of types is the skill's table, and its by-hand lens is the skill's."""

    def test_the_list_is_the_skills_table(self):
        table = _skill_table_types()
        assert table, "the audit-type table in the skill moved: update _skill_table_types"
        assert list(known_values.CODE_AUDIT_SWEEP_TYPES) == table, (
            "CODE_AUDIT_SWEEP_TYPES in claudlobby/known_values.py and the table in "
            "library/skills/code-audit-sweep/SKILL.md must list the same types in the same order"
        )

    def test_the_by_hand_lens_is_named_by_the_skill_and_is_not_in_its_table(self):
        text = _skill_text()
        assert known_values.CODE_AUDIT_BY_HAND_TYPES == {"data-model"}
        for lens in known_values.CODE_AUDIT_BY_HAND_TYPES:
            assert f"/claudna:audit {lens}" in text and "by hand" in text
            assert lens not in _skill_table_types()
        assert known_values.CODE_AUDIT_BY_HAND_TYPES.isdisjoint(known_values.CODE_AUDIT_SWEEP_TYPES)

    def test_every_old_name_maps_to_a_type_that_runs_or_runs_by_hand(self):
        current = (*known_values.CODE_AUDIT_SWEEP_TYPES, *known_values.CODE_AUDIT_BY_HAND_TYPES)
        assert known_values.CODE_AUDIT_RENAMED_TYPES, "the old table's names are mapped"
        for old, successor in known_values.CODE_AUDIT_RENAMED_TYPES.items():
            assert successor in current, f"{old} -> {successor}: not a type the skill runs"
            assert old not in current, f"{old} is a current type, so it would never be read here"

    def test_the_skill_stops_on_a_type_outside_its_table(self):
        # The skill names audit_failed three times; this is the sentence every warning line
        # rests on ("a run that picks it audits nothing and emits audit_failed"). The text
        # wraps in the file, so its whitespace is joined before the match.
        text = " ".join(_skill_text().split())
        assert (
            "If the type you were dispatched is not in the table, run no audit: "
            "emit `audit_failed`"
        ) in text


class TestSweepSelector:
    """Hermetic end-to-end of claudlobby/_runtime_scripts/code-audit-sweep.sh with gh + tmux shimmed.

    Runs the real selector (and real bot-sweep-cron.sh) against a fixture fleet:
    `gh` returns canned staleness (acme/beta oldest ⇒ stalest) and `tmux` fakes
    an idle session. No live GitHub or real bot — so it runs in CI offline.
    """

    def _run(self, tmp_path: Path, *, scratch_plane_env):
        repo_root = Path(__file__).resolve().parents[1]
        selector = repo_root / "claudlobby/_runtime_scripts" / "code-audit-sweep.sh"

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
        # An idle input box that shows what is typed and clears on Enter: the
        # send presses Enter only once the box shows the payload (#1236).
        from tests.conftest import fake_tmux_input_box
        fake_tmux_input_box(tmux, tmp_path / "typed")
        gh.chmod(0o755)

        from tests.conftest import read_fleet_events
        env = dict(os.environ)
        env["CLAUDLOBBY_ROOT"] = str(root)
        env["CLAUDLOBBY_FLEET"] = "tf"          # the fleet job's carrier: the sweep's events anchor on bot:tf/owner
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["TMUX_BIN"] = str(tmux)
        env.update(scratch_plane_env(root, initialize=True))
        with _serving(root, scratch_plane_env) as socket:
            proc = subprocess.run(
                ["bash", str(selector), "tf"],
                env={**env, "PLANE_SOCKET": str(socket)},
                capture_output=True,
                text=True,
                timeout=120,
            )
        # F18 R1: the sweep's events land on the plane, never in a per-bot file;
        # rendered back as the legacy rows (or None when nothing was recorded)
        assert not list((owner / "data" / "events").glob("fleet-*.jsonl"))
        events = read_fleet_events(root)
        return proc, (events or None)

    def test_selects_stalest_and_dispatches(self, tmp_path, *, scratch_plane_env):
        proc, log = self._run(tmp_path, scratch_plane_env=scratch_plane_env)
        assert proc.returncode == 0, proc.stderr
        assert log is not None, f"no event recorded on the plane: {proc.stderr}"
        assert '"bot":"owner"' in log                    # anchored on the owner bot
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
