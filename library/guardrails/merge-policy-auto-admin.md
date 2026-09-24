---
title: Auto-merge with --admin after peer review
description: Manager auto-merges PRs using --admin after a real peer review verdict + CI green. For same-identity fleets where regular merge is blocked by branch protection.
---

# Auto-merge with --admin after peer review

The manager auto-merges PRs using `--admin` when ALL of:

0. **RECONCILE THE HEAD BEFORE ANCHORING ANYTHING TO IT — and a mismatch is never a pass.** Read the PR's head and the branch's own ref, and refuse if they differ:

   ```bash
   REPO=<owner/repo>; N=<n>
   BR=$(gh pr view "$N" --repo "$REPO" --json headRefName --jq .headRefName)
   PH=$(gh api "repos/$REPO/pulls/$N" --jq .head.sha)          # full 40-char oid
   RH=$(gh api "repos/$REPO/git/ref/heads/$BR" --jq .object.sha)
   [ "$PH" = "$RH" ] || { echo "REFUSE: PR head $PH != branch ref $RH"; exit 1; }
   ```

   **Why this rung exists, and why the paragraphs below it were not enough.** This page already tells you to anchor your evidence to a head, in several places and at length. That did not help, because **the failure is not an operator forgetting to anchor — it is the anchor being reported stale by the surface the operator is told to read.** No amount of instruction to check the head fixes a head that answers wrongly.

   Measured on this estate: a PR head persisted at an **old** sha across **both** the GraphQL and REST surfaces, two reads each, while the branch ref sat one commit ahead. CI was green **on the old code** and the new commit had no checks at all. So every rung below — review, checks, mergeable — was answering truthfully about a commit that was no longer the branch tip, and would have passed.

   **It does NOT self-resolve on re-query the way `mergeable` does.** Rung 3 below tells you to re-query a lazy `UNKNOWN` until it settles; that instinct is wrong here and applying it wastes the window. A stale head stays stale, so the only safe response is to refuse and re-read after a push has demonstrably landed.

   **A mismatch means you do not yet know what you are merging.** It is not a slow answer to wait out — it is two sources disagreeing about which commit the PR *is*, and there is no rule for picking a winner. Refuse, confirm the push landed (`git ls-remote <remote> refs/heads/$BR` against your local `HEAD`), and start the rungs again.

