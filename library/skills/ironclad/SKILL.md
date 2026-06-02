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

#### Cycle Detection

Before creating a new scratch dir, scan `$CLAUDLOBBY_ROOT/state/ironclad-runs/` for existing dirs matching `<pr-number>-*`. Sort by timestamp descending. The current cycle number is `count of matching dirs + 1`. This makes cycle numbering automatic across re-invocations without any external state.

#### Directory Layout

```
state/ironclad-runs/<pr-number>-<YYYYMMDD-HHMMSS>/
  plan.md              # Copy of the plan content
  lenses/              # One subdir per dispatched lens
    adversarial-review/
      result.md        # Worker writes findings here (markdown with frontmatter)
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
cycle: <N>
status: in-progress
---

<full plan content>
```

Workers read `plan.md` from this scratch directory so they have local access without needing to fetch the PR themselves.

### Phase 3: Identify Idle Workers

1. Read `$FLEET_STATE_PATH` (defaults to `$CLAUDLOBBY_ROOT/state/fleet-state.json`).
2. Filter bots where `status == "idle"` and `current_task == null`.
3. Exclude yourself from the candidate pool.
4. If zero idle workers: check prior scratch dirs for this PR (`state/ironclad-runs/<pr-number>-*/lenses/`) and note which lenses have existing results from earlier partial cycles. Post: "No idle workers available for dispatch. Prior cycle results exist for: <lens list>." Stop. The human can re-invoke when workers free up.

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
2. Write the dispatch payload to `dispatch.md` in the lens subdir (large payloads go via file to avoid tmux escaping issues; also serves as audit trail). The payload must include:
   - The plan path and result path
   - The result format spec **verbatim** (see Result Format below)
   - Explicit instruction: **"Do NOT post to the GitHub PR. Do NOT create GitHub issues. Write findings ONLY to the result path."**
4. Dispatch via two-step tmux send-keys:

```bash
SCRATCH="$CLAUDLOBBY_ROOT/state/ironclad-runs/<run>"
DISPATCH_FILE="$SCRATCH/lenses/<lens-name>/dispatch.md"

tmux send-keys -t <worker> "set +H; cat $DISPATCH_FILE | claude"
sleep 0.3
tmux send-keys -t <worker> Enter
```

The `dispatch.md` content:

```
[BOTCOMMAND] <bot-id> | task | Run /<lens-skill> --dispatch on the plan at <PLAN_PATH>.
Write your structured findings to <RESULT_PATH> using EXACTLY this format:

<verbatim result format from Result Format section below>

IMPORTANT: Do NOT post comments to the GitHub PR. Do NOT create GitHub issues.
Write findings ONLY to the result path above. /ironclad owns all PR interaction.

When done: report-back.sh <your-bot-id> completed "<lens-name> lens complete" "skill:ironclad-lens"
| priority:high
```

5. Update `fleet-state.json` for dispatched worker: status `working`, current_task `ironclad:<lens-name>`.

#### Result Format (Workers Write This)

Workers write markdown with YAML frontmatter directly to `result.md`. No JSON. No translation layer. `/ironclad` reads this markdown as-is for aggregation.

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

Sections with zero findings must be **omitted entirely** — do not write empty headers. If every section is empty, write a single line under `## Findings`: "No findings surfaced by this lens."

### Phase 5: Collect Results

Monitor for `[BOTREPORT]` messages from dispatched workers. For each:

1. On `completed` with `skill:ironclad-lens`: read the corresponding `result.md` from the scratch dir.
2. On `failed`: mark the lens as failed in scratch state. Queue for retry (Phase 6).
3. On `blocked`: treat as failed. Queue for retry.

**Timeout:** source `$OBSERVABILITY_DISPATCH_DEADLINE` from `bot.conf` (composed by the compositor from fleet.yaml). If no report arrives within that deadline (default 1800s), treat the lens as failed.

Wait until all dispatched lenses have reported or timed out before proceeding.

### Phase 6: Retry Failed Lenses

For each failed lens:

1. Pick a different idle worker (never retry on the same worker that failed).
2. Re-dispatch using the same scratch subdir (overwrite `result.md`).
3. Collect results as in Phase 5.

**One retry per lens.** If the retry also fails, record the failure and proceed to aggregation with available results. Best-effort: partial findings are better than no findings.

### Phase 7: Aggregate and Deduplicate

1. Read all `result.md` files from `state/ironclad-runs/<run>/lenses/*/`. Parse YAML frontmatter and markdown body directly — no JSON translation.
2. Merge findings across lenses:
   - **Deduplicate:** if two lenses flag the same issue (same file/line, same concern), keep the higher-severity version and note both lenses identified it.
   - **Preserve lens attribution:** each finding tagged with which lens surfaced it.
   - **Empty sections:** omit any finding category that has zero entries across all lenses. If all lenses returned zero findings, post a summary note: "All lenses completed with no findings. Plan appears robust."
3. Sort by severity: Blockers first, then Risks, Gaps, Questions, Observations.
4. Produce the aggregated review body.

### Phase 8: Post to PR

`/ironclad` owns all PR interaction. Workers never post to the PR directly. `/ironclad` uses its own comment format — it does not follow any external PR comment style guide.

#### Minimize Prior Comments

On cycle 2+, before posting the new comment, collapse prior `/ironclad` comments as outdated:

1. List PR comments via `gh api repos/<owner>/<repo>/issues/<number>/comments`.
2. Identify comments containing `*Hardened by /ironclad —` in the body.
3. For each, minimize via GitHub GraphQL:

```bash
gh api graphql -f query='mutation { minimizeComment(input: {subjectId: "<comment-node-id>", classifier: OUTDATED}) { minimizedComment { isMinimized } } }'
```

This preserves the audit trail (comments are still expandable) while keeping the PR thread readable.

#### Comment Format

Post a single comment via `gh pr comment` or GitHub MCP:

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

Omit any finding section that has zero entries. If all sections are empty, post: "All lenses completed with no findings. Plan appears robust."

### Phase 9: Convergence Check

Convergence is determined entirely from the scratch directory and plan state — never by scanning PR comments. PR comments are the human-readable trail; the scratch dir is the source of truth.

A plan is **converged** (hardened) when:

1. Zero open Blockers remain (from aggregated `result.md` files in the scratch dir).
2. All forks from the plan's design phase are locked (ratified). To determine fork state, follow the `decision-fork-lifecycle` protocol — forks are locked when the plan document marks them as ratified.

**Partial lens failure does not block convergence.** A lens that fails after retry is noted in the convergence report but does not prevent convergence if the remaining criteria are met. The rationale: best-effort coverage with available results is more useful than blocking on a lens that may be broken or unavailable.

Evaluate:

- **Converged:** Update scratch `plan.md` frontmatter: `status: hardened`. Post `[IRONCLAD] Plan hardened. PR ready for /implement-plan.` as a final PR comment. Then report back:

  ```bash
  report-back.sh <bot-id> completed "Ironclad cycle <N> converged — plan hardened" "pr:<pr-url>" "skill:ironclad"
  ```

- **Not converged:** Post a summary of open items to the PR:

  ```
  ### Open Items
  - <N> unresolved Blockers
  - <N> open forks
  - <N> failed lenses (noted, not blocking)

  Re-invoke /ironclad for cycle <N+1> after addressing these.
  ```

  Then report back:

  ```bash
  report-back.sh <bot-id> completed "Ironclad cycle <N> — not converged, <N> open items" "pr:<pr-url>" "skill:ironclad"
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
