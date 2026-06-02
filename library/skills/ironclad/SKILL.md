---
name: ironclad
description: "Use when a plan PR needs fleet-orchestrated hardening before implementation. Dispatches review lenses to idle workers, collects findings, posts aggregated results to the PR, and checks convergence. Invoke after /forge produces a plan PR, before /implement-plan executes it."
argument-hint: "<pr-url> [--auto]"
---

# Ironclad

Fleet-orchestrated plan hardening. Takes a plan PR, dispatches review lenses to idle workers via `[BOTCOMMAND]`, collects structured findings, posts an aggregated review to the PR, and checks convergence. The plan is hardened when all forks are locked and no unresolved high-severity findings remain.

**Handoff chain:** `/forge` (produces plan PR) -> `/ironclad` (hardens it) -> `/implement-plan` (executes it).

**Pull model:** each invocation runs one hardening cycle. The human re-invokes for cycle 2+ until converged.

## Arguments

Parse `$ARGUMENTS`:

- **First positional arg:** GitHub PR URL (e.g. `https://github.com/org/repo/pull/42`). Required.
- `--auto`: Machine-consumable mode. Suppresses interactive output. Emits structured result JSON on completion (see Output section).

---

## Procedure

### Phase 1: Read the Plan

1. Extract `owner/repo` and PR number from the URL.
2. Fetch the PR diff via `gh pr diff <number> -R <owner/repo>`.
3. Identify the plan file(s) from the diff (typically markdown in `docs/`, `planning/`, or `shared/planning/`).
4. Read the full plan content. If the plan references other files, read those too.
5. Record the plan title and a one-line summary for downstream dispatch.

If the PR cannot be fetched (auth, 404, network), report the error verbatim and stop. Do not fabricate plan content.

### Phase 2: Prepare Scratch Directory

Create the run directory:

```
state/ironclad-runs/<pr-number>-<timestamp>/
  plan.md              # Copy of the plan content
  lenses/              # One subdir per dispatched lens
    adversarial-review/
      result.md        # Worker writes findings here
    first-principles/
      result.md
    ...
```

Write `plan.md` with frontmatter:

```markdown
---
pr_url: <url>
pr_number: <number>
repo: <owner/repo>
plan_title: <title>
started: <ISO timestamp>
cycle: <1-based cycle number, default 1>
status: in-progress
---

<full plan content>
```

Workers read `plan.md` from this scratch directory so they have local access without needing to fetch the PR themselves.

### Phase 3: Identify Idle Workers

1. Read `$FLEET_STATE_PATH` (defaults to `$CLAUDLOBBY_ROOT/state/fleet-state.json`).
2. Filter bots where `status == "idle"` and `current_task == null`.
3. Exclude yourself from the candidate pool.
4. If zero idle workers: post findings summary with note "no idle workers available for dispatch" and stop. The human can re-invoke when workers free up.

### Phase 4: Dispatch Lenses

#### Available Lenses

Design is lens-agnostic. Each lens is a skill name that accepts `--dispatch` (or equivalent) for autonomous execution. Lenses at launch:

| Lens | Skill | Status |
|------|-------|--------|
| Adversarial Review | `/adversarial-review` | Active |
| First Principles | `/first-principles` | Planned (document the slot; skip dispatch if skill does not exist) |

New lenses plug in by adding a row to this table and a corresponding subdir in the scratch directory. No code changes required.

#### Dispatch Sequence

For each lens, pick an idle worker (round-robin, no worker gets two lenses before all have one):

1. Create the lens subdir: `state/ironclad-runs/<run>/lenses/<lens-name>/`.
2. Write a `dispatch.md` in the lens subdir with the dispatch instructions for audit trail.
3. Dispatch via two-step tmux send-keys:

```bash
SCRATCH="$CLAUDLOBBY_ROOT/state/ironclad-runs/<run>"
PLAN_PATH="$SCRATCH/plan.md"
RESULT_PATH="$SCRATCH/lenses/<lens-name>/result.md"

tmux send-keys -t <worker> "set +H; [BOTCOMMAND] $(BOT_ID) | task | Run /<lens-skill> --dispatch on the plan at $PLAN_PATH. Write your structured findings to $RESULT_PATH using the result format specified below. When done, report back via report-back.sh completed \"<lens-name> lens complete\" --skill ironclad-lens | priority:high"
sleep 0.3
tmux send-keys -t <worker> Enter
```

4. Update `fleet-state.json` for dispatched worker: status `working`, current_task `ironclad:<lens-name>`.

#### Result Format (Workers Write This)

Workers must write their `result.md` with this structure:

```markdown
---
lens: <lens-name>
worker: <bot-id>
pr_url: <url>
started: <ISO timestamp>
completed: <ISO timestamp>
status: completed | failed
---

## Findings

### Blockers
- <numbered findings with evidence>

### Risks
- <numbered findings with severity + mitigation>

### Gaps
- <numbered findings>

### Questions
- <numbered ambiguities>

### Observations
- <bullet notes>
```

### Phase 5: Collect Results

Monitor for `[BOTREPORT]` messages from dispatched workers. For each:

