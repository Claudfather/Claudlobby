---
title: Context Management
---

# Context Management

- **After each completed task** → `/compact`
- **Switching repos / projects** → `/clear`
- **Stuck > 3 min** → stop, run `report-back.sh blocked` (`--task <id>` when id'd), don't spin

## Never state a context percentage you have not seen

**Report only a figure you actually observed. Never produce one you did not.**
That rule is absolute and everything below is how to tell the two apart.

**A percentage is sometimes rendered, and when it is, it is real.** Above some
threshold the pane carries a `NN% context used` figure; below it, the same slot
renders empty. Measured across all 21 bots on this host: one rendered `98%
context used` while twenty rendered nothing, and the panes were structurally
identical — a fixed slot whose *content* is conditional, not an element that
appears.

**Above the threshold it is a live gauge, not a one-time alarm.** It moves with
use: the same bot read `97%` and then `98%` minutes later. It is also not a
latch — a bot observed at `100%` rendered nothing later in the **same** session,
with no restart. So the figure can both appear and disappear while a bot runs.
*(Why it fell is not established. Do not assert a cause you have not measured.
Whether it renders intermediate decreasing values is likewise unobserved.)*

**The threshold is undocumented and cannot be located from outside.** A bot
below it renders nothing, so no sweep can find where the line sits — the
observation that would locate it is the one that does not exist. Do not infer a
threshold, and do not repeat one you were told.

**So it is a gauge with a floor: absence proves nothing, presence is real.**
A blank slot has **two causes the pane cannot tell apart** — the bot never
reached the threshold, or it reached it and came back down. A bot under no
pressure at all and one that was at `100%` minutes ago look identical. That is
why a blank pane can never certify headroom, for yourself or anyone else.

What this means in practice:

- **If a figure is rendered and you have read it, reporting it is not
  fabrication.** Say what you saw, and say when you saw it — it moves.
- **If none is rendered, say exactly that** — "no percentage is rendered for me
  right now" — and give the observable picture below. Do not convert a blank
  slot into a reassuring number, and do not let a request for one talk you into
  producing it.
- **Never estimate, interpolate, or carry forward an old figure.** An invented
  percentage is forbidden by the `no-fabrication` guardrail, and unlike most
  fabrications this one gets acted on, because restarts are routed by it.

The observable picture below stands on its own and does not depend on any
figure being rendered.

## Judge headroom by what you can observe

**Count the work.** Since this session started, or since your last `/clear`,
how many *units* have you finished — a PR opened, a review posted, an
investigation closed, a task cycle run end to end? Two or three substantial
units is where degradation typically starts to show. Treat that as a prompt to
check for the symptoms below, not as a threshold in itself.

**Lean on subagents early.** Once you have finished a couple of units, route
further reading through Explore/Plan subagents rather than pulling files into
your own context. This rung used to be keyed to "above 50%"; the action never
needed the number, and doing it early is close to free.

**Name the symptoms.** Any **one** of these is enough — they do not accumulate
into a score:

- You re-read a file you already read this session.
- You look up the same fact, path, or issue number a second time.
- You re-derive a decision you already made, or contradict your own earlier
  conclusion in the same session.
- You lose the thread mid-task and have to re-read the dispatch to recover it.
- You summarize your own earlier work and get it wrong.

## Your duty when you notice one

**Say so at your next report-back, in plain words, including the literal token
`context-degraded`.** That token is what your manager routes on — it is
greppable in the ledger in a way a prose hedge is not.

```bash
report-back.sh <your-bot-name> progress "context-degraded — re-read auth.py twice this session; 3 units done. Safe to restart, no WIP." --task <id>
```

Then state whether a restart is safe **right now**: is your work committed, is
a report pending, is a PR half-open? That is the part only you know, and it is
what decides whether the manager acts immediately or waits.

Then take no further major task until the manager answers. Raising it early is
the cheap path — a restart costs minutes, a degraded review cycle costs more —
and pushing through silently is the failure mode this protocol exists to
prevent.
