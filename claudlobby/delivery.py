"""Is finished work actually DELIVERED? (#1745)

Two states in which work is 100% done and 0% delivered, and **neither check
covers the other** — they are different failures of the same chain:

  **branch ahead, no PR.** Committed and pushed, no pull request ever opened.
  Invisible to `gh pr list`, to `brief`, to the plane. The live instance: a
  system-review defect's fix was written, committed and pushed, the issue stayed
  open, and it surfaced only because a manager asked by hand.

  **PR head behind its own branch ref.** A pull request exists and its head
  persisted at an OLDER sha than the branch it tracks — measured across both the
  GraphQL and REST surfaces, two reads each. CI was green on the old code and the
  new commit had no checks at all, so every merge rung answered truthfully about
  a commit that was no longer the tip.

The first is "no PR to look at"; the second is "a PR that is looking at the wrong
commit". A rung covering only one leaves the other whole.

**WHY A READ-SIDE RUNG AND NOT A CHECK IN `report-back.sh`.** That door is
non-blocking by design and runs on every report; a network call there would put
the fleet's reporting path behind GitHub's availability. This runs where reads
already happen and costs the reporting path nothing.

**THE MEMBERSHIP TEST IS EXACT, AND THAT IS THE WHOLE CORRECTNESS ARGUMENT.**
The obvious implementation — list the N most recent PRs and treat any branch not
in that list as having no PR — is wrong in one direction only, and it inflated
the issue's own census from ~1 to 31: a branch whose PR is older than the cap
appears falsely as undelivered. So the bulk list is used only for what it CAN
establish. It can produce a false *no-PR*; it can never produce a false *has-PR*.
Every apparent no-PR branch is therefore confirmed by an exact per-branch query
(`pulls?head=owner:branch&state=all`, which has no cap), and only those. On a
clean estate that is zero or one extra call, so exactness costs almost nothing —
the cap is not a bound on the answer, only on how much of it comes free.

**IT STATES ITS BOUNDS IN ITS OWN OUTPUT** (#1742's rule for windowed readers):
the window, the PR-list cap, how many confirmations it spent, and anything it
skipped. Silence that could mean "clean" or could mean "not looked" is the class
this rung exists to close, so it must not reproduce it one level up.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

#: Only branches pushed inside this window are candidates. Older ones are
#: indistinguishable from abandoned work by any automatic means -- the issue's
#: own census found 31 no-PR branches of which exactly 1 was recent, and said
#: plainly that "recent" still cannot separate stranded delivery from work
#: legitimately in flight. That judgement stays a human's.
DEFAULT_WINDOW_DAYS = 7

#: How many PRs the ONE bulk list fetches. Raising it makes fewer branches need
#: a confirming call; it never changes the answer, because every apparent no-PR
#: is confirmed exactly (see the module docstring).
DEFAULT_PR_CAP = 400

#: Hard ceiling on confirming calls, so a repo with a hundred stale branches
#: cannot turn a doctor run into a hundred round trips. Anything past it is
#: REPORTED AS UNCHECKED rather than silently assumed clean -- the one direction
#: this rung must never fail in.
DEFAULT_CONFIRM_CAP = 12

#: Wall-clock budget per repo. Measured on this estate after batching the ref
#: read: 4.1 s for a repo with 147 branches and 5 open PRs, 1.5 s for a smaller
#: one. The budget exists so a slow or large repo cannot silently make `doctor`
#: unpleasant enough to be skipped -- and a repo that exceeds it is reported
#: UNCHECKED, never clean, which is the only direction this rung may fail in.
DEFAULT_BUDGET_S = 20.0


@dataclass
class DeliveryFindings:
    """What the check found, and what it could not reach."""

    no_pr: list[str] = field(default_factory=list)          # branch names
    stale_pr_head: list[str] = field(default_factory=list)  # "#N branch pr=.. ref=.."
    unchecked: list[str] = field(default_factory=list)      # branches past the cap
    notes: list[str] = field(default_factory=list)          # why something was skipped
    confirmations: int = 0
    pr_cap: int = DEFAULT_PR_CAP
    window_days: int = DEFAULT_WINDOW_DAYS

    @property
    def clean(self) -> bool:
        return not (self.no_pr or self.stale_pr_head)

    def bound_line(self) -> str:
        """The bounds, always, whatever the verdict (#1742)."""
        bits = [f"window {self.window_days}d",
                f"PR-list cap {self.pr_cap}",
                f"{self.confirmations} exact confirmation(s)"]
        if self.unchecked:
            bits.append(f"{len(self.unchecked)} branch(es) UNCHECKED (confirm cap "
                        f"or time budget) — not a clean answer for those")
        return "; ".join(bits)


def _sh(argv: list[str], cwd: str | None = None, timeout: float = 20.0
        ) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _open_prs(repo: str) -> tuple[list[dict], str | None]:
    """Every OPEN pr with its head ref AND head oid — one call, both halves of
    the stale-head comparison."""
    rc, out = _sh(["gh", "pr", "list", "--repo", repo, "--state", "open",
                   "--limit", "200", "--json", "number,headRefName,headRefOid"])
    if rc != 0 or not out:
        return [], f"could not list open PRs for {repo}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return [], f"unreadable PR list for {repo}"


def _all_branch_refs(repo: str) -> tuple[dict[str, str], str | None]:
    """EVERY branch ref -> sha in ONE call.

    The obvious shape asks `git/ref/heads/<branch>` per open PR, and that makes
    the rung's cost scale with the number of open pull requests: measured, 2.5 s
    for five PRs on this estate, so a repo with thirty would spend ~15 s here
    alone and the rung would be skipped — which is the failure mode a costly
    health check actually has. `git/matching-refs/heads` returns all of them at
    once (147 refs, 1.0 s measured), so the comparison becomes O(1) in open PRs.
    """
    rc, out = _sh(["gh", "api", f"repos/{repo}/git/matching-refs/heads",
                   "--jq", ".[] | [.ref, .object.sha] | @tsv"], timeout=30.0)
    if rc != 0 or not out:
        return {}, f"could not read branch refs for {repo}"
    refs: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].startswith("refs/heads/"):
            refs[parts[0][len("refs/heads/"):]] = parts[1]
    return refs, None


