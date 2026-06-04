---
name: ironclad
description: "Use when a PR needs structured multi-lens review before merge. Applies to plan PRs (post-/forge), implementation PRs, and mixed PRs. Re-invoke for additional cycles until converged."
argument-hint: "<pr-url> [--auto]"
---

# Ironclad

Fleet-orchestrated multi-lens review for any PR. For plan PRs, sits in the `/forge` -> `/ironclad` -> `/implement-plan` chain. For implementation PRs, standalone quality gate before merge. Pull model: each invocation is one cycle. Human re-invokes until converged.

## Requirements

- Fleet dispatch capability: tmux sessions, fleet-state.json, report-back.sh
- GitHub CLI: `gh auth status` must pass
- Standalone mode (without fleet): planned via PR #140 dispatch preamble pattern

## Arguments

Parse `$ARGUMENTS`:

- **First positional arg:** GitHub PR URL (required).
- `--auto`: Machine-consumable mode. Emits structured JSON per `auto-output.md`.

---

## Procedure

### Phase 1: Read the PR and Classify

1. **Pre-flight:** run `gh auth status`. If not authenticated, stop with a clear error.
2. Extract `owner/repo` and PR number from the URL.
3. Fetch diff via `gh pr diff` and metadata via `gh pr view --json title,body`.
4. **Classify:**
   - **Plan PR:** diff modifies markdown in `docs/`, `planning/`, `shared/planning/`, or `documentation/` with plan structure (phases, forks, decision points). Read full plan content and any files it references.
   - **Implementation PR:** diff modifies source code, config, scripts, tests.
   - **Mixed:** both. All lenses apply. Convergence follows plan rules.
5. Record PR title, type, and one-line summary.

If the PR cannot be fetched, report the error verbatim and stop.

### Phase 2: Prepare Scratch Directory

**Cycle detection:** scan `$CLAUDLOBBY_ROOT/state/ironclad-runs/` for existing `<pr-number>-*` dirs. Cycle = count + 1.

Create `state/ironclad-runs/<pr-number>-<YYYYMMDD-HHMMSS>/` with `source.md` (frontmatter: pr_url, pr_number, repo, pr_title, pr_type, started, cycle, status: in-progress) and `lenses/` subdirs.

### Phase 3: Identify Idle Workers

1. Read `$FLEET_STATE_PATH` (defaults to `$CLAUDLOBBY_ROOT/state/fleet-state.json`).
2. Filter: `status == "idle"` and `current_task == null`. Exclude self.
3. Zero workers: note prior cycle results if any, post status, stop.

### Phase 4: Dispatch Lenses

#### Lens Table

| Lens | Skill | Applies To | Status |
|------|-------|-----------|--------|
| Adversarial Review | `/adversarial-review` | plan, implementation, mixed | Active |
| First Principles | `/first-principles` | plan, mixed | Active |
| Extension Check | `/extension-check` | implementation, mixed | Active |
| Precedent Check | `/precedent-check` | plan, implementation, mixed | Active |
| Plan Health Audit | `/plan-health-audit` | plan, mixed | Active |
| Cost-Benefit | `/cost-benefit` | plan, implementation, mixed | Active |

Only dispatch lenses whose `Applies To` matches the PR type. New lenses plug in by adding a row.

#### Dispatch Sequence

Round-robin across idle workers (no worker gets two before all have one):

1. Create lens subdir and write `dispatch.md` with: `[BOTCOMMAND]` header, source/result paths, result format from `result-format.md` **verbatim**, explicit instruction to write findings only to result path (no PR posts, no issues), and `report-back.sh` instruction.
2. Dispatch via `dispatch-task.sh` or two-step tmux: `tmux send-keys -t <worker> "set +H; cat $DISPATCH_FILE | claude"` / `sleep 0.3` / `tmux send-keys -t <worker> Enter`.
3. Update `fleet-state.json`: worker status `working`, current_task `ironclad:<lens>`.

### Phase 5: Collect Results

Monitor for `[BOTREPORT]` messages. On `completed` with `skill:ironclad-lens`: read `result.md`. On `failed`/`blocked`: queue for retry. Timeout: `$OBSERVABILITY_DISPATCH_DEADLINE` (default 1800s).

### Phase 6: Retry Failed Lenses

One retry per lens on a different worker. If retry fails, record failure and proceed with partial results.

### Phase 7: Aggregate and Deduplicate

Read all `result.md` files. Deduplicate (same file/line + same concern = keep higher severity, note both lenses). Preserve lens attribution. Sort: Blockers, Risks, Gaps, Questions, Observations. Omit empty sections.

### Phase 8: Post to PR

**Cycle 2+:** minimize prior `/ironclad` comments via GraphQL `minimizeComment(input: {subjectId: "<node-id>", classifier: OUTDATED})`.

Post single aggregated comment via `gh pr comment`:

```
## Ironclad Review: [PR Title]
Cycle: N | PR type: X | Lenses: completed/failed
[merged findings by severity section, empty sections omitted]
---
*Reviewed by /ironclad — cycle N, timestamp*
```

### Phase 9: Convergence Check

Source of truth: scratch directory, not PR comments.

- **Plan/mixed PRs:** converged when zero open Blockers AND all forks locked (scan PR comments for `[FORK-LOCK FN]` / `[FORK-REOPEN FN]` patterns per decision-fork-lifecycle protocol).
- **Implementation PRs:** converged when zero open Blockers.
- **Partial lens failure does not block convergence.**

**Converged:** update `source.md` status to `hardened`, post `[IRONCLAD] PR reviewed — no open blockers. Ready for merge.`, report-back completed.

**Not converged:** post open items summary (unresolved blockers, open forks, failed lenses), report-back completed with item count.

---

## Constraints

- Read-only on the PR — never modifies files, only posts comments.
- Centralized posting — workers write to scratch dir, only `/ironclad` posts to PR.
- No self-dispatch. No merge. Idempotent cycles.
