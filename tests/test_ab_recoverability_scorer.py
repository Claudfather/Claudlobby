"""Tests for lib/ab-recoverability-scorer.py — the #881 A2 recoverability axis.

A2 is the anti-lossy guard: A1 is gamed by saying less, so A2 asks whether the
detail that was compressed out still comes back on a follow-up, in full and
unre-summarised. These tests pin the four behaviours that make it a guard rather
than a rubber stamp:

  1. A re-summarised follow-up FAILS. This is the load-bearing case — the rule
     A2 gates says an explicit request for detail is never re-summarized, and a
     shorter summary of the summary is structurally indistinguishable from a
     pass, so only the semantic tier catches it.
  2. An unresolved pair is UNSCORED (None), never a failure. A missing judge
     must not be able to manufacture an A2 regression.
  3. The structural tier refutes without spend, and only ever refutes.
  4. No verdict is emitted. A2's bar is not ratified; the module emits a
     comparable score and stops.

The module is loaded by path because lib/ filenames are hyphenated and so are
not importable as modules — the ab-comms-verdict.py precedent.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_DIR / "lib" / "ab-recoverability-scorer.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "ab_recoverability_scorer", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scorer = _load_module()

LONG = "x" * 400
SHORT = "y" * 40


def _pair(
    task="T1", variant="with", rep=1, compressed=SHORT, withheld=LONG, followup=LONG
):
    return {
        "task": task,
        "variant": variant,
        "rep": rep,
        "compressed": compressed,
        "withheld": withheld,
        "followup_prompt": "show me the detail",
        "followup": followup,
    }


def _judgement(task="T1", variant="with", rep=1, in_full=True, unre_summarised=True):
    return {
        "task": task,
        "variant": variant,
        "rep": rep,
        "in_full": in_full,
        "unre_summarised": unre_summarised,
        "judge_model": "test-stub",
    }


class TestScorePair:
    def test_full_and_unresummarised_recovers(self):
        r = scorer.score_pair(_pair(), _judgement())
        assert r["recovered"] is True
        assert r["in_full"] is True and r["unre_summarised"] is True

    def test_resummarised_followup_fails(self):
        """The load-bearing case: it expanded, but into a summary of the summary."""
        r = scorer.score_pair(
            _pair(
                followup="a much shorter gloss of the detail, but longer than the msg"
                * 2
            ),
            _judgement(in_full=True, unre_summarised=False),
        )
        assert r["recovered"] is False
        assert "re-summarised" in r["reason"]

    def test_not_in_full_fails_and_is_named_distinctly(self):
        r = scorer.score_pair(_pair(), _judgement(in_full=False, unre_summarised=True))
        assert r["recovered"] is False
        assert "in full" in r["reason"]

    def test_structural_refutation_needs_no_judgement(self):
        """A follow-up no longer than the message cannot have returned the detail."""
        r = scorer.score_pair(_pair(compressed=LONG, followup=SHORT), judgement=None)
        assert r["recovered"] is False
        assert r["expanded"] is False
        assert r["reason"].startswith("structural:")

    def test_missing_judgement_is_unscored_not_failed(self):
        r = scorer.score_pair(_pair(), judgement=None)
        assert r["recovered"] is None
        assert "unscored" in r["reason"]

    def test_partial_judgement_is_unscored_not_failed(self):
        r = scorer.score_pair(_pair(), {"task": "T1", "variant": "with", "rep": 1})
        assert r["recovered"] is None

    def test_structural_only_never_consults_the_judge(self):
        r = scorer.score_pair(_pair(), _judgement(), structural_only=True)
        assert r["recovered"] is None
        assert r["in_full"] is None
        assert "structural-only" in r["reason"]

    def test_address_is_recorded_but_not_gating(self):
        """The ratified A2 is behavioural — a missing address must not fail a
        pair that demonstrably recovered."""
        r = scorer.score_pair(_pair(compressed="no pointer here at all"), _judgement())
        assert r["address_present"] is False
        assert r["recovered"] is True

    def test_address_detection_finds_the_conventional_forms(self):
        assert scorer.has_address("done. PR #954")
        assert scorer.has_address("see https://github.com/o/r/pull/1")
        assert scorer.has_address("detail in data/worklog/t-1785-ab.md")
        assert scorer.has_address("lib/ab-comms-verdict.py has it")
        assert not scorer.has_address("all finished, looks good")


class TestAggregate:
    def test_unscored_excluded_from_rate_and_disclosed(self):
        pairs = [
            _pair(rep=1),  # judged -> recovers
            _pair(rep=2),  # unjudged -> unscored
        ]
        doc = scorer.compute(pairs, [_judgement(rep=1)])
        g = doc["per_group"]["T1/with"]
        assert g["n_pairs"] == 2
        assert g["n_scored"] == 1
        assert g["n_unscored"] == 1
        assert g["recovery_rate"] == 1.0  # NOT 0.5 — the unscored pair is excluded
        assert doc["coverage"]["n_unscored"] == 1

    def test_rate_is_none_not_zero_when_nothing_scored(self):
        """An absent score must not read as total failure."""
        doc = scorer.compute([_pair()], [])
        assert doc["per_group"]["T1/with"]["recovery_rate"] is None

    def test_control_vs_treatment_delta_and_direction(self):
        pairs = [
            _pair(task="T2", variant="without", rep=1),
            _pair(task="T2", variant="with", rep=1),
        ]
        judgements = [
            _judgement(
                task="T2", variant="without", rep=1, in_full=True, unre_summarised=True
            ),
            _judgement(
                task="T2", variant="with", rep=1, in_full=True, unre_summarised=False
            ),
        ]
        doc = scorer.compute(pairs, judgements)
        d = doc["control_vs_treatment"]["T2"]
        assert d["without"] == 1.0
        assert d["with"] == 0.0
        assert d["delta"] == -1.0
        assert d["direction"] == "degraded"

    def test_delta_is_none_when_an_arm_is_missing(self):
        doc = scorer.compute(
            [_pair(task="T5", variant="with")], [_judgement(task="T5")]
        )
        assert doc["control_vs_treatment"]["T5"]["delta"] is None
        assert doc["control_vs_treatment"]["T5"]["direction"] is None

    def test_emits_a_score_and_no_verdict(self):
        """A2's bar is unratified — the module must not invent one."""
        doc = scorer.compute([_pair()], [_judgement()])
        assert doc["bar"] is None
        assert "NOT RATIFIED" in doc["bar_status"]
        blob = json.dumps(doc)
        for banned in ("PASS", "FAIL", "INCONCLUSIVE"):
            assert banned not in blob


