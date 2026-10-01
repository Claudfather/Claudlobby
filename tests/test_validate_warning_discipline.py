"""#1663 — a validate run whose warnings a reader can act on.

Four properties, each pinned here: a finding with ONE cause is one line however
many bots it reaches (the fold); every warning carries a category slug passed at
the site that raises it; doctor's ``fleet-yaml`` rung names those categories
instead of a bare count; and ``config validate --warn-baseline`` fails only on a
category that is new or has grown, so a fleet with accepted warnings still has a
gate. ``--strict`` is untouched and checked here too.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from claudlobby import validator as validator_module
from claudlobby.__main__ import main
from claudlobby.config import GithubAppConfig, load_fleet
from claudlobby.doctor import DoctorReport, check_fleet_validation
from tests.package_fixtures import source_package
from claudlobby.paths import Paths
from claudlobby.validator import (
    UNCATEGORIZED,
    WARNING_CATEGORIES,
    ValidationReport,
    render_warnings,
    validate,
    warning_summary,
)

WORKERS = ["w2", "w3", "w4", "w5"]


def _paths(root: Path) -> Paths:
    return Paths(root=root, fleet_dir=None, package=source_package())


def _env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "ghp_test")
    monkeypatch.setenv("TELEGRAM_TOKEN_LEAD", "123:abc")
    monkeypatch.setenv("TELEGRAM_TOKEN_WORKER1", "456:def")


def _grow(fleet_dir: Path, defaults: str = "", bots: dict[str, str] | None = None) -> None:
    """The shared fixture's two bots plus four plain workers: six bots, more
    than a folded line names, so a fold and its absence cannot look alike.
    ``defaults`` lands under ``fleet.defaults``; ``bots`` adds lines to one
    bot's own stanza."""
    path = fleet_dir / "fleet.yaml"
    text = path.read_text()
    anchor = "    protocols: [report-back]\n"
    assert text.count(anchor) == 1, "the fixture's defaults block moved"
    text = text.replace(anchor, anchor + defaults)
    for name in WORKERS:
        text += f"    {name}:\n      expertise: [software-engineering]\n"
        text += (bots or {}).get(name, "")
    path.write_text(text)


def _validate(fleet_dir: Path):
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    return validate(fleet, _paths(fleet_dir))


def _of(report: ValidationReport, kind: str) -> list[str]:
    return [m for k, m in report.categorized() if k == kind]


# ── the fold ────────────────────────────────────────────────────────────────


