"""CI's test lanes: which job runs which test, and how a flaky test leaves the
required lanes without disappearing.

A flaky test is quarantined with ``@pytest.mark.quarantine(issue=<number>)``.
The required lanes deselect it (test.yml's ``pytest`` and ``harness`` jobs,
conformance.yml's ``vault-tests``), and the ``quarantine`` workflow, which no
ruleset should require, runs it on every PR and push to main. So it still fails
where people can see it, without turning a merge red. The end-to-end harness
is marked ``harness`` and runs in its own job beside ``pytest``.

Pinned here, by running pytest's own selection on each job's real ``-m``
expression rather than by reading the expressions:

- every test lands in exactly one of the three lanes of the ``tests`` side,
  whatever its markers;
- a quarantine that names no tracking issue is refused, in the required lane
  too, before ``-m`` has deselected it;
- no required lane removes a test any other way. A ``--deselect`` node id that
  stops matching deselects nothing and says nothing, and ``-k`` is a substring
  match on names, so both drop tests without a trace;
- an empty quarantine is a green lane, not a red one.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
CONFIG = REPO / "pyproject.toml"
CONFTEST = REPO / "tests" / "conftest.py"

# (workflow file, job id) for each lane.
PYTEST = ("test.yml", "pytest")
HARNESS = ("test.yml", "harness")
VAULT = ("conformance.yml", "vault-tests")
QUARANTINE = ("quarantine.yml", "quarantine")
REQUIRED = (PYTEST, HARNESS, VAULT)

# One probe test per marker combination a real test can carry.
PROBE = """
import pytest


def test_plain():
    pass


@pytest.mark.harness
def test_harness():
    pass


@pytest.mark.vault
def test_vault():
    pass


@pytest.mark.quarantine(issue=1)
def test_quarantined():
    pass


@pytest.mark.harness
@pytest.mark.quarantine(issue=1)
def test_quarantined_harness():
    pass


@pytest.mark.vault
@pytest.mark.quarantine(issue=1)
def test_quarantined_vault():
    pass
"""

SHELL_OPERATORS = {"||", "&&", "|", ";", "&", ">", ">>", "<", "2>&1"}


def _steps(lane):
    workflow, job = lane
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return doc["jobs"][job]["steps"]


def _pytest_argvs(lane):
    """The arguments of every pytest invocation in a job's shell steps: after
    `pytest` or `python -m pytest`, cut at the first shell operator."""
    argvs = []
    for step in _steps(lane):
        run = step.get("run", "")
        for line in run.replace("\\\n", " ").splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            for i, tok in enumerate(tokens):
                direct = tok == "pytest" or tok.endswith("/pytest")
                module = tok == "pytest" and i > 0 and tokens[i - 1] == "-m"
                if direct or module:
                    rest = []
                    for arg in tokens[i + 1 :]:
                        if arg in SHELL_OPERATORS or arg.startswith((">", "2>")):
                            break
                        rest.append(arg)
                    argvs.append(rest)
                    break
    return argvs


def _expression(lane):
    """The one -m expression a lane runs, refused when it has none."""
    argvs = _pytest_argvs(lane)
    assert len(argvs) == 1, f"{lane}: expected one pytest invocation, found {argvs}"
    argv = argvs[0]
    assert "-m" in argv, (
        f"{lane} runs pytest with no -m, so it runs quarantined tests too: {argv}"
    )
    return argv[argv.index("-m") + 1]


def _probe_dir(tmp_path, source=PROBE):
    probe = tmp_path / "probe"
    probe.mkdir()
    # The REAL conftest, so the quarantine check under test is the shipped one.
    shutil.copy(CONFTEST, probe / "conftest.py")
    (probe / "test_probe.py").write_text(source)
    return probe


def _collect(probe, expression):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(CONFIG),
            "--rootdir",
            str(probe),
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "-m",
            expression,
            str(probe),
        ],
        capture_output=True,
        text=True,
        cwd=probe,
    )
    selected = {
        line.split("::")[-1]
        for line in proc.stdout.splitlines()
        if "::" in line and not line.startswith(" ")
    }
    return proc, selected


def test_every_test_lands_in_exactly_one_lane_of_the_tests_side(tmp_path):
    probe = _probe_dir(tmp_path)
    lanes = {
        lane: _collect(probe, _expression(lane))
        for lane in (PYTEST, HARNESS, QUARANTINE)
    }
    for lane, (proc, _) in lanes.items():
        assert proc.returncode in (0, 5), (lane, proc.stdout, proc.stderr)
    assert lanes[PYTEST][1] == {"test_plain", "test_vault"}
    assert lanes[HARNESS][1] == {"test_harness"}
    assert lanes[QUARANTINE][1] == {
        "test_quarantined",
        "test_quarantined_harness",
        "test_quarantined_vault",
    }
    every = set().union(*(sel for _, sel in lanes.values()))
    assert sum(len(sel) for _, sel in lanes.values()) == len(every) == 6


def _jobs_running_pytest():
    """Every (workflow, job) that invokes pytest, outside the quarantine lane."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        if wf.name == QUARANTINE[0]:
            continue
        for job in yaml.safe_load(wf.read_text()).get("jobs", {}):
            if _pytest_argvs((wf.name, job)):
                yield (wf.name, job)


