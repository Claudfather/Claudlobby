---
title: Eval Workflow
description: How to run an evaluation that can change a decision — target, battery, baseline, reps, verdict; pre-register the statistic before you look, and version-pin what the verdict is a verdict about.
---

# Eval Workflow

An eval exists to *change a decision*. If no result would change what you ship, do not run it — you are buying a number, not an answer. This protocol is the shape of an eval that earns its cost, generalized from the #716/#729 token-efficiency work and implemented by `lib/ab-comms-eval.sh` + `lib/ab-comms-verdict.py`.

The discipline in one line: **decide what would convince you before you look.**

## The five parts

Every eval names all five. An eval missing one of them cannot produce a verdict anyone should act on.

| Part | The question it answers | Failure if you skip it |
|------|------------------------|------------------------|
| **Target** | What single property is under test? | You measure a bundle and cannot attribute the result. |
| **Battery** | What tasks exercise that property? | You measure the tasks, not the property. |
| **Baseline** | Compared against what, run how? | A number with nothing to be better *than*. |
| **Reps** | How many paired runs, and when do you stop? | You stop when the answer looks good. |
| **Verdict** | What result means PASS, FAIL, or neither? | The result gets narrated into whatever you hoped for. |

### Target

One property, stated so a result could contradict it. "Does the token-efficiency protocol reduce tokens" is a target; "is the protocol good" is not.

Name what the target is **not**. A target that also sweeps in model version, prompt changes, and a library edit is three experiments wearing one coat, and no verdict from it attributes cleanly.

### Battery

The tasks that exercise the target, chosen *before* any run. Two rules:

- **Representative of the real workload**, not of the property. A comms-efficiency battery made only of chatty tasks measures the ceiling, not the effect you will actually get.
- **Per-task-type verdicts, never one pooled number.** Pooling hides the case where a change helps one task type and hurts another — which is the result you most need to see. `ab-comms-verdict.py` computes the pass-bar per task type for exactly this reason.

Battery content is **ratified before the run**, not tuned after. A battery edited in response to results is no longer evidence.

### Baseline

The control arm, run under conditions identical to the treatment in everything except the target. In the #729 harness this is two bots composed by the *real* `claudlobby generate`, differing only by one line in `fleet.yaml`.

**Pair the runs.** Compare treatment against control *within* a task and rep, then aggregate the paired deltas. Unpaired comparison lets task-to-task variance swamp the effect.

### Reps

Paired repetitions per task. Model behaviour is stochastic; a single pair is an anecdote.

The stopping rule is **pre-registered**, not discretionary:

- Compute a confidence interval on the effect.
- CI clears the bar → **PASS**. CI entirely below it → **FAIL**.
- CI **straddles** the bar → the eval has not answered the question. Extend reps.
- At `--reps-max`, a still-straddling result is **INCONCLUSIVE**.

"Run more reps until it passes" is the failure mode this rule exists to prevent. Reps extend on *straddle*, never on *disappointment*.

### Verdict

Three outcomes, not two. **INCONCLUSIVE is a real result and is never rounded up to PASS** — the single most important rule here. A change that cannot demonstrate its effect has not earned its landing, and "probably fine" is how an unmeasured regression ships.

Co-primary axes where a change has more than one cost. #729 gates on two — `protocol_sensitive` *and* `cost_weighted_total` — because a protocol that cuts visible chat while inflating standing cache is not a saving. **Both must clear.** A win on one axis and a regression on the other is a FAIL, not a trade to be argued in prose.

## Pre-register the statistic

Write down, before the first run: the axes, the threshold on each, the CI method and width, the seed, and the stopping rule.

The #729 implementation pins these in code — `BOOTSTRAP_N = 2000`, `BOOTSTRAP_SEED = 0`, `CI_PCT = 90` — so a dry run, a CI run, and a real run all reproduce the same arithmetic. A seeded bootstrap is not a formality: an unseeded one lets a re-run quietly produce a friendlier interval.

If the threshold is not yet ratified, the harness must emit INCONCLUSIVE **by construction** rather than defaulting to something permissive. `ab-comms-verdict.py` does this: with no `--threshold`, every task is INCONCLUSIVE and no PASS is reachable. A skeleton that can green a real gate before its bar exists is worse than no skeleton.

## Version-pin the verdict

A verdict is a statement about a *specific* system at a *specific* version. Record with every verdict:

- the model version (`--claude-version`),
- the hash of the artifact under test (`--proto-hash`),
- the battery version,
- the harness version.

Without pins, a verdict silently becomes a claim about whatever is running today. Model behaviour moves; a six-week-old PASS against an unnamed model is folklore. **A verdict whose pins no longer match the current system is expired, not weak evidence** — re-run it or drop the claim.

## Cost control

Real evals cost real money, and the run matrix multiplies: `tasks x reps x variants`. Two controls are mandatory.

- **A CI-safe `--dry-run`** that drives the real measurement and verdict path with synthesized inputs and **zero model calls**. This is what CI exercises, and it is what keeps the wiring honest between real runs.
- **An opt-in real mode** behind an explicit env gate (`AB_EVAL_REAL=1`), never a flag that a stray invocation can trip.

Set a **per-fleet budget ceiling** on real runs and check it before the matrix starts, not after. An eval that discovers its cost at the end has already spent it. Size the matrix first — `tasks x reps x variants x tokens-per-run` — and if the number is uncomfortable, cut reps or battery *before* running, which is also a decision that belongs on the record.

## Reporting

Report the verdict with its evidence and its pins, and report INCONCLUSIVE as plainly as PASS. State the effect size and interval, not just the outcome: "median 12% reduction, 90% CI [4%, 19%], PASS at T=3%" is actionable; "PASS" alone is not.

Report what was **not** measured. Every eval has a scope boundary, and the reader cannot see it from the number.

## Interaction with other protocols

- **`ab-gating-rollout`** — the pre-land gate that consumes this workflow's verdict. This protocol says how to produce a trustworthy verdict; that one says what a verdict does to a PR.
- **`canary-rollout`** — the post-decision deployment gate. Different lifecycle point: an eval decides *whether* to land, a canary decides *how* to roll what already landed. Neither substitutes for the other.
