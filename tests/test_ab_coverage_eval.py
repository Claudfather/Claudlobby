"""#866 coverage-honesty A/B — tests for the ab-comms-eval experiment mode.

Hermetic and CI-safe throughout: the dry-run path composes both variants with
real `claudlobby generate`, the frozen regexes are driven through the REAL
measurement engine (the sourced `cov_count_re`, i.e. `grep -oiE` — never a
re-implementation in another regex engine), and the analyzer's decision
branches are exercised in-process. The pre-registered REAL run is an operator
action.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


from tests.conftest import load_lib_module

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "lib" / "ab-comms-eval.sh"

verdict = load_lib_module("ab-coverage-verdict")


def _sourced(body: str, *args: str) -> str:
    """Run <body> in a shell that has SOURCED the harness (its source-guard
    stops before any side-effectful mode logic), so tests drive the real
    functions and the real frozen constants."""
    # Positional args are saved around the source: the harness's arg parser
    # must see none, but the body still needs them afterwards.
    script = f'_a=("$@"); set --; . "{HARNESS}"; set -- "${{_a[@]}}"; {body}'
    r = subprocess.run(
        ["bash", "-c", script, "_", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _count(regex_var: str, text: str, tmp_path: Path) -> int:
    f = tmp_path / "probe.txt"
    f.write_text(text, encoding="utf-8")
    out = _sourced(f'cov_count_re "${{{regex_var}}}" "$1"', str(f))
    return int(out.strip() or 0)


class TestFrozenRegexes:
    """Behavioral pins on the #866-frozen regexes through the AS-RUN engine."""

    def test_verif_regex_matches_coverage_language(self, tmp_path):
        assert (
            _count(
                "COV_VERIF_RE",
                "I scanned 40 of 212 files; the rest went unchecked.",
                tmp_path,
            )
            >= 3
        )
        assert _count("COV_VERIF_RE", "Verified and checked the output.", tmp_path) >= 2
        assert _count("COV_VERIF_RE", "The weather is nice today.", tmp_path) == 0

    def test_disclosure_regex_matches_bound_statements(self, tmp_path):
        assert (
            _count("COV_DISCLOSE_RE", "found nothing in what I checked", tmp_path) >= 1
        )
        assert (
            _count("COV_DISCLOSE_RE", "reviewed the first 10 files only", tmp_path) >= 1
        )
        assert (
            _count("COV_DISCLOSE_RE", "all clear, no problems anywhere", tmp_path) == 0
        )


