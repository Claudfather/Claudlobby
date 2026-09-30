---
title: Fleet Dispatch Capability
description: Overrides the clauDNA /ironclad skill's subagent dispatch with canonical fleet tasks and assignments when composed onto a fleet bot
---

# Fleet Dispatch Capability

<!-- Maintainer note: the literal token `fleet-dispatch-capability` must remain in this body. The clauDNA /ironclad skill detects this protocol by substring-matching the composed CLAUDE.md; renaming the token silently breaks the override handshake. -->

This is the **`fleet-dispatch-capability`** protocol. The `/ironclad` skill is subagent-only and lives in the clauDNA plugin (`claudna:ironclad`), not in this repo; its dispatch preamble checks your composed `CLAUDE.md` for a `fleet-dispatch-capability` protocol and follows it **instead of** its subagent dispatch. This protocol supplies that fleet path.

This is an override, not an addition: it **substitutes** only `/ironclad`'s dispatch step — how the lenses execute — and changes nothing else. Bots without this protocol composed in run subagents.

## What this overrides — and what it does not

This protocol replaces only the lens **execution backend**:

| Skill phase | Fleet override (this protocol) |
|-------------|-------------------------------|
| Classify the PR, build the source | unchanged — use the skill's Phase 1 |
| Scratch directory + cycle | **overridden** — persistent fleet scratch, real multi-cycle (below) |
| Dispatch lenses | **overridden** — admit, assign, and deliver fleet work to idle workers |
| Collect results | **overridden** — collect linked assignment reports and lens files |
| Retry failed lens | **overridden** — retry on a *different* worker |
| Aggregate + dedup + sort | unchanged — use the skill's Phase 7 |
| Post the aggregated comment | unchanged format; **plus** prior-comment minimization on cycle ≥ 2 (below) |
| Convergence check | unchanged — use the skill's Phase 9 |

The lens result contract, aggregation logic, comment format, `[FORK-LOCK]`/`[FORK-REOPEN]` scan, and `[IRONCLAD]` convergence marker are all the skill's — identical regardless of backend.

**Mode indicator.** Emit the skill's mode-indicator line as `Dispatching <N> lenses via fleet mode.` (the skill emits `subagent` when this protocol is absent). This is what makes a misconfigured fleet bot detectable.

## Scratch directory and cycle

Fleet scratch persists (unlike subagent mode's ephemeral `/tmp`), so `/ironclad` runs are multi-cycle in fleet mode:

```
$CLAUDLOBBY_ROOT/state/ironclad-runs/<pr-number>-<YYYYMMDD-HHMMSS>/
  source.md            # pr_url, pr_number, repo, pr_title, pr_type, started, cycle, status
  lenses/<lens>/dispatch.md   # the worker's instructions
  lenses/<lens>/result.md     # the worker's findings
```

**Cycle = (count of existing `<pr-number>-*` dirs under `state/ironclad-runs/`) + 1.** The run dir is the source of truth for the run, not the PR comments.

## Dispatch procedure

### 1. Select idle workers

Read `claudlobby --json fleet status` and inspect `data.bots[]` for observed worker availability; exclude yourself. A missing or unknown observation is not idle. If zero workers are observed idle, note any prior-cycle results, post a status comment, and stop — do not block.

### 2. Dispatch each applicable lens

Round-robin across the idle workers (no worker gets a second lens until every idle worker has one). For each lens:

1. Write `lenses/<lens>/dispatch.md` instructing the worker to read `skills/<lens>/SKILL.md`, apply it with `--dispatch` to the run's `source.md`, write findings **only** to `lenses/<lens>/result.md` (no PR posts or issues), and complete the assigned work with `--skill ironclad-lens`. Embed the lens result format the skill defines.
2. Use `claudlobby --json task admit --title "<lens outcome>" --request-id ADMIT_UUID`; inspect its canonical task ID. Then use `claudlobby --json task assign TASK_ID --bot WORKER --expected-by RFC3339_DEADLINE --request-id ASSIGN_UUID`; inspect its assignment ID. Finally use `claudlobby --json assignment deliver ASSIGNMENT_ID --file lenses/<lens>/dispatch.md --request-id DELIVER_UUID`. Each new step has its own retained UUID. `/fleet-ops` and `dispatch` own the detailed result and uncertainty rules.
3. Observe assignment acceptance and reports through the fleet read doors. Do not write a replacement task status into `fleet-state.json`.

### 3. Collect results

Read the linked assignment state and reports (`claudlobby --json assignment show ASSIGNMENT_ID` and `claudlobby --json fleet reports list`). On `completed` with `--skill ironclad-lens`, read that lens's `result.md`. On `failed`/`blocked`, queue the lens for retry. Set a concrete `--expected-by RFC3339` when assigning; an overdue observation calls for inspection, not an inferred failure.

### 4. Retry

Retry a blocked or failed lens **once, on a different observed-idle worker**.
For a blocked lens, the task is still open with a current assignment: use
`claudlobby --json task reassign TASK_ID --bot OTHER_WORKER --reason "<why>" --request-id REASSIGN_UUID`,
then deliver the successor `ASSIGNMENT_ID` with a new delivery UUID and the
retained lens instructions. Do not admit a duplicate task. A terminal failed
task cannot be reassigned; deliberately admit follow-up work, assign it to the
other worker, and deliver it with three new retained UUIDs. Name the original
task ID in the follow-up instructions, without treating it as a canonical
relationship. Resolve any uncertain write or send before deciding to retry.
If that one retry also fails, record the failure and proceed with partial
results — a single lens failure does not block aggregation or convergence.

## Prior-comment minimization (cycle ≥ 2)

Because fleet scratch persists, re-reviews increment the cycle. On cycle ≥ 2, before posting the new aggregated comment, minimize the prior `[IRONCLAD]` comments so the PR shows only the current review:

```bash
gh api graphql -f query='mutation($id:ID!){ minimizeComment(input:{subjectId:$id, classifier:OUTDATED}){ minimizedComment{ isMinimized } } }' -f id="<comment-node-id>"
```

(Subagent mode is always cycle 1 and skips this; it is fleet-only behavior, preserved here.)

## Configuration

Opt-in per bot via fleet.yaml — add to the bots that run `/ironclad` for the fleet (typically the manager):

```yaml
bots:
  manager-bot:
    protocols:
      - fleet-dispatch-capability   # /ironclad dispatches lenses to fleet workers
```

A bot without this protocol runs `/ironclad` in subagent mode. If `$FLEET_STATE_PATH` is set but this protocol is absent, the skill warns and falls back to subagent mode.
