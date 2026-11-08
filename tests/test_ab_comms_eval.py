"""Tests for harness/ab-comms-eval.sh — the #729 stage-C A/B comms-eval harness
scaffolding (F2/F4-independent).

Wrapped in Python so CI enforces it (the shell-tests-not-in-CI lesson). Two
surfaces:
  - ab-comms-eval.sh --dry-run: the full wiring end to end, zero model calls,
    no auth touch.
  - ab-comms-verdict.py: the pass-bar statistics in isolation, over hand-built
    results fixtures whose verdicts are deterministic (the bootstrap is seeded).

The default verdict is INCONCLUSIVE by construction: with no F2-ratified
threshold, or with the stub quality scorer, the harness can never emit PASS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import call_script_fn


REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPT = REPO_DIR / "harness" / "ab-comms-eval.sh"


@pytest.fixture
def _selected_built_cli(built_test_cli, monkeypatch):
    monkeypatch.setenv("CLAUDLOBBY_CLI", str(built_test_cli))


def test_private_compose_refuses_selected_data_root(tmp_path):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    (root / "state" / "selected-release.json").write_text("{}")
    result = subprocess.run(
        [sys.executable, str(REPO_DIR / "harness" / "compose.py"), "--root", str(root)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "active or unreadable release selection" in result.stderr
    assert not (root / "local").exists()


@pytest.mark.parametrize("marker", [None, "wrong-root", "symlink"])
def test_private_compose_refuses_unowned_data_root(tmp_path, marker):
    root = tmp_path / "data"
    bot = root / "runtime/bots/worker"
    bot.mkdir(parents=True)
    config = bot / "bot.conf"
    config.write_text("operator-owned\n")
    stamp = root / ".claudlobby-harness-root"
    if marker == "symlink":
        target = tmp_path / "marker"
        target.write_text(str(root))
        stamp.symlink_to(target)
    elif marker:
        stamp.write_text(marker)
    result = subprocess.run(
        [sys.executable, str(REPO_DIR / "harness/compose.py"), "--root", str(root)],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "marked disposable harness directory" in result.stderr
    assert config.read_text() == "operator-owned\n"


def test_private_compose_refuses_an_unbound_history_free_source(tmp_path):
    tree = tmp_path / "unbound"
    (tree / "harness").mkdir(parents=True)
    script = tree / "harness/compose.py"
    shutil.copy2(REPO_DIR / "harness/compose.py", script)
    root = tmp_path / "data"
    root.mkdir()
    (root / ".claudlobby-harness-root").write_text(str(root.resolve()))
    result = subprocess.run([sys.executable, str(script), "--root", str(root)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "no built artifact or Git revision identity" in result.stderr
    assert not (root / "runtime").exists()


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


def _pair(task, rep, ps_wo, ps_wi, cw_wo, cw_wi, quality=None):
    q = {"gate": "pass"} if quality is None else quality
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


VERDICT_MODULE = REPO_DIR / "harness" / "ab-comms-verdict.py"


def _verdict_run(tmp_path, name, rows, *flags):
    # Exercise the standalone verdict module directly (reps-now == reps-max so a
    # straddle resolves to its terminal INCONCLUSIVE, never PASS). Returns the run
    # and the --out path, which a refused verdict leaves unwritten.
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
    return r, out


def _module_verdict(tmp_path, name, rows, *flags):
    r, out = _verdict_run(tmp_path, name, rows, *flags)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(out.read_text())


# The run pins a real verdict needs, by the name a refusal gives each.
_RUN_PINS = {
    "battery_sha256": "b" * 64,
    "harness_revision": "c" * 40,
    "composed_sha256.without": "d" * 64,
    "composed_sha256.with": "e" * 64,
}


def _pin_flags(**changes):
    """Flags for _RUN_PINS, each change applied (None drops that pin)."""
    flags = []
    for name, value in {**_RUN_PINS, **changes}.items():
        if value is None:
            continue
        if name.startswith("composed_sha256."):
            flags += ["--composed-sha256", name.split(".", 1)[1] + "=" + value]
        else:
            flags += ["--" + name.replace("_", "-"), value]
    return flags


def _sourced_fn(call, **values):
    """Run one harness function, sourced, with its arguments passed as variables."""
    return call_script_fn(SCRIPT, call, env={**os.environ, **values}).strip()


@pytest.mark.usefixtures("_selected_built_cli")
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

    def test_stub_scored_rows_never_pass(self):
        # The synthetic tokens clear --threshold 0.15 on both co-primary axes, so
        # the verdict read PASS while the stub scored quality "pass". The stub
        # now scores nothing, and a quality nobody scored never passes.
        r = _run(
            "--dry-run", "--keep", "--tasks", "1", "--reps", "2", "--threshold", "0.15"
        )
        assert r.returncode == 0, r.stderr + r.stdout
        assert "ALL SCAFFOLDING CHECKS PASSED" in r.stdout
        root = _kv(r.stdout, "ROOT")
        try:
            body = Path(_kv(r.stdout, "RESULTS")).read_text()
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
            assert rows and all(row["quality"]["gate"] == "unscored" for row in rows)
            d = json.loads(Path(_kv(r.stdout, "VERDICT")).read_text())
            assert d["pins"]["threshold"] == 0.15
            (t,) = d["per_task"]
            # The bar is cleared on both axes, so only the quality holds it back.
            assert t["protocol_sensitive"]["ci"][0] >= 0.15
            assert t["cost_weighted_total"]["ci"][0] >= 0.0
            assert (t["verdict"], t["reason"], t["quality"]) == (
                "INCONCLUSIVE",
                "quality unscored",
                "unscored",
            )
            assert d["overall"] == "INCONCLUSIVE"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_verdict_pins_its_battery_harness_and_arms(self):
        # The harness checks that each pin is there and that the two arms differ.
        # Two dry runs differ only by their temporary root, so they pin the same.
        head = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        runs = []
        for _ in range(2):
            r = _run("--dry-run", "--keep", "--tasks", "2", "--reps", "1")
            assert r.returncode == 0, r.stderr + r.stdout
            assert "PASS  verdict.json pins the battery" in r.stdout
            root = Path(_kv(r.stdout, "ROOT"))
            try:
                pins = json.loads(Path(_kv(r.stdout, "VERDICT")).read_text())["pins"]
                battery = root / "battery.json"
                digest = hashlib.sha256(battery.read_bytes()).hexdigest()
                assert pins["battery_sha256"] == digest
                tasks = json.loads(battery.read_text())["tasks"]
                assert [t["id"] for t in tasks] == ["T1", "T2"]
                keys = ("battery_sha256", "harness_revision", "composed_sha256")
                runs.append({k: pins[k] for k in keys})
            finally:
                shutil.rmtree(root, ignore_errors=True)
        assert runs[0] == runs[1]
        revision = runs[0]["harness_revision"]
        assert revision in ((head, head + "-dirty") if head else (None,))

    def test_no_auth_files_created_in_config_dirs(self):
        r = _run("--dry-run", "--keep", "--tasks", "1", "--reps", "1")
        assert r.returncode == 0, r.stderr + r.stdout
        root = Path(_kv(r.stdout, "ROOT"))
        try:
            creds = list(root.glob("config-*/**/.credentials.json"))
            assert creds == [], f"auth leaked into a config dir: {creds}"
        finally:
            shutil.rmtree(root, ignore_errors=True)


@pytest.mark.usefixtures("_selected_built_cli")
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

    @pytest.mark.parametrize("threshold", [None, "0.15", "0.5"])
    def test_unscored_quality_is_inconclusive_whatever_the_threshold(
        self, tmp_path, threshold
    ):
        # A 20% / 30% reduction clears 0.15 and misses 0.5, and with no threshold
        # the sentinel applies: an unscored row is judged in none of the three.
        rows = []
        for rep in (1, 2, 3):
            rows += _pair("T1", rep, 1000, 800, 1000, 700, quality={"gate": "unscored"})
        flags = () if threshold is None else ("--threshold", threshold)
        d = _module_verdict(tmp_path, "unscored", rows, *flags)
        assert d["overall"] == "INCONCLUSIVE"
        (t,) = d["per_task"]
        assert (t["verdict"], t["reason"], t["quality"]) == (
            "INCONCLUSIVE",
            "quality unscored",
            "unscored",
        )


class TestVerdictPins:
    """The run pins: what the verdict records, and the refusal of a real verdict
    that lacks one."""

    @staticmethod
    def _rows():
        rows = []
        for rep in (1, 2, 3):
            rows += _pair("T1", rep, 1000, 800, 1000, 700)  # 20% ps, 30% cwt
        return rows

    def test_a_real_verdict_records_every_pin_and_is_judged_as_before(self, tmp_path):
        flags = ("--real", "--threshold", "0.1", *_pin_flags())
        d = _module_verdict(tmp_path, "real", self._rows(), *flags)
        p = d["pins"]
        assert p["battery_sha256"] == "b" * 64
        assert p["harness_revision"] == "c" * 40
        assert p["composed_sha256"] == {"without": "d" * 64, "with": "e" * 64}
        assert d["overall"] == "PASS"

    @pytest.mark.parametrize(
        "pin, value",
        [
            ("battery_sha256", None),
            ("battery_sha256", "unknown"),
            ("harness_revision", None),
            ("harness_revision", "c" * 40 + "-dirty"),
            ("harness_revision", "c" * 7),
            ("composed_sha256.without", None),
            ("composed_sha256.with", None),
        ],
    )
    def test_a_real_verdict_missing_a_pin_is_refused_naming_it(
        self, tmp_path, pin, value
    ):
        flags = ("--real", "--threshold", "0.1", *_pin_flags(**{pin: value}))
        r, out = _verdict_run(tmp_path, "refused", self._rows(), *flags)
        assert r.returncode == 3, r.stderr + r.stdout
        # Each gap reads "name (value)", so ".with (" is not ".without (".
        assert [p for p in _RUN_PINS if p + " (" in r.stderr] == [pin], r.stderr
        assert not out.exists()

    def test_without_real_an_unpinned_verdict_is_still_computed(self, tmp_path):
        # A dry run, or a verdict recomputed from a results file, records a
        # missing pin as null rather than refusing.
        d = _module_verdict(tmp_path, "unpinned", self._rows(), "--threshold", "0.1")
        p = d["pins"]
        assert (p["battery_sha256"], p["harness_revision"]) == (None, None)
        assert p["composed_sha256"] == {}
        assert d["overall"] == "PASS"


class TestRunPinFunctions:
    """The harness functions that take the pins, run sourced."""

    def test_the_composed_pin_covers_what_a_session_reads(self, tmp_path):
        def pin(name, token, claude="rules", skill="steps", conf="a=1"):
            top = tmp_path / name
            (top / ".claude" / "skills").mkdir(parents=True)
            (top / "CLAUDE.md").write_text(f"{claude} for /tmp/{token}/runtime\n")
            (top / ".claude" / "settings.local.json").write_text(
                f'{{"command": "/tmp/.harness-installation-{token}/bin/claudlobby"}}\n'
            )
            linked = tmp_path / f"{name}-library" / "fleet-ops"
            linked.mkdir(parents=True)
            (linked / "SKILL.md").write_text(skill + "\n")
            (top / ".claude" / "skills" / "fleet-ops").symlink_to(linked)
            (top / "bot.conf").write_text(f"{conf} /tmp/{token}\n")
            (top / "abev.x.service").write_text(conf + "\n")
            (top / "abev.x.plist").write_text(conf + "\n")
            return _sourced_fn(
                'composed_sha256 "$PIN_DIR" "$PIN_TOKEN"',
                PIN_DIR=str(top),
                PIN_TOKEN=token,
            )

        base = pin("a", "ab-comms-eval.AAAAAA")
        assert re.fullmatch("[0-9a-f]{64}", base)
        # Another run's root, and a link to another copy of the same skill.
        assert pin("b", "ab-comms-eval.BBBBBB") == base
        # bot.conf and the unit files are the supervisor's.
        assert pin("c", "ab-comms-eval.AAAAAA", conf="a=2") == base
        # The composed text, and a file a link points at, are the session's.
        assert pin("d", "ab-comms-eval.AAAAAA", claude="rule") != base
        assert pin("e", "ab-comms-eval.AAAAAA", skill="step") != base

    def test_the_harness_revision_is_the_trees_commit(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(tree), "-c", "user.name=Example",
                 "-c", "user.email=example@example.com",
                 "-c", "commit.gpgsign=false", *args],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

        def revision(d):
            return _sourced_fn('harness_revision "$PIN_DIR"', PIN_DIR=str(d))

        git("init", "-q")
        assert revision(tree) == ""  # no commit yet
        (tree / "f").write_text("1\n")
        git("add", "f")
        git("commit", "-q", "-m", "one")
        head = git("rev-parse", "HEAD")
        assert revision(tree) == head
        (tree / "f").write_text("2\n")
        assert revision(tree) == head + "-dirty"
        # A directory inside that repository is not a tree with a commit of its own.
        (tree / "export").mkdir()
        assert revision(tree / "export") == ""
