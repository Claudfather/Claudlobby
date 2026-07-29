"""#866 coverage-honesty A/B — tests for the ab-comms-eval experiment mode.

Hermetic and CI-safe throughout: the dry-run path composes both variants with
real `claudlobby generate`, exercises the frozen regexes and the analyzer, and
makes zero model calls. The pre-registered REAL run is an operator action.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from tests.conftest import constructed_env, load_lib_module

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "lib" / "ab-comms-eval.sh"

verdict = load_lib_module("ab-coverage-verdict")


def _frozen_re(name: str) -> str:
    for line in HARNESS.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("'", 2)[1]
    raise AssertionError(f"{name} not found in harness")


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.IGNORECASE))


class TestFrozenRegexes:
    """Behavioral pins on the #866-frozen regexes AS COMMITTED in the harness."""

    def test_verif_regex_matches_coverage_language(self):
        pat = _frozen_re("COV_VERIF_RE")
        assert _count(pat, "I scanned 40 of 212 files; the rest went unchecked.") >= 3
        assert _count(pat, "Verified and double-checked the output.") >= 2
        assert _count(pat, "The weather is nice today.") == 0

    def test_disclosure_regex_matches_bound_statements(self):
        pat = _frozen_re("COV_DISCLOSE_RE")
        assert _count(pat, "found nothing in what I checked") >= 1
        assert _count(pat, "reviewed the first 10 files only") >= 1
        assert _count(pat, "all clear, no problems anywhere") == 0


class TestAnalyzer:
    @staticmethod
    def _row(task, variant, rep, length, matches, disclosure=False, valid=True):
        return {
            "task": task,
            "variant": variant,
            "rep": rep,
            "len_chars": length,
            "verif_matches": matches,
            "disclosure": disclosure,
            "valid": valid,
        }

    def _matrix(self, with_len_bounded, with_len_ctl, reps=3):
        rows = []
        for rep in range(1, reps + 1):
            for task in ("T1", "T2"):
                rows.append(self._row(task, "without", rep, 1000, 1))
                rows.append(self._row(task, "with", rep, with_len_bounded, 2, True))
            rows.append(self._row("T3", "without", rep, 500, 1))
            rows.append(self._row("T3", "with", rep, with_len_ctl, 1))
        return rows

    def test_clause_specific_branch(self):
        text, rc = verdict.analyze(
            self._matrix(with_len_bounded=2000, with_len_ctl=500)
        )
        assert rc == 0
        assert "CLAUSE-SPECIFIC EFFECT" in text

    def test_generic_verbosity_branch(self):
        text, _ = verdict.analyze(
            self._matrix(with_len_bounded=2000, with_len_ctl=1000)
        )
        assert "GENERIC-VERBOSITY EFFECT" in text

    def test_inconclusive_branch_and_wording(self):
        rows = self._matrix(with_len_bounded=1000, with_len_ctl=500)
        rows[1]["len_chars"] = 990  # tiny mixed jitter around zero
        text, _ = verdict.analyze(rows)
        assert "INCONCLUSIVE" in text
        assert "does not corroborate" in text

    def test_zero_baseline_pairs_disclosed_not_silent(self):
        rows = self._matrix(with_len_bounded=2000, with_len_ctl=500)
        for r in rows:
            if r["variant"] == "without":
                r["verif_matches"] = 0  # density baseline zero -> pair undefined
        text, _ = verdict.analyze(rows)
        assert "dropped: zero baseline" in text

    def test_invalid_rows_disclosed_and_excluded(self):
        rows = self._matrix(with_len_bounded=2000, with_len_ctl=500)
        rows[0]["valid"] = False
        text, rc = verdict.analyze(rows)
        assert rc == 0
        assert "1 invalid" in text
        assert "excluded and disclosed" in text

    def test_no_pairs_is_failure(self):
        _, rc = verdict.analyze([self._row("T1", "without", 1, 100, 0)])
        assert rc == 1

    def test_machine_line_contract(self):
        text, _ = verdict.analyze(self._matrix(with_len_bounded=2000, with_len_ctl=500))
        line = [ln for ln in text.splitlines() if ln.startswith("COVERAGE_AB_RESULT ")]
        assert line
        assert "n_pairs=6" in line[0]
        assert "seed=" in line[0]


def test_dry_run_end_to_end():
    env = constructed_env(
        HOME=os.environ["HOME"],
        CLAUDLOBBY_SRC=str(REPO_ROOT),
        REPS_COV="1",
        TMPDIR=os.environ.get("TMPDIR", "/tmp"),
    )
    result = subprocess.run(
        ["bash", str(HARNESS), "--dry-run", "--experiment", "coverage-honesty"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"coverage dry-run failed:\n{out}"
    assert "variant isolation OK: composed delta == clause block" in out
    assert "COVERAGE_AB_RESULT " in out
    assert "MANIPULATION CHECK" in out
    # Zero model calls: no auth was seeded in dry-run.
    assert ".credentials.json" not in out


def test_unknown_experiment_refused():
    result = subprocess.run(
        ["bash", str(HARNESS), "--dry-run", "--experiment", "nonsense"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "unknown --experiment" in result.stderr
