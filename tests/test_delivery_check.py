"""#1745: is finished work actually delivered? — and can this rung ever go RED?

**THE CONTROL PROBLEM THIS FILE EXISTS FOR.** On a clean estate the rung reports
nothing, so a test that merely runs it and sees "clean" is a control that cannot
fail: it would pass identically against a rung that returns early, queries the
wrong thing, or was deleted. Every positive here is therefore CONSTRUCTED — the
branch-ahead-with-no-PR state and the PR-head-behind-its-branch state are both
built deliberately and the rung is asserted to FIRE on each.

The GitHub half is stubbed at the ONE subprocess seam (`delivery._sh`), because
the states being tested are states of a remote that cannot be created in a test.
What is NOT stubbed is the local git half: real branches in a real repository,
so "ahead of the default branch" is answered by git itself.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from claudlobby import delivery


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                               "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)})


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real repo with a real `origin/main` and a real branch ahead of it."""
    remote = tmp_path / "remote.git"; remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True,
                   capture_output=True)
    _git(work, "config", "user.email", "t@t"); _git(work, "config", "user.name", "t")
    (work / "base.md").write_text("base\n")
    _git(work, "add", "-A"); _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "main")
    # THE CONSTRUCTED STATE: a branch ahead of main, pushed, no PR.
    _git(work, "checkout", "-q", "-b", "feat/stranded")
    (work / "delivered.md").write_text("finished work nobody can see\n")
    _git(work, "add", "-A"); _git(work, "commit", "-qm", "the work")
    _git(work, "push", "-q", "origin", "feat/stranded")
    _git(work, "checkout", "-q", "main")
    _git(work, "fetch", "-q", "origin")
    return work


