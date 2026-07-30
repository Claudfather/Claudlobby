"""Tests for lib/ab-comms-eval.sh — the #729 stage-C A/B comms-eval harness
scaffolding (F2/F4-independent).

Wrapped in Python so CI enforces it (the shell-tests-not-in-CI lesson). Two
surfaces:
  - ab-comms-eval.sh --dry-run: the full wiring end to end, zero model calls,
    no auth touch.
  - ab-comms-verdict.py: the pass-bar statistics in isolation, over hand-built
    results fixtures whose verdicts are deterministic (the bootstrap is seeded).

The default verdict is INCONCLUSIVE by construction: with no F2-ratified
threshold, the harness can never emit PASS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPT = REPO_DIR / "lib" / "ab-comms-eval.sh"


def _run(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )


def _kv(stdout, key):
    for line in stdout.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key}= not found in harness output")


def _pair(task, rep, ps_wo, ps_wi, cw_wo, cw_wi):
    q = {"gate": "pass"}
    return [
        {
            "task": task,
            "variant": "without",
            "rep": rep,
            "protocol_sensitive": ps_wo,
            "cost_weighted_total": cw_wo,
            "model": "m",
            "quality": q,
            "mech_ok": True,
        },
        {
            "task": task,
            "variant": "with",
            "rep": rep,
            "protocol_sensitive": ps_wi,
            "cost_weighted_total": cw_wi,
            "model": "m",
            "quality": q,
            "mech_ok": True,
        },
    ]


VERDICT_MODULE = REPO_DIR / "lib" / "ab-comms-verdict.py"


def _module_verdict(tmp_path, name, rows, *flags):
    # Exercise the standalone verdict module directly (reps-now == reps-max so a
    # straddle resolves to its terminal INCONCLUSIVE, never PASS).
    f = tmp_path / f"{name}.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / f"{name}.verdict.json"
    r = subprocess.run(
        [
            "python3",
            str(VERDICT_MODULE),
            str(f),
            "--out",
            str(out),
            "--reps-now",
            "5",
            "--reps-max",
            "5",
            *flags,
        ],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(out.read_text())


class TestDryRun:
    def test_exits_zero_all_checks_pass_verdict_inconclusive(self):
        r = _run("--dry-run", "--keep", "--tasks", "2", "--reps", "2")
        assert r.returncode == 0, r.stderr + r.stdout
        assert "ALL SCAFFOLDING CHECKS PASSED" in r.stdout
        assert " FAIL " not in r.stdout  # every scaffolding check passed
        assert "no CLAUDE_CONFIG_DIR auth touched (zero model calls)" in r.stdout
        root = _kv(r.stdout, "ROOT")
        results = Path(_kv(r.stdout, "RESULTS"))
        verdict = Path(_kv(r.stdout, "VERDICT"))
        try:
            body = results.read_text()
            assert '"variant":"with"' in body and '"variant":"without"' in body
            d = json.loads(verdict.read_text())
            # INCONCLUSIVE by construction; both axes + pins present; no ratified T.
            assert d["overall"] == "INCONCLUSIVE"
            assert d["pins"]["threshold"] is None
            assert d["pins"]["weights"]["output"] == 5.0
            axes = d["per_task"][0]
            assert "protocol_sensitive" in axes and "cost_weighted_total" in axes
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_no_auth_files_created_in_config_dirs(self):
        r = _run("--dry-run", "--keep", "--tasks", "1", "--reps", "1")
        assert r.returncode == 0, r.stderr + r.stdout
        root = Path(_kv(r.stdout, "ROOT"))
        try:
            creds = list(root.glob("config-*/**/.credentials.json"))
            assert creds == [], f"auth leaked into a config dir: {creds}"
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestChannelBrevityDryRun:
    """#728 P1 gate wiring (--experiment channel-brevity), CI-safe dry path."""

    def test_dry_run_isolates_variants_and_reaches_verdict(self):
        r = _run(
            "--dry-run", "--keep", "--experiment", "channel-brevity", "--reps", "2"
        )
        assert r.returncode == 0, r.stderr + r.stdout
        # The run-blocking isolation assertion must have PASSED (it caught a
        # real leak on its first execution: report-back's {{CLAUDLOBBY_ROOT}}
        # interpolation diverging per sub-root — the baseline is chosen
        # placeholder-clean so the composed delta is exactly the component).
        assert "variant isolation: composed delta == component block" in r.stdout
        assert " FAIL " not in r.stdout
        # Deterministic synth exercises the SUPPORTED branch end to end:
        # channel shorter WITH, control identical, facts held everywhere.
        assert "VERDICT: SUPPORTED" in r.stdout
        assert "CHANNEL_BREVITY_AB_RESULT" in r.stdout
        assert "ci_pct=95" in r.stdout
        root = Path(_kv(r.stdout, "ROOT"))
        try:
            body = Path(_kv(r.stdout, "RESULTS")).read_text()
            assert '"arm":"channel"' in body and '"arm":"control"' in body
            assert '"facts_ok":true' in body
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_dry_run_touches_no_auth(self):
        r = _run(
            "--dry-run", "--keep", "--experiment", "channel-brevity", "--reps", "1"
        )
        assert r.returncode == 0, r.stderr + r.stdout
        root = Path(_kv(r.stdout, "ROOT"))
        try:
            creds = list(root.glob("**/.credentials.json"))
            assert creds == [], f"auth leaked into the fixture: {creds}"
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestComputeVerdict:
    def test_pass_when_both_axes_clear_the_bar(self, tmp_path):
        rows = []
        for rep in (1, 2, 3):
            rows += _pair("T1", rep, 1000, 800, 1000, 700)  # 20% ps, 30% cwt
        d = _module_verdict(tmp_path, "pass", rows, "--threshold", "0.1")
        assert d["overall"] == "PASS"

    def test_fail_below_threshold(self, tmp_path):
        rows = []
        for rep in (1, 2, 3):
            rows += _pair("T1", rep, 1000, 1000, 1000, 700)  # 0% ps reduction < 0.1
        d = _module_verdict(tmp_path, "fail", rows, "--threshold", "0.1")
        assert d["overall"] == "FAIL"

    def test_straddle_never_rounds_up_to_pass(self, tmp_path):
        # ps reductions {0.5, 0.5, -0.3, -0.3} -> CI straddles 0.1 -> INCONCLUSIVE
        rows = []
        for i, ps_wi in enumerate((500, 500, 1300, 1300), 1):
            rows += _pair("T1", i, 1000, ps_wi, 1000, 700)
        d = _module_verdict(tmp_path, "straddle", rows, "--threshold", "0.1")
        assert d["overall"] == "INCONCLUSIVE"

    def test_default_no_threshold_is_inconclusive(self, tmp_path):
        rows = []
        for rep in (
            1,
            2,
            3,
        ):  # a huge reduction, but no ratified T -> still INCONCLUSIVE
            rows += _pair("T1", rep, 1000, 100, 1000, 100)
        d = _module_verdict(tmp_path, "no_t", rows)
        assert d["overall"] == "INCONCLUSIVE"