class TestSharedCauseIsOneLine:
    def test_a_fleet_level_retired_key_warns_once_and_names_the_bot_count(
        self, fleet_dir, monkeypatch
    ):
        _env(monkeypatch)
        _grow(fleet_dir, defaults="    observability:\n      reap_days: 30\n")
        report = _validate(fleet_dir)
        hits = [w for w in report.warnings if "reap_days" in w]
        assert len(hits) == 1, hits  # one key in one block: one line, not six
        assert hits[0].startswith("defaults.observability.reap_days has no reader")
        assert hits[0].endswith("affects 6 bot(s): lead, worker-1, w2, w3 (+2 more)")
        assert report.warning_categories["retired-key"] == 1

    def test_a_retired_key_in_one_bots_own_stanza_stays_that_bots(
        self, fleet_dir, monkeypatch
    ):
        _env(monkeypatch)
        _grow(fleet_dir, bots={"w3": "      observability:\n        reap_days: 30\n"})
        hits = [w for w in _validate(fleet_dir).warnings if "reap_days" in w]
        assert hits == [
            "bot 'w3': observability.reap_days has no reader since the F18 closure"
            f" — remove it ({validator_module._RETIRED_OBSERVABILITY_KEYS['reap_days']})"
        ]

    def test_an_env_var_set_empty_above_the_bot_tier_warns_once_and_a_bot_tier_one_stays_its_own(
        self, fleet_dir, monkeypatch
    ):
        _env(monkeypatch)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        _grow(fleet_dir, defaults="    mcp: [github]\n")
        (fleet_dir / ".env").write_text("GITHUB_PAT=\n")
        own_env = _paths(fleet_dir).bot_runtime("w4") / ".env"
        own_env.parent.mkdir(parents=True, exist_ok=True)
        own_env.write_text("GITHUB_PAT=\n")
        # The fixture declares GITHUB_PAT on two surfaces (the MCP fragment and
        # the integration doc) and each surface warns; this is about the MCP one.
        hits = [w for w in _of(_validate(fleet_dir), "env-empty") if "mcp/github" in w]
        # Two causes, two lines: the fleet-tier stub reaching five bots, and
        # w4's own stub, which fixing the fleet tier would leave in place.
        assert len(hits) == 2, hits
        assert hits[0].startswith("bot 'w4': mcp/github requires GITHUB_PAT but it is SET BUT EMPTY")
        assert hits[1].startswith("mcp/github requires GITHUB_PAT but it is SET BUT EMPTY")
        assert hits[1].endswith("affects 5 bot(s): lead, worker-1, w2, w3 (+1 more)")

    def test_an_env_var_no_tier_sets_warns_once(self, fleet_dir, monkeypatch):
        _env(monkeypatch)
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        _grow(fleet_dir, defaults="    mcp: [github]\n")
        hits = [w for w in _validate(fleet_dir).warnings if "mcp/github requires GITHUB_PAT" in w]
        assert len(hits) == 1, hits
        assert "no .env tier sets it" in hits[0]
        assert hits[0].endswith("affects 6 bot(s): lead, worker-1, w2, w3 (+2 more)")

    def test_the_claudron_host_facts_warn_once_for_every_vault_wired_bot(
        self, fleet_dir, tmp_path, monkeypatch
    ):
        _env(monkeypatch)
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        missing = tmp_path / "no-vault-here"
        _grow(fleet_dir, defaults=f"    claudron_vault_path: {missing}\n")
        report = _validate(fleet_dir)
        for kind, text in (
            ("claudron-path", "claudron CLI is not on PATH"),
            ("vault-path", "is not a directory on this host"),
        ):
            hits = _of(report, kind)
            assert len(hits) == 1, (kind, hits)
            assert text in hits[0] and "affects 6 bot(s)" in hits[0], hits[0]

    def test_the_operator_gitconfig_gaps_warn_once(self, fleet_dir, tmp_path, monkeypatch):
        import claudlobby.composer as comp

        _env(monkeypatch)
        _grow(fleet_dir)
        op = tmp_path / "op.gitconfig"  # no identity, and an ssh-forcing rewrite
        op.write_text('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
        monkeypatch.setattr(comp, "_operator_gitconfig", lambda: op)
        fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
        for name in ("lead", "worker-1", "w2"):
            fleet.bots[name].git_credentials = {"OrgA": "ORG_A_PAT"}
        for name in ("w3", "w4"):
            fleet.bots[name].github_app = GithubAppConfig()
        report = validate(fleet, _paths(fleet_dir))
        identity = _of(report, "git-identity")
        # One host file, two consumers: the credential include (three bots)
        # and the App's identity fallback (two) — two findings, not five.
        assert len(identity) == 2, identity
        assert identity[0].endswith("affects 3 bot(s): lead, worker-1, w2")
        assert identity[1].endswith("affects 2 bot(s): w3, w4")
        rewrite = _of(report, "git-rewrite")
        assert len(rewrite) == 1 and rewrite[0].endswith("affects 2 bot(s): w3, w4"), rewrite


# ── categories ──────────────────────────────────────────────────────────────


def test_every_warning_has_a_category_and_the_counts_sum(fleet_dir, monkeypatch):
    """Positive control first: the fixture must actually raise the families it
    is built to raise, or an all-categorized result says nothing."""
    _env(monkeypatch)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)
    _grow(
        fleet_dir,
        defaults="    mcp: [github]\n    observability:\n      reap_days: 30\n",
        bots={
            "w2": "      voice: no-such-voice\n      skills: [no-such-skill]\n",
            "w3": "      guardrails: [no-such-guardrail]\n      mcp: [no-such-mcp]\n",
            "w4": "      model: gpt-banana\n      reports_to: nobody\n",
            "w5": "      observability:\n        pulse_interval: 0\n",
        },
    )
    report = _validate(fleet_dir)
    raised = set(report.warning_categories)
    assert {
        "voice-missing", "skill-missing", "guardrail-missing", "mcp-missing",
        "model-unknown", "topology", "obs-range", "retired-key", "env-unset",
    } <= raised, report.categorized()
    assert UNCATEGORIZED not in raised, report.categorized()
    assert raised <= set(WARNING_CATEGORIES), raised - set(WARNING_CATEGORIES)
    assert sum(report.warning_categories.values()) == len(report.warnings)