def test_unknown_arg_and_unknown_experiment_refused():
    r = subprocess.run(
        ["bash", str(HARNESS), "--experiment", "nonsense"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Parse-time validation: refused on the DEFAULT path, not only under
    # --dry-run (the late check was unreachable there).
    assert r.returncode == 1
    assert "unknown --experiment" in r.stderr


class TestAnalyzer:
    @staticmethod
    def _row(
        task, variant, rep, length, matches, disclosure=False, valid=True, arm=None
    ):
        row = {
            "task": task,
            "variant": variant,
            "rep": rep,
            "len_chars": length,
            "verif_matches": matches,
            "disclosure": disclosure,
            "valid": valid,
        }
        if arm is not None:
            row["arm"] = arm
        return row

    def _matrix(self, with_len_bounded, with_len_ctl, reps=3, arm_field=True):
        rows = []
        b = "bounded" if arm_field else None
        c = "control" if arm_field else None
        for rep in range(1, reps + 1):
            for task in ("T1", "T2"):
                rows.append(self._row(task, "without", rep, 1000, 1, arm=b))
                rows.append(
                    self._row(task, "with", rep, with_len_bounded, 2, True, arm=b)
                )
            rows.append(self._row("T3", "without", rep, 500, 1, arm=c))
            rows.append(self._row("T3", "with", rep, with_len_ctl, 1, arm=c))
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

    def test_negative_direction_named_not_mislabeled(self):
        # The clause SHORTENING output is outside the registered branches; the
        # verdict must say so rather than print the includes-0 text falsely.
        text, _ = verdict.analyze(self._matrix(with_len_bounded=500, with_len_ctl=500))
        assert "DIRECTIONAL-NEGATIVE" in text
        assert "includes 0" not in text

    def test_inconclusive_branch(self):
        rows = self._matrix(with_len_bounded=1000, with_len_ctl=500)
        rows[1]["len_chars"] = 990  # tiny mixed jitter around zero
        text, _ = verdict.analyze(rows)
        assert "VERDICT: INCONCLUSIVE" in text

    def test_arm_fallback_analyzes_legacy_rows(self):
        # Rows from the first published run predate the arm field; the
        # task-name fallback must keep them analyzable with identical shape.
        text, rc = verdict.analyze(
            self._matrix(with_len_bounded=2000, with_len_ctl=500, arm_field=False)
        )
        assert rc == 0
        assert "n_pairs=6" in text

    def test_zero_baseline_pairs_dropped_count_visible(self):
        rows = self._matrix(with_len_bounded=2000, with_len_ctl=500)
        for r in rows:
            if r["variant"] == "without":
                r["verif_matches"] = 0  # density baseline zero -> pair undefined
        text, _ = verdict.analyze(rows)
        assert "6 pair(s) dropped" in text

    def test_invalid_rows_disclosed_and_excluded(self):
        rows = self._matrix(with_len_bounded=2000, with_len_ctl=500)
        rows[0]["valid"] = False
        text, rc = verdict.analyze(rows)
        assert rc == 0
        assert "1 invalid" in text
        assert "T1/without/rep1" in text

    def test_no_pairs_is_failure(self):
        _, rc = verdict.analyze([self._row("T1", "without", 1, 100, 0)])
        assert rc == 1

    def test_machine_line_contract_and_ci_level(self):
        text, _ = verdict.analyze(self._matrix(with_len_bounded=2000, with_len_ctl=500))
        line = [ln for ln in text.splitlines() if ln.startswith("COVERAGE_AB_RESULT ")]
        assert line
        assert "n_pairs=6" in line[0]
        assert f"seed={verdict.BOOTSTRAP_SEED}" in line[0]
        # The registration text says 95%; the machine key must carry the real level.
        assert f"ci{verdict.CI_PCT}=" in line[0]
        assert verdict.CI_PCT == 95

    def test_t3_absent_reports_none_not_zero(self):
        rows = [
            self._row("T1", "without", 1, 1000, 1, arm="bounded"),
            self._row("T1", "with", 1, 2000, 2, arm="bounded"),
        ]
        text, _ = verdict.analyze(rows)
        assert "t3_median=none" in text


def test_dry_run_end_to_end(tmp_path):
    env = {**os.environ, "HOME": os.environ["HOME"]}
    result = subprocess.run(
        [
            "bash",
            str(HARNESS),
            "--dry-run",
            "--experiment",
            "coverage-honesty",
            "--reps",
            "1",
            "--keep",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"coverage dry-run failed:\n{out}"
    assert "variant isolation" in out
    assert "COVERAGE_AB_RESULT " in out
    assert "MANIPULATION CHECK" in out
    root = next(
        (ln.split("=", 1)[1] for ln in out.splitlines() if ln.startswith("ROOT=")), None
    )
    assert root, f"no ROOT= line:\n{out}"
    try:
        # Zero model calls: no auth seeded into either variant config dir —
        # asserted on the filesystem, not on stdout (a filename grep of the
        # output is vacuously true).
        assert not (Path(root) / "with" / "config" / ".credentials.json").exists()
        assert not (Path(root) / "without" / "config" / ".credentials.json").exists()
        # The dry path seeds no corpus (real-run material only).
        assert not (
            Path(root) / "without" / "runtime" / "bots" / "cov-probe" / "data" / "logs"
        ).exists()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_token_efficiency_dry_run_untouched():
    result = subprocess.run(
        ["bash", str(HARNESS), "--dry-run", "--tasks", "1", "--reps", "1"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 0, f"#729 dry-run regressed:\n{out}"
    assert "ALL SCAFFOLDING CHECKS PASSED" in out
