"""CI's test lanes: which job runs which test, and how a flaky test leaves the
required lanes without disappearing.

A flaky test is quarantined with ``@pytest.mark.quarantine(issue=<number>)``.
The required lanes deselect it (test.yml's shards, ``harness`` jobs and
``pytest``, conformance.yml's ``vault-tests``), and the ``quarantine`` workflow,
which no ruleset should require, runs it on every PR and push to main. So it
still fails where people can see it, without turning a merge red. The
end-to-end harness is marked ``harness`` and runs in a job of its own on each
platform.

A pull request runs the rest of the suite once on each supported platform,
Linux with Python 3.10 and macOS with Python 3.11, in shards that pytest-split
divides it into; ``pytest``, Linux with Python 3.11, runs on push to main only
(#1945). Each platform's gate job carries the platform's check name and passes
only when its shards ran the suite they collected, each test once
(tests/ci_shards.py, tests/test_ci_shards.py).

Pinned here, by running pytest's own selection on each job's real ``-m``
expression rather than by reading the expressions:

- on each event and platform, every test lands in exactly one lane of the
  ``tests`` side, whatever its markers;
- a quarantine that names no tracking issue is refused, in the required lane
  too, before ``-m`` has deselected it;
- no required lane removes a test any other way. A ``--deselect`` node id that
  stops matching deselects nothing and says nothing, and ``-k`` is a substring
  match on names, so both drop tests without a trace;
- an empty quarantine is a green lane, not a red one;
- every shard lists the suite it collected in the step and environment it runs
  it in, and each gate waits for every shard and passes only when each passed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests import ci_shards

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
CONFIG = REPO / "pyproject.toml"
QUARANTINE_POLICY = REPO / "tests" / "quarantine_policy.py"

# (workflow file, job id) for each lane.
PYTEST = ("test.yml", "pytest")
HARNESS = ("test.yml", "harness")
HARNESS_MACOS = ("test.yml", "harness-macos")
UBUNTU_SHARDS = ("test.yml", "supported-ubuntu-shard")
MACOS_SHARDS = ("test.yml", "supported-macos-shard")
VAULT = ("conformance.yml", "vault-tests")
QUARANTINE = ("quarantine.yml", "quarantine")
REQUIRED = (PYTEST, HARNESS, HARNESS_MACOS, UBUNTU_SHARDS, MACOS_SHARDS, VAULT)

# Each sharded lane's gate: its job id, and the check name the merge gate reads.
GATES = {
    UBUNTU_SHARDS: ("supported-ubuntu", "supported (ubuntu-latest, py3.10)"),
    MACOS_SHARDS: ("supported-macos", "supported (macos-latest, py3.11)"),
}
# The tests side of each run: the full-suite lane, the harness lane and the
# quarantine lane that one event runs for one platform.
SIDES = {
    "push-linux": (PYTEST, HARNESS, QUARANTINE),
    "pull-request-linux": (UBUNTU_SHARDS, HARNESS, QUARANTINE),
    "pull-request-macos": (MACOS_SHARDS, HARNESS_MACOS, QUARANTINE),
}

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


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _steps(lane):
    workflow, job = lane
    doc = _workflow(workflow)
    # A job that calls a reusable workflow (`uses:` at job level, as
    # rollout-check.yml's does) has no steps of its own. The workflow it calls
    # is a file here, and the finder scans that file's jobs as themselves.
    return doc["jobs"][job].get("steps", [])


def _run_argvs(run):
    """The arguments of every pytest invocation in one shell script: after
    `pytest` or `python -m pytest`, cut at the first shell operator."""
    argvs = []
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


def _pytest_argvs(lane):
    """The arguments of every pytest invocation in a job's shell steps."""
    return [argv for step in _steps(lane) for argv in _run_argvs(step.get("run", ""))]


