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

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict (or `COMMENT` with `**Approve**` verdict line under same-identity fallback).

   **MULTIPLE VERDICTS RESOLVE PER REVIEWER, NEVER GLOBAL-LATEST.** Each reviewer's own latest verdict stands, and the PR is blocked while **any** reviewer's latest is `REQUEST-CHANGES`.

   Global-latest is the intuitive rule and it is wrong in one specific, silent way: with reviewer A blocking and reviewer B approving later, newest-on-the-PR reports `APPROVE` **over an unresolved block**. It is correct only while a PR has exactly one reviewer — which is why it survives so long on a fleet where that is usually true, and why it fails the first time two people review.

   **This codebase already settled it, so read the rule from the tool rather than from here:** `lib/pr-review-state.py` documents the prototype's global-latest, the reversed-reviewers counterexample, and its own resolution, and `test_reversing_the_reviewers_flips_the_answer` pins it. If this paragraph and that tool ever disagree, the tool is right — it is the thing with a test.

2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.** Workflow names are a property of a repository, not of a fleet: one repo's set may be `Lint` / `Test` / `Security Scan` / `Changelog Check` while another's is `api-ci` / `frontend-ci`. A list written into this guardrail would be correct for exactly one repo and silently wrong on every other — hunting for names that do not exist there, and so either blocking every PR on that repo or, worse, being quietly softened by whoever hits it first. **The softening is the real hazard: a guardrail that fires wrongly gets weakened, and the weakening outlives the repo that caused it.**

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.** Where workflows are path-filtered — say `api-ci` on `api/**` and `frontend-ci` on `frontend/**` — a docs-only PR triggers neither, so no set is always-present and a required context would strand such a PR permanently. Under a hardcoded list that repo is a special case somebody has to remember; under declaration its exclusion falls out of the design: nothing declared, nothing to verify, and the gap is visible instead of forgotten.

   **ABSENCE is the failure mode, so absence is what this rung tests for.** A PR with conflicts has no computable merge commit, so `pull_request`-triggered workflows are **never scheduled** — while GitHub App integrations keep reporting `SUCCESS` against the head SHA. The rollup then reads all-success *with the entire test suite missing*, and a naive green gate passes. Measured on a real conflicting PR: three checks, all passing, `gh pr checks` exit `0`, no tests run.

   A **count** does not survive this. It is a proxy for "the tests ran": it breaks the moment a workflow is added or removed, and it is satisfied by the wrong N.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.** `statusCheckRollup` is the union of the Checks API and the legacy commit-Status API, so by construction it carries every gate whichever surface reports it. `/check-runs` finds all four names today, and keeps working only for as long as nobody ever adds a required gate as a legacy commit status — which a plain Vercel entry on one of these repos already is. Measured on one real commit: 2 check-runs, 1 status, 3 in the rollup. That is evidence a second surface exists and *can* carry a gate; it is not a claim that any of the four hides there today.

   **Incidentally correct is a proxy** — the same distinction as counting versus naming, one layer down. A thing that happens to be right is not a thing that must be right, and only the second belongs in a guardrail. Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

   **Do not rely on branch protection to enforce this for you — it enforces something else.** Measured across six repos of one estate via the repository **rulesets** API: five carry a ruleset covering branch deletion, non-fast-forward and pull-request rules — though only four are `enforcement: active`, the fifth being `disabled` and so covering nothing in practice; the sixth is on a plan that cannot have one. **Four of the five DECLARE an approval requirement and only three ENFORCE one**, the difference being that same disabled ruleset. **Not one of the five declares `required_status_checks`.** GitHub is enforcing *review* here and enforcing nothing about whether the tests ran, which is exactly the gap this rung covers.

   Two cautions, because the obvious ways to check this both return confident wrong answers. Read the **rulesets** API, not `/branches/main/protection` — the legacy endpoint answers `404 Branch not protected` for a repo fully protected by a ruleset, asserting a negative it has no standing to assert. Then read the ruleset's **`enforcement`** field, because one can exist and enforce nothing (`enforcement: disabled`). The two failures point in **opposite** directions: the legacy endpoint calls a protected repo unprotected, a bare ruleset listing calls an unprotected one protected.

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". GitHub computes this field **lazily**: the first read after a push returns `UNKNOWN`, and only a re-query resolves it. `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test. That field reports `BLOCKED` for the ordinary case of a PR still awaiting its review, so gating on `clean`/`unstable` refuses PRs that are perfectly mergeable.

4. **KEEP THE BRANCH WHILE ANY OPEN PR IS BASED ON IT.** `--delete-branch` makes GitHub close such a PR, and tells nobody: #1779 went `CLOSED` two seconds after #1776 merged (#1781). Delete only after a listing that succeeded and came back empty. A failed listing or an empty `$BR` (`gh pr list --base ""` is no filter) keeps the branch too:

   ```bash
   DELETE=--delete-branch
   [ -n "$BR" ] && STACKED=$(gh pr list --repo "$REPO" --base "$BR" --state open --json number --jq '.[].number') && [ -z "$STACKED" ] || { DELETE=""; echo "KEEPING $BR"; }
   ```

   Run rung 0, this rung and the merge command in one call: the Bash tool keeps no variables between calls, and a `$DELETE` that was never set just keeps the branch.

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

The manager posts "Merging #NN" to Telegram before executing, so the human has visibility — **naming any branch rung 4 kept**, because the stacked PR's author is told nowhere else.
