"""The two clauDNA jobs in the conformance workflow (#2539, #2550).

``export-contract-gate`` checks out clauDNA at the ref in ``claudlobby/contracts/claudna.ref`` and
``rename-map-gate`` checks out its default branch. Both are a hosted convenience that a clauDNA
outage must not block, so a clauDNA that does not answer skips with a notice. A clauDNA that answers
must be checked out: a ref it does not hold fails the checkout, and with it the job, instead of
reading green while checking nothing. A checkout given an empty ref takes clauDNA's default branch,
so the step that reads the ref fails the job when the file is missing or empty. These tests read the
workflow, run each job's reachability probe against a stand-in ``git`` and run the ref step on its
own; the real fetch is the CI jobs'.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
JOBS = ("export-contract-gate", "rename-map-gate")
PROBE = "git ls-remote --exit-code https://github.com/Claudfather/clauDNA.git HEAD"
REF_FILE = "claudlobby/contracts/claudna.ref"


def _steps(job: str) -> list[dict]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/conformance.yml").read_text())
    return workflow["jobs"][job]["steps"]


def _checkout(steps: list[dict]) -> int:
    found = [i for i, s in enumerate(steps) if s.get("with", {}).get("repository") == "Claudfather/clauDNA"]
    assert len(found) == 1, found
    return found[0]


def _probe(steps: list[dict]) -> int:
    found = [i for i, s in enumerate(steps) if PROBE in s.get("run", "")]
    assert len(found) == 1, found
    return found[0]


def _ref_step(steps: list[dict]) -> dict:
    """The step whose output the clauDNA checkout takes as its ``ref``."""
    wanted = re.fullmatch(r"\$\{\{ steps\.([\w-]+)\.outputs\.ref \}\}", steps[_checkout(steps)]["with"]["ref"])
    assert wanted
    found = [s for s in steps if s.get("id") == wanted[1]]
    assert len(found) == 1, found
    return found[0]


def _read_ref(cwd: Path, output: Path) -> subprocess.CompletedProcess:
    output.write_text("")
    return subprocess.run(
        ["bash", "-e", "-c", _ref_step(_steps("export-contract-gate"))["run"]],
        env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("job", JOBS)
def test_a_failed_claudna_checkout_fails_the_job(job):
    steps = _steps(job)
    _checkout(steps)
    assert not [s.get("name") for s in steps if "continue-on-error" in s]


@pytest.mark.parametrize("job", JOBS)
def test_only_an_unreachable_claudna_skips(job):
    steps = _steps(job)
    probe = _probe(steps)
    assert probe < _checkout(steps)
    reachable = f"steps.{steps[probe]['id']}.outputs.reachable == 'true'"
    assert [s.get("if") for s in steps[probe + 1 :]] == [reachable] * len(steps[probe + 1 :])
    skips = [i for i, s in enumerate(steps) if "SKIP" in f"{s.get('name', '')} {s.get('run', '')}"]
    assert skips == [probe]


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize(("rc", "reachable"), [(0, "true"), (2, "false"), (128, "false")])
def test_the_probe_reads_reachability_from_git(job, rc, reachable, tmp_path):
    steps = _steps(job)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path / 'argv'}\nexit {rc}\n")
    (bindir / "git").chmod(0o755)
    output = tmp_path / "github_output"
    run = subprocess.run(
        ["bash", "-e", "-c", steps[_probe(steps)]["run"]],
        env={"PATH": f"{bindir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "argv").read_text() == PROBE.removeprefix("git ") + "\n"
    assert output.read_text() == f"reachable={reachable}\n"
    assert ("::notice::SKIP" in run.stdout) == (reachable == "false")


def test_the_rename_map_gate_refuses_a_checkout_with_no_skills_dir(tmp_path):
    steps = _steps("rename-map-gate")
    gate = [s for s in steps if "claudlobby.conformance rename-map" in s.get("run", "")]
    assert len(gate) == 1
    run = subprocess.run(
        ["bash", "-e", "-c", gate[0]["run"]],
        env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "CLAUDNA_SKILLS_DIR": str(tmp_path / "absent" / "skills")},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1
    assert "::error::rename-map" in run.stdout


# The checkout trims its ref input of Unicode whitespace, a byte-order mark included, so a file that
# holds only one is empty to it.
@pytest.mark.parametrize(
    "content",
    [None, b"", b"\n", b" \t\n", b"\r\n", b"\xef\xbb\xbf\n"],
    ids=["missing", "empty", "newline", "blanks", "crlf", "bom"],
)
def test_an_empty_or_missing_claudna_ref_fails_the_job(content, tmp_path):
    if content is not None:
        (tmp_path / REF_FILE).parent.mkdir(parents=True)
        (tmp_path / REF_FILE).write_bytes(content)
    output = tmp_path / "github_output"
    run = _read_ref(tmp_path, output)
    assert run.returncode == 1, run.stderr
    errors = [line for line in run.stdout.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1 and REF_FILE in errors[0], run.stdout
    assert output.read_text() == ""


def test_a_claudna_ref_reaches_the_checkout_as_written(tmp_path):
    (tmp_path / REF_FILE).parent.mkdir(parents=True)
    (tmp_path / REF_FILE).write_text("v9.9.9\n")
    output = tmp_path / "github_output"
    run = _read_ref(tmp_path, output)
    assert run.returncode == 0, run.stderr
    assert output.read_text() == "ref=v9.9.9\n"


def test_the_committed_claudna_ref_is_not_empty(tmp_path):
    output = tmp_path / "github_output"
    run = _read_ref(ROOT, output)
    ref = (ROOT / REF_FILE).read_text().strip()
    assert run.returncode == 0, run.stdout + run.stderr
    assert ref
    assert output.read_text() == f"ref={ref}\n"


def test_the_ref_is_read_whether_or_not_claudna_answers():
    steps = _steps("export-contract-gate")
    ref = _ref_step(steps)
    assert steps.index(ref) < _probe(steps)
    assert "if" not in ref
