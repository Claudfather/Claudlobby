"""The merge guardrails' rollout rungs, run rather than read (#2116 review).

Each rung's snippet is taken from both merge guardrails exactly as a merger
copies it, and run against a stand-in `gh` (tests/fixtures/fake-gh-rungs.py)
that answers from a state file and refuses any read but the pinned one. The
rule under test: no rung passes on a failed read, and no exception is one the
PR's author can grant alone.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUARDRAILS = REPO / "library" / "guardrails"
FAKE_GH = REPO / "tests" / "fixtures" / "fake-gh-rungs.py"
FILES = ("merge-policy-auto-admin.md", "merge-policy-auto-after-review.md")

ADOPTION = "statusCheckRollup"        # rung 2: is the check adopted, and its newest run
WORKFLOWS = 'pulls/$N/files'          # rung 2: the PR's own workflow changes
HOLD = "rollout-hold"                 # rung 5: an open rollout hold

GREEN = {"name": "rollout-check / Rollout check", "startedAt": "2026-10-03T08:00:00Z",
         "conclusion": "SUCCESS"}
RED_LATER = {**GREEN, "startedAt": "2026-10-03T08:05:00Z", "conclusion": "FAILURE"}
PENDING_LATER = {**GREEN, "startedAt": "2026-10-03T08:05:00Z", "conclusion": None}


def snippet(name: str, marker: str) -> str:
    text = (GUARDRAILS / name).read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```bash\n(.*?)```", text, re.S) if marker in b]
    assert len(blocks) == 1, f"{name}: {len(blocks)} bash blocks carry {marker!r}"
    return textwrap.dedent(blocks[0])


def run_rung(tmp_path: Path, code: str, state: dict) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n', encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
           "FAKE_GH_STATE": str(tmp_path / "state.json"), "FAKE_GH_LOG": str(tmp_path / "gh.log")}
    return subprocess.run(["bash", "-c", f"REPO=example/repo\nN=7\n{code}\necho RUNG-PASSED\n"],
                          env=env, capture_output=True, text=True, timeout=30)


def passed(run: subprocess.CompletedProcess) -> bool:
    assert "fake gh:" not in run.stdout + run.stderr, f"a read is not the pinned one:\n{run.stdout}{run.stderr}"
    return run.returncode == 0 and "RUNG-PASSED" in run.stdout


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize(("state", "passes"), [
    ({"contents": "absent"}, True),
    ({"contents": "present", "rollup": [GREEN]}, True),
    ({"contents": "present", "rollup": [GREEN, RED_LATER]}, False),
    ({"contents": "present", "rollup": [GREEN, PENDING_LATER]}, False),
    ({"contents": "present", "rollup": []}, False),
    ({"contents": "present", "fail": ["contents"]}, False),
    ({"contents": "present", "fail": ["repo"]}, False),
    ({"contents": "present", "rollup": [GREEN], "fail": ["rollup"]}, False),
], ids=["not-adopted-404", "newest-green", "stale-green-under-a-newer-red", "newer-run-pending",
        "no-run", "adoption-read-fails", "default-branch-read-fails", "rollup-read-fails"])
def test_rung_2_takes_the_newest_rollout_run_and_refuses_a_failed_read(tmp_path, name, state, passes):
    """Only a 404 says the repo has not adopted the check; the rollup lists every run
    of a name, so only the newest counts."""
    assert passed(run_rung(tmp_path, snippet(name, ADOPTION), state)) is passes


@pytest.mark.parametrize("name", FILES)
def test_rung_2_reads_the_prs_own_files_for_a_workflow_change(tmp_path, name):
    """The checker's own warning is printed by code a workflow-editing PR controls, so
    the merger reads the PR's file list itself: every page, renames too, failing closed."""
    code = snippet(name, WORKFLOWS)
    run = run_rung(tmp_path, code, {"files": [
        {"filename": "README.md"},
        {"filename": ".github/workflows/renamed.yml", "previous_filename": ".github/workflows/old.yml"}]})
    assert passed(run)
    assert ".github/workflows/renamed.yml" in run.stdout and ".github/workflows/old.yml" in run.stdout
    assert "rung 1 verdict" in run.stdout
    quiet = run_rung(tmp_path, code, {"files": [{"filename": "README.md"}]})
    assert passed(quiet) and ".github/workflows/" not in quiet.stdout
    assert not passed(run_rung(tmp_path, code, {"fail": ["files"]}))


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize(("state", "passes", "hold"), [
    ({"contents": "present", "holds": []}, True, None),
    ({"contents": "present", "holds": [100], "closes": [100]}, True, 100),
    ({"contents": "present", "holds": [100, 200], "closes": [200]}, True, 200),
    ({"contents": "present", "holds": [100], "closes": [10]}, False, None),
    ({"contents": "present", "holds": [10], "closes": [100]}, False, None),
    ({"contents": "present", "holds": [100], "closes": []}, False, None),
    ({"contents": "present", "fail": ["holds"]}, False, None),
    ({"contents": "present", "holds": [100], "fail": ["closes"]}, False, None),
    ({"contents": "present", "fail": ["contents"]}, False, None),
    ({"contents": "present", "fail": ["repo"]}, False, None),
], ids=["no-hold", "closes-the-hold", "closes-one-of-two", "10-against-100", "100-against-10",
        "closes-nothing", "hold-listing-fails", "closes-read-fails", "adoption-read-fails",
        "default-branch-read-fails"])
def test_rung_5_refuses_a_failed_read_and_never_lets_the_keyword_alone_grant_it(tmp_path, name, state,
                                                                               passes, hold):
    """In a repo that has adopted the check. A `Closes #N` keyword is the author's to
    write, so a PR that closes a hold still merges only on the rung 1 verdict that
    names that hold; the snippet says which, so the merger can hold the verdict to it.
    An adoption read that fails cannot tell an adopted repo from one that is not, so
    it refuses."""
    run = run_rung(tmp_path, snippet(name, HOLD), state)
    assert passed(run) is passes, run.stdout
    if hold is not None:
        assert f"rung 1 verdict names #{hold}" in run.stdout, run.stdout


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("state", [
    {"contents": "absent", "holds": [100]},
    {"contents": "absent", "fail": ["holds", "closes"]},
], ids=["a-hold-labelled-issue", "the-listing-would-fail"])
def test_rung_5_is_inert_where_the_repo_has_not_adopted_the_check(tmp_path, name, state):
    """Opt-in (dara, for a fleet that composes this guardrail without verify-rollout):
    where the default branch has no rollout-check caller (a 404), rung 5 lists no holds
    and so cannot refuse a merge."""
    run = run_rung(tmp_path, snippet(name, HOLD), state)
    assert passed(run), run.stdout
    calls = [json.loads(line) for line in (tmp_path / "gh.log").read_text(encoding="utf-8").splitlines()]
    assert not any(call[:2] in (["issue", "list"], ["pr", "view"]) for call in calls), calls
