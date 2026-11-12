---
title: Auto-merge after peer review
description: Manager auto-merges PRs after a peer review verdict + CI green. Does not use --admin — requires GitHub branch protection to allow it.
---

# Auto-merge after peer review

The reasons behind these rungs, with the measurements and incidents they rest on, are in `.claude/skills/fleet-ops/merge-policy-reference.md`; `§X` below means its section X, and it adds no rule.

The manager auto-merges PRs when ALL of:

0. **RECONCILE THE HEAD BEFORE ANCHORING ANYTHING TO IT — and a mismatch is never a pass.**

   ```bash
   REPO=<owner/repo>; N=<n>
   BR=$(gh pr view "$N" --repo "$REPO" --json headRefName --jq .headRefName)
   PH=$(gh api "repos/$REPO/pulls/$N" --jq .head.sha)          # full 40-char oid
   RH=$(gh api "repos/$REPO/git/ref/heads/$BR" --jq .object.sha)
   [ "$PH" = "$RH" ] || { echo "REFUSE: PR head $PH != branch ref $RH"; exit 1; }
   ```

   The measurement behind this rung: §Rung 0.

   **It does NOT self-resolve on re-query the way `mergeable` does.** Rung 3 tells you to re-query a lazy `UNKNOWN`; that instinct is wrong here and applying it wastes the window. Refuse, confirm the push landed (`git ls-remote <remote> refs/heads/$BR` against your local `HEAD`), and start the rungs again.

   Why it is a mechanism, not a reminder: §Rung 0.

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict (or `COMMENT` with a `**[<bot>] [VERDICT] approve**` verdict line anchored to the reviewed SHA under same-identity fallback). Read `claudlobby --json task reviews OWNER/REPO --pr N` for current-head verdicts and recorded reviewer attribution; this read does not authorize merging. A verdict counts only when that read attributes it `MATCH` to a recorded review-role report; one it reads `UNKNOWN` or `AMBIGUOUS` never counts.

   **MULTIPLE VERDICTS RESOLVE PER REVIEWER, NEVER GLOBAL-LATEST.** Each reviewer's own latest verdict stands, and the PR is blocked while **any** reviewer's latest is `REQUEST-CHANGES`.

   Why not global-latest: §Rung 1.

   **Read the recorded state from `claudlobby --json task reviews OWNER/REPO --pr N`.** What it resolves and reports: §Rung 1. The read does not authorize a merge.

