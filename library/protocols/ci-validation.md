---
title: CI Validation Protocol
description: Pre-merge CI gate, failure routing, and retry budget for fleet-managed PRs
---

# CI Validation Protocol

Fleet-level protocol ensuring CI passes before any merge operation. Prevents broken code from landing and routes failures to the right bot automatically.

## 1. Pre-merge gate

Before executing any merge, the manager **must** check CI status:

```bash
gh pr checks <PR_NUMBER> --repo <owner>/<repo> --json name,state,conclusion
```

**Determining required checks:**

All checks returned by `gh pr checks` are treated as required by default. If the repo has branch protection rules with explicit required status checks, query them:

```bash
gh api repos/<owner>/<repo>/branches/main/protection/required_status_checks --jq '.contexts[]'
```

If the endpoint returns checks, only those are required — others are informational. If it 404s (no branch protection), treat all checks as required. This is slightly over-conservative but unambiguous.

**Gate logic:**

- All required checks `conclusion: "success"` or `conclusion: "skipped"` → proceed to merge
- `conclusion: "neutral"` → treat as pass (non-blocking informational workflow)
- Any required check `state: "pending"` or `state: "queued"` → wait and re-check in 60s (max 3 polls, then abort)
- Any required check `conclusion: "failure"` or `conclusion: "cancelled"` → **abort merge**, route to fix

**After 3 pending polls with no resolution** → abort merge, flag human in Telegram: "CI checks still pending after 3 min on PR #N — may need manual investigation (stuck workflow, queued runner, etc.)."

Never merge with failing or pending required checks. This is non-negotiable — even if the reviewer approved and the code looks correct. CI is the final gate.

**Interaction with existing guardrails:**

- `no-push-main` — CI validation adds to, not replaces, the branch-only workflow
- `merge-policy-human` — human still clicks merge; this protocol gates the manager's *recommendation* to merge
- `merge-policy-auto-*` — in auto-merge fleets where the manager executes merge, this protocol's gate logic applies equally; the manager runs the CI gate before executing `gh pr merge`
- `verify-before-merge` — CI check happens *before* verify-before-merge (no point parsing a verdict if CI is red)

## 2. CI failure routing

When CI fails on a PR, the manager auto-dispatches the **PR author** (not the reviewer) to fix:

**Dispatch includes:**

1. The PR number and branch name
2. The specific failing check name(s)
3. The failure output (extracted via `gh pr checks <N> --json` or `gh run view <run_id> --log-failed`)
4. Instruction: "Fix the CI failure on branch `<branch>`. Do NOT open a new PR — push to the existing branch."

**Example dispatch prompt:**

```
CI failed on <owner>/<repo>#<N> (branch: <branch-name>).
Failing check: "test" — exit code 1.
Failure output: [paste relevant error lines]
Fix the failure and push to the same branch. Report back when CI is green.
```

**Do not:**

- Route CI failures to the reviewer (wastes their context)
- Merge with a "CI will probably pass next time" assumption
- Re-run CI without a code change (flaky tests are a separate issue to file)

## 3. CI-aware review dispatch

When a PR is ready for review, check CI status **before** dispatching to the reviewer:

- CI green → dispatch review immediately
- CI pending → wait for completion, then dispatch review if green
- CI failing → route back to author for fix first; do NOT dispatch review

**Rationale:** Reviewer context is expensive. A reviewer who reads through code, formulates feedback, and writes a review — only to find CI was red the whole time — has wasted that entire context window. Gate review dispatch on CI to protect reviewer bandwidth.

**Flow:**

```
PR opened/updated
  → Manager checks CI
    → Green: dispatch to reviewer
    → Pending: wait (poll 60s, max 3 polls)
      → Green: dispatch to reviewer
      → Still pending after 3 polls: flag human
      → Failed: route to author
    → Failed: route to author for fix
```

## 4. Retry budget

Auto-dispatch fix attempts are capped at **2 per CI failure cycle**, with a per-PR lifetime cap of **4 total dispatch cycles**:

| Attempt | Action |
|---------|--------|
| 1st failure | Dispatch author with failure output |
| 2nd failure (after author's fix push) | Dispatch author again with new failure output |
| 3rd failure | **Stop.** Flag to human in Telegram: "CI has failed 3 times on PR #N after 2 fix attempts. Needs human investigation." |

**Reset:** The retry counter resets when:

- CI passes (success clears all state)
- A different check fails (new failure type = new counter)
- Human explicitly says "try again"

**Lifetime cap:** Regardless of resets, a single PR gets at most 4 total auto-dispatch fix cycles. After that, flag human even if CI went green-then-red again. This prevents a flaky-test loop from generating unlimited dispatch noise.

**Why cap at 2:** Bots fixing CI failures is high-leverage when the fix is obvious (missing import, type error, test assertion drift). After 2 failed attempts, the failure is likely architectural or environmental — human judgement needed.

## Manager checklist (pre-merge)

```
1. gh pr checks <N> → all green/skipped/neutral?
   - No (failed) → abort, route to author
   - Pending → poll 60s × 3, then abort + flag human
2. If CI green → run verify-before-merge checklist
3. Merge (or recommend merge to human, per active merge-policy guardrail)
```
