---
title: Auto-merge with --admin after peer review
description: Manager auto-merges PRs using --admin after a real peer review verdict + CI green. For same-identity fleets where regular merge is blocked by branch protection.
---

# Auto-merge with --admin after peer review

The reasons behind these rungs, with the measurements and incidents they rest on, are in `.claude/skills/fleet-ops/merge-policy-reference.md`; `§X` below means its section X, and it adds no rule.

The manager auto-merges PRs using `--admin` when ALL of:

0. **RECONCILE THE HEAD BEFORE ANCHORING ANYTHING TO IT — and a mismatch is never a pass.** Read the PR's head and the branch's own ref, and refuse if they differ:

   ```bash
   REPO=<owner/repo>; N=<n>
   BR=$(gh pr view "$N" --repo "$REPO" --json headRefName --jq .headRefName)
   PH=$(gh api "repos/$REPO/pulls/$N" --jq .head.sha)          # full 40-char oid
   RH=$(gh api "repos/$REPO/git/ref/heads/$BR" --jq .object.sha)
   [ "$PH" = "$RH" ] || { echo "REFUSE: PR head $PH != branch ref $RH"; exit 1; }
   ```

   Why this rung exists: §Rung 0.

   **It does NOT self-resolve on re-query the way `mergeable` does.** Rung 3 below tells you to re-query a lazy `UNKNOWN` until it settles; that instinct is wrong here and applying it wastes the window. A stale head stays stale, so the only safe response is to refuse and re-read after a push has demonstrably landed.

   **A mismatch means you do not yet know what you are merging.** It is not a slow answer to wait out — it is two sources disagreeing about which commit the PR *is*, and there is no rule for picking a winner. Refuse, confirm the push landed (`git ls-remote <remote> refs/heads/$BR` against your local `HEAD`), and start the rungs again.

1. **Peer review posted — and attributed BY HAND.** A reviewer has posted an `APPROVE` verdict, or a `COMMENT` with a `**[<bot>] [VERDICT] approve**` verdict line, anchored to the reviewed SHA (same-identity fallback). The review must be from a different bot than the PR author — no self-reviews. `claudlobby --json task reviews OWNER/REPO --pr N` reads the recorded reviewer attribution and current-head verdict, but does not establish authorship or authorize merging. A verdict counts only when that read attributes it `MATCH` to a recorded review-role report; one it reads `UNKNOWN` or `AMBIGUOUS` never counts.

   **MULTIPLE VERDICTS RESOLVE PER REVIEWER, NEVER GLOBAL-LATEST.** Each reviewer's own latest verdict stands, and the PR is blocked while **any** reviewer's latest is `REQUEST-CHANGES`.

   **Read the recorded state from `claudlobby --json task reviews OWNER/REPO --pr N`.** The read does not authorize a merge.

   (That is not in tension with the warning below about not reading the tool for **authorship**. It is authoritative on *which verdicts are live and blocking* and is not an authority on *who wrote the PR* — two different questions, one of which it answers.)

   **This authorship rung cannot be verified from GitHub or `task reviews`.** Read that before the rungs below, because reviewer attribution alone reads green on a self-review.

   **So perform it by hand, on every `--admin` merge.** An opening report with explicit `pr_role: authored` is the recorded authorship evidence; older reports may carry only prose, which cannot acquire a missing role afterward:

   ```bash
   FLEET=ai-platform; SINCE=2026-09-20T00:00:00Z  # replace with the candidate fleet and window
   claudlobby --json --fleet "$FLEET" fleet reports list --since "$SINCE" --limit 100
   # Continue with the returned data.next_cursor, keeping FLEET and SINCE identical:
   claudlobby --json --fleet "$FLEET" fleet reports list --since "$SINCE" --limit 100 --cursor "$NEXT_CURSOR"
   ```

   Read every page until `data.next_cursor` is null, across each fleet that may have opened the PR. Inspect `data.items[].evidence.pr_url` for the exact owner/repo and bounded `pull/N` URL, and require explicit `data.items[].evidence.pr_role == "authored"` before attributing the opening report. Keep the author distinct from `task reviews`' reviewer actor. If the same bot authored and approved, **rung 1 is unsatisfied: do not merge**, however green everything else reads. An absent role or content-stripped summary cannot prove authorship. Bound the PR number or `pull/328` also matches `pull/3281`.

   **When it cannot answer, this rung REFUSES — it never passes.** Three states are not a pass:

   - **No opening row at all.** No answer is available at any confidence.
   - **An older row with no explicit authored role.** The current explicit `pr_role: authored` metadata survives that content policy; an older absent role cannot be backfilled or assumed.
   - **The author is on another fleet.** `fleet reports list` is fleet-scoped. Measured with the retired report reader: the same PR returned 5 rows under `--fleet ai-platform` and **0 under `--fleet crog-eng-team`**. Zero rows from the wrong fleet is indistinguishable from zero rows because nobody reported.

   In all three the honest output is "rung 1 unanswerable — verify by hand or do not merge", never a pass. Neither an empty page nor an unavailable source proves a different author.

   **Do NOT read `task reviews` as proof of PR authorship.** A result with no live block says nothing about the author.
