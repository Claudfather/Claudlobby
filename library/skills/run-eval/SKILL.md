---
name: run-eval
description: "Run an A/B evaluation to decide whether a change's claimed effect is real — pre-register target/battery/baseline/reps/verdict, run control vs treatment paired, emit a version-pinned PASS/FAIL/INCONCLUSIVE. Dry-run by default; real runs are opt-in and cost money."
argument-hint: "[--dry-run|--real] [--target <what>] [--reps N] [--reps-max N]"
---

# Run Eval

Produce a verdict that can change a decision. Implements the `eval-workflow` protocol; the verdict it emits is what the `ab-gating-rollout` gate consumes.

**Default is `--dry-run`.** Real runs cost real money and are opt-in — see Modes.

## When to use

- A PR claims a measurable effect (cheaper, faster, fewer tokens) and needs the pre-land gate.
- You want to know whether a protocol/prompt/model change actually does what it says.

**When not to use:** correctness fixes, refactors claiming no effect, docs. If no result would change what you ship, do not run this — you are buying a number, not an answer. Say so and move on.

## Step 1: Pre-register — before any run

Write these down first. An eval registered after the results are known is not evidence.

| Field | Rule |
|-------|------|
| **Target** | One property, stated so a result could contradict it. Name what it is *not*. |
| **Battery** | Tasks chosen now, representative of the real workload. Verdicts are **per task type**, never pooled. |
| **Baseline** | Control arm identical in everything except the target. |
| **Reps** | Paired reps per task; stopping rule is the CI rule below, not your judgement. |
| **Verdict** | Axes, threshold per axis, CI method/width/seed. |
| **Pins** | Model version, artifact hash, battery version, harness version. |

If a threshold is not yet ratified, the run emits **INCONCLUSIVE by construction**. Do not substitute a permissive default to get a result — a gate that can green itself before its bar exists is worse than no gate.

Restate the pre-registration back to the user and get agreement before spending anything.

## Step 2: Size the matrix — before running, not after

```
cells = tasks x reps x variants
```

Estimate tokens per cell and multiply. Check the total against the fleet's budget ceiling **now**. An eval that discovers its cost at the end has already spent it.

If the number is uncomfortable, cut reps or battery *before* running and record that you did — a narrowed battery is a scope statement the verdict has to carry.

## Step 3: Run

Existing assets — use them, do not rebuild:

| Asset | Role |
|-------|------|
| `lib/ab-comms-eval.sh` | Two-variant fixture composed by real `generate`; paired task x rep x variant matrix |
| `lib/transcript-usage.py` | The measurement — `protocol_sensitive` + `cost_weighted_total` axes |
| `lib/ab-comms-verdict.py` | Paired deltas, seeded bootstrap CI, the pass-bar |

Run control and treatment **paired** — compare within a task and rep, then aggregate the paired deltas. Unpaired comparison lets task variance swamp the effect.

## Step 4: Verdict

Three outcomes. Apply the pre-registered rule; do not add an axis or move a threshold after seeing data.

- **PASS** — every co-primary axis clears its bar.
- **FAIL** — an axis is measurably below its bar.
- **STRADDLE** — CI spans the bar; extend reps up to `--reps-max`.
- **INCONCLUSIVE** — still straddling at `--reps-max`.

**INCONCLUSIVE is never rounded up to PASS.** Reps extend on a straddling interval, never on a disappointing one. If more than one axis is in play, *all* must clear — a win on one and a regression on another is a FAIL, not a trade to argue in prose.

## Step 5: Report

Report the effect size **and** its interval, with pins:

```
median 12% reduction, 90% CI [4%, 19%], PASS at T=3%
pins: claude-<version> · proto <hash> · battery v<n> · harness v<n>
```

Also report **what was not measured** — every eval has a scope boundary the number cannot show.

Report INCONCLUSIVE as plainly as PASS. Attach the verdict to the PR when this is running as the `ab-gating-rollout` gate.

## Modes

| Mode | Behaviour |
|------|-----------|
| `--dry-run` (**default**) | Zero model calls. Synthesized inputs driven through the **real** measurement and verdict path, so the wiring stays honest between real runs. CI-safe. |
| `--real` | Requires `AB_EVAL_REAL=1` in the environment as well as the flag. Boots real bots and spends real quota. |

Real mode is deliberately two-key: a flag alone cannot trip it, and the env gate cannot trip it without intent. Never set `AB_EVAL_REAL` on a bot's standing config.

Confirm the sized cost with the user before a real run. If the harness refuses real mode because its battery or threshold is still a stub, **report the refusal** — do not work around it.

## Honesty rules

- Report the verdict you got, including when it is not the one hoped for.
- Never re-run to a better number.
- Never narrow the battery after seeing results.
- A verdict whose pins no longer match the running system is **expired**, not weak evidence. Re-run or drop the claim.
