"""Tests for lib/ab-recoverability-judge.py — the #881 A2 semantic judge.

The judge fills the slot lib/ab-recoverability-scorer.py left open: it emits
judgements, the scorer consumes them, and neither decides anything. These tests
pin the properties that keep that true:

  1. Fail closed. An unreadable verdict is OMITTED, never guessed — so a flaky
     judge degrades coverage (which the scorer discloses as UNSCORED) instead of
     manufacturing a result.
  2. Calibration is per-boolean, never pooled. `in_full` is the easy axis and
     `unre_summarised` is the subtle one; a blended number can look healthy while
     the load-bearing half is a coin flip.
  3. Real calls are two-key, and the matrix key is NOT one of them.
  4. No threshold, no PASS/FAIL — same posture as the scorer.
  5. Round-trip: what the judge writes is what the scorer reads.

Loaded by path because lib/ filenames are hyphenated (ab-comms-verdict.py
precedent). No test here makes a model call.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
JUDGE_PATH = REPO_DIR / "lib" / "ab-recoverability-judge.py"
SCORER_PATH = REPO_DIR / "lib" / "ab-recoverability-scorer.py"
FIXTURES = REPO_DIR / "tests" / "fixtures"
PAIRS = FIXTURES / "a2_calibration_pairs.jsonl"
GOLD = FIXTURES / "a2_calibration_gold.jsonl"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


judge = _load(JUDGE_PATH, "ab_recoverability_judge")
scorer = _load(SCORER_PATH, "ab_recoverability_scorer")


def _pair(**kw):
    base = {
        "task": "T1",
        "variant": "with",
        "rep": 1,
        "compressed": "done. #954",
        "withheld": "x" * 200,
        "followup": "x" * 400,
    }
    base.update(kw)
    return base


class TestPrompt:
    def test_prompt_carries_all_three_inputs(self):
        p = judge.build_prompt(
            _pair(compressed="CMP-MARK", withheld="WHD-MARK", followup="FUP-MARK")
        )
        assert "CMP-MARK" in p and "WHD-MARK" in p and "FUP-MARK" in p

    def test_rubric_states_both_hard_cases_explicitly(self):
        """The two clauses that carry the calibration's hard cases must be in the
        rubric, not left to inference: paraphrase is not compression, and
        complete-but-terse still fails."""
        p = judge.build_prompt(_pair())
        assert "paraphrase" in p.lower()
        assert "(true, false)" in p


class TestParseVerdict:
    def test_plain_json(self):
        v = judge.parse_verdict('{"in_full": true, "unre_summarised": false}')
        assert v["in_full"] is True and v["unre_summarised"] is False

    def test_json_embedded_in_prose(self):
        v = judge.parse_verdict(
            'Sure!\n{"in_full": false, "unre_summarised": true, "notes":"n"}\nDone.'
        )
        assert v["in_full"] is False and v["unre_summarised"] is True

    def test_missing_axis_is_none(self):
        assert judge.parse_verdict('{"in_full": true}') is None

    def test_non_boolean_axis_is_none(self):
        """A 'yes' string must not be coerced — that is how a judge starts
        rubber-stamping."""
        assert judge.parse_verdict('{"in_full":"yes","unre_summarised":"yes"}') is None

    def test_garbage_is_none(self):
        assert judge.parse_verdict("I could not decide.") is None
        assert judge.parse_verdict("") is None
        assert judge.parse_verdict(None) is None


class TestFailClosed:
    def test_unreadable_verdict_is_omitted_not_guessed(self, monkeypatch):
        monkeypatch.setattr(judge, "call_judge", lambda *a, **k: (None, None))
        out = judge.judge_pairs([_pair(task="A"), _pair(task="B")], dry_run=False)
        assert out == []

    def test_omitted_pair_lands_unscored_in_the_scorer(self, monkeypatch):
        """The contract that makes fail-closed safe: a missing judgement becomes
        UNSCORED (None), never a failure."""
        monkeypatch.setattr(judge, "call_judge", lambda *a, **k: (None, None))
        pairs = [_pair(task="A")]
        judgements = judge.judge_pairs(pairs, dry_run=False)
        doc = scorer.compute(pairs, judgements)
        assert doc["pairs"][0]["recovered"] is None
        assert doc["coverage"]["n_unscored"] == 1

    def test_partial_batch_keeps_the_readable_ones(self, monkeypatch):
        seq = [
            ({"in_full": True, "unre_summarised": True, "notes": ""}, "m"),
            (None, None),
        ]
        monkeypatch.setattr(judge, "call_judge", lambda *a, **k: seq.pop(0))
        out = judge.judge_pairs([_pair(task="A"), _pair(task="B")], dry_run=False)
        assert [j["task"] for j in out] == ["A"]


class TestCalibration:
    def test_reports_per_axis_never_pooled(self):
        doc = judge.calibrate(
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": True,
                },
                {
                    "task": "B",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": False,
                },
            ],
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": False,
                },
                {
                    "task": "B",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": False,
                },
            ],
        )
        assert doc["per_axis"]["in_full"]["agreement"] == 1.0
        assert doc["per_axis"]["unre_summarised"]["agreement"] == 0.5
        assert "pooled" not in {k.lower() for k in doc["per_axis"]}
        # No single blended figure anywhere.
        assert "agreement" not in doc

    def test_unre_summarised_disagreement_is_flagged_loudly(self):
        doc = judge.calibrate(
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": True,
                }
            ],
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": False,
                }
            ],
        )
        assert doc["unre_summarised_clean"] is False
        out = judge.render_calibration(doc)
        assert "STOP" in out
        assert "decoration" in out

    def test_clean_unre_summarised_does_not_emit_the_stop_banner(self):
        doc = judge.calibrate(
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": False,
                    "unre_summarised": True,
                }
            ],
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": True,
                }
            ],
        )
        assert doc["unre_summarised_clean"] is True
        assert "STOP" not in judge.render_calibration(doc)

    def test_unjudged_gold_is_disclosed_not_counted(self):
        doc = judge.calibrate(
            [],
            [
                {
                    "task": "A",
                    "variant": "w",
                    "rep": 1,
                    "in_full": True,
                    "unre_summarised": True,
                    "case": "c",
                }
            ],
        )
        assert doc["per_axis"]["in_full"]["n_compared"] == 0
        assert [u["task"] for u in doc["unjudged"]] == ["A"]


class TestFixtures:
    def test_gold_is_balanced_on_both_axes(self):
        """A skewed prior would inflate agreement — 5/5 on each axis keeps a
        constant-answer judge at 50%, not 90%."""
        rows = [json.loads(x) for x in GOLD.read_text().splitlines() if x.strip()]
        assert len(rows) == 10
        for axis in ("in_full", "unre_summarised"):
            assert sum(1 for r in rows if r[axis]) == 5, axis

    def test_gold_covers_all_four_cells(self):
        rows = [json.loads(x) for x in GOLD.read_text().splitlines() if x.strip()]
        cells = {(r["in_full"], r["unre_summarised"]) for r in rows}
        assert cells == {(True, True), (True, False), (False, True), (False, False)}

    def test_pairs_and_gold_join_exactly(self):
        p = {json.loads(x)["task"] for x in PAIRS.read_text().splitlines() if x.strip()}
        g = {json.loads(x)["task"] for x in GOLD.read_text().splitlines() if x.strip()}
        assert p == g


class TestCli:
    def _run(self, *args, env=None):
        import os

        e = dict(os.environ)
        e.pop("AB_JUDGE_REAL", None)
        e.pop("AB_EVAL_REAL", None)
        if env:
            e.update(env)
        return subprocess.run(
            [sys.executable, str(JUDGE_PATH), *args],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            env=e,
        )

    def test_dry_run_exits_zero_and_makes_no_model_call(self):
        r = self._run(str(PAIRS), "--dry-run")
        assert r.returncode == 0, r.stderr
        assert "judged 10/10" in r.stdout

    def test_requires_an_explicit_mode(self):
        r = self._run(str(PAIRS))
        assert r.returncode != 0
        assert "--dry-run" in r.stderr

    def test_real_needs_the_second_key(self):
        r = self._run(str(PAIRS), "--real")
        assert r.returncode != 0
        assert "AB_JUDGE_REAL=1" in r.stderr

    def test_ab_eval_real_does_not_enable_real_judging(self):
        """The matrix key must never let this module spend."""
        r = self._run(str(PAIRS), "--real", env={"AB_EVAL_REAL": "1"})
        assert r.returncode != 0
        assert "AB_JUDGE_REAL=1" in r.stderr

    def test_module_never_reads_the_matrix_key(self):
        src = JUDGE_PATH.read_text()
        assert 'environ.get("AB_EVAL_REAL")' not in src
        assert "environ['AB_EVAL_REAL']" not in src

    def test_emits_no_verdict_language(self):
        src = JUDGE_PATH.read_text()
        for banned in ('"PASS"', '"FAIL"', '"INCONCLUSIVE"', "--threshold"):
            assert banned not in src, banned

    def test_dry_run_calibration_renders_per_axis(self):
        r = self._run(str(PAIRS), "--dry-run", "--calibrate", str(GOLD))
        assert r.returncode == 0, r.stderr
        assert "`in_full`" in r.stdout and "`unre_summarised`" in r.stdout
        assert "UNRE_SUMMARISED_DISAGREEMENTS=" in r.stdout

    def test_calibration_never_exits_nonzero_on_disagreement(self):
        """This module reports; it does not decide. A non-zero exit would be a
        verdict."""
        r = self._run(str(PAIRS), "--dry-run", "--calibrate", str(GOLD))
        assert "STOP" in r.stdout  # the stub judge does disagree
        assert r.returncode == 0

    def test_roundtrip_judge_output_feeds_the_scorer(self, tmp_path):
        out = tmp_path / "j.jsonl"
        r = self._run(str(PAIRS), "--dry-run", "--out", str(out))
        assert r.returncode == 0, r.stderr
        s = subprocess.run(
            [
                sys.executable,
                str(SCORER_PATH),
                str(PAIRS),
                "--judgements",
                str(out),
                "--out",
                str(tmp_path / "s.json"),
            ],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )
        assert s.returncode == 0, s.stderr
        doc = json.loads((tmp_path / "s.json").read_text())
        # Every pair resolved — nothing left UNSCORED, which is what proves the
        # judge filled the contract the scorer defined.
        assert doc["coverage"]["n_unscored"] == 0
        assert all(p["recovered"] is not None for p in doc["pairs"])
