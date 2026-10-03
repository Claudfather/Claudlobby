---
title: Auto-merge after peer review
description: Manager auto-merges PRs after a peer review verdict + CI green. Does not use --admin — requires GitHub branch protection to allow it.
---

# Auto-merge after peer review

The manager auto-merges PRs when ALL of:

0. **RECONCILE THE HEAD BEFORE ANCHORING ANYTHING TO IT — and a mismatch is never a pass.**

   ```bash
   REPO=<owner/repo>; N=<n>
   BR=$(gh pr view "$N" --repo "$REPO" --json headRefName --jq .headRefName)
   PH=$(gh api "repos/$REPO/pulls/$N" --jq .head.sha)          # full 40-char oid
   RH=$(gh api "repos/$REPO/git/ref/heads/$BR" --jq .object.sha)
   [ "$PH" = "$RH" ] || { echo "REFUSE: PR head $PH != branch ref $RH"; exit 1; }
   ```

   **Measured on this estate:** a PR head persisted at an **old** sha across **both** the GraphQL and REST surfaces, two reads each, while the branch ref sat one commit ahead. CI was green **on the old code** and the new commit had no checks at all — so every rung below answered truthfully about a commit that was no longer the branch tip, and would have passed.

   **It does NOT self-resolve on re-query the way `mergeable` does.** Rung 3 tells you to re-query a lazy `UNKNOWN`; that instinct is wrong here and applying it wastes the window. Refuse, confirm the push landed (`git ls-remote <remote> refs/heads/$BR` against your local `HEAD`), and start the rungs again.

   This rung is a **mechanism, not a reminder**: telling an operator to anchor their evidence to a head cannot help when the surface they are told to read reports the head wrongly.

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict (or `COMMENT` with a `**[<bot>] [VERDICT] approve**` verdict line anchored to the reviewed SHA under same-identity fallback). Read `claudlobby --json task reviews OWNER/REPO --pr N` for current-head verdicts and recorded reviewer attribution; this read does not authorize merging.

   **MULTIPLE VERDICTS RESOLVE PER REVIEWER, NEVER GLOBAL-LATEST.** Each reviewer's own latest verdict stands, and the PR is blocked while **any** reviewer's latest is `REQUEST-CHANGES`.

   Global-latest is the intuitive rule and it is wrong in one specific, silent way: with reviewer A blocking and reviewer B approving later, newest-on-the-PR reports `APPROVE` **over an unresolved block**. It is correct only while a PR has exactly one reviewer — which is why it survives so long on a fleet where that is usually true, and why it fails the first time two people review.

   **Read the recorded state from `claudlobby --json task reviews OWNER/REPO --pr N`.** It resolves each reviewer's latest verdict, not the globally latest verdict, and reports unknown attribution or an unanchored verdict explicitly. The read does not authorize a merge.