2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.**

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.**

   **ABSENCE is the failure mode, so absence is what this rung tests for.**

   Why names, not counts, and why the rollup: §Rung 2.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.**

   Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

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

   **A PR that changes anything under `.github/workflows/` gets no proof from that `SUCCESS`.** So the merger reads the PR's own file list from the API, every page and each rename's old name, and a failed read refuses. A PR that lists any workflow file merges only on the rung 1 verdict (a different bot's, at this head) that names each one:

   ```bash
   WF=$(gh api --paginate "repos/$REPO/pulls/$N/files" --jq '.[] | .filename, (.previous_filename // empty)') || { echo "REFUSE: cannot read the PR's files"; exit 1; }
   WF=$(printf '%s\n' "$WF" | grep '^\.github/workflows/' || true)
   [ -z "$WF" ] || { printf 'WORKFLOW CHANGE: %s\n' $WF; echo "Merge only on the rung 1 verdict that names each workflow file above."; }
   ```

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test.

4. **KEEP THE BRANCH WHILE ANY OPEN PR IS BASED ON IT.** Delete only after a listing that succeeded and came back empty. A failed listing or an empty `$BR` (`gh pr list --base ""` is no filter) keeps the branch too:

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
gh pr merge "$N" --repo "$REPO" --squash --admin $DELETE --match-head-commit "$PH"
```

**`--match-head-commit` makes the gate refuse a head that moved under it**, which rung 0 cannot do on its own: rung 0 reconciles at the *start*, and the rungs take time.

**Reuse `$PH` — do not re-read the sha, and never abbreviate it.** The flag requires the full 40-character object id and **fails closed** on anything else:

```
$ gh pr merge <n> --squash --admin --delete-branch --match-head-commit bda6de9
GraphQL: Variable $input of type MergePullRequestInput! was provided invalid value
for expectedHeadOid (Could not coerce value "bda6de9" to GitObjectID)
```

Why the flag fails closed, why `--admin`, and what branch protection enforces on this estate: §The merge flag, §Why --admin, §The ruleset survey.

**Red lines (even with --admin):**
- Never `--admin` merge without an actual peer review on the PR.
- Never `--no-verify` — hooks exist for a reason.
- Never force-push main.
- Never merge a PR with `Request Changes` verdict outstanding.
- Never merge a PR where CI is failing — **or where a required workflow is missing from the rollup.** "Not failing" is not "passed": an absent workflow cannot fail.
- Never merge a PR the manager itself authored without a separate reviewer.
- Never merge into a repo that has adopted the rollout check while a `rollout-hold` issue is open, except a PR that closes it and whose rung 1 verdict names it (rung 5).
- Never treat an **unanswerable** rung 1 as a satisfied one. No authorship row is a refusal, not a clear — the same absence is produced by a self-review nobody reported, by a stripped summary, and by querying the wrong fleet.

The manager posts "Merging #NN (--admin, reviewed by <reviewer>)" to Telegram before executing, **naming any branch rung 4 kept** — the stacked PR's author is told nowhere else.

**This guardrail replaces both `merge-policy-human` and `no-merge-admin`.** Do not stack with either.