def _expression(lane):
    """The -m expression a lane runs. Every pytest invocation in the job carries
    one, and the same one: a shard lists its suite with the selection it runs."""
    argvs = _pytest_argvs(lane)
    assert argvs, f"{lane}: runs no pytest"
    expressions = set()
    for argv in argvs:
        assert "-m" in argv, (
            f"{lane} runs pytest with no -m, so it runs quarantined tests too: {argv}"
        )
        expressions.add(argv[argv.index("-m") + 1])
    assert len(expressions) == 1, f"{lane} selects differently in one job: {expressions}"
    return expressions.pop()


def _probe_dir(tmp_path, source=PROBE):
    probe = tmp_path / "probe"
    probe.mkdir()
    # The real collection policy, without unrelated application-origin checks.
    shutil.copy(QUARANTINE_POLICY, probe / "conftest.py")
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


@pytest.mark.parametrize("side", SIDES)
def test_every_test_lands_in_exactly_one_lane_of_the_tests_side(tmp_path, side):
    full, harness, quarantine = SIDES[side]
    probe = _probe_dir(tmp_path)
    lanes = {lane: _collect(probe, _expression(lane)) for lane in SIDES[side]}
    for lane, (proc, _) in lanes.items():
        assert proc.returncode in (0, 5), (lane, proc.stdout, proc.stderr)
    assert lanes[full][1] == {"test_plain", "test_vault"}
    assert lanes[harness][1] == {"test_harness"}
    assert lanes[quarantine][1] == {
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
    # A shard's --splits and --group take the rest of the suite away from it;
    # its gate proves the shards' shares add up to the suite.
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


@pytest.mark.parametrize(
    "lane,installer",
    [(HARNESS, "apt-get install"), (HARNESS_MACOS, "brew install")],
    ids=["linux", "macos"],
)
def test_the_harness_job_installs_tmux_so_the_harness_cannot_skip_silently(lane, installer):
    installs = " ".join(s.get("run", "") for s in _steps(lane))
    assert installer in installs and "tmux" in installs, installs


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


# --- the shards and their gates (#1945) ---------------------------------------------------------

def _job(lane):
    workflow, job = lane
    return _workflow(workflow)["jobs"][job]


def _shard_step(lane):
    steps = [s for s in _job(lane)["steps"] if _run_argvs(s.get("run", ""))]
    assert len(steps) == 1, [s.get("name") for s in steps]  # one step, one environment
    return steps[0]


def test_the_linux_311_lane_runs_on_push_to_main_only():
    """A pull request runs the Linux suite once, on 3.10, the floor version;
    3.11 runs on macOS there, and on Linux in the push run after the merge.
    Every other job of the tests side runs on both events."""
    jobs = _workflow("test.yml")["jobs"]
    assert jobs["pytest"]["if"] == "github.event_name == 'push'"
    others = {job: spec.get("if") for job, spec in jobs.items() if job != "pytest"}
    assert set(others.values()) <= {None, "always()"}, others


def test_the_release_rehearsal_still_runs_on_every_pull_request():
    """It ran in pytest, which no longer runs on pull requests, so it runs in
    one Linux 3.10 shard as well."""
    steps = [s for s in _steps(UBUNTU_SHARDS) if "release_assembly_smoke.py" in s.get("run", "")]
    assert len(steps) == 1 and steps[0]["if"] == "matrix.group == 1", steps


def test_a_run_needs_no_more_macos_jobs_than_the_account_runs_at_once():
    """GitHub runs five macOS jobs at once on this account's plan: a sixth
    would wait for one of the run's own first five to finish."""
    jobs = _workflow("test.yml")["jobs"].values()
    macos = sum(
        len(spec.get("strategy", {}).get("matrix", {}).get("group", [None]))
        for spec in jobs
        if str(spec.get("runs-on", "")).startswith("macos")
    )
    assert macos <= 5, macos


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
def test_each_shard_job_runs_every_group_once(lane):
    doc = _workflow(lane[0])
    job = doc["jobs"][lane[1]]
    splits = int(doc["env"]["TEST_SPLITS"])
    assert job["strategy"] == {"fail-fast": False, "matrix": {"group": list(range(1, splits + 1))}}
    assert job["env"]["TEST_GROUP"] == "${{ matrix.group }}"
    # The first step refuses a matrix that does not match TEST_SPLITS.
    check = job["steps"][0]["run"]
    for total, ok in ((splits, True), (splits - 1, False), (splits + 1, False)):
        proc = subprocess.run(
            ["bash", "-e", "-c", check.replace("${{ strategy.job-total }}", str(total))],
            env={"PATH": "/usr/bin:/bin", "TEST_SPLITS": str(splits)},
            capture_output=True,
        )
        assert (proc.returncode == 0) is ok, (total, proc.returncode)


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
def test_each_shard_lists_its_suite_with_the_selection_it_runs(lane):
    step = _shard_step(lane)
    listing, run = _run_argvs(step["run"])
    assert listing[:2] == ["--collect-only", "--verbosity=-1"], listing
    assert run[run.index("--splits") + 1] == "$TEST_SPLITS", run
    assert run[run.index("--group") + 1] == "$TEST_GROUP", run
    assert "--junitxml=pytest-results.xml" in run, run
    assert "tests/ci_shards.py ids collection.txt collected.txt" in step["run"]
    assert _job(lane)["env"]["PYTHONHASHSEED"] == "0"  # one collection order in every shard
    _expression(lane)  # the listing and the run select the same tests


# Stands in for .venv/bin/python in a shard's step: records each pytest
# invocation's arguments and environment, and passes ci_shards.py to Python.
STAND_IN = '''#!{python}
import json, os, subprocess, sys

args = sys.argv[1:]
if args[:1] == ["tests/ci_shards.py"]:
    sys.exit(subprocess.call([{python!r}, {script!r}, *args[1:]]))
with open(os.environ["STAND_IN_LOG"], "a") as log:
    log.write(json.dumps({{"argv": args, "env": dict(os.environ)}}) + "\\n")
if "--collect-only" in args:
    print(os.environ["STAND_IN_LISTING"], end="")
    sys.exit(int(os.environ["STAND_IN_LISTING_RC"]))
sys.exit(int(os.environ["STAND_IN_RC"]))
'''
LISTING = "tests/test_x.py::test_a\ntests/test_x.py::test_b\n\n2 tests collected in 0.01s\n"


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
@pytest.mark.parametrize(
    "listing,listing_rc,run_rc,step_rc",
    [
        (LISTING, 0, 0, 0),
        (LISTING, 0, 1, 1),
        (LISTING, 2, None, 2),
        (LISTING.replace("2 tests", "3 tests"), 0, None, 1),
    ],
    ids=["passes", "a-test-fails", "collection-fails", "listing-miscounted"],
)
def test_a_shard_step_lists_and_runs_its_suite_in_one_environment(
    tmp_path, lane, listing, listing_rc, run_rc, step_rc
):
    """The step as Actions runs it (bash -e), with a stand-in interpreter: the
    listing and the run see one environment, the run gets this shard's group,
    and a listing that fails or that pytest does not count runs nothing."""
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    stand_in = venv / "python"
    script = str(REPO / "tests" / "ci_shards.py")
    stand_in.write_text(STAND_IN.format(python=sys.executable, script=script))
    stand_in.chmod(0o755)
    (tmp_path / ".test_durations").write_text("{}\n")
    log = tmp_path / "invocations.jsonl"
    env = {
        "PATH": os.environ["PATH"], "TMPDIR": str(tmp_path), "TEST_SPLITS": "4", "TEST_GROUP": "2",
        "STAND_IN_LOG": str(log), "STAND_IN_LISTING": listing,
        "STAND_IN_LISTING_RC": str(listing_rc), "STAND_IN_RC": str(run_rc or 0),
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", _shard_step(lane)["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls, proc.stdout + proc.stderr  # the listing always runs
    # HOME, TMPDIR and the XDG dirs are one fresh scratch tree, wherever mktemp
    # made it: macOS's ignores TMPDIR and uses its own temp dir, so the step's
    # tree is removed below when it lies outside tmp_path.
    scratch = Path(calls[0]["env"]["HOME"]).parent
    try:
        assert proc.returncode == step_rc, proc.stdout + proc.stderr
        assert (tmp_path / "pytest.rc").read_text() == f"{step_rc}\n"
        assert len(calls) == (1 if run_rc is None else 2), [c["argv"] for c in calls]
        for key, sub in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"),
                         ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"),
                         ("XDG_DATA_HOME", "data")):
            assert calls[0]["env"][key] == str(scratch / sub), (key, calls[0]["env"][key])
        assert calls[0]["env"]["PLANE_EMIT_DISABLED"] == "1"
        if run_rc is not None:
            listed, ran = calls
            assert "--collect-only" in listed["argv"] and "--collect-only" not in ran["argv"]
            assert ran["argv"][ran["argv"].index("--splits") + 1] == "4"
            assert ran["argv"][ran["argv"].index("--group") + 1] == "2"
            assert listed["env"] == ran["env"]
            assert (tmp_path / "collected.txt").read_text() == listing.split("\n\n")[0] + "\n"
    finally:
        made = ["cache", "config", "data", "home", "state", "tmp"]
        if (scratch.is_dir() and not scratch.is_relative_to(tmp_path)
                and scratch.name.startswith("tmp.")
                and sorted(child.name for child in scratch.iterdir()) == made):
            shutil.rmtree(scratch)


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
def test_each_gate_carries_its_check_name_and_waits_for_every_shard(lane):
    gate_id, check_name = GATES[lane]
    gate = _workflow(lane[0])["jobs"][gate_id]
    assert gate["name"] == check_name
    assert gate["needs"] == [lane[1]]
    # always(), so it never skips: a skipped job reports success.
    assert gate["if"] == "always()"


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
@pytest.mark.parametrize(
    "results,passes",
    [("success", True), ("failure", False), ("cancelled", False), ("skipped", False),
     ("", False), ("success failure", False)],
)
def test_a_gate_passes_only_when_every_shard_passed(lane, results, passes):
    first = _workflow(lane[0])["jobs"][GATES[lane][0]]["steps"][0]
    assert first["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    proc = subprocess.run(
        ["bash", "-e", "-c", first["run"]],
        env={"PATH": "/usr/bin:/bin", "RESULTS": results}, capture_output=True, text=True,
    )
    assert (proc.returncode == 0) is passes, (results, proc.stdout, proc.stderr)


@pytest.mark.parametrize("lane", GATES, ids=lambda lane: lane[1])
def test_each_gate_checks_its_own_shards_against_its_own_ceiling(lane):
    gate_id, check_name = GATES[lane]
    jobs = _workflow(lane[0])["jobs"]
    upload = next(
        s for s in jobs[lane[1]]["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact")
        and "matrix.group" in s["with"]["name"]
    )
    download = next(
        s for s in jobs[gate_id]["steps"]
        if s.get("uses", "").startswith("actions/download-artifact")
    )
    assert download["with"]["pattern"] == upload["with"]["name"].replace("${{ matrix.group }}", "*")
    # The files the gate reads sit at the artifact's root only while every path
    # is in the workspace: one in the runner's temp directory would root the
    # artifact at the two directories' common parent.
    paths = upload["with"]["path"].split()
    assert {"collected.txt", "pytest-results.xml", "pytest.rc", "durations.json"} <= set(paths)
    assert all("/" not in path and "$" not in path for path in paths), paths
    runs = " ".join(s.get("run", "") for s in jobs[gate_id]["steps"])
    check = shlex.split(runs.replace("\\\n", " "))
    assert check[check.index("tests/ci_shards.py") + 1] == "check"
    assert check[check.index("--lane") + 1] == check_name
    assert check[check.index("--splits") + 1] == "$TEST_SPLITS"


def test_every_skip_ceiling_belongs_to_a_gate():
    assert set(ci_shards.SKIP_CEILINGS) == {name for _, name in GATES.values()}