1. On `completed` with `skill:ironclad-lens`: read the corresponding `result.md` from the scratch dir.
2. On `failed`: mark the lens as failed in scratch state. Queue for retry (Phase 6).
3. On `blocked`: treat as failed. Queue for retry.

**Timeout:** if no report arrives within `$OBSERVABILITY_DISPATCH_DEADLINE` (default 1800s), treat the lens as failed.

Wait until all dispatched lenses have reported or timed out before proceeding.

### Phase 6: Retry Failed Lenses

For each failed lens:

1. Pick a different idle worker (never retry on the same worker that failed).
2. Re-dispatch using the same scratch subdir (overwrite `result.md`).
3. Collect results as in Phase 5.

**One retry per lens.** If the retry also fails, record the failure and proceed to aggregation with available results. Best-effort: partial findings are better than no findings.

### Phase 7: Aggregate and Deduplicate

1. Read all `result.md` files from `state/ironclad-runs/<run>/lenses/*/`.
2. Merge findings across lenses:
   - **Deduplicate:** if two lenses flag the same issue (same file/line, same concern), keep the higher-severity version and note both lenses identified it.
   - **Preserve lens attribution:** each finding tagged with which lens surfaced it.
3. Sort by severity: Blockers first, then Risks, Gaps, Questions, Observations.
4. Produce the aggregated review body.

### Phase 8: Post to PR

`/ironclad` owns all PR interaction. Workers never post to the PR directly.

Post a single review comment to the PR via `gh pr comment` or GitHub MCP:

```markdown
## Ironclad Review: [Plan Title]

**Cycle:** <N>
**Lenses completed:** <list>
**Lenses failed:** <list, if any>

### Blockers
<merged blockers with lens attribution>

### Risks
<merged risks with severity>

### Gaps
<merged gaps>

### Questions
<merged questions>

### Observations
<merged observations>

---
*Hardened by /ironclad — cycle <N>, <timestamp>*
```

If a previous ironclad comment exists on the PR (from a prior cycle), post a new comment rather than editing. Each cycle's findings are preserved as a record.

### Phase 9: Convergence Check

A plan is **converged** (hardened) when:

1. All dispatched lenses completed successfully (no unresolved failures).
2. Zero open Blockers remain.
3. All forks from the plan's design phase are locked (ratified).

Evaluate:

- **Converged:** post `[IRONCLAD] Plan hardened. PR ready for /implement-plan.` as a final PR comment. Update scratch `plan.md` frontmatter: `status: hardened`.
- **Not converged:** post a summary of open items to the PR:
  ```
  ### Open Items
  - <N> unresolved Blockers
  - <N> open forks
  - <N> failed lenses (no results)

  Re-invoke /ironclad for cycle <N+1> after addressing these.
  ```

---

## Output

### Interactive Mode (default)

Present the aggregated review in chat and confirm the PR comment was posted. Include the PR URL for easy access.

### `--auto` Mode

Emit structured JSON to stdout:

```json
{
  "skill": "ironclad",
  "outcome": "converged | not-converged | failed",
  "artifacts": {
    "pr_url": "<url>",
    "review_cycle": <N>,
    "findings_posted": true,
    "lenses_completed": ["adversarial-review"],
    "lenses_failed": [],
    "forks_open": <count>,
    "forks_locked": <count>,
    "converged": true
  },
  "summary": "<one-line summary>",
  "next": "<recommended next action>",
  "errors": [],
  "blocker_description": null
}
```

`outcome` values:
- `converged` — plan hardened, ready for `/implement-plan`.
- `not-converged` — findings posted but open items remain. Human re-invokes.
- `failed` — could not complete (no workers, PR fetch failed, etc.).

---

## Scratch Directory Lifecycle

- **Created:** Phase 2, on each invocation.
- **Populated:** Phases 4-6, by dispatched workers writing `result.md`.
- **Read:** Phases 5, 7, by `/ironclad` for aggregation.
- **Retained:** scratch dirs persist for audit trail. The human or a cron job cleans up old runs.

Path pattern: `$CLAUDLOBBY_ROOT/state/ironclad-runs/<pr-number>-<YYYYMMDD-HHMMSS>/`

---

## Constraints

- **Read-only on the plan.** `/ironclad` never modifies the plan PR's files. It only posts review comments.
- **Centralized PR posting.** Workers write to the scratch directory. Only `/ironclad` posts to the PR.
- **No self-dispatch.** The bot running `/ironclad` does not dispatch a lens to itself.
- **No merge.** `/ironclad` never merges the PR. The human merges.
- **Lens-agnostic dispatch.** The dispatch mechanism is identical for all lenses. New lenses are added by name — no orchestration code changes.
- **Idempotent cycles.** Each invocation is a fresh cycle with its own scratch dir. Re-invocation is safe.

---

## Notes

- `/ironclad` is the quality gate between planning and implementation. A plan that survives ironclad review has been stress-tested by multiple lenses across multiple workers.
- The pull model (human re-invokes) is deliberate: it keeps the human in the loop for deciding when findings have been addressed and another cycle is warranted.
- The `/first-principles` lens is documented as a slot for when the skill is built. Until then, `/ironclad` dispatches only available lenses and notes skipped ones in the PR comment.