1. **Peer review posted — and attributed BY HAND.** A reviewer has posted an `APPROVE` verdict, or a `COMMENT` with `**Approve**` verdict line (same-identity fallback). The review must be from a different bot than the PR author — no self-reviews.

   **MULTIPLE VERDICTS RESOLVE PER REVIEWER, NEVER GLOBAL-LATEST.** Each reviewer's own latest verdict stands, and the PR is blocked while **any** reviewer's latest is `REQUEST-CHANGES`.

   Global-latest is the intuitive rule and it is wrong in one specific, silent way: with reviewer A blocking and reviewer B approving later, newest-on-the-PR reports `APPROVE` **over an unresolved block**. It is correct only while a PR has exactly one reviewer — which is why it survives so long on a fleet where that is usually true, and why it fails the first time two people review.

   **This codebase already settled it, so read the rule from the tool rather than from here:** `lib/pr-review-state.py` documents the prototype's global-latest, the reversed-reviewers counterexample, and its own resolution, and `test_reversing_the_reviewers_flips_the_answer` pins it. If this paragraph and that tool ever disagree, the tool is right — it is the thing with a test.

   (That is not in tension with the warning below about not reading the tool for **authorship**. It is authoritative on *which verdicts are live and blocking* and is not an authority on *who wrote the PR* — two different questions, one of which it answers.)

   **This rung cannot be verified from GitHub, and no shipped door checks it for you.** Read that before the rungs below, because every automated surface on this page reads green on a self-review. The fleet shares one GitHub identity, so every mechanical source of authorship collapses to the same login: the PR `author` field (18 of 18 recent PRs), the commit author and committer (12 of 12 merged commits, resolved from a host-global gitconfig — and rewritten by squash-merge anyway), and the branch name (18 of 18 encode an issue number, never a bot). There is no field to read, so the comparison this rung asks for is `x != x`.

   **So perform it by hand, on every `--admin` merge.** Authorship is recorded in exactly one place on this estate — the prose of the report a bot files when it opens a PR:

   ```bash
   claudlobby --fleet <fleet> report-back --since 7d --json | grep -E 'pull/<N>([^0-9]|$)'
   ```

   Read the rows in time order and find the one saying the PR was *opened* — `"clauDNA PR #328 opened: …"`. That bot is the author. If a clearing verdict on the same PR comes from that same bot, **rung 1 is unsatisfied: do not merge**, however green everything else reads. Bound the number or the match is wrong in the silent direction: plain `pull/328` also matches `pull/3281`.

   **The manager's own authorship needs no detector** — it knows what it wrote. The case this manual check exists for is the third-party one: bot A opens, bot A approves, the manager merges. Nothing visible to that manager distinguishes it from a clean peer review.

   **When it cannot answer, this rung REFUSES — it never passes.** Three states are not a pass:

   - **No opening row at all.** Over the 21 PRs created in the current plane epoch, **4 (19%) have no citing report of any kind** — one of them merged. No answer is available at any confidence.
   - **An empty summary.** Authorship lives in a content-capped field: **17 of 57 PR-citing task events (29%) carry an empty summary**, and a restrictive capture policy strips that prose by design. The one channel this rung depends on is a setting away from returning nothing — and "nothing" reads exactly like "no self-review".
   - **The author is on another fleet.** `report-back` is fleet-scoped. Measured: the same PR returns 5 rows under `--fleet ai-platform` and **0 under `--fleet crog-eng-team`**. Zero rows from the wrong fleet is indistinguishable from zero rows because nobody reported.

   In all three the honest output is "rung 1 unanswerable — verify by hand or do not merge", never a pass. An *empty* answer is at least distinguishable from a *failed* one: the door prints `0 event(s) matched — read N row(s) from the plane (fleet F)`, and refuses at rc 3 rather than printing an empty table when it cannot reach the plane. So zero-matched-out-of-N means the plane answered and the row is genuinely absent — which is a refusal, not a clear.

   **Do NOT read `lib/pr-review-state.py`'s output as bearing on this rung.** It does not fetch PR authorship and never has — its `PR_FIELDS` is `number,title,reviews,comments,headRefOid`. The name it reports is the *reviewer's* self-reported name parsed out of a verdict header, which is the other operand entirely. A clean `SUMMARY: N live verdict(s), 0 blocking` is a true statement about verdicts and says nothing whatever about who wrote the PR. Measured on a real self-review: it read `2 live verdict(s), 1/2 anchored, 0 stale, 0 blocking` on a PR whose author had cleared their own work.
2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.** Workflow names are a property of a repository, not of a fleet: one repo's set may be `Lint` / `Test` / `Security Scan` / `Changelog Check` while another's is `api-ci` / `frontend-ci`. A list written into this guardrail would be correct for exactly one repo and silently wrong on every other — hunting for names that do not exist there, and so either blocking every PR on that repo or, worse, being quietly softened by whoever hits it first. **The softening is the real hazard: a guardrail that fires wrongly gets weakened, and the weakening outlives the repo that caused it.**

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.** Where workflows are path-filtered — say `api-ci` on `api/**` and `frontend-ci` on `frontend/**` — a docs-only PR triggers neither, so no set is always-present and a required context would strand such a PR permanently. Under a hardcoded list that repo is a special case somebody has to remember; under declaration its exclusion falls out of the design: nothing declared, nothing to verify, and the gap is visible instead of forgotten.

   **ABSENCE is the failure mode, so absence is what this rung tests for.** A PR with conflicts has no computable merge commit, so `pull_request`-triggered workflows are **never scheduled** — while GitHub App integrations keep reporting `SUCCESS` against the head SHA. The rollup then reads all-success *with the entire test suite missing*, and a naive green gate passes. Measured on a real conflicting PR: three checks, all passing, `gh pr checks` exit `0`, no tests run.

   A **count** does not survive this. It is a proxy for "the tests ran": it breaks the moment a workflow is added or removed, and it is satisfied by the wrong N.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.** `statusCheckRollup` is the union of the Checks API and the legacy commit-Status API, so by construction it carries every gate whichever surface reports it. `/check-runs` finds all four names today, and keeps working only for as long as nobody ever adds a required gate as a legacy commit status — which a plain Vercel entry on one of these repos already is. Measured on one real commit: 2 check-runs, 1 status, 3 in the rollup. That is evidence a second surface exists and *can* carry a gate; it is not a claim that any of the four hides there today.

   **Incidentally correct is a proxy** — the same distinction as counting versus naming, one layer down. A thing that happens to be right is not a thing that must be right, and only the second belongs in a guardrail. Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". GitHub computes this field **lazily**: the first read after a push returns `UNKNOWN`, and only a re-query resolves it. `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test. That field reports `BLOCKED` for the ordinary case of a PR still awaiting its review, so gating on `clean`/`unstable` refuses PRs that are perfectly mergeable.