class TestCli:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *args],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )

    def test_dry_run_exits_zero_and_scores(self):
        r = self._run("--dry-run")
        assert r.returncode == 0, r.stderr
        assert "A2 — recoverability" in r.stdout
        assert "NOT RATIFIED" in r.stdout

    def test_dry_run_exercises_every_branch(self, tmp_path):
        out = tmp_path / "scores.json"
        r = self._run("--dry-run", "--out", str(out))
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text())
        reasons = [p["reason"] for p in doc["pairs"]]
        assert any(x.startswith("structural:") for x in reasons)
        assert any("re-summarised" in x for x in reasons)
        assert any("unscored" in x for x in reasons)
        assert any(p["recovered"] is True for p in doc["pairs"])
        assert any(p["recovered"] is False for p in doc["pairs"])
        assert any(p["recovered"] is None for p in doc["pairs"])

    def test_dry_run_makes_no_network_or_model_call(self, tmp_path):
        """CI-safe posture: the dry-run path is pure computation. Proven by
        running it with no network access available to the child."""
        script = (
            "import socket,sys,runpy\n"
            "socket.socket = None\n"
            "sys.argv = ['s', '--dry-run']\n"
            "runpy.run_path(%r, run_name='__main__')\n" % str(MODULE_PATH)
        )
        r = subprocess.run(
            [sys.executable, "-c", script], cwd=REPO_DIR, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr
        assert "A2 — recoverability" in r.stdout

    def test_emit_rows_uses_the_a1_join_keys(self, tmp_path):
        results = tmp_path / "results.jsonl"
        results.write_text('{"task":"T3","variant":"with","rep":1,"len_chars":10}\n')
        r = self._run("--dry-run", "--emit-rows", str(results))
        assert r.returncode == 0, r.stderr
        rows = [json.loads(x) for x in results.read_text().splitlines() if x.strip()]
        a2 = [x for x in rows if x.get("axis") == "A2_recoverability"]
        assert a2, "no A2 rows appended"
        for row in a2:
            assert {"task", "variant", "rep"} <= set(row)
        # The pre-existing A1 row is preserved, not clobbered.
        assert any("len_chars" in x for x in rows)

    def test_reads_pairs_and_judgements_from_files(self, tmp_path):
        pairs = tmp_path / "pairs.jsonl"
        judgements = tmp_path / "j.jsonl"
        pairs.write_text(json.dumps(_pair()) + "\n")
        judgements.write_text(json.dumps(_judgement()) + "\n")
        out = tmp_path / "s.json"
        r = self._run(str(pairs), "--judgements", str(judgements), "--out", str(out))
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text())
        assert doc["pairs"][0]["recovered"] is True

    def test_structural_only_flag_skips_semantics(self, tmp_path):
        pairs = tmp_path / "pairs.jsonl"
        judgements = tmp_path / "j.jsonl"
        pairs.write_text(json.dumps(_pair()) + "\n")
        judgements.write_text(json.dumps(_judgement()) + "\n")
        out = tmp_path / "s.json"
        r = self._run(
            str(pairs),
            "--judgements",
            str(judgements),
            "--structural-only",
            "--out",
            str(out),
        )
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text())
        assert doc["structural_only"] is True
        assert doc["pairs"][0]["recovered"] is None

    def test_requires_pairs_without_dry_run(self):
        r = self._run()
        assert r.returncode != 0
        assert "required" in (r.stderr + r.stdout)

    def test_does_not_reference_the_real_mode_key(self):
        """This module never spends, so it must not carry AB_EVAL_REAL."""
        assert "AB_EVAL_REAL" not in MODULE_PATH.read_text()