def test_every_slug_the_validator_passes_is_registered_and_every_registered_one_is_used():
    """Static, so it covers sites no fixture reaches: an unregistered slug is a
    typo the baseline gate would report as a new category forever, and a
    registered one nothing passes is documentation of a warning that no longer
    exists. And no site may bypass ``warn`` — a direct append has no category."""
    src = Path(validator_module.__file__).read_text()
    used = set(re.findall(r'(?:report\.warn|shared\.add)\(\s*"([a-z-]+)"', src))
    used |= set(re.findall(r'\benv_kind = "([a-z-]+)"', src))
    used |= set(re.findall(r'_mp\.[A-Z]+: "([a-z-]+)"', src))
    used |= set(validator_module._REF_MISSING.values())
    assert used - set(WARNING_CATEGORIES) == set(), "unregistered slug(s)"
    assert set(WARNING_CATEGORIES) - used == set(), "registered but never raised"
    assert src.count(".warnings.append(") == 1, "only warn() itself may append"
    assert ".warnings.extend(" not in src


def test_a_direct_append_reads_uncategorized_and_shifts_nothing():
    report = ValidationReport()
    report.warn("topology", "a")
    report.warnings.append("b")  # a caller that bypassed warn()
    report.warn("dead-flag", "c")
    assert report.categorized() == [
        ("topology", "a"), (UNCATEGORIZED, "b"), ("dead-flag", "c"),
    ]
    assert sum(report.warning_categories.values()) == len(report.warnings)


def test_the_summary_leads_with_the_largest_category_and_states_its_cap():
    report = ValidationReport()
    for kind, n in [
        ("topology", 1), ("env-empty", 3), ("dead-flag", 2), ("grant-broad", 3),
        ("skill-missing", 1), ("unknown-key", 1), ("obs-range", 1),
    ]:
        for i in range(n):
            report.warn(kind, f"{kind} {i}")
    assert warning_summary(report) == (
        "12 warning(s): env-empty×3, grant-broad×3, dead-flag×2, obs-range×1,"
        " skill-missing×1, and 2 more categories"
    )
    report.warn("repo-format", "x")
    assert warning_summary(report, top=7).endswith("topology×1, and 1 more category")


def test_validate_prints_each_warning_under_its_category():
    report = ValidationReport()
    report.warn("topology", "bot 'a': reports_to 'b' not found in fleet.bots")
    assert render_warnings(report) == [
        "[topology] bot 'a': reports_to 'b' not found in fleet.bots"
    ]


def test_fleet_yaml_rung_names_warning_categories(fleet_dir, monkeypatch):
    _env(monkeypatch)
    _grow(fleet_dir, defaults="    observability:\n      reap_days: 30\n")
    fleet, _md = load_fleet(fleet_dir / "fleet.yaml")
    report = DoctorReport()
    check_fleet_validation(fleet, _paths(fleet_dir), report)
    rung = next(c for c in report.checks if c.name == "fleet-yaml")
    assert rung.status == "warn"
    assert re.search(r"\b[a-z]+(?:-[a-z]+)*×\d+", rung.detail), rung.detail
    assert rung.detail.startswith(warning_summary(validate(fleet, _paths(fleet_dir))))


# ── the baseline gate ───────────────────────────────────────────────────────


def _public_rc(root: Path, *, warn_baseline: str | None = None,
               write: bool = False, strict: bool = False) -> int:
    argv = ["--root", str(root), "--json", "config", "validate"]
    if warn_baseline:
        argv.extend(("--warn-baseline", warn_baseline))
    if write:
        argv.append("--write")
    if strict:
        argv.append("--strict")
    return main(argv)


def _red_fleet(fleet_dir: Path, monkeypatch) -> None:
    """A fleet with accepted warnings (a retired key, two unset tokens), so
    ``--strict`` can never pass on it."""
    _env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_TOKEN_LEAD", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN_WORKER1", raising=False)
    _grow(fleet_dir, defaults="    observability:\n      reap_days: 30\n")