def _branches_with_any_pr(repo: str, cap: int) -> tuple[set[str], str | None]:
    """Head refs of the most recent `cap` PRs, ANY state. Establishes has-PR
    only — never absence (see the module docstring)."""
    rc, out = _sh(["gh", "pr", "list", "--repo", repo, "--state", "all",
                   "--limit", str(cap), "--json", "headRefName"])
    if rc != 0 or not out:
        return set(), f"could not list PRs for {repo}"
    try:
        return {r["headRefName"] for r in json.loads(out)}, None
    except (json.JSONDecodeError, KeyError, TypeError):
        return set(), f"unreadable PR list for {repo}"


def _has_pr_exactly(repo: str, branch: str) -> bool | None:
    """THE exact membership test: uncapped, per branch. None = unreachable,
    which is reported as unchecked rather than resolved either way."""
    owner = repo.split("/", 1)[0]
    rc, out = _sh(["gh", "api",
                   f"repos/{repo}/pulls?head={owner}:{branch}&state=all",
                   "--jq", "length"])
    if rc != 0 or not out.isdigit():
        return None
    return int(out) > 0


def check_repo(repo: str, checkout: str, *, default_branch: str = "main",
               window_days: int = DEFAULT_WINDOW_DAYS,
               pr_cap: int = DEFAULT_PR_CAP,
               confirm_cap: int = DEFAULT_CONFIRM_CAP,
               budget_s: float = DEFAULT_BUDGET_S) -> DeliveryFindings:
    """Both halves for one repo. Local git where possible, network only where
    the answer genuinely lives on GitHub."""
    import time as _time
    _deadline = _time.monotonic() + budget_s
    f = DeliveryFindings(pr_cap=pr_cap, window_days=window_days)

    # --- half 2: an open PR whose head is behind its branch ref --------------
    # ONE call gives every open PR's head ref and oid; the branch's own tip comes
    # from the same list's ref name resolved against the remote, so the
    # comparison is between two values GitHub itself reports.
    prs, note = _open_prs(repo)
    if note:
        f.notes.append(note)
    all_refs, ref_note = _all_branch_refs(repo) if prs else ({}, None)
    if ref_note:
        f.notes.append(ref_note + " — stale-head half not run")
    for pr in prs:
        branch, head = pr.get("headRefName"), pr.get("headRefOid")
        if not branch or not head:
            continue
        ref = all_refs.get(branch)
        if not ref:
            if not ref_note:
                f.notes.append(
                    f"#{pr.get('number')} {branch}: no branch ref — unchecked "
                    "(the branch may have been deleted)")
            continue
        if ref != head:
            f.stale_pr_head.append(
                f"#{pr.get('number')} {branch}: PR head {head[:12]} != branch ref "
                f"{ref[:12]} — rungs anchored to the PR head would verify the "
                f"wrong commit")

    # --- half 1: a branch ahead of the default branch with no PR ------------
    if not checkout:
        f.notes.append(f"{repo}: no local checkout — branch half not run")
        return f
    rc, out = _sh(["git", "for-each-ref", "--format=%(refname:short) %(committerdate:unix)",
                   f"refs/remotes/origin"], cwd=checkout)
    if rc != 0:
        f.notes.append(f"{repo}: could not enumerate branches — branch half not run")
        return f
    import time
    cutoff = time.time() - window_days * 86400
    recent: list[str] = []
    for line in out.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        name, when = parts[0], int(parts[1])
        short = name.split("/", 1)[1] if "/" in name else name
        if short in (default_branch, "HEAD") or when < cutoff:
            continue
        # Ahead of the default branch? Local, no network.
        rc2, cnt = _sh(["git", "rev-list", "--count",
                        f"origin/{default_branch}..{name}"], cwd=checkout)
        if rc2 == 0 and cnt.isdigit() and int(cnt) > 0:
            recent.append(short)

    known, note = _branches_with_any_pr(repo, pr_cap)
    if note:
        f.notes.append(note)
    apparent = [b for b in recent if b not in known]
    for branch in apparent:
        if _time.monotonic() > _deadline:
            f.unchecked.append(branch)
            if not any("budget" in n for n in f.notes):
                f.notes.append(
                    f"{repo}: {budget_s:.0f}s budget spent — remaining branches "
                    "UNCHECKED, not assumed clean")
            continue
        if f.confirmations >= confirm_cap:
            f.unchecked.append(branch)
            continue
        f.confirmations += 1
        exact = _has_pr_exactly(repo, branch)
        if exact is None:
            f.unchecked.append(branch)
            f.notes.append(f"{branch}: exact PR query unreachable — unchecked")
        elif not exact:
            f.no_pr.append(branch)
    return f