def _stub(monkeypatch, *, open_prs=(), all_pr_branches=(), exact=None, refs=None,
          calls=None):
    """Stub the ONE subprocess seam. Real `git` passes through, so the local half
    is genuinely exercised; only `gh` is answered."""
    real = delivery._sh

    def fake(argv, cwd=None, timeout=20.0):
        if calls is not None:
            calls.append(tuple(argv))
        if argv[0] != "gh":
            return real(argv, cwd=cwd, timeout=timeout)
        if "pr" in argv and "list" in argv and "open" in argv:
            return 0, json.dumps(list(open_prs))
        if "pr" in argv and "list" in argv:
            return 0, json.dumps([{"headRefName": b} for b in all_pr_branches])
        if "git/matching-refs/heads" in " ".join(argv):
            # The BATCHED shape: one call, TSV of `refs/heads/<branch>\t<sha>`.
            # This stub previously answered the per-PR `git/ref/heads/<branch>`
            # form and failed when the implementation batched — which is the
            # test noticing an API change rather than a defect, and is why the
            # stub sits at one seam instead of mocking a client.
            if not refs:
                return 1, ""
            return 0, "\n".join(f"refs/heads/{b}\t{sha}" for b, sha in refs.items())
        if "pulls?head=" in " ".join(argv):
            if exact is None:
                return 1, ""
            return 0, str(exact)
        return real(argv, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(delivery, "_sh", fake)


class TestItCanGoRed:
    """The positives, constructed. If these ever stop failing on a broken rung
    the rung has become decorative."""

    def test_a_branch_ahead_with_NO_PR_is_reported(self, checkout, monkeypatch):
        # Bulk list knows nothing about it AND the exact query confirms zero PRs.
        _stub(monkeypatch, all_pr_branches=["some/other"], exact=0)
        f = delivery.check_repo("o/r", str(checkout))
        assert "feat/stranded" in f.no_pr, f
        assert not f.clean, "the rung reported clean on a stranded branch"

    def test_a_PR_head_BEHIND_its_branch_ref_is_reported(self, checkout, monkeypatch):
        _stub(monkeypatch,
              open_prs=[{"number": 42, "headRefName": "feat/stranded",
                         "headRefOid": "a" * 40}],
              all_pr_branches=["feat/stranded"],
              refs={"feat/stranded": "b" * 40})
        f = delivery.check_repo("o/r", str(checkout))
        assert f.stale_pr_head, f
        assert "#42" in f.stale_pr_head[0] and "wrong commit" in f.stale_pr_head[0]
        assert not f.clean

    def test_the_two_halves_are_independent(self, checkout, monkeypatch):
        """Neither covers the other, so a rung that found only one would pass a
        test written for the other. Here the PR head matches (half 2 clean) while
        the branch has no PR (half 1 fires)."""
        _stub(monkeypatch,
              open_prs=[{"number": 7, "headRefName": "other/branch",
                         "headRefOid": "c" * 40}],
              all_pr_branches=["other/branch"], exact=0,
              refs={"other/branch": "c" * 40})
        f = delivery.check_repo("o/r", str(checkout))
        assert not f.stale_pr_head, "half 2 should be clean here"
        assert "feat/stranded" in f.no_pr, "half 1 should fire here"


class TestTheCensusArtifactIsNotShipped:
    """#1745's own census inflated 1 -> 31 by treating "not in the most recent
    400 PRs" as "has no PR". The bulk list can produce a false NO-PR and can
    never produce a false HAS-PR, so every apparent no-PR is confirmed exactly."""

    def test_a_branch_whose_PR_is_older_than_the_cap_is_NOT_reported(
            self, checkout, monkeypatch):
        # The artifact exactly: absent from the bulk list, but the exact query
        # finds a PR. A rung with the bug reports this branch as undelivered.
        calls: list[tuple] = []
        _stub(monkeypatch, all_pr_branches=[], exact=1, calls=calls)
        f = delivery.check_repo("o/r", str(checkout))
        assert f.no_pr == [], (
            "a branch whose PR is older than the bulk cap was reported as having "
            "no PR — this is the artifact that inflated the issue's census 1->31")
        assert f.confirmations == 1, "the exact query must actually have been made"
        assert any("pulls?head=" in " ".join(c) for c in calls), calls

    def test_an_unreachable_exact_query_is_UNCHECKED_not_clean(
            self, checkout, monkeypatch):
        _stub(monkeypatch, all_pr_branches=[], exact=None)
        f = delivery.check_repo("o/r", str(checkout))
        assert f.no_pr == [], "an unreachable query must not be read as no-PR"
        assert "feat/stranded" in f.unchecked, (
            "nor as clean — unreachable is its own answer")


class TestItStatesItsBounds:
    """#1742's rule: silence that could mean clean or could mean not-looked is
    the class this rung closes, so it must not reproduce it one level up."""

    def test_the_bound_line_names_the_window_cap_and_confirmations(
            self, checkout, monkeypatch):
        _stub(monkeypatch, all_pr_branches=[], exact=1)
        f = delivery.check_repo("o/r", str(checkout))
        line = f.bound_line()
        assert "window 7d" in line and "cap 400" in line, line
        assert "confirmation" in line, line

    def test_past_the_confirm_cap_branches_are_UNCHECKED_and_the_bound_says_so(
            self, checkout, monkeypatch):
        _stub(monkeypatch, all_pr_branches=[], exact=0)
        f = delivery.check_repo("o/r", str(checkout), confirm_cap=0)
        assert f.no_pr == [], "with no confirmations spent nothing can be asserted"
        assert f.unchecked == ["feat/stranded"]
        assert "UNCHECKED" in f.bound_line(), f.bound_line()
        assert "not a clean answer" in f.bound_line()


class TestItIsCheapWhereItCanBe:
    def test_the_branch_half_makes_no_network_call_per_branch(
            self, checkout, monkeypatch):
        """Ahead-of-default is answered by local git. Only the has-PR question
        costs a round trip, and only for apparent no-PR branches."""
        calls: list[tuple] = []
        _stub(monkeypatch, all_pr_branches=["feat/stranded"], calls=calls)
        delivery.check_repo("o/r", str(checkout))
        gh = [c for c in calls if c[0] == "gh"]
        assert not any("pulls?head=" in " ".join(c) for c in gh), (
            "a branch already known to have a PR must cost no confirmation")
        assert len(gh) <= 2, f"more bulk calls than necessary: {gh}"


def test_the_time_budget_fires_and_reports_UNCHECKED_not_clean(checkout, monkeypatch):
    """The budget exists so a large repo cannot make `doctor` slow enough to be
    skipped — which is the failure mode a costly health check actually has. It
    must report UNCHECKED, the only direction this rung may fail in."""
    _stub(monkeypatch, all_pr_branches=[], exact=0)
    f = delivery.check_repo("o/r", str(checkout), budget_s=0.0)
    assert f.no_pr == [], "a spent budget must not report a no-PR verdict"
    assert f.unchecked, "the budget did not fire"
    assert any("budget spent" in n for n in f.notes), f.notes
    assert "UNCHECKED" in f.bound_line() and "not a clean answer" in f.bound_line()
