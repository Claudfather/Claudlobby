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

**Gate logic:**

- All required checks `conclusion: "success"` �� proceed to merge
- Any required check `state: "pending"` or `state: "queued"` → wait and re-check in 60s (max 3 polls)
- Any required check `conclusion: "failure"` or `conclusion: "cancelled"` → **abort merge**, route to fix

Never merge with failing or pending required checks. This is non-negotiable — even if the reviewer approved and the code looks correct. CI is the final gate.

**Interaction with existing guardrails:**

- `no-push-main` — CI validation adds to, not replaces, the branch-only workflow
- `merge-policy-human` — human still clicks merge; this protocol gates the manager's *recommendation* to merge
- `verify-before-merge` — CI check happens *before* the verdict check (no point parsing a verdict if CI is red)

## 2. CI failure routing

When CI fails on a PR, the manager auto-dispatches the **PR author** (not the reviewer) to fix:

**Dispatch includes:**

1. The PR number and branch name
2. The specific failing check name(s)
3. The failure output (extracted via `gh pr checks <N> --json` or `gh run view <run_id> --log-failed`)
4. Instruction: "Fix the CI failure on branch `<branch>`. Do NOT open a new PR — push to the existing branch."

**Example dispatch prompt:**

```
CI failed on chrisrogers37/storydump#353 (branch: fix/railway-health-check).
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
    → Pending: wait (poll 60s, max 5 min)
      → Green: dispatch to reviewer
      → Failed: route to author
    → Failed: route to author for fix
```

## 4. Retry budget

Auto-dispatch fix attempts are capped at **2 per CI failure cycle**:

| Attempt | Action |
|---------|--------|
| 1st failure | Dispatch author with failure output |
| 2nd failure (after author's fix push) | Dispatch author again with new failure output |
| 3rd failure | **Stop.** Flag to human in Telegram: "CI has failed 3 times on PR #N after 2 fix attempts. Needs human investigation." |

**Reset:** The retry counter resets when:

- CI passes (success clears all state)
- A different check fails (new failure type = new counter)
- Human explicitly says "try again"

**Why cap at 2:** Bots fixing CI failures is high-leverage when the fix is obvious (missing import, type error, test assertion drift). After 2 failed attempts, the failure is likely architectural or environmental — human judgement needed.

## Manager checklist (pre-merge)

```
1. gh pr checks <N> → all green?
   - No → abort, route to author
   - Pending → poll (60s × 3)
2. Review verdict = "Ship it"?
   - No → bounce to author with reviewer feedback
3. Migration files present?
   - Yes → verify deployment plan
4. Merge (or recommend merge to human per merge-policy)
```