4. **NO OPEN PR MAY STILL BE BASED ON THE BRANCH THIS MERGE DELETES — retarget each one FIRST.** An open PR whose *base* is this PR's head branch is **closed** by GitHub the moment `--delete-branch` removes that branch: not retargeted, not reported, and its author is not told. Observed: #1779 went `CLOSED` two seconds after #1776 merged, and its author found it twenty minutes later (#1781). So before the merge, move every dependent onto this PR's own base, and keep `--delete-branch` only if none is left:

   ```bash
   BASE=$(gh pr view "$N" --repo "$REPO" --json baseRefName --jq .baseRefName)
   STACKED=$(gh pr list --repo "$REPO" --base "$BR" --state open --json number --jq '.[].number')
   for M in $STACKED; do gh pr edit "$M" --repo "$REPO" --base "$BASE"; done
   DELETE=--delete-branch
   LEFT=$(gh pr list --repo "$REPO" --base "$BR" --state open --json number --jq '.[].number')
   [ -z "$LEFT" ] || { DELETE=""; echo "KEEPING $BR: still the base of $LEFT"; }
   ```

   `$BR` is rung 0's head branch. **The re-list is the check, not the edit's exit code**: a PR that still names `$BR` as its base is exactly what the deletion would close, however the edit reported. A branch kept for a dependent is deleted once that PR has been retargeted or closed. **A retargeted PR needs its new base merged into it after this merge**: a squash lands this PR's commits under a new sha, so the dependent's own copies of them conflict until its author merges the base in — which is why the announcement below names it.

Merge command — **carrying the same `$PH` rung 0 anchored to**:

```bash
gh pr merge "$N" --repo "$REPO" --squash --admin $DELETE --match-head-commit "$PH"
```

`$DELETE` is rung 4's, unquoted on purpose: a kept branch leaves no argument at all rather than an empty one.

**`--match-head-commit` makes the gate refuse a head that moved under it**, which rung 0 cannot do on its own: rung 0 reconciles at the *start*, and the rungs take time. This closes the window between them.

**Reuse `$PH` — do not re-read the sha, and never abbreviate it.** The flag requires the full 40-character object id and **fails closed** on anything else:

```
$ gh pr merge <n> --squash --admin --delete-branch --match-head-commit bda6de9
GraphQL: Variable $input of type MergePullRequestInput! was provided invalid value
for expectedHeadOid (Could not coerce value "bda6de9" to GitObjectID)
```

The merge did **not** proceed and the PR stayed `OPEN` — verified on a real merge. That direction is the right one: an abbreviation is *rejected*, never silently coerced or ignored. A flag that quietly no-opped on a malformed value would be worse than no flag, because the gate would report itself protected while protecting nothing — the same "reads green on the wrong thing" family as the stale head itself.

`$PH` from rung 0 is already the full id, so **using one variable for both the anchor and the flag makes the abbreviation mistake unavailable by construction** — which is why this page hands you a variable rather than telling you to be careful.

**Why --admin:** Same-identity fleets share one GitHub PAT. Branch protection's "required approvals" check counts only formal `APPROVE` state, which GitHub blocks for same-identity. `--admin` bypasses the branch protection gate — but the **real gate is the peer review verdict**, not GitHub's checkbox. And a verdict is only a gate once rung 1 has attributed it: the same bypass that makes `--admin` necessary is what makes a self-review invisible.

