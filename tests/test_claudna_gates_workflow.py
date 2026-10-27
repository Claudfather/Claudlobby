"""The two clauDNA jobs in the conformance workflow (#2539).

``export-contract-gate`` checks out clauDNA at the ref in ``claudlobby/contracts/claudna.ref`` and
``rename-map-gate`` checks out its default branch. Both are a hosted convenience that a clauDNA
outage must not block, so a clauDNA that does not answer skips with a notice. A clauDNA that answers
must be checked out: a ref it does not hold fails the checkout, and with it the job, instead of
reading green while checking nothing. These tests read the workflow and run each job's reachability
probe against a stand-in ``git``; the real fetch is the CI jobs'.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
JOBS = ("export-contract-gate", "rename-map-gate")
PROBE = "git ls-remote --exit-code https://github.com/Claudfather/clauDNA.git HEAD"


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
