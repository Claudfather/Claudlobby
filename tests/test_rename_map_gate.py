"""F8 rename-map drift gate — boundary phase L4, deliverable 1.

The gate fails when a live value in ``known_values.CLAUDNA_SKILL_RENAMES`` points
at a clauDNA ``skills/`` dir that has been renamed away (stale-live-values only —
a brand-new rename with no map entry is undetectable by construction). These
tests prove the pure detection logic fires on a synthetic gone-dir and passes on
head; the network leg (clone/skip) is covered by the module's local-first design
and exercised live in the ``rename-map-gate`` CI job.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claudlobby import conformance
from claudlobby.known_values import CLAUDNA_SKILL_RENAMES

# Every distinct skill-dir token the current map's live values require present.
ALL_TOKENS = set(conformance.live_value_tokens().values())


class TestSkillTokenExtraction:
    def test_multiplexer_verb_takes_first_token(self):
        assert conformance.skill_token("/claudna:audit security") == "audit"
        assert conformance.skill_token("/claudna:session handoff") == "session"

    def test_whole_skill_successor_keeps_hyphen(self):
        assert conformance.skill_token("/claudna:review-work") == "review-work"
        assert conformance.skill_token("/claudna:using-claudna") == "using-claudna"

    def test_the_live_token_is_not_the_dead_name_minus_prefix(self):
        # security-audit → `audit`, not `security-audit` (the map's whole reason).
        assert conformance.live_value_tokens(
            {"/claudna:security-audit": "/claudna:audit security"}
        ) == {"/claudna:audit security": "audit"}


class TestStaleDetection:
    def test_all_dirs_present_is_clean(self):
        assert conformance.stale_live_values(ALL_TOKENS) == []

    def test_deliberate_violation_fires(self):
        """The proof the gate CAN fail: drop the `audit` dir and every
        `/claudna:audit *` live value must report stale."""
        existing = ALL_TOKENS - {"audit"}
        stale = conformance.stale_live_values(existing)
        stale_values = {value for value, _ in stale}
        # The eight audit-lens renames all collapse onto the gone `audit` dir.
        expected_audit = {
            v for v in CLAUDNA_SKILL_RENAMES.values() if v.startswith("/claudna:audit ")
        }
        assert expected_audit  # sanity: the map really has audit lenses
        assert expected_audit <= stale_values
        # …and nothing whose dir still exists is dragged in.
        assert all(token == "audit" for _, token in stale)

    def test_check_rename_map_reports_and_exits(self, tmp_path):
        # A synthetic skills/ missing `session` → nonzero + a named offender.
        skills = tmp_path / "skills"
        for token in ALL_TOKENS - {"session"}:
            (skills / token).mkdir(parents=True)
        code, report = conformance.check_rename_map(skills)
        assert code == 1
        assert "STALE" in report
        assert "session" in report

    def test_check_rename_map_passes_when_all_present(self, tmp_path):
        skills = tmp_path / "skills"
        for token in ALL_TOKENS:
            (skills / token).mkdir(parents=True)
        code, report = conformance.check_rename_map(skills)
        assert code == 0
        assert "OK" in report


class TestRefResolution:
    def test_env_ref_wins(self, monkeypatch):
        monkeypatch.setenv("CLAUDNA_REF", "v9.9.9")
        assert conformance.resolve_claudna_ref(Path("/nonexistent")) == "v9.9.9"

    def test_fleet_pin_read_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDNA_REF", raising=False)
        (tmp_path / "fleet.yaml").write_text(
            "fleet:\n  name: t\nbots:\n  b:\n    claudna_version: v1.2.3\n"
        )
        assert conformance.resolve_claudna_ref(tmp_path) == "v1.2.3"

    def test_unpinned_is_marketplace_latest_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDNA_REF", raising=False)
        (tmp_path / "fleet.yaml").write_text("fleet:\n  name: t\n")
        assert conformance.resolve_claudna_ref(tmp_path) is None

    def test_repo_names_the_marketplace_ssot(self):
        # The gate clones exactly what the composer defaults bots to.
        assert conformance._claudna_repo().endswith("/clauDNA")


class TestCliSkipSafety:
    def test_override_missing_dir_skips_zero(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("CLAUDNA_SKILLS_DIR", str(tmp_path / "nope"))
        assert conformance._run(["rename-map"]) == 0
        assert "SKIP" in capsys.readouterr().out

    def test_override_dir_runs_the_check(self, tmp_path, monkeypatch):
        skills = tmp_path / "skills"
        for token in ALL_TOKENS:
            (skills / token).mkdir(parents=True)
        monkeypatch.setenv("CLAUDNA_SKILLS_DIR", str(skills))
        assert conformance._run(["rename-map"]) == 0

    def test_bad_subcommand_usage_error(self):
        assert conformance._run(["bogus"]) == 2


# The real-data check: run the gate against a genuine clauDNA skills/ dir when
# one is reachable (CLAUDNA_SKILLS_DIR, or a sibling checkout beside the repo).
# Skipped otherwise — the network clone is the CI job's concern, not the suite's.
def _local_skills_dir() -> Path | None:
    override = os.environ.get("CLAUDNA_SKILLS_DIR")
    if override and Path(override).is_dir():
        return Path(override)
    for base in (Path.home() / "Projects", Path(__file__).resolve().parents[2]):
        cand = base / "clauDNA" / "skills"
        if cand.is_dir():
            return cand
    return None


@pytest.mark.skipif(
    _local_skills_dir() is None,
    reason="no local clauDNA skills/ checkout (network clone is the CI job's leg)",
)
def test_head_passes_against_real_clauDNA():
    code, report = conformance.check_rename_map(_local_skills_dir())
    assert code == 0, report