2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.** Workflow names are a property of a repository, not of a fleet: one repo's set may be `Lint` / `Test` / `Security Scan` / `Changelog Check` while another's is `api-ci` / `frontend-ci`. A list written into this guardrail would be correct for exactly one repo and silently wrong on every other — hunting for names that do not exist there, and so either blocking every PR on that repo or, worse, being quietly softened by whoever hits it first. **The softening is the real hazard: a guardrail that fires wrongly gets weakened, and the weakening outlives the repo that caused it.**

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.** Where workflows are path-filtered — say `api-ci` on `api/**` and `frontend-ci` on `frontend/**` — a docs-only PR triggers neither, so no set is always-present and a required context would strand such a PR permanently. Under a hardcoded list that repo is a special case somebody has to remember; under declaration its exclusion falls out of the design: nothing declared, nothing to verify, and the gap is visible instead of forgotten.

   **ABSENCE is the failure mode, so absence is what this rung tests for.** A PR with conflicts has no computable merge commit, so `pull_request`-triggered workflows are **never scheduled** — while GitHub App integrations keep reporting `SUCCESS` against the head SHA. The rollup then reads all-success *with the entire test suite missing*, and a naive green gate passes. Measured on a real conflicting PR: three checks, all passing, `gh pr checks` exit `0`, no tests run.

   A **count** does not survive this. It is a proxy for "the tests ran": it breaks the moment a workflow is added or removed, and it is satisfied by the wrong N.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.** `statusCheckRollup` is the union of the Checks API and the legacy commit-Status API, so by construction it carries every gate whichever surface reports it. `/check-runs` finds all four names today, and keeps working only for as long as nobody ever adds a required gate as a legacy commit status — which a plain Vercel entry on one of these repos already is. Measured on one real commit: 2 check-runs, 1 status, 3 in the rollup. That is evidence a second surface exists and *can* carry a gate; it is not a claim that any of the four hides there today.

   **Incidentally correct is a proxy** — the same distinction as counting versus naming, one layer down. A thing that happens to be right is not a thing that must be right, and only the second belongs in a guardrail. Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

   **Do not rely on branch protection to enforce this for you — it enforces something else.** Measured across six repos of one estate via the repository **rulesets** API: five carry a ruleset covering branch deletion, non-fast-forward and pull-request rules — though only four are `enforcement: active`, the fifth being `disabled` and so covering nothing in practice; the sixth is on a plan that cannot have one. **Four of the five DECLARE an approval requirement and only three ENFORCE one**, the difference being that same disabled ruleset. **Not one of the five declares `required_status_checks`.** GitHub is enforcing *review* here and enforcing nothing about whether the tests ran, which is exactly the gap this rung covers.

   Two cautions, because the obvious ways to check this both return confident wrong answers. Read the **rulesets** API, not `/branches/main/protection` — the legacy endpoint answers `404 Branch not protected` for a repo fully protected by a ruleset, asserting a negative it has no standing to assert. Then read the ruleset's **`enforcement`** field, because one can exist and enforce nothing (`enforcement: disabled`). The two failures point in **opposite** directions: the legacy endpoint calls a protected repo unprotected, a bare ruleset listing calls an unprotected one protected.

   **One name is fixed in every repo that has adopted it: the rollout check** (`verify-rollout`). Read whether the repo's default branch carries `.github/workflows/rollout-check.yml`: only a 404 means the repo has not adopted the check, and any other failed read refuses. Where it has, the newest `rollout-check / Rollout check` run at the head must be `SUCCESS`. The rollup lists every run of a name, so a read that takes any `SUCCESS` would pass a stale green after a red body edit. Anything but `SUCCESS` in the newest run, or no run, refuses. So does any run that has not completed, newest or not: a queued run's `startedAt` can be null or a zero time, which sorts it before the runs that started, so the read names it `PENDING` instead of trusting the order. Run it in the same call as rung 0, which sets `$REPO` and `$N`:

   ```bash
   DEFAULT=$(gh api "repos/$REPO" --jq .default_branch) || { echo "REFUSE: cannot read the default branch"; exit 1; }
   if ERR=$(gh api "repos/$REPO/contents/.github/workflows/rollout-check.yml?ref=$DEFAULT" --silent 2>&1); then
     ROLLOUT=$(gh pr view "$N" --repo "$REPO" --json statusCheckRollup --jq '[.statusCheckRollup[] | select(.name == "rollout-check / Rollout check")] | if any(.status != "COMPLETED") then "PENDING" else (sort_by(.startedAt) | last | .conclusion // "ABSENT") end') || { echo "REFUSE: cannot read the rollout check"; exit 1; }
     [ "$ROLLOUT" = SUCCESS ] || { echo "REFUSE: the rollout check reads $ROLLOUT"; exit 1; }
   else
     case "$ERR" in *"HTTP 404"*) echo "This repo has not adopted the rollout check" ;; *) echo "REFUSE: cannot tell whether this repo runs the rollout check: $ERR"; exit 1 ;; esac
   fi
   ```

   **A PR that changes anything under `.github/workflows/` gets no proof from that `SUCCESS`.** It can rename or replace any job, this one included, and the check's own warning about it is printed by code such a PR controls (in another repo it can re-point `uses:`). So the merger reads the PR's own file list from the API, every page and each rename's old name, and a failed read refuses. A PR that lists any workflow file merges only on the rung 1 verdict (a different bot's, at this head) that names each one:

   ```bash
   WF=$(gh api --paginate "repos/$REPO/pulls/$N/files" --jq '.[] | .filename, (.previous_filename // empty)') || { echo "REFUSE: cannot read the PR's files"; exit 1; }
   WF=$(printf '%s\n' "$WF" | grep '^\.github/workflows/' || true)
   [ -z "$WF" ] || { printf 'WORKFLOW CHANGE: %s\n' $WF; echo "Merge only on the rung 1 verdict that names each workflow file above."; }
   ```

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". GitHub computes this field **lazily**: the first read after a push returns `UNKNOWN`, and only a re-query resolves it. `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test. That field reports `BLOCKED` for the ordinary case of a PR still awaiting its review, so gating on `clean`/`unstable` refuses PRs that are perfectly mergeable.

4. **KEEP THE BRANCH WHILE ANY OPEN PR IS BASED ON IT.** `--delete-branch` makes GitHub close such a PR, and tells nobody: #1779 went `CLOSED` two seconds after #1776 merged (#1781). Delete only after a listing that succeeded and came back empty. A failed listing or an empty `$BR` (`gh pr list --base ""` is no filter) keeps the branch too:

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

**This matters more here than under `--admin`, not less.** This policy relies on branch protection to allow the merge, and protection does not check that the head you verified is the head you are merging — measured on this estate, not one ruleset of nine declares `required_status_checks` at all. So the flag is the only thing refusing a head that moved between rung 0 and the merge.

**Reuse `$PH` — never re-read or abbreviate it.** The flag needs the full 40-character oid and **fails closed** on anything else (`Could not coerce value "bda6de9" to GitObjectID`; the merge does not proceed, verified on a real merge). An abbreviation is *rejected*, never silently ignored — which is the right direction, since a flag that quietly no-opped would leave the gate reporting itself protected while protecting nothing. `$PH` from rung 0 is already the full id, so **one variable for both the anchor and the flag makes that mistake unavailable by construction.**

**Not auto-merged:**
- PRs with `Request Changes` verdict — bounce to engineer first.
- PRs with unresolved review threads.
- PRs where CI is failing or pending — **or where a required workflow is missing from the rollup.** "Not failing" is not "passed": an absent workflow cannot fail.
- PRs the manager authored (self-merge requires a second reviewer).
- PRs into a repo that has adopted the rollout check while a `rollout-hold` issue is open, except one that closes it and whose rung 1 verdict names it (rung 5).

The manager posts "Merging #NN" to Telegram before executing, so the human has visibility — **naming any branch rung 4 kept**, because the stacked PR's author is told nowhere else.
