---
title: Auto-merge with --admin after peer review
description: Manager auto-merges PRs using --admin after a real peer review verdict + CI green. For same-identity fleets where regular merge is blocked by branch protection.
---

# Auto-merge with --admin after peer review

The manager auto-merges PRs using `--admin` when ALL of:

1. **Peer review posted** — a reviewer has posted an `APPROVE` verdict, or a `COMMENT` with `**Approve**` verdict line (same-identity fallback). The review must be from a different bot than the PR author — no self-reviews.
2. **CI green — the repo's DECLARED required checks, BY NAME, never by count.** Verify that every check **this repo declares as required** appears in the status rollup by name, and that every one is `SUCCESS`.

   **The required set is declared per repo and is deliberately not listed here.** Workflow names are a property of a repository, not of a fleet: one repo's set may be `Lint` / `Test` / `Security Scan` / `Changelog Check` while another's is `api-ci` / `frontend-ci`. A list written into this guardrail would be correct for exactly one repo and silently wrong on every other — hunting for names that do not exist there, and so either blocking every PR on that repo or, worse, being quietly softened by whoever hits it first. **The softening is the real hazard: a guardrail that fires wrongly gets weakened, and the weakening outlives the repo that caused it.**

   **A repo that cannot declare a stable set declares nothing, and that is the design working rather than a gap in it.** Where workflows are path-filtered — say `api-ci` on `api/**` and `frontend-ci` on `frontend/**` — a docs-only PR triggers neither, so no set is always-present and a required context would strand such a PR permanently. Under a hardcoded list that repo is a special case somebody has to remember; under declaration its exclusion falls out of the design: nothing declared, nothing to verify, and the gap is visible instead of forgotten.

   **ABSENCE is the failure mode, so absence is what this rung tests for.** A PR with conflicts has no computable merge commit, so `pull_request`-triggered workflows are **never scheduled** — while GitHub App integrations keep reporting `SUCCESS` against the head SHA. The rollup then reads all-success *with the entire test suite missing*, and a naive green gate passes. Measured on a real conflicting PR: three checks, all passing, `gh pr checks` exit `0`, no tests run.

   A **count** does not survive this. It is a proxy for "the tests ran": it breaks the moment a workflow is added or removed, and it is satisfied by the wrong N.

   **Read the rollup, not `/check-runs` — because the rollup is GUARANTEED complete and `/check-runs` is only INCIDENTALLY complete.** `statusCheckRollup` is the union of the Checks API and the legacy commit-Status API, so by construction it carries every gate whichever surface reports it. `/check-runs` finds all four names today, and keeps working only for as long as nobody ever adds a required gate as a legacy commit status — which a plain Vercel entry on one of these repos already is. Measured on one real commit: 2 check-runs, 1 status, 3 in the rollup. That is evidence a second surface exists and *can* carry a gate; it is not a claim that any of the four hides there today.

   **Incidentally correct is a proxy** — the same distinction as counting versus naming, one layer down. A thing that happens to be right is not a thing that must be right, and only the second belongs in a guardrail. Use `gh pr view <n> --json statusCheckRollup` or `gh pr checks <n>`.

3. **Mergeable reads `MERGEABLE` explicitly** — never "not `CONFLICTING`". GitHub computes this field **lazily**: the first read after a push returns `UNKNOWN`, and only a re-query resolves it. `UNKNOWN` is not `false`, so a not-conflicting test passes on a field that has not been computed yet. Re-query until the value is `MERGEABLE` or `CONFLICTING`, and treat a persistent `UNKNOWN` as not mergeable.

   This rung is deliberately **not** a `mergeStateStatus` test. That field reports `BLOCKED` for the ordinary case of a PR still awaiting its review, so gating on `clean`/`unstable` refuses PRs that are perfectly mergeable.

Merge command: `gh pr merge <n> --squash --admin --delete-branch`

**Why --admin:** Same-identity fleets share one GitHub PAT. Branch protection's "required approvals" check counts only formal `APPROVE` state, which GitHub blocks for same-identity. `--admin` bypasses the branch protection gate — but the **real gate is the peer review verdict**, not GitHub's checkbox.

**Why the rungs above carry the whole weight.** There *is* a server-side backstop — it simply does not cover this, and it covers far less than a first look suggests. Measured across **nine** repos of one estate via the repository **rulesets** API (re-measured 2026-08-22):

| state | count | repos |
|---|---|---|
| ruleset `enforcement: active` | **4** | branch deletion, non-fast-forward, pull-request rules |
| ruleset present but `enforcement: disabled` | **2** | covers nothing in practice |
| **no ruleset possible at all** — private repo on a plan returning `403 Upgrade to GitHub Pro or make the repository public` | **3** | nothing to configure, nothing to bypass |

**So five of the nine have no protection currently enforced** — and those five are not one bucket. The two `disabled` repos are one flag flip from live; the three plan-403 repos **cannot be protected at any configuration** without a paid upgrade. Same headline, opposite remediation cost: reserve "unenforceable" for the plan-403 three, where it is literally true.

Of the six that carry a ruleset, **four DECLARE an approval requirement and only three ENFORCE one** — declared and enforced are separate counts, which is what *A present ruleset proves nothing until you read its `enforcement` field* (below) is about. Read the rule, not its presence: one active repo carries a `pull_request` rule with **`required_approving_review_count: 0`**, which declares the *absence* of an approval requirement rather than one; and one of the disabled pair carries no `pull_request` rule at all.

**Not one of the nine declares `required_status_checks`. Zero of nine** — verified rule-by-rule on all six rulesets, not inferred.

**Do not read the plan-403 state as an edge case.** It is a *third* of this estate, and a repo in it cannot be protected at any configuration — so on those repos an auto-merge clause does not "fail closed against a server-side control"; it simply merges. A prior revision of this paragraph surveyed six repos and reported the disabled and plan-403 states as one repo each, which invited exactly that misreading.

So GitHub enforces *review* and *force-push* on this estate, and enforces **nothing** about whether the tests ran. That is precisely and only the gap these rungs address — they are not a belt beside braces, they are the sole control over exactly the failure mode described above them.

**Check the right surface, or you will confidently conclude the opposite.** That measurement is from the rulesets API, **not** `/branches/main/protection`. The legacy endpoint returns `404 Branch not protected` for a repo that is fully protected by a ruleset — asserting a negative in plain English that it has no standing to assert. This is the same guaranteed-versus-incidental distinction as the rollup above, one layer out: a surface that answers is not the same as a surface that answers *completely*.

**A present ruleset proves nothing until you read its `enforcement` field.** A ruleset can exist and enforce nothing (`enforcement: disabled`), and **two** repos in that survey are in exactly that state. The two obvious checks then fail in **opposite** directions: the legacy endpoint reports "not protected" about a repo that is, while a ruleset *listing* reports "protected" about a repo that is not. Two confident wrong answers pointing opposite ways — only the `enforcement` field settles it, and neither reviewer who checked this had that third state in hand.

**Red lines (even with --admin):**
- Never `--admin` merge without an actual peer review on the PR.
- Never `--no-verify` — hooks exist for a reason.
- Never force-push main.
- Never merge a PR with `Request Changes` verdict outstanding.
- Never merge a PR where CI is failing — **or where a required workflow is missing from the rollup.** "Not failing" is not "passed": an absent workflow cannot fail.
- Never merge a PR the manager itself authored without a separate reviewer.

The manager posts "Merging #NN (--admin, reviewed by <reviewer>)" to Telegram before executing.

**This guardrail replaces both `merge-policy-human` and `no-merge-admin`.** Do not stack with either.