class TestWarnBaseline:
    def test_public_validate_uses_generated_fleet_context_without_a_flag(
        self, fleet_dir, monkeypatch, capsys
    ):
        overlay = fleet_dir / "local" / "test-fleet"
        overlay.mkdir(parents=True)
        (fleet_dir / "fleet.yaml").rename(overlay / "fleet.yaml")
        monkeypatch.setenv("CLAUDLOBBY_ROOT", str(fleet_dir))
        monkeypatch.setenv("FLEET_NAME", "test-fleet")
        monkeypatch.setenv("FLEET_ROOT", str(overlay))

        rc = main(["--json", "config", "validate"])
        result = json.loads(capsys.readouterr().out)
        assert rc == 0 and result["ok"] is True, result
        assert result["command"] == "config.validate"
        assert result["data"]["fleet"] == "test-fleet"

        # An explicit selector still wins over generated session context.
        rc = main(["--fleet", "missing", "--json", "config", "validate"])
        result = json.loads(capsys.readouterr().out)
        assert rc == 3 and result["error"]["code"] == "not_found"

    def test_public_config_validate_keeps_strict_and_baseline_gates(self, fleet_dir, tmp_path, monkeypatch, capsys):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"

        def call(*options):
            rc = main(["--root", str(fleet_dir), "--json", "config", "validate", *options])
            return rc, json.loads(capsys.readouterr().out)

        rc, written = call("--warn-baseline", str(base), "--write")
        assert rc == 0 and written["command"] == "config.validate"
        assert written["data"]["baseline_written"] is True
        assert written["data"]["warning_categories"] == json.loads(base.read_text())
        rc, accepted = call("--warn-baseline", str(base))
        assert rc == 0 and accepted["ok"] is True and accepted["data"]["warning_count"] > 0
        counts = json.loads(base.read_text())
        grown = next(kind for kind, count in counts.items() if count > 0)
        base.write_text(json.dumps({**counts, grown: counts[grown] - 1}))
        rc, rejected = call("--warn-baseline", str(base))
        assert rc == 4 and rejected["error"]["code"] == "conflict"
        rc, strict = call("--strict", "--warn-baseline", str(base))
        assert rc == 4 and strict["error"]["code"] == "conflict"
        rc, missing = call("--warn-baseline", str(tmp_path / "missing.json"))
        assert rc == 6 and missing["error"]["code"] == "unavailable"
        rc, invalid = call("--write")
        assert rc == 2 and invalid["error"]["code"] == "invalid_argument"

    def test_warn_baseline_passes_on_an_unchanged_red_baseline(self, fleet_dir, tmp_path, monkeypatch):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"
        assert _public_rc(fleet_dir, warn_baseline=str(base), write=True) == 0
        recorded = json.loads(base.read_text())
        assert recorded.get("retired-key") == 1 and recorded.get("env-unset") == 2, recorded
        assert _public_rc(fleet_dir, warn_baseline=str(base)) == 0

    def test_warn_baseline_fails_on_a_new_category_only(self, fleet_dir, tmp_path, monkeypatch, caplog, capsys):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"
        assert _public_rc(fleet_dir, warn_baseline=str(base), write=True) == 0
        capsys.readouterr()
        path = fleet_dir / "fleet.yaml"
        path.write_text(path.read_text() + "      skills: [no-such-skill]\n")  # onto w5
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            assert _public_rc(fleet_dir, warn_baseline=str(base)) == 4
        assert "new warning category: skill-missing (0 → 1)" in caplog.text
        result = json.loads(capsys.readouterr().out)
        assert any("[skill-missing] bot 'w5'" in line for line in result["data"]["warnings"])

    def test_a_grown_category_fails_and_a_shrunk_one_does_not(self, fleet_dir, tmp_path, monkeypatch, caplog):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"
        assert _public_rc(fleet_dir, warn_baseline=str(base), write=True) == 0
        counts = json.loads(base.read_text())
        base.write_text(json.dumps({**counts, "retired-key": 3, "topology": 2}))
        assert _public_rc(fleet_dir, warn_baseline=str(base)) == 0
        base.write_text(json.dumps({**counts, "env-unset": 1}))
        with caplog.at_level(logging.INFO, logger="claudlobby"):
            assert _public_rc(fleet_dir, warn_baseline=str(base)) == 4
        assert "warning category grew: env-unset (1 → 2)" in caplog.text

    def test_a_baseline_that_cannot_be_read_refuses_rather_than_passing(self, fleet_dir, tmp_path, monkeypatch):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"
        assert _public_rc(fleet_dir, warn_baseline=str(base)) == 6  # absent
        for bad in ("{not json", "[1, 2]", '{"topology": -1}', '{"topology": true}'):
            base.write_text(bad)
            assert _public_rc(fleet_dir, warn_baseline=str(base)) == 6, bad

    def test_write_without_a_baseline_file_is_refused(self, fleet_dir, monkeypatch):
        _red_fleet(fleet_dir, monkeypatch)
        assert _public_rc(fleet_dir, write=True) == 2

    def test_strict_still_fails_on_any_warning_whatever_the_baseline_says(self, fleet_dir, tmp_path, monkeypatch):
        _red_fleet(fleet_dir, monkeypatch)
        base = tmp_path / "baseline.json"
        assert _public_rc(fleet_dir, warn_baseline=str(base), write=True) == 0
        assert _public_rc(fleet_dir, warn_baseline=str(base), strict=True) == 4
        assert _public_rc(fleet_dir, strict=True) == 4
