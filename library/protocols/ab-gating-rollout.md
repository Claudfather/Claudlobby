---
title: A/B Gating Rollout
description: The pre-land evidence gate — a change claiming a measurable effect does not merge on argument, it merges on a version-pinned A/B verdict. Complements canary-rollout, which governs deployment after the decision.
---

# A/B Gating Rollout

Some changes claim an *effect*: this protocol saves tokens, this model is cheaper at equal quality, this refactor is faster. A claim like that is a factual assertion about behaviour, and the fleet's rule for factual assertions is the same everywhere — **evidence, not argument**.

This protocol is the gate that enforces it before the change lands. It consumes a verdict produced per `eval-workflow`.

## Where this sits

Two gates, two lifecycle points, easy to confuse:

| Gate | Question | When |
|------|----------|------|
| **A/B gating** (this protocol) | *Should this land at all?* Does the claimed effect exist? | **Before merge** |
| **`canary-rollout`** | *Is it safe to roll what we already decided to land?* | **After merge, before fleet-wide** |

They are not alternatives and neither covers the other. An A/B verdict says a change does what it claims; it says nothing about whether deploying it to 17 bots at once will melt something. A canary says the rollout is survivable; it says nothing about whether the change was worth making. A change claiming an effect and shipping fleet-wide passes **both**.

## When this gate fires

It fires when a change's *justification* is a measurable effect. Signals:

- The PR body contains a number, a percentage, or a comparative ("cheaper", "faster", "fewer tokens").
- The change is a protocol, prompt, model selection, or library edit whose whole purpose is behavioural.
- Landing it would make the fleet spend differently.

It does **not** fire for correctness fixes, refactors that claim no effect, or docs. A bug fix is justified by the bug. Demanding an A/B for everything trains people to fake one.

**If you cannot state the effect as something an eval could refute, the gate does not apply — but neither does the claim.** Drop the claim from the PR body and land it on its other merits.

## The gate

1. **Pre-register.** Target, battery, baseline, reps, verdict rule, thresholds, pins — per `eval-workflow`, before the first run. A gate registered after the results are known is not a gate.
2. **Run control vs treatment**, paired, identical in everything except the change.
3. **Compute the verdict** on the pre-registered statistic. Do not add an axis or move a threshold after seeing the data. If the pre-registration was wrong, say so explicitly and re-register — publicly, as a new run, not as a quiet edit.
4. **Attach the verdict to the PR**, with its pins and its interval.
5. **Route by outcome** — the table below.

## Outcomes

| Verdict | Meaning | What happens to the PR |
|---------|---------|------------------------|
| **PASS** | Every co-primary axis clears its bar | Gate satisfied. Proceed to review/merge on normal terms. |
| **FAIL** | An axis is measurably below its bar | Does not land on this justification. Fix the change or withdraw the claim. |
| **INCONCLUSIVE** | The effect could not be demonstrated at `--reps-max` | **Does not pass the gate.** See below. |

**INCONCLUSIVE is never rounded up to PASS.** This is the rule the whole protocol exists to hold, because it is the one under the most pressure — the work is done, the author believes it, the interval *nearly* cleared. "Directionally right" and "no evidence of harm" are not the bar. A change that cannot demonstrate its effect has not earned a landing *on that effect*.

What INCONCLUSIVE permits: landing on a **different, stated** justification (it is simpler, it removes a dependency) with the effect claim **removed from the PR body**. What it does not permit: landing on the original claim while calling the eval noisy.

## Co-primary axes

Where a change has more than one cost, gate on all of them, and require **all** to clear. #729's precedent: `protocol_sensitive` and `cost_weighted_total`. A protocol that cuts visible chat while inflating standing cache has not saved anything, and a single-axis gate would have called it a win.

A regression on a secondary axis paired with a win on the primary is a **FAIL**, not a trade to be litigated in the PR thread. If the trade is genuinely worth making, that is a human's decision to record explicitly — not something a gate should absorb.

## Version-pinning and expiry

Every gate verdict records the model version, the artifact hash, the battery version, and the harness version.

**A verdict expires when its pins stop matching the running system.** A PASS against a superseded model is not weak evidence for the current one — it is evidence about a system that no longer exists. Re-run the gate or drop the claim; do not carry the old verdict forward.

This matters most for long-lived protocols: the thing that measured as a saving under one model can be a no-op or a cost under the next.

## Cost discipline

The gate is a real spend. Follow `eval-workflow`'s cost controls: CI exercises the `--dry-run` path only; real runs are opt-in behind an explicit env gate; the run matrix is sized against a per-fleet budget ceiling **before** it starts.

If the gate would cost more than the change is worth, that is itself a finding — say so and land the change without the effect claim, rather than running a matrix nobody wanted.

## Honesty rules

- **Report the verdict you got.** A gate whose owner also wants the change to land is exactly where a thumb lands on the scale.
- **Never re-run to a better number.** Reps extend on a straddling CI, per the pre-registered stopping rule — never on a disappointing result.
- **Never narrow the battery after seeing results.** Dropping the tasks where the change did badly is fabrication with extra steps.
- **Report what was not measured.** Scope boundaries are invisible in a verdict and are where the next surprise lives.