2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.** Why: §Rung 2.

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.** Why: §Rung 2.

   **ABSENCE is the failure mode, so absence is what this rung tests for.** How it happens, and why a count does not survive it: §Rung 2.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.** Why: §Rung 2.

   Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

   **Do not rely on branch protection to enforce this for you — it enforces something else.** What it enforced when measured: §Rung 2.

   Two cautions, because the obvious ways to check this both return confident wrong answers. Read the **rulesets** API, not `/branches/main/protection` — the legacy endpoint answers `404 Branch not protected` for a repo fully protected by a ruleset, asserting a negative it has no standing to assert. Then read the ruleset's **`enforcement`** field, because one can exist and enforce nothing (`enforcement: disabled`). The two failures point in **opposite** directions: the legacy endpoint calls a protected repo unprotected, a bare ruleset listing calls an unprotected one protected.

   **One name is fixed in every repo that has adopted it: the rollout check** (`verify-rollout`). Read whether the repo's default branch carries `.github/workflows/rollout-check.yml`: only a 404 means the repo has not adopted the check, and any other failed read refuses. Where it has, the newest `rollout-check / Rollout check` run at the head must be `SUCCESS`. The rollup lists every run of a name, so a read that takes any `SUCCESS` would pass a stale green after a red body edit. Anything but `SUCCESS` in the newest run, or no run, refuses. A run that has not completed has not passed, newest or not, so the read names it `PENDING` rather than trusting timestamps to order runs that have not finished. Run it in the same call as rung 0, which sets `$REPO` and `$N`:

   ```bash
   DEFAULT=$(gh api "repos/$REPO" --jq .default_branch) || { echo "REFUSE: cannot read the default branch"; exit 1; }
   if ERR=$(gh api "repos/$REPO/contents/.github/workflows/rollout-check.yml?ref=$DEFAULT" --silent 2>&1); then
     ROLLOUT=$(gh pr view "$N" --repo "$REPO" --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.name == "rollout-check / Rollout check")] | if any(.status != "COMPLETED") then "PENDING" else (sort_by(.startedAt) | last | .conclusion // "ABSENT") end') || { echo "REFUSE: cannot read the rollout check"; exit 1; }
     [ "$ROLLOUT" = SUCCESS ] || { echo "REFUSE: the rollout check reads $ROLLOUT"; exit 1; }
   else
     case "$ERR" in *"HTTP 404"*) echo "This repo has not adopted the rollout check" ;; *) echo "REFUSE: cannot tell whether this repo runs the rollout check: $ERR"; exit 1 ;; esac
   fi
   ```

   **A PR that changes anything under `.github/workflows/` gets no proof from that `SUCCESS`.** Why: §Rung 2. So the merger reads the PR's own file list from the API, every page and each rename's old name, and a failed read refuses. A PR that lists any workflow file merges only on the rung 1 verdict (a different bot's, at this head) that names each one:

   ```bash
   WF=$(gh api --paginate "repos/$REPO/pulls/$N/files" --jq '.[] | .filename, (.previous_filename // empty)') || { echo "REFUSE: cannot read the PR's files"; exit 1; }
   WF=$(printf '%s\n' "$WF" | grep '^\.github/workflows/' || true)
   [ -z "$WF" ] || { printf 'WORKFLOW CHANGE: %s\n' $WF; echo "Merge only on the rung 1 verdict that names each workflow file above."; }
   ```

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". How GitHub computes it: §Rung 3. `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test. Why: §Rung 3.

4. **KEEP THE BRANCH WHILE ANY OPEN PR IS BASED ON IT.** Why: §Rung 4. Delete only after a listing that succeeded and came back empty. A failed listing or an empty `$BR` (`gh pr list --base ""` is no filter) keeps the branch too:

   ```bash
   DELETE=--delete-branch
   [ -n "$BR" ] && STACKED=$(gh pr list --repo "$REPO" --base "$BR" --state open --json number --jq '.[].number') && [ -z "$STACKED" ] || { DELETE=""; echo "KEEPING $BR"; }
   ```

   Run rung 0, this rung and the merge command in one call: the Bash tool keeps no variables between calls, and a `$DELETE` that was never set just keeps the branch.

5. **NO OPEN ROLLOUT HOLD on a repo that has adopted the rollout check** (`verify-rollout`). This rung applies only where the repo's default branch carries `.github/workflows/rollout-check.yml`, read as rung 2 reads it. Elsewhere it is inert: a fleet that composes this guardrail without opting into `verify-rollout` gains no hold listing that could refuse its merge. Only a 404 says the repo has not adopted the check; any other failed read refuses, because it cannot tell the two apart. Where the rung applies, a failed rollout check opens an issue labelled `rollout-hold`. While one is open, only a PR that fixes or reverts the change that failed may merge into the repo, and it needs both:
   - **It closes that hold** (`closingIssuesReferences`, filled from a `Closes #N` keyword in its body). It may close any one open hold: requiring every hold would deadlock two independent ones.
   - **The rung 1 verdict names the hold:** a different bot's verdict, at this head, that names the hold issue and says this PR fixes or reverts the change that failed. The keyword is the author's to write, and a body edit after the review adds it without moving the head, so the keyword never grants the exception by itself.

   A failed listing or read refuses, like rung 4's. Run it in the same call as rung 0, which sets `$REPO` and `$N`. When the PR closes a hold, the snippet names it, and the verdict is held to that name:

   ```bash
   DEFAULT=$(gh api "repos/$REPO" --jq .default_branch) || { echo "REFUSE: cannot read the default branch"; exit 1; }
   if ERR=$(gh api "repos/$REPO/contents/.github/workflows/rollout-check.yml?ref=$DEFAULT" --silent 2>&1); then
     HOLDS=$(gh issue list --repo "$REPO" --label rollout-hold --state open --json number --jq '.[].number') || { echo "REFUSE: the rollout-hold listing failed"; exit 1; }
     if [ -n "$HOLDS" ]; then
       CLOSES=$(gh pr view "$N" --repo "$REPO" --json closingIssuesReferences --jq '.closingIssuesReferences[].number') || { echo "REFUSE: cannot read what this PR closes"; exit 1; }
       HOLD=$(printf '%s\n' $HOLDS | grep -xF -f <(printf '%s\n' $CLOSES) | head -1) || true
       [ -n "$HOLD" ] || { echo "REFUSE: rollout hold open: $HOLDS"; exit 1; }
       echo "This PR closes rollout hold #$HOLD: merge only if the rung 1 verdict names #$HOLD and says this PR fixes or reverts the change that failed."
     fi
   else
     case "$ERR" in *"HTTP 404"*) echo "This repo has not adopted the rollout check: no hold applies" ;; *) echo "REFUSE: cannot tell whether this repo runs the rollout check: $ERR"; exit 1 ;; esac
   fi
   ```

   After the merge, the PR's own rollout check is the merger's to run, once the change is live: see `verify-rollout`.

Merge command — **carrying the same `$PH` rung 0 anchored to**:

```bash
[ -n "$REPO" ] && [ -n "$N" ] && [ -n "$PH" ] || { echo "REFUSE: REPO, N and PH (rung 0) are not all set in this shell; run rung 0 and this merge in one call"; exit 1; }
gh pr merge "$N" --repo "$REPO" --squash $DELETE --match-head-commit "$PH"
```

**This matters more here than under `--admin`, not less.** This policy relies on branch protection to allow the merge, and protection does not check that the head you verified is the head you are merging. So the flag is the only thing refusing a head that moved between rung 0 and the merge. The measurement: §The merge flag.

**Reuse `$PH` — never re-read or abbreviate it.** The flag needs the full 40-character oid and **fails closed** on anything else. Its refusal, and why one variable: §The merge flag.

**Not auto-merged:**
- PRs with `Request Changes` verdict — bounce to engineer first.
- PRs with unresolved review threads.
- PRs where CI is failing or pending — **or where a required workflow is missing from the rollup.** "Not failing" is not "passed": an absent workflow cannot fail.
- PRs the manager authored (self-merge requires a second reviewer).
- PRs into a repo that has adopted the rollout check while a `rollout-hold` issue is open, except one that closes it and whose rung 1 verdict names it (rung 5).

The manager posts "Merging #NN" to Telegram before executing, so the human has visibility — **naming any branch rung 4 kept**, because the stacked PR's author is told nowhere else.
