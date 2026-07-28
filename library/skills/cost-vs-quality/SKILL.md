---
name: cost-vs-quality
description: "Decide whether a cheaper configuration is actually cheaper — measure spend and quality on the SAME runs and report the frontier, so a saving that quietly degrades output is visible rather than banked. Dry-run by default; real runs are opt-in."
argument-hint: "[--dry-run|--real] [--variants <a,b>] [--reps N]"
---

# Cost vs Quality

Answer "is the cheap option good enough?" with evidence instead of vibes.

A cost number alone is not a saving — you can always spend less by doing worse. **The only honest unit is cost paired with quality, measured on the same runs.** This skill produces that pair and reports the frontier.

Sibling of `run-eval`: that one asks *does the claimed effect exist*, this one asks *what did the effect cost us elsewhere*. Both implement `eval-workflow`; both feed `ab-gating-rollout`.

**Default is `--dry-run`.** Real runs cost money.

## When to use

- Choosing a model tier for a role (does Sonnet hold quality for reviewers?).
- Weighing a protocol or prompt change that trades output for spend.
- Auditing a config already in production on the suspicion it is quietly cheap-and-worse.

**When not to use:** when quality is not measurable for the task. If you cannot state a quality rubric a second party could apply, this skill will produce a cost number wearing a quality costume. Say so instead.

## The rule this exists to enforce

**A cost win with an unmeasured quality effect is not a result.** It is the most common way a fleet degrades: each change saves a little, nobody measures output, and the decline is invisible because no single step owned it.

So: **refuse to report a saving without a quality number beside it.** If quality could not be measured, report the cost *and* say the quality effect is unknown — never let the saving stand alone.

## Step 1: Pre-register both axes

Per `eval-workflow`, before any run:

| Axis | What to fix in advance |
|------|------------------------|
| **Cost** | Which measure — `cost_weighted_total` for token spend (not raw output tokens; standing cache is real money). Threshold or direction. |
| **Quality** | The rubric, written out, applied blind to which variant produced the output. The **minimum acceptable** level — the floor, not a preference. |

Quality is a **floor, not a co-equal trade**. State it as "quality must not drop below X"; then among variants that clear the floor, cheaper wins. This ordering is what stops a large saving from buying its way past a real degradation.

Pin the battery and the versions. Same rules as `run-eval`.

## Step 2: Measure both on the same runs

Non-negotiable: cost and quality come from the **same** executions. Measuring cost on one run and quality on another compares two different things and hides exactly the correlation you are looking for.

- Cost via `lib/transcript-usage.py` (`cost_weighted_total`).
- Quality via the pre-registered rubric, scored **blind** to variant. If a model or a person scores knowing which arm produced the output, the score is contaminated.

Run variants paired per task and rep, as in `run-eval`.

## Step 3: Report the frontier

Never a single number. A table, one row per variant:

| Variant | Cost (axis, with CI) | Quality (rubric, with CI) | Clears floor? |
|---------|---------------------|---------------------------|---------------|

Then the recommendation, stated as a decision rather than a datum:

- **Cheaper and clears the floor** → recommend it, with the interval.
- **Cheaper and below the floor** → **not a saving.** Say so plainly; do not report the cost win as the headline with quality in a footnote.
- **Quality indistinguishable, cost materially lower** → recommend, and say how much of the "indistinguishable" is genuinely tight CI versus a rubric too blunt to separate them.
- **Inconclusive on either axis** → inconclusive overall. Do not bank the half that came out well.

An unmeasured axis is reported as unmeasured, never as neutral.

## Modes

| Mode | Behaviour |
|------|-----------|
| `--dry-run` (**default**) | Zero model calls. Synthesized inputs through the real measurement, scoring, and reporting path. CI-safe. |
| `--real` | Requires `AB_EVAL_REAL=1` in the environment *and* the flag. Spends real quota. |

Size the matrix (`variants x tasks x reps`) against the fleet budget ceiling **before** starting, and confirm the estimate with the user. Cost-vs-quality runs are especially prone to matrix creep — every extra variant multiplies.

## Honesty rules

- **Never report a saving without its quality number.** This is the whole point of the skill.
- Score quality blind to variant.
- Do not move the quality floor after seeing which variant missed it.
- Report the rubric alongside the verdict so a reader can judge whether it measures what they care about.
- A verdict whose pins no longer match the running system is **expired** — model behaviour moves, and a tier that held quality six weeks ago may not today.