def test_every_other_job_that_runs_pytest_leaves_quarantine_out(tmp_path):
    """Not only the named lanes: a job added later (a platform matrix, say)
    must deselect quarantined tests too, or it runs them red on every PR."""
    jobs = list(_jobs_running_pytest())
    assert set(REQUIRED) <= set(jobs), jobs  # the finder sees the known lanes
    probe = _probe_dir(tmp_path)
    quarantined = {"test_quarantined", "test_quarantined_harness", "test_quarantined_vault"}
    for lane in jobs:
        proc, selected = _collect(probe, _expression(lane))
        assert proc.returncode in (0, 5), (lane, proc.stdout, proc.stderr)
        assert not selected & quarantined, (lane, selected)


def test_the_vault_lane_leaves_quarantine_out(tmp_path):
    proc, selected = _collect(_probe_dir(tmp_path), _expression(VAULT))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert selected == {"test_vault"}


BAD_QUARANTINES = {
    "bare": "@pytest.mark.quarantine",
    "reason_only": '@pytest.mark.quarantine(reason="flaky")',
    "issue_as_text": '@pytest.mark.quarantine(issue="1947")',
    "issue_zero": "@pytest.mark.quarantine(issue=0)",
}


def test_a_quarantine_naming_no_tracking_issue_is_refused_in_the_required_lane(
    tmp_path,
):
    """Refused in the lane that deselects it, and every refusal is named, not
    just the first."""
    source = "import pytest\n" + "".join(
        f"\n\n{mark}\ndef test_{name}():\n    pass\n"
        for name, mark in BAD_QUARANTINES.items()
    )
    proc, _ = _collect(_probe_dir(tmp_path, source), _expression(PYTEST))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 4, out
    assert "quarantine(issue=<number>)" in out, out
    for name in BAD_QUARANTINES:
        assert f"test_probe.py::test_{name}" in out, (name, out)


def test_a_quarantine_naming_its_issue_is_accepted(tmp_path):
    source = "import pytest\n\n\n@pytest.mark.quarantine(issue=1947)\ndef test_flaky():\n    pass\n"
    proc, selected = _collect(_probe_dir(tmp_path, source), _expression(QUARANTINE))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert selected == {"test_flaky"}


@pytest.mark.parametrize("lane", REQUIRED, ids=lambda lane: lane[1])
def test_no_required_lane_removes_a_test_but_by_marker(lane):
    for argv in _pytest_argvs(lane):
        for arg in argv:
            assert not arg.startswith(("--deselect", "--ignore", "-k")), (lane, argv)


@pytest.mark.parametrize(
    "fake_rc,step_ok", [(0, True), (5, True), (1, False), (4, False)]
)
def test_the_quarantine_lane_is_green_when_empty_and_red_when_a_test_fails(
    tmp_path, fake_rc, step_ok
):
    """Run the lane's real step with a stand-in pytest that exits fake_rc,
    under the shell Actions uses for a run step (bash -e)."""
    run = [s["run"] for s in _steps(QUARANTINE) if "pytest" in s.get("run", "")]
    assert len(run) == 1, run
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "pytest"
    fake.write_text(f"#!/bin/sh\nexit {fake_rc}\n")
    fake.chmod(0o755)
    proc = subprocess.run(
        ["bash", "-e", "-c", run[0]],
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert (proc.returncode == 0) is step_ok, (fake_rc, proc.returncode, proc.stderr)


def test_the_harness_job_installs_tmux_so_the_harness_cannot_skip_silently():
    installs = " ".join(s.get("run", "") for s in _steps(HARNESS))
    assert "apt-get install" in installs and "tmux" in installs, installs


def test_the_harness_is_marked_for_its_own_lane():
    from tests import test_validate_harness

    marks = {
        m.name
        for m in getattr(
            test_validate_harness.test_validate_bot_change_harness, "pytestmark", []
        )
    }
    assert "harness" in marks, marks


def test_the_lane_markers_are_registered(pytestconfig):
    names = {
        line.split(":")[0].split("(")[0].strip()
        for line in pytestconfig.getini("markers")
    }
    assert {"vault", "harness", "quarantine"} <= names, names