**Why the rungs above carry the whole weight.** There *is* a server-side backstop — it simply does not cover this, and it covers far less than a first look suggests. Measured across **nine** repos of one estate via the repository **rulesets** API (re-measured 2026-08-22):

| state | count | repos |
|---|---|---|
| ruleset `enforcement: active` | **4** | branch deletion, non-fast-forward, pull-request rules |
| ruleset present but `enforcement: disabled` | **2** | covers nothing in practice |
| **no ruleset possible at all** — private repo on a plan returning `403 Upgrade to GitHub Pro or make the repository public` | **3** | nothing to configure, nothing to bypass |

**So five of the nine have no ruleset enforcing anything at all** — and those five are not one bucket. The two `disabled` repos are one flag flip from live; the three plan-403 repos **cannot be protected at any configuration** without a paid upgrade. Same headline, opposite remediation cost: reserve "unenforceable" for the plan-403 three, where it is literally true.

Of the six that carry a ruleset, **four DECLARE an approval requirement and only three ENFORCE one** — declared and enforced are separate counts, which is what *A present ruleset proves nothing until you read its `enforcement` field* (below) is about. Read the rule, not its presence: one active repo carries a `pull_request` rule with **`required_approving_review_count: 0`**, which declares the *absence* of an approval requirement rather than one; and one of the disabled pair carries no `pull_request` rule at all.

**Not one of the nine declares `required_status_checks`. Zero of nine** — verified rule-by-rule on all six rulesets, not inferred.

**Do not read the plan-403 state as an edge case.** It is a *third* of this estate, and a repo in it cannot be protected at any configuration — so on those repos an auto-merge clause does not "fail closed against a server-side control"; it simply merges. A prior revision of this paragraph surveyed six repos and reported the disabled and plan-403 states as one repo each, which invited exactly that misreading.

**Be precise about what that leaves enforced, because it is less than it looks.** Counting the whole nine, not the six with rulesets: force-push and branch-deletion are enforced on **4 of 9**; an approving review on **3 of 9**; and whether the tests ran on **0 of 9**. So GitHub enforces force-push protection on under half this estate, an approving review on a third of it, and **nothing anywhere** about test status. That is precisely and only the gap these rungs address — they are not a belt beside braces, they are the sole control over exactly the failure mode described above them.

**Check the right surface, or you will confidently conclude the opposite.** That measurement is from the rulesets API, **not** `/branches/main/protection`. The legacy endpoint returns `404 Branch not protected` for a repo that is fully protected by a ruleset — asserting a negative in plain English that it has no standing to assert. This is the same guaranteed-versus-incidental distinction as the rollup above, one layer out: a surface that answers is not the same as a surface that answers *completely*.

**A present ruleset proves nothing until you read its `enforcement` field.** A ruleset can exist and enforce nothing (`enforcement: disabled`), and **two** repos in that survey are in exactly that state. The two obvious checks then fail in **opposite** directions: the legacy endpoint reports "not protected" about a repo that is, while a ruleset *listing* reports "protected" about a repo that is not. Two confident wrong answers pointing opposite ways — only the `enforcement` field settles it, and neither reviewer who checked this had that third state in hand.

**Red lines (even with --admin):**
- Never `--admin` merge without an actual peer review on the PR.
- Never `--no-verify` — hooks exist for a reason.
- Never force-push main.
- Never merge a PR with `Request Changes` verdict outstanding.
- Never merge a PR where CI is failing — **or where a required workflow is missing from the rollup.** "Not failing" is not "passed": an absent workflow cannot fail.
- Never merge a PR the manager itself authored without a separate reviewer.
- Never `--delete-branch` a branch an open PR is still based on (rung 4): the deletion closes that PR silently.
- Never treat an **unanswerable** rung 1 as a satisfied one. No authorship row is a refusal, not a clear — the same absence is produced by a self-review nobody reported, by a stripped summary, and by querying the wrong fleet.

The manager posts "Merging #NN (--admin, reviewed by <reviewer>)" to Telegram before executing, **naming every PR rung 4 retargeted** ("retargeted #M to <base>; merge <base> into it") **and any branch it kept for a dependent** — the stacked PR's author is told nowhere else.

**This guardrail replaces both `merge-policy-human` and `no-merge-admin`.** Do not stack with either.
