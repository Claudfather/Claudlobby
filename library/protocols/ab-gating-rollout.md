---
title: A/B Gating Rollout
description: The pre-land evidence gate — a change whose justification is a measurable effect merges on a version-pinned verdict from the run-eval skill, not on argument. Complements canary-rollout, which governs deployment after the decision.
requires:
  skills: [run-eval]
---

# A/B Gating Rollout

A change whose justification is a measurable effect lands on evidence, not argument. The gate fires when the PR body states a number, a percentage or a comparative ("cheaper", "faster", "fewer tokens"), when a protocol, prompt, model or library change exists to change behaviour, or when landing it would change what the fleet spends. It does not fire for correctness fixes, refactors that claim no effect, or docs. An effect you cannot state so that an eval could refute it is no claim: drop it from the PR body and land the change on its other merits.

When the gate fires, the PR needs a verdict from the `run-eval` skill, which also says what each outcome does to the PR. A verdict counts only if its statistic was registered before the first run, no axis or threshold changed after the results, and its pins (model, artifact, battery and harness versions) still match the running system. **INCONCLUSIVE never passes the gate.**
